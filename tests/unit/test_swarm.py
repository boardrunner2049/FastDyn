from pathlib import Path

from fastdyn import swarm


def test_worker_ports_are_strided():
    ports = swarm.worker_ports(index=2, base_port=15000, port_stride=20)

    assert ports.monitor == 15040
    assert ports.mavlink_firmware == 15041
    assert ports.mavlink_gcs == 15042
    assert ports.mavcesium == 15043
    assert ports.rumoca_http == 15044
    assert ports.rumoca_ws == 15045
    assert ports.gdb == 15046
    assert ports.gps_input == 15047
    assert ports.mavcesium_url == "http://127.0.0.1:15043/mavcesium/"


def test_build_worker_plans_assigns_isolated_state(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("[Machine]\n", encoding="utf-8")

    plans = swarm.build_worker_plans(
        config=config,
        root_dir=tmp_path / "runs",
        instances=2,
        base_port=16000,
        port_stride=20,
        fastdyn_executable="/usr/bin/fastdyn",
    )

    assert len(plans) == 2
    assert plans[0].work_dir == Path(tmp_path / "runs" / "worker-000").resolve()
    assert plans[1].work_dir == Path(tmp_path / "runs" / "worker-001").resolve()
    assert plans[0].ports.monitor == 16000
    assert plans[1].ports.monitor == 16020
    assert plans[0].env["GPS_INPUT_PORT"] == "16007"
    assert plans[1].env["GPS_INPUT_PORT"] == "16027"
    assert plans[0].env["FASTDYN_QEMU_MEMORY_DIR"].endswith("worker-000/qemu-memory")
    assert plans[1].env["FASTDYN_QEMU_MEMORY_DIR"].endswith("worker-001/qemu-memory")
    assert plans[0].command == [
        "/usr/bin/fastdyn",
        "run",
        "-c",
        str(config.resolve()),
        "-o",
        str((tmp_path / "runs" / "worker-000").resolve()),
        "--no-build-fmu",
    ]


def test_check_port_availability_rejects_overlapping_stride(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("[Machine]\n", encoding="utf-8")

    try:
        swarm.build_worker_plans(
            config=config,
            root_dir=tmp_path / "runs",
            instances=2,
            base_port=16000,
            port_stride=6,
            fastdyn_executable="/usr/bin/fastdyn",
        )
    except swarm.SwarmError as exc:
        assert "--port-stride" in str(exc)
    else:
        raise AssertionError("expected overlapping stride to be rejected")


def test_smoke_worker_plans_stops_live_workers(tmp_path):
    plan = swarm.WorkerPlan(
        index=0,
        count=1,
        work_dir=tmp_path / "worker-000",
        log_path=tmp_path / "worker-000" / "fastdyn.log",
        command=["sh", "-c", "sleep 5"],
        env={},
        ports=swarm.worker_ports(0, 18000, 20),
    )

    swarm.smoke_worker_plans([plan], jobs=1, smoke_sec=0.25, echo=lambda _msg: None)

    assert plan.log_path.exists()

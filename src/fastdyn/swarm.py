"""Process-level FastDyn swarm runner for parallel campaigns."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
from typing import Callable


class SwarmError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerPorts:
    monitor: int
    mavlink_firmware: int
    mavlink_gcs: int
    mavcesium: int
    rumoca_http: int
    rumoca_ws: int
    gdb: int
    gps_input: int

    @property
    def mavcesium_url(self) -> str:
        return f"http://127.0.0.1:{self.mavcesium}/mavcesium/"

    def as_env(self) -> dict[str, str]:
        return {
            "FASTDYN_MONITOR_PORT": str(self.monitor),
            "FASTDYN_MAVLINK_FIRMWARE_PORT": str(self.mavlink_firmware),
            "FASTDYN_MAVLINK_GCS_PORT": str(self.mavlink_gcs),
            "FASTDYN_MAVCESIUM_PORT": str(self.mavcesium),
            "FASTDYN_RUMOCA_HTTP_PORT": str(self.rumoca_http),
            "FASTDYN_RUMOCA_WS_PORT": str(self.rumoca_ws),
            "FASTDYN_GDB_PORT": str(self.gdb),
            "GPS_INPUT_PORT": str(self.gps_input),
        }


@dataclass(frozen=True)
class WorkerPlan:
    index: int
    count: int
    work_dir: Path
    log_path: Path
    command: list[str]
    env: dict[str, str]
    ports: WorkerPorts


@dataclass
class ActiveWorker:
    plan: WorkerPlan
    process: subprocess.Popen
    log_handle: object


@dataclass(frozen=True)
class WorkerResult:
    plan: WorkerPlan
    returncode: int


def worker_ports(index: int, base_port: int, port_stride: int) -> WorkerPorts:
    if index < 0:
        raise SwarmError("worker index must be non-negative")
    if port_stride < 10:
        raise SwarmError("--port-stride must be at least 10")
    start = base_port + index * port_stride
    if start < 1 or start + 7 > 65535:
        raise SwarmError(
            f"worker {index} port range {start}-{start + 7} is outside the valid TCP/UDP range"
        )
    return WorkerPorts(
        monitor=start,
        mavlink_firmware=start + 1,
        mavlink_gcs=start + 2,
        mavcesium=start + 3,
        rumoca_http=start + 4,
        rumoca_ws=start + 5,
        gdb=start + 6,
        gps_input=start + 7,
    )


def build_worker_plans(
    *,
    config: str | Path,
    root_dir: str | Path,
    instances: int,
    base_port: int,
    port_stride: int,
    fastdyn_executable: str,
    runner: str = "run",
    fmu: str | None = None,
    no_run_processes: bool = False,
) -> list[WorkerPlan]:
    if instances < 1:
        raise SwarmError("--instances must be at least 1")
    if runner not in ("run", "loop"):
        raise SwarmError("runner must be 'run' or 'loop'")

    config_path = Path(config).expanduser().resolve()
    root_path = Path(root_dir).expanduser().resolve()
    plans: list[WorkerPlan] = []

    for index in range(instances):
        work_dir = root_path / f"worker-{index:03d}"
        ports = worker_ports(index, base_port, port_stride)
        command = [
            fastdyn_executable,
            runner,
            "-c",
            str(config_path),
            "-o",
            str(work_dir),
            "--no-build-fmu",
        ]
        if fmu:
            command.extend(["--fmu", fmu])
        if no_run_processes:
            command.append("--no-run-processes")

        env = {
            "FASTDYN_INSTANCE_INDEX": str(index),
            "FASTDYN_INSTANCE_COUNT": str(instances),
            "FASTDYN_WORKER_ID": f"{index:03d}",
            "FASTDYN_SWARM_ROOT": str(root_path),
            "FASTDYN_QMP_SOCKET": str(work_dir / "qmp.sock"),
            "FASTDYN_QEMU_MEMORY_DIR": str(work_dir / "qemu-memory"),
            **ports.as_env(),
        }
        plans.append(
            WorkerPlan(
                index=index,
                count=instances,
                work_dir=work_dir,
                log_path=root_path / "logs" / f"worker-{index:03d}.log",
                command=command,
                env=env,
                ports=ports,
            )
        )

    return plans


def _can_bind_tcp(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _can_bind_udp(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def check_port_availability(plans: list[WorkerPlan]) -> None:
    used: dict[int, str] = {}
    errors: list[str] = []
    for plan in plans:
        tcp_ports = {
            "monitor": plan.ports.monitor,
            "mavcesium": plan.ports.mavcesium,
            "rumoca_http": plan.ports.rumoca_http,
            "rumoca_ws": plan.ports.rumoca_ws,
            "gdb": plan.ports.gdb,
        }
        udp_ports = {
            "mavlink_firmware": plan.ports.mavlink_firmware,
            "mavlink_gcs": plan.ports.mavlink_gcs,
            "gps_input": plan.ports.gps_input,
        }

        for label, port in {**tcp_ports, **udp_ports}.items():
            owner = used.setdefault(port, f"worker {plan.index} {label}")
            if owner != f"worker {plan.index} {label}":
                errors.append(f"port {port} is assigned to both {owner} and worker {plan.index} {label}")

        for label, port in tcp_ports.items():
            if not _can_bind_tcp(port):
                errors.append(f"worker {plan.index} TCP {label} port {port} is already in use")
        for label, port in udp_ports.items():
            if not _can_bind_udp(port):
                errors.append(f"worker {plan.index} UDP {label} port {port} is already in use")

    if errors:
        raise SwarmError("\n".join(errors))


def _start_worker(plan: WorkerPlan, echo: Callable[[str], None]) -> ActiveWorker:
    plan.work_dir.mkdir(parents=True, exist_ok=True)
    plan.log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = plan.log_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    env.update(plan.env)
    process = subprocess.Popen(
        plan.command,
        cwd=Path.cwd(),
        env=env,
        start_new_session=True,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    echo(
        f"worker {plan.index:03d} started pid={process.pid} "
        f"monitor={plan.ports.monitor} mav={plan.ports.mavlink_firmware}/{plan.ports.mavlink_gcs} "
        f"web={plan.ports.mavcesium_url} log={plan.log_path}"
    )
    return ActiveWorker(plan=plan, process=process, log_handle=log_handle)


def _terminate_worker(active: ActiveWorker, timeout_sec: float = 5.0) -> None:
    if active.process.poll() is not None:
        return
    try:
        os.killpg(active.process.pid, signal.SIGINT)
        active.process.wait(timeout=timeout_sec)
        return
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        try:
            active.process.send_signal(signal.SIGINT)
            active.process.wait(timeout=timeout_sec)
            return
        except Exception:
            pass

    try:
        os.killpg(active.process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        active.process.terminate()

    try:
        active.process.wait(timeout=timeout_sec)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(active.process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except Exception:
        active.process.kill()
    active.process.wait(timeout=timeout_sec)


def run_worker_plans(
    plans: list[WorkerPlan],
    *,
    jobs: int,
    timeout_sec: float | None = None,
    stop_on_failure: bool = False,
    echo: Callable[[str], None] = print,
) -> list[WorkerResult]:
    if jobs < 1:
        raise SwarmError("--jobs must be at least 1")

    pending = list(plans)
    active: list[ActiveWorker] = []
    results: list[WorkerResult] = []
    start_time = time.monotonic()

    def launch_more() -> None:
        while pending and len(active) < jobs:
            active.append(_start_worker(pending.pop(0), echo))

    try:
        launch_more()
        while active:
            if timeout_sec is not None and time.monotonic() - start_time > timeout_sec:
                raise SwarmError(f"swarm timed out after {timeout_sec:g}s")

            still_active: list[ActiveWorker] = []
            for worker in active:
                returncode = worker.process.poll()
                if returncode is None:
                    still_active.append(worker)
                    continue
                worker.log_handle.close()
                results.append(WorkerResult(worker.plan, returncode))
                echo(
                    f"worker {worker.plan.index:03d} exited rc={returncode} "
                    f"log={worker.plan.log_path}"
                )
                if returncode != 0 and stop_on_failure:
                    pending.clear()

            active = still_active
            if stop_on_failure and any(result.returncode != 0 for result in results):
                break
            launch_more()
            if active:
                time.sleep(0.25)
    except BaseException:
        for worker in active:
            _terminate_worker(worker)
            worker.log_handle.close()
        raise

    for worker in active:
        _terminate_worker(worker)
        worker.log_handle.close()

    return results


def smoke_worker_plans(
    plans: list[WorkerPlan],
    *,
    jobs: int,
    smoke_sec: float,
    echo: Callable[[str], None] = print,
) -> None:
    if jobs < len(plans):
        raise SwarmError("--smoke-sec requires --jobs to be at least --instances")
    if smoke_sec <= 0:
        raise SwarmError("--smoke-sec must be positive")

    active: list[ActiveWorker] = []
    try:
        for plan in plans:
            active.append(_start_worker(plan, echo))

        deadline = time.monotonic() + smoke_sec
        while time.monotonic() < deadline:
            for worker in active:
                returncode = worker.process.poll()
                if returncode is None:
                    continue
                raise SwarmError(
                    f"worker {worker.plan.index:03d} exited during swarm smoke "
                    f"with rc={returncode}; log={worker.plan.log_path}"
                )
            time.sleep(0.25)

        echo(f"swarm smoke: {len(active)} workers stayed alive for {smoke_sec:g}s")
    finally:
        for worker in active:
            _terminate_worker(worker)
            worker.log_handle.close()

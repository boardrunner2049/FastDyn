from types import SimpleNamespace

import pytest

from fastdyn import mission_report as report


def msg(kind, system=1, **values):
    return SimpleNamespace(
        get_type=lambda: kind, get_srcSystem=lambda: system, **values
    )


def position(time=1000, altitude=3000, **extra):
    return msg(
        "GLOBAL_POSITION_INT",
        time_boot_ms=time,
        lat=400000000,
        lon=-860000000,
        relative_alt=altitude,
        **extra,
    )


def target(time=1000, altitude=164.0, frame=0, mask=0):
    return msg(
        "POSITION_TARGET_GLOBAL_INT",
        time_boot_ms=time,
        coordinate_frame=frame,
        type_mask=mask,
        alt=altitude,
    )


def test_target_reference_frames_and_invalid_targets():
    telemetry = report.read_telemetry(
        [
            target(),  # Home may arrive later in the log.
            msg("HOME_POSITION", altitude=149000),
            target(time=2000, altitude=8.0, frame=6),
            target(time=3000, mask=4),
            target(time=4000, frame=11),
            target(time=5000, altitude=float("nan")),
        ]
    )
    assert report.altitude_targets(telemetry) == [(1.0, 15.0), (2.0, 8.0)]
    assert report.altitude_targets(report.read_telemetry([target()])) == []


def test_control_setpoint_uses_measured_altitude_and_controller_error():
    telemetry = report.read_telemetry(
        [
            position(system=2),
            msg(
                "NAV_CONTROLLER_OUTPUT", alt_error=99.0
            ),  # No position from this vehicle yet.
            position(),
            msg("NAV_CONTROLLER_OUTPUT", alt_error=2.0),
            msg("NAV_CONTROLLER_OUTPUT", alt_error=float("nan")),
            msg("SYSTEM_TIME", time_boot_ms=5000),
            msg("NAV_CONTROLLER_OUTPUT", alt_error=99.0),  # Stale position.
        ]
    )
    assert telemetry.control_targets == [(1.0, 5.0)]


def test_rows_align_targets_by_time_and_preserve_missing_data():
    telemetry = report.read_telemetry(
        [
            position(time=1000),
            msg("HEARTBEAT", base_mode=128),
            target(time=1500, altitude=10.0, frame=3),
            position(time=2000, altitude=4000),
            msg("NAV_CONTROLLER_OUTPUT", alt_error=1.5),
            msg("MISSION_CURRENT", seq=5),
            position(time=3000, altitude=5000),
        ]
    )
    rows, start, label = report.report_rows(
        telemetry, dict(latitude_deg=40.0, longitude_deg=-86.0)
    )
    assert start == 1.0
    assert label == "Time since arming (s)"
    assert [row["time_s"] for row in rows] == [0.0, 1.0, 2.0]
    assert rows[0]["altitude_setpoint_m"] is None
    assert rows[0]["navigation_altitude_target_m"] is None
    assert rows[1]["navigation_altitude_target_m"] == 10.0
    assert rows[1]["altitude_setpoint_m"] == 5.5
    assert rows[2]["mission_item"] == 5
    assert rows[1]["east_m"] == pytest.approx(0.0)
    assert rows[1]["north_m"] == pytest.approx(0.0)


def test_rover_speed_uses_velocity_units_and_does_not_invent_altitude_setpoint():
    telemetry = report.read_telemetry(
        [position(vx=300, vy=-400), msg("NAV_CONTROLLER_OUTPUT", alt_error=0.0)],
        vehicle="rover",
    )
    assert telemetry.positions[0]["ground_speed_mps"] == 5.0
    assert telemetry.control_targets == []


def test_plane_altitude_setpoint_uses_controller_error():
    telemetry = report.read_telemetry(
        [position(altitude=92000), msg("NAV_CONTROLLER_OUTPUT", alt_error=8.0)],
        vehicle="plane",
    )
    assert telemetry.control_targets == [(1.0, 100.0)]


@pytest.mark.parametrize("vehicle", ["copter", "plane", "rover"])
def test_report_writes_plot_and_underlying_data(tmp_path, monkeypatch, vehicle):
    mission = tmp_path / "mission.waypoints"
    mission.write_text("QGC WPL 110\n0\t1\t0\t16\t0\t0\t0\t0\t40\t-86\t149\t1\n")
    monkeypatch.setattr(
        report,
        "log_messages",
        lambda _: iter(
            [
                msg("HOME_POSITION", altitude=149000),
                position(vx=300, vy=400),
                target(),
                msg("NAV_CONTROLLER_OUTPUT", alt_error=2.0),
                position(time=2000, altitude=4000, vx=0, vy=0),
                msg("NAV_CONTROLLER_OUTPUT", alt_error=1.0),
            ]
        ),
    )
    output = tmp_path / "report" / "summary.png"
    summary = report.write_report(tmp_path / "mission.tlog", mission, output, vehicle=vehicle)
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert "<svg" in output.with_suffix(".svg").read_text()
    assert "altitude_setpoint_m" in output.with_suffix(".csv").read_text()
    assert output.with_suffix(".json").exists()
    assert summary["max_relative_altitude_m"] == 4.0
    assert summary["altitude_target_samples"] == (0 if vehicle == "rover" else 2)
    assert summary["max_ground_speed_mps"] == 5.0
    assert summary["vehicle"] == vehicle
    svg = output.with_suffix(".svg").read_text()
    assert report.VEHICLE_NAMES[vehicle] + " mission summary" in svg
    assert ("Measured ground speed" if vehicle == "rover" else "Measured altitude") in svg
    if vehicle == "rover":
        assert "Control altitude setpoint (derived)" not in svg

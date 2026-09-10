import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from pymavlink import mavutil


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "virtuals/physics/flight_controllers/courbet/mavlink/mav_command_and_control.py"
)
spec = importlib.util.spec_from_file_location("mission_control", SCRIPT)
mission = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mission)


def message(kind, system=1, **fields):
    return SimpleNamespace(
        get_type=lambda: kind, get_srcSystem=lambda: system, **fields
    )


def test_readiness_from_repeated_telemetry_without_startup_text():
    state = {}
    mav = SimpleNamespace(target_system=1)
    mission.handle_runtime_message(
        mav,
        message(
            "HEARTBEAT",
            base_mode=0,
            custom_mode=0,
            type=10,
            autopilot=3,
            system_status=mavutil.mavlink.MAV_STATE_STANDBY,
        ),
        state=state,
    )
    mission.handle_runtime_message(mav, message("GPS_RAW_INT", fix_type=3), state=state)
    mission.handle_runtime_message(
        mav, message("EKF_STATUS_REPORT", flags=19), state=state
    )
    assert state["autopilot_ready"] and state["gps_ready"] and state["ekf_ready"]
    mission.handle_runtime_message(
        mav, message("EKF_STATUS_REPORT", flags=1), state=state
    )
    assert not state["ekf_ready"]


@pytest.mark.parametrize(
    "messages,completed",
    [
        (
            [
                message("MISSION_CURRENT", seq=4),
                message("GLOBAL_POSITION_INT", relative_alt=0),
            ],
            False,
        ),
        ([message("MISSION_ITEM_REACHED", seq=3)], False),
        ([message("MISSION_ITEM_REACHED", system=2, seq=4)], False),
        ([message("MISSION_ITEM_REACHED", seq=4)], True),
    ],
)
def test_waypoint_completion_requires_arrival_from_target_vehicle(
    monkeypatch, messages, completed
):
    ticks = iter(range(20))
    monkeypatch.setattr(mission.time, "monotonic", lambda: next(ticks))
    incoming = iter(messages)
    mav = SimpleNamespace(target_system=1, recv_match=lambda **_: next(incoming, None))
    if completed:
        mission.monitor(mav, 5, 5, exit_on_complete=True, completion="waypoints")
    else:
        with pytest.raises(RuntimeError, match="mission did not complete"):
            mission.monitor(mav, 5, 5, exit_on_complete=True, completion="waypoints")


@pytest.mark.parametrize("landed,completed", [(None, False), (2, False), (1, True)])
def test_landing_requires_firmware_touchdown_report(monkeypatch, landed, completed):
    messages = [message("MISSION_CURRENT", seq=5),
                message("GLOBAL_POSITION_INT", relative_alt=500)]
    if landed is not None:
        messages.append(message("EXTENDED_SYS_STATE", landed_state=landed))
    ticks = iter(range(30))
    monkeypatch.setattr(mission.time, "monotonic", lambda: next(ticks))
    incoming = iter(messages)
    mav = SimpleNamespace(target_system=1, recv_match=lambda **_: next(incoming, None))
    if completed:
        mission.monitor(mav, 6, 10, exit_on_complete=True)
    else:
        with pytest.raises(RuntimeError, match="mission did not complete"):
            mission.monitor(mav, 6, 10, exit_on_complete=True)

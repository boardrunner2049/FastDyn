"""Protocol regressions for the separate ArduPilot and PX4 mission helpers."""

import importlib.util
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest
from pymavlink import mavutil

from fastdyn import runtime_config


ROOT = Path(__file__).resolve().parents[2]
HELPERS = ROOT / "virtuals/physics/flight_controllers/courbet/mavlink"
MAV = mavutil.mavlink


def load_helper(name):
    spec = importlib.util.spec_from_file_location(name, HELPERS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


px4 = load_helper("mav_command_control_px4")
ardu = load_helper("mav_command_control_ardupilot")


class Clock:
    def __init__(self):
        self.now = 10.0

    def monotonic(self):
        return self.now


class Message(SimpleNamespace):
    def get_type(self):
        return self.kind

    def get_srcSystem(self):
        return getattr(self, "system", 1)

    def get_srcComponent(self):
        return getattr(self, "component", 1)


def msg(kind, **fields):
    return Message(kind=kind, **fields)


def heartbeat(*, armed=False, mission=False, **fields):
    return msg("HEARTBEAT", type=MAV.MAV_TYPE_QUADROTOR,
               autopilot=MAV.MAV_AUTOPILOT_PX4,
               base_mode=MAV.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED |
               (MAV.MAV_MODE_FLAG_SAFETY_ARMED if armed else 0),
               custom_mode=(4 << 16 | 4 << 24) if mission else 0, **fields)


class Endpoint:
    """Decode actual pymavlink output bytes, with scripted autopilot replies."""

    def __init__(self, clock):
        self.clock = clock
        self.source_system = 255
        self.source_component = MAV.MAV_COMP_ID_MISSIONPLANNER
        self.target_system = self.target_component = 1
        self.mav = MAV.MAVLink(self, srcSystem=self.source_system, srcComponent=self.source_component)
        self.decoder = MAV.MAVLink(None)
        self.incoming = []
        self.sent = []
        self.react = lambda message: None
        self.closed = False

    def write(self, data):
        message = self.decoder.parse_char(data)
        assert message is not None
        self.sent.append(message)
        self.react(message)
        return len(data)

    def recv_match(self, **kwargs):
        self.clock.now += 0.1
        return self.incoming.pop(0) if self.incoming else None

    def close(self):
        self.closed = True

    def sent_type(self, kind):
        return [message for message in self.sent if message.get_type() == kind]


@pytest.fixture
def endpoint(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(px4, "time", clock)
    monkeypatch.setattr(ardu, "time", clock)
    return Endpoint(clock)


def connected(endpoint):
    link = px4.PX4Link(endpoint)
    link.system = link.component = 1
    return link


def mission_peer(endpoint, *, after_upload=lambda: None):
    count = 0

    def react(message):
        nonlocal count
        kind = message.get_type()
        if kind == "MISSION_CLEAR_ALL":
            endpoint.incoming.append(msg("MISSION_ACK", type=MAV.MAV_MISSION_ACCEPTED))
        elif kind == "MISSION_COUNT":
            count = message.count
            # A delayed clear ACK must not count as a successful upload.
            endpoint.incoming.extend([msg("MISSION_ACK", type=MAV.MAV_MISSION_ACCEPTED),
                                      msg("MISSION_REQUEST_INT", seq=0)])
        elif kind in ("MISSION_ITEM", "MISSION_ITEM_INT"):
            if message.seq + 1 < count:
                # Exercise both wire formats in the same upload.
                request = "MISSION_REQUEST" if message.seq % 2 == 0 else "MISSION_REQUEST_INT"
                endpoint.incoming.append(msg(request, seq=message.seq + 1))
            else:
                endpoint.incoming.append(msg("MISSION_ACK", type=MAV.MAV_MISSION_ACCEPTED))
                after_upload()

    return react


def test_ardupilot_keeps_home_and_px4_starts_at_takeoff(endpoint):
    path = HELPERS / "copter_mission.waypoints"
    endpoint.react = mission_peer(endpoint)
    count = ardu.upload_mission(endpoint, path, 10)
    assert count == 6
    assert endpoint.sent_type("MISSION_ITEM_INT")[0].command == MAV.MAV_CMD_NAV_WAYPOINT

    endpoint.sent.clear()
    items = px4.load_mission(path)
    px4.upload_mission(connected(endpoint), items, 10)
    assert endpoint.sent_type("MISSION_COUNT")[0].count == 5
    first = endpoint.sent_type("MISSION_ITEM_INT")[0]
    assert first.seq == 0
    assert first.command == MAV.MAV_CMD_NAV_TAKEOFF
    assert first.frame == MAV.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT
    assert (first.x, first.y) == (404149290, -869323870)
    assert endpoint.sent_type("MISSION_ITEM")[0].frame == MAV.MAV_FRAME_GLOBAL_RELATIVE_ALT
    assert len(px4.load_mission(path, keep_home=True)) == 6


@pytest.mark.parametrize("value", [0.0, 1.0, -1.0, 1100.0])
def test_px4_int_parameter_encoding_on_wire(endpoint, value):
    def react(message):
        if message.get_type() == "PARAM_REQUEST_READ":
            endpoint.incoming.append(msg("PARAM_VALUE", param_id="TEST_INT", param_value=0.0,
                                         param_type=MAV.MAV_PARAM_TYPE_INT32))
        elif message.get_type() == "PARAM_SET":
            endpoint.incoming.append(msg("PARAM_VALUE", param_id="TEST_INT", param_value=message.param_value,
                                         param_type=MAV.MAV_PARAM_TYPE_INT32))
    endpoint.react = react
    px4.set_parameter(connected(endpoint), "TEST_INT", value)
    sent = endpoint.sent_type("PARAM_SET")[0]
    assert sent.param_type == MAV.MAV_PARAM_TYPE_INT32
    assert struct.unpack("<i", struct.pack("<f", sent.param_value))[0] == int(value)


@pytest.mark.parametrize("value", [1.5, float("nan"), float("inf"), float(2**31)])
def test_px4_rejects_invalid_integer_values(value):
    with pytest.raises(ValueError):
        px4.encode_parameter(value, MAV.MAV_PARAM_TYPE_INT32)


def test_px4_float_parameter_and_unconfirmed_write(endpoint):
    def react(message):
        if message.get_type() in ("PARAM_REQUEST_READ", "PARAM_SET"):
            endpoint.incoming.append(msg("PARAM_VALUE", param_id="TEST_FLOAT", param_value=0.0,
                                         param_type=MAV.MAV_PARAM_TYPE_REAL32))
    endpoint.react = react
    with pytest.raises(TimeoutError, match="not confirmed"):
        px4.set_parameter(connected(endpoint), "TEST_FLOAT", 1.25, timeout=0.5)
    sent = endpoint.sent_type("PARAM_SET")[0]
    assert sent.param_type == MAV.MAV_PARAM_TYPE_REAL32
    assert sent.param_value == 1.25


def test_px4_filters_other_vehicles_and_components(endpoint):
    link = connected(endpoint)
    endpoint.incoming.extend([heartbeat(armed=True, system=2), heartbeat(armed=True, component=100),
                              msg("COMMAND_ACK", command=400, result=0, target_system=254),
                              heartbeat()])
    assert link.receive() is None
    assert link.receive() is None
    assert link.receive() is None
    assert link.latest("HEARTBEAT") is None
    assert link.receive().get_type() == "HEARTBEAT"


def test_px4_connect_rejects_ardupilot_and_closes(endpoint, monkeypatch):
    hb = heartbeat()
    hb.autopilot = MAV.MAV_AUTOPILOT_ARDUPILOTMEGA
    endpoint.incoming.append(hb)
    monkeypatch.setattr(mavutil, "mavlink_connection", lambda *args, **kwargs: endpoint)
    with pytest.raises(RuntimeError, match="Expected PX4"):
        px4.connect("test", 1)
    assert endpoint.closed


def test_px4_missing_bridge_times_out_and_keeps_gcs_alive(endpoint, monkeypatch):
    monkeypatch.setattr(mavutil, "mavlink_connection", lambda *args, **kwargs: endpoint)
    with pytest.raises(TimeoutError, match="MAVLink bridge"):
        px4.connect("test", 2.5)
    assert len(endpoint.sent_type("HEARTBEAT")) >= 3
    assert endpoint.closed


def test_px4_readiness_requires_fresh_estimator_validity(endpoint):
    link = connected(endpoint)
    endpoint.incoming.extend([msg("GPS_RAW_INT", fix_type=3), msg("GLOBAL_POSITION_INT"),
                              msg("ESTIMATOR_STATUS", flags=MAV.ESTIMATOR_ATTITUDE)])
    with pytest.raises(TimeoutError, match="readiness"):
        px4.wait_ready(link, 0.5)
    assert not endpoint.sent_type("COMMAND_LONG")


def test_px4_mission_mode_and_normal_arm_on_wire(endpoint):
    def react(message):
        if message.get_type() != "COMMAND_LONG":
            return
        endpoint.incoming.append(msg("COMMAND_ACK", command=message.command, result=MAV.MAV_RESULT_ACCEPTED))
        if message.command == MAV.MAV_CMD_DO_SET_MODE:
            endpoint.incoming.append(heartbeat(mission=True))
        elif message.command == MAV.MAV_CMD_COMPONENT_ARM_DISARM:
            endpoint.incoming.append(heartbeat(armed=True, mission=True))
    endpoint.react = react
    px4.start_mission(connected(endpoint), 5, mode_timeout=2, arm_timeout=2)
    commands = endpoint.sent_type("COMMAND_LONG")
    assert [command.command for command in commands] == [MAV.MAV_CMD_DO_SET_MODE,
                                                        MAV.MAV_CMD_COMPONENT_ARM_DISARM,
                                                        MAV.MAV_CMD_MISSION_START]
    mode, arm, start = commands
    assert int(mode.param1) & MAV.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
    assert (mode.param2, mode.param3) == (4, 4)
    assert (arm.param1, arm.param2) == (1, 0)
    assert (start.param1, start.param2) == (0, 4)


def test_px4_arm_denial_fails_without_starting_mission(endpoint):
    def react(message):
        if message.get_type() == "COMMAND_LONG":
            if message.command == MAV.MAV_CMD_DO_SET_MODE:
                endpoint.incoming.append(heartbeat(mission=True))
            else:
                endpoint.incoming.append(msg("COMMAND_ACK", command=message.command, result=MAV.MAV_RESULT_DENIED))
    endpoint.react = react
    with pytest.raises(RuntimeError, match="arming rejected"):
        px4.start_mission(connected(endpoint), 5, mode_timeout=2, arm_timeout=2)
    assert MAV.MAV_CMD_MISSION_START not in [command.command for command in endpoint.sent_type("COMMAND_LONG")]


def test_px4_upload_rejects_invalid_sequence(endpoint):
    endpoint.incoming.extend([msg("MISSION_ACK", type=0), msg("MISSION_REQUEST_INT", seq=99)])
    with pytest.raises(RuntimeError, match="out-of-range"):
        px4.upload_mission(connected(endpoint), px4.load_mission(HELPERS / "copter_mission.waypoints"), 2)


def test_px4_ground_state_is_not_mission_completion(endpoint):
    endpoint.incoming.extend([msg("MISSION_CURRENT", seq=4), heartbeat(),
                              msg("EXTENDED_SYS_STATE", landed_state=MAV.MAV_LANDED_STATE_ON_GROUND)])
    with pytest.raises(TimeoutError, match="did not finish"):
        px4.monitor(connected(endpoint), 5, 1, exit_on_complete=True)


def test_px4_no_arm_uploads_without_mode_or_start(endpoint, monkeypatch):
    endpoint.incoming.append(heartbeat())
    endpoint.react = mission_peer(endpoint)
    monkeypatch.setattr(mavutil, "mavlink_connection", lambda *args, **kwargs: endpoint)
    assert px4.main([str(HELPERS / "px4_init.param"), str(HELPERS / "copter_mission.waypoints"), "--no-arm"]) == 0
    assert {command.command for command in endpoint.sent_type("COMMAND_LONG")} == {MAV.MAV_CMD_SET_MESSAGE_INTERVAL}
    assert endpoint.closed


def test_px4_full_protocol_sequence(endpoint, monkeypatch):
    def ready():
        endpoint.incoming.extend([msg("GPS_RAW_INT", fix_type=3), msg("GLOBAL_POSITION_INT"),
                                  msg("ESTIMATOR_STATUS", flags=1 | 2 | 4 | 16 | 32)])
    upload = mission_peer(endpoint, after_upload=ready)

    def react(message):
        upload(message)
        if message.get_type() != "COMMAND_LONG":
            return
        if message.command == MAV.MAV_CMD_DO_SET_MODE:
            endpoint.incoming.append(heartbeat(mission=True))
        elif message.command == MAV.MAV_CMD_COMPONENT_ARM_DISARM:
            endpoint.incoming.append(heartbeat(armed=True, mission=True))
        elif message.command == MAV.MAV_CMD_MISSION_START:
            endpoint.incoming.extend([
                msg("COMMAND_ACK", command=message.command, result=MAV.MAV_RESULT_ACCEPTED),
                msg("EXTENDED_SYS_STATE", landed_state=MAV.MAV_LANDED_STATE_IN_AIR),
                msg("MISSION_CURRENT", seq=4),
                msg("EXTENDED_SYS_STATE", landed_state=MAV.MAV_LANDED_STATE_ON_GROUND),
                heartbeat(),
            ])
    endpoint.react = react
    endpoint.incoming.append(heartbeat())
    monkeypatch.setattr(mavutil, "mavlink_connection", lambda *args, **kwargs: endpoint)
    assert px4.main([str(HELPERS / "px4_init.param"), str(HELPERS / "copter_mission.waypoints")]) == 0
    assert endpoint.closed


def test_configs_select_correct_helper_and_px4_transport():
    for config in sorted((ROOT / "configs").glob("copter462*.toml")):
        processes = runtime_config.load_processes(config, repo_root=ROOT)
        mission = next(process for process in processes if process.name == "mission")
        assert mission.command[1].endswith("/mav_command_control_ardupilot.py")
        assert (ROOT / mission.command[1]).is_file()
    px4_config = ROOT / "configs/px4_fmu_v6c_fire_p2.toml"
    processes = {p.name: p for p in runtime_config.load_processes(px4_config, repo_root=ROOT)}
    assert processes["mission"].command[1].endswith("/mav_command_control_px4.py")
    assert any("--master=udpout:127.0.0.1:" in arg for arg in processes["mavproxy"].command)
    assert any("fastdyn_cesium" in arg for arg in processes["mavproxy"].command)


def test_legacy_entry_point_forwards_to_ardupilot(monkeypatch):
    monkeypatch.syspath_prepend(str(HELPERS))
    legacy = load_helper("mav_command_and_control")
    assert legacy.main.__module__ == "mav_command_control_ardupilot"

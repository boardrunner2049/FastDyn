"""Direction and mission completion regressions for the Betaflight RC pilot."""

import importlib.util
import io
import json
from pathlib import Path
import socket
import threading
from types import SimpleNamespace

import pytest
from pymavlink import mavutil

from fastdyn import runtime_config


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "virtuals/physics/flight_controllers/courbet/mavlink/mav_mission_betaflight.py"
spec = importlib.util.spec_from_file_location("bf_mission", SCRIPT)
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)
MAV = mavutil.mavlink


@pytest.mark.parametrize("north,east", [(30, 0), (-30, 0), (0, 30), (0, -30)])
def test_sticks_accelerate_toward_each_cardinal_waypoint(north, east):
    args = SimpleNamespace(max_speed=3, max_tilt=10, angle_limit=60, roll_sign=1, pitch_sign=1,
                           position_gain=0.3, velocity_gain=6, center_sensitivity=70, max_rate=670)
    roll, pitch = bf.horizontal_sticks(north, east, 0, 0, args)
    # With zero yaw, thrust along body +Z in the FLU plant accelerates north
    # for positive pitch and east for positive roll (world +Y is west).
    north_accel, east_accel = pitch - 1500, roll - 1500
    assert north_accel * north + east_accel * east > 0
    assert (north_accel == 0) == (north == 0)
    assert (east_accel == 0) == (east == 0)


def test_controller_brakes_at_target_instead_of_holding_velocity():
    args = SimpleNamespace(max_speed=3, max_tilt=10, angle_limit=60, roll_sign=1, pitch_sign=1,
                           position_gain=0.3, velocity_gain=6, center_sensitivity=70, max_rate=670)
    roll, pitch = bf.horizontal_sticks(0, 0, 3, -2, args)
    assert roll > 1500
    assert pitch < 1500


@pytest.mark.parametrize("waypoints", ["nan,0", "0,inf", "30", "a,0"])
def test_rejects_invalid_waypoints_before_opening_transport(waypoints, monkeypatch):
    monkeypatch.setattr(mavutil, "mavlink_connection", lambda *a, **k: pytest.fail("opened transport"))
    with pytest.raises(SystemExit) as error:
        bf.main(["--waypoints", waypoints])
    assert error.value.code == 2


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Message(SimpleNamespace):
    def get_type(self):
        return self.kind

    def get_srcSystem(self):
        return getattr(self, "system", 1)

    def get_srcComponent(self):
        return 0


class Endpoint:
    """Decode actual outgoing MAVLink frames and reply with a small flight sequence."""

    def __init__(self, clock, *, never_land=False, lose_position=False):
        self.clock = clock
        self.never_land = never_land
        self.lose_position = lose_position
        self.target_system = self.target_component = 0
        self.mav = MAV.MAVLink(self, srcSystem=250, srcComponent=190)
        self.decoder = MAV.MAVLink(None)
        self.sent = []
        self.closed = False
        self.rc = (1500, 1500, 1000, 1500, 1000, 1800)
        self.sample = 0
        self.airborne = False
        self.height = 0.0
        self.initial = True

    def heartbeat(self, *, system=1):
        armed = self.rc[4] > 1700
        return Message(kind="HEARTBEAT", autopilot=bf.BETAFLIGHT_AUTOPILOT,
                       base_mode=MAV.MAV_MODE_FLAG_SAFETY_ARMED if armed else 0,
                       custom_mode=bf.ANGLE_MODE, system=system)

    def write(self, data):
        message = self.decoder.parse_char(data)
        self.sent.append(message)
        if message.get_type() == "RC_CHANNELS_OVERRIDE":
            self.rc = tuple(getattr(message, f"chan{i}_raw") for i in range(1, 7))
            if self.rc[2] > 1700:
                self.airborne = True
                self.height = 15.0
            elif self.airborne and self.rc[2] < 1600 and not self.never_land:
                self.height = 0.0
        return len(data)

    def recv_match(self, **kwargs):
        self.clock.now += 0.02
        if self.initial:
            self.initial = False
            return self.heartbeat()
        self.sample += 1
        kind = self.sample % 5
        if kind == 0:
            return self.heartbeat()
        if kind == 1:
            return Message(kind="GPS_RAW_INT", fix_type=3)
        if kind == 2:
            if self.lose_position and self.airborne:
                return None
            return Message(kind="GLOBAL_POSITION_INT", lat=404149290, lon=-869323870,
                           alt=int((149.1 + self.height) * 1000), vx=0, vy=0, vz=0)
        if kind == 3:
            return Message(kind="ATTITUDE", roll=0.0, pitch=0.0, yaw=0.0)
        # Foreign vehicle messages must not change the selected vehicle state.
        return Message(kind="HEARTBEAT", base_mode=0, custom_mode=99, system=99)

    def close(self):
        self.closed = True


def run_peer(monkeypatch, extra_args=(), **kwargs):
    clock = Clock()
    endpoint = Endpoint(clock, **kwargs)
    monkeypatch.setattr(bf, "time", clock)
    monkeypatch.setattr(mavutil, "mavlink_connection", lambda *a, **k: endpoint)
    result = bf.main(["--waypoints", "", "--preflight", "0.05", "--stage-timeout", "2",
                      "--settle-seconds", "0.1", "--telemetry-timeout", "0.3", *extra_args])
    assert endpoint.closed
    last_rc = [m for m in endpoint.sent if m.get_type() == "RC_CHANNELS_OVERRIDE"][-1]
    assert (last_rc.chan1_raw, last_rc.chan2_raw, last_rc.chan3_raw, last_rc.chan4_raw,
            last_rc.chan5_raw) == (1500, 1500, 1000, 1500, 1000)
    assert (last_rc.target_system, last_rc.target_component) == (1, 0)
    return result, endpoint


def test_complete_takeoff_return_landing_and_disarm(monkeypatch, capsys):
    assert run_peer(monkeypatch)[0] == 0
    output = capsys.readouterr().out
    assert "stage -> CLIMB" in output
    assert "stage -> SETTLE" in output
    assert "landed and disarmed: mission complete" in output


def test_airborne_vehicle_cannot_be_reported_as_landed(monkeypatch, capsys):
    assert run_peer(monkeypatch, never_land=True)[0] == 1
    output = capsys.readouterr().out
    assert "stage LAND timed out" in output
    assert "mission complete" not in output


def test_stale_position_aborts_and_disarms(monkeypatch, capsys):
    assert run_peer(monkeypatch, lose_position=True)[0] == 1
    output = capsys.readouterr().out
    assert "lost fresh heartbeat/GPS/position/attitude" in output
    assert "mission complete" not in output


def test_default_north_mission_sends_positive_pitch_on_the_wire(monkeypatch):
    result, endpoint = run_peer(monkeypatch, extra_args=("--waypoints", "30,0"), never_land=True)
    # The peer has no horizontal motion; navigation should time out, but the
    # first command must accelerate north rather than repeat the southbound bug.
    assert result == 1
    pitch = [m.chan2_raw for m in endpoint.sent
             if m.get_type() == "RC_CHANNELS_OVERRIDE" and m.chan2_raw != 1500]
    assert pitch and pitch[0] > 1500


def test_config_routes_mission_through_mavproxy_and_propagates_failure(monkeypatch):
    for name in ("FASTDYN_MAVLINK_FIRMWARE_PORT", "FASTDYN_MAVLINK_GCS_PORT", "FASTDYN_MAVCESIUM_PORT"):
        monkeypatch.delenv(name, raising=False)
    processes = {p.name: p for p in runtime_config.load_processes(ROOT / "configs/betaflight_f405.toml", repo_root=ROOT)}
    assert processes["mission"].terminate_run_on_exit
    assert "udpin:127.0.0.1:15552" in processes["mission"].command
    assert "--master=udpout:127.0.0.1:15551" in processes["mavproxy"].command
    assert "--out=udpout:127.0.0.1:15552" in processes["mavproxy"].command


def test_config_preserves_negative_waypoint_arguments(monkeypatch):
    monkeypatch.setenv("FASTDYN_BF_WAYPOINTS", "-30,0;-30,-30;0,-30")
    processes = {p.name: p for p in runtime_config.load_processes(ROOT / "configs/betaflight_f405.toml", repo_root=ROOT)}
    assert "--waypoints=-30,0;-30,-30;0,-30" in processes["mission"].command


def test_completed_mission_publishes_exact_plan_for_viewer(monkeypatch, tmp_path):
    monkeypatch.setenv("FASTDYN_WORK_DIR", str(tmp_path))
    assert run_peer(monkeypatch)[0] == 0
    state = json.loads((tmp_path / "betaflight_mission.json").read_text())
    assert state["phase"] == "COMPLETE"
    assert state["home"] == [40.414929, -86.932387, 149.1]
    assert state["altitude"] == 15
    assert state["armed"] is False


def test_viewing_window_occurs_after_confirmed_landing(monkeypatch, capsys):
    monkeypatch.setattr(bf, "pause_completed_simulation", lambda: True)
    assert run_peer(monkeypatch, extra_args=("--viewer-seconds", "0.5"))[0] == 0
    output = capsys.readouterr().out
    assert output.index("landed and disarmed") < output.index("simulation paused after landing")
    assert "viewer remains available for 0.5 s" in output


def test_postflight_pause_uses_this_runs_qmp_socket(monkeypatch, tmp_path):
    path = tmp_path / "qmp.sock"
    config = tmp_path / "config.toml"
    config.write_text(f'[Machine]\nqmp_socket = {json.dumps(str(path))}\n')
    monkeypatch.setenv("FASTDYN_CONFIG", str(config))
    commands = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        listener.listen(1)

        def serve():
            connection, _ = listener.accept()
            with connection, connection.makefile("rwb") as stream:
                stream.write(b'{"QMP": {}}\n')
                stream.flush()
                for _ in range(2):
                    request = json.loads(stream.readline())
                    commands.append(request["execute"])
                    # QMP can interleave asynchronous events with command replies.
                    stream.write(b'{"event":"STOP"}\n')
                    stream.write((json.dumps({"return": {}, "id": request["id"]}) + "\n").encode())
                    stream.flush()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        assert bf.pause_completed_simulation()
        thread.join(timeout=2)
    assert commands == ["qmp_capabilities", "stop"]


def test_closed_viewer_port_reports_failure_instead_of_flying(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(bf, "time", clock)

    class Unavailable:
        def open(self, url, **kwargs):
            raise OSError("connection refused")

    monkeypatch.setattr(bf, "build_opener", lambda *args: Unavailable())
    with pytest.raises(RuntimeError, match="viewer unavailable at http://127.0.0.1:15500"):
        bf.wait_for_viewer("http://127.0.0.1:15500", 0.3)


def test_viewer_waits_for_completed_recording_acknowledgement(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(bf, "time", clock)
    phases = iter(["LAND", "COMPLETE"])

    class RecordingServer:
        def open(self, url, **kwargs):
            return io.BytesIO(json.dumps({"snapshot": True, "mission": {"phase": next(phases)}}).encode())

    monkeypatch.setattr(bf, "build_opener", lambda *args: RecordingServer())
    bf.wait_for_viewer("http://127.0.0.1:15500", 0.3, phase="COMPLETE")
    assert clock.now >= 0.1

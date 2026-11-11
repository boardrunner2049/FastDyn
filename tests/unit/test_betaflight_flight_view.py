"""Recording retention and the HTTP/WebSocket contract of the local flight view."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

from tornado.testing import AsyncHTTPTestCase, gen_test
from tornado.websocket import websocket_connect


ROOT = Path(__file__).resolve().parents[2]
HELPERS = ROOT / "virtuals/physics/flight_controllers/courbet/mavlink"
sys.path.insert(0, str(HELPERS))
spec = importlib.util.spec_from_file_location("flight_view", HELPERS / "fastdyn_flight_view.py")
view = importlib.util.module_from_spec(spec)
spec.loader.exec_module(view)


def position(alt=164100):
    return {"mav_data": {"mavpackettype": "GLOBAL_POSITION_INT", "lat": 404149290,
                         "lon": -869323870, "alt": alt, "vx": 300, "vy": 400, "vz": -125}}


def test_late_connection_receives_recorded_positions_and_correct_units(tmp_path):
    recording = view.FlightRecording(tmp_path)
    recording.ingest({"mav_data": {"mavpackettype": "ATTITUDE", "roll": 0.1, "pitch": 0.2, "yaw": 0.3}})
    recording.ingest(position(), now=1)
    recording.ingest(position(164200), now=2)
    snapshot = json.loads(json.dumps(recording.snapshot()))
    assert len(snapshot["history"]) == 2
    assert snapshot["history"][1]["t"] == 1
    assert snapshot["history"][0]["alt"] == 164.1
    assert snapshot["history"][0]["speed"] == 5
    assert snapshot["history"][0]["climb"] == 1.25
    assert snapshot["history"][0]["roll"] == 0.1


def test_completed_recording_does_not_grow_during_viewing_window(tmp_path):
    recording = view.FlightRecording(tmp_path)
    recording.ingest(position(), now=1)
    state = {"phase": "COMPLETE", "home": [40.4, -86.9, 149.1], "waypoints": [[30, 0]]}
    (tmp_path / "betaflight_mission.json").write_text(json.dumps(state))
    assert recording.read_mission()
    for second in range(2, 122):
        recording.ingest(position(149100), now=second)
    assert len(recording.snapshot()["history"]) == 1
    saved = json.loads((tmp_path / "betaflight_flight.json").read_text())
    assert saved["mission"] == state
    assert saved["history"][0]["alt"] == 164.1


def test_runtime_provenance_is_kept_in_saved_recordings(tmp_path, monkeypatch):
    monkeypatch.setenv('FASTDYN_CONFIG', '/runtime/config.toml')
    monkeypatch.setattr(view, 'capture_provenance', lambda path: {'config_path': 'configs/hardware.toml', 'components': []})
    recording = view.FlightRecording(tmp_path)
    recording.ingest(position(), now=1)
    (tmp_path / 'betaflight_mission.json').write_text(json.dumps({'phase': 'COMPLETE'}))
    recording.read_mission()
    saved = json.loads((tmp_path / 'betaflight_flight.json').read_text())
    assert saved['provenance']['captured_during_run'] is True
    assert saved['provenance']['config_path'] == 'configs/hardware.toml'


class TestFlightViewHTTP(AsyncHTTPTestCase):
    def get_app(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        config = SimpleNamespace(APP_PREFIX="mavcesium/", SERVER_PORT=0, SERVER_INTERFACE="127.0.0.1")
        self.owner = view.FlightViewServer(config, self.directory.name)
        self.owner.recording.ingest(position(), now=1)
        self.owner.recording.mission = {"phase": "COMPLETE", "waypoints": [[30, 0]]}
        return self.owner.application()

    def test_page_and_its_local_assets_share_the_existing_port(self):
        response = self.fetch("/mavcesium/")
        assert response.code == 200
        assert b"/mavcesium/view/flight.js" in response.body
        assert self.fetch("/mavcesium/view/flight.js").code == 200
        assert self.fetch("/mavcesium/static/Build/Cesium/Widgets/widgets.css").code == 200
        state = json.loads(self.fetch("/mavcesium/state.json").body)
        assert state["mission"]["phase"] == "COMPLETE"

    @gen_test
    async def test_reconnecting_browser_receives_history_before_any_new_packet(self):
        for _ in range(2):
            connection = await websocket_connect(self.get_url("/mavcesium/websocket/").replace("http:", "ws:"))
            state = json.loads(await connection.read_message())
            assert state["mission"]["phase"] == "COMPLETE"
            assert len(state["history"]) == 1
            connection.close()

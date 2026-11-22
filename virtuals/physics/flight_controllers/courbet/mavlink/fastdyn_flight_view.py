"""Local Cesium flight view served by the MAVProxy adapter on its existing port."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import threading
import time

import tornado.httpserver
import tornado.ioloop
import tornado.web
import tornado.websocket

from MAVProxy.modules.mavproxy_cesium.app import cesium_web_server


ASSETS = Path(__file__).with_name("flight_view")


class FlightRecording:
    """Cache telemetry for late browser connections and completed-flight replay."""

    def __init__(self, work_dir):
        self.work_dir = Path(work_dir)
        self.telemetry = {}
        self.mission = {}
        self.history = []
        self.started = None
        self.last_sample = -math.inf
        self.saved = False

    def read_mission(self):
        try:
            state = json.loads((self.work_dir / "betaflight_mission.json").read_text())
        except (OSError, ValueError):
            return False
        changed = state != self.mission
        self.mission = state
        if state.get("phase") == "COMPLETE" and not self.saved:
            try:
                (self.work_dir / "betaflight_flight.json").write_text(json.dumps(self.snapshot()))
                self.saved = True
            except OSError:
                pass
        return changed

    def ingest(self, payload, now=None):
        message = payload.get("mav_data")
        if not message:
            return None
        kind = message.get("mavpackettype")
        self.telemetry[kind] = message
        now = time.monotonic() if now is None else now
        if kind != "GLOBAL_POSITION_INT" or now - self.last_sample < 0.1 or self.mission.get("phase") == "COMPLETE":
            return None
        self.started = now if self.started is None else self.started
        self.last_sample = now
        attitude = self.telemetry.get("ATTITUDE", {})
        sample = {
            "t": now - self.started,
            "lat": message["lat"] / 1e7, "lon": message["lon"] / 1e7,
            "alt": message["alt"] / 1000.0,
            "roll": attitude.get("roll", 0), "pitch": attitude.get("pitch", 0),
            "yaw": attitude.get("yaw", 0),
            "speed": math.hypot(message["vx"], message["vy"]) / 100.0,
            "climb": -message["vz"] / 100.0,
        }
        self.history.append(sample)
        if len(self.history) > 12000:
            del self.history[:1000]
        return sample

    def snapshot(self):
        return {"snapshot": True, "telemetry": self.telemetry,
                "mission": self.mission, "history": self.history}


class FlightViewServer:
    def __init__(self, config, work_dir):
        self.config = config
        self.recording = FlightRecording(work_dir)
        self.clients = set()
        self.loop = None
        self.ready = threading.Event()
        self.error = None
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._serve, name="fastdyn-flight-view", daemon=True)
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError("flight view did not start within 10 seconds")
        if self.error:
            raise RuntimeError(f"flight view could not start: {self.error}") from self.error
        return self

    def stop(self):
        if self.loop:
            self.loop.add_callback(self.loop.stop)

    def publish(self, payload):
        if self.loop:
            self.loop.add_callback(self._publish, payload)

    def _publish(self, payload):
        sample = self.recording.ingest(payload)
        self._broadcast({**payload, **({"sample": sample} if sample else {})})

    def _broadcast(self, payload):
        for client in tuple(self.clients):
            if client.ws_connection is None:
                self.clients.discard(client)
                continue
            try:
                client.write_message(json.dumps(payload))
            except tornado.websocket.WebSocketClosedError:
                self.clients.discard(client)

    def _mission_tick(self):
        if self.recording.read_mission():
            self._broadcast({"mission": self.recording.mission})

    def application(self):
        owner = self
        prefix = "/" + self.config.APP_PREFIX

        class Index(tornado.web.RequestHandler):
            def get(self):
                self.set_header("Cache-Control", "no-store")
                self.render("index.html", prefix=prefix)

        class State(tornado.web.RequestHandler):
            def get(self):
                self.set_header("Cache-Control", "no-store")
                self.write(owner.recording.snapshot())

        class Stream(tornado.websocket.WebSocketHandler):
            def check_origin(self, origin):
                return True

            def open(self):
                self.set_nodelay(True)
                owner.clients.add(self)
                self.write_message(json.dumps(owner.recording.snapshot()))

            def on_close(self):
                owner.clients.discard(self)

        return tornado.web.Application([
            (prefix, Index),
            (prefix + "state.json", State),
            (prefix + "websocket/", Stream),
            (prefix + r"view/(.*)", tornado.web.StaticFileHandler, {"path": str(ASSETS)}),
            (prefix + r"static/(.*)", tornado.web.StaticFileHandler, {"path": cesium_web_server.APP_STATIC}),
        ], template_path=str(ASSETS))

    def _serve(self):
        asyncio.set_event_loop(asyncio.new_event_loop())
        self.loop = tornado.ioloop.IOLoop.current()
        server = tornado.httpserver.HTTPServer(self.application())
        timer = tornado.ioloop.PeriodicCallback(self._mission_tick, 200)
        try:
            server.listen(int(self.config.SERVER_PORT), address=self.config.SERVER_INTERFACE)
            self.recording.read_mission()
            timer.start()
            self.ready.set()
            self.loop.start()
        except Exception as exc:
            self.error = exc
            self.ready.set()
        finally:
            timer.stop()
            for client in tuple(self.clients):
                client.close()
            server.stop()
            self.loop.close(all_fds=True)

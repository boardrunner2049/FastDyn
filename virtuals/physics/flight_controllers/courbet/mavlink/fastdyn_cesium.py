"""FastDyn MAVCesium adapter.

FastDyn vehicle configs use WGS84 ellipsoid altitude so MAVCesium can render
positions directly. A display-only altitude offset is still accepted for older
configs, but defaults to zero.
"""

from __future__ import annotations

import os
from pathlib import Path
import time

from MAVProxy.modules.mavproxy_cesium import CesiumModule


def _write_dynamic_config(
    *,
    port: int,
    websocket_port: int | None = None,
    server_interface: str = "0.0.0.0",
    websocket_interface: str = "0.0.0.0",
) -> Path:
    websocket_port = port if websocket_port is None else websocket_port
    work_dir = Path(os.environ.get("FASTDYN_WORK_DIR", ".")).expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    config_path = work_dir / "mavcesium.ini"
    config_path.write_text(
        "\n".join(
            [
                "[general]",
                f"server_interface = {server_interface}",
                f"server_port = {port}",
                f"websocket_interface = {websocket_interface}",
                f"websocket_port = {websocket_port}",
                "app_secret_key = ''",
                "app_prefix = mavcesium/",
                "",
                "[api_keys]",
                "",
                "[debug]",
                "app_debug = 1",
                "module_debug = 1",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


class FastDynCesiumModule(CesiumModule):
    def __init__(
        self,
        mpstate,
        *,
        altitude_offset_m: float | str = 0.0,
        msl_to_wgs84_offset_m: float | str | None = None,
        port: int | str | None = None,
        server_port: int | str | None = None,
        websocket_port: int | str | None = None,
        server_interface: str = "0.0.0.0",
        websocket_interface: str = "0.0.0.0",
        flight_view: bool | str | None = None,
        **kwargs,
    ):
        if flight_view is None:
            flight_view = os.environ.get("FASTDYN_FLIGHT_VIEW", "0")
        self.flight_view = str(flight_view).lower() in ("true", "1", "yes")
        self.flight_server = None
        if msl_to_wgs84_offset_m is not None:
            altitude_offset_m = msl_to_wgs84_offset_m
        self.altitude_offset_m = float(altitude_offset_m)
        selected_port = port if port is not None else server_port
        if selected_port is not None and "configuration" not in kwargs:
            config_path = _write_dynamic_config(
                port=int(selected_port),
                websocket_port=int(websocket_port) if websocket_port is not None else None,
                server_interface=server_interface,
                websocket_interface=websocket_interface,
            )
            kwargs["configuration"] = str(config_path)
        super().__init__(mpstate, **kwargs)
        if self.flight_view:
            self.data_stream.extend(["HEARTBEAT", "GPS_RAW_INT"])
        self.description = "FastDyn Cesium map module"
        self.mpstate.console.writeln(
            "FastDyn MAVCesium display altitude offset: "
            f"{self.altitude_offset_m:g} m"
        )

    def start_server(self):
        if not self.flight_view:
            return super().start_server()
        from fastdyn_flight_view import FlightViewServer

        self.flight_server = FlightViewServer(self.config, os.environ.get("FASTDYN_WORK_DIR", ".")).start()
        self.server_thread = self.flight_server.thread

    def stop_server(self):
        if self.flight_server is not None:
            self.flight_server.stop()
        else:
            super().stop_server()

    def send_data(self, data, target=None):
        if self.flight_server is not None:
            self.flight_server.publish(data)
        else:
            super().send_data(data, target=target)

    def _display_alt_mm(self, altitude_mm: int | float) -> int:
        return int(round(float(altitude_mm) + self.altitude_offset_m * 1000.0))

    def _display_alt_m(self, altitude_m: int | float) -> float:
        return float(altitude_m) + self.altitude_offset_m

    def send_mission(self):
        self.mission = {}
        self.mission_points_to_send = self.mpstate.public_modules["wp"].wploader.wpoints
        for point in self.mission_points_to_send:
            point_dict = point.to_dict()
            seq = point_dict["seq"]
            del point_dict["seq"]
            if int(point_dict.get("frame", -1)) == 0 and "z" in point_dict:
                point_dict["z"] = self._display_alt_m(point_dict["z"])
            self.mission[seq] = point_dict
        self.send_data({"mission_data": self.mission})

    def mavlink_packet(self, m):
        if self.master.flightmode != self.flightmode:
            self.send_flightmode()

        if m.get_type() == "POSITION_TARGET_GLOBAL_INT":
            msg_dict = m.to_dict()
            self.pos_target["lat"] = msg_dict["lat_int"]
            self.pos_target["lon"] = msg_dict["lon_int"]
            self.pos_target["alt_wgs84"] = self._display_alt_m(msg_dict["alt"])

            if None not in self.pos_target.values():
                self.send_data({"pos_target_data": self.pos_target})

        if m.get_type() in self.data_stream:
            msg_dict = m.to_dict()
            if m.get_type() == "GLOBAL_POSITION_INT":
                msg_dict["alt"] = self._display_alt_mm(msg_dict["alt"])
            msg_dict["timestamp"] = m._timestamp
            self.send_data({"mav_data": msg_dict})

        last_wp_change = self.module("wp").wploader.last_change
        if self.wp_change_time != last_wp_change and abs(time.time() - last_wp_change) > 1:
            self.wp_change_time = last_wp_change
            self.send_mission()
            self.rally_change_time = time.time()

        if self.fence_change_time != self.module("fence").wploader.last_change:
            self.fence_change_time = self.module("fence").wploader.last_change
            self.send_fence()


def init(mpstate, **kwargs):
    return FastDynCesiumModule(mpstate, **kwargs)

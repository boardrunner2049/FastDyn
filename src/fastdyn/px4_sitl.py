"""Offline PX4 Simulator MAVLink boundary for the native FMI 3 plant.

This prototype does not emulate flight-controller hardware or transport
middleware. Motor ordering and the unidirectional output range must be
selected explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import select
import socket
import time

from pymavlink.dialects.v20 import common as mavlink


class Plant:
    """Reuse FastDyn's source-FMU runtime; reject incomplete FMI steps."""

    def __init__(self, path: Path):
        from fmpy import read_model_description
        from fmpy.fmi3 import FMU3Slave
        from .fmu_runtime import prepare

        runtime = prepare(path)
        model = read_model_description(str(path))
        self.variables = {v.name: v for v in model.modelVariables}
        self.time_us = 0
        self.fmu = FMU3Slave(
            guid=model.instantiationToken,
            unzipDirectory=str(runtime.library.parents[2]),
            modelIdentifier=model.coSimulation.modelIdentifier,
            instanceName="fastdyn_px4_offline",
        )
        self.fmu.instantiate()
        self.fmu.enterInitializationMode(startTime=0.0)
        self.fmu.setFloat64([self.variables["pwm"].valueReference], [1000.0] * 4)
        self.fmu.exitInitializationMode()

    def read(self, name: str) -> list[float]:
        variable = self.variables[name]
        size = math.prod(d.start for d in variable.dimensions)
        values = self.fmu.getFloat64([variable.valueReference], nValues=size)
        if not all(math.isfinite(v) for v in values):
            raise RuntimeError(f"nonfinite FMU output: {name}")
        return values

    def step(self, pwm: list[float], dt_us: int = 2000):
        if len(pwm) != 4 or not all(math.isfinite(v) and 900 <= v <= 2200 for v in pwm):
            raise ValueError("expected four finite PWM values in [900, 2200] us")
        if not isinstance(dt_us, int) or not 1 <= dt_us <= 2000:
            raise ValueError("FMI communication step must be 1..2000 integer us")
        self.fmu.setFloat64([self.variables["pwm"].valueReference], pwm)
        end_us = self.time_us + dt_us
        event, terminate, early, last = self.fmu.doStep(self.time_us / 1e6, dt_us / 1e6)
        if event or terminate or early or abs(last - end_us / 1e6) > 1e-8:
            raise RuntimeError(f"incomplete FMI step: {(event, terminate, early, last)}")
        self.time_us = end_us

    def close(self):
        try:
            self.fmu.terminate()
        finally:
            self.fmu.freeInstance()


@dataclass(frozen=True)
class MotorMapping:
    """Map each plant motor to its PX4 channel; no silent saturation."""

    channels: tuple[int, int, int, int]
    pwm_min: float = 1100.0
    pwm_max: float = 1900.0
    disarmed_pwm: float = 1000.0
    control_min: float = 0.0
    control_max: float = 1.0

    def __post_init__(self):
        if sorted(self.channels) != [0, 1, 2, 3]:
            raise ValueError("motor mapping must permute PX4 channels 0..3")
        if not 900 <= self.disarmed_pwm <= self.pwm_min < self.pwm_max <= 2200:
            raise ValueError("invalid motor PWM range")
        if not math.isfinite(self.control_min) or not math.isfinite(self.control_max):
            raise ValueError("motor control endpoints must be finite")
        if self.control_min >= self.control_max:
            raise ValueError("motor control range must be increasing")

    def convert(self, message) -> list[float]:
        if message.get_type() != "HIL_ACTUATOR_CONTROLS":
            raise ValueError("expected HIL_ACTUATOR_CONTROLS")
        if not message.mode & mavlink.MAV_MODE_FLAG_SAFETY_ARMED:
            return [self.disarmed_pwm] * 4
        controls = [message.controls[c] for c in self.channels]
        valid = all(
            math.isfinite(v) and self.control_min <= v <= self.control_max
            for v in controls
        )
        if not valid:
            raise ValueError(
                f"motor control outside [{self.control_min},{self.control_max}]"
            )
        scale = self.pwm_max - self.pwm_min
        return [self.pwm_min + (v - self.control_min) * scale /
                (self.control_max - self.control_min) for v in controls]


@dataclass(frozen=True)
class SensorStall:
    start_us: int
    duration_us: int

    def __post_init__(self):
        if self.start_us < 0 or self.duration_us < 0:
            raise ValueError("negative stall interval")

    def active(self, time_us: int) -> bool:
        return self.start_us <= time_us < self.start_us + self.duration_us


class SensorWriter:
    """MAVLink 2 simulator messages; plant time never depends on send success.

    HIL_SENSOR: FRD acceleration m/s², gyro rad/s, mag gauss, pressure hPa.
    HIL_GPS: latitude/longitude degE7, altitude mm, NED velocity cm/s.
    Flow/range and receiver identity/time validation are subsequent milestones.
    """

    def __init__(
        self,
        stream,
        *,
        geoid_separation_m: float,
        source_system: int = 1,
        gps_period_us: int = 100_000,
    ):
        if not math.isfinite(geoid_separation_m):
            raise ValueError("explicit finite WGS84-minus-MSL geoid separation required")
        if not isinstance(gps_period_us, int) or gps_period_us <= 0:
            raise ValueError("GPS period must be a positive integer number of microseconds")
        self.geoid_separation_m = geoid_separation_m
        self.mav = mavlink.MAVLink(stream, srcSystem=source_system, srcComponent=51)
        self.gps_period_us = gps_period_us
        self.next_gps_us = gps_period_us
        self.sensor_count = 0
        self.gps_count = 0

    def send(self, plant: Plant, *, imu_stall: SensorStall | None = None):
        now = plant.time_us
        if imu_stall is None or not imu_stall.active(now):
            accel, gyro, mag = (plant.read(n) for n in ("accel", "gyro", "mag"))
            pressure_pa = plant.read("baro_pressure_pa")[0]
            if pressure_pa <= 0:
                raise ValueError("nonpositive barometric pressure")
            pressure_alt_m = (1 - (pressure_pa / 101325.0) ** (1 / 5.25588)) / 2.25577e-5
            self.mav.hil_sensor_send(
                now, *accel, *gyro, *mag,
                pressure_pa / 100.0, 0.0, pressure_alt_m,
                plant.read("baro_temperature_c")[0], 0x1FFF,
            )
            self.sensor_count += 1
        # PX4's FMI communication step need not divide the GPS period.  Emit once
        # when plant time crosses a deadline, then skip missed deadlines rather
        # than bursting stale GPS samples.
        if now >= self.next_gps_us:
            lat, lon, alt = plant.read("gps")
            vn, ve, vd = plant.read("vel_ned")
            speed = math.hypot(vn, ve)
            course = round(math.degrees(math.atan2(ve, vn)) % 360 * 100) % 36000
            self.mav.hil_gps_send(
                now, 3, round(lat * 1e7), round(lon * 1e7),
                round((alt - self.geoid_separation_m) * 1000),
                100, 100, round(speed * 100), round(vn * 100), round(ve * 100),
                round(vd * 100), course, 12,
            )
            self.gps_count += 1
            while self.next_gps_us <= now:
                self.next_gps_us += self.gps_period_us


class ActuatorReceiver:
    """Parse PX4's Simulator MAVLink actuator stream without accepting commands."""

    def __init__(self, mapping: MotorMapping, *, clock=time.monotonic):
        self.mapping = mapping
        self.clock = clock
        self.parser = mavlink.MAVLink(None)
        self.last_pwm = None
        self.last_received = None
        self.message_count = 0

    def feed(self, data: bytes) -> int:
        accepted = 0
        for message in self.parser.parse_buffer(data) or ():
            if message.get_type() != "HIL_ACTUATOR_CONTROLS":
                continue
            self.last_pwm = self.mapping.convert(message)
            self.last_received = self.clock()
            self.message_count += 1
            accepted += 1
        return accepted

    def pwm(self, *, now: float | None = None, timeout_s: float = 0.1) -> list[float]:
        if timeout_s <= 0 or not math.isfinite(timeout_s):
            raise ValueError("actuator timeout must be finite and positive")
        now = self.clock() if now is None else now
        if (self.last_pwm is None or self.last_received is None
                or now - self.last_received > timeout_s):
            return [self.mapping.disarmed_pwm] * 4
        return list(self.last_pwm)


class PeerBackpressure(ConnectionError):
    """A simulator peer stopped accepting bounded sensor output."""


class _SocketWriter:
    """Nonblocking, bounded MAVLink stream writer.

    MAVLink's stream interface is synchronous, but a TCP peer can stop reading.
    Queue partial writes without ever stalling the plant loop; end the session
    once the bound is reached instead of retaining stale sensor data indefinitely.
    """

    def __init__(self, sock: socket.socket, *, max_pending_bytes: int):
        if not isinstance(max_pending_bytes, int) or max_pending_bytes <= 0:
            raise ValueError("max pending bytes must be a positive integer")
        self.sock = sock
        self.max_pending_bytes = max_pending_bytes
        self.pending = bytearray()

    def write(self, data: bytes) -> int:
        self.flush()
        if len(data) > self.max_pending_bytes or (
            len(self.pending) + len(data) > self.max_pending_bytes
        ):
            raise PeerBackpressure(
                f"PX4 peer is not reading; {len(self.pending)} pending bytes"
            )
        self.pending.extend(data)
        self.flush()
        return len(data)

    def flush(self) -> None:
        while self.pending:
            try:
                sent = self.sock.send(self.pending)
            except BlockingIOError:
                return
            except OSError as exc:
                raise ConnectionError("PX4 peer write failed") from exc
            if sent <= 0:
                raise ConnectionError("PX4 peer closed while writing")
            del self.pending[:sent]


def run_mavlink_session(
    connection: socket.socket,
    plant: Plant,
    *,
    mapping: MotorMapping,
    geoid_separation_m: float,
    step_us: int = 2000,
    actuator_timeout_s: float = 0.1,
    max_pending_bytes: int = 16 * 1024,
) -> None:
    """Run one real-time-paced PX4 Simulator MAVLink session over TCP."""
    if not 1 <= step_us <= 2000:
        raise ValueError("step_us must be between 1 and 2000")
    if not isinstance(max_pending_bytes, int) or max_pending_bytes <= 0:
        raise ValueError("max pending bytes must be a positive integer")
    connection.setblocking(False)
    receiver = ActuatorReceiver(mapping)
    writer = _SocketWriter(connection, max_pending_bytes=max_pending_bytes)
    sensors = SensorWriter(writer, geoid_separation_m=geoid_separation_m)
    period = step_us / 1_000_000
    deadline = time.monotonic()
    report_at = deadline + 1.0
    window_start = deadline
    steps = 0
    late_steps = 0

    while True:
        try:
            while select.select([connection], [], [], 0)[0]:
                try:
                    data = connection.recv(65536)
                except BlockingIOError:
                    break
                if not data:
                    return
                receiver.feed(data)

            plant.step(receiver.pwm(timeout_s=actuator_timeout_s), step_us)
            sensors.send(plant)
            writer.flush()
        except (ConnectionError, PeerBackpressure):
            return
        steps += 1

        deadline += period
        delay = deadline - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            # Do not attempt an unbounded catch-up burst after a slow FMI step.
            late_steps += 1
            deadline = time.monotonic()

        now = time.monotonic()
        if now >= report_at:
            elapsed = now - window_start
            print(
                f"BIL pacing: {steps / elapsed:.1f} steps/s "
                f"(target {1 / period:.1f}), late={late_steps}/{steps}, "
                f"actuator={receiver.message_count}, sim_t={plant.time_us / 1e6:.2f}s",
                flush=True,
            )
            report_at = now + 1.0
            window_start = now
            steps = 0
            late_steps = 0


def serve_tcp(
    fmu_path: Path,
    *,
    host: str = "0.0.0.0",
    port: int = 4560,
    mapping: MotorMapping,
    geoid_separation_m: float,
    step_us: int = 2000,
    actuator_timeout_s: float = 0.1,
) -> None:
    """Accept PX4 SITL's TCP connection and serve Simulator MAVLink packets."""
    if not 1 <= port <= 65535:
        raise ValueError("TCP port must be in 1..65535")
    plant = Plant(fmu_path)
    try:
        with socket.create_server((host, port), reuse_port=False) as server:
            print(f"FastDyn PX4 BIL listening on {host}:{port}; waiting for PX4", flush=True)
            connection, peer = server.accept()
            print(f"PX4 connected from {peer[0]}:{peer[1]}", flush=True)
            with connection:
                run_mavlink_session(
                    connection, plant, mapping=mapping,
                    geoid_separation_m=geoid_separation_m,
                    step_us=step_us, actuator_timeout_s=actuator_timeout_s,
                )
    finally:
        plant.close()

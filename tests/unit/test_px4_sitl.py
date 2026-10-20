import io
import math
import socket
import threading
import time

import pytest
from pymavlink.dialects.v20 import common as mavlink

from fastdyn.px4_sitl import (
    ActuatorReceiver,
    MotorMapping,
    PeerBackpressure,
    SensorStall,
    SensorWriter,
    _SocketWriter,
    run_mavlink_session,
)


def actuator(controls, armed=True):
    return mavlink.MAVLink_hil_actuator_controls_message(
        123, controls + [0.0] * 12,
        mavlink.MAV_MODE_FLAG_SAFETY_ARMED if armed else 0, 0,
    )


def test_explicit_motor_order_and_disarm():
    mapping = MotorMapping((2, 0, 3, 1))
    message = actuator([0.0, 0.25, 0.5, 1.0])
    assert mapping.convert(message) == [1500, 1100, 1900, 1300]
    message.mode = 0
    assert mapping.convert(message) == [1000] * 4


@pytest.mark.parametrize("value", [-0.1, 1.1, math.nan, math.inf])
def test_bad_motor_output_is_not_hidden_by_clamping(value):
    with pytest.raises(ValueError):
        MotorMapping((0, 1, 2, 3)).convert(actuator([value, 0, 0, 0]))


class KnownPlant:
    time_us = 100000

    def read(self, name):
        return {
            "accel": [1, 2, -9.8], "gyro": [0.1, -0.2, 0.3],
            "mag": [0.2, 0, 0.4], "gps": [40.0, -86.0, 149.0],
            "vel_ned": [1.0, -2.0, 0.5], "baro_pressure_pa": [100000.0],
            "baro_altitude_m": [0], "baro_temperature_c": [15],
        }[name]


def decode(data):
    return mavlink.MAVLink(None).parse_buffer(data) or []


def test_wire_units_and_frames():
    stream = io.BytesIO()
    SensorWriter(stream, geoid_separation_m=30.0).send(KnownPlant())
    sensor, gps = decode(stream.getvalue())
    assert sensor.get_type() == "HIL_SENSOR"
    assert sensor.time_usec == 100000 and sensor.abs_pressure == 1000
    assert sensor.zacc == pytest.approx(-9.8)
    assert sensor.ygyro == pytest.approx(-0.2)
    assert gps.lat == 400000000 and gps.lon == -860000000
    assert 100 < sensor.pressure_alt < 115  # absolute ISA altitude, not AGL
    assert gps.alt == 119000 and (gps.vn, gps.ve, gps.vd) == (100, -200, 50)


def test_stall_boundaries_keep_gps_and_do_not_freeze_plant_time():
    plant, stream = KnownPlant(), io.BytesIO()
    writer = SensorWriter(stream, geoid_separation_m=0.0)
    stall = SensorStall(100000, 200000)
    for now in (98000, 100000, 200000, 298000, 300000):
        plant.time_us = now
        writer.send(plant, imu_stall=stall)
    messages = decode(stream.getvalue())
    assert [m.time_usec for m in messages if m.get_type() == "HIL_SENSOR"] == [98000, 300000]
    assert [m.time_usec for m in messages if m.get_type() == "HIL_GPS"] == [100000, 200000, 300000]


@pytest.mark.parametrize("step_us", [1500, 1999, 2000])
def test_gps_deadline_crossing_is_independent_of_fmi_step_size(step_us):
    plant, stream = KnownPlant(), io.BytesIO()
    writer = SensorWriter(stream, geoid_separation_m=0.0)
    while plant.time_us < 1_000_000:
        plant.time_us += step_us
        writer.send(plant)
    gps = [m for m in decode(stream.getvalue()) if m.get_type() == "HIL_GPS"]
    assert len(gps) == 10
    assert [m.time_usec for m in gps] == sorted(m.time_usec for m in gps)


def test_nonblocking_writer_bounds_a_stalled_peer_without_sendall():
    class StalledSocket:
        def send(self, data):
            raise BlockingIOError

    writer = _SocketWriter(StalledSocket(), max_pending_bytes=8)
    assert writer.write(b"1234") == 4
    assert writer.write(b"5678") == 4
    with pytest.raises(PeerBackpressure, match="not reading"):
        writer.write(b"9")


def test_tcp_session_does_not_freeze_when_peer_stops_reading():
    class Plant:
        def __init__(self):
            self.time_us = 0
            self.applied = []

        def step(self, pwm, dt_us):
            self.applied.append(list(pwm))
            self.time_us += dt_us

        def read(self, name):
            return {
                "accel": [0, 0, -9.8], "gyro": [0, 0, 0], "mag": [0.2, 0, -0.4],
                "gps": [40, -86, 149], "vel_ned": [0, 0, 0],
                "baro_pressure_pa": [101325], "baro_temperature_c": [15],
            }[name]

    plant = Plant()
    server, client = socket.socketpair()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
    packet = io.BytesIO()
    mavlink.MAVLink(packet).send(actuator([1.0, 1.0, 1.0, 1.0]))
    client.sendall(packet.getvalue())
    errors = []

    def run():
        try:
            run_mavlink_session(
                server, plant,
                mapping=MotorMapping((0, 1, 2, 3)), geoid_separation_m=0.0,
                max_pending_bytes=4096,
            )
        except Exception as exc:  # pragma: no cover - assertion reports the type
            errors.append(type(exc).__name__)

    try:
        started = time.monotonic()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(0.5)
        elapsed = time.monotonic() - started
        stopped_at_bound = not thread.is_alive()
    finally:
        client.close()
        thread.join(1)
        server.close()

    assert stopped_at_bound, f"session still blocked after {elapsed:.3f}s"
    assert not errors
    assert len(plant.applied) > 7
    assert plant.applied[0] == [1900] * 4
    assert plant.applied[-1] == [1000] * 4


def test_mapping_cannot_duplicate_a_motor():
    with pytest.raises(ValueError):
        MotorMapping((0, 0, 2, 3))


def test_motor_control_range_is_explicit_and_linearly_scaled():
    mapping = MotorMapping((0, 1, 2, 3), control_min=-1.0, control_max=1.0)
    assert mapping.convert(actuator([-1.0, 0.0, 0.5, 1.0])) == [1100, 1500, 1700, 1900]
    with pytest.raises(ValueError, match="control outside"):
        MotorMapping((0, 1, 2, 3)).convert(actuator([-0.01, 0, 0, 0]))


def test_receiver_handles_fragmented_mavlink_and_stales_to_disarmed():
    now = [10.0]
    mapping = MotorMapping((2, 0, 3, 1))
    receiver = ActuatorReceiver(mapping, clock=lambda: now[0])
    assert receiver.pwm() == [1000] * 4

    wire = io.BytesIO()
    mavlink.MAVLink(wire).send(actuator([0.0, 0.25, 0.5, 1.0]))
    packet = wire.getvalue()
    assert receiver.feed(packet[:5]) == 0
    assert receiver.feed(packet[5:]) == 1
    assert receiver.pwm() == [1500, 1100, 1900, 1300]

    now[0] += 0.101
    assert receiver.pwm(timeout_s=0.1) == [1000] * 4


def test_receiver_ignores_non_actuator_messages():
    receiver = ActuatorReceiver(MotorMapping((0, 1, 2, 3)))
    wire = io.BytesIO()
    mavlink.MAVLink(wire).heartbeat_send(2, 3, 0, 0, 0, 3)
    assert receiver.feed(wire.getvalue()) == 0
    assert receiver.pwm() == [1000] * 4


def test_tcp_session_receives_actuator_and_sends_simulator_sensor():
    class Plant:
        def __init__(self):
            self.time_us = 0
            self.applied = []

        def step(self, pwm, dt_us):
            self.applied.append(list(pwm))
            self.time_us += dt_us

        def read(self, name):
            return {
                "accel": [0, 0, -9.8], "gyro": [0, 0, 0], "mag": [0.2, 0, -0.4],
                "baro_pressure_pa": [101325], "baro_temperature_c": [15],
            }[name]

    plant = Plant()
    server, client = socket.socketpair()
    client.settimeout(1)
    outgoing = io.BytesIO()
    mavlink.MAVLink(outgoing).send(actuator([0.25, 0.5, 0.75, 1.0]))
    client.sendall(outgoing.getvalue())
    thread = threading.Thread(
        target=run_mavlink_session,
        args=(server, plant),
        kwargs={"mapping": MotorMapping((0, 1, 2, 3)), "geoid_separation_m": 0.0},
    )
    thread.start()
    try:
        received = client.recv(4096)
    finally:
        client.close()
        thread.join(timeout=1)
        server.close()
    assert not thread.is_alive()
    assert plant.applied[0] == [1300, 1500, 1700, 1900]
    messages = decode(received)
    assert messages and messages[0].get_type() == "HIL_SENSOR"

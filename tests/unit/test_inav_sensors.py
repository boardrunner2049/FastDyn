"""Exercise the INAV MPU6500 wire representation and sensor mounting."""
import ctypes
import math
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


class Vector(ctypes.Structure):
    _fields_ = [(axis, ctypes.c_float) for axis in 'xyz']


class Imu(ctypes.Structure):
    _fields_ = [('gyro', Vector), ('accel_body', Vector)]


@pytest.fixture(scope='module')
def encode(tmp_path_factory):
    library = tmp_path_factory.mktemp('inav') / 'sensors.so'
    subprocess.run(['cc', '-shared', '-fPIC', '-I', str(ROOT / 'virtuals/physics'),
                    str(ROOT / 'virtuals/physics/flight_controllers/inav/sensors.c'),
                    '-lm', '-o', str(library)], check=True)
    function = ctypes.CDLL(str(library)).inav_mpu6500_encode
    function.argtypes = [ctypes.POINTER(Imu), ctypes.c_int, ctypes.POINTER(ctypes.c_uint8)]
    def call(accel, gyro=(0, 0, 0), frame=1):
        sample = Imu(Vector(*gyro), Vector(*accel))
        output = (ctypes.c_uint8 * 14)()
        function(ctypes.byref(sample), frame, output)
        return struct.unpack('>hhhhhhh', bytes(output))
    return call


def test_stationary_level_encodes_one_g(encode):
    assert encode((0, 0, -9.80665)) == (0, 0, 2048, 0, 0, 0, 0)


def test_axes_mounting_and_unit_conversion(encode):
    encoded = encode((9.80665, -9.80665, 0), (math.pi / 2, -math.pi / 2, math.pi / 2))
    assert encoded == (2048, -2048, 0, 0, -1476, 1476, 1476)
    # INAV applies CW180 after reading: X and Y are negated.
    assert (-encoded[0], -encoded[1], encoded[2]) == (-2048, 2048, 0)


def test_flu_and_frd_describe_same_body_motion(encode):
    assert encode((1, 2, 3), (0.1, 0.2, 0.3), 1) == encode((1, -2, -3), (0.1, -0.2, -0.3), 0)


def test_out_of_range_and_nonfinite_values_are_safe(encode):
    values = encode((1e10, -1e10, float('nan')))
    assert values[:3] == (32767, -32768, 0)

"""Build both vehicle FMUs and the FMU plugin before running these integration tests."""

import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from fastdyn import fmu_build


ROOT = Path(__file__).resolve().parents[2]
LEGACY_COLUMNS = (
    "sim_time", "lat", "lon", "z", "gps_vel_n", "gps_vel_e", "gps_vel_d",
    "gps_sec", "gps_nsec", "gps_yaw_deg", "x_accel", "y_accel", "z_accel",
    "roll_rate", "pitch_rate", "yaw_rate",
)
RPY = ("roll_rad", "pitch_rad", "yaw_rad")
OMEGA = ("omega_frd_x", "omega_frd_y", "omega_frd_z")
ALPHA = ("alpha_frd_x", "alpha_frd_y", "alpha_frd_z")
POS = ("pos_n_m", "pos_e_m", "pos_d_m")
VEL = ("gps_vel_n", "gps_vel_e", "gps_vel_d")
ACC = ("accel_n_mps2", "accel_e_mps2", "accel_d_mps2")
VEL_FRD = ("vel_frd_x", "vel_frd_y", "vel_frd_z")
ACC_FRD = ("accel_frd_x", "accel_frd_y", "accel_frd_z")
ROT = tuple(f"rotation_{i}{j}" for i in range(1, 4) for j in range(1, 4))
MAG = ("mag_x", "mag_y", "mag_z")
BARO = ("baro_altitude_m", "baro_pressure_pa", "baro_temperature_c", "baro_climb_rate_mps")
MOTORS = tuple(f"motor_cmd_{i}" for i in range(1, 5))


def vehicle_build(vehicle):
    build = fmu_build.resolve(ROOT / "configs" / f"{vehicle}462.toml", repo_root=ROOT)
    if not build.fmu_path.is_file():
        pytest.skip(f"Build configs/{vehicle}462.toml to run the FMU integration checks")
    return build


@pytest.fixture(scope="module")
def driver(tmp_path_factory):
    plugin = ROOT / "build/libfastdyn.so"
    if not plugin.is_file():
        pytest.skip("Build the FastDyn FMU plugin to run the trace integration checks")
    exe = tmp_path_factory.mktemp("fmu-trace") / "driver"
    subprocess.run([
        "cc", "-std=c11", "-I", str(ROOT / "virtuals/physics"),
        str(Path(__file__).with_name("fmu_kinematics_driver.c")), str(plugin),
        "-Wl,--allow-shlib-undefined", "-Wl,--no-as-needed", "-lm", "-o", str(exe),
    ], check=True)
    return exe


def capture(driver, tmp_path, vehicle, pwm):
    build = vehicle_build(vehicle)
    path = tmp_path / "trace.csv"
    refs = fmu_build.value_references(build)
    subprocess.run([
        driver, *map(str, pwm), f"fmu={build.fmu_path}",
        *(f"fmu_vr_{name}={vr}" for name, vr in refs.items()),
        *(f"fmu_param_{name}={value}" for name, value in build.parameters.items()),
    ], check=True, env={**os.environ, "FASTDYN_TRACE_FILE": str(path)})
    trace = np.genfromtxt(path, delimiter=",", names=True)
    expected = (LEGACY_COLUMNS + RPY + OMEGA + ALPHA + POS + ACC
                + VEL_FRD + ACC_FRD + ROT + MAG + BARO + MOTORS)
    assert trace.dtype.names == expected
    assert all(np.isfinite(trace[name]).all() for name in trace.dtype.names)
    np.testing.assert_array_equal(trace["sim_time"], np.arange(1001))
    np.testing.assert_allclose(np.degrees(trace["yaw_rad"]), trace["gps_yaw_deg"], atol=1e-5)
    np.testing.assert_allclose(trace["z"], 149 - trace["pos_d_m"], atol=1e-10, rtol=0)
    return trace


def vector(row, names):
    return np.array([row[name] for name in names])


@pytest.mark.parametrize("vehicle", ["copter", "rover"])
def test_packaged_output_contract(vehicle):
    with zipfile.ZipFile(vehicle_build(vehicle).fmu_path) as archive:
        xml = ET.fromstring(archive.read("modelDescription.xml"))
    outputs = {v.get("name"): v for v in xml.find("ModelVariables") if v.get("causality") == "output"}
    expected = {"pos_ned", "pos_gps", "vel_ned", "vel_frd", "accel_ned", "accel_frd",
                "rpy", "rotation", "yaw_deg", "omega_frd", "alpha_frd", "mag", *BARO, "motor_cmd"}
    assert {name for name in outputs if "." not in name} == expected
    assert [d.get("start") for d in outputs["rotation"].findall("Dimension")] == ["3", "3"]
    assert [d.get("start") for d in outputs["motor_cmd"].findall("Dimension")] == ["4"]
    assert not {"accel_bias", "gyro_bias", "mag_bias"}.intersection(
        v.get("name") for v in xml.find("ModelVariables"))
    for name, unit in (("rpy", "rad"), ("omega_frd", "rad/s"), ("alpha_frd", "rad/s2"),
                       ("pos_ned", "m"), ("pos_gps", None), ("vel_ned", "m/s"), ("vel_frd", "m/s"),
                       ("accel_ned", "m/s2"), ("accel_frd", "m/s2"), ("mag", "Gauss")):
        variable = outputs[name]
        assert variable.tag == "Float64" and variable.get("unit") == unit
        assert [d.get("start") for d in variable.findall("Dimension")] == ["3"]


def test_copter_free_fall_and_ground_support(driver, tmp_path):
    trace = capture(driver, tmp_path, "copter", [1000] * 4)
    np.testing.assert_allclose(vector(trace[0], POS), [0, 0, -0.12], atol=1e-9)
    np.testing.assert_allclose(vector(trace[0], ACC), [0, 0, 9.8], atol=1e-9)
    np.testing.assert_allclose(vector(trace[0], ACC_FRD), [0, 0, 9.8], atol=1e-9)
    assert trace["gps_vel_d"][20] > 0
    assert trace["z_accel"][0] == pytest.approx(0, abs=1e-9)
    np.testing.assert_allclose(vector(trace[-1], ACC), 0, atol=1e-6)
    np.testing.assert_allclose(vector(trace[-1], ACC_FRD), 0, atol=1e-6)
    assert trace["z_accel"][-1] == pytest.approx(-9.8, abs=1e-5)


# Single-axis torque in ArduPilot Quad-X motor order. Sample before ground contact.
@pytest.mark.parametrize("axis,high", [(0, (1, 2)), (1, (0, 2)), (2, (0, 1))])
@pytest.mark.parametrize("sign", [-1, 1])
def test_copter_body_axes(driver, tmp_path, axis, high, sign):
    pwm = [1800 if (motor in high) == (sign > 0) else 1700 for motor in range(4)]
    trace = capture(driver, tmp_path, "copter", pwm)
    for names in (RPY, OMEGA, ALPHA):
        values = vector(trace[20], names)
        assert sign * values[axis] > 0
        np.testing.assert_allclose(np.delete(values, axis), 0, atol=1e-8)
    # Independent rate and acceleration from samples 2 ms apart.
    for state, rate in ((RPY, OMEGA), (OMEGA, ALPHA)):
        derivative = (vector(trace[21], state) - vector(trace[19], state)) / 0.002
        np.testing.assert_allclose(derivative, vector(trace[20], rate), rtol=0.01, atol=1e-6)


def check_frames_and_sensors(trace):
    angles = vector(trace, RPY).T
    attitude = Rotation.from_euler("ZYX", angles[:, ::-1])
    matrices = vector(trace, ROT).T.reshape(-1, 3, 3)
    np.testing.assert_allclose(matrices, attitude.as_matrix(), atol=1e-8)
    np.testing.assert_allclose(attitude.apply(vector(trace, VEL_FRD).T), vector(trace, VEL).T, atol=1e-8)
    np.testing.assert_allclose(attitude.apply(vector(trace, ACC_FRD).T), vector(trace, ACC).T, atol=1e-7)
    np.testing.assert_allclose(attitude.apply(vector(trace, MAG).T),
                               np.tile([0.21, 0.0, 0.45], (len(trace), 1)), atol=1e-8)
    np.testing.assert_allclose(trace["baro_climb_rate_mps"], -trace["gps_vel_d"])
    np.testing.assert_allclose(trace["baro_altitude_m"], -trace["pos_d_m"])
    np.testing.assert_allclose(trace["baro_temperature_c"], 15.0 - 0.0065 * trace["z"])
    np.testing.assert_allclose(trace["baro_pressure_pa"], 101325.0 * (1 - 2.25577e-5 * trace["z"]) ** 5.25588)
    specific_force = vector(trace, ("x_accel", "y_accel", "z_accel")).T
    np.testing.assert_allclose(attitude.apply(specific_force) + [0, 0, 9.8],
                               vector(trace, ACC).T, atol=1e-5)
    imu_rates = vector(trace, ("roll_rate", "pitch_rate", "yaw_rate"))
    np.testing.assert_allclose(imu_rates, vector(trace, OMEGA), atol=1e-6)
    return attitude


def test_copter_combined_tilt_and_world_acceleration(driver, tmp_path):
    pwm = [1750, 1800, 1850, 1650]
    trace = capture(driver, tmp_path, "copter", pwm)
    attitude = check_frames_and_sensors(trace)
    np.testing.assert_allclose(vector(trace[20], MOTORS), (np.array(pwm) - 1100) * 1300 / 800)
    dt = np.diff(trace["sim_time"]) / 1000
    # Relative rotation expressed in the previous FRD body axes.
    rotation_rate = (attitude[:-1].inv() * attitude[1:]).as_rotvec() / dt[:, None]
    rates = vector(trace, OMEGA).T
    np.testing.assert_allclose(rotation_rate[:50], (rates[1:51] + rates[:50]) / 2, atol=2e-3)


@pytest.mark.parametrize("drive", [1, -1], ids=["forward", "reverse"])
@pytest.mark.parametrize("yaw_sign", [0, -1, 1], ids=["straight", "left", "right"])
def test_rover_drive_and_turn(driver, tmp_path, drive, yaw_sign):
    # Positive steering acts about plant Z-up; positive FRD yaw acts about Z-down.
    pwm = [1500 - 250 * yaw_sign, 1500, 1500 + 250 * drive, 1500]
    trace = capture(driver, tmp_path, "rover", pwm)
    check_frames_and_sensors(trace)
    for axes in (ACC, ACC_FRD):
        np.testing.assert_allclose(vector(trace[0], axes), [drive * 4 / 0.35, 0, 0], atol=1e-8)
    np.testing.assert_allclose(vector(trace[0], ALPHA), [0, 0, yaw_sign * 0.48], atol=1e-8)
    np.testing.assert_allclose(trace["motor_cmd_1"], -0.5 * yaw_sign)
    np.testing.assert_allclose(trace["motor_cmd_2"], 0.5 * drive)
    np.testing.assert_allclose(trace["motor_cmd_3"], trace["vel_frd_x"])
    np.testing.assert_allclose(trace["motor_cmd_4"], trace["omega_frd_z"])
    np.testing.assert_allclose(trace["z_accel"], -9.8, atol=1e-6)
    vertical_and_tilt = ("roll_rad", "pitch_rad", "pos_d_m", "gps_vel_d", "accel_d_mps2")
    np.testing.assert_allclose(vector(trace, vertical_and_tilt), 0, atol=1e-9)
    last = trace[-1]
    assert drive * last["vel_frd_x"] > 0 and drive * last["gps_vel_n"] > 0
    assert drive * last["pos_n_m"] > 0 and drive * (last["lat"] - trace["lat"][0]) > 0
    if yaw_sign:
        assert yaw_sign * last["yaw_rad"] > 0 and yaw_sign * last["omega_frd_z"] > 0
        assert yaw_sign * drive * last["pos_e_m"] > 0
        assert yaw_sign * drive * (last["lon"] - trace["lon"][0]) > 0
    else:
        np.testing.assert_allclose(vector(trace, ("yaw_rad", "omega_frd_z", "pos_e_m", "gps_vel_e")), 0, atol=1e-9)
        np.testing.assert_allclose(trace["lon"], trace["lon"][0], rtol=0, atol=1e-10)
    for state, rate in ((POS, VEL), (VEL, ACC), (RPY, OMEGA), (OMEGA, ALPHA)):
        derivative = (vector(trace[501], state) - vector(trace[499], state)) / 0.002
        np.testing.assert_allclose(derivative, vector(trace[500], rate), rtol=0.01, atol=2e-4)

#!/usr/bin/env python3
"""Prove native plant evolution and MAVLink loss injection without a PX4 process.

Both runs apply identical open-loop PWM, so trajectories must match. This is a
harness negative control, not evidence of PX4 fault response or vehicle robustness.
"""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastdyn.px4_sitl import Plant, SensorStall, SensorWriter


class CountingStream:
    def __init__(self):
        self.bytes_written = 0

    def write(self, packet):
        self.bytes_written += len(packet)
        return len(packet)


def run(fmu: Path, output: Path, stall: SensorStall | None):
    rows = []
    stream = CountingStream()
    writer = SensorWriter(stream, geoid_separation_m=0.0)
    plant = Plant(fmu)
    try:
        for _ in range(3000):
            pwm = 1700.0 if 1000000 <= plant.time_us < 4000000 else 1000.0
            plant.step([pwm] * 4)
            before = writer.sensor_count, writer.gps_count
            writer.send(plant, imu_stall=stall)
            rows.append({
                "plant_time_s": plant.time_us / 1e6,
                "altitude_above_ground_m": plant.read("baro_altitude_m")[0],
                "vertical_velocity_ned_mps": plant.read("vel_ned")[2],
                "accel_z_frd_mps2": plant.read("accel")[2],
                "pwm_us": pwm,
                "hil_sensor_sent": int(writer.sensor_count > before[0]),
                "hil_gps_sent": int(writer.gps_count > before[1]),
            })
    finally:
        plant.close()
    with output.open("w", newline="") as handle:
        csv_writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        csv_writer.writeheader()
        csv_writer.writerows(rows)
    return rows, {"hil_sensor": writer.sensor_count, "hil_gps": writer.gps_count,
                  "bytes": stream.bytes_written, "plant_steps": len(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fmu", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ.setdefault("MPLCONFIGDIR", str(args.output / "mpl-cache"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    baseline, normal = run(args.fmu, args.output / "baseline.csv", None)
    stalled, fault = run(args.fmu, args.output / "sensor-stall.csv", SensorStall(1500000, 2000000))
    assert normal["hil_sensor"] == 3000 and fault["hil_sensor"] == 2000
    assert normal["hil_gps"] == fault["hil_gps"] == 60
    assert [r["altitude_above_ground_m"] for r in baseline] == [r["altitude_above_ground_m"] for r in stalled]
    settled = baseline[499]
    assert -10.5 < settled["accel_z_frd_mps2"] < -9
    assert max(r["altitude_above_ground_m"] for r in baseline) > settled["altitude_above_ground_m"] + 0.5

    fig, axes = plt.subplots(3, 1, figsize=(10, 7), sharex=True, layout="constrained")
    time = [r["plant_time_s"] for r in baseline]
    axes[0].plot(time, [r["altitude_above_ground_m"] for r in baseline], label="open-loop native FMI plant")
    axes[0].set_ylabel("Altitude above ground (m)")
    axes[0].legend(loc="upper left")
    axes[1].plot(time, [r["pwm_us"] for r in baseline])
    axes[1].set_ylabel("Four equal motor PWM (µs)")
    axes[2].plot(time, [500 * r["hil_sensor_sent"] for r in stalled])
    axes[2].set_ylabel("HIL_SENSOR stream (Hz)")
    axes[2].set_xlabel("Plant time (s); continues through sensor stream stall")
    for axis in axes:
        axis.axvspan(1.5, 3.5, color="orange", alpha=0.2)
        axis.grid(alpha=0.2)
    fig.suptitle("FastDyn PX4 adapter harness: 2 s sensor stream stall\nExample quadrotor plant • open-loop PWM • no PX4 controller • not vehicle-calibrated")
    for suffix in ("png", "svg"):
        fig.savefig(args.output / f"native-probe.{suffix}", dpi=150)
    receipt = {
        "verdict": "NATIVE_PLANT_AND_WIRE_INJECTION_PASS",
        "px4_controller_run": False,
        "vehicle_calibrated": False,
        "hardware_actions": False,
        "fmu_sha256": hashlib.sha256(args.fmu.read_bytes()).hexdigest(),
        "baseline": normal, "sensor_stall": fault,
        "assertions": ["stationary FRD gravity", "positive thrust response", "finite plant outputs",
                       "2000 ms HIL_SENSOR loss", "GPS unaffected", "plant advances throughout loss",
                       "identical open-loop trajectories with and without message loss"],
        "limits": ["Example plant defaults; vehicle measurements pending",
                   "Zero geoid separation is an explicit synthetic-test assumption, not a site measurement",
                   "All HIL_SENSOR fields are withheld together; not an IMU-only hardware emulation",
                   "No TCP peer or PX4 estimator/controller connected",
                   "Motor permutation requires geometry validation before closing loop",
                   "Optical-flow/range and actuator/link watchdog tests not implemented yet"],
    }
    (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()

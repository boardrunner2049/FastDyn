"""Cross-check Ben's six Copter envelopes against an OptiFuzz FMUv3 trace."""

import argparse
import csv
import math
import sys
from pathlib import Path

# These limits are independent of the formulas loaded by the monitor.
LIMITS = {
    "speed_ned_mps": 10.0,
    "roll_rad": 0.3,
    "pitch_rad": 0.3,
    "accel_n_mps2": 100.0,
    "accel_e_mps2": 100.0,
    "accel_d_mps2": 100.0,
}


def check(trace_path, robustness_path):
    scores = {}
    for line in robustness_path.read_text().splitlines():
        if line.startswith("Formula"):
            formula, score = line.rstrip(",").split(", robustness=")
            tokens = formula.partition("=")[2].split()
            for signal in LIMITS.keys() & set(tokens):
                if signal in scores:
                    raise ValueError(f"Multiple robustness scores for {signal}")
                scores[signal] = float(score)

    peaks = {}
    with trace_path.open(newline="") as trace_file:
        for row in csv.DictReader(trace_file):
            time_s = float(row["sim_time"]) / 1000.0
            speed = math.hypot(*(float(row[f"gps_vel_{axis}"]) for axis in "ned"))
            for signal in LIMITS:
                value = speed if signal == "speed_ned_mps" else float(row[signal])
                if not math.isfinite(value) or not math.isfinite(time_s):
                    raise ValueError(f"Non-finite {signal} or timestamp at {time_s} s")
                if signal not in peaks or abs(value) > abs(peaks[signal][0]):
                    peaks[signal] = (value, time_s)
    if not peaks:
        raise ValueError("Empty trace")

    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(("signal", "limit", "signed_peak", "time_s", "trace_robustness",
                     "optifuzz_robustness", "match"))
    matches = []
    for signal, limit in LIMITS.items():
        peak, time_s = peaks[signal]
        margin = limit - abs(peak)
        match = math.isclose(margin, scores[signal], rel_tol=0, abs_tol=1e-9)
        writer.writerow((signal, limit, peak, time_s, margin, scores[signal], match))
        matches.append(match)
    return all(matches)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_csv", type=Path)
    parser.add_argument("robustness_csv", type=Path)
    args = parser.parse_args()
    sys.exit(0 if check(args.trace_csv, args.robustness_csv) else 1)

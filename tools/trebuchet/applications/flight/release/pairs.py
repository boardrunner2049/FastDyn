"""Run and summarize the predeclared, cacheless PX4 qualification diagnostic."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import statistics
import subprocess
import sys

HERE = Path(__file__).resolve().parent
POLICY = HERE / "cacheless-policy.json"


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def interval(values):
    rng = random.Random(2601)
    draws = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(20000))
    return {"median_original_over_firmbolt": statistics.median(values),
            "bootstrap_95pct": [draws[499], draws[19499]]}


def run(output, payload, plugin):
    policy = json.loads(POLICY.read_text())
    output.mkdir()
    trials = []
    conditions = [("warmup", image) for image in policy["warmups"]]
    conditions += [(f"pair-{i:02}", policy[letter])
                   for i, order in enumerate(policy["pair_orders"], 1) for letter in order]
    for group, image in conditions:
        dest = output / (group + "-" + image)
        cmd = [sys.executable, str(HERE.parent / "baseline.py"), "run", "px4", str(dest),
               "--plugin", str(plugin), "--release-pair", str(payload), "--measure-cpu",
               "--timeout", str(policy["timeout_each_mission_seconds"])]
        if image == "firmbolt":
            cmd += ["--candidate-elf", str(payload / "firmware.elf"),
                    "--candidate-layout", str(payload / "firmbolt.json.layout.json")]
        rc = subprocess.run(cmd).returncode
        receipt = json.loads((dest / "receipt.json").read_text())
        passed = (rc == 0 and receipt["state"] == "complete" and receipt["operational_baseline"] and
                  receipt["inputs"]["elf"] == policy["guest_sha256"][image] and
                  receipt["cold"]["oracle"]["disarmed_after_arming"])
        trial = {"group": group, "image": image, "passed": passed, "receipt_sha256": sha(dest / "receipt.json")}
        if passed:
            trial.update({key: receipt["cold"][key] for key in
                          ("cpu_progress_end_s", "process_cpu_seconds_at_45s", "full_fastdyn_wall_seconds")})
        else:
            trial["error"] = receipt.get("error", "failure in receipt identity, oracle or timing")
        trials.append(trial)
        # The terminal console, MAVLink stream and independent plant trace are the
        # frozen oracle inputs. The high-volume bus-level diagnostic is unused.
        (dest / "cold/run/io.log").unlink(missing_ok=True)
        (output / "trials.json").write_text(json.dumps(trials, indent=2) + "\n")
    report = {"policy_sha256": sha(POLICY), "trials": trials,
              "all_passed": all(row["passed"] for row in trials)}
    if report["all_passed"]:
        for metric in ("process_cpu_seconds_at_45s", "full_fastdyn_wall_seconds"):
            vals = []
            for i in range(1, 13):
                pair = {r["image"]: r[metric] for r in trials if r["group"] == f"pair-{i:02}"}
                vals.append(pair["original"] / pair["firmbolt"])
            report[metric] = {"pairs": vals, **interval(vals)}
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    return not report["all_passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("payload", type=Path)
    parser.add_argument("plugin", type=Path)
    args = parser.parse_args()
    sys.exit(run(args.output.resolve(), args.payload.resolve(), args.plugin.resolve()))

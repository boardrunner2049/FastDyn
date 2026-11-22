"""Independently verify a downloaded PX4 cacheless campaign and summarize it."""
import json
from pathlib import Path
import statistics
import sys
from pairs import POLICY, interval, sha

FLIGHT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FLIGHT))
from oracle import validate


def verify(root):
    policy = json.loads(POLICY.read_text())
    summary = json.loads((root / "summary.json").read_text())
    manifest = json.loads((FLIGHT / "manifest.json").read_text())
    mission = FLIGHT.parents[3] / manifest["mission"]
    if summary["policy_sha256"] != sha(POLICY) or sha(mission) != manifest["mission_sha256"]:
        raise ValueError("wrong experiment policy or mission")
    schedule = [("warmup", image) for image in policy["warmups"]]
    schedule += [(f"pair-{i:02}", policy[c]) for i, order in enumerate(policy["pair_orders"], 1) for c in order]
    rows = summary["trials"]
    if [(t["group"], t["image"]) for t in rows] != schedule:
        raise ValueError("trial schedule differs from policy")
    errors = []
    identities = set()
    observed = {}
    for row in rows:
        group, image = row["group"], row["image"]
        directory = root / (group + "-" + image)
        receipt_path = directory / "receipt.json"
        receipt = json.loads(receipt_path.read_text())
        if sha(receipt_path) != row["receipt_sha256"]:
            raise ValueError(f"{directory}: receipt hash differs")
        if not row["passed"]:
            failure = {"group": group, "image": image, "error": receipt.get("error", row.get("error"))}
            cold = directory / "cold"
            try:
                failure["independent_oracle"] = validate(
                    "px4", mission, manifest, cold / "plant.csv", cold / "run/mav.tlog",
                    (cold / "console.log").read_text(errors="replace"))
            except (OSError, ValueError) as exc:
                failure["independent_oracle_error"] = str(exc)
            errors.append(failure)
            continue
        if (not receipt["operational_baseline"] or receipt["inputs"]["elf"] != policy["guest_sha256"][image]
                or receipt["inputs"]["mission"] != manifest["mission_sha256"] or "training_profiler" in receipt):
            raise ValueError(f"{directory}: wrong image, mission or instrumentation")
        if image == "firmbolt" and (not receipt["candidate"]["placement"]["admissible"] or
                                    receipt["inputs"]["parent_elf"] != policy["guest_sha256"]["original"]):
            raise ValueError(f"{directory}: transformed ELF lacks its checked original parent")
        runtime = receipt["runtime"]
        identities.add(tuple(runtime[k] for k in ("qemu", "plugin", "plant", "plant_library", "plant_description")))
        cold = directory / "cold"
        for file, expected in receipt["cold"]["raw_outputs"].items():
            if sha(cold / file) != expected:
                raise ValueError(f"{directory}: raw hash mismatch for {file}")
        result = validate("px4", mission, manifest, cold / "plant.csv", cold / "run/mav.tlog",
                          (cold / "console.log").read_text(errors="replace"))
        if result != receipt["cold"]["oracle"] or not result["disarmed_after_arming"]:
            raise ValueError(f"{directory}: independently reconstructed mission oracle differs")
        marks = [[float(v) for v in line.split(",")] for line in (cold / "cpu-marks.csv").read_text().splitlines()]
        if (len(marks) != 2 or marks[0][0] != 0.0 or not 44.999999 <= marks[1][0] <= 45.002001
                or marks[1][0] != row["cpu_progress_end_s"]
                or marks[1][1] - marks[0][1] != row["process_cpu_seconds_at_45s"]
                or row["full_fastdyn_wall_seconds"] != receipt["cold"]["full_fastdyn_wall_seconds"]):
            raise ValueError(f"{directory}: timing differs from raw CPU marks or process observation")
        observed[group, image] = result
    if len(identities) > 1:
        raise ValueError("native runtime or plant identity varies between successful measurements")
    report = {"checked_raw_missions": len(observed), "excluded": errors, "all_passed": not errors}
    if (not errors) != summary["all_passed"]:
        raise ValueError("completion status differs from receipts")
    if not errors:
        for metric in ("process_cpu_seconds_at_45s", "full_fastdyn_wall_seconds"):
            grouped = {(t["group"], t["image"]): t[metric] for t in rows}
            original = [grouped[f"pair-{i:02}", "original"] for i in range(1, 13)]
            guest = [grouped[f"pair-{i:02}", "firmbolt"] for i in range(1, 13)]
            ratios = [original[i] / guest[i] for i in range(12)]
            result = {"pairs": ratios, **interval(ratios)}
            if result != summary[metric]:
                raise ValueError(f"paired analysis differs for {metric}")
            report[metric] = {**result, "median_original_seconds": statistics.median(original),
                              "median_firmbolt_seconds": statistics.median(guest)}
    return report


if __name__ == "__main__":
    print(json.dumps(verify(Path(sys.argv[1])), indent=2))

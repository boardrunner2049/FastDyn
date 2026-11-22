"""Audit the hosted training archive after verifying its GitHub artifact digest."""
import argparse
import json
from pathlib import Path
from statistics import median
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_board import pins, require, sha


def audit(root):
    summary = json.loads((root / "summary.json").read_text())
    out = {"scope": "training only, modeled BTT device, GPIO observer active",
           "preparations": [], "paired_runs": [], "primary_metric": "job_wall_ns"}
    frozen = {}
    for name in ("normal", "candidate"):
        state = json.loads((root / f"{name}-frozen.json").read_text())
        frozen[name] = state["n"]
    times = {}
    for row in summary["runs"]:
        name, label = row["image"], row["phase"]
        rd = root / f"{name}-{label}"
        r = json.loads((rd / "receipt.json").read_text())
        require(row["exit"] == 0 and row["state"] == r["state"] == "passed", f"{rd}: state")
        require(r["image_sha256"] == summary["images"][name] and r["accepted_commands"] == 101, f"{rd}: input")
        require(r["ordered_rises_sha256"] == "142b861eb33e74522b85251b8b5fb0d75a99f03e23eadc3567616db2ddba156f"
                and r["completed_axis_pulses"] == 130999 and r["qemu_exit"] == 0, f"{rd}: pins/exit")
        if label == "pair0":
            require(sha(rd / "pins.txt") == r["pins_sha256"], f"{rd}: edge file")
            p = pins(rd)
            require(p["axis_steps"] == [72414, 55226, 3359], f"{rd}: redecoded edge counts")
        proof = None
        if label != "prepare0":
            proof = json.loads((rd / "proof.json").read_text())
            require(proof["verdict"] == row["proof"] == "PASS", f"{rd}: cache was replaced")
            require(row["appended"] == proof["record_count_after"] - proof["n"] == proof["appended"],
                    f"{rd}: incomplete cache proof")
        record = {"image": name, "phase": label, "wall_ms": r["job_wall_ns"]/1e6, "appended": row.get("appended")}
        if label.startswith("pair"):
            require(proof["n"] == frozen[name], f"{rd}: mismatched frozen inventory")
            times.setdefault(label, {})[name] = record["wall_ms"]
            out["paired_runs"].append(record)
        else:
            out["preparations"].append(record)
    require(set(times) == {"pair0", "pair1", "pair2"} and len(out["paired_runs"]) == 6, "incomplete pairing")
    out["frozen_records"] = frozen
    paired = out["paired_runs"]
    out["fully_warm"] = all(r["appended"] == 0 for r in paired)
    require(out["fully_warm"] == summary["fully_warm_training_comparison"], "inconsistent warm verdict")
    if out["fully_warm"]:
        out["training_median_paired_normal_over_candidate"] = median(t["normal"]/t["candidate"] for t in times.values())
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("runs", type=Path)
    print(json.dumps(audit(p.parse_args().runs), indent=2))

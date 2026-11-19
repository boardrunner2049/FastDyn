"""Check a completed reproduction from its published ledgers, artifacts and matched runs."""
import argparse
import json
import tomllib
from pathlib import Path


def read(path):
    return json.loads(path.read_text())


def check_campaign(directory, arms, n_inputs, n_pairs):
    campaign = read(directory / "campaign.json")
    ratios = read(directory / "ratios.json")
    rows = [json.loads(line) for line in (directory / "runs.jsonl").read_text().splitlines()]
    expected = {(i, r, arm) for i in range(n_inputs) for r in range(n_pairs) for arm in arms}
    actual = {(row["input"], row["rep"], row["arm"]): row["bench_ns"] for row in rows}
    if (set(actual) != expected or len(rows) != len(expected) or not campaign["freeze_ok"]
            or campaign["n_valid"] != len(expected) or campaign["n_runs"] != len(expected)
            or any(not row["valid"] or row["appended"] != 0 for row in rows)):
        raise ValueError(f"incomplete or invalid fixed-work campaign in {directory}")
    samples = {(sample["input"], sample["rep"], arm): ns for sample in ratios["samples_ns"]
               for arm, ns in sample["ns"].items()}
    if samples != actual:
        raise ValueError(f"unmatched per-arm samples in {directory}")
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    base = parser.parse_args().directory
    state = base / "state-forward"
    for w in (base, state):
        baseline = read(w / "baseline.json")
        dynamic = read(w / "v10/dynamic.json")
        if (dynamic["verdict"] != "pass" or set(dynamic["appended"]["artifact"]) != {0}
                or dynamic["baseline"] != baseline["snapshot"]):
            raise ValueError(f"warm baseline qualification failed in {w}")

    programs = state / "pass-timings"
    passes = [p["name"] for p in tomllib.loads((programs / "pipeline.toml").read_text())["pass"]]
    passes.insert(0, "host-inline-splice")
    effects = read(programs / "pass-effects.json")
    expected_effects = {f"{kind}/{name}" for kind in ("incremental", "ablation") for name in passes}
    if (set(effects["effects"]) != expected_effects
            or any(s.get("v10_status") != 0 for s in effects["stages"].values())):
        raise ValueError("one or more pipeline prefixes or ablations lack qualified comparisons")
    combined = read(state / "composed/result.json")
    v10 = read(state / "composed/v10/dynamic.json")
    if (combined["status"] != "published" or combined["path"] != "b3"
            or "fail" in combined["validation"].values() or v10["verdict"] != "pass"
            or any(any(values) for values in v10["appended"].values())):
        raise ValueError("combined artifact failed qualification")
    warm_arms = {"warm", "identity", "b3", "b4", "host-inline-splice", "frame", "compact-frame"}
    n_base = check_campaign(base / "comparison", warm_arms, 2, 5)
    n_state = check_campaign(programs / "timing", {"warm", "identity", *effects["stages"]}, 2, 5)
    print(json.dumps({"baseline_runs": n_base, "pipeline_runs": n_state,
                      "paired_comparisons": len(expected_effects), "artifact": combined["artifact_id"]}))


if __name__ == "__main__":
    main()

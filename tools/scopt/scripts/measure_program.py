"""Interleaved warm firmware measurements for named artifact directories."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import statistics
import platform
import random

from measure_host_inline import validate_evidence, variability, verify_run
from scopt.run.arms import campaign_from_args, run_campaign
from scopt.run.launcher import file_sha256

def summarize(blocks, candidate, reference):
    def ratio(row):
        return row[reference] / row[candidate]

    by_input = {}
    for (index, _), row in sorted(blocks.items()):
        by_input.setdefault(index, []).append(ratio(row))

    def stats(groups):
        groups = tuple(groups)
        result = variability([value for group in groups for value in group])
        # Resample paired ratios independently within each fixed held-out case.
        rng = random.Random(20261118)
        boot = sorted(statistics.median([
            rng.choice(group) for group in groups for _ in group
        ]) for _ in range(5000))
        result["paired_bootstrap_95pct"] = [boot[125], boot[4874]]
        return result

    summary = stats(by_input.values())
    summary["by_input"] = {str(index): stats((group,)) for index, group in by_input.items()}
    return summary


def compare_coverage_caches(sources):
    from scopt.canon import sha256_id
    from scopt.formats.sc_v6 import read_sc6

    caches = {name: Path(sources[name]) / "cache.sc" for name in ("partial", "complete")}
    partial, complete = (read_sc6(path) for path in caches.values())
    if not (partial.loadable and complete.loadable and partial.ext and complete.ext):
        raise ValueError("coverage mechanism comparison requires loadable v6 caches")
    hashes = {name: file_sha256(path) for name, path in caches.items()}
    return {
        "schema": "scopt.coverage_timing_mechanism", "version": 1,
        "cache_sha256": hashes, "cache_files_identical": hashes["partial"] == hashes["complete"],
        "general_bodies_identical": partial.sc.bodies == complete.sc.bodies,
        "general_records_identical": partial.sc.record_bytes == complete.sc.record_bytes,
        "variant_bodies_identical": partial.variant_bodies == complete.variant_bodies,
        "extension_arrays_identical": partial.ext.arrays() == complete.ext.arrays(),
        "general_snapshot_id": partial.sc.snapshot_id(),
        "variant_body_sha256": [sha256_id(body) for body in partial.variant_bodies],
        "general_records": len(partial.sc.valid_slots), "variants": len(partial.ext.variants),
        "specialized_links": len(partial.ext.links),
        "extension_artifact_id8_differs": partial.ext.artifact_id8 != complete.ext.artifact_id8,
        "arms": {name: sources[name] for name in caches},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--arm", action="append", default=[], metavar="NAME=EXPERIMENT")
    parser.add_argument("--oracle", type=Path,
                        help="Use specialization-coverage.json and this fixture's inputs.json.")
    parser.add_argument("--pairs", default=5, type=int)
    args = parser.parse_args()
    if args.pairs < 5:
        parser.error("at least five pairs required")
    w = args.baseline.resolve()
    if args.oracle is None:
        if not args.arm:
            parser.error("supply --arm or the qualified coverage fixture's --oracle")
        meta = json.loads((w / "baseline.json").read_text())
        snap, cfg = Path(meta["snapshot"]), w / "host-inline.toml"
        cases = meta["oracle"]["heldout"]
        identity = Path(meta["artifact"])
        qualified = {"identity": (identity, w / "v10/dynamic.json")}
        for entry in args.arm:
            name, exp = entry.split("=", 1)
            if not name or name in {"warm", *qualified}:
                parser.error("arm names must be unique")
            d = Path(exp).resolve()
            artifact = Path(json.loads((d / "result.json").read_text())["artifact_dir"])
            qualified[name] = artifact, d / "v10/dynamic.json"
    else:
        if args.arm:
            parser.error("the coverage fixture supplies the comparison arms")
        fixture = json.loads((w / "specialization-coverage.json").read_text())
        snap, cfg = Path(fixture["snapshot"]), Path(fixture["config"])
        cases = json.loads(args.oracle.read_text())["heldout"]
        meta = json.loads((w / "runtime.json").read_text())
        qualified = {name: (Path(arm["artifact"]), Path(arm["dynamic"]))
                     for name, arm in fixture["arms"].items()}
        os.environ["SCOPT_ENVLAYOUT_PATH"] = str(w / "envlayouts")
        os.environ["SCOPT_RUNTIME_QEMU"] = fixture["qemu"]
        qemu_sha = meta["qemu_sha256"].removeprefix("sha256:")
        os.environ["SCOPT_RUNTIME_QEMU_SHA256"] = qemu_sha
        os.environ["SCOPT_V6_EXES"] = qemu_sha
    inputs = [w / f"heldout-{i}.bin" for i in range(len(cases))]
    if not inputs:
        raise ValueError("the qualifying oracle has no held-out cases")
    for path, case in zip(inputs, cases, strict=True):
        if path.read_bytes() != int(case["seed"], 0).to_bytes(4, "little"):
            raise ValueError(f"{path} differs from the independent oracle seed")
    sources = {}
    qemu = None
    for name, (artifact, evidence) in qualified.items():
        _, exe = validate_evidence(evidence, artifact, snap, cfg, inputs)
        if qemu is not None and qemu != exe:
            raise ValueError("QEMU differs between arms")
        qemu = exe
        sources[name] = str(artifact)
    if qemu is None:
        raise ValueError("no qualified timing arms")
    if file_sha256(qemu).removeprefix("sha256:") != meta["qemu_sha256"].removeprefix("sha256:"):
        raise ValueError("timing requires the same QEMU recorded at qualification")
    campaign = campaign_from_args(
        base_config=str(w / "prep/run.toml"), arms=("warm", *sources), out_name="host-inline",
        artifacts=sources, warm=str(snap), reps=args.pairs, seed=20261118, split="heldout",
        inputs=[str(p) for p in inputs], qemu=str(qemu), timeout_s=60)
    # Preparation may run in parallel, but two timing campaigns would interfere.
    lock_path = Path("/tmp") / f"scopt-fixed-work-{os.getuid()}.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = run_campaign(campaign, args.out, log=lambda line: print(line, flush=True))
        if {"partial", "complete"} <= sources.keys():
            mechanism = compare_coverage_caches(sources)
            (args.out / "mechanism.json").write_text(json.dumps(mechanism, indent=2) + "\n")
    expected = (len(sources) + 1) * args.pairs * len(inputs)
    if not result["freeze_ok"] or result["n_valid"] != expected:
        raise ValueError("warm campaign incomplete")
    qemu_digest = file_sha256(qemu)
    input_digests = [file_sha256(path) for path in inputs]
    blocks = {}
    for line in (args.out / "runs.jsonl").read_text().splitlines():
        row = json.loads(line)
        run = json.loads(Path(row["run_json"]).read_text())
        i = row["input"]
        ns = verify_run(run, int(cases[i]["expected"], 16), qemu_digest, input_digests[i])
        blocks.setdefault((i, row["rep"]), {})[row["arm"]] = ns
    report = {"runtime": meta["qemu_sha256"], "plugin": meta["plugin_sha256"],
              "affinity": sorted(os.sched_getaffinity(0)), "kernel": platform.release(),
              "python_version": platform.python_version(),
              "cpuinfo": [line for line in Path("/proc/cpuinfo").read_text().splitlines()
                          if line.startswith(("model name", "Hardware"))][:1], "baseline": str(snap),
              "arms": sources, "seed": 20261118, "samples_ns": [
                  {"input": i, "rep": rep, "ns": x} for (i, rep), x in sorted(blocks.items())],
              "speedup": {}}
    if len(blocks) != args.pairs * len(inputs) or any(
            set(row) != {"warm", *sources} for row in blocks.values()):
        raise ValueError("campaign did not produce complete paired blocks")
    for arm in sources:
        report["speedup"][arm] = summarize(blocks, arm, "warm")
    if {"partial", "complete"} <= sources.keys():
        report["complete_vs_partial"] = summarize(blocks, "complete", "partial")
    report["paired_elapsed_ns_medians_by_input"] = {
        str(index): {
            arm: statistics.median(row[arm] for (i, _), row in blocks.items() if i == index)
            for arm in ("warm", *sources)
        }
        for index in range(len(inputs))
    }
    (args.out / "ratios.json").write_text(json.dumps(report, indent=2) + "\n")

if __name__ == "__main__":
    main()

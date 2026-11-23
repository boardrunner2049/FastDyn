#!/usr/bin/env python3
"""Paired real-firmware timing for warm, current-scopt and firmware-inline.

Each sample is a fresh FastDyn process with a freshly deployed but already
warm cache.  Arm order is shuffled inside every block.  Diagnostics such as
QEMU_SC_V7_STATS are intentionally excluded from these timing runs; collect
them with ``scopt rundir launch --v7-stats`` first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import statistics
from pathlib import Path

from scopt.run.deploy import deploy
from scopt.run.launcher import create_run_dir, launch


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def interval(values: list[float], rng: random.Random, rounds: int = 10_000) -> list[float]:
    if not values:
        return [float("nan"), float("nan")]
    draws = sorted(statistics.median(rng.choices(values, k=len(values)))
                   for _ in range(rounds))
    return [draws[int(.025 * rounds)], draws[min(rounds - 1, int(.975 * rounds))]]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--qemu", required=True, type=Path)
    p.add_argument("--warm", required=True, type=Path)
    p.add_argument("--current", required=True, type=Path)
    p.add_argument("--firmware-inline", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--pairs", type=int, default=12)
    p.add_argument("--seed", type=int, default=0xF17E)
    p.add_argument("--timeout", type=float, default=120.0)
    args = p.parse_args()
    if args.pairs < 2:
        p.error("--pairs must be at least 2")
    args.out.mkdir(parents=True, exist_ok=False)
    artifacts = {"warm": args.warm.resolve(), "current": args.current.resolve(),
                 "firmware-inline": getattr(args, "firmware_inline").resolve()}
    rng = random.Random(args.seed)
    rows = []
    expected_norm = None
    for block in range(args.pairs):
        order = list(artifacts)
        rng.shuffle(order)
        for arm in order:
            rd = create_run_dir(args.config.resolve(), args.out / f"b{block:02d}-{arm}",
                                qemu=args.qemu.resolve(), label=f"firmware-inline-{block}-{arm}")
            dep = deploy(artifacts[arm], rd, timeout_s=args.timeout)
            run = launch(rd, timeout_s=args.timeout)
            if run["verdict"] != "PASS" or run["appended"] != 0 or not run["bench"]:
                raise RuntimeError(f"block {block} {arm}: invalid run {run['verdict']}, "
                                   f"appended={run['appended']}")
            if expected_norm is None:
                expected_norm = run["guest_norm"]
            if run["guest_norm"] != expected_norm:
                raise RuntimeError(f"block {block} {arm}: guest output changed")
            rows.append({"block": block, "order": order, "arm": arm,
                         "native_ns": run["bench"][0]["elapsed_ns"],
                         "wall_s": run["timings"]["wall_s"],
                         "deploy_s": dep["deploy_seconds"],
                         "appended": run["appended"],
                         "artifact": str(artifacts[arm]),
                         "artifact_file_sha256": sha256(artifacts[arm] / "cache.sc"),
                         "run_dir": str(rd.root)})
    with (args.out / "runs.jsonl").open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
    by_block = {b: {r["arm"]: r for r in rows if r["block"] == b}
                for b in range(args.pairs)}
    comparisons = {}
    for baseline, candidate in (("warm", "current"), ("warm", "firmware-inline"),
                                ("current", "firmware-inline")):
        ratios = [by_block[b][baseline]["native_ns"] /
                  by_block[b][candidate]["native_ns"] for b in by_block]
        comparisons[f"{candidate}_vs_{baseline}"] = {
            "speedup_per_block": ratios, "median_speedup": statistics.median(ratios),
            "bootstrap_95": interval(ratios, random.Random(args.seed ^ len(comparisons)))}
    summary = {"schema": "scopt.firmware-inline-timing/1", "pairs": args.pairs,
               "seed": args.seed, "host": {"node": platform.node(),
               "platform": platform.platform(), "cpu_count": os.cpu_count(),
               "affinity": sorted(os.sched_getaffinity(0))},
               "qemu": str(args.qemu.resolve()), "qemu_sha256": sha256(args.qemu),
               "artifacts": {k: str(v) for k, v in artifacts.items()},
               "native_ns": {arm: [r["native_ns"] for r in rows if r["arm"] == arm]
                             for arm in artifacts},
               "comparisons": comparisons}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

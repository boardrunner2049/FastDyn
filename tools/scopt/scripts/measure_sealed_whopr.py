#!/usr/bin/env python3
"""Measure compiled trusted-sealed launchers in randomized paired blocks.

The launchers are prepared before this script starts, so cache production,
deployment, validation, and Python frontend time are outside the firmware
``[BENCH]`` interval.  Each scored observation is a fresh QEMU process pinned
to one CPU.  Cache hashes and normalized register output are checked before a
result is published.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import re
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any


BENCH = re.compile(r"^\[BENCH\]\[fw\].*\belapsed_ns=(\d+)\b", re.MULTILINE)
REG = re.compile(r"^Reg ([0-3]):(0x[0-9a-fA-F]{8})$", re.MULTILINE)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def cache_path(launcher: Path) -> Path:
    doc = json.loads((launcher.parent / "launch.json").read_text())
    for index, arg in enumerate(doc["argv"]):
        if arg == "-accel" and index + 1 < len(doc["argv"]):
            for item in doc["argv"][index + 1].split(","):
                if item.startswith("supercache="):
                    return Path(item.split("=", 1)[1]).resolve()
    raise ValueError(f"{launcher}: launch.json has no SuperCache path")


def interval(values: list[float], seed: int, rounds: int = 20_000) -> list[float]:
    rng = random.Random(seed)
    draws = sorted(statistics.median(rng.choices(values, k=len(values)))
                   for _ in range(rounds))
    return [draws[int(rounds * .025)], draws[int(rounds * .975)]]


def run(launcher: Path, cpu: int, log: Path) -> dict[str, Any]:
    env = dict(os.environ)
    env["QEMU_SC_SEALED"] = "1"
    started = time.monotonic_ns()
    completed = subprocess.run(
        ["taskset", "-c", str(cpu), str(launcher)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env, check=False,
    )
    wall_ns = time.monotonic_ns() - started
    log.write_text(completed.stdout)
    benches = BENCH.findall(completed.stdout)
    registers = [value.lower() for _, value in REG.findall(completed.stdout)]
    if completed.returncode or len(benches) != 1 or len(registers) != 16:
        raise RuntimeError(f"invalid launch {launcher}; see {log}")
    return {"native_ns": int(benches[0]), "wall_ns": wall_ns,
            "registers": registers, "log": str(log)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True,
                        help="compiled baseline run.sh")
    parser.add_argument("--candidate", type=Path, required=True,
                        help="compiled WHOPR-7 run.sh")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=6)
    parser.add_argument("--cpu", type=int, default=2)
    parser.add_argument("--seed", type=int, default=0x57484F50)
    args = parser.parse_args()
    if args.pairs < 2:
        parser.error("--pairs must be at least two")
    launchers = {"baseline": args.baseline.resolve(strict=True),
                 "whopr7": args.candidate.resolve(strict=True)}
    args.out.mkdir(parents=True, exist_ok=False)
    caches = {name: cache_path(path) for name, path in launchers.items()}
    before = {name: sha256(path) for name, path in caches.items()}
    rng = random.Random(args.seed)
    rows: list[dict[str, Any]] = []
    expected: list[str] | None = None
    for block in range(args.pairs):
        order = list(launchers)
        rng.shuffle(order)
        for sequence, name in enumerate(order):
            observed = run(launchers[name], args.cpu,
                           args.out / f"block-{block + 1:02d}-{sequence}-{name}.log")
            if expected is None:
                expected = observed["registers"]
            elif observed["registers"] != expected:
                raise RuntimeError(f"block {block + 1} {name}: guest output changed")
            rows.append({"block": block + 1, "sequence": sequence,
                         "order": order, "arm": name, **observed})
    after = {name: sha256(path) for name, path in caches.items()}
    if before != after:
        raise RuntimeError("a measured cache changed")
    by_block = {block: {row["arm"]: row for row in rows if row["block"] == block}
                for block in range(1, args.pairs + 1)}
    ratios = [row["baseline"]["native_ns"] / row["whopr7"]["native_ns"]
              for row in by_block.values()]
    medians = {name: statistics.median(
        row["native_ns"] for row in rows if row["arm"] == name)
        for name in launchers}
    result = {
        "schema": "scopt.sealed-whopr-timing/1",
        "pairs": args.pairs,
        "seed": args.seed,
        "cpu": args.cpu,
        "host": {"node": platform.node(), "platform": platform.platform(),
                 "affinity": sorted(os.sched_getaffinity(0))},
        "launchers": {name: str(path) for name, path in launchers.items()},
        "caches": {name: {"path": str(caches[name]), "sha256": before[name]}
                   for name in launchers},
        "registers": expected,
        "runs": rows,
        "median_native_ns": medians,
        "paired_speedups": ratios,
        "median_paired_speedup": statistics.median(ratios),
        "bootstrap_95": interval(ratios, args.seed ^ 0x37),
    }
    (args.out / "results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

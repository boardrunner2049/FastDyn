#!/usr/bin/env python3
"""Run randomized paired PX4 missions for the scoped memset layout experiment."""
from __future__ import annotations

import argparse
import json
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
MISSION = (
    ROOT
    / "virtuals/physics/flight_controllers/courbet/tests/smoke_px4_mission.py"
)


def run_mission(*, python: Path, config: Path, port: int) -> dict[str, object]:
    command = [
        str(python),
        str(MISSION),
        "--timeout",
        "240",
        "--base-port",
        str(port),
        "--runtime-config",
        str(config),
    ]
    started_ns = time.monotonic_ns()
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    elapsed_ns = time.monotonic_ns() - started_ns
    return {
        "command": command,
        "elapsed_ns": elapsed_ns,
        "returncode": completed.returncode,
        "output": completed.stdout,
        "passed": completed.returncode == 0 and "PASS: real PX4" in completed.stdout,
    }


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * q)]


def bootstrap_interval(ratios: list[float], seed: int) -> list[float]:
    rng = random.Random(seed)
    boot = [
        statistics.median(rng.choices(ratios, k=len(ratios)))
        for _ in range(20_000)
    ]
    return [percentile(boot, 0.025), percentile(boot, 0.975)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--blocks", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20261122)
    parser.add_argument("--base-port", type=int, default=18000)
    parser.add_argument(
        "--python", type=Path, default=ROOT / "fastdyn-env/bin/python"
    )
    args = parser.parse_args()
    if args.blocks < 2:
        parser.error("--blocks must be at least two")

    rng = random.Random(args.seed)
    records: list[dict[str, object]] = []
    for block in range(args.blocks):
        order = ["baseline", "candidate"]
        rng.shuffle(order)
        for position, variant in enumerate(order):
            config = args.baseline if variant == "baseline" else args.candidate
            record = run_mission(
                python=args.python,
                config=config.resolve(),
                port=args.base_port + (block * 2 + position) * 3,
            )
            record.update({"block": block, "position": position, "variant": variant})
            records.append(record)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                "\n".join(json.dumps(item, sort_keys=True) for item in records) + "\n",
                encoding="utf-8",
            )
            print(
                f"block={block} position={position} variant={variant} "
                f"elapsed_s={record['elapsed_ns'] / 1e9:.6f} passed={record['passed']}",
                flush=True,
            )
            if not record["passed"]:
                return 1

    by_block: dict[int, dict[str, float]] = {}
    for record in records:
        by_block.setdefault(int(record["block"]), {})[str(record["variant"])] = (
            int(record["elapsed_ns"]) / 1e9
        )
    ratios = [
        pair["baseline"] / pair["candidate"]
        for _, pair in sorted(by_block.items())
    ]
    summary = {
        "blocks": args.blocks,
        "seed": args.seed,
        "paired_speedups": ratios,
        "median_paired_speedup": statistics.median(ratios),
        "bootstrap_95_interval": bootstrap_interval(ratios, args.seed + 1),
        "baseline_median_s": statistics.median(
            pair["baseline"] for pair in by_block.values()
        ),
        "candidate_median_s": statistics.median(
            pair["candidate"] for pair in by_block.values()
        ),
    }
    summary_path = args.output.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Interleave finite whole-program variants and verify identical output."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import resource
import statistics
import subprocess
import time
from pathlib import Path


TICK_RE = re.compile(r" tick=\d+$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def observe(path: Path, program_arguments: tuple[str, ...] = ()) -> dict[str, object]:
    before_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    before = time.perf_counter_ns()
    result = subprocess.run(
        [str(path), *program_arguments], capture_output=True, text=True, check=False
    )
    elapsed = time.perf_counter_ns() - before
    after_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "wall_ns": elapsed,
        "user_ns": round((after_usage.ru_utime - before_usage.ru_utime) * 1e9),
        "system_ns": round((after_usage.ru_stime - before_usage.ru_stime) * 1e9),
        "cpu_ns": round(
            (
                after_usage.ru_utime
                - before_usage.ru_utime
                + after_usage.ru_stime
                - before_usage.ru_stime
            )
            * 1e9
        ),
        "max_rss_kib": after_usage.ru_maxrss,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def median(samples: list[dict[str, object]], field: str) -> float:
    return statistics.median(float(sample[field]) for sample in samples)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", action="append", required=True)
    parser.add_argument("--samples", type=int, default=21)
    parser.add_argument("--seed", type=int, default=20261116)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--paired-blocks", action="store_true")
    parser.add_argument(
        "--program-arg", action="append", default=[],
        help="argument passed identically to every variant",
    )
    args = parser.parse_args()

    variants: dict[str, Path] = {}
    for specification in args.variant:
        name, separator, raw_path = specification.partition("=")
        if not separator:
            parser.error(f"invalid variant: {specification}")
        variants[name] = Path(raw_path).resolve()

    program_arguments = tuple(str(item) for item in args.program_arg)
    reference_output: str | None = None
    for name, path in variants.items():
        warmup = observe(path, program_arguments)
        if warmup["returncode"] != 0:
            raise RuntimeError(f"{name} warmup failed: {warmup}")
        normalized = TICK_RE.sub("", str(warmup["stdout"]))
        if reference_output is None:
            reference_output = normalized
        elif normalized != reference_output:
            raise RuntimeError(f"{name} output differs from reference")

    randomizer = random.Random(args.seed)
    order: list[tuple[int, str]] = []
    if args.paired_blocks:
        for block in range(args.samples):
            block_order = list(variants)
            randomizer.shuffle(block_order)
            order.extend((block, name) for name in block_order)
    else:
        names = [name for _ in range(args.samples) for name in variants]
        randomizer.shuffle(names)
        order = [(sequence, name) for sequence, name in enumerate(names)]
    samples: dict[str, list[dict[str, object]]] = {name: [] for name in variants}
    for sequence, (block, name) in enumerate(order, 1):
        sample = observe(variants[name], program_arguments)
        sample["sequence"] = sequence
        sample["block"] = block
        sample["output_matches"] = TICK_RE.sub("", str(sample["stdout"])) == reference_output
        samples[name].append(sample)
        print(f"[{sequence}/{len(order)}] {name}: {sample['wall_ns'] / 1e9:.4f}s", flush=True)

    reference_name = next(iter(variants))
    reference_median = median(samples[reference_name], "wall_ns")
    report = {
        "schema_version": 1,
        "method": {
            "measurement": "fresh-process wall time",
            "samples_per_variant": args.samples,
            "interleaved_randomization_seed": args.seed,
            "reference_variant": reference_name,
            "paired_blocks": args.paired_blocks,
            "semantic_output": reference_output,
            "program_arguments": list(program_arguments),
        },
        "variants": {},
    }
    for name, path in variants.items():
        wall_median = median(samples[name], "wall_ns")
        report["variants"][name] = {
            "path": str(path),
            "sha256": sha256(path),
            "file_bytes": path.stat().st_size,
            "all_outputs_match": all(bool(item["output_matches"]) for item in samples[name]),
            "samples": samples[name],
            "median_wall_ns": wall_median,
            "median_user_ns": median(samples[name], "user_ns"),
            "median_system_ns": median(samples[name], "system_ns"),
            "median_cpu_ns": median(samples[name], "cpu_ns"),
            "reference_over_variant": reference_median / wall_median,
        }
    if args.paired_blocks:
        paired = {}
        reference_by_block = {
            int(item["block"]): item for item in samples[reference_name]
        }
        for name in variants:
            if name == reference_name:
                continue
            candidate_by_block = {int(item["block"]): item for item in samples[name]}
            comparisons = {}
            for metric in ("wall_ns", "cpu_ns"):
                ratios = [
                    float(reference_by_block[block][metric])
                    / float(candidate_by_block[block][metric])
                    for block in range(args.samples)
                ]
                bootstrap = random.Random(args.seed + (1 if metric == "wall_ns" else 2))
                medians = [
                    statistics.median(
                        ratios[bootstrap.randrange(len(ratios))] for _ in ratios
                    )
                    for _ in range(100_000)
                ]
                comparisons[metric] = {
                    "ratio_definition": f"reference {metric} / candidate {metric}",
                    "paired_ratios": ratios,
                    "median_ratio": statistics.median(ratios),
                    "bootstrap_95_percent": [
                        percentile(medians, 0.025), percentile(medians, 0.975)
                    ],
                    "bootstrap_resamples": 100_000,
                }
            paired[name] = comparisons
        report["paired_comparisons"] = paired
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({name: data["reference_over_variant"] for name, data in report["variants"].items()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

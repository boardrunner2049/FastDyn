#!/usr/bin/env python3
"""Benchmark SmartSpeaker ELFs for exact completed interrupt-handler work."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import resource
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any


EVENT = b"[JS:Debug] Interrupt_Handler_Task running"
INTERRUPT_EVENTS = frozenset({
    b"WWDG_IRQ", b"RTC_WKUP_IRQ", b"DMA1_Stream3_IRQ",
    b"DMA1_Stream4_IRQ", b"DMA1_Stream5_IRQ", b"DMA1_Stream6_IRQ",
    b"USART1_IRQ", b"USART2_IRQ", b"RTC_Alarm_IRQ", b"SDIO_IRQ",
})
DRIVER_RE = re.compile(
    rb"FAST_FIXED_WORK start_tick=(\d+) end_tick=(\d+) completed_ticks=(\d+) "
    rb"start_markers=(\d+) end_markers=(\d+) completed_units=(\d+) "
    rb"driver_cpu_ns=(\d+)"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def observe(
    path: Path,
    preload: Path,
    work_units: int,
    tick_address: int,
) -> dict[str, Any]:
    environment = dict(os.environ)
    environment.update({
        "LD_PRELOAD": str(preload),
        "FASTDYN_FIXED_WORK_UNITS": str(work_units),
        "FASTDYN_TICK_COUNTER_ADDRESS": hex(tick_address),
        "FASTDYN_FIXED_TICK_DRIVER_CPU": "25",
    })
    before_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter_ns()
    result = subprocess.run(
        [str(path)],
        env=environment,
        capture_output=True,
        timeout=120,
        check=False,
    )
    wall_ns = time.perf_counter_ns() - started
    after_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    process_cpu_ns = round(
        (
            after_usage.ru_utime - before_usage.ru_utime
            + after_usage.ru_stime - before_usage.ru_stime
        ) * 1e9
    )
    match = DRIVER_RE.search(result.stderr)
    driver = None
    if match is not None:
        driver = {
            "start_tick": int(match.group(1)),
            "end_tick": int(match.group(2)),
            "completed_ticks": int(match.group(3)),
            "start_markers": int(match.group(4)),
            "end_markers": int(match.group(5)),
            "completed_units": int(match.group(6)),
            "cpu_ns": int(match.group(7)),
        }
    trace = [line for line in result.stdout.splitlines() if line in INTERRUPT_EVENTS]
    trace_prefix = trace[:400]
    target_cpu_ns = process_cpu_ns - int(driver["cpu_ns"]) if driver else None
    ok = bool(
        result.returncode == 2
        and driver is not None
        and driver["completed_ticks"] > 0
        and driver["end_tick"] - driver["start_tick"]
        == driver["completed_ticks"]
        and driver["completed_units"] == work_units
        and target_cpu_ns is not None
        and target_cpu_ns > 0
        and result.stdout.count(EVENT) > 0
        and len(trace_prefix) == 400
    )
    failure_reasons = []
    if result.returncode != 2:
        failure_reasons.append("unexpected_returncode")
    if driver is None:
        failure_reasons.append("missing_fixed_work_driver_record")
    elif driver["completed_units"] != work_units:
        failure_reasons.append("completed_work_mismatch")
    if target_cpu_ns is None or target_cpu_ns <= 0:
        failure_reasons.append("invalid_target_cpu")
    if len(trace_prefix) != 400:
        failure_reasons.append("short_interrupt_prefix")
    observation = {
        "returncode": result.returncode,
        "ok": ok,
        "wall_ns": wall_ns,
        "process_cpu_ns": process_cpu_ns,
        "driver_cpu_ns": driver["cpu_ns"] if driver else None,
        "target_cpu_ns": target_cpu_ns,
        "start_tick": driver["start_tick"] if driver else None,
        "end_tick": driver["end_tick"] if driver else None,
        "completed_ticks": driver["completed_ticks"] if driver else None,
        "completed_units": driver["completed_units"] if driver else None,
        "dispatch_markers": result.stdout.count(EVENT),
        "interrupt_trace_events": len(trace),
        "interrupt_trace_prefix_sha256": hashlib.sha256(
            b"\n".join(trace_prefix) + b"\n"
        ).hexdigest(),
        "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
        "stdout_bytes": len(result.stdout),
        "stderr_bytes": len(result.stderr),
        "failure_reasons": failure_reasons,
    }
    if not ok:
        observation["failure_stdout_tail"] = result.stdout[-8192:].decode(
            "utf-8", errors="replace"
        )
        observation["failure_stderr_tail"] = result.stderr[-8192:].decode(
            "utf-8", errors="replace"
        )
    return observation


def comparison(
    reference: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    metric: str,
    seed: int,
) -> dict[str, Any]:
    reference_by_block = {int(item["block"]): item for item in reference}
    candidate_by_block = {int(item["block"]): item for item in candidate}
    ratios = [
        float(reference_by_block[block][metric])
        / float(candidate_by_block[block][metric])
        for block in sorted(reference_by_block)
    ]
    generator = random.Random(seed)
    medians = [
        statistics.median(
            ratios[generator.randrange(len(ratios))] for _ in ratios
        )
        for _ in range(100_000)
    ]
    return {
        "ratio_definition": f"stock BOLT {metric} / candidate {metric}",
        "median_ratio": statistics.median(ratios),
        "bootstrap_95_percent": [
            percentile(medians, 0.025), percentile(medians, 0.975)
        ],
        "paired_ratios": ratios,
        "bootstrap_resamples": 100_000,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", action="append", required=True)
    parser.add_argument("--preload", type=Path, required=True)
    parser.add_argument("--work-units", type=int, required=True)
    parser.add_argument("--tick-address", type=lambda value: int(value, 0), required=True)
    parser.add_argument(
        "--variant-tick-address", action="append", default=[],
        help="optional NAME=ADDRESS override for a variant with a different link layout",
    )
    parser.add_argument("--samples", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument(
        "--affinity-cpus", default="24,25",
        help="comma-separated target-process CPUs, recorded as measurement metadata",
    )
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.work_units <= 0 or args.samples <= 0:
        parser.error("work units and samples must be positive")
    try:
        affinity_cpus = [int(item) for item in args.affinity_cpus.split(",")]
    except ValueError as error:
        parser.error(f"invalid --affinity-cpus: {error}")
    if not affinity_cpus or any(cpu < 0 for cpu in affinity_cpus):
        parser.error("--affinity-cpus must contain nonnegative CPU identifiers")

    variants: dict[str, Path] = {}
    for specification in args.variant:
        name, separator, raw_path = specification.partition("=")
        if not separator:
            parser.error(f"invalid variant: {specification}")
        variants[name] = Path(raw_path).resolve()
    if next(iter(variants)) != "stock_bolt":
        parser.error("stock_bolt must be the first/reference variant")
    tick_addresses = {name: args.tick_address for name in variants}
    for specification in args.variant_tick_address:
        name, separator, raw_address = specification.partition("=")
        if not separator or name not in variants:
            parser.error(f"invalid --variant-tick-address: {specification}")
        try:
            tick_addresses[name] = int(raw_address, 0)
        except ValueError as error:
            parser.error(f"invalid --variant-tick-address: {error}")

    preload = args.preload.resolve()
    generator = random.Random(args.seed)
    order: list[tuple[int, str]] = []
    for block in range(args.samples):
        names = list(variants)
        generator.shuffle(names)
        order.extend((block, name) for name in names)
    journal = args.journal.resolve() if args.journal is not None else None
    identity = {
        "schema_version": 1,
        "record": "header",
        "seed": args.seed,
        "work_units": args.work_units,
        "preload_sha256": sha256(preload),
        "variants": {name: sha256(path) for name, path in variants.items()},
        "tick_addresses": tick_addresses,
        "order_length": len(order),
    }
    samples: dict[str, list[dict[str, Any]]] = {name: [] for name in variants}
    completed = 0
    failed_attempts: list[dict[str, Any]] = []
    if journal is not None and journal.exists():
        if not args.resume:
            raise RuntimeError(f"journal exists; pass --resume: {journal}")
        records = [json.loads(line) for line in journal.read_text().splitlines()]
        if not records:
            raise RuntimeError("journal is empty")
        header = dict(records[0])
        warmups = header.pop("warmups", None)
        if header != identity or not isinstance(warmups, dict):
            raise RuntimeError("journal identity differs from requested experiment")
        for record in records[1:]:
            expected_sequence = completed + 1
            block, name = order[completed]
            sample = record.get("sample")
            if (
                record.get("record") != "sample"
                or record.get("sequence") != expected_sequence
                or record.get("block") != block
                or record.get("variant") != name
                or not isinstance(sample, dict)
            ):
                raise RuntimeError("journal is not a deterministic attempt prefix")
            if not sample.get("ok"):
                raise RuntimeError(
                    "journal is not a successful deterministic prefix: "
                    f"sample {expected_sequence} for {name} failed"
                )
            samples[name].append(sample)
            completed += 1
    else:
        if args.resume:
            raise RuntimeError("cannot resume: journal does not exist")
        warmups = {
            name: observe(
                path, preload, min(args.work_units, 1_000),
                tick_addresses[name],
            )
            for name, path in variants.items()
        }
        if not all(item["ok"] for item in warmups.values()):
            raise RuntimeError(f"fixed-tick warmup failed: {warmups}")
        if len({item["interrupt_trace_prefix_sha256"] for item in warmups.values()}) != 1:
            raise RuntimeError("fixed-tick warmup trace prefixes differ")
        if len({item["dispatch_markers"] for item in warmups.values()}) != 1:
            raise RuntimeError("fixed-tick warmup completed work differs")
        if journal is not None:
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text(json.dumps({**identity, "warmups": warmups}, sort_keys=True) + "\n")

    for sequence, (block, name) in enumerate(order[completed:], completed + 1):
        sample = observe(
            variants[name], preload, args.work_units,
            tick_addresses[name],
        )
        sample.update({"sequence": sequence, "block": block})
        samples[name].append(sample)
        if journal is not None:
            with journal.open("a") as stream:
                stream.write(json.dumps({
                    "record": "sample",
                    "sequence": sequence,
                    "block": block,
                    "variant": name,
                    "sample": sample,
                }, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        target_cpu = sample["target_cpu_ns"]
        target_text = (
            f"{target_cpu / 1e9:.3f}s" if target_cpu is not None else "unavailable"
        )
        print(
            f"[{sequence}/{len(order)}] {name}: work={sample['completed_units']} "
            f"wall={sample['wall_ns'] / 1e9:.3f}s "
            f"target_cpu={target_text} ok={sample['ok']}",
            flush=True,
        )
        if not sample["ok"]:
            raise RuntimeError(
                f"fixed-work sample failed at sequence {sequence}; journal preserved"
            )

    traces = {
        item["interrupt_trace_prefix_sha256"]
        for group in samples.values() for item in group
    }
    markers = {
        item["dispatch_markers"] for group in samples.values() for item in group
    }
    all_ok = all(item["ok"] for group in samples.values() for item in group)
    if not all_ok or len(traces) != 1 or len(markers) != 1:
        raise RuntimeError(
            f"fixed-work validation failed: all_ok={all_ok}, "
            f"traces={len(traces)}, marker_counts={sorted(markers)}"
        )

    reference = samples["stock_bolt"]
    paired = {}
    for candidate_index, name in enumerate(list(variants)[1:], 1):
        paired[name] = {
            metric: comparison(
                reference, samples[name], metric,
                args.seed + candidate_index * 10 + metric_index,
            )
            for metric_index, metric in enumerate(
                ("target_cpu_ns", "wall_ns", "process_cpu_ns")
            )
        }
    report = {
        "schema_version": 1,
        "status": "valid_fixed_tick_comparison",
        "method": {
            "completed_work_units_per_execution": args.work_units,
            "tick_schedule": "closed loop: inject next acknowledged tick until exact work completes",
            "samples_per_variant": args.samples,
            "randomization_seed": args.seed,
            "tick_counter_address": args.tick_address,
            "tick_counter_addresses": tick_addresses,
            "affinity_cpus": affinity_cpus,
            "driver_cpu": 25,
            "primary_metric": "process CPU minus measured driver-thread CPU",
            "journal": str(journal) if journal is not None else None,
            "resumed_successful_prefix": completed,
        },
        "preload": {"path": str(preload), "sha256": sha256(preload)},
        "validation": {
            "all_executions_ok": all_ok,
            "dispatch_markers_per_execution": next(iter(markers)),
            "trace_prefix_events": 400,
            "trace_prefix_sha256": next(iter(traces)),
            "failed_attempt_count": len(failed_attempts),
            "failed_attempts": failed_attempts,
        },
        "variants": {
            name: {
                "path": str(path), "sha256": sha256(path),
                "file_bytes": path.stat().st_size,
                "warmup": warmups[name], "samples": samples[name],
            }
            for name, path in variants.items()
        },
        "paired_comparisons": paired,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        name: {
            metric: {
                "median": value["median_ratio"],
                "ci": value["bootstrap_95_percent"],
            }
            for metric, value in metrics.items()
        }
        for name, metrics in paired.items()
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

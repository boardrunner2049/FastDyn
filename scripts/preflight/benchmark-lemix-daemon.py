#!/usr/bin/env python3
"""Measure fixed-duration work and CPU cost for a LEMIX-style RTOS daemon."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import resource
import signal
import statistics
import subprocess
import tempfile
import time
from pathlib import Path


EVENT = b"[JS:Debug] Interrupt_Handler_Task running"
INTERRUPT_EVENTS = frozenset({
    b"WWDG_IRQ", b"RTC_WKUP_IRQ", b"DMA1_Stream3_IRQ",
    b"DMA1_Stream4_IRQ", b"DMA1_Stream5_IRQ", b"DMA1_Stream6_IRQ",
    b"USART1_IRQ", b"USART2_IRQ", b"RTC_Alarm_IRQ", b"SDIO_IRQ",
})
TRACE_PREFIX_EVENTS = 400


def sha256(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def observe(path: Path, preload: Path, duration: float) -> dict[str, object]:
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    environment = dict(os.environ)
    environment["LD_PRELOAD"] = str(preload)
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            [str(path)], stdout=output, stderr=subprocess.STDOUT,
            env=environment, start_new_session=True,
        )
        time.sleep(duration)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        ended = time.monotonic()
        output.seek(0)
        content = output.read()
    interrupt_trace = [
        line for line in content.splitlines() if line in INTERRUPT_EVENTS
    ]
    trace_prefix = interrupt_trace[:TRACE_PREFIX_EVENTS]
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "wall_seconds": ended - started,
        "user_seconds": after.ru_utime - before.ru_utime,
        "system_seconds": after.ru_stime - before.ru_stime,
        "cpu_seconds": (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime),
        "interrupt_dispatches": content.count(EVENT),
        "interrupt_trace_events": len(interrupt_trace),
        "interrupt_trace_prefix_events": len(trace_prefix),
        "interrupt_trace_prefix_sha256": hashlib.sha256(
            b"\n".join(trace_prefix) + (b"\n" if trace_prefix else b"")
        ).hexdigest(),
        "output_bytes": len(content),
        "returncode": process.returncode,
        "ok": process.returncode == 2 and content.count(EVENT) > 0,
    }


def median(items: list[dict[str, object]], key: str) -> float:
    return statistics.median(float(item[key]) for item in items)


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
    parser.add_argument("--preload", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--samples", type=int, default=11)
    parser.add_argument("--seed", type=int, default=20261116)
    parser.add_argument(
        "--paired-blocks", action="store_true",
        help="randomize variants within each repetition block and retain pairing",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    variants: dict[str, Path] = {}
    for specification in args.variant:
        name, separator, raw_path = specification.partition("=")
        if not separator:
            parser.error(f"invalid variant: {specification}")
        variants[name] = Path(raw_path).resolve()

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
        sample = observe(variants[name], args.preload.resolve(), args.duration)
        sample["sequence"] = sequence
        sample["block"] = block
        samples[name].append(sample)
        print(f"[{sequence}/{len(order)}] {name}: events={sample['interrupt_dispatches']} "
              f"cpu={sample['cpu_seconds']:.3f}s ok={sample['ok']}", flush=True)

    reference = next(iter(variants))
    reference_events = median(samples[reference], "interrupt_dispatches")
    reference_cpu = median(samples[reference], "cpu_seconds")
    report: dict[str, object] = {
        "schema_version": 1,
        "method": {
            "duration_seconds": args.duration,
            "samples_per_variant": args.samples,
            "order_seed": args.seed,
            "reference": reference,
            "paired_blocks": args.paired_blocks,
            "work_metric": "application interrupt-dispatch loop iterations",
            "fidelity": "zero-backed MMIO addressability only; no device model",
        },
        "preload": {"path": str(args.preload.resolve()), "sha256": sha256(args.preload.resolve())},
        "variants": {},
    }
    for name, path in variants.items():
        events = median(samples[name], "interrupt_dispatches")
        cpu = median(samples[name], "cpu_seconds")
        report["variants"][name] = {
            "path": str(path), "sha256": sha256(path), "file_bytes": path.stat().st_size,
            "all_ok": all(bool(item["ok"]) for item in samples[name]),
            "median_interrupt_dispatches": events,
            "median_cpu_seconds": cpu,
            "event_rate_relative_to_reference": events / reference_events,
            "reference_cpu_over_variant": reference_cpu / cpu,
            "samples": samples[name],
        }
    if args.paired_blocks:
        paired: dict[str, object] = {}
        reference_by_block = {int(item["block"]): item for item in samples[reference]}
        for name in variants:
            if name == reference:
                continue
            candidate_by_block = {int(item["block"]): item for item in samples[name]}
            ratios = []
            for block in range(args.samples):
                ref = reference_by_block[block]
                candidate = candidate_by_block[block]
                ref_cost = float(ref["cpu_seconds"]) / int(ref["interrupt_dispatches"])
                candidate_cost = (
                    float(candidate["cpu_seconds"])
                    / int(candidate["interrupt_dispatches"])
                )
                ratios.append(ref_cost / candidate_cost)
            bootstrap_random = random.Random(args.seed + 1)
            medians = []
            for _ in range(100_000):
                resample = [
                    ratios[bootstrap_random.randrange(len(ratios))]
                    for _ in ratios
                ]
                medians.append(statistics.median(resample))
            paired[name] = {
                "ratio_definition": "reference CPU cost per marker / candidate CPU cost per marker",
                "paired_ratios": ratios,
                "median_ratio": statistics.median(ratios),
                "bootstrap_95_percent": [
                    percentile(medians, 0.025), percentile(medians, 0.975)
                ],
                "bootstrap_resamples": 100_000,
            }
        report["paired_comparisons"] = paired
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({name: {"events_ratio": item["event_rate_relative_to_reference"],
                                  "cpu_ratio": item["reference_cpu_over_variant"]}
                      for name, item in report["variants"].items()}, sort_keys=True))
    return 0 if all(item["all_ok"] for item in report["variants"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

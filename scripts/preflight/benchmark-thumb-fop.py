#!/usr/bin/env python3
"""Measure the bounded Thumb FOP rule on a fixed-work Cortex-M3 fixture."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import random
import re
import resource
import shutil
import statistics
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastdyn.preflight import (
    Decision,
    EmitInPlace,
    PassRegistry,
    PreflightContext,
    PreflightPipeline,
    ThumbStackForwardingPass,
)
from fastdyn.preflight.model import canonical_json_bytes, hash_file


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGE = ROOT / "build/preflight/thumb-fop-benchmark/thumb-fop-benchmark.elf"
DEFAULT_FASTDYN = ROOT / "fastdyn-env/bin/fastdyn"
DEFAULT_OUTPUT = ROOT / "build/preflight/thumb-fop-benchmark/results"
BENCH_RE = re.compile(r"\[BENCH\]\[thumb-fop\]\s+elapsed_ns=(\d+)")
ITERATIONS = 25_000_000


def load_pilot() -> Any:
    path = ROOT / "scripts/preflight/run-thumb-preflight-pilot.py"
    spec = importlib.util.spec_from_file_location("thumb_preflight_pilot", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value: Any) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def symbol_address(image: Path, name: str) -> int:
    result = subprocess.run(
        ["arm-none-eabi-nm", "-n", str(image)],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[2] == name:
            return int(fields[0], 16) & ~1
    raise RuntimeError(f"missing symbol {name}")


def config_text(image: Path, start: int, end: int, tag: str) -> str:
    return f'''[Machine]
platform = "generic-cortexm"
monitor_port = 0
qmp_socket = "/tmp/fastdyn-thumb-fop-{tag}.qmp"
log_options = "none"
semihosting = false
coverage = false

[Memory.main]
id = "ram0"
base_address = "0x20000000"
memory_size = "64K"
memory_type = "SRAM"
backend = "ram"
share = false

[CPU]
[[CPU.cpu0]]
arch = "arm"
machine = "cortexm"
cpu = "cortex-m3"
plugin_library = "{ROOT / 'build/libfastdyn.so'}"
binary = "{image}"
init_nsvtor = "0x08000000"
twintrace = "None"
existing_config_path = false

[[CPU.cpu0.virtuals]]
at = "0x{start:08x}"
instruction = "benchmark_start"

[[CPU.cpu0.virtuals]]
at = "0x{end:08x}"
instruction = "benchmark_end"
args = ["thumb-fop"]
'''


def observe(fastdyn: Path, config: Path, output: Path) -> dict[str, Any]:
    before_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    before = time.perf_counter_ns()
    command = [str(fastdyn), "-q", "run", "-c", str(config), "-o", str(output)]
    run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    wall = time.perf_counter_ns() - before
    after_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    combined = run.stdout + run.stderr
    match = BENCH_RE.search(combined)
    return {
        "command": command,
        "returncode": run.returncode,
        "wall_ns": wall,
        "cpu_ns": round(
            (
                after_usage.ru_utime - before_usage.ru_utime
                + after_usage.ru_stime - before_usage.ru_stime
            ) * 1e9
        ),
        "guest_benchmark_ns": int(match.group(1)) if match else None,
        "completed_calls": ITERATIONS if match else 0,
        "stdout": run.stdout.splitlines(),
        "stderr": run.stderr.splitlines(),
    }


def percentile(values: list[float], fraction: float) -> float:
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def compare(samples: dict[str, list[dict[str, Any]]], seed: int) -> dict[str, Any]:
    reference = {item["block"]: item for item in samples["reference"]}
    candidate = {item["block"]: item for item in samples["candidate"]}
    result = {}
    for offset, metric in enumerate(("guest_benchmark_ns", "cpu_ns", "wall_ns")):
        ratios = [reference[index][metric] / candidate[index][metric] for index in reference]
        generator = random.Random(seed + offset)
        medians = [
            statistics.median(
                ratios[generator.randrange(len(ratios))] for _ in ratios
            )
            for _ in range(100_000)
        ]
        result[metric] = {
            "ratio_definition": f"reference {metric} / candidate {metric}",
            "median_ratio": statistics.median(ratios),
            "bootstrap_95_percent": [percentile(medians, 0.025), percentile(medians, 0.975)],
            "paired_ratios": ratios,
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    parser.add_argument("--fastdyn", type=Path, default=DEFAULT_FASTDYN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    image = args.image.resolve()
    output = args.output.resolve()
    if output.exists():
        if not args.replace:
            raise SystemExit(f"output exists: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)

    pilot = load_pilot()
    unit, decode = pilot.build_unit(image)
    states = pilot.bounded_states()
    analysis = {
        "complete": True,
        "store_identity": "thumb-1",
        "load_identity": "thumb-2",
        "width": 4,
        "base_register": "sp",
        "private_stack": True,
        "address_escaped": False,
        "observer_between": False,
        "evidence": "bounded_sp_relative_no_address_escape",
    }
    proposal = f"{unit.unit_id}:thumb-stack-forwarding"
    context = PreflightContext(
        analyses={
            "state_decision": Decision("accepted", "bounded_thumb_states_ready"),
            "effect_summary": unit.effects.semantic_dict(),
            "entry_completeness": True,
            "reference_graph": [],
            "stack_slot_forwarding": analysis,
        },
        testcase_decisions={proposal: Decision("accepted", "bounded_states_agree")},
        state_set_hash=states["state_set_hash"],
        work_budget=states["state_count"],
    )
    registry = PassRegistry()
    registry.register(ThumbStackForwardingPass())
    candidate = output / "thumb-fop-benchmark.optimized.elf"
    outcome = PreflightPipeline(registry, EmitInPlace()).execute(
        "thumb-stack-forwarding-v1", unit, context, image, candidate
    )
    if outcome.status != "accepted":
        raise RuntimeError(f"preflight rejected: {outcome.status}/{outcome.reason_code}")

    start = symbol_address(image, "benchmark_start_anchor")
    end = symbol_address(image, "benchmark_end_anchor")
    reference_config = output / "reference.toml"
    candidate_config = output / "candidate.toml"
    reference_config.write_text(config_text(image, start, end, "reference"))
    candidate_config.write_text(config_text(candidate, start, end, "candidate"))

    randomizer = random.Random(20261116)
    samples: dict[str, list[dict[str, Any]]] = {"reference": [], "candidate": []}
    sequence = 0
    for block in range(args.samples):
        order = ["reference", "candidate"]
        randomizer.shuffle(order)
        for variant in order:
            sequence += 1
            sample = observe(
                args.fastdyn.resolve(),
                reference_config if variant == "reference" else candidate_config,
                output / "runs" / f"{block:02d}-{variant}",
            )
            sample.update({"block": block, "sequence": sequence, "variant": variant})
            if sample["returncode"] != 0 or sample["completed_calls"] != ITERATIONS:
                raise RuntimeError(f"{variant} block {block} failed: {sample}")
            samples[variant].append(sample)
            print(
                f"[{sequence}/{args.samples * 2}] {variant}: "
                f"guest={sample['guest_benchmark_ns'] / 1e9:.6f}s",
                flush=True,
            )

    comparison = compare(samples, 20261116)
    report = {
        "schema_version": 1,
        "classification": "generated_cortex_m3_fixed_work_acceptance_fixture",
        "input": {"path": str(image), "sha256": hash_file(image)},
        "candidate": {"path": str(candidate), "sha256": hash_file(candidate)},
        "work": {
            "completed_calls_per_sample": ITERATIONS,
            "target_function": "vListInitialiseItem",
            "original_dynamic_stack_loads": ITERATIONS,
            "candidate_dynamic_stack_loads": 0,
        },
        "transformation": {
            "pass": "thumb-stack-forwarding-v1",
            "region_contract_hash": unit.contract_hash,
            "proposal_hash": outcome.edit.proposal_hash if outcome.edit else None,
            "stage_ledger": [item.to_dict() for item in outcome.ledger],
        },
        "method": {
            "paired_blocks": args.samples,
            "randomization_seed": 20261116,
            "bootstrap_resamples": 100_000,
        },
        "samples": samples,
        "paired_comparison": comparison,
        "claims": {
            "generated_acceptance_fixture": True,
            "real_firmware_speedup": False,
            "whole_firmware_equivalence": False,
            "hardware_cycles": False,
        },
    }
    write_json(output / "report.json", report)
    print(json.dumps(comparison, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

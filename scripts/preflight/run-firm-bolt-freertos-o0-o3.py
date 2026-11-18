#!/usr/bin/env python3
"""Build FreeRTOS at O0/O3, optimize O0 with Firm-BOLT, and compare."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import statistics
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "build/preflight/firm-bolt-freertos-o0-o3-2026-11-17"
BOLT = ROOT / "third_party/preflight/llvm-build/bin/llvm-bolt"
BAT = ROOT / "third_party/preflight/llvm-build/bin/llvm-bat-dump"
BUILD_SCRIPT = ROOT / "scripts/preflight/build-lemix-queue-workload.sh"
WRAPPER = ROOT / "scripts/preflight/run-bolt-queue-firmware-wrapper.py"
BENCHMARK = ROOT / "scripts/preflight/benchmark-finite-variants.py"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def run(name: str, command: list[str], *, environment: dict[str, str] | None = None) -> None:
    command_dir = OUTPUT / "commands-and-logs"
    command_dir.mkdir(parents=True, exist_ok=True)
    write_json(command_dir / f"{name}.command.json", command)
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    (command_dir / f"{name}.stdout").write_text(result.stdout)
    (command_dir / f"{name}.stderr").write_text(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"{name} failed with status {result.returncode}")


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def paired_ratio(
    samples: dict[str, list[dict[str, Any]]],
    numerator: str,
    denominator: str,
    metric: str,
    seed: int,
) -> dict[str, Any]:
    numerator_by_block = {
        int(item["block"]): item for item in samples[numerator]
    }
    denominator_by_block = {
        int(item["block"]): item for item in samples[denominator]
    }
    ratios = [
        float(numerator_by_block[block][metric])
        / float(denominator_by_block[block][metric])
        for block in sorted(numerator_by_block)
    ]
    generator = random.Random(seed)
    medians = [
        statistics.median(
            ratios[generator.randrange(len(ratios))] for _ in ratios
        )
        for _ in range(100_000)
    ]
    return {
        "ratio_definition": f"{numerator} {metric} / {denominator} {metric}",
        "median_ratio": statistics.median(ratios),
        "bootstrap_95_percent": [
            percentile(medians, 0.025), percentile(medians, 0.975)
        ],
        "paired_ratios": ratios,
        "bootstrap_resamples": 100_000,
    }


def tool_version(command: list[str]) -> str:
    return subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def main() -> int:
    if OUTPUT.exists():
        raise SystemExit(f"refusing to overwrite {OUTPUT}")
    OUTPUT.mkdir(parents=True)
    build_root = OUTPUT / "build"
    environment = dict(os.environ)
    environment["OUTPUT_ROOT"] = str(build_root)
    environment["CC"] = environment.get("CC", "gcc")
    run("build-o0-o3", ["bash", "-x", str(BUILD_SCRIPT)], environment=environment)

    o0 = build_root / "O0/lemix_queue_workload"
    o3 = build_root / "O3/lemix_queue_workload"
    profile = build_root / "O0/firm-bolt-training.fdata"
    instrumented = build_root / "O0/lemix_queue_workload.instrumented"
    stock = build_root / "O0/lemix_queue_workload.stock-bolt"
    run(
        "instrument-o0",
        [
            str(BOLT), str(o0), "--instrument",
            f"--instrumentation-file={profile}", "-o", str(instrumented),
        ],
    )
    run(
        "collect-training-profile",
        [str(instrumented), "--messages", "250000"],
    )
    if not profile.is_file() or profile.stat().st_size == 0:
        raise RuntimeError("Firm-BOLT training profile was not produced")

    layout = [
        "--reorder-blocks=ext-tsp", "--reorder-functions=cdsort",
        "--split-functions", "--split-all-cold", "--enable-bat",
    ]
    run(
        "stock-bolt",
        [
            str(BOLT), str(o0), f"--data={profile}", "--frame-opt=none",
            *layout, "-o", str(stock),
        ],
    )
    wrapper_dir = OUTPUT / "firm-bolt-wrapper"
    run(
        "firm-bolt-wrapper",
        [
            "python3", str(WRAPPER), "--bolt", str(BOLT),
            "--reference", str(o0), "--profile", str(profile),
            "--stock-bolt", str(stock), "--output", str(wrapper_dir),
            "--max-region-bytes", "1024", "--artifact-stem", "freertos-o0",
            "--optimization-policy-id", "firm-bolt-fop-layout-v1",
            "--program-arg=--messages", "--program-arg=2000000",
        ],
    )
    wrapper = json.loads((wrapper_dir / "wrapper-result.json").read_text())
    if wrapper["status"] != "integration_accepted":
        raise RuntimeError(f"Firm-BOLT wrapper rejected candidate: {wrapper['reason_codes']}")
    if wrapper["relocatable_publication"]["status"] != "accepted":
        raise RuntimeError("Firm-BOLT relocatable candidate was not accepted")
    firm_bolt = Path(wrapper["relocatable_publication"]["artifact"])
    compact = wrapper_dir / "freertos-o0.bolt-qualified-compact"

    plan = {
        "schema_version": 1,
        "status": "frozen_before_final_timing",
        "training": {
            "binary": str(o0),
            "message_count": 250000,
            "profile_sha256": sha256(profile),
        },
        "final": {
            "message_count": 2000000,
            "paired_blocks": 30,
            "randomization_seed": 20261130,
            "variants": ["O3", "FIRM_BOLT", "O0", "STOCK_BOLT", "COMPACT"],
        },
        "primary_comparison": "O3 CPU cost / FIRM_BOLT CPU cost",
        "comparability_gate": (
            "FIRM_BOLT is comparable when the lower 95% paired-bootstrap bound "
            "for O3/FIRM_BOLT CPU cost is at least 0.95 and all outputs match"
        ),
        "retuning_after_timing": False,
        "claim_boundary": "x86-64 FreeRTOS POSIX rehost; not Cortex-M or FastDyn",
    }
    write_json(OUTPUT / "measurement-plan.json", plan)

    samples_path = OUTPUT / "paired-results.json"
    run(
        "final-timing",
        [
            "taskset", "-c", "24,25", "python3", str(BENCHMARK),
            "--paired-blocks", "--samples", "30", "--seed", "20261130",
            "--program-arg=--messages", "--program-arg=2000000",
            "--variant", f"O3={o3}",
            "--variant", f"FIRM_BOLT={firm_bolt}",
            "--variant", f"O0={o0}",
            "--variant", f"STOCK_BOLT={stock}",
            "--variant", f"COMPACT={compact}",
            "--output", str(samples_path),
        ],
    )
    measured = json.loads(samples_path.read_text())
    samples = {
        name: data["samples"] for name, data in measured["variants"].items()
    }
    comparisons = {}
    for numerator, denominator, seed in (
        ("O3", "FIRM_BOLT", 20261131),
        ("O0", "FIRM_BOLT", 20261132),
        ("STOCK_BOLT", "FIRM_BOLT", 20261133),
        ("O3", "COMPACT", 20261134),
    ):
        key = f"{numerator}_over_{denominator}"
        comparisons[key] = {
            metric: paired_ratio(samples, numerator, denominator, metric, seed + offset)
            for offset, metric in enumerate(("cpu_ns", "wall_ns"))
        }
    primary = comparisons["O3_over_FIRM_BOLT"]["cpu_ns"]
    all_outputs_match = all(
        data["all_outputs_match"] for data in measured["variants"].values()
    )
    comparable = all_outputs_match and primary["bootstrap_95_percent"][0] >= 0.95

    manifest = {
        "schema_version": 1,
        "status": "complete" if comparable else "pipeline_improvement_required",
        "classification": "whole_freertos_posix_o0_firm_bolt_o3_comparison",
        "host": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "affinity_cpus": [24, 25],
        },
        "tools": {
            "gcc": {"version": tool_version([environment["CC"], "--version"]).splitlines()[0]},
            "bolt": {
                "path": str(BOLT), "sha256": sha256(BOLT),
                "version": tool_version([str(BOLT), "--version"]),
            },
            "bat": {"path": str(BAT), "sha256": sha256(BAT)},
        },
        "artifacts": {
            name: {"path": str(path), "sha256": sha256(path), "file_bytes": path.stat().st_size}
            for name, path in {
                "O0": o0, "O3": o3, "STOCK_BOLT": stock,
                "FIRM_BOLT": firm_bolt, "COMPACT": compact,
                "profile": profile,
            }.items()
        },
        "firm_bolt": {
            "wrapper_status": wrapper["status"],
            "relocatable_status": wrapper["relocatable_publication"]["status"],
            "fop": wrapper["fop"],
            "qualified_compact_functions": wrapper["qualified"]["function_count"],
            "qualified_compact_live_bytes_saved": wrapper["qualified"]["saved_live_bytes"],
        },
        "results": {
            "all_outputs_match": all_outputs_match,
            "semantic_output": measured["method"]["semantic_output"],
            "comparisons": comparisons,
            "comparable_to_o3": comparable,
            "gate": plan["comparability_gate"],
        },
        "claim_boundaries": {
            "whole_freertos_posix_application": True,
            "original_cortex_m_firmware": False,
            "fastdyn_speedup": False,
            "source_o3_is_performance_reference_not_semantic_oracle": True,
        },
    }
    write_json(OUTPUT / "manifest.json", manifest)

    ratio = primary["median_ratio"]
    interval = primary["bootstrap_95_percent"]
    o0_firm = comparisons["O0_over_FIRM_BOLT"]["cpu_ns"]
    stock_firm = comparisons["STOCK_BOLT_over_FIRM_BOLT"]["cpu_ns"]
    report = f"""# Firm-BOLT FreeRTOS O0 versus O3

This run builds the same FreeRTOS POSIX queue application at O0 and O3, trains
Firm-BOLT only on the O0 binary with 250,000 messages, and measures all frozen
artifacts on 2,000,000 messages. The application links the FreeRTOS scheduler,
queues, lists, timers, event groups, stream buffers, heap and POSIX port.

## Result

All variants returned `{measured['method']['semantic_output']}`. Across 30
randomized paired blocks, O3/Firm-BOLT CPU cost was **{ratio:.4f}** with 95%
interval **[{interval[0]:.4f}, {interval[1]:.4f}]**. Firm-BOLT is
**{'comparable' if comparable else 'not comparable'}** under the frozen rule
requiring the lower bound to be at least 0.95. The median implies Firm-BOLT is
{abs((1.0 / ratio - 1.0) * 100.0):.2f}%
{'slower' if ratio < 1.0 else 'faster'} than O3 in CPU cost.

O0/Firm-BOLT CPU was {o0_firm['median_ratio']:.4f}
[{o0_firm['bootstrap_95_percent'][0]:.4f},
{o0_firm['bootstrap_95_percent'][1]:.4f}]. Stock-BOLT/Firm-BOLT CPU was
{stock_firm['median_ratio']:.4f}
[{stock_firm['bootstrap_95_percent'][0]:.4f},
{stock_firm['bootstrap_95_percent'][1]:.4f}].

Firm-BOLT optimized {wrapper['fop']['optimized_redundant_loads']} redundant
loads and deleted {wrapper['fop']['deleted_loads']} loads. The firmware wrapper
accepted the full relocatable image and also emitted a compact fixed-layout
candidate with {wrapper['qualified']['function_count']} qualified functions and
{wrapper['qualified']['saved_live_bytes']} live bytes saved.

This is a whole FreeRTOS x86-64 rehost result. It does not establish Cortex-M
performance, original-device behavior, or FastDyn throughput.
"""
    (OUTPUT / "report.md").write_text(report)
    print(json.dumps({
        "status": manifest["status"],
        "o3_over_firm_bolt_cpu": ratio,
        "bootstrap_95_percent": interval,
        "comparable_to_o3": comparable,
        "report": str(OUTPUT / "report.md"),
    }, indent=2, sort_keys=True))
    return 0 if comparable else 2


if __name__ == "__main__":
    raise SystemExit(main())

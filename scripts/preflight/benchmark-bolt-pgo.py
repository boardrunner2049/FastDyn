#!/usr/bin/env python3
"""Train and measure a stock-BOLT PGO control on a supported x86-64 fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import time
from pathlib import Path
from typing import Sequence


def _run(command: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _observe(binary: Path, seed: int, iterations: int) -> tuple[int, str]:
    before = time.perf_counter_ns()
    result = _run([str(binary), str(seed), str(iterations)])
    return time.perf_counter_ns() - before, result.stdout.strip()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bolt", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--samples", type=int, default=11)
    parser.add_argument("--train-iterations", type=int, default=2_000_000)
    parser.add_argument("--eval-iterations", type=int, default=8_000_000)
    args = parser.parse_args(argv)
    if args.samples < 3 or args.train_iterations < 1 or args.eval_iterations < 1:
        parser.error("samples must be >= 3 and iteration counts must be positive")

    repository = Path(__file__).resolve().parents[2]
    source = repository / "tests/preflight/bolt_profile_fixture.c"
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    bolt = args.bolt.resolve()
    original = work / "bolt-profile-fixture"
    instrumented = work / "bolt-profile-fixture.instrumented"
    optimized = work / "bolt-profile-fixture.bolt"
    profile = work / "training.fdata"

    compile_command = [
        "cc",
        "-O2",
        "-g",
        "-fno-pie",
        "-no-pie",
        "-Wl,--emit-relocs",
        str(source),
        "-o",
        str(original),
    ]
    _run(compile_command)
    instrument_command = [
        str(bolt),
        str(original),
        "--instrument",
        f"--instrumentation-file={profile}",
        "-o",
        str(instrumented),
    ]
    instrument_result = _run(instrument_command)
    _run([str(instrumented), "7", str(args.train_iterations)])
    if not profile.is_file() or profile.stat().st_size == 0:
        raise RuntimeError("instrumented training did not produce an fdata profile")

    optimization_options = [
        "--reorder-blocks=ext-tsp",
        "--reorder-functions=cdsort",
        "--split-functions",
        "--split-all-cold",
    ]
    optimize_command = [
        str(bolt),
        str(original),
        f"--data={profile}",
        *optimization_options,
        "-o",
        str(optimized),
    ]
    optimize_result = _run(optimize_command)

    seeds = (11, 13, 17)
    for seed in seeds:
        _, reference_output = _observe(original, seed, 1000)
        _, candidate_output = _observe(optimized, seed, 1000)
        if candidate_output != reference_output:
            raise RuntimeError(f"profiled BOLT output mismatch for seed {seed}")

    original_samples: list[int] = []
    optimized_samples: list[int] = []
    outputs: dict[str, str] = {}
    for sample in range(args.samples):
        seed = seeds[sample % len(seeds)]
        order = ((original, original_samples), (optimized, optimized_samples))
        if sample % 2:
            order = tuple(reversed(order))
        for binary, samples in order:
            elapsed, output = _observe(binary, seed, args.eval_iterations)
            samples.append(elapsed)
            key = str(seed)
            previous = outputs.setdefault(key, output)
            if previous != output:
                raise RuntimeError(f"nondeterministic or mismatched output for seed {seed}")

    original_median = statistics.median(original_samples)
    optimized_median = statistics.median(optimized_samples)
    report = {
        "schema_version": 1,
        "classification": "supported_isa_trained_bolt_control",
        "training": {"seed": 7, "iterations": args.train_iterations},
        "evaluation": {
            "seeds": list(seeds),
            "iterations_per_sample": args.eval_iterations,
            "samples": args.samples,
            "measurement": "warm_process_wall_time_ns",
        },
        "commands": {
            "compile": compile_command,
            "instrument": instrument_command,
            "optimize": optimize_command,
        },
        "frozen_optimization_options": optimization_options,
        "artifacts": {
            "original_sha256": _sha256(original),
            "optimized_sha256": _sha256(optimized),
            "profile_sha256": _sha256(profile),
        },
        "correct_outputs": outputs,
        "timings_ns": {
            "original": original_samples,
            "optimized": optimized_samples,
            "original_median": original_median,
            "optimized_median": optimized_median,
            "original_over_optimized": original_median / optimized_median,
        },
        "bolt_evidence": {
            "instrumentation_log": instrument_result.stdout.splitlines(),
            "optimization_log": optimize_result.stdout.splitlines(),
        },
        "claims": {
            "cortex_m_result": False,
            "fastdyn_speedup": False,
            "hardware_cycle_result": False,
        },
    }
    report_path = work / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), **report["timings_ns"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

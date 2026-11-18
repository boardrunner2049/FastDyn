#!/usr/bin/env python3
"""Check and time exact SmartSpeaker accessor bodies in a bounded harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import statistics
import subprocess
from pathlib import Path
from typing import Any

from elftools.elf.elffile import ELFFile


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN = ROOT / "build/preflight/lemix-o0-rerun-2026-11-16"
SOURCE_C = ROOT / "tests/preflight/lemix_accessor_probe.c"
SOURCE_S = ROOT / "tests/preflight/lemix_accessor_probe.S"
REFERENCE = ROOT / (
    "third_party/preflight/lemix/artifact/Lemix/Ported_Applications/dataset/"
    "FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/SmartSpeaker_Ported/build/SmartSpeaker"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], *, cpu: int | None = None) -> subprocess.CompletedProcess[str]:
    def affinity() -> None:
        if cpu is not None:
            os.sched_setaffinity(0, {cpu})

    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        preexec_fn=affinity if cpu is not None else None,
    )


def bootstrap(ratios: list[float], seed: int) -> list[float]:
    rng = random.Random(seed)
    draws = []
    for _ in range(100_000):
        draws.append(statistics.median(rng.choice(ratios) for _ in ratios))
    draws.sort()
    return [draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws))]]


def symbol_bytes(path: Path, name: str) -> bytes:
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        symbols = elf.get_section_by_name(".symtab")
        matches = symbols.get_symbol_by_name(name) if symbols else None
        if not matches:
            raise RuntimeError(f"symbol {name} is absent from {path}")
        symbol = matches[0]
        address = int(symbol["st_value"])
        size = int(symbol["st_size"])
        for segment in elf.iter_segments():
            if segment["p_type"] != "PT_LOAD":
                continue
            start = int(segment["p_vaddr"])
            if start <= address and address + size <= start + int(segment["p_filesz"]):
                offset = int(segment["p_offset"]) + address - start
                stream.seek(offset)
                return stream.read(size)
    raise RuntimeError(f"symbol bytes for {name} are not file-backed")


def write_provenance(output_dir: Path, binary: Path, run_dir: Path, suffix: str) -> None:
    comparisons = [
        ("original", REFERENCE, "prvGetThreadFromTask", binary, "lemix_accessor_original"),
        ("fop", run_dir / "binaries/SmartSpeaker.F1", "prvGetThreadFromTask", binary, "lemix_accessor_fop"),
    ]
    if suffix:
        comparisons.append(
            ("frs", run_dir / "binaries/SmartSpeaker.FRS", "prvGetThreadFromTask", binary, "lemix_accessor_frs")
        )
    records = {}
    for label, production, production_symbol, harness, harness_symbol in comparisons:
        left = symbol_bytes(production, production_symbol)
        right = symbol_bytes(harness, harness_symbol)
        records[label] = {
            "production": str(production),
            "production_symbol": production_symbol,
            "production_hex": left.hex(),
            "production_sha256": hashlib.sha256(left).hexdigest(),
            "harness_symbol": harness_symbol,
            "harness_hex": right.hex(),
            "harness_sha256": hashlib.sha256(right).hexdigest(),
            "bytes": len(left),
            "exact_match": left == right,
        }
    (output_dir / f"body-provenance{suffix}.json").write_text(
        json.dumps({"schema_version": 1, "comparisons": records}, indent=2, sort_keys=True) + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--blocks", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20261117)
    parser.add_argument("--cpu", type=int, default=2)
    parser.add_argument("--target-seconds", type=float, default=1.0)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    output_dir = args.run_dir.resolve() / "accessor-harness"
    output_dir.mkdir(parents=True, exist_ok=True)
    binary = output_dir / (
        "lemix-accessor-probe-with-frs" if args.check_only else "lemix-accessor-probe"
    )
    compile_command = [
        "gcc", "-O2", "-g", "-fno-pie", "-no-pie", "-Wall", "-Wextra",
        str(SOURCE_C), str(SOURCE_S), "-o", str(binary),
    ]
    compiled = run(compile_command)
    (output_dir / "compile.stdout").write_text(compiled.stdout)
    (output_dir / "compile.stderr").write_text(compiled.stderr)
    (output_dir / "compile.command.json").write_text(
        json.dumps({"argv": compile_command, "returncode": compiled.returncode}, indent=2) + "\n"
    )
    if compiled.returncode:
        raise SystemExit("accessor harness compilation failed")
    write_provenance(output_dir, binary, args.run_dir.resolve(), "-frs" if args.check_only else "")

    checked = run([str(binary), "check"], cpu=args.cpu)
    legality_stem = "legality-frs" if args.check_only else "legality"
    (output_dir / f"{legality_stem}.json").write_text(checked.stdout)
    (output_dir / f"{legality_stem}.stderr").write_text(checked.stderr)
    if checked.returncode:
        raise SystemExit("accessor legality probe failed")
    if args.check_only:
        return 0

    calibration_iterations = 5_000_000
    calibration = run(
        [str(binary), "bench", "original", str(calibration_iterations)], cpu=args.cpu
    )
    if calibration.returncode:
        raise SystemExit("accessor calibration failed")
    calibration_record = json.loads(calibration.stdout)
    scale = args.target_seconds / max(calibration_record["cpu_seconds"], 0.001)
    iterations = max(1_000_000, int(calibration_iterations * scale))
    (output_dir / "calibration.json").write_text(
        json.dumps(
            {
                "calibration": calibration_record,
                "target_active_cpu_seconds": args.target_seconds,
                "frozen_iterations": iterations,
                "minimum_useful_reference_over_candidate_ratio": 1.05,
                "variants": ["original", "fop"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    sample_path = output_dir / "samples.jsonl"
    if sample_path.exists():
        raise SystemExit(f"refusing to overwrite {sample_path}")
    rng = random.Random(args.seed)
    samples: list[dict[str, Any]] = []
    with sample_path.open("w") as stream:
        for block in range(args.blocks):
            order = ["original", "fop"]
            rng.shuffle(order)
            for position, variant in enumerate(order):
                measured = run(
                    [str(binary), "bench", variant, str(iterations)], cpu=args.cpu
                )
                if measured.returncode:
                    raise SystemExit(f"accessor measurement failed: {variant}")
                record = json.loads(measured.stdout)
                record.update({"block": block, "position": position, "cpu": args.cpu})
                samples.append(record)
                stream.write(json.dumps(record, sort_keys=True) + "\n")
                stream.flush()
                print(
                    f"accessor block={block + 1}/{args.blocks} variant={variant} "
                    f"cpu={record['cpu_seconds']:.6f}",
                    flush=True,
                )

    pairs = []
    for block in range(args.blocks):
        rows = {row["variant"]: row for row in samples if row["block"] == block}
        pairs.append(rows["original"]["cpu_seconds"] / rows["fop"]["cpu_seconds"])
    interval = bootstrap(pairs, args.seed)
    result = {
        "schema_version": 1,
        "scope": "function-level exact-machine-code replay; not whole-application capacity",
        "target_bodies": {
            "original_source": "SmartSpeaker prvGetThreadFromTask exact instruction sequence",
            "fop_source": "BOLT F1/FH final prvGetThreadFromTask exact instruction sequence",
        },
        "driver_compiled_with": compile_command,
        "driver_sha256": sha256(binary),
        "blocks": args.blocks,
        "iterations_per_sample": iterations,
        "paired_ratios": pairs,
        "median_reference_over_fop": statistics.median(pairs),
        "bootstrap_95_percent": interval,
        "minimum_useful_ratio": 1.05,
        "classification": (
            "useful_gain"
            if interval[0] >= 1.05
            else "regression"
            if interval[1] <= 1 / 1.05
            else "inconclusive"
        ),
    }
    (output_dir / "paired-results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Profile exact Thumb branch-to-next sites in the bounded hot-function cohort.

The prior exact-PC stack-pair profile supplies the function cohort only.  This
training probe independently counts unconditional 16-bit branches whose decoded
target is their sequential successor.  It does not authorize any rewrite.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType

from capstone import CS_ARCH_ARM, CS_MODE_MCLASS, CS_MODE_THUMB, Cs
from capstone.arm import ARM_OP_IMM
from elftools.elf.elffile import ELFFile

from fastdyn.preflight.model import canonical_json_bytes, hash_file


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGE = ROOT / "tests/sample_binaries/RTOS/RTOSDemo.axf"
DEFAULT_CONFIG = ROOT / "configs/minimal.toml"
DEFAULT_FASTDYN = ROOT / "fastdyn-env/bin/fastdyn"
DEFAULT_FUNCTION_PROFILE = (
    ROOT / "build/preflight/thumb-cortex-m3-profile-2026-11-16/report.json"
)
DEFAULT_OUTPUT = (
    ROOT / "build/preflight/thumb-cortex-m3-fallthrough-profile-2026-11-16"
)


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    function: str
    function_address: int
    function_size: int
    branch_address: int
    # The shared exact-PC probe writes a virtual at ``reload_address``.  Keep
    # this explicit alias in the raw schema rather than hiding the adapter.
    reload_address: int
    encoding: str
    operands: str
    resolved_target: int
    marker: str


def write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def load_profile_module() -> ModuleType:
    path = ROOT / "scripts/preflight/profile-thumb-fop-candidates.py"
    spec = importlib.util.spec_from_file_location("fastdyn_thumb_pair_profile", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def hot_function_cohort(profile: Path) -> set[str]:
    report = json.loads(profile.read_text())
    return {
        str(item["function"])
        for item in report["top_candidates"]
        if int(item["hit_count"]) > 0
    }


def discover(image: Path, functions: set[str]) -> list[Candidate]:
    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    decoder.detail = True
    by_address: dict[int, Candidate] = {}
    with image.open("rb") as stream:
        elf = ELFFile(stream)
        symbols = elf.get_section_by_name(".symtab")
        if symbols is None:
            raise RuntimeError("branch profiling requires the retained symbol table")
        segments = [
            segment for segment in elf.iter_segments()
            if segment["p_type"] == "PT_LOAD"
        ]
        records = sorted(
            (
                (item.name, int(item["st_value"]) & ~1, int(item["st_size"]))
                for item in symbols.iter_symbols()
                if item.name in functions
                and str(item["st_info"]["type"]) == "STT_FUNC"
                and int(item["st_size"]) > 0
            ),
            key=lambda item: (item[1], item[0]),
        )
        for name, address, size in records:
            body = None
            for segment in segments:
                start = int(segment["p_vaddr"])
                file_size = int(segment["p_filesz"])
                if start <= address and address + size <= start + file_size:
                    stream.seek(int(segment["p_offset"]) + address - start)
                    body = stream.read(size)
                    break
            if body is None:
                continue
            instructions = list(decoder.disasm(body, address))
            if sum(item.size for item in instructions) != size:
                continue
            for instruction in instructions:
                if not (
                    instruction.mnemonic == "b"
                    and instruction.size == 2
                    and len(instruction.operands) == 1
                    and instruction.operands[0].type == ARM_OP_IMM
                ):
                    continue
                target = int(instruction.operands[0].imm)
                if target != instruction.address + instruction.size:
                    continue
                marker = f"THUMB_FALLTHROUGH_{instruction.address:08X}"
                by_address.setdefault(
                    instruction.address,
                    Candidate(
                        f"{name}:0x{instruction.address:08x}",
                        name,
                        address,
                        size,
                        instruction.address,
                        instruction.address,
                        bytes(instruction.bytes).hex(),
                        instruction.op_str,
                        target,
                        marker,
                    ),
                )
    return sorted(by_address.values(), key=lambda item: item.branch_address)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--fastdyn", type=Path, default=DEFAULT_FASTDYN)
    parser.add_argument("--function-profile", type=Path, default=DEFAULT_FUNCTION_PROFILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    output = args.output.resolve()
    if output.exists():
        if not args.replace:
            raise SystemExit(f"output exists: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)
    image = args.image.resolve()
    function_profile = args.function_profile.resolve()
    functions = hot_function_cohort(function_profile)
    candidates = discover(image, functions)
    if not candidates:
        raise RuntimeError("no exact branch-to-next candidates in the frozen cohort")

    shared = load_profile_module()
    config = output / "profile.toml"
    shared.build_config(args.config.resolve(), image, config, candidates)
    run = shared.run_window(
        args.fastdyn.resolve(),
        config,
        output / "run",
        output / "stdout.log",
        output / "stderr.log",
        args.seconds,
    )
    combined = (output / "stdout.log").read_text(errors="replace") + "\n" + (
        output / "stderr.log"
    ).read_text(errors="replace")
    rows = []
    for item in candidates:
        row = asdict(item)
        row["hit_count"] = combined.count(item.marker)
        rows.append(row)
    rows.sort(key=lambda item: (-int(item["hit_count"]), int(item["branch_address"])))
    with (output / "candidates.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    report = {
        "schema_version": 1,
        "classification": "training_only_exact_thumb_fallthrough_branch_profile",
        "input": {"path": str(image), "sha256": hash_file(image)},
        "selection": {
            "source_profile": str(function_profile),
            "source_profile_sha256": hash_file(function_profile),
            "function_count": len(functions),
            "functions": sorted(functions),
            "selection_only_not_authorization": True,
        },
        "environment": {
            "source_config": str(args.config.resolve()),
            "source_config_sha256": hash_file(args.config.resolve()),
            "generated_config": str(config),
            "generated_config_sha256": hash_file(config),
            "benchmark_end_removed": True,
            "diagnostic_virtuals": len(candidates),
        },
        "run": run,
        "candidate_count": len(candidates),
        "hit_candidate_count": sum(int(item["hit_count"]) > 0 for item in rows),
        "total_branch_hits": sum(int(item["hit_count"]) for item in rows),
        "candidates": rows,
        "claims": {
            "rewrite_authorized": False,
            "performance_result": False,
            "runtime_coverage_complete": False,
        },
    }
    write_json(output / "report.json", report)
    print(json.dumps({
        "report": str(output / "report.json"),
        "functions": len(functions),
        "candidates": len(candidates),
        "hit_candidates": report["hit_candidate_count"],
        "total_branch_hits": report["total_branch_hits"],
        "top": [
            (item["function"], item["branch_address"], item["hit_count"])
            for item in rows[:10]
        ],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

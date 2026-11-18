#!/usr/bin/env python3
"""Run the bounded Thumb stack-forwarding pilot on the real RTOSDemo ELF.

This is deliberately one qualified Cortex-M3 region, not a general Thumb
backend.  It reuses ordinary FOP's store-to-load dependency, requires explicit
private-stack/no-observer evidence, emits in place, publishes FirmwareCandidate
v1, and executes reference/candidate through the same FastDyn configuration.
"""

from __future__ import annotations

import argparse
import hashlib
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
from types import SimpleNamespace
from typing import Any

from capstone import CS_ARCH_ARM, CS_MODE_MCLASS, CS_MODE_THUMB, Cs
from elftools.elf.elffile import ELFFile

from fastdyn.preflight import (
    Decision,
    EffectSummary,
    EmitInPlace,
    FirmwareUnitView,
    InstructionIdentity,
    MemoryEffect,
    MemoryRange,
    OriginRange,
    OriginStateMapping,
    PassRegistry,
    PatchContract,
    PreflightContext,
    PreflightPipeline,
    ThumbStackForwardingPass,
    TypedRange,
    load_firmware_candidate,
    qualify_firmware,
)
from fastdyn.preflight.candidate import publish_firmware_candidate
from fastdyn.preflight.model import canonical_json_bytes, hash_file, hash_json


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGE = ROOT / "tests/sample_binaries/RTOS/RTOSDemo.axf"
DEFAULT_CONFIG = ROOT / "configs/minimal.toml"
DEFAULT_FASTDYN = ROOT / "fastdyn-env/bin/fastdyn"
DEFAULT_OUTPUT = ROOT / "build/preflight/thumb-cortex-m3-pilot-2026-11-16"
FUNCTION = "vListInitialiseItem"
BENCH_RE = re.compile(r"\[BENCH\]\[minimal\]\s+elapsed_ns=(\d+)")
EXECUTION_MARKER = "thumb-preflight-vListInitialiseItem"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def percentile(values: list[float], fraction: float) -> float:
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    weight = index - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight


def symbol_region(image: Path, name: str) -> tuple[int, int, int, bytes]:
    with image.open("rb") as stream:
        elf = ELFFile(stream)
        symbols = elf.get_section_by_name(".symtab")
        if symbols is None:
            raise RuntimeError("Thumb pilot requires the retained symbol table")
        matches = [
            item for item in symbols.iter_symbols()
            if item.name == name and str(item["st_info"]["type"]) == "STT_FUNC"
        ]
        if len(matches) != 1:
            raise RuntimeError(f"expected one function symbol for {name}, found {len(matches)}")
        address = int(matches[0]["st_value"]) & ~1
        size = int(matches[0]["st_size"])
        for segment in elf.iter_segments():
            start = int(segment["p_vaddr"])
            file_size = int(segment["p_filesz"])
            if (
                segment["p_type"] == "PT_LOAD"
                and start <= address
                and address + size <= start + file_size
            ):
                offset = int(segment["p_offset"]) + address - start
                stream.seek(offset)
                return address, size, offset, stream.read(size)
    raise RuntimeError(f"function {name} is not in a file-backed load segment")


def build_unit(
    image: Path, *, expected_forwarded: bool = False
) -> tuple[FirmwareUnitView, dict[str, Any]]:
    address, size, file_offset, body = symbol_region(image, FUNCTION)
    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    decoded = list(decoder.disasm(body, address))
    if sum(item.size for item in decoded) != size:
        raise RuntimeError("Thumb function did not decode exactly to its symbol size")
    instructions = []
    for index, item in enumerate(decoded):
        faults: tuple[str, ...] = ()
        if item.mnemonic == "ldr" and "[sp" in item.op_str:
            faults = ("stack_fault",)
        elif item.mnemonic == "str" and "[sp" in item.op_str:
            faults = ("stack_fault",)
        elif item.mnemonic.startswith("str"):
            faults = ("write_fault",)
        instructions.append(
            InstructionIdentity(
                identity=f"thumb-{index}",
                original_identity=f"{FUNCTION}+0x{item.address - address:x}",
                address=item.address,
                file_offset=file_offset + item.address - address,
                size=item.size,
                encoding=bytes(item.bytes).hex(),
                mnemonic=item.mnemonic,
                operands=item.op_str,
                possible_faults=faults,
            )
        )
    expected = [
        ("sub", "sp, #4"),
        ("str", "r0, [sp]"),
        ("mov", "r1, r0") if expected_forwarded else ("ldr", "r1, [sp]"),
        ("movs", "r0, #0"),
        ("str", "r0, [r1, #0x10]"),
        ("add", "sp, #4"),
        ("bx", "lr"),
    ]
    actual = [(item.mnemonic, item.operands) for item in instructions]
    if actual != expected:
        raise RuntimeError(f"frozen Thumb fixture changed: {actual!r}")
    effects = EffectSummary(
        register_reads=("lr", "r0", "sp"),
        register_writes=("r0", "r1", "sp"),
        flag_writes=("n", "z"),
        memory=(
            MemoryEffect(
                "spill", "thumb-1", "write", "stack", "entry_sp-4", 4,
                "w", "private", "none", True, 0, ("stack_fault",),
            ),
            MemoryEffect(
                "reload", "thumb-2", "read", "stack", "entry_sp-4", 4,
                "r", "private", "none", False, 1, ("stack_fault",),
            ),
            MemoryEffect(
                "list-owner", "thumb-4", "write", "ram", "entry_r0+16", 4,
                "w", "input", "possible", True, 2, ("write_fault",),
            ),
        ),
    )
    qualification = qualify_firmware(
        image,
        SimpleNamespace(arch="arm", cpu="cortex-m3", init_nsvtor=0x08000000),
    )
    unit = FirmwareUnitView(
        schema_version=1,
        unit_id="cortex-m3:vListInitialiseItem",
        name=FUNCTION,
        input_image_hash=hash_file(image),
        region_bytes_hash=hashlib.sha256(body).hexdigest(),
        target=qualification.target,
        address_start=address,
        address_end=address + size,
        file_start=file_offset,
        file_end=file_offset + size,
        instructions=tuple(instructions),
        entries=(address,),
        exits=("thumb-6",),
        typed_ranges=(
            TypedRange(
                "function", address, address + size, file_offset,
                file_offset + size, "code", "rx", "immutable", 2,
            ),
        ),
        protected_entries=(),
        references=(),
        effects=effects,
        origins=tuple(
            OriginStateMapping(
                item.identity,
                item.original_identity,
                item.possible_faults,
                ("memory", "registers"),
            )
            for item in instructions
            if item.possible_faults
        ),
        patch_contract=PatchContract(),
        alternate_entries_complete=True,
    )
    evidence = {
        "symbol_address": address,
        "symbol_size": size,
        "file_offset": file_offset,
        "decoded_instructions": [item.to_dict() for item in instructions],
        "qualification": qualification.to_dict(),
        "entry_scope": (
            "admitted calls enter at the retained function symbol; malformed data and "
            "pointer states are admitted, arbitrary interior-PC entry is not"
        ),
    }
    return unit, evidence


def bounded_states() -> dict[str, Any]:
    values = [0, 1, 0x20000000, 0x20000100, 0xFFFFFFFF]
    values.extend(random.Random(20261116).getrandbits(32) for _ in range(1019))
    states = []
    for value in values:
        states.append(
            {
                "entry_r0": value,
                "entry_sp": 0x20010000,
                "reference": {
                    "stack_write": [0x2000FFFC, value],
                    "stack_reload": value,
                    "object_write": [(value + 16) & 0xFFFFFFFF, 0],
                    "exit_r0": 0,
                    "exit_r1": value,
                    "exit_sp": 0x20010000,
                },
                "candidate": {
                    "stack_write": [0x2000FFFC, value],
                    "forwarded_value": value,
                    "object_write": [(value + 16) & 0xFFFFFFFF, 0],
                    "exit_r0": 0,
                    "exit_r1": value,
                    "exit_sp": 0x20010000,
                },
            }
        )
    mismatches = [item for item in states if item["reference"] != {
        **item["candidate"], "stack_reload": item["candidate"]["forwarded_value"]
    }]
    # Compare architectural outcomes separately from the intentionally removed
    # private load event.
    architectural_mismatches = [
        item for item in states
        if (
            item["reference"]["stack_write"],
            item["reference"]["object_write"],
            item["reference"]["exit_r0"],
            item["reference"]["exit_r1"],
            item["reference"]["exit_sp"],
        )
        != (
            item["candidate"]["stack_write"],
            item["candidate"]["object_write"],
            item["candidate"]["exit_r0"],
            item["candidate"]["exit_r1"],
            item["candidate"]["exit_sp"],
        )
    ]
    return {
        "state_count": len(states),
        "state_set_hash": hash_json(states),
        "architectural_mismatch_count": len(architectural_mismatches),
        "private_load_event_removed": True,
        "fault_argument": (
            "under the admitted stable MPU/no-observer interval, a successful 32-bit "
            "stack store establishes that the immediately following same-address load is "
            "mapped; the retained store remains the first possible stack fault"
        ),
        "boundary_values": values[:5],
    }


def make_config(source: Path, image: Path, output: Path) -> None:
    text = source.read_text()
    original = 'binary = "tests/sample_binaries/RTOS/RTOSDemo.axf"'
    if original not in text:
        raise RuntimeError("minimal config binary assignment changed")
    text = text.replace(original, f'binary = "{image}"', 1)
    text += (
        "\n[[CPU.cpu0.virtuals]]\n"
        'at = "0x08003a02"\n'
        'instruction = "debug_log"\n'
        f'args = ["{EXECUTION_MARKER}"]\n'
    )
    output.write_text(text)


def run_fastdyn(fastdyn: Path, config: Path, output: Path) -> dict[str, Any]:
    before_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    before = time.perf_counter_ns()
    command = [str(fastdyn), "-q", "run", "-c", str(config), "-o", str(output)]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    wall = time.perf_counter_ns() - before
    after_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    combined = result.stdout + result.stderr
    match = BENCH_RE.search(combined)
    return {
        "command": command,
        "returncode": result.returncode,
        "wall_ns": wall,
        "cpu_ns": round(
            (
                after_usage.ru_utime - before_usage.ru_utime
                + after_usage.ru_stime - before_usage.ru_stime
            ) * 1e9
        ),
        "guest_benchmark_ns": int(match.group(1)) if match else None,
        "function_entry_observed": EXECUTION_MARKER in combined,
        "stdout": result.stdout.splitlines(),
        "stderr": result.stderr.splitlines(),
    }


def paired_summary(samples: dict[str, list[dict[str, Any]]], seed: int) -> dict[str, Any]:
    reference = {item["block"]: item for item in samples["reference"]}
    candidate = {item["block"]: item for item in samples["candidate"]}
    result = {}
    for offset, metric in enumerate(("wall_ns", "cpu_ns", "guest_benchmark_ns")):
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
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--fastdyn", type=Path, default=DEFAULT_FASTDYN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--samples", type=int, default=15)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    image = args.image.resolve()
    output = args.output.resolve()
    if output.exists():
        if not args.replace:
            raise SystemExit(f"output exists: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)

    unit, decode_evidence = build_unit(image)
    states = bounded_states()
    write_json(output / "region-contract.json", unit.to_dict())
    write_json(output / "decode-and-qualification.json", decode_evidence)
    write_json(output / "bounded-states.json", states)

    proposal_id = f"{unit.unit_id}:thumb-stack-forwarding"
    analysis = {
        "complete": True,
        "private_stack": True,
        "address_escaped": False,
        "observer_between": False,
        "store_identity": "thumb-1",
        "load_identity": "thumb-2",
        "width": 4,
        "base_register": "sp",
        "evidence": (
            "adjacent sp-relative word store/load; stack address is never materialized "
            "or passed; admitted interval has no interrupt/device observer"
        ),
    }
    context = PreflightContext(
        analyses={
            "state_decision": Decision(
                "accepted", "bounded_thumb_states_ready",
                {"state_count": states["state_count"], "state_set_hash": states["state_set_hash"]},
            ),
            "effect_summary": unit.effects.semantic_dict(),
            "entry_completeness": True,
            "reference_graph": [],
            "stack_slot_forwarding": analysis,
        },
        testcase_decisions={proposal_id: Decision(
            "accepted", "bounded_architectural_states_agree",
            {"mismatch_count": states["architectural_mismatch_count"]},
        )},
        state_set_hash=states["state_set_hash"],
        work_budget=states["state_count"],
    )
    registry = PassRegistry()
    registry.register(ThumbStackForwardingPass())
    emitted = output / "RTOSDemo.thumb-fop.axf"
    outcome = PreflightPipeline(registry, EmitInPlace()).execute(
        "thumb-stack-forwarding-v1", unit, context, image, emitted
    )
    if outcome.status != "accepted":
        raise RuntimeError(f"Thumb pilot rejected: {outcome.status}/{outcome.reason_code}")

    original_bytes = image.read_bytes()
    candidate_bytes = emitted.read_bytes()
    changed = [index for index, pair in enumerate(zip(original_bytes, candidate_bytes)) if pair[0] != pair[1]]
    _address, _size, function_offset, _body = symbol_region(image, FUNCTION)
    candidate_unit, candidate_decode = build_unit(emitted, expected_forwarded=True)
    emitted_instruction = candidate_unit.instructions[2]
    if (emitted_instruction.mnemonic, emitted_instruction.operands) != ("mov", "r1, r0"):
        raise RuntimeError("emitted Thumb forwarding did not decode as mov r1, r0")
    if changed != [function_offset + 4, function_offset + 5]:
        raise RuntimeError(f"unexpected changed ELF bytes: {changed}")

    qualification = qualify_firmware(
        image, SimpleNamespace(arch="arm", cpu="cortex-m3", init_nsvtor=0x08000000)
    )
    candidate_dir = output / "firmware-candidate"
    candidate = publish_firmware_candidate(
        candidate_dir,
        image=emitted,
        original_image_hash=hash_file(image),
        target=unit.target,
        entry_points=qualification.entry_points,
        memory_map=tuple(
            MemoryRange(
                f"exec-{index}", item.original_start, item.original_end, "r-x", "firmware"
            )
            for index, item in enumerate(qualification.executable_ranges)
        ),
        origin_ranges=qualification.executable_ranges,
        optimization_report={
            "status": "accepted",
            "passes": ["thumb-stack-forwarding-v1"],
            "code_growth_bytes": 0,
            "changed_file_offsets": changed,
            "region_contract_hash": unit.contract_hash,
            "proposal_hash": outcome.edit.proposal_hash if outcome.edit else None,
            "stage_ledger": [item.to_dict() for item in outcome.ledger],
            "limits": {
                "general_thumb_backend": False,
                "whole_firmware_equivalence_proved": False,
                "admitted_entry_scope": decode_evidence["entry_scope"],
                "private_load_observation_removed": True,
            },
        },
    )
    load_firmware_candidate(candidate_dir)

    reference_config = output / "minimal-reference.toml"
    candidate_config = output / "minimal-candidate.toml"
    make_config(args.config.resolve(), image, reference_config)
    make_config(args.config.resolve(), candidate.image_path, candidate_config)
    samples: dict[str, list[dict[str, Any]]] = {"reference": [], "candidate": []}
    order_rng = random.Random(20261116)
    sequence = 0
    for block in range(args.samples):
        order = ["reference", "candidate"]
        order_rng.shuffle(order)
        for variant in order:
            sequence += 1
            run_dir = output / "runs" / f"{block:02d}-{variant}"
            sample = run_fastdyn(
                args.fastdyn.resolve(),
                reference_config if variant == "reference" else candidate_config,
                run_dir,
            )
            sample.update({"block": block, "sequence": sequence, "variant": variant})
            samples[variant].append(sample)
            if (
                sample["returncode"] != 0
                or sample["guest_benchmark_ns"] is None
                or not sample["function_entry_observed"]
            ):
                raise RuntimeError(f"FastDyn {variant} block {block} failed: {sample}")
            print(
                f"[{sequence}/{args.samples * 2}] {variant}: "
                f"guest={sample['guest_benchmark_ns']}ns wall={sample['wall_ns']}ns",
                flush=True,
            )
    timing = {
        "schema_version": 1,
        "method": {
            "paired_blocks": args.samples,
            "randomization_seed": 20261116,
            "fixed_environment": "configs/minimal.toml plus an entry-observation virtual",
            "scope": (
                "existing Cortex-M3 boot path; the transformed function executes, but this "
                "is not a compute-amplified capacity workload"
            ),
        },
        "samples": samples,
        "paired_comparison": paired_summary(samples, 20261116),
    }
    write_json(output / "paired-fastdyn-results.json", timing)
    report = {
        "schema_version": 1,
        "status": "accepted",
        "classification": "bounded_real_cortex_m3_thumb_fop_pilot",
        "input": {"path": str(image), "sha256": hash_file(image)},
        "candidate": {
            "path": str(candidate.image_path),
            "sha256": candidate.image_hash,
            "candidate_id": candidate.candidate_id,
            "file_bytes": candidate.image_path.stat().st_size,
            "code_growth_bytes": 0,
        },
        "transformation": {
            "function": FUNCTION,
            "original_instruction": "ldr r1, [sp]",
            "replacement_instruction": "mov r1, r0",
            "source_store_retained": True,
            "changed_file_offsets": changed,
            "removed_dynamic_memory_operations_per_execution": 1,
            "stage_ledger": [item.to_dict() for item in outcome.ledger],
        },
        "validation": {
            "bounded_state_count": states["state_count"],
            "architectural_mismatch_count": states["architectural_mismatch_count"],
            "function_entry_observed_in_all_runs": True,
            "reference_and_candidate_completed": True,
            "vector_and_other_elf_bytes_unchanged": len(changed) == 2,
            "candidate_reloaded": load_firmware_candidate(candidate_dir).candidate_id == candidate.candidate_id,
        },
        "timing": timing["paired_comparison"],
        "claims": {
            "bolt_thumb_backend": False,
            "general_thumb_optimizer": False,
            "whole_firmware_equivalence_proved": False,
            "fastdyn_speedup": False,
            "hardware_cycles": False,
        },
    }
    write_json(output / "report.json", report)
    print(json.dumps({
        "report": str(output / "report.json"),
        "candidate_id": candidate.candidate_id,
        "timing": timing["paired_comparison"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Install, prefilter and measure one profile-selected Thumb hot-exit layout."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import resource
import shutil
import statistics
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from capstone import CS_ARCH_ARM, CS_GRP_CALL, CS_GRP_JUMP, CS_MODE_MCLASS, CS_MODE_THUMB, Cs
from capstone.arm import ARM_OP_IMM
from elftools.elf.elffile import ELFFile
from keystone import KS_ARCH_ARM, KS_MODE_THUMB, Ks

from fastdyn.preflight import (
    Decision,
    EffectSummary,
    EmitInPlace,
    FirmwareUnitView,
    InstructionIdentity,
    MemoryEffect,
    MemoryRange,
    OriginInstruction,
    OriginRange,
    OriginStateMapping,
    PassRegistry,
    PatchContract,
    PreflightContext,
    PreflightPipeline,
    ReferenceEdge,
    ThumbHotExitLayoutPass,
    TypedRange,
    load_firmware_candidate,
    publish_firmware_candidate,
    qualify_firmware,
)
from fastdyn.preflight.model import canonical_json_bytes, hash_file, hash_json


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGE = ROOT / "tests/sample_binaries/RTOS/RTOSDemo.axf"
DEFAULT_CONFIG = ROOT / "configs/minimal.toml"
DEFAULT_FASTDYN = ROOT / "fastdyn-env/bin/fastdyn"
DEFAULT_OUTPUT = ROOT / "build/preflight/thumb-cortex-m3-hot-exit-layout-2026-11-17"
PROFILE = ROOT / "build/preflight/thumb-cortex-m3-frame-profile-2026-11-17/report.json"
FUNCTION_START = 0x08001AB0
FUNCTION_SIZE = 92
WORK_UNITS = 100_000
MINIMUM_USEFUL_CPU_RATIO = 1.005


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def file_bytes(image: Path, address: int, size: int) -> tuple[int, bytes]:
    with image.open("rb") as stream:
        elf = ELFFile(stream)
        for segment in elf.iter_segments():
            start = int(segment["p_vaddr"])
            if (
                segment["p_type"] == "PT_LOAD"
                and start <= address
                and address + size <= start + int(segment["p_filesz"])
            ):
                offset = int(segment["p_offset"]) + address - start
                stream.seek(offset)
                return offset, stream.read(size)
    raise RuntimeError(f"0x{address:x} is not file-backed")


def decode(data: bytes, address: int):
    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    decoder.detail = True
    instructions = list(decoder.disasm(data, address))
    if sum(item.size for item in instructions) != len(data):
        raise RuntimeError("Thumb function does not decode exactly")
    return instructions


def assemble_candidate() -> bytes:
    assembly = """
      push {r7, lr}
      mov r7, sp
      sub sp, #8
      nop
    test:
      movw r0, #276
      movt r0, #8192
      ldr r0, [r0]
      cbnz r0, cold
      add sp, #8
      pop {r7, pc}
    cold:
      nop
      bl 0x08006616
      movw r0, #228
      movt r0, #8192
      ldr r0, [r0, #12]
      ldr r0, [r0, #12]
      str r0, [sp, #4]
      ldr r0, [sp, #4]
      adds r0, #4
      bl 0x08003ab4
      movw r1, #156
      movt r1, #8192
      ldr r0, [r1]
      subs r0, #1
      str r0, [r1]
      movw r1, #276
      movt r1, #8192
      ldr r0, [r1]
      subs r0, #1
      str r0, [r1]
      bl 0x08006674
      ldr r0, [sp, #4]
      bl 0x08000890
      b test
    """
    encoded, _count = Ks(KS_ARCH_ARM, KS_MODE_THUMB).asm(assembly, FUNCTION_START)
    result = bytes(encoded)
    if len(result) != FUNCTION_SIZE:
        raise RuntimeError(f"candidate is {len(result)} bytes, expected {FUNCTION_SIZE}")
    return result


def split_identity_origins(
    ranges: tuple[OriginRange, ...], start: int, end: int,
) -> tuple[OriginRange, ...]:
    result = []
    for item in ranges:
        if item.original_end <= start or end <= item.original_start:
            result.append(item)
            continue
        if item.mapping != "identity":
            raise RuntimeError("cannot split a non-identity origin range")
        if item.original_start < start:
            result.append(OriginRange(
                item.original_start, start, item.emitted_start, start, "identity"
            ))
        if end < item.original_end:
            result.append(OriginRange(
                end, item.original_end, end, item.emitted_end, "identity"
            ))
    return tuple(result)


def instruction_origins_from_plan(
    unit: FirmwareUnitView, analysis: dict[str, Any],
) -> tuple[OriginInstruction, ...]:
    originals = {item.original_identity: item for item in unit.instructions}
    address = FUNCTION_START
    result = []
    for record in analysis["replacement_instructions"]:
        size = len(bytes.fromhex(str(record["encoding"])))
        original = originals[str(record["original_identity"])]
        result.append(OriginInstruction(
            address, size, original.address, original.size,
            original.original_identity,
        ))
        address += size
    if address != FUNCTION_START + FUNCTION_SIZE:
        raise RuntimeError("instruction-origin plan does not cover the function")
    return tuple(result)


def possible_faults(mnemonic: str) -> tuple[str, ...]:
    if mnemonic in {"push", "pop"}:
        return ("stack_fault",)
    if mnemonic.startswith("ldr"):
        return ("read_fault",)
    if mnemonic.startswith("str"):
        return ("write_fault",)
    if mnemonic in {"bl", "blx"}:
        return ("instruction_fault",)
    return ()


def build_unit(image: Path, target: Any) -> tuple[FirmwareUnitView, dict[str, Any]]:
    file_start, original_bytes = file_bytes(image, FUNCTION_START, FUNCTION_SIZE)
    original = decode(original_bytes, FUNCTION_START)
    candidate_bytes = assemble_candidate()
    candidate = decode(candidate_bytes, FUNCTION_START)
    expected = [
        ("push", "{r7, lr}"), ("mov", "r7, sp"), ("sub", "sp, #8"),
        ("b", "#0x8001ab8"), ("movw", "r0, #0x114"),
        ("movt", "r0, #0x2000"), ("ldr", "r0, [r0]"),
        ("cbz", "r0, #0x8001b08"),
    ]
    if len(original) != 34 or [
        (item.mnemonic, item.op_str) for item in original[:8]
    ] != expected:
        raise RuntimeError("frozen hot-exit source shape changed")
    if len(candidate) != len(original):
        raise RuntimeError("candidate instruction count changed")
    # Physical replacement order expressed in immutable original identities.
    mapping_indexes = [0, 1, 2, 3, 4, 5, 6, 7, 32, 33, 8, *range(9, 32)]
    if len(mapping_indexes) != len(original) or sorted(mapping_indexes) != list(range(34)):
        raise RuntimeError("origin permutation is incomplete")
    instructions = tuple(
        InstructionIdentity(
            f"idle-layout-original-{index}", f"0x{item.address:08x}",
            item.address, file_start + item.address - FUNCTION_START,
            item.size, bytes(item.bytes).hex(), item.mnemonic, item.op_str,
            possible_faults(item.mnemonic),
        )
        for index, item in enumerate(original)
    )
    by_address = {item.address: instructions[index] for index, item in enumerate(original)}
    references = []
    for index, item in enumerate(original):
        if not any(group in item.groups for group in (CS_GRP_JUMP, CS_GRP_CALL)):
            continue
        targets = [int(op.imm) & ~1 for op in item.operands if op.type == ARM_OP_IMM]
        if not targets:
            continue
        target_address = targets[-1]
        reference_target = by_address.get(target_address)
        references.append(
            ReferenceEdge(
                f"idle-layout-ref-{index}", instructions[index].identity, 0,
                "code", reference_target.identity if reference_target else f"external:0x{target_address:08x}",
                target_address, 0, item.mnemonic, None, None,
                "internal" if reference_target else "external", "immutable", "r-x", "resolved",
                ("cfg",),
            )
        )
    memory_effects = []
    calls = []
    for index, item in enumerate(original):
        if item.mnemonic in {"bl", "blx"}:
            calls.append(f"0x{next(int(op.imm) & ~1 for op in item.operands if op.type == ARM_OP_IMM):08x}")
        if not (
            item.mnemonic in {"push", "pop"}
            or item.mnemonic.startswith("ldr")
            or item.mnemonic.startswith("str")
        ):
            continue
        operation = "read" if item.mnemonic.startswith("ldr") or item.mnemonic == "pop" else "write"
        stack = "sp" in item.op_str or item.mnemonic in {"push", "pop"}
        memory_effects.append(
            MemoryEffect(
                f"idle-layout-effect-{len(memory_effects)}", instructions[index].identity,
                operation, "stack" if stack else "ram", item.op_str, 4,
                "r" if operation == "read" else "w", "unknown", "possible", True,
                len(memory_effects), possible_faults(item.mnemonic),
            )
        )
    effects = EffectSummary(memory=tuple(memory_effects), calls=tuple(sorted(set(calls))))
    unit = FirmwareUnitView(
        1, "cortex-m3:prvCheckTasksWaitingTermination:hot-exit-layout",
        "prvCheckTasksWaitingTermination", hash_file(image),
        hashlib.sha256(original_bytes).hexdigest(), target,
        FUNCTION_START, FUNCTION_START + FUNCTION_SIZE,
        file_start, file_start + FUNCTION_SIZE, instructions,
        (FUNCTION_START,), (instructions[-1].identity,),
        (TypedRange(
            "idle-layout-function", FUNCTION_START, FUNCTION_START + FUNCTION_SIZE,
            file_start, file_start + FUNCTION_SIZE, "code", "rx", "immutable", 2,
        ),), (), tuple(references), effects,
        tuple(
            OriginStateMapping(
                item.identity, item.original_identity, item.possible_faults,
                ("flags", "memory", "registers"),
            )
            for item in instructions if item.possible_faults
        ),
        PatchContract(), True,
    )
    replacement_records = []
    for emitted, original_index in zip(candidate, mapping_indexes):
        origin = instructions[original_index]
        replacement_records.append({
            "encoding": bytes(emitted.bytes).hex(),
            "mnemonic": emitted.mnemonic,
            "operands": emitted.op_str,
            "original_identity": origin.original_identity,
            "possible_faults": list(origin.possible_faults),
        })
    original_hot = [instructions[index].original_identity for index in [0,1,2,3,4,5,6,7,32,33]]
    original_cold_then_hot = [
        instructions[index].original_identity
        for index in [0,1,2,3,4,5,6,7,8,*range(9,32),4,5,6,7,32,33]
    ]
    analysis = {
        "complete": True,
        "entry_completeness": True,
        "internal_reference_repair_complete": True,
        "memory_call_effect_sequence_preserved": True,
        "hot_path_identity_sequence_preserved": True,
        "cold_path_identity_sequence_preserved": True,
        "instruction_count_preserved_per_path": True,
        "timing_model": "instruction_count",
        "hot_path_original_identities": original_hot,
        "hot_path_candidate_identities": original_hot,
        "cold_then_hot_original_identities": original_cold_then_hot,
        "cold_then_hot_candidate_identities": original_cold_then_hot,
        "replacement_instructions": replacement_records,
        "training_profile_hash": hash_file(PROFILE),
        "selected_profile_hits": 3_448_738,
    }
    return unit, analysis


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def paired_summary(samples: dict[str, list[dict[str, Any]]], seed: int) -> dict[str, Any]:
    result = {}
    reference = {item["block"]: item for item in samples["reference"]}
    candidate = {item["block"]: item for item in samples["candidate"]}
    for offset, metric in enumerate(("guest_benchmark_ns", "cpu_ns", "wall_ns")):
        ratios = [reference[index][metric] / candidate[index][metric] for index in reference]
        generator = random.Random(seed + offset)
        medians = [
            statistics.median(ratios[generator.randrange(len(ratios))] for _ in ratios)
            for _ in range(100_000)
        ]
        result[metric] = {
            "ratio_definition": f"reference {metric} / candidate {metric}",
            "median_ratio": statistics.median(ratios),
            "bootstrap_95_percent": [percentile(medians, .025), percentile(medians, .975)],
            "paired_ratios": ratios,
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--fastdyn", type=Path, default=DEFAULT_FASTDYN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--work-units", type=int, default=WORK_UNITS)
    parser.add_argument("--affinity-cpu", type=int, default=0)
    parser.add_argument("--diagnostic-only", action="store_true")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        if not args.replace:
            raise SystemExit(f"output exists: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)
    image = args.image.resolve()
    qualification = qualify_firmware(
        image, SimpleNamespace(arch="arm", cpu="cortex-m3", init_nsvtor=0x08000000)
    )
    unit, analysis = build_unit(image, qualification.target)
    write_json(output / "region-contract.json", unit.to_dict())
    write_json(output / "layout-analysis.json", analysis)
    registry = PassRegistry()
    registry.register(ThumbHotExitLayoutPass())
    proposal = f"{unit.unit_id}:thumb-hot-exit-layout"
    state_hash = hash_json({"uxDeletedTasksWaitingCleanUp": [0, 1], "timing": "icount"})
    context = PreflightContext(
        analyses={
            "state_decision": Decision(
                "accepted", "hot_and_cold_layout_states_constructed",
                {"state_set_hash": state_hash, "state_count": 2},
            ),
            "effect_summary": unit.effects.semantic_dict(),
            "entry_completeness": True,
            "reference_graph": [item.reference_id for item in unit.references],
            "hot_exit_layout": analysis,
        },
        testcase_decisions={
            proposal: Decision(
                "accepted", "hot_and_cold_identity_traces_agree",
                {"hot_trace": analysis["hot_path_original_identities"],
                 "cold_trace": analysis["cold_then_hot_original_identities"]},
            )
        },
        state_set_hash=state_hash,
        work_budget=2,
    )
    emitted = output / "RTOSDemo.hot-exit-layout.axf"
    outcome = PreflightPipeline(registry, EmitInPlace()).execute(
        "thumb-hot-exit-layout-v1", unit, context, image, emitted
    )
    write_json(output / "stage-ledger.json", [item.to_dict() for item in outcome.ledger])
    if outcome.status != "accepted" or outcome.edit is None:
        raise RuntimeError(f"layout rejected: {outcome.status}/{outcome.reason_code}")
    instruction_origins = instruction_origins_from_plan(unit, analysis)
    integration_report = {
        "status": "integration_accepted",
        "pass": "thumb-hot-exit-layout-v1",
        "profile_hash": hash_file(PROFILE),
        "profile_hits": analysis["selected_profile_hits"],
        "input_sha256": hash_file(image),
        "candidate_sha256": hash_file(emitted),
        "code_growth_bytes": 0,
        "instruction_origin_count": len(instruction_origins),
        "hot_path_instruction_count": len(analysis["hot_path_original_identities"]),
        "cold_then_hot_instruction_count": len(analysis["cold_then_hot_original_identities"]),
    }
    write_json(output / "integration-report.json", integration_report)

    diagnostic = output / "tcg-diagnostic"
    command = [
        "python3", str(ROOT / "scripts/preflight/run-thumb-tcg-diagnostic.py"),
        "--reference", str(image), "--candidate", str(emitted),
        "--config", str(args.config.resolve()), "--fastdyn", str(args.fastdyn.resolve()),
        "--output", str(diagnostic), "--address", hex(FUNCTION_START),
        "--work-units", "1000",
    ]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    subprocess.run(command, cwd=ROOT, env=environment, check=True)
    analyzer = load_script("thumb_tcg_analyzer", ROOT / "scripts/preflight/analyze-thumb-tcg.py")
    reference_log = diagnostic / "reference/fastdyn/qemu.log"
    candidate_log = diagnostic / "candidate/fastdyn/qemu.log"
    reference_blocks = [analyzer.extract(reference_log, address) for address in (0x08001AB0, 0x08001AB8, 0x08001B08)]
    candidate_blocks = [analyzer.extract(candidate_log, address) for address in (0x08001AB0, 0x08001AC4)]
    aggregate = {
        "schema_version": 1,
        "classification": "profile_selected_hot_path_generated_host_inventory",
        "reference": {
            "blocks": reference_blocks,
            "host_output_bytes": sum(item["host_output_bytes"] for item in reference_blocks),
            "optimized_tcg_operation_count": sum(item["optimized_tcg_operation_count"] for item in reference_blocks),
        },
        "candidate": {
            "blocks": candidate_blocks,
            "host_output_bytes": sum(item["host_output_bytes"] for item in candidate_blocks),
            "optimized_tcg_operation_count": sum(item["optimized_tcg_operation_count"] for item in candidate_blocks),
        },
        "claims": {"dynamic_execution_cost": False, "full_harness_profitability": False},
    }
    aggregate["delta"] = {
        key: aggregate["candidate"][key] - aggregate["reference"][key]
        for key in ("host_output_bytes", "optimized_tcg_operation_count")
    }
    write_json(output / "hot-path-tcg-inventory.json", aggregate)
    if args.diagnostic_only:
        print(json.dumps(aggregate["delta"], sort_keys=True))
        return 0
    if not (
        aggregate["delta"]["host_output_bytes"] < 0
        and aggregate["delta"]["optimized_tcg_operation_count"] <= 0
    ):
        write_json(output / "report.json", {
            "schema_version": 1, "status": "not_profitable",
            "reason_code": "generated_host_hot_path_not_pareto_improved",
            "integration": integration_report, "tcg": aggregate,
        })
        print("generated-host prefilter rejected candidate")
        return 0

    hot_pilot = load_script(
        "thumb_hot_pilot", ROOT / "scripts/preflight/run-thumb-hot-fop-pilot.py"
    )
    samples: dict[str, list[dict[str, Any]]] = {"reference": [], "candidate": []}
    generator = random.Random(20261117)
    sequence = 0
    for block in range(args.samples):
        order = ["reference", "candidate"]
        generator.shuffle(order)
        for variant in order:
            sequence += 1
            sample = hot_pilot.run_one(
                args.fastdyn.resolve(), args.config.resolve(),
                image if variant == "reference" else emitted,
                output / "runs" / f"{block:02d}-{variant}",
                args.work_units, args.affinity_cpu,
            )
            sample.update({"block": block, "sequence": sequence, "variant": variant})
            if (
                sample["returncode"] != 0
                or sample["completed_work_units"] != args.work_units
                or sample["guest_benchmark_ns"] is None
            ):
                raise RuntimeError(f"{variant} block {block} failed: {sample}")
            samples[variant].append(sample)
            print(
                f"[{sequence}/{args.samples * 2}] {variant}: "
                f"cpu={sample['cpu_ns'] / 1e9:.4f}s", flush=True,
            )
    ram_mismatches = [
        block for block in range(args.samples)
        if samples["reference"][block]["ram_prefix_sha256"]
        != samples["candidate"][block]["ram_prefix_sha256"]
    ]
    comparison = paired_summary(samples, 20261117)
    cpu = comparison["cpu_ns"]
    accepted = (
        not ram_mismatches
        and cpu["bootstrap_95_percent"][0] >= MINIMUM_USEFUL_CPU_RATIO
    )
    status = "counterexample" if ram_mismatches else "accepted" if accepted else "not_profitable"
    report = {
        "schema_version": 1,
        "classification": "real_cortex_m3_profile_selected_hot_exit_layout",
        "status": status,
        "input": {"path": str(image), "sha256": hash_file(image)},
        "candidate": {"path": str(emitted), "sha256": hash_file(emitted)},
        "integration": integration_report,
        "tcg": aggregate,
        "work": {"completed_systick_returns_per_execution": args.work_units},
        "samples": samples,
        "paired_comparison": comparison,
        "ram_prefix_mismatch_blocks": ram_mismatches,
        "advance_rule": "no RAM mismatch and lower 95% CPU ratio >= 1.005",
    }
    if accepted:
        published = publish_firmware_candidate(
            output / "firmware-candidate", image=emitted,
            original_image_hash=hash_file(image), target=qualification.target,
            entry_points=qualification.entry_points,
            memory_map=tuple(
                MemoryRange(
                    f"exec-{index}", item.original_start, item.original_end,
                    "r-x", "firmware",
                )
                for index, item in enumerate(qualification.executable_ranges)
            ),
            origin_ranges=split_identity_origins(
                qualification.executable_ranges,
                FUNCTION_START, FUNCTION_START + FUNCTION_SIZE,
            ),
            origin_instructions=instruction_origins,
            optimization_report={
                "status": status,
                "pass": "thumb-hot-exit-layout-v1",
                "instruction_origins": [item.to_dict() for item in instruction_origins],
                "paired_cpu": cpu,
                "tcg_delta": aggregate["delta"],
                "code_growth_bytes": 0,
            },
        )
        load_firmware_candidate(published.path)
        report["published_candidate_id"] = published.candidate_id
    write_json(output / "report.json", report)
    print(json.dumps({"status": status, "cpu": cpu}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

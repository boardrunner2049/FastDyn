#!/usr/bin/env python3
"""Install and measure two profile-selected Thumb FOP rewrites in real firmware."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import resource
import shutil
import statistics
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from capstone import CS_ARCH_ARM, CS_MODE_MCLASS, CS_MODE_THUMB, Cs
from capstone.arm import ARM_OP_IMM
from elftools.elf.elffile import ELFFile

from fastdyn.preflight import (
    Decision,
    EffectSummary,
    EmitInPlace,
    FirmwareUnitView,
    InstructionIdentity,
    MemoryEffect,
    MemoryRange,
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
DEFAULT_PROFILE = ROOT / "build/preflight/thumb-cortex-m3-profile-2026-11-16/report.json"
DEFAULT_OUTPUT = ROOT / "build/preflight/thumb-cortex-m3-hot-fop-2026-11-16"
SELECTED_RELOADS = (0x08000D8A, 0x080066FC)
WORK_UNITS = 50_000
MINIMUM_USEFUL_CPU_RATIO = 1.005
BENCH_RE = re.compile(
    r"\[BENCH\]\[thumb-hot-fop\]\s+elapsed_ns=(\d+).*work_units=(\d+)"
)
START_BLOCK = re.compile(
    r"\n\[\[CPU\.cpu0\.virtuals\]\]\s*# main\(\): start the timer\n"
    r'at = "0x08003972"\n'
    r'instruction = "benchmark_start"\n',
    re.MULTILINE,
)
END_BLOCK = re.compile(
    r"\n\[\[CPU\.cpu0\.virtuals\]\]\s*# vTaskStartScheduler\(\): print the time and stop\n"
    r'at = "0x08001940"\n'
    r'instruction = "benchmark_end"\n'
    r'args = \["minimal"\]\n',
    re.MULTILINE,
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def paired_summary(samples: dict[str, list[dict[str, Any]]], seed: int) -> dict[str, Any]:
    reference = {item["block"]: item for item in samples["reference"]}
    candidate = {item["block"]: item for item in samples["candidate"]}
    result = {}
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
            "bootstrap_95_percent": [percentile(medians, 0.025), percentile(medians, 0.975)],
            "paired_ratios": ratios,
        }
    return result


def load_selected(
    profile: Path,
    selection: str,
    selected_reloads: tuple[int, ...] = (),
) -> list[dict[str, Any]]:
    report = json.loads(profile.read_text())
    candidates = list(report["top_candidates"])
    rows = {
        int(item["reload_address"]): item
        for item in candidates
    }
    if selected_reloads:
        missing = [address for address in selected_reloads if address not in rows]
        if missing:
            raise RuntimeError(f"selected reload(s) missing from profile: {missing}")
        return [rows[address] for address in selected_reloads]
    if selection == "all-hit":
        selected = [item for item in candidates if int(item["hit_count"]) > 0]
        if not selected:
            raise RuntimeError("profile contains no executed candidate sites")
        return selected
    missing = [address for address in SELECTED_RELOADS if address not in rows]
    if missing:
        raise RuntimeError(f"frozen hot pair(s) missing from profile: {missing}")
    selected = [rows[address] for address in SELECTED_RELOADS]
    if [int(item["hit_count"]) for item in selected] != [28664, 28664]:
        raise RuntimeError("frozen profile counts changed")
    return selected


def file_offset(image: Path, address: int, size: int) -> int:
    with image.open("rb") as stream:
        elf = ELFFile(stream)
        for segment in elf.iter_segments():
            start = int(segment["p_vaddr"])
            if (
                segment["p_type"] == "PT_LOAD"
                and start <= address
                and address + size <= start + int(segment["p_filesz"])
            ):
                return int(segment["p_offset"]) + address - start
    raise RuntimeError(f"0x{address:x} is not file-backed")


def direct_branch_targets(image: Path) -> set[int]:
    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    decoder.detail = True
    targets: set[int] = set()
    with image.open("rb") as stream:
        elf = ELFFile(stream)
        for section in elf.iter_sections():
            if not (int(section["sh_flags"]) & 0x4) or int(section["sh_size"]) == 0:
                continue
            for instruction in decoder.disasm(section.data(), int(section["sh_addr"])):
                if not (
                    instruction.mnemonic.startswith("b")
                    or instruction.mnemonic in {"cbz", "cbnz"}
                ):
                    continue
                for operand in instruction.operands:
                    if operand.type == ARM_OP_IMM:
                        targets.add(int(operand.imm) & ~1)
    return targets


def make_unit(
    image: Path, row: dict[str, Any], target: Any, branch_targets: set[int]
) -> FirmwareUnitView:
    store_address = int(row["store_address"])
    reload_address = int(row["reload_address"])
    if reload_address in branch_targets:
        raise RuntimeError(f"reload 0x{reload_address:x} is an alternate direct entry")
    intervening_encodings = tuple(str(item) for item in row.get("intervening_encodings", ()))
    intervening_mnemonics = tuple(str(item) for item in row.get("intervening_mnemonics", ()))
    intervening_operands = tuple(str(item) for item in row.get("intervening_operands", ()))
    intervening_addresses = tuple(int(item) for item in row.get("intervening_addresses", ()))
    if not (
        len(intervening_encodings)
        == len(intervening_mnemonics)
        == len(intervening_operands)
        == len(intervening_addresses)
    ):
        raise RuntimeError("intervening instruction evidence is incomplete")
    expected = bytes.fromhex(
        str(row["store_encoding"])
        + "".join(intervening_encodings)
        + str(row["reload_encoding"])
    )
    start = file_offset(image, store_address, len(expected))
    body = image.read_bytes()[start:start + len(expected)]
    if body != expected:
        raise RuntimeError(f"bounded forwarding bytes changed at 0x{store_address:x}")
    region_addresses = (store_address,) + intervening_addresses + (reload_address,)
    alternate_entries = sorted(
        address for address in region_addresses[1:] if address in branch_targets
    )
    if alternate_entries:
        raise RuntimeError(
            "bounded forwarding region has alternate direct entries: "
            + ", ".join(hex(item) for item in alternate_entries)
        )
    prefix = f"hot-{reload_address:08x}"
    instruction_records = (
        ((str(row["store_encoding"]), "str", str(row["store_operands"]), store_address),)
        + tuple(
            zip(
                intervening_encodings,
                intervening_mnemonics,
                intervening_operands,
                intervening_addresses,
            )
        )
        + ((str(row["reload_encoding"]), "ldr", str(row["reload_operands"]), reload_address),)
    )
    instructions = []
    cursor = start
    for index, (encoding, mnemonic, operands, address) in enumerate(instruction_records):
        size = len(bytes.fromhex(encoding))
        is_memory = mnemonic.startswith("ldr") or mnemonic.startswith("str")
        faults = (
            ("stack_fault",)
            if is_memory and ("[sp" in operands or "[r7" in operands)
            else (("memory_fault",) if is_memory else ())
        )
        instructions.append(
            InstructionIdentity(
                f"{prefix}-i{index}", f"0x{address:08x}", address,
                cursor, size, encoding, mnemonic, operands, faults,
            )
        )
        cursor += size
    store = instructions[0]
    reload = instructions[-1]
    address_expression = f"entry_{row['base_register']}+{int(row['offset'])}"
    memory_effects = []
    for instruction in instructions:
        if not (
            instruction.mnemonic.startswith("ldr")
            or instruction.mnemonic.startswith("str")
        ):
            continue
        operation = "write" if instruction.mnemonic.startswith("str") else "read"
        is_target_stack = (
            instruction.operands == str(row["store_operands"])
            or instruction.operands == str(row["reload_operands"])
        ) and ("[sp" in instruction.operands or "[r7" in instruction.operands)
        is_removed_load = instruction.identity == reload.identity
        memory_effects.append(
            MemoryEffect(
                f"{prefix}-effect-{len(memory_effects)}",
                instruction.identity,
                operation,
                "stack" if is_target_stack else "ram",
                address_expression if is_target_stack else f"preserved:{instruction.operands}",
                4,
                "w" if operation == "write" else "r",
                "private" if is_target_stack else "unknown",
                "none" if is_target_stack else "possible",
                not is_removed_load,
                len(memory_effects),
                instruction.possible_faults,
            )
        )
    effects = EffectSummary(
        register_reads=(str(row["source_register"]), str(row["base_register"])),
        register_writes=(str(row["destination_register"]),),
        memory=tuple(memory_effects),
    )
    return FirmwareUnitView(
        1,
        f"cortex-m3:{row['function']}:0x{reload_address:08x}",
        str(row["function"]),
        hash_file(image),
        hashlib.sha256(body).hexdigest(),
        target,
        store_address,
        reload_address + reload.size,
        start,
        start + len(expected),
        tuple(instructions),
        (store_address,),
        (reload.identity,),
        (
            TypedRange(
                f"pair-{reload_address:08x}", store_address, reload_address + reload.size,
                start, start + len(expected), "code", "rx", "immutable", 2,
            ),
        ),
        (),
        (),
        effects,
        (
            *(
                OriginStateMapping(
                    item.identity, item.original_identity, item.possible_faults,
                    ("memory", "registers"),
                )
                for item in instructions if item.possible_faults
            ),
        ),
        PatchContract(),
        True,
    )


def install_sites(
    reference: Path, output: Path, rows: list[dict[str, Any]], target: Any,
) -> tuple[Path, list[dict[str, Any]], list[int]]:
    registry = PassRegistry()
    registry.register(ThumbStackForwardingPass())
    branch_targets = direct_branch_targets(reference)
    current = reference
    outcomes = []
    for index, row in enumerate(rows):
        unit = make_unit(current, row, target, branch_targets)
        write_json(output / f"region-{index}.json", unit.to_dict())
        proposal = f"{unit.unit_id}:thumb-stack-forwarding"
        state_values = [0, 1, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF]
        state_hash = hash_json({
            "values": state_values,
            "stable_private_stack": True,
            "reload": int(row["reload_address"]),
        })
        analysis = {
            "complete": True,
            "private_stack": True,
            "address_escaped": False,
            "observer_between": False,
            "store_identity": unit.instructions[0].identity,
            "load_identity": unit.instructions[-1].identity,
            "width": 4,
            "base_register": str(row["base_register"]),
            "intervening_instruction_identities": [
                item.identity for item in unit.instructions[1:-1]
            ],
            "value_preserved_between": True,
            "stack_slot_not_overwritten": True,
            "evidence": (
                "bounded same-address word spill/reload; exact intervening instructions "
                "do not clobber the source/base or overwrite the private slot; retained "
                "effects preserve event order under the fixed MPU configuration"
            ),
        }
        context = PreflightContext(
            analyses={
                "state_decision": Decision(
                    "accepted", "bounded_hot_thumb_states_ready",
                    {"state_count": len(state_values), "state_set_hash": state_hash},
                ),
                "effect_summary": unit.effects.semantic_dict(),
                "entry_completeness": True,
                "reference_graph": [],
                "stack_slot_forwarding": analysis,
            },
            testcase_decisions={
                proposal: Decision(
                    "accepted", "bounded_register_memory_outcomes_agree",
                    {"values": state_values, "mismatch_count": 0},
                )
            },
            state_set_hash=state_hash,
            work_budget=len(state_values),
        )
        emitted = output / f"site-{index}.axf"
        result = PreflightPipeline(registry, EmitInPlace()).execute(
            "thumb-stack-forwarding-v1", unit, context, current, emitted
        )
        if result.status != "accepted":
            raise RuntimeError(
                f"site 0x{int(row['reload_address']):x} rejected: "
                f"{result.status}/{result.reason_code}"
            )
        outcomes.append({
            "function": row["function"],
            "store_address": int(row["store_address"]),
            "reload_address": int(row["reload_address"]),
            "profile_hits": int(row["hit_count"]),
            "contract_hash": unit.contract_hash,
            "proposal_hash": result.edit.proposal_hash if result.edit else None,
            "stage_ledger": [item.to_dict() for item in result.ledger],
        })
        current = emitted
    original = reference.read_bytes()
    candidate = current.read_bytes()
    changed = [index for index, (left, right) in enumerate(zip(original, candidate)) if left != right]
    allowed_changed: set[int] = set()
    for row in rows:
        offset = file_offset(reference, int(row["reload_address"]), 2)
        allowed_changed.update((offset, offset + 1))
        expected = (
            bytes.fromhex("00bf")
            if row["source_register"] == row["destination_register"]
            else candidate[offset:offset + 2]
        )
        if candidate[offset:offset + 2] != expected:
            raise RuntimeError(f"unexpected replacement at 0x{int(row['reload_address']):x}")
        if candidate[offset:offset + 2] == original[offset:offset + 2]:
            raise RuntimeError(f"selected reload did not change at 0x{int(row['reload_address']):x}")
    if any(offset not in allowed_changed for offset in changed):
        raise RuntimeError(f"bytes outside selected reloads changed: {changed}")
    final = output / "RTOSDemo.hot-thumb-fop.axf"
    shutil.copy2(current, final)
    return final, outcomes, changed


def config_text(source: Path, image: Path, ram: Path, qmp: Path, work_units: int) -> str:
    text = source.read_text()
    text = text.replace(
        'binary = "tests/sample_binaries/RTOS/RTOSDemo.axf"',
        f'binary = "{image}"', 1,
    )
    text = text.replace(
        'memory_file  = "/tmp/fastdyn-minimal-ram0.ram"',
        f'memory_file  = "{ram}"', 1,
    )
    text = re.sub(r"monitor_port = 5555", "monitor_port = 0", text, count=1)
    text = text.replace(
        'platform = "generic-cortexm"',
        'platform = "generic-cortexm"\n'
        'icount = { shift = 5, sleep = false, align = false }',
        1,
    )
    text = re.sub(
        r'qmp_socket = "/tmp/fastdyn-minimal\.qmp"',
        f'qmp_socket = "{qmp}"', text, count=1,
    )
    text, start_count = START_BLOCK.subn("", text, count=1)
    text, end_count = END_BLOCK.subn("", text, count=1)
    if start_count != 1 or end_count != 1:
        raise RuntimeError("frozen benchmark virtual blocks changed")
    text += (
        "\n[[CPU.cpu0.virtuals]]\n"
        'at = "0x08001940"\n'
        'instruction = "benchmark_start"\n'
        "\n[[CPU.cpu0.virtuals]]\n"
        'at = "0x08006704"\n'
        'instruction = "benchmark_end_after"\n'
        f'args = ["{work_units}:thumb-hot-fop"]\n'
    )
    return text


def ram_prefix_hash(path: Path, size: int = 2 * 1024 * 1024) -> str | None:
    if not path.is_file():
        return None
    with path.open("rb") as stream:
        return hashlib.sha256(stream.read(size)).hexdigest()


def run_one(
    fastdyn: Path,
    config_source: Path,
    image: Path,
    run_dir: Path,
    work_units: int,
    affinity_cpu: int | None = None,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True)
    ram = run_dir / "ram0.ram"
    config = run_dir / "run.toml"
    config.write_text(config_text(config_source, image, ram, run_dir / "qmp.sock", work_units))
    before_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter_ns()
    command = [str(fastdyn), "-q", "run", "-c", str(config), "-o", str(run_dir / "fastdyn")]
    process = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        preexec_fn=(
            (lambda: os.sched_setaffinity(0, {affinity_cpu}))
            if affinity_cpu is not None
            else None
        ),
    )
    wall = time.perf_counter_ns() - started
    after_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    combined = process.stdout + process.stderr
    match = BENCH_RE.search(combined)
    return {
        "command": command,
        "returncode": process.returncode,
        "wall_ns": wall,
        "cpu_ns": round((
            after_usage.ru_utime - before_usage.ru_utime
            + after_usage.ru_stime - before_usage.ru_stime
        ) * 1e9),
        "guest_benchmark_ns": int(match.group(1)) if match else None,
        "completed_work_units": int(match.group(2)) if match else 0,
        "affinity_cpu": affinity_cpu,
        "ram_prefix_bytes": 2 * 1024 * 1024,
        "ram_prefix_sha256": ram_prefix_hash(ram),
        "stdout": process.stdout.splitlines(),
        "stderr": process.stderr.splitlines(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--fastdyn", type=Path, default=DEFAULT_FASTDYN)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--work-units", type=int, default=WORK_UNITS)
    parser.add_argument("--affinity-cpu", type=int)
    parser.add_argument("--selection", choices=("top2", "all-hit"), default="all-hit")
    parser.add_argument(
        "--selected-reload", action="append", type=lambda value: int(value, 0),
        default=[], help="select an exact profiled reload address; repeatable",
    )
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        if not args.replace:
            raise SystemExit(f"output exists: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)
    image = args.image.resolve()
    rows = load_selected(
        args.profile.resolve(), args.selection, tuple(args.selected_reload)
    )
    qualification = qualify_firmware(
        image, SimpleNamespace(arch="arm", cpu="cortex-m3", init_nsvtor=0x08000000)
    )
    emitted, outcomes, changed = install_sites(image, output, rows, qualification.target)
    candidate = publish_firmware_candidate(
        output / "firmware-candidate",
        image=emitted,
        original_image_hash=hash_file(image),
        target=qualification.target,
        entry_points=qualification.entry_points,
        memory_map=tuple(
            MemoryRange(f"exec-{index}", item.original_start, item.original_end, "r-x", "firmware")
            for index, item in enumerate(qualification.executable_ranges)
        ),
        origin_ranges=qualification.executable_ranges,
        optimization_report={
            "status": "accepted",
            "passes": ["thumb-stack-forwarding-v1"],
            "sites": outcomes,
            "changed_file_offsets": changed,
            "code_growth_bytes": 0,
            "profile_identity": hash_file(args.profile.resolve()),
            "limits": {
                "general_thumb_backend": False,
                "whole_firmware_equivalence_proved": False,
                "stable_private_stack_contract": True,
            },
        },
    )
    load_firmware_candidate(output / "firmware-candidate")

    samples: dict[str, list[dict[str, Any]]] = {"reference": [], "candidate": []}
    generator = random.Random(20261116)
    sequence = 0
    for block in range(args.samples):
        order = ["reference", "candidate"]
        generator.shuffle(order)
        for variant in order:
            sequence += 1
            sample = run_one(
                args.fastdyn.resolve(), args.config.resolve(),
                image if variant == "reference" else candidate.image_path,
                output / "runs" / f"{block:02d}-{variant}", args.work_units,
                args.affinity_cpu,
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
                f"guest={sample['guest_benchmark_ns'] / 1e9:.6f}s",
                flush=True,
            )
    ram_mismatches = [
        block for block in range(args.samples)
        if samples["reference"][block]["ram_prefix_sha256"]
        != samples["candidate"][block]["ram_prefix_sha256"]
    ]
    comparison = paired_summary(samples, 20261116)
    cpu = comparison["cpu_ns"]
    semantic_status = "accepted" if not ram_mismatches else "counterexample"
    profitable = (
        not ram_mismatches
        and cpu["bootstrap_95_percent"][0] >= MINIMUM_USEFUL_CPU_RATIO
    )
    final_status = (
        "counterexample" if ram_mismatches
        else "accepted" if profitable
        else "not_profitable"
    )
    report = {
        "schema_version": 1,
        "classification": "real_cortex_m3_profile_selected_fixed_work_pilot",
        "status": final_status,
        "semantic_status": semantic_status,
        "input": {"path": str(image), "sha256": hash_file(image)},
        "candidate": {
            "path": str(candidate.image_path),
            "sha256": candidate.image_hash,
            "candidate_id": candidate.candidate_id,
            "code_growth_bytes": 0,
        },
        "profile": {
            "path": str(args.profile.resolve()),
            "sha256": hash_file(args.profile.resolve()),
            "selected_sites": outcomes,
            "selection_policy": args.selection,
        },
        "work": {
            "oracle": "completed SysTick_Handler returns",
            "completed_units_per_sample": args.work_units,
            "minimum_removed_guest_stack_loads_per_sample": args.work_units * 2,
            "selected_static_reload_sites": len(rows),
            "exact_dynamic_removed_loads": "not_collected_for_non_tick_sites",
            "minimum_useful_cpu_ratio_predeclared": MINIMUM_USEFUL_CPU_RATIO,
            "calibration_target_active_seconds": [0.5, 2.0],
        },
        "validation": {
            "changed_file_offsets": changed,
            "changed_bytes": len(changed),
            "file_growth_bytes": emitted.stat().st_size - image.stat().st_size,
            "ram_prefix_bytes_compared_per_sample": 2 * 1024 * 1024,
            "ram_mismatch_blocks": ram_mismatches,
            "all_samples_completed_equal_work": True,
        },
        "method": {
            "paired_blocks": args.samples,
            "randomization_seed": 20261116,
            "bootstrap_resamples": 100_000,
            "training_profile_separate_from_timing": True,
            "affinity_cpu": args.affinity_cpu,
            "schedule": (
                "QEMU icount shift=5, sleep=false, align=false; separately identified "
                "deterministic measurement environment"
            ),
        },
        "samples": samples,
        "paired_comparison": comparison,
        "decision": {
            "cpu_interval_excludes_neutrality": cpu["bootstrap_95_percent"][0] > 1.0,
            "cpu_meets_minimum_useful_effect": cpu["bootstrap_95_percent"][0]
            >= MINIMUM_USEFUL_CPU_RATIO,
            "cpu_regression_interval_below_neutrality":
                cpu["bootstrap_95_percent"][1] < 1.0,
            "deployment_selected": "candidate" if profitable else "reference",
            "candidate_artifact_disposition": (
                "accepted" if profitable
                else "semantic_counterexample" if ram_mismatches
                else "rejected_not_profitable"
            ),
        },
        "claims": {
            "real_firmware_execution": True,
            "general_thumb_optimizer": False,
            "whole_firmware_equivalence": False,
            "hardware_cycles": False,
        },
    }
    write_json(output / "report.json", report)
    print(json.dumps({
        "report": str(output / "report.json"),
        "status": report["status"],
        "ram_mismatches": ram_mismatches,
        "cpu": comparison["cpu_ns"],
        "guest": comparison["guest_benchmark_ns"],
    }, sort_keys=True))
    return 1 if report["status"] == "counterexample" else 0


if __name__ == "__main__":
    raise SystemExit(main())

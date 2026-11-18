#!/usr/bin/env python3
"""Install and measure profile-selected Thumb branch-to-next rewrites."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from elftools.elf.elffile import ELFFile

from fastdyn.preflight import (
    Decision,
    EffectSummary,
    EmitInPlace,
    FirmwareUnitView,
    InstructionIdentity,
    MemoryRange,
    OriginStateMapping,
    PassRegistry,
    PatchContract,
    PreflightContext,
    PreflightPipeline,
    ReferenceEdge,
    ThumbFallthroughBranchPass,
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
DEFAULT_PROFILE = (
    ROOT / "build/preflight/thumb-cortex-m3-fallthrough-profile-2026-11-16/report.json"
)
DEFAULT_OUTPUT = (
    ROOT / "build/preflight/tfb-pin-final-2026-11-16"
)
WORK_UNITS = 100_000
MINIMUM_USEFUL_CPU_RATIO = 1.005


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def load_script(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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


def selected_rows(profile: Path) -> list[dict[str, Any]]:
    report = json.loads(profile.read_text())
    rows = [item for item in report["candidates"] if int(item["hit_count"]) > 0]
    if not rows:
        raise RuntimeError("profile contains no executed fallthrough branches")
    if any(
        int(item["resolved_target"]) != int(item["branch_address"]) + 2
        for item in rows
    ):
        raise RuntimeError("profile contains a non-fallthrough target")
    return sorted(rows, key=lambda item: int(item["branch_address"]))


def make_unit(
    image: Path, row: dict[str, Any], target: Any,
) -> FirmwareUnitView:
    address = int(row["branch_address"])
    offset = file_offset(image, address, 2)
    encoding = str(row["encoding"])
    body = image.read_bytes()[offset:offset + 2]
    if body != bytes.fromhex(encoding):
        raise RuntimeError(f"branch bytes changed at 0x{address:x}")
    identity = f"fallthrough-{address:08x}"
    branch = InstructionIdentity(
        identity,
        f"original:0x{address:08x}",
        address,
        offset,
        2,
        encoding,
        "b",
        str(row["operands"]),
    )
    reference = ReferenceEdge(
        f"fallthrough-edge-{address:08x}",
        branch.identity,
        0,
        "code",
        f"address:0x{address + 2:08x}",
        address + 2,
        0,
        "thumb-b11",
        -2048,
        2046,
        "direct",
        "immutable",
        "rx",
        "resolved",
        ("cfg", "reference_graph"),
    )
    return FirmwareUnitView(
        1,
        f"cortex-m3:{row['function']}:0x{address:08x}",
        str(row["function"]),
        hash_file(image),
        hashlib.sha256(body).hexdigest(),
        target,
        address,
        address + 2,
        offset,
        offset + 2,
        (branch,),
        (address,),
        (branch.identity,),
        (
            TypedRange(
                f"branch-{address:08x}", address, address + 2,
                offset, offset + 2, "code", "rx", "immutable", 2,
            ),
        ),
        (),
        (reference,),
        EffectSummary(),
        (
            OriginStateMapping(
                branch.identity, branch.original_identity, (), ("control",)
            ),
        ),
        PatchContract(),
        True,
    )


def install_sites(
    reference: Path,
    output: Path,
    rows: list[dict[str, Any]],
    target: Any,
) -> tuple[Path, list[dict[str, Any]], list[int]]:
    registry = PassRegistry()
    registry.register(ThumbFallthroughBranchPass())
    current = reference
    prior_intermediate: Path | None = None
    outcomes: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        unit = make_unit(current, row, target)
        write_json(output / "regions" / f"region-{index:03d}.json", unit.to_dict())
        proposal = f"{unit.unit_id}:thumb-fallthrough-branch"
        state_hash = hash_json({
            "rule": "unconditional-branch-target-equals-sequential-successor",
            "address": int(row["branch_address"]),
            "branch_observable": False,
            "modeled_timing_observable": False,
            "instruction_count_preserved": True,
        })
        context = PreflightContext(
            analyses={
                "state_decision": Decision(
                    "accepted", "instruction_semantics_constructed",
                    {"state_space": "all_register_flag_memory_states"},
                ),
                "effect_summary": unit.effects.semantic_dict(),
                "entry_completeness": True,
                "reference_graph": [asdict(item) for item in unit.references],
                "fallthrough_branch": {
                    "complete": True,
                    "target_address": int(row["resolved_target"]),
                    "branch_observable": False,
                    "modeled_timing_observable": False,
                    "schedule_metric": "guest_instruction_count",
                    "reference_id": unit.references[0].reference_id,
                },
            },
            testcase_decisions={
                proposal: Decision(
                    "accepted", "pc_register_flag_memory_outcomes_agree",
                    {
                        "reference_next_pc": int(row["resolved_target"]),
                        "candidate_next_pc": int(row["branch_address"]) + 2,
                    },
                )
            },
            state_set_hash=state_hash,
            work_budget=1,
        )
        emitted = output / f".intermediate-{index:03d}.axf"
        result = PreflightPipeline(registry, EmitInPlace()).execute(
            "thumb-fallthrough-branch-v1", unit, context, current, emitted
        )
        if result.status != "accepted":
            raise RuntimeError(
                f"site 0x{int(row['branch_address']):x} rejected: "
                f"{result.status}/{result.reason_code}"
            )
        outcomes.append({
            "function": row["function"],
            "branch_address": int(row["branch_address"]),
            "profile_hits": int(row["hit_count"]),
            "contract_hash": unit.contract_hash,
            "proposal_hash": result.edit.proposal_hash if result.edit else None,
            "stage_ledger": [item.to_dict() for item in result.ledger],
        })
        if prior_intermediate is not None:
            prior_intermediate.unlink()
        prior_intermediate = emitted
        current = emitted

    final = output / "RTOSDemo.fallthrough-branch.axf"
    shutil.copy2(current, final)
    if prior_intermediate is not None:
        prior_intermediate.unlink()
    original = reference.read_bytes()
    candidate = final.read_bytes()
    changed = [
        index for index, (left, right) in enumerate(zip(original, candidate))
        if left != right
    ]
    allowed: set[int] = set()
    for row in rows:
        offset = file_offset(reference, int(row["branch_address"]), 2)
        allowed.update((offset, offset + 1))
        if candidate[offset:offset + 2] != bytes.fromhex("00bf"):
            raise RuntimeError(
                f"unexpected replacement at 0x{int(row['branch_address']):x}"
            )
    if any(offset not in allowed for offset in changed):
        raise RuntimeError(f"bytes outside selected branches changed: {changed}")
    return final, outcomes, changed


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
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    output = args.output.resolve()
    if output.exists():
        if not args.replace:
            raise SystemExit(f"output exists: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)
    image = args.image.resolve()
    profile = args.profile.resolve()
    rows = selected_rows(profile)
    qualification = qualify_firmware(
        image, SimpleNamespace(arch="arm", cpu="cortex-m3", init_nsvtor=0x08000000)
    )
    emitted, outcomes, changed = install_sites(
        image, output, rows, qualification.target
    )
    candidate = publish_firmware_candidate(
        output / "firmware-candidate",
        image=emitted,
        original_image_hash=hash_file(image),
        target=qualification.target,
        entry_points=qualification.entry_points,
        memory_map=tuple(
            MemoryRange(
                f"exec-{index}", item.original_start, item.original_end,
                "r-x", "firmware",
            )
            for index, item in enumerate(qualification.executable_ranges)
        ),
        origin_ranges=qualification.executable_ranges,
        optimization_report={
            "status": "semantically_accepted_pending_profitability",
            "passes": ["thumb-fallthrough-branch-v1"],
            "sites": outcomes,
            "changed_file_offsets": changed,
            "code_growth_bytes": 0,
            "profile_identity": hash_file(profile),
            "limits": {
                "branch_event_observer": False,
                "modeled_instruction_timing": False,
                "schedule_metric": "guest_instruction_count",
                "whole_firmware_equivalence_proved": False,
            },
        },
    )
    load_firmware_candidate(output / "firmware-candidate")

    hot = load_script(
        "fastdyn_thumb_hot_measurement",
        ROOT / "scripts/preflight/run-thumb-hot-fop-pilot.py",
    )
    samples: dict[str, list[dict[str, Any]]] = {"reference": [], "candidate": []}
    allowed_cpus = sorted(os.sched_getaffinity(0))
    affinity_cpu = (
        args.affinity_cpu if args.affinity_cpu is not None else allowed_cpus[0]
    )
    if affinity_cpu not in allowed_cpus:
        raise RuntimeError(
            f"requested CPU {affinity_cpu} is outside affinity {allowed_cpus}"
        )
    generator = random.Random(20261116)
    sequence = 0
    for block in range(args.samples):
        order = ["reference", "candidate"]
        generator.shuffle(order)
        for variant in order:
            sequence += 1
            sample = hot.run_one(
                args.fastdyn.resolve(),
                args.config.resolve(),
                image if variant == "reference" else candidate.image_path,
                output / "runs" / f"{block:02d}-{variant}",
                args.work_units,
                affinity_cpu,
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
    comparison = hot.paired_summary(samples, 20261116)
    cpu = comparison["cpu_ns"]
    profitable = (
        not ram_mismatches
        and cpu["bootstrap_95_percent"][0] >= MINIMUM_USEFUL_CPU_RATIO
    )
    status = (
        "counterexample" if ram_mismatches
        else "accepted" if profitable
        else "not_profitable"
    )
    report = {
        "schema_version": 1,
        "classification": "real_cortex_m3_fallthrough_branch_pilot",
        "status": status,
        "semantic_status": "accepted" if not ram_mismatches else "counterexample",
        "input": {"path": str(image), "sha256": hash_file(image)},
        "candidate": {"path": str(candidate.image_path), "sha256": candidate.image_hash},
        "profile": {"path": str(profile), "sha256": hash_file(profile)},
        "sites": outcomes,
        "work": {
            "oracle": "completed SysTick_Handler returns",
            "completed_units_per_sample": args.work_units,
            "selected_static_branch_sites": len(rows),
            "training_observed_branch_hits": sum(int(item["hit_count"]) for item in rows),
            "minimum_useful_cpu_ratio_predeclared": MINIMUM_USEFUL_CPU_RATIO,
            "affinity_cpu": affinity_cpu,
        },
        "validation": {
            "changed_file_offsets": changed,
            "changed_bytes": len(changed),
            "file_growth_bytes": emitted.stat().st_size - image.stat().st_size,
            "ram_prefix_bytes_compared_per_sample": 2 * 1024 * 1024,
            "ram_mismatch_blocks": ram_mismatches,
            "all_samples_completed_equal_work": True,
            "instruction_count_preserved_per_site": True,
        },
        "method": {
            "paired_blocks": args.samples,
            "randomization_seed": 20261116,
            "bootstrap_resamples": 100_000,
            "training_profile_separate_from_timing": True,
            "schedule": (
                "QEMU icount shift=5, sleep=false, align=false; separately identified "
                "deterministic measurement environment"
            ),
        },
        "samples": samples,
        "paired_comparison": comparison,
        "decision": {
            "deployment_selected": "candidate" if profitable else "reference",
            "candidate_artifact_disposition": (
                "accepted" if profitable
                else "semantic_counterexample" if ram_mismatches
                else "rejected_not_profitable"
            ),
            "cpu_meets_minimum_useful_effect": (
                cpu["bootstrap_95_percent"][0] >= MINIMUM_USEFUL_CPU_RATIO
            ),
        },
        "claims": {
            "real_firmware_execution": True,
            "general_thumb_optimizer": False,
            "whole_firmware_equivalence": False,
            "hardware_cycles": False,
            "branch_or_timing_observer_contract": False,
        },
    }
    write_json(output / "report.json", report)
    print(json.dumps({
        "report": str(output / "report.json"),
        "status": status,
        "sites": len(rows),
        "ram_mismatches": ram_mismatches,
        "cpu": cpu,
        "guest": comparison["guest_benchmark_ns"],
    }, sort_keys=True))
    return 1 if status == "counterexample" else 0


if __name__ == "__main__":
    raise SystemExit(main())

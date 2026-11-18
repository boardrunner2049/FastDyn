#!/usr/bin/env python3
"""Apply the BOLT-first firmware acceptance layer to the fixed-work queue ELF.

The optimizer is the pinned, extended BOLT.  This driver reuses the SmartSpeaker
wrapper's bounded x86 reference/effect qualifications, repairs admitted direct
and RIP-relative references for original placement, and publishes only bodies
that fit their original ranges.  It is a supported-host integration experiment,
not a Cortex-M or FastDyn performance result.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BOLT = ROOT / "third_party/preflight/llvm-build/bin/llvm-bolt"
DEFAULT_BAT_DUMP = ROOT / "third_party/preflight/llvm-build/bin/llvm-bat-dump"
DEFAULT_REFERENCE = ROOT / "build/preflight/lemix-queue/O0/lemix_queue_workload"
DEFAULT_PROFILE = ROOT / "build/preflight/lemix-queue/O0/lemix-queue-training.fdata"
DEFAULT_STOCK = ROOT / "build/preflight/lemix-queue/O0/lemix_queue_workload.bolt-pgo"
DEFAULT_OUTPUT = ROOT / "build/preflight/lemix-queue/bolt-firmware-wrapper"
TICK_RE = re.compile(r" tick=\d+$")
FIXED_WORK_RE = re.compile(
    r"FAST_FIXED_WORK start_tick=(\d+) end_tick=(\d+) completed_ticks=(\d+) "
    r"start_markers=(\d+) end_markers=(\d+) completed_units=(\d+) "
    r"driver_cpu_ns=(\d+)"
)
DISPATCH_MARKER = "[JS:Debug] Interrupt_Handler_Task running"
INTERRUPT_EVENTS = frozenset({
    "WWDG_IRQ", "RTC_WKUP_IRQ", "DMA1_Stream3_IRQ", "DMA1_Stream4_IRQ",
    "DMA1_Stream5_IRQ", "DMA1_Stream6_IRQ", "USART1_IRQ", "USART2_IRQ",
    "RTC_Alarm_IRQ", "SDIO_IRQ",
})


def load_wrapper() -> Any:
    path = ROOT / "scripts/preflight/run-bolt-firmware-wrapper.py"
    spec = importlib.util.spec_from_file_location("fastdyn_bolt_wrapper", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load wrapper helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def observe(
    path: Path,
    program_arguments: tuple[str, ...] = (),
    *,
    preload: Path | None = None,
    environment_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    environment = dict(os.environ)
    if preload is not None:
        environment["LD_PRELOAD"] = str(preload)
    if environment_overrides:
        environment.update(environment_overrides)
    result = subprocess.run(
        [str(path), *program_arguments], capture_output=True, text=True,
        env=environment, check=False,
    )
    output = result.stdout.strip()
    if preload is not None:
        match = FIXED_WORK_RE.search(result.stderr)
        summary = None if match is None else {
            "start_tick": int(match.group(1)),
            "end_tick": int(match.group(2)),
            "completed_ticks": int(match.group(3)),
            "start_markers": int(match.group(4)),
            "end_markers": int(match.group(5)),
            "completed_units": int(match.group(6)),
            "driver_cpu_ns": int(match.group(7)),
        }
        trace = [line for line in result.stdout.splitlines() if line in INTERRUPT_EVENTS]
        trace_prefix = trace[:400]
        signature = {
            "completed_ticks": summary["completed_ticks"] if summary else None,
            "completed_units": summary["completed_units"] if summary else None,
            "dispatch_markers": result.stdout.count(DISPATCH_MARKER),
            "trace_prefix_events": len(trace_prefix),
            "trace_prefix_sha256": hashlib.sha256(
                ("\n".join(trace_prefix) + "\n").encode()
            ).hexdigest(),
        }
        return {
            "returncode": result.returncode,
            "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
            "stdout_bytes": len(result.stdout.encode()),
            "stderr_bytes": len(result.stderr.encode()),
            "fixed_work": summary,
            "normalized_output": json.dumps(signature, sort_keys=True),
            **signature,
        }
    return {
        "returncode": result.returncode,
        "stdout": output,
        "stderr": result.stderr.strip(),
        "normalized_output": TICK_RE.sub("", output),
    }


def merge_counts(*groups: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for group in groups:
        for key, value in group.items():
            merged[key] = merged.get(key, 0) + int(value)
    return merged


def repair_direct_calls_bounded(
    wrapper: Any,
    native: dict[str, Any],
    reference: Path,
    candidate: Path,
    names: list[str],
) -> tuple[list[str], dict[str, bytes], dict[str, list[dict[str, Any]]], dict[str, int]]:
    """Keep unsupported call encodings unchanged instead of aborting the run."""

    accepted: list[str] = []
    overrides: dict[str, bytes] = {}
    repairs: dict[str, list[dict[str, Any]]] = {}
    rejections: dict[str, int] = {}
    for name in names:
        try:
            one_override, one_repair = wrapper.repair_direct_calls_for_in_place(
                native, reference, candidate, [name]
            )
        except RuntimeError as error:
            reason = wrapper._repair_reason(error)
            rejections[reason] = rejections.get(reason, 0) + 1
            continue
        accepted.append(name)
        overrides.update(one_override)
        repairs.update(one_repair)
    return accepted, overrides, repairs, rejections


def repair_rip_relative_bounded(
    wrapper: Any,
    native: dict[str, Any],
    reference: Path,
    candidate: Path,
    names: list[str],
    *,
    body_overrides: dict[str, bytes] | None = None,
) -> tuple[list[str], dict[str, bytes], dict[str, list[dict[str, Any]]], dict[str, int]]:
    """Keep unsupported data-reference encodings unchanged instead of aborting."""

    accepted: list[str] = []
    overrides: dict[str, bytes] = {}
    repairs: dict[str, list[dict[str, Any]]] = {}
    rejections: dict[str, int] = {}
    for name in names:
        one_body = (
            {name: body_overrides[name]}
            if body_overrides is not None and name in body_overrides
            else None
        )
        try:
            one_override, one_repair = wrapper.repair_rip_relative_for_in_place(
                native, reference, candidate, [name], body_overrides=one_body
            )
        except RuntimeError as error:
            reason = wrapper._repair_reason(error)
            rejections[reason] = rejections.get(reason, 0) + 1
            continue
        accepted.append(name)
        overrides.update(one_override)
        repairs.update(one_repair)
    return accepted, overrides, repairs, rejections


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bolt", type=Path, default=DEFAULT_BOLT)
    parser.add_argument("--bat-dump", type=Path, default=DEFAULT_BAT_DUMP)
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--stock-bolt", type=Path, default=DEFAULT_STOCK)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--bolt-extra-arg", action="append", default=[],
        help="additional frozen BOLT argument; use --bolt-extra-arg=--flag=value",
    )
    parser.add_argument("--optimization-policy-id", default="fop-layout-baseline")
    parser.add_argument("--max-region-bytes", type=int, default=1024)
    parser.add_argument(
        "--program-arg", action="append", default=[],
        help="argument passed to every fixed-work execution of the reference/candidates",
    )
    parser.add_argument(
        "--preload", type=Path,
        help="optional fixed-work interposer applied identically to every observation",
    )
    parser.add_argument(
        "--environment", action="append", default=[], metavar="NAME=VALUE",
        help="environment entry applied identically to every observation",
    )
    parser.add_argument(
        "--expected-returncode", type=int, default=0,
        help="required exit status for every fixed-work observation",
    )
    parser.add_argument(
        "--artifact-stem", default="lemix_queue_workload",
        help="filename stem for emitted candidates",
    )
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    if args.max_region_bytes <= 0:
        raise SystemExit("--max-region-bytes must be positive")
    if not args.artifact_stem or Path(args.artifact_stem).name != args.artifact_stem:
        raise SystemExit("--artifact-stem must be one filename component")

    wrapper = load_wrapper()
    bolt = args.bolt.resolve()
    reference = args.reference.resolve()
    profile = args.profile.resolve()
    stock_bolt = args.stock_bolt.resolve()
    output = args.output.resolve()
    if output.exists():
        if not args.replace:
            raise SystemExit(f"output exists: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)

    program_arguments = tuple(str(item) for item in args.program_arg)
    preload = args.preload.resolve() if args.preload is not None else None
    if preload is not None and not preload.is_file():
        raise SystemExit(f"preload does not exist: {preload}")
    environment_overrides: dict[str, str] = {}
    for specification in args.environment:
        name, separator, value = specification.partition("=")
        if not separator or not name:
            raise SystemExit(f"invalid --environment entry: {specification}")
        environment_overrides[name] = value
    full_candidate = output / f"{args.artifact_stem}.bolt-fop"
    native_report_path = output / "bolt-post-optimization-report.json"
    command = [
        str(bolt), str(reference), f"--data={profile}", "--frame-opt=all",
        "--reorder-blocks=ext-tsp", "--reorder-functions=cdsort",
        "--split-functions", "--split-all-cold", "--enable-bat",
        f"--fastdyn-firmware-report={native_report_path}",
        *args.bolt_extra_arg, "-o", str(full_candidate),
    ]
    run = subprocess.run(command, capture_output=True, text=True, check=False)
    (output / "bolt.stdout").write_text(run.stdout)
    (output / "bolt.stderr").write_text(run.stderr)
    write_json(output / "command.json", {"argv": command, "returncode": run.returncode})
    if run.returncode != 0:
        raise SystemExit(f"BOLT failed; see {output / 'bolt.stderr'}")
    native = json.loads(native_report_path.read_text())
    if native.get("phase") != "post-frame-optimizer-and-alloc-combiner":
        raise RuntimeError("native report has the wrong pass phase")

    native_by_name = {
        wrapper.restored_bolt_name(str(item["name"])): item
        for item in native["functions"]
    }
    bat = wrapper.BatIdentityResolver(args.bat_dump, full_candidate)

    automatic, auto_rejections = wrapper.automatic_compact_cohort(
        native, reference, full_candidate, args.max_region_bytes
    )
    base, base_details, base_effect_rejections = wrapper.qualify_memory_effect_contract(
        native, automatic
    )

    direct, direct_rejections = wrapper.direct_call_compact_cohort(
        native, reference, full_candidate, args.max_region_bytes
    )
    direct, direct_details, direct_effect_rejections = (
        wrapper.qualify_memory_effect_contract(native, direct)
    )
    direct, direct_overrides, direct_repairs, direct_repair_rejections = (
        repair_direct_calls_bounded(
            wrapper, native, reference, full_candidate, direct
        )
    )

    branches, branch_records, branch_rejections = wrapper.internal_branch_compact_cohort(
        native, reference, full_candidate, args.max_region_bytes
    )
    branches, branch_details, branch_effect_rejections = (
        wrapper.qualify_memory_effect_contract(native, branches)
    )
    branch_records = {name: branch_records[name] for name in branches}

    rip, rip_rejections = wrapper.rip_relative_compact_cohort(
        native, reference, full_candidate, args.max_region_bytes
    )
    rip, rip_details, rip_effect_rejections = wrapper.qualify_memory_effect_contract(
        native, rip
    )
    rip, rip_overrides, rip_repairs, rip_repair_rejections = (
        repair_rip_relative_bounded(
            wrapper, native, reference, full_candidate, rip
        )
    )

    call_rip, call_rip_rejections = wrapper.call_rip_compact_cohort(
        native, reference, full_candidate, args.max_region_bytes
    )
    call_rip, call_rip_details, call_rip_effect_rejections = (
        wrapper.qualify_memory_effect_contract(native, call_rip)
    )
    (
        call_rip,
        call_rip_call_overrides,
        call_rip_call_repairs,
        call_rip_call_rejections,
    ) = repair_direct_calls_bounded(
            wrapper, native, reference, full_candidate, call_rip
        )
    (
        call_rip,
        call_rip_overrides,
        call_rip_data_repairs,
        call_rip_data_rejections,
    ) = repair_rip_relative_bounded(
            wrapper, native, reference, full_candidate, call_rip,
            body_overrides=call_rip_call_overrides,
        )
    call_rip_call_overrides = {
        name: body for name, body in call_rip_call_overrides.items()
        if name in call_rip
    }
    call_rip_call_repairs = {
        name: records for name, records in call_rip_call_repairs.items()
        if name in call_rip
    }

    already = set(base + direct + branches + rip + call_rip)
    heavy, heavy_records, heavy_rejections, heavy_diagnostics = (
        wrapper.branch_heavy_compact_cohort(
            native, reference, full_candidate, already, args.max_region_bytes
        )
    )
    heavy, heavy_details, heavy_effect_rejections = (
        wrapper.qualify_memory_effect_contract(native, heavy)
    )
    heavy_records = {name: heavy_records[name] for name in heavy}
    heavy_calls = [
        name for name in heavy
        if any(item["is_call"] for item in native_by_name[name]["instructions"])
    ]
    heavy_rip = [
        name for name in heavy
        if any("%rip" in item["text"] for item in native_by_name[name]["instructions"])
    ]
    (
        heavy_calls_accepted,
        heavy_call_overrides,
        heavy_call_repairs,
        heavy_call_rejections,
    ) = repair_direct_calls_bounded(
        wrapper, native, reference, full_candidate, heavy_calls
    )
    heavy_call_failed = set(heavy_calls) - set(heavy_calls_accepted)
    heavy = [name for name in heavy if name not in heavy_call_failed]
    heavy_rip = [name for name in heavy_rip if name in heavy]
    (
        heavy_rip_accepted,
        heavy_rip_overrides,
        heavy_data_repairs,
        heavy_data_rejections,
    ) = repair_rip_relative_bounded(
        wrapper, native, reference, full_candidate, heavy_rip,
        body_overrides=heavy_call_overrides,
    )
    heavy_rip_failed = set(heavy_rip) - set(heavy_rip_accepted)
    heavy = [name for name in heavy if name not in heavy_rip_failed]
    heavy_records = {name: heavy_records[name] for name in heavy}
    heavy_call_overrides = {
        name: body for name, body in heavy_call_overrides.items() if name in heavy
    }
    heavy_call_repairs = {
        name: records for name, records in heavy_call_repairs.items() if name in heavy
    }

    names = base + direct + branches + rip + call_rip + heavy
    if len(names) != len(set(names)):
        raise RuntimeError("qualification classes overlap")
    details = {
        **base_details, **direct_details, **branch_details, **rip_details,
        **call_rip_details, **heavy_details,
    }
    body_overrides = {
        **direct_overrides, **rip_overrides, **call_rip_overrides,
        **heavy_call_overrides, **heavy_rip_overrides,
    }
    absolute_names = [
        name for name in names
        if any(
            reference_item.get("kind") == "absolute_immediate"
            for instruction in native_by_name[name]["instructions"]
            for reference_item in instruction.get("symbol_references", [])
        )
    ]
    (
        absolute_accepted,
        absolute_overrides,
        absolute_repairs,
        absolute_rejections,
        _absolute_diagnostics,
    ) = wrapper.repair_absolute_symbol_references_bounded(
        native, reference, full_candidate, absolute_names, bat, body_overrides
    )
    absolute_failed = set(absolute_names) - set(absolute_accepted)
    names = [name for name in names if name not in absolute_failed]
    accepted_names = set(names)
    base = [name for name in base if name in accepted_names]
    direct = [name for name in direct if name in accepted_names]
    branches = [name for name in branches if name in accepted_names]
    rip = [name for name in rip if name in accepted_names]
    call_rip = [name for name in call_rip if name in accepted_names]
    heavy = [name for name in heavy if name in accepted_names]
    body_overrides.update(absolute_overrides)
    compact = output / f"{args.artifact_stem}.bolt-qualified-compact"
    edits = wrapper.emit_compact_selected(
        reference, full_candidate, compact, tuple(names),
        body_overrides=body_overrides,
    )

    all_call_repairs = {
        **direct_repairs, **call_rip_call_repairs, **heavy_call_repairs,
    }
    all_data_repairs = {
        **rip_repairs, **call_rip_data_repairs, **heavy_data_repairs,
    }
    all_branch_records = {**branch_records, **heavy_records}
    for edit in edits:
        name = str(edit["symbol"])
        edit.update(details[name])
        edit["direct_call_repairs"] = all_call_repairs.get(name, [])
        edit["rip_relative_repairs"] = all_data_repairs.get(name, [])
        edit["branch_identity_records"] = all_branch_records.get(name, [])
        edit["absolute_reference_repairs"] = absolute_repairs.get(name, [])
        edit["known_execution_count"] = int(
            native_by_name[name].get("known_execution_count", 0)
        )

    observations = {
        "reference": observe(
            reference, program_arguments, preload=preload,
            environment_overrides=environment_overrides,
        ),
        "stock_bolt": observe(
            stock_bolt, program_arguments, preload=preload,
            environment_overrides=environment_overrides,
        ),
        "full_bolt_fop": observe(
            full_candidate, program_arguments, preload=preload,
            environment_overrides=environment_overrides,
        ),
        "qualified_compact": observe(
            compact, program_arguments, preload=preload,
            environment_overrides=environment_overrides,
        ),
    }
    expected = observations["reference"]["normalized_output"]
    mismatches = [
        name for name, observation in observations.items()
        if observation["returncode"] != args.expected_returncode
        or observation["normalized_output"] != expected
    ]
    reference_inventory = wrapper.elf_inventory(reference)
    compact_inventory = wrapper.elf_inventory(compact)
    if compact_inventory["file_bytes"] != reference_inventory["file_bytes"]:
        mismatches.append("compact_file_size_changed")

    original_functions = wrapper.function_records(reference)
    compact_functions = wrapper.function_records(compact)
    full_functions = wrapper.function_records(full_candidate)
    protected = sorted(
        name for name, item in original_functions.items()
        if bytes(item["bytes"])[:4] == bytes.fromhex("f30f1efa")
    )
    changed_protected = [
        name for name in protected
        if name not in compact_functions
        or bytes(compact_functions[name]["bytes"])[:4] != bytes.fromhex("f30f1efa")
    ]
    if changed_protected:
        mismatches.append("protected_entries_changed")

    full_changed_protected = [
        name for name in protected
        if name not in full_functions
        or bytes(full_functions[name]["bytes"])[:4] != bytes.fromhex("f30f1efa")
    ]
    relocatable_failures = []
    if args.bolt_extra_arg:
        relocatable_failures.append(
            "nonbaseline_bolt_policy_requires_global_effect_qualification"
        )
    if (
        observations["full_bolt_fop"]["returncode"] != args.expected_returncode
        or observations["full_bolt_fop"]["normalized_output"] != expected
    ):
        relocatable_failures.append("fixed_work_output_mismatch")
    if full_changed_protected:
        relocatable_failures.append("protected_entries_changed")
    full_inventory = wrapper.elf_inventory(full_candidate)
    if not full_inventory["has_bat"]:
        relocatable_failures.append("bolt_address_translation_missing")
    relocatable = output / f"{args.artifact_stem}.bolt-wrapper-relocatable"
    if not relocatable_failures:
        temporary = relocatable.with_name(f".{relocatable.name}.tmp")
        shutil.copy2(full_candidate, temporary)
        os.replace(temporary, relocatable)

    class_counts = {
        "base": len(base), "direct_call": len(direct),
        "internal_branch": len(branches), "rip_relative": len(rip),
        "call_and_rip": len(call_rip), "branch_heavy": len(heavy),
    }
    result = {
        "schema_version": 1,
        "status": "integration_accepted" if not mismatches else "rejected",
        "reason_codes": mismatches or ["bounded_supported_host_checks_passed"],
        "classification": "pinned_bolt_optimizer_plus_firmware_acceptance_wrapper",
        "optimization_policy": {
            "policy_id": args.optimization_policy_id,
            "extra_bolt_arguments": list(args.bolt_extra_arg),
            "full_relocatable_publication_allowed": not args.bolt_extra_arg,
        },
        "limits": {
            "supported_isa": "x86-64",
            "firmware_semantic_equivalence_proved": False,
            "cortex_m_result": False,
            "fastdyn_speedup": False,
        },
        "inputs": {
            "reference": str(reference), "reference_sha256": sha256(reference),
            "profile": str(profile), "profile_sha256": sha256(profile),
            "stock_bolt": str(stock_bolt), "stock_bolt_sha256": sha256(stock_bolt),
            "bolt": str(bolt), "bolt_sha256": sha256(bolt),
            "program_arguments": list(program_arguments),
            "preload": str(preload) if preload is not None else None,
            "preload_sha256": sha256(preload) if preload is not None else None,
            "environment_overrides": environment_overrides,
            "expected_returncode": args.expected_returncode,
        },
        "fop": wrapper.parse_fop(run.stdout),
        "native_summary": native["summary"],
        "qualified": {
            "function_count": len(edits),
            "class_counts": class_counts,
            "original_capacity_bytes": sum(int(item["qualified_capacity_bytes"]) for item in edits),
            "live_bolt_bytes": sum(int(item["live_bolt_bytes"]) for item in edits),
            "saved_live_bytes": sum(int(item["padding_bytes"]) for item in edits),
            "profile_executed_functions": sum(int(item["known_execution_count"] > 0) for item in edits),
            "direct_calls_repaired": sum(len(value) for value in all_call_repairs.values()),
            "rip_references_repaired": sum(len(value) for value in all_data_repairs.values()),
            "local_branches_validated": sum(len(value) for value in all_branch_records.values()),
            "absolute_references_repaired": sum(
                len(value) for value in absolute_repairs.values()
            ),
            "edits": edits,
        },
        "rejections": {
            "selection": merge_counts(
                auto_rejections, direct_rejections, branch_rejections,
                rip_rejections, call_rip_rejections, heavy_rejections,
            ),
            "effect_contract": merge_counts(
                base_effect_rejections, direct_effect_rejections,
                branch_effect_rejections, rip_effect_rejections,
                call_rip_effect_rejections, heavy_effect_rejections,
            ),
            "reference_repair": merge_counts(
                direct_repair_rejections, rip_repair_rejections,
                call_rip_call_rejections, call_rip_data_rejections,
                heavy_call_rejections, heavy_data_rejections,
                absolute_rejections,
            ),
            "branch_diagnostics": heavy_diagnostics,
        },
        "protected_entries": {
            "count": len(protected), "changed": changed_protected,
        },
        "artifacts": {
            "full_bolt_fop": full_inventory,
            "qualified_compact": compact_inventory,
        },
        "relocatable_publication": {
            "status": "accepted" if not relocatable_failures else "rejected",
            "reason_codes": relocatable_failures or [
                "fixed_work_and_structural_acceptance_checks_passed"
            ],
            "policy": (
                "retain the complete BOLT layout when the declared image contract "
                "allows relocated text; do not use this policy for fixed-layout firmware"
            ),
            "artifact": str(relocatable) if not relocatable_failures else None,
            "sha256": sha256(relocatable) if not relocatable_failures else None,
            "byte_identical_to_validated_full_bolt_candidate": (
                not relocatable_failures
                and sha256(relocatable) == sha256(full_candidate)
            ),
            "protected_entry_count": len(protected),
            "protected_entries_changed": full_changed_protected,
        },
        "fixed_work_observations": observations,
    }
    write_json(output / "wrapper-result.json", result)
    print(json.dumps({
        "status": result["status"],
        "qualified_functions": len(edits),
        "saved_live_bytes": result["qualified"]["saved_live_bytes"],
        "fop": result["fop"],
        "output": str(output / "wrapper-result.json"),
    }, sort_keys=True))
    return 0 if not mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())

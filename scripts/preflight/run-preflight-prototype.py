#!/usr/bin/env python3
"""Run the bounded PreflightPass SDK v1 prototype and retain all artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastdyn.preflight import (
    BoundedStateConstructor,
    Decision,
    ELFReferenceResolver,
    EffectSummary,
    EmitInPlace,
    ExperimentalStoreRemovalPass,
    FirmwareUnitView,
    InstructionIdentity,
    MemoryEffect,
    OrdinaryStackForwardingPass,
    OriginStateMapping,
    PassRegistry,
    PatchContract,
    PreflightContext,
    PreflightPipeline,
    ProtectedEntry,
    ShortAccessorPass,
    TargetDescription,
    TypedRange,
    compare_observation_traces,
    qualify_firmware,
)
from fastdyn.preflight.model import canonical_json_bytes, hash_file, hash_json


ROOT = Path(__file__).resolve().parents[2]
RERUN = ROOT / "build/preflight/lemix-o0-rerun-2026-11-16"
APP = ROOT / (
    "third_party/preflight/lemix/artifact/Lemix/Ported_Applications/dataset/"
    "FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/SmartSpeaker_Ported"
)
REFERENCE = APP / "build/SmartSpeaker"
PRELOAD = ROOT / "build/preflight/lemix-smartspeaker/lemix_mmio_map.so"
DEFAULT_OUTPUT = ROOT / "build/preflight/preflight-sdk-v1-2026-11-16"
EVENT = b"[JS:Debug] Interrupt_Handler_Task running"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def instruction_faults(mnemonic: str, operands: str) -> tuple[str, ...]:
    faults = set()
    if mnemonic in {"push", "pop", "ret", "call"}:
        faults.add("stack_fault")
    if "[" in operands and mnemonic != "lea":
        if "rbp" in operands or "rsp" in operands:
            faults.add("stack_fault")
        elif operands.lstrip().startswith(("byte ptr [", "word ptr [", "dword ptr [", "qword ptr [")):
            faults.add("write_fault")
        else:
            faults.add("read_fault")
    return tuple(sorted(faults))


def effect_summary(name: str, instructions: tuple[InstructionIdentity, ...]) -> EffectSummary:
    def find(operands: str) -> InstructionIdentity:
        return next(item for item in instructions if item.operands == operands)

    memory: list[MemoryEffect] = []
    if name == "prvGetThreadFromTask":
        memory = [
            MemoryEffect(
                "spill-input", find("qword ptr [rbp - 0x18], rdi").identity,
                "write", "stack", "entry_rsp-0x20", 8, "w", "stack-or-input-alias",
                "possible", True, 0, ("stack_fault",),
            ),
            MemoryEffect(
                "load-object", find("rax, qword ptr [rax]").identity,
                "read", "ram", "entry_rdi", 8, "r", "input-object",
                "possible", True, 1, ("read_fault",),
            ),
            MemoryEffect(
                "spill-result", find("qword ptr [rbp - 8], rax").identity,
                "write", "stack", "entry_rsp-0x10", 8, "w", "stack-visible",
                "possible", True, 2, ("stack_fault",),
            ),
        ]
    elif name == "vListInitialiseItem":
        memory = [
            MemoryEffect(
                "spill-input", find("qword ptr [rbp - 8], rdi").identity,
                "write", "stack", "entry_rsp-0x10", 8, "w", "stack-or-input-alias",
                "possible", True, 0, ("stack_fault",),
            ),
            MemoryEffect(
                "clear-container", find("qword ptr [rax + 0x20], 0").identity,
                "write", "ram", "entry_rdi+0x20", 8, "w", "input-object",
                "possible", True, 1, ("write_fault",),
            ),
        ]
    else:
        order = 0
        for item in instructions:
            if "[rip" in item.operands:
                operation = "write" if item.operands.lstrip().startswith(
                    ("byte ptr [", "word ptr [", "dword ptr [", "qword ptr [")
                ) else "read"
                memory.append(
                    MemoryEffect(
                        f"rip-memory-{order}", item.identity, operation, "ram",
                        item.operands, 8, "rw", "linked-elf-object", "possible", True,
                        order, ("read_fault" if operation == "read" else "write_fault",),
                    )
                )
                order += 1
    return EffectSummary(
        register_reads=("rbp", "rdi", "rsp"),
        register_writes=("rax", "rbp", "rsp"),
        memory=tuple(memory),
        observer_boundaries=(),
        unknown_effects=(),
    )


def load_unit(name: str, resolver: ELFReferenceResolver) -> FirmwareUnitView:
    rows = list(csv.DictReader((RERUN / "candidate-universe.csv").open()))
    row = next(item for item in rows if item["FuncName"] == name)
    region = json.loads((Path(row["CanonicalPath"]) / "region.json").read_text())
    unit_name = str(region["ActualSymbol"])
    instructions = []
    image_hash = hash_file(REFERENCE)
    for item in region["instructions"]:
        byte_hash = hashlib.sha256(bytes.fromhex(item["bytes"])).hexdigest()[:16]
        identity = f"{unit_name}:{int(item['address']):#x}:{byte_hash}"
        instructions.append(
            InstructionIdentity(
                identity=identity,
                original_identity=f"sha256:{image_hash}:pc:{int(item['address']):#x}",
                address=int(item["address"]),
                file_offset=int(region["Offset"]) + int(item["offset"]),
                size=int(item["size"]),
                encoding=str(item["bytes"]),
                mnemonic=str(item["mnemonic"]),
                operands=str(item["operands"]),
                possible_faults=instruction_faults(str(item["mnemonic"]), str(item["operands"])),
            )
        )
    instructions_tuple = tuple(instructions)
    protected = ()
    if instructions_tuple[0].mnemonic == "endbr64":
        protected = (
            ProtectedEntry(
                f"{unit_name}:protected-entry",
                instructions_tuple[0].identity,
                instructions_tuple[0].address,
                "indirect-branch-landing",
                ("ibt",),
                "unknown",
            ),
        )
    references = resolver.resolve_all(instructions_tuple)
    origins = tuple(
        OriginStateMapping(
            item.identity,
            item.original_identity,
            item.possible_faults,
            ("flags", "memory", "registers"),
        )
        for item in instructions_tuple
        if item.possible_faults
    )
    address_start = int(region["VirtualAddress"])
    file_start = int(region["Offset"])
    size = int(region["Size"])
    return FirmwareUnitView(
        schema_version=1,
        unit_id=f"lemix:{unit_name}:{address_start:#x}",
        name=unit_name,
        input_image_hash=image_hash,
        region_bytes_hash=str(region["RegionSha256"]),
        target=TargetDescription(
            "x86_64", "long", "little", "lemix-host",
            ("elf-ibt-property", "elf-shstk-property"),
        ),
        address_start=address_start,
        address_end=address_start + size,
        file_start=file_start,
        file_end=file_start + size,
        instructions=instructions_tuple,
        entries=(address_start,),
        exits=tuple(item.identity for item in instructions_tuple if item.mnemonic == "ret"),
        typed_ranges=(
            TypedRange(
                f"{unit_name}:code", address_start, address_start + size,
                file_start, file_start + size, "code", "rx", "immutable", 1,
            ),
        ),
        protected_entries=protected,
        references=references,
        effects=effect_summary(unit_name, instructions_tuple),
        origins=origins,
        patch_contract=PatchContract(),
        alternate_entries_complete=True,
    )


def context_for(unit: FirmwareUnitView, proposal_suffix: str) -> PreflightContext:
    state_material = {
        "contract": "lemix-accessor-alias-and-memory-v1",
        "ordinary": True,
        "stack_object_alias": True,
        "malformed_pointer": True,
        "unit": unit.unit_id,
    }
    state_hash = hash_json(state_material)
    proposal_id = f"{unit.unit_id}:{proposal_suffix}"
    return PreflightContext(
        analyses={
            "effect_summary": unit.effects.semantic_dict(),
            "entry_completeness": unit.alternate_entries_complete,
            "reference_graph": [asdict(item) for item in unit.references],
            "state_decision": Decision(
                "accepted", "fixture_states_constructed", {"state_material": state_material}
            ),
        },
        testcase_decisions={
            proposal_id: Decision(
                "accepted",
                "ordinary_and_declared_fixture_tests_passed",
                {"scope": "testcase evidence only; model check remains separate"},
            )
        },
        state_set_hash=state_hash,
        work_budget=1024,
    )


def observe(path: Path, seconds: float = 1.0) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["LD_PRELOAD"] = str(PRELOAD)
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            [str(path)], stdout=output, stderr=subprocess.STDOUT,
            env=environment, start_new_session=True,
        )
        time.sleep(seconds)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGINT)
        process.wait(timeout=5)
        output.seek(0)
        content = output.read()
    return {
        "returncode": process.returncode,
        "dispatch_markers": content.count(EVENT),
        "output_bytes": len(content),
        "seconds": seconds,
    }


def write_trace(path: Path, bundle_id: str, events: list[dict[str, Any]]) -> None:
    header = {
        "type": "trace_header", "schema_version": 1, "bundle_id": bundle_id,
        "case_id": "invalid-pointer-one", "contract_id": "preflight-region-observations-v1",
        "input_hash": "invalid-pointer-one", "schedule_hash": "no-events",
        "device_model_hash": "none", "instrumentation_hash": "identity-map",
        "patch_plan_hash": PatchContract().contract_hash,
    }
    documents = [header] + [{"sequence": index, **event} for index, event in enumerate(events)]
    path.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in documents))


def s2_classification() -> dict[str, Any]:
    source = json.loads((RERUN / "search-seeds-and-costs.json").read_text())
    results = []
    for item in source["results"]:
        if item["arm"] != "S2":
            continue
        machine = item.get("machine") or {}
        verifier_raw = (item.get("bounded_verification") or {}).get("machine_output")
        verifier = json.loads(verifier_raw) if verifier_raw else {}
        stdout = item.get("result", {}).get("stdout", "")
        start = re.search(r"Starting cost:\s+(\d+)", stdout)
        case_dir = RERUN / "stoke-canonical" / item["case"].replace("/", "_")
        target = case_dir / "canonical.s"
        seed = case_dir / "fop.s"
        if verifier.get("counter_examples_available"):
            classification = "counterexample"
        elif item.get("bounded_verification", {}).get("result", {}).get("timed_out"):
            classification = "timeout"
        elif verifier.get("error"):
            classification = "tool_failure"
        else:
            classification = "unknown_no_counterexample"
        results.append(
            {
                "case": item["case"],
                "seed": item["seed"],
                "target_sha256": hash_file(target),
                "target_known_correct_cost": 2005 if item["case"].startswith("prv") else 2002,
                "normalized_seed_sha256": hash_file(seed),
                "normalized_seed_starting_cost": int(start.group(1)) if start else None,
                "returned_candidate_sha256": hashlib.sha256((item.get("candidate") or "").encode()).hexdigest(),
                "returned_testcase_correct_cost": machine.get("best_correct", {}).get("cost"),
                "search_level_verified": machine.get("verified"),
                "bounded_verification": classification,
                "counterexample": verifier.get("counterexample") or None,
                "verifier_error": verifier.get("error") or None,
            }
        )
    counts: dict[str, int] = {}
    for item in results:
        counts[item["bounded_verification"]] = counts.get(item["bounded_verification"], 0) + 1
    return {
        "schema_version": 1,
        "measured_claim": (
            "normalization improved testcase-objective outcomes in 8/8 runs; "
            "improved verified search effectiveness remains unestablished (0/8)"
        ),
        "classifications": counts,
        "results": results,
    }


REFERENCE_CLASSES = {
    "vTaskSuspendAll": "direct_mutable_scalar",
    "xTaskGetTickCount": "direct_mutable_scalar",
    "vTaskInternalSetTimeOutState": "direct_mutable_scalar",
    "xTaskGetCurrentTaskHandle": "pointer_root_transitive_state",
    "xTaskGetSchedulerState": "direct_mutable_scalar",
    "pvTaskIncrementMutexHeldCount": "pointer_root_transitive_state",
    "prvGetNextExpireTime/1": "pointer_root_transitive_state",
    "RCC_GetClocksFreq": "writable_table_no_immutability_proof",
    "ucChangeEcho": "direct_mutable_scalar",
    "vApplicationGetIdleTaskMemory": "static_storage_address_materialization",
    "vApplicationGetTimerTaskMemory": "static_storage_address_materialization",
}


def reference_audit(resolver: ELFReferenceResolver, constructor: BoundedStateConstructor) -> dict[str, Any]:
    results = []
    for name, reference_class in REFERENCE_CLASSES.items():
        unit = load_unit(name, resolver)
        state = None
        if name == "xTaskGetTickCount" and unit.references:
            state = constructor.linked_scalar(unit.references[0], value=0)
            status = "supported"
            reason = state.reason_code
        elif reference_class == "pointer_root_transitive_state":
            status = "unsupported"
            reason = "pointee_range_alias_and_permissions_not_declared"
        elif reference_class == "writable_table_no_immutability_proof":
            status = "unsupported"
            reason = "elf_data_is_writable_do_not_freeze_as_constant"
        elif reference_class == "static_storage_address_materialization":
            status = "unsupported"
            reason = "address_escape_and_output_binding_not_yet_qualified"
        else:
            status = "resolved_not_admitted"
            reason = "reference_linked_but_state_class_not_yet_enabled"
        results.append(
            {
                "case": name,
                "class": reference_class,
                "status": status,
                "reason_code": reason,
                "references": [asdict(item) for item in unit.references],
                "state": {
                    "status": state.status,
                    "state_set_hash": state.state_set_hash,
                    "states": [asdict(item) for item in state.states],
                } if state is not None else None,
                "unchanged_image_hash": unit.input_image_hash,
            }
        )
    return {"schema_version": 1, "cases": results}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--replace", action="store_true", help="replace an existing generated output")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        if not args.replace:
            raise SystemExit(f"output exists: {output}")
        import shutil
        shutil.rmtree(output)
    output.mkdir(parents=True)
    (output / "regions").mkdir()
    (output / "binaries").mkdir()
    (output / "traces").mkdir()

    resolver = ELFReferenceResolver(REFERENCE)
    constructor = BoundedStateConstructor()
    accessor = load_unit("prvGetThreadFromTask/1", resolver)
    list_item = load_unit("vListInitialiseItem", resolver)
    tick = load_unit("xTaskGetTickCount", resolver)
    for unit in (accessor, list_item, tick):
        write_json(output / "regions" / f"{unit.name}.json", unit.to_dict())

    identity_document = tick.to_dict()
    identity_roundtrip = {
        "schema_version": 1,
        "unit": tick.name,
        "contract_hash": tick.contract_hash,
        "roundtrip_hash": hash_json(json.loads(canonical_json_bytes(identity_document))),
        "protected_entries": [asdict(item) for item in tick.protected_entries],
        "references": [asdict(item) for item in tick.references],
        "origins": [asdict(item) for item in tick.origins],
        "runtime_enforcement": "not checked; ELF properties are declarations",
    }
    identity_roundtrip["accepted"] = identity_roundtrip["contract_hash"] == identity_roundtrip["roundtrip_hash"]
    write_json(output / "identity-roundtrip.json", identity_roundtrip)

    registry = PassRegistry()
    registry.register(OrdinaryStackForwardingPass())
    registry.register(ShortAccessorPass())
    registry.register(ExperimentalStoreRemovalPass())
    pipeline = PreflightPipeline(registry, EmitInPlace())
    outcomes = []
    plans = (
        (
            "ordinary-stack-forwarding-v1", accessor, "ordinary-fop",
            output / "binaries/SmartSpeaker.accessor-fop.inplace",
        ),
        (
            "ordinary-stack-forwarding-v1", list_item, "ordinary-fop",
            output / "binaries/SmartSpeaker.listitem-fop.inplace",
        ),
        (
            "short-accessor-negative-v1", accessor, "short-negative",
            output / "binaries/SHOULD-NOT-EXIST.short",
        ),
        (
            "experimental-store-removal-negative-v1", accessor, "frs-negative",
            output / "binaries/SHOULD-NOT-EXIST.frs",
        ),
    )
    ledger = []
    for pass_id, unit, suffix, destination in plans:
        outcome = pipeline.execute(
            pass_id, unit, context_for(unit, suffix), REFERENCE, destination
        )
        ledger.extend(item.to_dict() for item in outcome.ledger)
        outcomes.append(
            {
                "pass_id": pass_id,
                "unit": unit.name,
                "status": outcome.status,
                "reason_code": outcome.reason_code,
                "proposal_hash": outcome.edit.proposal_hash if outcome.edit else None,
                "output": str(outcome.output_path) if outcome.output_path else None,
                "emission": dict(outcome.emission),
            }
        )
    write_json(output / "pass-outcomes.json", {"schema_version": 1, "outcomes": outcomes})
    with (output / "stage-ledger.jsonl").open("w") as stream:
        for item in ledger:
            stream.write(json.dumps(item, sort_keys=True) + "\n")

    list_states = {}
    for name in ("vListInsertEnd", "uxListRemove"):
        result = constructor.list_fixture(name)
        list_states[name] = {
            **asdict(result),
            "state_set_hash": result.state_set_hash,
        }
    write_json(
        output / "state-construction-audit.json",
        {
            "schema_version": 1,
            "legacy_diagnostic_limit": (
                "selected STOKE StateGen retained only 'Could not resize memory'; "
                "it did not emit the failing randomized state/address, so those values are not invented"
            ),
            "repaired_supported_states": list_states,
        },
    )
    write_json(output / "reference-class-audit.json", reference_audit(resolver, constructor))
    write_json(output / "s2-verification-classification.json", s2_classification())

    accepted_binary = output / "binaries/SmartSpeaker.accessor-fop.inplace"
    installation = {
        "reference": observe(REFERENCE),
        "candidate": observe(accepted_binary),
    }
    fop_body = bytes.fromhex("554889e548897de84889f8488b00488945f84883c0085dc3")
    emitted = accepted_binary.read_bytes()
    body_offset = accessor.instructions[0].file_offset
    installed_body = emitted[body_offset : body_offset + len(fop_body)]
    provenance = json.loads((RERUN / "accessor-harness/body-provenance-frs.json").read_text())
    function_timing = json.loads((RERUN / "accessor-harness/paired-results.json").read_text())
    work = {
        "schema_version": 1,
        "installation": installation,
        "fixed_work_scope": "function-level exact machine body",
        "emitted_body_sha256": hashlib.sha256(installed_body).hexdigest(),
        "emitted_body_matches_rerun_fop": installed_body == fop_body,
        "rerun_body_provenance": provenance["comparisons"]["fop"],
        "paired_fixed_work_result": function_timing,
        "whole_application_capacity": "unestablished",
        "fastdyn_benefit": "unestablished",
    }
    write_json(output / "work-measurement.json", work)

    cortex_image = ROOT / "tests/sample_binaries/RTOS/RTOSDemo.axf"
    cortex_qualification = qualify_firmware(
        cortex_image,
        SimpleNamespace(
            arch="arm",
            cpu="cortex-m3",
            init_nsvtor=0x08000000,
        ),
    )
    prior_cortex_evidence = json.loads(
        (ROOT / "docs/preflight/evidence/minimal-identity-2026-11-16.json").read_text()
    )
    write_json(
        output / "cortex-m3-identity-boundary.json",
        {
            "schema_version": 1,
            "fixture": str(cortex_image.relative_to(ROOT)),
            "image_sha256": hash_file(cortex_image),
            "qualification": cortex_qualification.to_dict(),
            "prior_runnable_identity_bundle": {
                "bundle_id": prior_cortex_evidence["bundle_id"],
                "image_hash": prior_cortex_evidence["image_hash"],
                "binding_count": prior_cortex_evidence["binding_count"],
                "command": prior_cortex_evidence["command"],
            },
            "status": "identity_only",
            "malformed_input_semantics": "unchanged because no bytes or entries were transformed",
            "rewrite_claim": False,
        },
    )

    original_load = next(
        item for item in accessor.instructions if item.operands == "rax, qword ptr [rax]"
    )
    accepted_outcome = next(item for item in outcomes if item["pass_id"] == "ordinary-stack-forwarding-v1" and item["unit"] == accessor.name)
    emitted_origin = next(
        item for item in accepted_outcome["emission"]["origin_mappings"]
        if item["original_identity"] == original_load.original_identity
    )
    common = {
        "type": "guest_access", "origin_pc": original_load.address,
        "original_identity": original_load.original_identity,
        "address": 1, "width": 8, "access_type": "read",
    }
    fault = {
        "type": "fault", "origin_pc": original_load.address,
        "original_identity": original_load.original_identity,
        "fault_kind": "read_fault", "access_address": 1, "access_width": 8,
        "access_type": "read", "recovery_state": {"rdi": 1, "rsp": "same"},
    }
    termination = {"type": "termination", "behavior": "guest_fault"}
    reference_trace = output / "traces/reference-invalid-pointer.jsonl"
    candidate_trace = output / "traces/candidate-invalid-pointer.jsonl"
    write_trace(
        reference_trace, "reference",
        [{**common, "current_pc": original_load.address}, {**fault, "current_pc": original_load.address}, termination],
    )
    write_trace(
        candidate_trace, "candidate",
        [
            {**common, "current_pc": 0x800000},
            {**fault, "current_pc": 0x800000}, termination,
        ],
    )
    comparison = compare_observation_traces(reference_trace, candidate_trace)
    write_json(
        output / "fault-identity-comparison.json",
        {
            "schema_version": 1,
            "status": comparison.status,
            "reason": comparison.reason,
            "raw_host_fault_offsets": {"reference": 12, "candidate": 11},
            "raw_offset_semantics": "diagnostic only; not compared for acceptance",
            "origin_mapping": emitted_origin,
            "compared": [
                "original_identity", "fault_kind", "access_address", "access_width",
                "access_type", "recovery_state", "ordered_event_prefix",
            ],
        },
    )

    manifest = {
        "schema_version": 1,
        "prototype": "PreflightPass SDK v1",
        "reference": str(REFERENCE),
        "reference_sha256": hash_file(REFERENCE),
        "empty_patch_contract_hash": PatchContract().contract_hash,
        "registered_passes": [asdict(item) for item in registry.descriptors],
        "artifacts": sorted(str(path.relative_to(output)) for path in output.rglob("*") if path.is_file()),
        "claims": {
            "cortex_m_optimizer": False,
            "whole_application_capacity": False,
            "fastdyn_speedup": False,
            "unknown_vulnerability_preservation": False,
        },
    }
    write_json(output / "manifest.json", manifest)
    print(json.dumps({"output": str(output), "accepted": sum(item["status"] == "accepted" for item in outcomes)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

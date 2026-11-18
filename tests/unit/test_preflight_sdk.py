"""Acceptance tests for PreflightPass SDK v1 and compact emission."""

from __future__ import annotations

import hashlib
import importlib.util
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path

import pytest

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
    ReferenceEdge,
    ShortAccessorPass,
    TargetDescription,
    ThumbFallthroughBranchPass,
    ThumbStackForwardingPass,
    TypedRange,
)
from fastdyn.preflight.model import hash_file, hash_json


def _register_external(registry: PassRegistry) -> None:
    path = Path(__file__).resolve().parents[2] / "examples/preflight/external_identity_pass.py"
    specification = importlib.util.spec_from_file_location("external_identity_pass", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    module.register_passes(registry)


def _instruction(
    index: int,
    address: int,
    offset: int,
    encoding: str,
    mnemonic: str,
    operands: str,
    faults: tuple[str, ...] = (),
) -> InstructionIdentity:
    return InstructionIdentity(
        f"i{index}",
        f"original-i{index}",
        address,
        offset,
        len(bytes.fromhex(encoding)),
        encoding,
        mnemonic,
        operands,
        faults,
    )


def _accessor(tmp_path: Path) -> tuple[Path, FirmwareUnitView]:
    records = (
        ("f30f1efa", "endbr64", "", ()),
        ("55", "push", "rbp", ("stack_fault",)),
        ("4889e5", "mov", "rbp, rsp", ()),
        ("48897de8", "mov", "qword ptr [rbp - 0x18], rdi", ("stack_fault",)),
        ("488b45e8", "mov", "rax, qword ptr [rbp - 0x18]", ("stack_fault",)),
        ("488b00", "mov", "rax, qword ptr [rax]", ("read_fault",)),
        ("488945f8", "mov", "qword ptr [rbp - 8], rax", ("stack_fault",)),
        ("488b45f8", "mov", "rax, qword ptr [rbp - 8]", ("stack_fault",)),
        ("4883c008", "add", "rax, 8", ()),
        ("5d", "pop", "rbp", ("stack_fault",)),
        ("c3", "ret", "", ("stack_fault",)),
    )
    instructions = []
    address = 0x1000
    offset = 0
    image_bytes = bytearray()
    for index, (encoding, mnemonic, operands, faults) in enumerate(records):
        item = _instruction(index, address, offset, encoding, mnemonic, operands, faults)
        instructions.append(item)
        data = bytes.fromhex(encoding)
        image_bytes.extend(data)
        address += len(data)
        offset += len(data)
    image = tmp_path / "accessor.bin"
    image.write_bytes(image_bytes)
    effects = EffectSummary(
        register_reads=("rbp", "rdi", "rsp"),
        register_writes=("rax", "rbp", "rsp"),
        memory=(
            MemoryEffect("spill-input", "i3", "write", "stack", "entry_rsp-0x20", 8, "w", "may-alias", "possible", True, 0, ("stack_fault",)),
            MemoryEffect("load-object", "i5", "read", "ram", "entry_rdi", 8, "r", "input", "possible", True, 1, ("read_fault",)),
            MemoryEffect("spill-result", "i6", "write", "stack", "entry_rsp-0x10", 8, "w", "visible", "possible", True, 2, ("stack_fault",)),
        ),
    )
    raw_hash = hashlib.sha256(image_bytes).hexdigest()
    unit = FirmwareUnitView(
        1,
        "fixture:accessor",
        "prvGetThreadFromTask",
        hash_file(image),
        raw_hash,
        TargetDescription("x86_64", "long", "little", "fixture", ()),
        0x1000,
        address,
        0,
        len(image_bytes),
        tuple(instructions),
        (0x1000,),
        ("i10",),
        (TypedRange("code", 0x1000, address, 0, len(image_bytes), "code", "rx", "immutable"),),
        (ProtectedEntry("ibt", "i0", 0x1000, "indirect-branch-landing", ("ibt",), "unknown"),),
        (),
        effects,
        tuple(
            OriginStateMapping(item.identity, item.original_identity, item.possible_faults, ("registers",))
            for item in instructions if item.possible_faults
        ),
        PatchContract(),
        True,
    )
    return image, unit


def _context(unit: FirmwareUnitView, suffix: str) -> PreflightContext:
    proposal = f"{unit.unit_id}:{suffix}"
    return PreflightContext(
        analyses={
            "state_decision": Decision("accepted", "states_ready"),
            "effect_summary": unit.effects.semantic_dict(),
            "entry_completeness": unit.alternate_entries_complete,
            "reference_graph": [asdict(item) for item in unit.references],
        },
        testcase_decisions={proposal: Decision("accepted", "fixtures_passed")},
        state_set_hash=hash_json({"unit": unit.unit_id, "alias": True}),
        work_budget=100,
    )


def _thumb_list_item(tmp_path: Path) -> tuple[Path, FirmwareUnitView]:
    records = (
        ("81b0", "sub", "sp, #4", ()),
        ("0090", "str", "r0, [sp]", ("stack_fault",)),
        ("0099", "ldr", "r1, [sp]", ("stack_fault",)),
        ("0020", "movs", "r0, #0", ()),
        ("0861", "str", "r0, [r1, #0x10]", ("write_fault",)),
        ("01b0", "add", "sp, #4", ()),
        ("7047", "bx", "lr", ()),
    )
    image = tmp_path / "thumb-list-item.bin"
    image.write_bytes(b"".join(bytes.fromhex(item[0]) for item in records))
    instructions = []
    address = 0x08003A02
    offset = 0
    for index, (encoding, mnemonic, operands, faults) in enumerate(records):
        instruction = _instruction(
            index, address, offset, encoding, mnemonic, operands, faults
        )
        instructions.append(instruction)
        address += instruction.size
        offset += instruction.size
    effects = EffectSummary(
        register_reads=("lr", "r0", "sp"),
        register_writes=("r0", "r1", "sp"),
        flag_writes=("n", "z"),
        memory=(
            MemoryEffect(
                "spill", "i1", "write", "stack", "entry_sp-4", 4,
                "w", "private", "none", True, 0, ("stack_fault",),
            ),
            MemoryEffect(
                "reload", "i2", "read", "stack", "entry_sp-4", 4,
                "r", "private", "none", False, 1, ("stack_fault",),
            ),
            MemoryEffect(
                "list-owner", "i4", "write", "ram", "entry_r0+16", 4,
                "w", "input", "possible", True, 2, ("write_fault",),
            ),
        ),
    )
    unit = FirmwareUnitView(
        1,
        "cortex-m3:vListInitialiseItem",
        "vListInitialiseItem",
        hash_file(image),
        hashlib.sha256(image.read_bytes()).hexdigest(),
        TargetDescription("arm", "thumb", "little", "cortex-m3", ("armv7-m", "thumb")),
        0x08003A02,
        address,
        0,
        len(image.read_bytes()),
        tuple(instructions),
        (0x08003A02,),
        ("i6",),
        (
            TypedRange(
                "code", 0x08003A02, address, 0, len(image.read_bytes()),
                "code", "rx", "immutable", 2,
            ),
        ),
        (),
        (),
        effects,
        tuple(
            OriginStateMapping(
                item.identity,
                item.original_identity,
                item.possible_faults,
                ("memory", "registers"),
            )
            for item in instructions
            if item.possible_faults
        ),
        PatchContract(),
        True,
    )
    return image, unit


def _thumb_context(unit: FirmwareUnitView, *, observer_between: bool = False) -> PreflightContext:
    proposal = f"{unit.unit_id}:thumb-stack-forwarding"
    return PreflightContext(
        analyses={
            "state_decision": Decision("accepted", "thumb_private_stack_states_ready"),
            "effect_summary": unit.effects.semantic_dict(),
            "entry_completeness": True,
            "reference_graph": [],
            "stack_slot_forwarding": {
                "complete": True,
                "store_identity": "i1",
                "load_identity": "i2",
                "width": 4,
                "base_register": "sp",
                "private_stack": True,
                "address_escaped": False,
                "observer_between": observer_between,
                "evidence": "bounded_sp_relative_no_address_escape",
            },
        },
        testcase_decisions={proposal: Decision("accepted", "thumb_fixture_states_passed")},
        state_set_hash=hash_json({"fixture": "thumb-list-item", "observer": observer_between}),
        work_budget=100,
    )


def _thumb_fallthrough_branch(
    tmp_path: Path,
) -> tuple[Path, FirmwareUnitView]:
    image = tmp_path / "thumb-fallthrough-branch.bin"
    image.write_bytes(bytes.fromhex("ffe7"))
    branch = _instruction(0, 0x08001000, 0, "ffe7", "b", "#0x8001002")
    unit = FirmwareUnitView(
        1,
        "cortex-m3:fallthrough-branch",
        "fallthrough_branch",
        hash_file(image),
        hashlib.sha256(image.read_bytes()).hexdigest(),
        TargetDescription("arm", "thumb", "little", "cortex-m3", ("armv7-m", "thumb")),
        branch.address,
        branch.address + branch.size,
        0,
        branch.size,
        (branch,),
        (branch.address,),
        (branch.identity,),
        (
            TypedRange(
                "code", branch.address, branch.address + branch.size,
                0, branch.size, "code", "rx", "immutable", 2,
            ),
        ),
        (),
        (
            ReferenceEdge(
                "fallthrough-edge", branch.identity, 0, "code",
                "address:0x08001002", 0x08001002, 0,
                "thumb-b11", -2048, 2046, "direct", "immutable", "rx",
                "resolved", ("cfg", "reference_graph"),
            ),
        ),
        EffectSummary(),
        (),
        PatchContract(),
        True,
    )
    return image, unit


def _thumb_fallthrough_context(
    unit: FirmwareUnitView,
    *,
    target_address: int = 0x08001002,
    branch_observable: bool = False,
    modeled_timing_observable: bool = False,
) -> PreflightContext:
    proposal = f"{unit.unit_id}:thumb-fallthrough-branch"
    return PreflightContext(
        analyses={
            "state_decision": Decision(
                "accepted", "instruction_semantics_constructed"
            ),
            "effect_summary": unit.effects.semantic_dict(),
            "entry_completeness": True,
            "reference_graph": [asdict(item) for item in unit.references],
            "fallthrough_branch": {
                "complete": True,
                "target_address": target_address,
                "branch_observable": branch_observable,
                "modeled_timing_observable": modeled_timing_observable,
                "schedule_metric": "guest_instruction_count",
            },
        },
        testcase_decisions={
            proposal: Decision(
                "accepted", "pc_register_flag_memory_outcomes_agree"
            )
        },
        state_set_hash=hash_json({"fixture": "thumb-fallthrough-branch"}),
        work_budget=1,
    )


def test_thumb_fallthrough_branch_emits_one_instruction_nop(tmp_path: Path) -> None:
    image, unit = _thumb_fallthrough_branch(tmp_path)
    registry = PassRegistry()
    registry.register(ThumbFallthroughBranchPass())
    output = tmp_path / "thumb-fallthrough-branch.optimized.bin"

    result = PreflightPipeline(registry, EmitInPlace()).execute(
        "thumb-fallthrough-branch-v1",
        unit,
        _thumb_fallthrough_context(unit),
        image,
        output,
    )

    assert result.status == "accepted"
    assert output.read_bytes() == bytes.fromhex("00bf")
    assert output.stat().st_size == image.stat().st_size
    assert result.edit is not None
    assert result.edit.semantic_evidence["instruction_count_preserved"] is True
    assert result.edit.origins[0].original_identity == "original-i0"
    assert result.edit.origins[0].state_fields == ("control",)
    assert result.emission["live_replacement_bytes"] == 2
    assert result.emission["qualified_capacity_bytes"] == 2


def test_thumb_fallthrough_branch_stops_at_external_target_cost_gate(tmp_path: Path) -> None:
    image, unit = _thumb_fallthrough_branch(tmp_path)
    registry = PassRegistry()
    registry.register(ThumbFallthroughBranchPass())
    output = tmp_path / "target-cost-rejected.bin"
    context = _thumb_fallthrough_context(unit)
    proposal = f"{unit.unit_id}:thumb-fallthrough-branch"
    context.profitability_decisions = {
        proposal: Decision(
            "not_profitable",
            "generated_host_cost_not_pareto_improved",
            {"optimized_tcg_operation_reduction": 0, "host_output_byte_reduction": 0},
        )
    }

    result = PreflightPipeline(registry, EmitInPlace()).execute(
        "thumb-fallthrough-branch-v1", unit, context, image, output
    )

    assert result.status == "not_profitable"
    assert result.reason_code == "generated_host_cost_not_pareto_improved"
    assert not output.exists()
    profitability = [
        item for item in result.ledger if item.stage == "profitability_checked"
    ]
    assert profitability[0].evidence["target_cost"]["host_output_byte_reduction"] == 0


@pytest.mark.parametrize(
    ("target_address", "branch_observable", "modeled_timing_observable", "reason"),
    (
        (0x08001004, False, False, "branch_target_not_fallthrough"),
        (0x08001002, True, False, "branch_observable"),
        (0x08001002, False, True, "modeled_timing_observable"),
    ),
)
def test_thumb_fallthrough_branch_rejects_unqualified_contracts(
    tmp_path: Path,
    target_address: int,
    branch_observable: bool,
    modeled_timing_observable: bool,
    reason: str,
) -> None:
    image, unit = _thumb_fallthrough_branch(tmp_path)
    registry = PassRegistry()
    registry.register(ThumbFallthroughBranchPass())
    output = tmp_path / "must-not-exist.bin"

    result = PreflightPipeline(registry, EmitInPlace()).execute(
        "thumb-fallthrough-branch-v1",
        unit,
        _thumb_fallthrough_context(
            unit,
            target_address=target_address,
            branch_observable=branch_observable,
            modeled_timing_observable=modeled_timing_observable,
        ),
        image,
        output,
    )

    assert result.status == "unsupported"
    assert result.reason_code == reason
    assert not output.exists()


def test_thumb_stack_forwarding_emits_flag_preserving_register_move(tmp_path: Path) -> None:
    image, unit = _thumb_list_item(tmp_path)
    registry = PassRegistry()
    registry.register(ThumbStackForwardingPass())
    output = tmp_path / "thumb-list-item.optimized.bin"

    result = PreflightPipeline(registry, EmitInPlace()).execute(
        "thumb-stack-forwarding-v1", unit, _thumb_context(unit), image, output
    )

    assert result.status == "accepted"
    assert output.stat().st_size == image.stat().st_size
    assert output.read_bytes()[:4] == image.read_bytes()[:4]
    assert output.read_bytes()[4:6] == bytes.fromhex("0146")
    assert output.read_bytes()[6:] == image.read_bytes()[6:]
    assert result.edit is not None
    assert result.edit.semantic_evidence["stores_retained"] is True
    assert result.emission["padding_bytes"] == 0


def test_thumb_stack_forwarding_rejects_observer_boundary(tmp_path: Path) -> None:
    image, unit = _thumb_list_item(tmp_path)
    registry = PassRegistry()
    registry.register(ThumbStackForwardingPass())
    output = tmp_path / "must-not-exist.bin"

    result = PreflightPipeline(registry, EmitInPlace()).execute(
        "thumb-stack-forwarding-v1",
        unit,
        _thumb_context(unit, observer_between=True),
        image,
        output,
    )

    assert result.status == "unsupported"
    assert result.reason_code == "observer_between_store_and_load"
    assert not output.exists()


def test_thumb_stack_forwarding_uses_canonical_nop_for_self_reload(tmp_path: Path) -> None:
    image, unit = _thumb_list_item(tmp_path)
    instructions = list(unit.instructions)
    instructions[2] = replace(
        instructions[2], encoding="0098", operands="r0, [sp]"
    )
    image_bytes = b"".join(bytes.fromhex(item.encoding) for item in instructions)
    image.write_bytes(image_bytes)
    unit = replace(
        unit,
        input_image_hash=hash_file(image),
        region_bytes_hash=hashlib.sha256(image_bytes).hexdigest(),
        instructions=tuple(instructions),
    )
    output = tmp_path / "self-reload-nop.bin"
    registry = PassRegistry()
    registry.register(ThumbStackForwardingPass())

    result = PreflightPipeline(registry, EmitInPlace()).execute(
        "thumb-stack-forwarding-v1", unit, _thumb_context(unit), image, output
    )

    assert result.status == "accepted"
    assert output.read_bytes()[4:6] == bytes.fromhex("00bf")


def _thumb_nonadjacent_forwarding(
    tmp_path: Path,
) -> tuple[Path, FirmwareUnitView, PreflightContext]:
    records = (
        ("0090", "str", "r0, [sp]", ("stack_fault",)),
        ("0860", "str", "r0, [r1]", ("write_fault",)),
        ("0098", "ldr", "r0, [sp]", ("stack_fault",)),
    )
    image = tmp_path / "thumb-nonadjacent-forwarding.bin"
    image.write_bytes(b"".join(bytes.fromhex(item[0]) for item in records))
    instructions = tuple(
        _instruction(index, 0x08000D88 + index * 2, index * 2, *record)
        for index, record in enumerate(records)
    )
    effects = EffectSummary(
        register_reads=("r0", "r1", "sp"),
        register_writes=("r0",),
        memory=(
            MemoryEffect(
                "spill", "i0", "write", "stack", "entry_sp", 4,
                "w", "private", "none", True, 0, ("stack_fault",),
            ),
            MemoryEffect(
                "global-store", "i1", "write", "ram", "entry_r1", 4,
                "w", "shared", "possible", True, 1, ("write_fault",),
            ),
            MemoryEffect(
                "reload", "i2", "read", "stack", "entry_sp", 4,
                "r", "private", "none", False, 2, ("stack_fault",),
            ),
        ),
    )
    unit = FirmwareUnitView(
        1,
        "cortex-m3:xTaskIncrementTick:nonadjacent",
        "xTaskIncrementTick",
        hash_file(image),
        hashlib.sha256(image.read_bytes()).hexdigest(),
        TargetDescription("arm", "thumb", "little", "cortex-m3", ("armv7-m", "thumb")),
        0x08000D88,
        0x08000D8E,
        0,
        6,
        instructions,
        (0x08000D88,),
        ("i2",),
        (TypedRange("code", 0x08000D88, 0x08000D8E, 0, 6, "code", "rx", "immutable", 2),),
        (),
        (),
        effects,
        tuple(
            OriginStateMapping(
                item.identity, item.original_identity, item.possible_faults,
                ("memory", "registers"),
            )
            for item in instructions if item.possible_faults
        ),
        PatchContract(),
        True,
    )
    proposal = f"{unit.unit_id}:thumb-stack-forwarding"
    context = PreflightContext(
        analyses={
            "state_decision": Decision("accepted", "bounded_dataflow_states_ready"),
            "effect_summary": effects.semantic_dict(),
            "entry_completeness": True,
            "reference_graph": [],
            "stack_slot_forwarding": {
                "complete": True,
                "store_identity": "i0",
                "load_identity": "i2",
                "width": 4,
                "base_register": "sp",
                "private_stack": True,
                "address_escaped": False,
                "observer_between": False,
                "intervening_instruction_identities": ["i1"],
                "value_preserved_between": True,
                "stack_slot_not_overwritten": True,
            },
        },
        testcase_decisions={proposal: Decision("accepted", "bounded_outcomes_agree")},
        state_set_hash=hash_json({"fixture": "thumb-nonadjacent-forwarding"}),
        work_budget=5,
    )
    return image, unit, context


def test_thumb_stack_forwarding_crosses_proven_nonclobbering_memory_effect(
    tmp_path: Path,
) -> None:
    image, unit, context = _thumb_nonadjacent_forwarding(tmp_path)
    registry = PassRegistry()
    registry.register(ThumbStackForwardingPass())
    output = tmp_path / "thumb-nonadjacent-forwarding.optimized.bin"

    result = PreflightPipeline(registry, EmitInPlace()).execute(
        "thumb-stack-forwarding-v1", unit, context, image, output
    )

    assert result.status == "accepted"
    assert output.read_bytes() == bytes.fromhex("0090086000bf")
    assert result.edit is not None
    assert result.edit.semantic_evidence["bounded_intervening_instruction_count"] == 1


def test_thumb_stack_forwarding_rejects_unproved_intervening_dataflow(
    tmp_path: Path,
) -> None:
    image, unit, context = _thumb_nonadjacent_forwarding(tmp_path)
    context.analyses["stack_slot_forwarding"]["value_preserved_between"] = False
    registry = PassRegistry()
    registry.register(ThumbStackForwardingPass())
    output = tmp_path / "must-not-exist.bin"

    result = PreflightPipeline(registry, EmitInPlace()).execute(
        "thumb-stack-forwarding-v1", unit, context, image, output
    )

    assert result.status == "unsupported"
    assert result.reason_code == "intervening_dataflow_incomplete"
    assert not output.exists()


def test_thumb_stack_forwarding_rejects_observable_reload(tmp_path: Path) -> None:
    image, unit = _thumb_list_item(tmp_path)
    reload_effect = unit.effects.memory[1]
    unsafe_effects = EffectSummary(
        register_reads=unit.effects.register_reads,
        register_writes=unit.effects.register_writes,
        flag_reads=unit.effects.flag_reads,
        flag_writes=unit.effects.flag_writes,
        memory=(
            unit.effects.memory[0],
            MemoryEffect(
                reload_effect.effect_id,
                reload_effect.instruction_identity,
                reload_effect.operation,
                reload_effect.space,
                reload_effect.address_expression,
                reload_effect.width,
                reload_effect.permissions,
                reload_effect.alias_class,
                reload_effect.escape,
                True,
                reload_effect.order,
                reload_effect.possible_faults,
            ),
            unit.effects.memory[2],
        ),
    )
    unsafe = FirmwareUnitView(
        unit.schema_version,
        unit.unit_id,
        unit.name,
        unit.input_image_hash,
        unit.region_bytes_hash,
        unit.target,
        unit.address_start,
        unit.address_end,
        unit.file_start,
        unit.file_end,
        unit.instructions,
        unit.entries,
        unit.exits,
        unit.typed_ranges,
        unit.protected_entries,
        unit.references,
        unsafe_effects,
        unit.origins,
        unit.patch_contract,
        unit.alternate_entries_complete,
    )
    context = _thumb_context(unsafe)
    context.analyses["effect_summary"] = unsafe.effects.semantic_dict()
    registry = PassRegistry()
    registry.register(ThumbStackForwardingPass())
    output = tmp_path / "observable-reload-must-not-exist.bin"

    result = PreflightPipeline(registry, EmitInPlace()).execute(
        "thumb-stack-forwarding-v1", unsafe, context, image, output
    )

    assert result.status == "counterexample"
    assert result.reason_code == "thumb_stack_effect_contract_not_forwardable"
    assert not output.exists()


@pytest.mark.parametrize(
    ("frame_base_established", "expected_status"),
    ((True, "accepted"), (False, "unsupported")),
)
def test_thumb_r7_forwarding_requires_established_frame_base(
    tmp_path: Path, frame_base_established: bool, expected_status: str
) -> None:
    image, unit = _thumb_list_item(tmp_path)
    rewritten_instructions = list(unit.instructions)
    rewritten_instructions[1] = replace(
        rewritten_instructions[1], encoding="7860", operands="r0, [r7, #4]"
    )
    rewritten_instructions[2] = replace(
        rewritten_instructions[2], encoding="7b68", operands="r3, [r7, #4]"
    )
    image_bytes = b"".join(bytes.fromhex(item.encoding) for item in rewritten_instructions)
    image.write_bytes(image_bytes)
    unit = replace(
        unit,
        input_image_hash=hash_file(image),
        region_bytes_hash=hashlib.sha256(image_bytes).hexdigest(),
        instructions=tuple(rewritten_instructions),
    )
    context = _thumb_context(unit)
    analysis = context.analyses["stack_slot_forwarding"]
    analysis.update(
        {
            "base_register": "r7",
            "frame_base_established": frame_base_established,
        }
    )
    output = tmp_path / f"r7-{frame_base_established}.bin"
    registry = PassRegistry()
    registry.register(ThumbStackForwardingPass())

    result = PreflightPipeline(registry, EmitInPlace()).execute(
        "thumb-stack-forwarding-v1", unit, context, image, output
    )

    assert result.status == expected_status
    if frame_base_established:
        assert output.read_bytes()[4:6] == bytes.fromhex("0346")
    else:
        assert result.reason_code == "pattern_not_supported"
        assert not output.exists()


def test_firmware_unit_is_immutable_and_hash_stable(tmp_path: Path) -> None:
    _image, unit = _accessor(tmp_path)
    assert unit.contract_hash == hash_json(unit.to_dict())
    with pytest.raises(FrozenInstanceError):
        unit.name = "changed"  # type: ignore[misc]


def test_ordinary_fop_pass_checks_emits_and_installs_in_place(tmp_path: Path) -> None:
    image, unit = _accessor(tmp_path)
    registry = PassRegistry()
    registry.register(OrdinaryStackForwardingPass())
    output = tmp_path / "accepted.bin"

    result = PreflightPipeline(registry, EmitInPlace()).execute(
        "ordinary-stack-forwarding-v1",
        unit,
        _context(unit, "ordinary-fop"),
        image,
        output,
    )

    assert result.status == "accepted"
    assert output.exists()
    assert output.stat().st_size == image.stat().st_size
    assert output.read_bytes()[:4] == bytes.fromhex("f30f1efa")
    assert result.emission["live_replacement_bytes"] == 24
    assert result.emission["padding_bytes"] == 5
    assert any(item.stage == "accepted" for item in result.ledger)
    load_origin = next(
        item for item in result.emission["origin_mappings"]
        if item["original_identity"] == "original-i5"
    )
    assert load_origin["fault_kinds"] == ("read_fault",) or load_origin["fault_kinds"] == ["read_fault"]


def test_declared_required_analysis_is_enforced_before_pass_execution(tmp_path: Path) -> None:
    image, unit = _accessor(tmp_path)
    registry = PassRegistry()
    registry.register(OrdinaryStackForwardingPass())
    context = _context(unit, "ordinary-fop")
    del context.analyses["reference_graph"]
    output = tmp_path / "missing-analysis.bin"

    result = PreflightPipeline(registry, EmitInPlace()).execute(
        "ordinary-stack-forwarding-v1", unit, context, image, output
    )

    assert result.status == "unsupported"
    assert result.reason_code == "required_analysis_missing"
    assert result.ledger[-1].evidence == {"analyses": ["reference_graph"]}
    assert not output.exists()


@pytest.mark.parametrize(
    ("implementation", "pass_id", "suffix"),
    [
        (ShortAccessorPass(), "short-accessor-negative-v1", "short-negative"),
        (
            ExperimentalStoreRemovalPass(),
            "experimental-store-removal-negative-v1",
            "frs-negative",
        ),
    ],
)
def test_alias_negative_fixtures_are_rejected_without_partial_output(
    tmp_path: Path, implementation, pass_id: str, suffix: str
) -> None:
    image, unit = _accessor(tmp_path)
    registry = PassRegistry()
    registry.register(implementation)
    output = tmp_path / f"{suffix}.bin"

    result = PreflightPipeline(registry, EmitInPlace()).execute(
        pass_id, unit, _context(unit, suffix), image, output
    )

    assert result.status == "counterexample"
    assert result.reason_code == "observable_effect_summary_changed"
    assert not output.exists()
    assert result.ledger[-1].stage == "model_checked"


def _nop_unit(tmp_path: Path, protected: bool) -> tuple[Path, FirmwareUnitView]:
    image = tmp_path / ("protected.bin" if protected else "ordinary.bin")
    image.write_bytes(b"\x90")
    instruction = _instruction(0, 0x2000, 0, "90", "nop", "")
    return image, FirmwareUnitView(
        1,
        f"nop:{protected}",
        "nop",
        hash_file(image),
        hashlib.sha256(b"\x90").hexdigest(),
        TargetDescription("x86_64", "long", "little", "fixture", ()),
        0x2000,
        0x2001,
        0,
        1,
        (instruction,),
        (0x2000,) if protected else (),
        ("i0",),
        (TypedRange("code", 0x2000, 0x2001, 0, 1, "code", "rx", "immutable"),),
        (ProtectedEntry("landing", "i0", 0x2000, "landing", (), "unknown"),) if protected else (),
        (),
        EffectSummary(),
        (),
        PatchContract(),
        True,
    )


def test_external_pass_registration_has_accepted_and_rejected_cases(tmp_path: Path) -> None:
    registry = PassRegistry()
    _register_external(registry)
    pipeline = PreflightPipeline(registry, EmitInPlace())
    image, unit = _nop_unit(tmp_path, False)
    accepted = pipeline.execute(
        "external-identity-nop-v1",
        unit,
        _context(unit, "external-identity-nop"),
        image,
        tmp_path / "nop-out.bin",
    )
    protected_image, protected_unit = _nop_unit(tmp_path, True)
    rejected = pipeline.execute(
        "external-identity-nop-v1",
        protected_unit,
        _context(protected_unit, "external-identity-nop"),
        protected_image,
        tmp_path / "protected-out.bin",
    )

    assert accepted.status == "accepted"
    assert rejected.status == "emission_failure"
    assert rejected.reason_code == "protected_entry_selected"
    assert not (tmp_path / "protected-out.bin").exists()


def test_elf_reference_is_linked_to_mutable_object_and_state(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    image = root / (
        "third_party/preflight/lemix/artifact/Lemix/Ported_Applications/dataset/"
        "FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/SmartSpeaker_Ported/"
        "build/SmartSpeaker"
    )
    if not image.exists():
        pytest.skip("recovered LEMIX fixture is unavailable")
    instruction = InstructionIdentity(
        "tick-load", "tick-load", 0x40807C, 0x807C, 7,
        "488b05edd50800", "mov", "rax, qword ptr [rip + 0x8d5ed]", ("read_fault",),
    )
    edge = ELFReferenceResolver(image).resolve_instruction(instruction)
    assert edge is not None
    assert edge.status == "resolved"
    assert edge.target_identity is not None and "xTickCount" in edge.target_identity
    assert edge.mutability == "mutable"
    states = BoundedStateConstructor().linked_scalar(edge)
    assert states.status == "accepted"
    assert states.states[0].requests[0].address == 0x495670


def test_unresolved_reference_is_unsupported_not_frozen() -> None:
    edge = ReferenceEdge(
        "ref", "i0", 0, "runtime", None, None, 0, "external", None, None,
        "runtime_binding", "external", "", "unsupported", (),
    )
    result = BoundedStateConstructor().linked_scalar(edge)
    assert result.status == "unsupported"
    assert result.reason_code == "reference_target_unresolved"


def test_list_state_repair_retains_exact_ranges_permissions_and_requests() -> None:
    constructor = BoundedStateConstructor()
    for name in ("vListInsertEnd", "uxListRemove"):
        result = constructor.list_fixture(name)
        assert result.status == "accepted"
        state = result.states[0]
        assert all(segment.permissions == "rw" for segment in state.memory)
        assert {request.segment_id for request in state.requests} == {"list", "item"}
        assert all(request.outcome == "mapped" for request in state.requests)

"""Deterministic bounded passes used by the PreflightPass SDK v1 prototype."""

from __future__ import annotations

from dataclasses import replace
import re
from typing import Iterable

from .region import (
    EffectSummary,
    FirmwareUnitView,
    InstructionIdentity,
    OriginStateMapping,
)
from .sdk import PassDescriptor, PassResult, PreflightContext, ReplacementEdit, RewriteBuilder


def _descriptor(pass_id: str) -> PassDescriptor:
    return PassDescriptor(
        pass_id=pass_id,
        version=1,
        required_analyses=("effect_summary", "entry_completeness", "reference_graph"),
        supported_targets=("x86_64",),
        required_features=(),
        allowed_edits=("replace_contiguous",),
        preserved_analyses=("entry_completeness", "reference_graph"),
        invalidated_analyses=("instruction_index", "cost"),
        max_code_growth_bytes=0,
        max_region_bytes=256,
        max_work_units=1024,
        skip_reasons=(
            "pattern_not_supported", "unknown_effects", "patch_contract_not_empty",
            "protected_instruction_in_body",
        ),
    )


def _selected_body(unit: FirmwareUnitView) -> tuple[InstructionIdentity, ...]:
    protected = {item.instruction_identity for item in unit.protected_entries}
    return tuple(item for item in unit.instructions if item.identity not in protected)


def _replacement_instructions(
    unit: FirmwareUnitView,
    selected: tuple[InstructionIdentity, ...],
    encodings: Iterable[tuple[str, str, str, str, tuple[str, ...]]],
) -> tuple[InstructionIdentity, ...]:
    """Build replacement records.

    Each tuple is encoding, mnemonic, operands, original identity, faults.
    """

    address = selected[0].address
    file_offset = selected[0].file_offset
    result = []
    for index, (encoding, mnemonic, operands, original, faults) in enumerate(encodings):
        size = len(bytes.fromhex(encoding))
        result.append(
            InstructionIdentity(
                identity=f"{unit.unit_id}:candidate:{index}",
                original_identity=original,
                address=address,
                file_offset=file_offset,
                size=size,
                encoding=encoding,
                mnemonic=mnemonic,
                operands=operands,
                possible_faults=tuple(sorted(faults)),
            )
        )
        address += size
        file_offset += size
    return tuple(result)


def _origin_for_faults(replacement: tuple[InstructionIdentity, ...]) -> tuple[OriginStateMapping, ...]:
    return tuple(
        OriginStateMapping(
            emitted_identity=item.identity,
            original_identity=item.original_identity,
            fault_kinds=item.possible_faults,
            state_fields=("flags", "memory", "registers"),
        )
        for item in replacement
        if item.possible_faults
    )


class OrdinaryStackForwardingPass:
    """Reuse the established FOP transformation on two qualified O0 shapes."""

    descriptor = _descriptor("ordinary-stack-forwarding-v1")

    def run(
        self,
        input: FirmwareUnitView,
        context: PreflightContext,
        edits: RewriteBuilder,
    ) -> PassResult:
        if input.effects.unknown_effects:
            return PassResult("skipped", "unknown_effects")
        selected = _selected_body(input)
        by_text = {(item.mnemonic, item.operands): item for item in selected}
        if input.name == "prvGetThreadFromTask":
            required = [
                ("push", "rbp"),
                ("mov", "rbp, rsp"),
                ("mov", "qword ptr [rbp - 0x18], rdi"),
                ("mov", "rax, qword ptr [rbp - 0x18]"),
                ("mov", "rax, qword ptr [rax]"),
                ("mov", "qword ptr [rbp - 8], rax"),
                ("mov", "rax, qword ptr [rbp - 8]"),
                ("add", "rax, 8"),
                ("pop", "rbp"),
                ("ret", ""),
            ]
            if any(item not in by_text for item in required):
                return PassResult("skipped", "pattern_not_supported")
            original = {key: by_text[key].original_identity for key in required}
            encodings = (
                ("55", "push", "rbp", original[("push", "rbp")], ("stack_fault",)),
                ("4889e5", "mov", "rbp, rsp", original[("mov", "rbp, rsp")], ()),
                ("48897de8", "mov", "qword ptr [rbp - 0x18], rdi", original[("mov", "qword ptr [rbp - 0x18], rdi")], ("stack_fault",)),
                ("4889f8", "mov", "rax, rdi", original[("mov", "rax, qword ptr [rbp - 0x18]")], ()),
                ("488b00", "mov", "rax, qword ptr [rax]", original[("mov", "rax, qword ptr [rax]")], ("read_fault",)),
                ("488945f8", "mov", "qword ptr [rbp - 8], rax", original[("mov", "qword ptr [rbp - 8], rax")], ("stack_fault",)),
                ("4883c008", "add", "rax, 8", original[("add", "rax, 8")], ()),
                ("5d", "pop", "rbp", original[("pop", "rbp")], ("stack_fault",)),
                ("c3", "ret", "", original[("ret", "")], ("stack_fault",)),
            )
        elif input.name == "vListInitialiseItem":
            required = [
                ("push", "rbp"), ("mov", "rbp, rsp"),
                ("mov", "qword ptr [rbp - 8], rdi"),
                ("mov", "rax, qword ptr [rbp - 8]"),
                ("mov", "qword ptr [rax + 0x20], 0"),
                ("nop", ""), ("pop", "rbp"), ("ret", ""),
            ]
            if any(item not in by_text for item in required):
                return PassResult("skipped", "pattern_not_supported")
            original = {key: by_text[key].original_identity for key in required}
            encodings = (
                ("55", "push", "rbp", original[("push", "rbp")], ("stack_fault",)),
                ("4889e5", "mov", "rbp, rsp", original[("mov", "rbp, rsp")], ()),
                ("48897df8", "mov", "qword ptr [rbp - 8], rdi", original[("mov", "qword ptr [rbp - 8], rdi")], ("stack_fault",)),
                ("4889f8", "mov", "rax, rdi", original[("mov", "rax, qword ptr [rbp - 8]")], ()),
                ("48c7402000000000", "mov", "qword ptr [rax + 0x20], 0", original[("mov", "qword ptr [rax + 0x20], 0")], ("write_fault",)),
                ("90", "nop", "", original[("nop", "")], ()),
                ("5d", "pop", "rbp", original[("pop", "rbp")], ("stack_fault",)),
                ("c3", "ret", "", original[("ret", "")], ("stack_fault",)),
            )
        else:
            return PassResult("skipped", "pattern_not_supported")

        replacement = _replacement_instructions(input, selected, encodings)
        edit = ReplacementEdit(
            proposal_id=f"{input.unit_id}:ordinary-fop",
            edit_kind="replace_contiguous",
            selected_identities=tuple(item.identity for item in selected),
            replacement=replacement,
            replacement_bytes="".join(item.encoding for item in replacement),
            proposed_effects=input.effects,
            origins=_origin_for_faults(replacement),
            semantic_evidence={
                "rule": "ordinary-stack-forwarding-v1",
                "source": "BOLT FrameOptimizer-equivalent bounded rule",
                "stores_retained": True,
                "state_set_hash": context.state_set_hash,
            },
        )
        edits.replace(edit)
        return PassResult("proposed", "qualified_stack_forwarding", (edit.proposal_id,))


def _thumb_descriptor(pass_id: str) -> PassDescriptor:
    return PassDescriptor(
        pass_id=pass_id,
        version=1,
        required_analyses=(
            "effect_summary",
            "entry_completeness",
            "reference_graph",
            "stack_slot_forwarding",
        ),
        supported_targets=("arm",),
        required_features=("thumb",),
        allowed_edits=("replace_contiguous",),
        preserved_analyses=("entry_completeness", "reference_graph"),
        invalidated_analyses=("instruction_index", "cost"),
        max_code_growth_bytes=0,
        max_region_bytes=256,
        max_work_units=1024,
        skip_reasons=(
            "pattern_not_supported",
            "unknown_effects",
            "stack_slot_not_private",
            "stack_address_escaped",
            "observer_between_store_and_load",
            "stack_forwarding_analysis_incomplete",
            "intervening_dataflow_incomplete",
        ),
    )


def _thumb_fallthrough_descriptor(pass_id: str) -> PassDescriptor:
    return PassDescriptor(
        pass_id=pass_id,
        version=1,
        required_analyses=(
            "effect_summary",
            "entry_completeness",
            "reference_graph",
            "fallthrough_branch",
        ),
        supported_targets=("arm",),
        required_features=("thumb",),
        allowed_edits=("replace_contiguous",),
        preserved_analyses=("entry_completeness",),
        invalidated_analyses=("instruction_index", "cost", "cfg", "reference_graph"),
        max_code_growth_bytes=0,
        max_region_bytes=4,
        max_work_units=64,
        skip_reasons=(
            "pattern_not_supported",
            "unknown_effects",
            "branch_target_not_fallthrough",
            "branch_observable",
            "modeled_timing_observable",
            "fallthrough_analysis_incomplete",
        ),
    )


def _thumb_hot_exit_layout_descriptor(pass_id: str) -> PassDescriptor:
    return PassDescriptor(
        pass_id=pass_id,
        version=1,
        required_analyses=(
            "effect_summary",
            "entry_completeness",
            "reference_graph",
            "hot_exit_layout",
        ),
        supported_targets=("arm",),
        required_features=("thumb",),
        allowed_edits=("replace_contiguous",),
        preserved_analyses=("entry_completeness",),
        invalidated_analyses=("instruction_index", "cost", "cfg", "reference_graph"),
        max_code_growth_bytes=0,
        max_region_bytes=1024,
        max_work_units=4096,
        skip_reasons=(
            "pattern_not_supported",
            "unknown_effects",
            "layout_analysis_incomplete",
            "identity_mapping_incomplete",
            "path_equivalence_incomplete",
            "timing_model_not_preserved",
        ),
    )


_THUMB_STACK_ACCESS = re.compile(
    r"^(r(?:1[0-2]|[0-9])), \[(?P<base>sp|r7)"
    r"(?:, #(?P<offset>0x[0-9a-f]+|[0-9]+))?\]$"
)


def _thumb_stack_access(
    instruction: InstructionIdentity,
) -> tuple[int, str, int] | None:
    match = _THUMB_STACK_ACCESS.match(instruction.operands)
    if match is None:
        return None
    register = int(match.group(1)[1:])
    raw_offset = match.group("offset")
    return register, match.group("base"), int(raw_offset, 0) if raw_offset is not None else 0


def _thumb_mov_encoding(destination: int, source: int) -> str:
    if not (0 <= destination <= 15 and 0 <= source <= 15):
        raise ValueError("Thumb MOV register is outside r0-r15")
    halfword = 0x4600 | (source << 3) | (destination & 7) | ((destination & 8) << 4)
    return halfword.to_bytes(2, "little").hex()


class ThumbStackForwardingPass:
    """Forward one bounded private-stack spill/reload in Thumb machine code.

    This is the target form of ordinary FOP, not a new dataflow algorithm.  The
    analysis must explicitly establish a writable private stack slot, no escape,
    and no observer/interrupt boundary between the store and load.
    """

    descriptor = _thumb_descriptor("thumb-stack-forwarding-v1")

    def run(
        self,
        input: FirmwareUnitView,
        context: PreflightContext,
        edits: RewriteBuilder,
    ) -> PassResult:
        if input.target.mode != "thumb":
            return PassResult("skipped", "pattern_not_supported")
        if input.effects.unknown_effects:
            return PassResult("skipped", "unknown_effects")
        analysis = context.analyses.get("stack_slot_forwarding")
        if not isinstance(analysis, dict) or not analysis.get("complete"):
            return PassResult("skipped", "stack_forwarding_analysis_incomplete")
        if not analysis.get("private_stack"):
            return PassResult("skipped", "stack_slot_not_private")
        if analysis.get("address_escaped"):
            return PassResult("skipped", "stack_address_escaped")
        if analysis.get("observer_between"):
            return PassResult("skipped", "observer_between_store_and_load")

        selected = _selected_body(input)
        match: tuple[int, int, InstructionIdentity, InstructionIdentity, int, int] | None = None
        for store_index, store in enumerate(selected):
            if store.mnemonic != "str":
                continue
            for load_index in range(store_index + 1, len(selected)):
                load = selected[load_index]
                if load.mnemonic != "ldr":
                    continue
                store_access = _thumb_stack_access(store)
                load_access = _thumb_stack_access(load)
                if store_access is None or load_access is None:
                    continue
                source, store_base, store_offset = store_access
                destination, load_base, load_offset = load_access
                if (
                    store_base != load_base
                    or store_offset != load_offset
                    or store.size != 2
                    or load.size != 2
                ):
                    continue
                if analysis.get("base_register") != store_base:
                    continue
                if store_base == "r7" and not analysis.get("frame_base_established"):
                    continue
                if analysis.get("store_identity") != store.identity:
                    continue
                if analysis.get("load_identity") != load.identity:
                    continue
                if int(analysis.get("width", 0)) != 4:
                    continue
                intervening = selected[store_index + 1:load_index]
                declared_intervening = tuple(
                    str(item)
                    for item in analysis.get("intervening_instruction_identities", ())
                )
                if tuple(item.identity for item in intervening) != declared_intervening:
                    continue
                if intervening and not analysis.get("value_preserved_between"):
                    return PassResult("skipped", "intervening_dataflow_incomplete")
                if intervening and not analysis.get("stack_slot_not_overwritten"):
                    return PassResult("skipped", "intervening_dataflow_incomplete")
                match = (
                    store_index, load_index, store, load, source, destination
                )
                break
            if match is not None:
                break
        if match is None:
            return PassResult("skipped", "pattern_not_supported")

        _store_index, _load_index, store, load, source, destination = match
        encodings = []
        for instruction in selected:
            if instruction.identity == load.identity:
                if destination == source:
                    # The loaded value is already resident in the destination.
                    # Use the architectural 16-bit Thumb NOP instead of a
                    # high-register-form self move; both preserve flags and
                    # register state, but NOP is the canonical no-op encoding.
                    encodings.append(
                        ("00bf", "nop", "", load.original_identity, ())
                    )
                else:
                    encodings.append(
                        (
                            _thumb_mov_encoding(destination, source),
                            "mov",
                            f"r{destination}, r{source}",
                            load.original_identity,
                            (),
                        )
                    )
            else:
                encodings.append(
                    (
                        instruction.encoding,
                        instruction.mnemonic,
                        instruction.operands,
                        instruction.original_identity,
                        instruction.possible_faults,
                    )
                )
        replacement = _replacement_instructions(input, selected, encodings)
        edit = ReplacementEdit(
            proposal_id=f"{input.unit_id}:thumb-stack-forwarding",
            edit_kind="replace_contiguous",
            selected_identities=tuple(item.identity for item in selected),
            replacement=replacement,
            replacement_bytes="".join(item.encoding for item in replacement),
            proposed_effects=input.effects,
            origins=_origin_for_faults(replacement),
            semantic_evidence={
                "rule": "thumb-stack-forwarding-v1",
                "source": "ordinary FOP adapted to a bounded Thumb spill/reload",
                "stores_retained": True,
                "removed_load_original_identity": load.original_identity,
                "source_store_original_identity": store.original_identity,
                "stack_analysis": dict(analysis),
                "bounded_intervening_instruction_count": _load_index - _store_index - 1,
                "intervening_original_identities": tuple(
                    item.original_identity
                    for item in selected[_store_index + 1:_load_index]
                ),
                "state_set_hash": context.state_set_hash,
            },
        )
        edits.replace(edit)
        return PassResult(
            "proposed", "qualified_thumb_stack_forwarding", (edit.proposal_id,)
        )


class ThumbFallthroughBranchPass:
    """Replace a 16-bit unconditional branch-to-next with one Thumb NOP.

    O0 firmware frequently contains ``b.n`` instructions whose resolved target
    is the immediately following halfword.  Architecturally they have no data,
    flag, memory, or control-flow effect, but they terminate a QEMU translation
    block.  The rule is admitted only when the resolved code reference is exact
    and the contract does not observe branch events or instruction timing.
    Replacing one instruction with one instruction also preserves the icount
    scheduling boundary used by the fixed-work firmware experiment.
    """

    descriptor = _thumb_fallthrough_descriptor("thumb-fallthrough-branch-v1")

    def run(
        self,
        input: FirmwareUnitView,
        context: PreflightContext,
        edits: RewriteBuilder,
    ) -> PassResult:
        if input.target.mode != "thumb":
            return PassResult("skipped", "pattern_not_supported")
        if input.effects.unknown_effects:
            return PassResult("skipped", "unknown_effects")
        analysis = context.analyses.get("fallthrough_branch")
        if not isinstance(analysis, dict) or not analysis.get("complete"):
            return PassResult("skipped", "fallthrough_analysis_incomplete")
        if analysis.get("branch_observable"):
            return PassResult("skipped", "branch_observable")
        if analysis.get("modeled_timing_observable"):
            return PassResult("skipped", "modeled_timing_observable")

        selected = _selected_body(input)
        if len(selected) != 1:
            return PassResult("skipped", "pattern_not_supported")
        branch = selected[0]
        if branch.mnemonic != "b" or branch.size != 2:
            return PassResult("skipped", "pattern_not_supported")
        target = int(analysis.get("target_address", -1))
        if target != branch.address + branch.size:
            return PassResult("skipped", "branch_target_not_fallthrough")
        references = [
            item for item in input.references if item.source_identity == branch.identity
        ]
        if not (
            len(references) == 1
            and references[0].status == "resolved"
            and references[0].target_kind == "code"
            and references[0].target_address == target
        ):
            return PassResult("skipped", "branch_target_not_fallthrough")

        replacement = _replacement_instructions(
            input,
            selected,
            (("00bf", "nop", "", branch.original_identity, ()),),
        )
        edit = ReplacementEdit(
            proposal_id=f"{input.unit_id}:thumb-fallthrough-branch",
            edit_kind="replace_contiguous",
            selected_identities=(branch.identity,),
            replacement=replacement,
            replacement_bytes="00bf",
            proposed_effects=input.effects,
            origins=(
                OriginStateMapping(
                    replacement[0].identity,
                    branch.original_identity,
                    (),
                    ("control",),
                ),
            ),
            semantic_evidence={
                "rule": "thumb-fallthrough-branch-v1",
                "source": "resolved unconditional Thumb branch to sequential successor",
                "removed_reference_id": references[0].reference_id,
                "branch_original_identity": branch.original_identity,
                "fallthrough_analysis": dict(analysis),
                "instruction_count_preserved": True,
                "state_set_hash": context.state_set_hash,
            },
        )
        edits.replace(edit)
        return PassResult(
            "proposed", "qualified_thumb_fallthrough_branch", (edit.proposal_id,)
        )


class ThumbHotExitLayoutPass:
    """Install a checked, size-preserving Thumb hot-exit block layout.

    Discovery and branch repair are separate analysis services.  This pass
    accepts only a complete one-to-one original-instruction identity
    permutation with explicit hot/cold path and instruction-count proofs.
    """

    descriptor = _thumb_hot_exit_layout_descriptor("thumb-hot-exit-layout-v1")

    def run(
        self,
        input: FirmwareUnitView,
        context: PreflightContext,
        edits: RewriteBuilder,
    ) -> PassResult:
        if input.target.mode != "thumb" or input.effects.unknown_effects:
            return PassResult(
                "skipped",
                "unknown_effects" if input.effects.unknown_effects else "pattern_not_supported",
            )
        analysis = context.analyses.get("hot_exit_layout")
        if not isinstance(analysis, dict) or not analysis.get("complete"):
            return PassResult("skipped", "layout_analysis_incomplete")
        if not all(
            analysis.get(name) is True
            for name in (
                "entry_completeness",
                "memory_call_effect_sequence_preserved",
                "hot_path_identity_sequence_preserved",
                "cold_path_identity_sequence_preserved",
            )
        ):
            return PassResult("skipped", "path_equivalence_incomplete")
        if not (
            analysis.get("timing_model") == "instruction_count"
            and analysis.get("instruction_count_preserved_per_path") is True
        ):
            return PassResult("skipped", "timing_model_not_preserved")
        records = analysis.get("replacement_instructions")
        if not isinstance(records, list) or not records:
            return PassResult("skipped", "layout_analysis_incomplete")
        original_identities = [item.original_identity for item in input.instructions]
        replacement_identities = [
            str(item.get("original_identity", ""))
            for item in records if isinstance(item, dict)
        ]
        if (
            len(replacement_identities) != len(original_identities)
            or sorted(replacement_identities) != sorted(original_identities)
            or len(set(replacement_identities)) != len(replacement_identities)
        ):
            return PassResult("skipped", "identity_mapping_incomplete")
        try:
            encodings = tuple(
                (
                    str(item["encoding"]),
                    str(item["mnemonic"]),
                    str(item.get("operands", "")),
                    str(item["original_identity"]),
                    tuple(sorted(str(value) for value in item.get("possible_faults", ()))),
                )
                for item in records
            )
            replacement = _replacement_instructions(
                input, input.instructions, encodings
            )
        except (KeyError, TypeError, ValueError):
            return PassResult("skipped", "layout_analysis_incomplete")
        if sum(item.size for item in replacement) != sum(
            item.size for item in input.instructions
        ):
            return PassResult("skipped", "pattern_not_supported")
        edit = ReplacementEdit(
            proposal_id=f"{input.unit_id}:thumb-hot-exit-layout",
            edit_kind="replace_contiguous",
            selected_identities=tuple(item.identity for item in input.instructions),
            replacement=replacement,
            replacement_bytes="".join(item.encoding for item in replacement),
            proposed_effects=input.effects,
            origins=_origin_for_faults(replacement),
            semantic_evidence={
                "rule": "thumb-hot-exit-layout-v1",
                "layout_analysis": {
                    key: value for key, value in analysis.items()
                    if key != "replacement_instructions"
                },
                "original_identity_sequence": original_identities,
                "replacement_identity_sequence": replacement_identities,
                "instruction_count_preserved": True,
            },
        )
        edits.replace(edit)
        return PassResult(
            "proposed", "qualified_thumb_hot_exit_layout", (edit.proposal_id,)
        )


def _without_stack_stores(summary: EffectSummary) -> EffectSummary:
    return replace(
        summary,
        memory=tuple(
            item for item in summary.memory
            if not (item.space == "stack" and item.operation in {"write", "read_write"})
        ),
    )


class ShortAccessorPass:
    """Negative fixture: the proposed three-instruction accessor."""

    descriptor = _descriptor("short-accessor-negative-v1")

    def run(self, input: FirmwareUnitView, context: PreflightContext, edits: RewriteBuilder) -> PassResult:
        if input.name != "prvGetThreadFromTask":
            return PassResult("skipped", "pattern_not_supported")
        selected = _selected_body(input)
        pointer_load = next(item for item in selected if item.operands == "rax, qword ptr [rax]")
        add = next(item for item in selected if item.operands == "rax, 8")
        ret = next(item for item in selected if item.mnemonic == "ret")
        replacement = _replacement_instructions(
            input,
            selected,
            (
                ("488b07", "mov", "rax, qword ptr [rdi]", pointer_load.original_identity, ("read_fault",)),
                ("4883c008", "add", "rax, 8", add.original_identity, ()),
                ("c3", "ret", "", ret.original_identity, ("stack_fault",)),
            ),
        )
        edit = ReplacementEdit(
            f"{input.unit_id}:short-negative",
            "replace_contiguous",
            tuple(item.identity for item in selected),
            replacement,
            "".join(item.encoding for item in replacement),
            _without_stack_stores(input.effects),
            _origin_for_faults(replacement),
            {"rule": "unqualified-short-accessor", "alias_contract": "stack-and-object-may-alias"},
        )
        edits.replace(edit)
        return PassResult("proposed", "negative_fixture_proposed", (edit.proposal_id,))


class ExperimentalStoreRemovalPass(ShortAccessorPass):
    """Negative fixture matching the observed FRS accessor body."""

    descriptor = _descriptor("experimental-store-removal-negative-v1")

    def run(self, input: FirmwareUnitView, context: PreflightContext, edits: RewriteBuilder) -> PassResult:
        if input.name != "prvGetThreadFromTask":
            return PassResult("skipped", "pattern_not_supported")
        selected = _selected_body(input)
        originals = {item.operands: item.original_identity for item in selected}
        replacement = _replacement_instructions(
            input,
            selected,
            (
                ("55", "push", "rbp", next(item.original_identity for item in selected if item.mnemonic == "push"), ("stack_fault",)),
                ("4889e5", "mov", "rbp, rsp", originals["rbp, rsp"], ()),
                ("4889f8", "mov", "rax, rdi", originals["rax, qword ptr [rbp - 0x18]"], ()),
                ("488b00", "mov", "rax, qword ptr [rax]", originals["rax, qword ptr [rax]"], ("read_fault",)),
                ("4883c008", "add", "rax, 8", originals["rax, 8"], ()),
                ("5d", "pop", "rbp", next(item.original_identity for item in selected if item.mnemonic == "pop"), ("stack_fault",)),
                ("c3", "ret", "", next(item.original_identity for item in selected if item.mnemonic == "ret"), ("stack_fault",)),
            ),
        )
        edit = ReplacementEdit(
            f"{input.unit_id}:frs-negative",
            "replace_contiguous",
            tuple(item.identity for item in selected),
            replacement,
            "".join(item.encoding for item in replacement),
            _without_stack_stores(input.effects),
            _origin_for_faults(replacement),
            {"rule": "experimental-store-removal", "alias_contract": "stack-and-object-may-alias"},
        )
        edits.replace(edit)
        return PassResult("proposed", "negative_fixture_proposed", (edit.proposal_id,))

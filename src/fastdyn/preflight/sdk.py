"""PreflightPass SDK v1: immutable inputs and checked edit proposals."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

from .model import BundleValidationError, hash_json
from .region import EffectSummary, FirmwareUnitView, InstructionIdentity, OriginStateMapping


FINAL_STATUSES = {
    "accepted", "counterexample", "unknown", "unsupported", "emission_failure",
    "not_profitable", "skipped", "tool_failure",
}


@dataclass(frozen=True)
class PassDescriptor:
    """Machine-checkable pass capabilities and budgets."""

    pass_id: str
    version: int
    required_analyses: tuple[str, ...]
    supported_targets: tuple[str, ...]
    required_features: tuple[str, ...]
    allowed_edits: tuple[str, ...]
    preserved_analyses: tuple[str, ...]
    invalidated_analyses: tuple[str, ...]
    max_code_growth_bytes: int
    max_region_bytes: int
    max_work_units: int
    skip_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.pass_id or self.version != 1:
            raise BundleValidationError("pass descriptor requires a v1 identity")
        if self.max_code_growth_bytes < 0 or self.max_region_bytes <= 0 or self.max_work_units <= 0:
            raise BundleValidationError("pass budgets are invalid")


@dataclass(frozen=True)
class PassResult:
    """Return value of ``PreflightPass.run`` before acceptance services."""

    status: str
    reason_code: str
    proposal_ids: tuple[str, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {"proposed", "skipped", "rejected"}:
            raise BundleValidationError("pass result status is invalid")


@dataclass(frozen=True)
class ReplacementEdit:
    """One atomic contiguous in-place replacement proposal."""

    proposal_id: str
    edit_kind: str
    selected_identities: tuple[str, ...]
    replacement: tuple[InstructionIdentity, ...]
    replacement_bytes: str
    proposed_effects: EffectSummary
    origins: tuple[OriginStateMapping, ...]
    semantic_evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.proposal_id or self.edit_kind != "replace_contiguous":
            raise BundleValidationError("replacement proposal identity/type is invalid")
        if not self.selected_identities:
            raise BundleValidationError("replacement proposal must select instructions")
        try:
            payload = bytes.fromhex(self.replacement_bytes)
        except ValueError as exc:
            raise BundleValidationError("replacement bytes must be hexadecimal") from exc
        if len(payload) != sum(item.size for item in self.replacement):
            raise BundleValidationError("replacement instruction sizes do not match bytes")

    @property
    def proposal_hash(self) -> str:
        return hash_json(asdict(self))


class RewriteBuilder:
    """Collect proposals without mutating the input unit or image."""

    def __init__(self, input_unit: FirmwareUnitView) -> None:
        self._input = input_unit
        self._edits: list[ReplacementEdit] = []

    def replace(self, edit: ReplacementEdit) -> None:
        identities = {item.identity for item in self._input.instructions}
        if any(item not in identities for item in edit.selected_identities):
            raise BundleValidationError("edit selects an instruction outside the immutable input")
        if any(existing.proposal_id == edit.proposal_id for existing in self._edits):
            raise BundleValidationError("proposal IDs must be unique")
        self._edits.append(edit)

    @property
    def edits(self) -> tuple[ReplacementEdit, ...]:
        return tuple(self._edits)


@dataclass
class PreflightContext:
    """Shared services and immutable evidence selected for one pipeline run."""

    analyses: dict[str, Any]
    testcase_decisions: Mapping[str, "Decision"]
    state_set_hash: str
    work_budget: int
    empty_patch_contract_required: bool = True
    profitability_decisions: Mapping[str, "Decision"] = field(default_factory=dict)


class PreflightPass(Protocol):
    """External pass-writer interface for SDK v1."""

    descriptor: PassDescriptor

    def run(
        self,
        input: FirmwareUnitView,
        context: PreflightContext,
        edits: RewriteBuilder,
    ) -> PassResult:
        ...


class PassRegistry:
    """Explicit pass registration; importing a module does not register a pass."""

    def __init__(self) -> None:
        self._passes: dict[str, PreflightPass] = {}

    def register(self, implementation: PreflightPass) -> None:
        pass_id = implementation.descriptor.pass_id
        if pass_id in self._passes:
            raise BundleValidationError(f"pass {pass_id!r} is already registered")
        self._passes[pass_id] = implementation

    def get(self, pass_id: str) -> PreflightPass:
        try:
            return self._passes[pass_id]
        except KeyError as exc:
            raise BundleValidationError(f"pass {pass_id!r} is not registered") from exc

    @property
    def descriptors(self) -> tuple[PassDescriptor, ...]:
        return tuple(item.descriptor for item in self._passes.values())


@dataclass(frozen=True)
class Decision:
    """Stable result from a separate checking, emission, or cost service."""

    status: str
    reason_code: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {
            "accepted", "counterexample", "unknown", "unsupported",
            "emission_failure", "not_profitable", "tool_failure",
        }:
            raise BundleValidationError(f"invalid decision status {self.status!r}")

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"


@dataclass(frozen=True)
class StageRecord:
    """One append-only candidate stage ledger entry."""

    candidate_id: str
    input_contract_hash: str
    patch_contract_hash: str
    state_set_hash: str
    stage: str
    status: str
    reason_code: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StageLedger:
    STAGES = (
        "selected", "exported", "states_constructed", "proposed",
        "testcase_checked", "model_checked", "profitability_checked",
        "emitted", "installed", "accepted",
    )

    def __init__(self) -> None:
        self._records: list[StageRecord] = []

    def append(
        self,
        *,
        candidate_id: str,
        unit: FirmwareUnitView,
        state_set_hash: str,
        stage: str,
        decision: Decision,
    ) -> None:
        if stage not in self.STAGES:
            raise BundleValidationError(f"unknown ledger stage {stage!r}")
        self._records.append(
            StageRecord(
                candidate_id,
                unit.contract_hash,
                unit.patch_contract.contract_hash,
                state_set_hash,
                stage,
                decision.status,
                decision.reason_code,
                dict(decision.evidence),
            )
        )

    @property
    def records(self) -> tuple[StageRecord, ...]:
        return tuple(self._records)


class SemanticChecker:
    """Bounded structural checker for deterministic rules.

    It is deliberately not a general equivalence proof.  It requires an exact
    effect summary and original-identity coverage for every potentially
    faulting replacement instruction.  Rule-specific evidence is then checked
    against a small allowlist.
    """

    SUPPORTED_RULES = {
        "ordinary-stack-forwarding-v1",
        "thumb-stack-forwarding-v1",
        "thumb-fallthrough-branch-v1",
        "thumb-hot-exit-layout-v1",
        "identity-v1",
    }

    def check(self, unit: FirmwareUnitView, edit: ReplacementEdit) -> Decision:
        if edit.proposed_effects.semantic_dict() != unit.effects.semantic_dict():
            before = [asdict(item) for item in unit.effects.memory]
            after = [asdict(item) for item in edit.proposed_effects.memory]
            return Decision(
                "counterexample",
                "observable_effect_summary_changed",
                {"before_memory": before, "after_memory": after},
            )
        rule = str(edit.semantic_evidence.get("rule", ""))
        if rule not in self.SUPPORTED_RULES:
            return Decision("unsupported", "semantic_rule_not_qualified", {"rule": rule})
        if rule == "thumb-stack-forwarding-v1":
            analysis = edit.semantic_evidence.get("stack_analysis")
            if not isinstance(analysis, dict) or not analysis.get("complete"):
                return Decision("unknown", "thumb_stack_analysis_incomplete")
            if not analysis.get("private_stack") or analysis.get("address_escaped"):
                return Decision("counterexample", "thumb_stack_not_private")
            if analysis.get("observer_between"):
                return Decision("counterexample", "thumb_stack_observer_between")
            store_identity = str(
                edit.semantic_evidence.get("source_store_original_identity", "")
            )
            load_identity = str(
                edit.semantic_evidence.get("removed_load_original_identity", "")
            )
            memory_by_original: dict[str, list[Any]] = {}
            original_by_current = {
                item.identity: item.original_identity for item in unit.instructions
            }
            for effect in unit.effects.memory:
                original = original_by_current.get(effect.instruction_identity, "")
                memory_by_original.setdefault(original, []).append(effect)
            stores = memory_by_original.get(store_identity, [])
            loads = memory_by_original.get(load_identity, [])
            if len(stores) != 1 or len(loads) != 1:
                return Decision("unknown", "thumb_stack_effect_identity_incomplete")
            store, load = stores[0], loads[0]
            selected_originals = [item.original_identity for item in unit.instructions]
            try:
                store_index = selected_originals.index(store_identity)
                load_index = selected_originals.index(load_identity)
            except ValueError:
                return Decision("unknown", "thumb_stack_instruction_identity_incomplete")
            intervening = selected_originals[store_index + 1:load_index]
            declared_intervening = [
                str(item)
                for item in edit.semantic_evidence.get(
                    "intervening_original_identities", ()
                )
            ]
            intervening_proved = (
                intervening == declared_intervening
                and (
                    not intervening
                    or (
                        analysis.get("value_preserved_between") is True
                        and analysis.get("stack_slot_not_overwritten") is True
                    )
                )
            )
            conflicting_effects = [
                item for item in unit.effects.memory
                if store.order < item.order < load.order
                and item.operation == "write"
                and item.alias_class == store.alias_class
                and item.address_expression == store.address_expression
            ]
            if not (
                store.operation == "write"
                and load.operation == "read"
                and store.space == load.space == "stack"
                and store.address_expression == load.address_expression
                and store.width == load.width == int(analysis.get("width", 0))
                and store.alias_class == load.alias_class == "private"
                and store.escape == load.escape == "none"
                and not load.observable
                and store.order < load.order
                and intervening_proved
                and not conflicting_effects
            ):
                return Decision(
                    "counterexample",
                    "thumb_stack_effect_contract_not_forwardable",
                    {
                        "store": asdict(store),
                        "load": asdict(load),
                    },
                )
        if rule == "thumb-fallthrough-branch-v1":
            analysis = edit.semantic_evidence.get("fallthrough_analysis")
            if not isinstance(analysis, dict) or not analysis.get("complete"):
                return Decision("unknown", "thumb_fallthrough_analysis_incomplete")
            if analysis.get("branch_observable"):
                return Decision("counterexample", "thumb_branch_event_is_observable")
            if analysis.get("modeled_timing_observable"):
                return Decision("counterexample", "thumb_instruction_timing_is_observable")
            if not edit.semantic_evidence.get("instruction_count_preserved"):
                return Decision("counterexample", "thumb_instruction_count_changed")
            branch_id = str(
                edit.semantic_evidence.get("branch_original_identity", "")
            )
            original = next(
                (
                    item for item in unit.instructions
                    if item.original_identity == branch_id
                ),
                None,
            )
            target = int(analysis.get("target_address", -1))
            if original is None or target != original.address + original.size:
                return Decision("counterexample", "thumb_branch_target_not_fallthrough")
            references = [
                item for item in unit.references
                if item.source_identity == original.identity
            ]
            if not (
                len(references) == 1
                and references[0].status == "resolved"
                and references[0].target_kind == "code"
                and references[0].target_address == target
            ):
                return Decision("unknown", "thumb_branch_reference_not_exact")
        if rule == "thumb-hot-exit-layout-v1":
            analysis = edit.semantic_evidence.get("layout_analysis")
            if not isinstance(analysis, dict) or not analysis.get("complete"):
                return Decision("unknown", "thumb_layout_analysis_incomplete")
            required_proofs = (
                "entry_completeness",
                "internal_reference_repair_complete",
                "memory_call_effect_sequence_preserved",
                "hot_path_identity_sequence_preserved",
                "cold_path_identity_sequence_preserved",
                "instruction_count_preserved_per_path",
            )
            missing_proofs = [
                name for name in required_proofs if analysis.get(name) is not True
            ]
            if missing_proofs:
                return Decision(
                    "unknown", "thumb_layout_path_proof_incomplete",
                    {"missing": missing_proofs},
                )
            if analysis.get("timing_model") != "instruction_count":
                return Decision("unsupported", "thumb_layout_timing_model_unsupported")
            original = [item.original_identity for item in unit.instructions]
            declared_original = [
                str(item) for item in edit.semantic_evidence.get(
                    "original_identity_sequence", ()
                )
            ]
            replacement = [item.original_identity for item in edit.replacement]
            declared_replacement = [
                str(item) for item in edit.semantic_evidence.get(
                    "replacement_identity_sequence", ()
                )
            ]
            if (
                original != declared_original
                or replacement != declared_replacement
                or sorted(original) != sorted(replacement)
                or len(set(original)) != len(original)
            ):
                return Decision("counterexample", "thumb_layout_identity_permutation_invalid")
            originals = {item.original_identity: item for item in unit.instructions}
            fault_mismatches = [
                item.original_identity for item in edit.replacement
                if item.original_identity not in originals
                or item.possible_faults
                != originals[item.original_identity].possible_faults
            ]
            if fault_mismatches:
                return Decision(
                    "counterexample", "thumb_layout_fault_contract_changed",
                    {"original_identities": fault_mismatches},
                )
            if any(item.status != "resolved" for item in unit.references):
                return Decision("unknown", "thumb_layout_reference_unresolved")
        mapped = {item.emitted_identity for item in edit.origins}
        missing = [
            item.identity for item in edit.replacement
            if item.possible_faults and item.identity not in mapped
        ]
        if missing:
            return Decision(
                "unknown", "faulting_instruction_missing_origin", {"instructions": missing}
            )
        return Decision(
            "accepted",
            "bounded_rule_and_effect_contract_agree",
            {"rule": rule, "origin_mappings": len(edit.origins)},
        )


class ProfitabilityChecker:
    """Apply pass growth policy independently of semantic and placement checks."""

    def check(
        self,
        unit: FirmwareUnitView,
        edit: ReplacementEdit,
        descriptor: PassDescriptor,
    ) -> Decision:
        identities = {item.identity: item for item in unit.instructions}
        capacity = sum(identities[item].size for item in edit.selected_identities)
        replacement_size = len(bytes.fromhex(edit.replacement_bytes))
        growth = replacement_size - capacity
        evidence = {
            "qualified_capacity_bytes": capacity,
            "replacement_bytes": replacement_size,
            "growth_bytes": growth,
            "growth_budget_bytes": descriptor.max_code_growth_bytes,
        }
        if growth > descriptor.max_code_growth_bytes:
            return Decision("not_profitable", "pass_code_growth_budget_exceeded", evidence)
        return Decision("accepted", "pass_growth_policy_satisfied", evidence)


@dataclass(frozen=True)
class PipelineOutcome:
    status: str
    reason_code: str
    edit: ReplacementEdit | None
    output_path: Path | None
    ledger: tuple[StageRecord, ...]
    emission: Mapping[str, Any] = field(default_factory=dict)


class CompactEmitter(Protocol):
    def validate(
        self, unit: FirmwareUnitView, edit: ReplacementEdit, descriptor: PassDescriptor
    ) -> Decision:
        ...

    def emit(
        self,
        image: Path,
        output: Path,
        unit: FirmwareUnitView,
        edit: ReplacementEdit,
    ) -> Mapping[str, Any]:
        ...


class PreflightPipeline:
    """Run one registered pass and publish only after every gate accepts."""

    def __init__(
        self,
        registry: PassRegistry,
        emitter: CompactEmitter,
        semantic_checker: SemanticChecker | None = None,
        profitability_checker: ProfitabilityChecker | None = None,
    ) -> None:
        self.registry = registry
        self.emitter = emitter
        self.semantic_checker = semantic_checker or SemanticChecker()
        self.profitability_checker = profitability_checker or ProfitabilityChecker()

    def execute(
        self,
        pass_id: str,
        unit: FirmwareUnitView,
        context: PreflightContext,
        image: Path,
        output: Path,
    ) -> PipelineOutcome:
        implementation = self.registry.get(pass_id)
        descriptor = implementation.descriptor
        ledger = StageLedger()
        candidate_id = f"{unit.unit_id}:{pass_id}"

        def record(stage: str, decision: Decision) -> None:
            ledger.append(
                candidate_id=candidate_id,
                unit=unit,
                state_set_hash=context.state_set_hash,
                stage=stage,
                decision=decision,
            )

        if unit.target.isa not in descriptor.supported_targets:
            decision = Decision("unsupported", "target_not_supported")
            record("selected", decision)
            return PipelineOutcome(decision.status, decision.reason_code, None, None, ledger.records)
        if any(feature not in unit.target.features for feature in descriptor.required_features):
            decision = Decision("unsupported", "required_target_feature_missing")
            record("selected", decision)
            return PipelineOutcome(decision.status, decision.reason_code, None, None, ledger.records)
        if unit.file_end - unit.file_start > descriptor.max_region_bytes:
            decision = Decision("unsupported", "region_budget_exceeded")
            record("selected", decision)
            return PipelineOutcome(decision.status, decision.reason_code, None, None, ledger.records)
        missing_analyses = [
            name for name in descriptor.required_analyses if name not in context.analyses
        ]
        if missing_analyses:
            decision = Decision(
                "unsupported",
                "required_analysis_missing",
                {"analyses": missing_analyses},
            )
            record("selected", decision)
            return PipelineOutcome(decision.status, decision.reason_code, None, None, ledger.records)
        if context.empty_patch_contract_required and unit.patch_contract.anchors:
            decision = Decision("unsupported", "phase_one_requires_empty_patch_contract")
            record("selected", decision)
            return PipelineOutcome(decision.status, decision.reason_code, None, None, ledger.records)

        record("selected", Decision("accepted", "pass_and_target_selected"))
        record("exported", Decision("accepted", "immutable_region_contract_hashed"))
        state_decision = context.analyses.get(
            "state_decision", Decision("unknown", "state_construction_not_supplied")
        )
        record("states_constructed", state_decision)
        if not state_decision.accepted:
            return PipelineOutcome(
                state_decision.status, state_decision.reason_code, None, None, ledger.records
            )

        builder = RewriteBuilder(unit)
        result = implementation.run(unit, context, builder)
        if result.status != "proposed" or len(builder.edits) != 1:
            decision = Decision(
                "unsupported" if result.status == "skipped" else "unknown",
                result.reason_code,
                dict(result.diagnostics),
            )
            record("proposed", decision)
            return PipelineOutcome(decision.status, decision.reason_code, None, None, ledger.records)
        edit = builder.edits[0]
        record(
            "proposed",
            Decision("accepted", "single_atomic_edit_proposed", {"proposal_hash": edit.proposal_hash}),
        )
        testcase = context.testcase_decisions.get(
            edit.proposal_id, Decision("unknown", "testcase_result_missing")
        )
        record("testcase_checked", testcase)
        if not testcase.accepted:
            return PipelineOutcome(testcase.status, testcase.reason_code, edit, None, ledger.records)
        semantic = self.semantic_checker.check(unit, edit)
        record("model_checked", semantic)
        if not semantic.accepted:
            return PipelineOutcome(semantic.status, semantic.reason_code, edit, None, ledger.records)
        static_profitability = self.profitability_checker.check(unit, edit, descriptor)
        external_profitability = context.profitability_decisions.get(edit.proposal_id)
        if static_profitability.accepted and external_profitability is not None:
            profitability = Decision(
                external_profitability.status,
                external_profitability.reason_code,
                {
                    "static_growth": dict(static_profitability.evidence),
                    "target_cost": dict(external_profitability.evidence),
                },
            )
        else:
            profitability = static_profitability
        record("profitability_checked", profitability)
        if not profitability.accepted:
            return PipelineOutcome(
                profitability.status,
                profitability.reason_code,
                edit,
                None,
                ledger.records,
            )
        placement = self.emitter.validate(unit, edit, descriptor)
        record("emitted", placement)
        if not placement.accepted:
            return PipelineOutcome(placement.status, placement.reason_code, edit, None, ledger.records)
        try:
            emission = self.emitter.emit(image, output, unit, edit)
        except (OSError, BundleValidationError) as exc:
            decision = Decision("emission_failure", "atomic_emit_failed", {"message": str(exc)})
            record("installed", decision)
            return PipelineOutcome(decision.status, decision.reason_code, edit, None, ledger.records)
        record("installed", Decision("accepted", "candidate_atomically_installed", emission))
        record("accepted", Decision("accepted", "all_preflight_gates_accepted"))
        return PipelineOutcome("accepted", "all_preflight_gates_accepted", edit, output, ledger.records, emission)

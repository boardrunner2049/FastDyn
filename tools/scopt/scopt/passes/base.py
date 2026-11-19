"""Pass API types (design §5.4-5.5). Interfaces only — frozen by WP0; WP9 implements.

A pass is a class with a ``descriptor: ClassVar[PassDescriptor]`` and a
``run(unit_or_snapshot, ctx, edits) -> PassResult`` method. It reads through
``PassContext`` and proposes edits through a ``ProposalBuilder`` (concrete class
in ``scopt.proposal``, WP9); it never mutates a snapshot itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from ..model.native import InlineCall
from ..model.unit import SnapshotView, UnitView

__all__ = [
    "EditKind", "B2_EDIT_KINDS", "B3_EDIT_KINDS", "NATIVE_EDIT_KINDS", "LayoutSection", "Budgets",
    "PassDescriptor", "Outcome", "PassResult", "PassContext", "ProposalBuilderAPI",
    "Pass", "BudgetExceeded", "EditCapabilityError", "StaleFact", "Reason",
    # B3/B4 typed payloads (design §3.7, frozen at WP18's first merge)
    "GuardAtom", "GuardedSlot", "Clone", "SpecEntry", "FactRef", "Patch", "PayloadError",
    "InlineCall", "B3Reason", "SLOT_KINDS", "EXIT_CLASSES", "PATCH_TEMPLATES", "PAYLOAD_KINDS",
]


class EditKind(Enum):
    ANNOTATE = 1
    PIN = 2
    LAYOUT_ORDER = 3
    PLACE = 4  # B2
    CLONE = 10
    REDIRECT_EXIT = 11
    ADD_ENTRY = 12
    PURE_REWRITE = 13  # B3/B4 (defined, rejected in B2)
    ADD_REGION = 14
    ADD_RECOVERY = 15
    ATTACH_FACT = 16
    INLINE_CALL = 17


B2_EDIT_KINDS = frozenset({EditKind.ANNOTATE, EditKind.PIN, EditKind.LAYOUT_ORDER, EditKind.PLACE})
B3_EDIT_KINDS = frozenset({EditKind.CLONE, EditKind.REDIRECT_EXIT, EditKind.ADD_ENTRY,
                          EditKind.PURE_REWRITE, EditKind.ADD_REGION, EditKind.ADD_RECOVERY,
                          EditKind.ATTACH_FACT})
NATIVE_EDIT_KINDS = frozenset({EditKind.INLINE_CALL})

LayoutSection = Literal["hot", "warm", "unknown", "boot", "cold"]


@dataclass(frozen=True)
class Budgets:
    max_wall_s: float = 60.0
    max_code_growth_bytes: int = 0
    max_region_members: int = 1
    max_context_versions: int = 0
    max_units: int | None = None


def _empty_map() -> Mapping[str, Any]:
    return MappingProxyType({})


@dataclass(frozen=True)
class PassDescriptor:
    name: str
    version: int
    domain: Literal["HostCache"] = "HostCache"
    unit_scope: Literal["unit", "snapshot", "region"] = "snapshot"
    requires: tuple[tuple[str, int], ...] = ()
    requires_profile_phase: str | None = None
    min_profile_match: float | None = None
    requires_capture_level: int = 1
    capabilities: frozenset[EditKind] = frozenset()
    budgets: Budgets = Budgets()
    preserves: frozenset[str] = frozenset()  # analysis names, or {"*"}
    invalidates: frozenset[str] = frozenset()  # facets: "layout", "code", "meta"
    params_schema: Mapping[str, type] = field(default_factory=_empty_map)
    overrides: frozenset[str] = frozenset()  # e.g. {"layout"}: later pass wins on conflicts (§5.6)


class Outcome(Enum):
    NO_CHANGE = "no_change"
    CANDIDATE = "candidate"
    SKIPPED_UNSUPPORTED = "skipped_unsupported"
    SKIPPED_UNKNOWN = "skipped_unknown"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"


@dataclass(frozen=True)
class PassResult:
    outcome: Outcome
    reason: str  # stable code, e.g. "unexplained_reference"
    detail: str = ""
    affected: tuple[str, ...] = ()  # UnitIds / RegionIds
    guest_origins: tuple[str, ...] = ()  # vaddr ranges
    stats: Mapping[str, float] = field(default_factory=_empty_map)


class Reason:
    """Stable reason codes used by the manager/packager (strings, never renumbered)."""

    CAPABILITY_NOT_ENABLED = "capability_not_enabled"
    STALE_EDIT = "stale_edit"
    STALE_FACT = "stale_fact"
    CONFLICT = "conflict"
    BUDGET_EXHAUSTED = "budget_exhausted"
    EXCEPTION = "exception"
    MISSING_ANALYSIS = "missing_analysis"
    MISSING_PROFILE = "missing_profile"
    PROFILE_INCOMPATIBLE = "profile_incompatible"
    CAPTURE_LEVEL = "capture_level"
    NOT_RELOCATABLE = "not_relocatable"


class BudgetExceeded(Exception):
    """Raised by ``PassContext.budget`` when the pass's wall-clock deadline passes."""


class EditCapabilityError(Exception):
    """An edit kind outside ``descriptor.capabilities`` was proposed."""


class StaleFact(Exception):
    """A cited analysis result no longer matches its dependencies."""


@runtime_checkable
class PassContext(Protocol):
    params: Mapping[str, Any]
    bundle: Any
    runtime: Any
    snapshot_id: str
    log: Any

    def analysis(self, name: str, scope: Any, min_version: int = 1) -> Any: ...  # AnalysisResult
    def fact(self, *args: Any, **kwargs: Any) -> Any: ...
    def profile(self, phase: str) -> Any | None: ...  # ProfileView or None
    def budget(self) -> None: ...  # raises BudgetExceeded past the deadline


@runtime_checkable
class ProposalBuilderAPI(Protocol):
    """Edit methods of ``scopt.proposal.ProposalBuilder`` (§5.5)."""

    def annotate(self, uid: str, key: str, value: Any) -> None: ...
    def pin(self, uid: str, reason: str) -> None: ...
    def layout_order(self, section: LayoutSection, uids: tuple[str, ...] | list[str]) -> None: ...
    def place(self, uid: str, tb_offset: int) -> None: ...
    # B3/B4: defined with CacheRegion v1 types; the B2 manager rejects them.
    def clone(self, *args: Any, **kwargs: Any) -> None: ...
    def redirect_exit(self, *args: Any, **kwargs: Any) -> None: ...
    def add_entry(self, *args: Any, **kwargs: Any) -> None: ...
    def pure_rewrite(self, *args: Any, **kwargs: Any) -> None: ...
    def add_region(self, *args: Any, **kwargs: Any) -> None: ...
    def add_recovery(self, *args: Any, **kwargs: Any) -> None: ...
    def inline_call(self, payload: InlineCall) -> None: ...
    def attach_fact(self, *args: Any, **kwargs: Any) -> None: ...


class Pass(Protocol):
    descriptor: ClassVar[PassDescriptor]

    def run(self, unit: UnitView | SnapshotView, ctx: PassContext,
            edits: ProposalBuilderAPI) -> PassResult: ...


# ===========================================================================
# B3/B4 typed EditKind payloads (design §3.7; frozen at WP18's first merge).
# Changes only through a design-doc edit reviewed by every dependent WP.
# ===========================================================================

import re as _re

from ..ids import TBKey as _TBKey

SLOT_KINDS = ("GENERAL", "SPEC")
EXIT_CLASSES = ("return", "static")
#: PURE_REWRITE template ids a pass may use (V15 checks the pass/template pair)
SCALAR_TEMPLATES = ("scalar_repeat_nop", "scalar_test_nop", "scalar_mask_nop", "scalar_forward_nop",
                    "scalar_range_branch", "scalar_expression_nop")

PATCH_TEMPLATES = {
    "fold-exit-pc-update": ("pc_update_mem_add",),
    "irq-entry-mem-compare": ("irq_entry_mem_compare",),
    "region-state-forward": ("region_same_store",),
    "region-state-dse": ("b4_nop_store",),
    "native-scalar-cleanup": SCALAR_TEMPLATES,
    "local-scalar-cleanup": SCALAR_TEMPLATES,
    "constant-propagation": ("cp_xor_zero", "cp_mov_imm"),
    "branch-folding": ("bf_jcc_to_jmp", "bf_jcc_to_nop"),
    "dead-host-ops": ("dho_nop",),
    "eliminate-state-sync": ("b4_nop_store", "b4_nop_pair", "b4_absorb_nop"),
}
_SHA = _re.compile(r"^sha256:[0-9a-f]{64}$")
_FIELDS = tuple(f"r{i}" for i in range(13)) + ("NF", "ZF", "CF", "VF")


class PayloadError(ValueError):
    """A malformed payload (``reason`` is ``edit_precondition``)."""

    reason = "edit_precondition"


def _chk(ok: bool, msg: str) -> None:
    if not ok:
        raise PayloadError(msg)


def _u32(v: Any, what: str) -> None:
    _chk(isinstance(v, int) and not isinstance(v, bool) and 0 <= v < (1 << 32),
         f"{what} must be a u32, got {v!r}")


@dataclass(frozen=True)
class GuardAtom:
    """One guard atom (CacheRegion ``GuardAtom``, §3.9)."""

    atom: str  # g_bp | g_thumb | g_cond | g_flags | g_flags2 | g_pc
    env_field: str  # EnvLayout element name
    width: int  # 1 | 4 | 8
    op: str  # eq | zero
    value: int

    def __post_init__(self) -> None:
        _chk(self.atom in ("g_bp", "g_thumb", "g_cond", "g_flags", "g_flags2", "g_pc", "g_r0_zero"),
             f"unknown guard atom {self.atom!r}")
        _chk(self.width in (1, 4, 8), f"atom width {self.width}")
        _chk(self.op in ("eq", "zero"), f"atom op {self.op!r}")

    def to_json(self) -> dict[str, Any]:
        return {"atom": self.atom, "env_field": self.env_field, "width": self.width,
                "op": self.op, "value": hex(self.value)}


@dataclass(frozen=True)
class FactRef:
    """A certified return-edge fact (§3.2, §3.9 ``FactRef``)."""

    fact_id: str
    field: str  # r0..r12, NF, ZF, CF, VF
    value: int  # 32-bit
    def_insn: int
    source: str  # patch | guest_code
    cert_hash: str
    summary_row_hash: str

    def __post_init__(self) -> None:
        _chk(bool(_SHA.match(self.fact_id)), "fact_id must be a sha256 id")
        _chk(self.field in _FIELDS, f"fact field {self.field!r}")
        _u32(self.value, "fact value")
        _chk(self.source in ("patch", "guest_code", "guard"), f"fact source {self.source!r}")
        _chk(self.source != "guard" or (self.field == "r0" and self.value == 0),
             "guard fact must be r0 == 0")
        _chk(bool(_SHA.match(self.cert_hash)) and bool(_SHA.match(self.summary_row_hash)),
             "cert_hash/summary_row_hash must be sha256 ids")

    def to_json(self) -> dict[str, Any]:
        return {"fact_id": self.fact_id, "field": self.field, "value": hex(self.value),
                "def_insn": self.def_insn, "source": self.source, "cert_hash": self.cert_hash,
                "summary_row_hash": self.summary_row_hash}


@dataclass(frozen=True)
class GuardedSlot:
    """REDIRECT_EXIT (11): a guarded slot on exit ``exit_id`` of unit ``uid``."""

    uid: str
    exit_id: str  # region exit id of the lookup tail (x<k>)
    slot_n: int
    kind: str  # GENERAL | SPEC
    exit_class: str  # return | static
    target_vaddr: int
    key_C: _TBKey
    guard_template_id: str = "guard-v1"
    variant_ref: str | None = None  # variant_id (SPEC only)
    tail_off: int | None = None  # host offset T of the tail (resolved from exit_id if None)

    def __post_init__(self) -> None:
        _chk(self.slot_n in (0, 1), f"slot_n {self.slot_n}")
        _chk(self.kind in SLOT_KINDS, f"slot kind {self.kind!r}")
        _chk(self.exit_class in EXIT_CLASSES, f"exit class {self.exit_class!r}")
        _u32(self.target_vaddr, "target_vaddr")
        _chk(not self.target_vaddr & 1, "target_vaddr must have the Thumb bit clear")
        _chk((self.kind == "SPEC") == (self.variant_ref is not None),
             "variant_ref is required for SPEC and forbidden for GENERAL")
        _chk(isinstance(self.key_C, _TBKey), "key_C must be a TBKey")

    def to_json(self) -> dict[str, Any]:
        k = self.key_C
        return {"uid": self.uid, "exit_id": self.exit_id, "slot_n": self.slot_n,
                "kind": self.kind, "exit_class": self.exit_class,
                "target_vaddr": hex(self.target_vaddr),
                "key_C": {"page_addr0": hex(k.page_addr0),
                          "page_addr1": None if k.page_addr1 is None else hex(k.page_addr1),
                          "flags": hex(k.flags), "cs_base": hex(k.cs_base),
                          "cflags": hex(k.cflags)},
                "guard_template_id": self.guard_template_id, "variant_ref": self.variant_ref,
                "tail_off": self.tail_off}


@dataclass(frozen=True)
class Clone:
    """CLONE (10): variant ``variant_id`` of ``src_uid`` assuming ``facts``."""

    src_uid: str
    variant_id: str
    facts: tuple[FactRef, ...] = ()

    def __post_init__(self) -> None:
        _chk(bool(_SHA.match(self.variant_id)), "variant_id must be a sha256 id")
        _chk(all(isinstance(f, FactRef) for f in self.facts), "facts must be FactRef")

    def to_json(self) -> dict[str, Any]:
        return {"src_uid": self.src_uid, "variant_id": self.variant_id,
                "facts": [f.to_json() for f in self.facts]}


@dataclass(frozen=True)
class SpecEntry:
    """ADD_ENTRY (12): the one predecessor slot of a variant and its precondition."""

    variant_id: str
    pred: tuple[str, int]  # (R uid, slot_n)
    guard_atoms: tuple[GuardAtom, ...] = ()
    facts: tuple[FactRef, ...] = ()

    def __post_init__(self) -> None:
        _chk(bool(_SHA.match(self.variant_id)), "variant_id must be a sha256 id")
        _chk(isinstance(self.pred, tuple) and len(self.pred) == 2 and self.pred[1] in (0, 1),
             "pred must be (uid, slot_n)")

    def to_json(self) -> dict[str, Any]:
        return {"variant_id": self.variant_id, "pred": [self.pred[0], self.pred[1]],
                "precondition": {"guard_atoms": [a.to_json() for a in self.guard_atoms],
                                 "facts": [f.to_json() for f in self.facts]}}


@dataclass(frozen=True)
class Patch:
    """PURE_REWRITE (13): same-length bytes at ``host_off`` of a unit or a variant."""

    target: str  # UnitId or variant_id
    host_off: int
    old_bytes: bytes
    new_bytes: bytes
    template_id: str
    justification: str = ""

    def __post_init__(self) -> None:
        _chk(isinstance(self.host_off, int) and self.host_off >= 0, "host_off")
        _chk(len(self.old_bytes) == len(self.new_bytes) and len(self.old_bytes) > 0,
             "PURE_REWRITE must be same-length and non-empty")
        _chk(any(self.template_id in v for v in PATCH_TEMPLATES.values()),
             f"unknown patch template {self.template_id!r}")

    def to_json(self) -> dict[str, Any]:
        return {"target": self.target, "host_off": self.host_off,
                "old_bytes": bytes(self.old_bytes).hex(), "new_bytes": bytes(self.new_bytes).hex(),
                "template_id": self.template_id, "justification": self.justification}


PAYLOAD_KINDS = {
    EditKind.REDIRECT_EXIT: GuardedSlot,
    EditKind.CLONE: Clone,
    EditKind.ADD_ENTRY: SpecEntry,
    EditKind.ATTACH_FACT: FactRef,
    EditKind.PURE_REWRITE: Patch,
}


class B3Reason:
    """Stable B3/B4 reason codes (design §3.8, §7.5 and the WP17/WP18 additions).
    Each gives ``skipped_unsupported``/``skipped_unknown`` and keeps the
    baseline, except :data:`FAILED` codes (a candidate bug, spec:47)."""

    # exit admission
    EXIT_NOT_GOTO_PTR = "exit_not_goto_ptr"
    TAIL_IS_BRANCH_TARGET = "tail_is_branch_target"
    HANDLER_MODE_RETURN = "handler_mode_return"
    PC_MODIFIER_EXIT = "pc_modifier_exit"
    INDIRECT_MULTIWAY = "indirect_multiway"
    # unit admission
    SPECIAL_CFLAGS_UNIT = "special_cflags_unit"
    TWO_PAGE_TARGET = "two_page_target"
    RAM_RESIDENT = "ram_resident"
    NO_META = "no_meta"
    UNIT_NOT_RELOCATABLE = "unit_not_relocatable"
    NO_FREE_SLOT = "no_free_slot"
    # edge admission
    TARGET_NOT_PROFILED = "target_not_profiled"
    CONTINUATION_KEY_MISMATCH = "continuation_key_mismatch"
    CONTINUATION_NOT_IN_SNAPSHOT = "continuation_not_in_snapshot"
    # facts
    FACT_NOT_CONSTANT = "fact_not_constant"
    FACT_KILLED_BY_HELPER = "fact_killed_by_helper"
    FACT_NOT_LOCAL_TO_RETURN_TB = "fact_not_local_to_return_tb"
    FACT_FROM_PROFILE_ONLY = "fact_from_profile_only"
    PATCH_JOIN_MISMATCH = "patch_join_mismatch"
    PROVENANCE_UNEXPLAINED = "provenance_unexplained"
    # simplification
    FACT_UNUSED = "fact_unused"
    NO_SIMPLIFICATION = "no_simplification"
    NO_SAME_LENGTH_ENCODING = "no_same_length_encoding"
    FLAGS_LIVE = "flags_live"
    # policy
    SLOT_BUDGET = "slot_budget"
    CONTEXT_BUDGET = "context_budget"
    GROWTH_BUDGET = "growth_budget"
    PATH_BUDGET_EXHAUSTED = "path_budget_exhausted"
    # placement
    EDIT_IN_PREFIX = "edit_in_prefix"
    EDIT_INSIDE_LDST_BLOCK = "edit_inside_ldst_block"
    EDIT_IN_EXIT_STUB = "edit_in_exit_stub"
    EDIT_ON_INSN_BOUNDARY = "edit_on_insn_boundary"
    EDIT_INSIDE_LOG_BLOCK = "edit_inside_log_block"  # close-out: the coverage LOG_REG block
    # runtime identity
    GUARD_LAYOUT_UNBOUND = "guard_layout_unbound"
    UNSUPPORTED_GUEST = "unsupported_guest"
    OBSERVER_CONTRACT_UNKNOWN = "observer_contract_unknown"
    # validator
    VALIDATION_INCONCLUSIVE = "validation_inconclusive"
    VALIDATION_MISMATCH = "validation_mismatch"
    FACT_DERIVATION_MISMATCH = "fact_derivation_mismatch"
    GUARD_CONTRACT_VIOLATION = "guard_contract_violation"
    NOT_PROFITABLE = "not_profitable"
    UNSUPPORTED_CONTRACT_FACT_SOURCE = "unsupported_contract_fact_source"
    NO_PATCH_BINDINGS = "no_patch_bindings"
    STALE_FACT = "stale_fact"
    CAPABILITY_NOT_ENABLED = "capability_not_enabled"
    # WP17 additions (V20 / identity)
    TB_START_CB_CONTRADICTED = "tb_start_cb_contradicted"
    TB_START_CB_UNCHECKED = "tb_start_cb_unchecked"
    AC4_EXCLUDED_VIRTUAL = "ac4_excluded_virtual"
    # WP18 additions (packaging / ext table)
    EXT_TABLE_TOO_LARGE = "ext_table_too_large"
    CERTIFICATION_NOT_RUN = "certification_not_run"  # V12/V14/V18 did not run (fail closed)
    MULTI_TB_REGION = "multi_tb_region"

    FAILED = frozenset({"validation_mismatch", "fact_derivation_mismatch",
                        "guard_contract_violation",
                        "multi_tb_region"})  # internal inconsistency = tool bug (spec:47)
    UNKNOWN = frozenset({"validation_inconclusive", "path_budget_exhausted",
                         "observer_contract_unknown", "certification_not_run"})

    @classmethod
    def all(cls) -> frozenset[str]:
        return frozenset(v for k, v in vars(cls).items() if k.isupper() and isinstance(v, str))

    @classmethod
    def outcome(cls, code: str) -> "Outcome":
        if code in cls.FAILED:
            return Outcome.FAILED
        if code in cls.UNKNOWN:
            return Outcome.SKIPPED_UNKNOWN
        return Outcome.SKIPPED_UNSUPPORTED

"""B3/B4 part of the PassManager (design §3.4-3.9, §10.3; plan ``## WP21``). WP21.

The B2 :class:`~scopt.model.snapshot.WorkingSet` only knows layout edits
(it refuses B3 kinds). B3/B4 edits live in an immutable overlay,
:class:`B3State`, that the manager carries next to the WorkingSet:

* **exits** ``(R uid, tail offset) -> ExitPlan``: the guarded exit of a grown
  unit R with its 1-2 targets (slot, ``Vc``, ``GENERAL``/``SPEC``, the
  continuation C, the variant for SPEC, the pass that claimed the slot);
* **variants** ``clone id -> VariantPlan``: a clone ``C'`` of C entered only
  through one predecessor slot, its facts and guard atoms, and its *current*
  bytes (C's bytes with every PURE_REWRITE applied so far, in pipeline order);
* **rewrites** ``uid -> patches``: same-length rewrites of ordinary units
  (B4, WP25);
* **facts** ``uid -> FactRefs`` (ATTACH_FACT).

``B3State.apply(pass, edits)`` checks the §3.7 manager preconditions of one
pass's proposal as a whole and returns the next state, or a refusal
``(code, detail)`` with a §3.8 code; the manager then discards the proposal
and records ``B3Reason.outcome(code)`` (``skipped_unsupported`` /
``skipped_unknown``; ``stale_fact`` and malformed proposals are ``failed``).

Region scope (§3.6): a ``unit_scope = "region"`` pass runs once per variant
of the overlay, on a :class:`VariantView` (the variant's current bytes and
C's metadata), and may edit the clone created by an earlier pass ("edits on
clones").

:func:`materialize` turns the final overlay into WP18's
:class:`~scopt.link.B3Plan`: per variant the composed patches relative to
C's captured bytes (one patch per original host instruction that changed,
so a DHO nop of CP's ``xor`` composes with CP's patch), the variant id
``sha256(C.uid, R.uid, E, sorted(F), patch set)`` (§3.4), and the
**downgrade rule**: a variant whose bytes equal ``C`` becomes a GENERAL
slot to the general C (``no_simplification``). One manager-generated
``ADD_REGION`` record per remaining SPEC link (§3.9); the region rows are
written by WP18's :func:`scopt.validate.multi_tb_rows` at packaging.

:func:`optimize_b3` is PassManager -> materialize -> WP18's
:func:`scopt.validate.package_b3` with WP19's hostsym inputs (fail closed:
V12/V14/V18 must certify). A candidate without any B3/B4 edit goes through
the B1/B2 :class:`~scopt.validate.ValidateAndPackage` instead, so V9 byte
identity applies unchanged (design §9.6 "Identity", T-B3-7).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from .analysis.api import ScopeRef
from .passes.base import (B3Reason, EditKind, FactRef, GuardAtom, GuardedSlot, Outcome,
                          Patch, SpecEntry)

__all__ = [
    "B3Env", "B3Ctx", "B3State", "ExitPlan", "SlotPlan", "VariantPlan", "VariantView",
    "B3WorkingSetView", "B3Refusal", "materialize", "Materialized", "optimize_b3",
    "LINK_FAILED_CODES", "b3_reject_outcome", "analysis_unit", "variant_weight",
    "stub_bytes_estimate", "hot_set_bytes",
]

#: NativeLinker v2 / packaging reasons that can only come from an internally
#: inconsistent plan (a manager or pass bug): ``failed`` (spec:47), never
#: skipped. WP18 review minor: they are not ``B3Reason`` members, so
#: ``B3Reason.outcome`` would have called them ``skipped_unsupported``.
LINK_FAILED_CODES = frozenset({
    "link_missing", "variant_pred_slot", "unknown_unit", "patch_length", "patch_old_bytes",
    "patch_overlap", "edit_on_fixup", "edit_in_tail", "spec_slot_unregistered", "slot_kind",
    "multi_tb_region", "reimport_failed", "write_sc", "rel32_range",
})


def b3_reject_outcome(reason: str | None) -> Outcome:
    """Outcome of a packaging rejection ``reason`` (``failed:<code>``, ``V12:<code>``, a bare
    code): §3.8 classes via :meth:`B3Reason.outcome`, plus :data:`LINK_FAILED_CODES`."""
    if not reason:
        return Outcome.FAILED
    code = reason.split(":", 1)[-1] if ":" in reason else reason
    code = code.split(";")[0].strip()
    if code in LINK_FAILED_CODES or code in B3Reason.FAILED:
        return Outcome.FAILED
    if code in B3Reason.UNKNOWN:
        return Outcome.SKIPPED_UNKNOWN
    if code in B3Reason.all():
        return Outcome.SKIPPED_UNSUPPORTED
    # V-row failures of a structural validator (V1-V9, V11, V13, V15-V17, V19, V23) are
    # tool bugs for a plan the manager admitted
    return Outcome.FAILED


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------

@dataclass
class B3Env:
    """External inputs of a B3/B4 run (all optional; missing inputs fail closed).

    ``layout``: EnvLayout of the snapshot's exe (default: the store's ``envlayout@1``).
    ``runtime``/``v6_exes``: the RuntimeIdentity the artifact targets (V7/V20).
    ``selftest``: the V18 hostsym self-test document. ``calibration``: optional §10.3
    costs overriding workspace calibration. Pipeline and caller pass parameters
    take precedence. ``certify``: run WP19's V14
    certifier on the CalleeSummary facts (without it no fact is citable).
    """

    layout: Any = None
    runtime: Mapping[str, Any] | None = None
    v6_exes: tuple[str, ...] = ()
    selftest: Mapping[str, Any] | None = None
    calibration: Mapping[str, float] | None = None
    certify: bool = True
    hot_set_coverage: float = 0.99


class B3Ctx:
    """Per-run B3 inputs shared by the manager and the passes (``ctx.b3``)."""

    def __init__(self, env: B3Env, store: Any):
        self.env = env
        self.store = store
        self.snapshot = store.view.working_set.base
        self.code_address = self.snapshot.header.code_address
        self.page_bits = self.snapshot.header.target_page_bits
        sid = store.view.snapshot_id
        self._sref = ScopeRef("snapshot", sid)
        lay_doc = None
        try:
            lay_doc = store.get("envlayout", self._sref).value
        except Exception:  # noqa: BLE001 - no layout: unbound
            lay_doc = None
        self.layout = env.layout if env.layout is not None else \
            (lay_doc or {}).get("layout")
        self.layout_bound = bool(env.layout is not None or (lay_doc or {}).get("bound"))
        self.layout_reason = None if self.layout_bound else \
            ((lay_doc or {}).get("reason") or "guard_layout_unbound")
        from .guard import GuardError, GuardOffsets

        try:
            self.offsets = GuardOffsets.from_layout(self.layout)
        except GuardError:
            self.offsets = None
            self.layout_bound = False
            self.layout_reason = self.layout_reason or "guard_layout_unbound"
        self._rows: list[dict[str, Any]] | None = None
        self._summary_inputs: tuple[str, ...] = ()
        self._cert_tally: Counter = Counter()
        self._units = {u.uid: u for u in self.snapshot.units()}

    # -- snapshot access ------------------------------------------------------
    def unit(self, uid: str) -> Any:
        return self._units[uid]

    def units(self) -> Mapping[str, Any]:
        return self._units

    def hostops(self, uid: str) -> Any:
        return self.store.get("hostops", ScopeRef("unit", uid)).value

    def hcfg(self, uid: str) -> Any:
        return self.store.get("hcfg", ScopeRef("unit", uid)).value

    def edges(self) -> Any:
        return self.store.get("return-edges", self._sref).value

    # -- certified CalleeSummary rows (V14 certifier, WP19) -------------------
    def summaries(self) -> list[dict[str, Any]]:
        """CalleeSummary v1 rows of every return unit, facts certified by hostsym (V14);
        computed once per run. Without a bound layout or with ``certify=False`` the rows
        are WP20's proposed rows (no fact is citable)."""
        from .analysis.global_values import EFFECT_INPUT

        prefix = f"external:{EFFECT_INPUT}:"
        inputs = tuple(sorted(i for i in self.store.inputs() if i.startswith(prefix)))
        if self._rows is not None and self._summary_inputs == inputs:
            return self._rows
        self._summary_inputs = inputs
        from .analysis.b3_inputs import guest_image_for, patch_plan_for
        from .analysis.callee_summary import build_callee_summaries, guarded_static_summaries

        s = self.snapshot
        certify: Any = None
        if self.env.certify and self.layout is not None:
            from .validate_sym.link_check import summary_certifier

            inner = summary_certifier(s, self.layout)
            tally = self._cert_tally

            def _certify(u, es, f):  # noqa: ANN001
                r = inner(u, es, f)
                tally[{True: "certified", False: "fact_derivation_mismatch"}.get(
                    r[0], "inconclusive")] += 1
                return r
            certify = _certify
        bundle = s.bundle
        self._rows = build_callee_summaries(
            tuple(self._units.values()), self.edges(), snapshot_id=s.snapshot_id,
            hostops_of=lambda u: self.hostops(u.uid),
            facts_of=lambda u: self.store.get("exit-facts", ScopeRef("unit", u.uid)).value,
            hcfg_of=lambda u: self.hcfg(u.uid), guest=guest_image_for(s, bundle),
            plan=patch_plan_for(s, bundle), bundle=bundle, certify=certify,
            page_bits=self.page_bits)
        if self.offsets is not None and self.offsets.r0 is not None:
            self._rows = guarded_static_summaries(
                self._rows, self.edges(), lambda uid: self.store.get("local-summary", ScopeRef("unit", uid)),
                offset=self.offsets.r0)
        return self._rows

    @property
    def certifier_tally(self) -> dict[str, int]:
        return dict(self._cert_tally)

    def summary_row(self, uid: str, exit_id: str) -> dict[str, Any] | None:
        return next((r for r in self.summaries() if r["return_unit"] == uid and
                     r["exit_id"] == exit_id), None)


# ---------------------------------------------------------------------------
# overlay
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SlotPlan:
    slot_n: int
    vaddr: int
    kind: str  # GENERAL | SPEC
    c_uid: str | None
    variant: str | None
    owner: str
    weight: int = 0  # train lookup_hit count of the edge
    r0_zero: bool = False
    compact: bool = False


@dataclass(frozen=True)
class ExitPlan:
    uid: str
    exit_id: str
    tail_off: int
    exit_class: str
    key_flags: int
    key_cs_base: int
    slots: tuple[SlotPlan, ...] = ()
    total_weight: int = 0  # N_E: every lookup edge of the exit (train)


@dataclass(frozen=True)
class VariantPlan:
    clone_id: str
    src_uid: str
    pred: tuple[str, int]
    exit_id: str
    facts: tuple[FactRef, ...]
    guard_atoms: tuple[GuardAtom, ...]
    code: bytes  # current C' code bytes [0, tc_size)
    patches: tuple[tuple[str, Patch], ...] = ()
    owner: str = ""
    weight: int = 0  # train entries of C' (the R -> C edge count)

    @property
    def fact_map(self) -> dict[str, int]:
        return {f.field: f.value for f in self.facts}


@dataclass(frozen=True)
class B3Refusal:
    code: str
    detail: str = ""

    @property
    def outcome(self) -> Outcome:
        if self.code in ("edit_precondition", "stale_fact", "conflict", "unknown_unit"):
            return Outcome.FAILED
        return B3Reason.outcome(self.code)


def stub_bytes_estimate(n_targets: int, r0_zero: bool = False) -> int:
    """§4.1 size estimate of one guard stub (compares + per-target compare/slot + F)."""
    return 56 + 24 * n_targets + 11 + (13 if r0_zero else 0)


@dataclass(frozen=True)
class B3State:
    exits: Mapping[tuple[str, int], ExitPlan] = field(
        default_factory=lambda: MappingProxyType({}))
    variants: Mapping[str, VariantPlan] = field(default_factory=lambda: MappingProxyType({}))
    rewrites: Mapping[str, tuple[tuple[str, Patch], ...]] = field(
        default_factory=lambda: MappingProxyType({}))
    unit_code: Mapping[str, bytes] = field(default_factory=lambda: MappingProxyType({}))
    facts: Mapping[str, tuple[FactRef, ...]] = field(default_factory=lambda: MappingProxyType({}))

    max_code_growth_bytes: int | None = None
    max_variants_per_c: int = 2
    max_variants: int = 64

    # -- queries --------------------------------------------------------------
    @property
    def empty(self) -> bool:
        return not (self.exits or self.variants or self.rewrites)

    def grown_uids(self) -> frozenset[str]:
        return frozenset(u for u, _ in self.exits)

    def claimed_slots(self, uid: str) -> frozenset[int]:
        return frozenset(s.slot_n for (u, _), x in self.exits.items() if u == uid
                         for s in x.slots)

    def exit_plan(self, uid: str, tail_off: int) -> ExitPlan | None:
        return self.exits.get((uid, tail_off))

    def variant_sources(self) -> frozenset[str]:
        return frozenset(v.src_uid for v in self.variants.values())

    def variants_of(self, src_uid: str) -> list[VariantPlan]:
        return [v for v in self.variants.values() if v.src_uid == src_uid]

    def growth_bytes(self, units: Mapping[str, Any]) -> int:
        """Specialization growth (§3.4 ``max_code_growth_bytes``): variant bytes plus the
        stubs of exits that carry a SPEC slot. GENERAL-only stubs (chain-returns,
        chain-static) have their own budget (≤ 2 slots/unit, 256 units, §3.6) and are
        reported by V21, not budgeted here."""
        g = sum(stub_bytes_estimate(len(x.slots), any(s.r0_zero for s in x.slots)) for x in self.exits.values()
                if any(s.kind == "SPEC" for s in x.slots))
        for v in self.variants.values():
            c = units[v.src_uid]
            g += c.end_offset - c.tb_offset
        return g

    def current_code(self, target: str, units: Mapping[str, Any]) -> bytes:
        if target in self.variants:
            return self.variants[target].code
        return self.unit_code.get(target, units[target].code)

    # -- apply one pass's proposal ---------------------------------------------
    def apply(self, pass_name: str, edits: Sequence[Any], b3: "B3Ctx", *,
              max_per_c: int = 2, max_variants: int = 64,
              max_growth: int | None = None) -> "tuple[B3State | None, B3Refusal | None]":
        """Check §3.7's manager preconditions for the B3/B4 edits of one pass
        and return the next overlay (or a refusal; the proposal is then dropped)."""
        from .guard import GuardError, guard_atoms, guard_key
        from .link import free_slots, lookup_tails, tail_problem

        units = b3.units()
        exits = dict(self.exits)
        variants = dict(self.variants)
        rewrites = {k: tuple(v) for k, v in self.rewrites.items()}
        unit_code = dict(self.unit_code)
        facts = {k: tuple(v) for k, v in self.facts.items()}
        by_kind: dict[EditKind, list[Any]] = {}
        for e in edits:
            by_kind.setdefault(e.kind, []).append(e)

        def refuse(code: str, detail: str = "") -> tuple[None, B3Refusal]:
            return None, B3Refusal(code, detail)

        clones = {e.payload.variant_id: e.payload for e in by_kind.get(EditKind.CLONE, ())}
        entries: dict[str, SpecEntry] = {}
        for e in by_kind.get(EditKind.ADD_ENTRY, ()):
            p: SpecEntry = e.payload
            if p.variant_id in entries:
                return refuse("edit_precondition", f"two ADD_ENTRY for variant {p.variant_id}")
            entries[p.variant_id] = p
        attached: dict[str, set[str]] = {}
        row_of = {r.get("row_hash"): r for r in b3.summaries()} if \
            by_kind.get(EditKind.ATTACH_FACT) else {}
        fact_exit: dict[str, tuple[str, str]] = {}
        for e in by_kind.get(EditKind.ATTACH_FACT, ()):
            f: FactRef = e.payload
            try:
                from .formats.callee_summary_v1 import check_factref

                fr = check_factref(f.to_json(), b3.summaries())
            except Exception as ex:  # noqa: BLE001 - StaleFact or a malformed row
                return refuse("stale_fact", str(ex)[:300])
            row = row_of.get(f.summary_row_hash)
            if (fr.get("field") != f.field or fr.get("source") != f.source
                    or int(str(fr.get("value")), 0) != f.value):
                return refuse("stale_fact", f"{f.fact_id}: field/value differ from the row")
            if row is None or tuple(e.units) != (row["return_unit"],):
                return refuse("edit_precondition", f"fact {f.fact_id} attached to {e.units}, "
                              f"its row is {None if row is None else row['return_unit']}")
            fact_exit[f.fact_id] = (row["return_unit"], row["exit_id"])
            for u in e.units:
                attached.setdefault(u, set()).add(f.fact_id)
                facts[u] = tuple(dict.fromkeys(facts.get(u, ()) + (f,)))
        # ---- CLONE ---------------------------------------------------------------
        for vid, c in clones.items():
            if vid in variants:
                return refuse("edit_precondition", f"variant {vid} cloned twice")
            src = units.get(c.src_uid)
            if src is None:
                return refuse("unknown_unit", c.src_uid)
            if src.meta_status != "ok":
                return refuse("no_meta", c.src_uid)
            if not src.reloc.relocatable:
                return refuse("unit_not_relocatable", c.src_uid)
            if src.origin == "ram_resident":
                return refuse("ram_resident", c.src_uid)
            if c.src_uid in self.grown_uids() or any(
                    e.payload.uid == c.src_uid for e in by_kind.get(EditKind.REDIRECT_EXIT, ())):
                # WP19 integration note item 4: a clone of a grown C carries C's registered
                # GENERAL reset (exit_tb(C'|n)); the loader's variant struct is not known to
                # register it. Fail closed: variants are clones of un-grown units only.
                return refuse("unit_not_relocatable", f"{c.src_uid}: variant source is grown")
            ent = entries.get(vid)
            if ent is None:
                return refuse("edit_precondition", f"CLONE {vid} without ADD_ENTRY")
            for fr in c.facts:
                if fr.fact_id not in attached.get(ent.pred[0], set()):
                    return refuse("edit_precondition",
                                  f"variant fact {fr.fact_id} not attached to {ent.pred[0]}")
            n_c = sum(1 for v in variants.values() if v.src_uid == c.src_uid)
            if n_c + 1 > max_per_c:
                return refuse("context_budget", f"more than {max_per_c} variants of {c.src_uid}")
            if len(variants) + 1 > max_variants:
                return refuse("context_budget", f"more than {max_variants} variants")
            gs = next((e.payload for e in by_kind.get(EditKind.REDIRECT_EXIT, ())
                       if e.payload.variant_ref == vid), None)
            if gs is None or (gs.uid, gs.slot_n) != tuple(ent.pred):
                return refuse("edit_precondition",
                              f"variant {vid}: ADD_ENTRY pred {ent.pred} has no SPEC "
                              "REDIRECT_EXIT in this proposal")
            if set(ent.facts) != set(c.facts):
                return refuse("edit_precondition", f"variant {vid}: CLONE and ADD_ENTRY facts "
                              "differ")
            for fr in c.facts:  # every fact is a fact of the predecessor's own exit (§3.2)
                if fact_exit.get(fr.fact_id) != (gs.uid, gs.exit_id):
                    return refuse("edit_precondition", f"variant {vid}: fact {fr.fact_id} is "
                                  f"not a fact of {gs.uid}/{gs.exit_id}")
            # NativeLinker v2 clones the *effective* C (C with its own rewrites, e.g. B4)
            variants[vid] = VariantPlan(vid, c.src_uid, (ent.pred[0], ent.pred[1]), gs.exit_id,
                                        tuple(c.facts), tuple(ent.guard_atoms),
                                        bytes(unit_code.get(c.src_uid, src.code)), (),
                                        pass_name)
        for vid in entries:
            if vid not in clones:
                return refuse("edit_precondition", f"ADD_ENTRY for a variant not cloned: {vid}")
        # ---- REDIRECT_EXIT ---------------------------------------------------------
        for e in by_kind.get(EditKind.REDIRECT_EXIT, ()):
            gs: GuardedSlot = e.payload
            u = units.get(gs.uid)
            if u is None:
                return refuse("unknown_unit", gs.uid)
            if not b3.layout_bound or b3.offsets is None:
                return refuse("guard_layout_unbound", b3.layout_reason or "")
            if u.meta_status != "ok":
                return refuse("no_meta", gs.uid)
            if not u.reloc.relocatable:
                return refuse("unit_not_relocatable", gs.uid)
            if u.origin == "ram_resident":
                return refuse("ram_resident", gs.uid)
            if gs.uid in self.variant_sources() or gs.uid in {c.src_uid for c in clones.values()}:
                return refuse("unit_not_relocatable", f"{gs.uid}: unit is a variant source")
            tails = {t.off: t for t in lookup_tails(u)}
            t = tails.get(gs.tail_off) if gs.tail_off is not None else None
            if t is None:
                return refuse("exit_not_goto_ptr", f"{gs.uid}: no lookup tail at {gs.tail_off}")
            prob = tail_problem(u, t, b3.code_address)
            if prob:
                return refuse(prob, gs.uid)
            row = next((x for x in b3.edges().exits if x.uid == gs.uid and
                        x.exit_id == gs.exit_id), None)
            if row is None or row.tail_off != gs.tail_off:
                return refuse("exit_not_goto_ptr", f"{gs.uid}/{gs.exit_id}: no return-edges row")
            if not row.admitted:
                return refuse(row.reason_codes[0], f"{gs.uid}/{gs.exit_id}")
            if row.exit_class != gs.exit_class:
                return refuse("indirect_multiway",
                              f"{gs.uid}: exit class {row.exit_class} != {gs.exit_class}")
            if gs.slot_n not in free_slots(u):
                return refuse("no_free_slot", f"{gs.uid} slot {gs.slot_n} registered")
            key = (gs.uid, gs.tail_off)
            x = exits.get(key)
            if x is not None and gs.slot_n in {s.slot_n for s in x.slots}:
                return refuse("no_free_slot", f"{gs.uid} slot {gs.slot_n} already claimed")
            if gs.slot_n in {s.slot_n for (uu, _), xx in exits.items() if uu == gs.uid
                             for s in xx.slots}:
                return refuse("no_free_slot", f"{gs.uid} slot {gs.slot_n} already claimed")
            try:
                kk = guard_key(gs.key_C.flags, gs.key_C.cs_base)
            except GuardError as ex:
                return refuse("continuation_key_mismatch", ex.detail)
            if x is not None:
                if guard_key(x.key_flags, x.key_cs_base) != kk:
                    return refuse("continuation_key_mismatch",
                                  f"{gs.uid}: second target key differs from the first")
                if any(s.vaddr == gs.target_vaddr for s in x.slots):
                    return refuse("edit_precondition", f"{gs.uid}: Vc {gs.target_vaddr:#x} "
                                  "guarded twice")
            cands = {c.vaddr: c for c in row.candidates if c.admitted}
            cand = cands.get(gs.target_vaddr)
            if cand is None:
                return refuse("target_not_profiled", f"{gs.uid}: Vc {gs.target_vaddr:#x}")
            c_unit = units.get(cand.dst) if cand.dst else None
            if c_unit is None or c_unit.key != gs.key_C:
                return refuse("continuation_key_mismatch", f"{gs.uid}: key_C is not C's key")
            r0_zero = False
            if x is not None and any(s.r0_zero for s in x.slots):
                return refuse("slot_budget", f"{gs.uid}: a value guard uses the whole stub")
            if gs.kind == "SPEC":
                if gs.variant_ref not in clones:
                    return refuse("edit_precondition", f"SPEC slot of {gs.uid} without CLONE")
                if clones[gs.variant_ref].src_uid != c_unit.uid:
                    return refuse("continuation_key_mismatch",
                                  f"SPEC slot of {gs.uid}: variant is not a clone of C")

                r0_zero = any(f.source == "guard" for f in clones[gs.variant_ref].facts)
                if r0_zero and (x is not None or b3.offsets.r0 is None):
                    return refuse("slot_budget", f"{gs.uid}: requires a single bound value guard")
                if r0_zero:
                    expected = guard_atoms(b3.offsets, *kk, [gs.target_vaddr], r0_zero=True)
                    actual = [a.to_json() for a in entries[gs.variant_ref].guard_atoms]
                    if expected != actual:
                        return refuse("edit_precondition", f"{gs.uid}: value guard differs")
                from .link import GuardedExitSpec, spec_binding_problem
                from .guard import GuardTarget

                spec = GuardedExitSpec(gs.tail_off, gs.exit_class,
                                       (GuardTarget(gs.target_vaddr, gs.slot_n, "SPEC", r0_zero=r0_zero),),
                                       gs.key_C.flags, gs.key_C.cs_base)
                bp = spec_binding_problem(spec, gs.slot_n, c_unit)
                if bp is not None:
                    return refuse("continuation_key_mismatch", bp)
                variants[gs.variant_ref] = replace(variants[gs.variant_ref],
                                                   weight=int(cand.count))
            from .guard import COMPACT_GUARD
            sp = SlotPlan(gs.slot_n, gs.target_vaddr, gs.kind, c_unit.uid, gs.variant_ref,
                          pass_name, int(cand.count), r0_zero, gs.guard_template_id == COMPACT_GUARD)
            tot = sum(int(c.count) for c in row.candidates)
            if x is None:
                x = ExitPlan(gs.uid, gs.exit_id, gs.tail_off, gs.exit_class, gs.key_C.flags,
                             gs.key_C.cs_base, (sp,), tot)
            else:
                x = replace(x, slots=tuple(sorted(x.slots + (sp,), key=lambda s: s.slot_n)))
            exits[key] = x
        # ---- PURE_REWRITE ------------------------------------------------------------
        for e in by_kind.get(EditKind.PURE_REWRITE, ()):
            p: Patch = e.payload
            if p.target in variants:
                v = variants[p.target]
                src = units[v.src_uid]
                cur = v.code
            elif p.target in units:
                # a rewrite of an ordinary unit (B4): NativeLinker v2 applies it before
                # growing the unit, so grown units may be rewritten too (placement keeps
                # the patch out of the lookup tail); the guard stub is not code of src
                src = units[p.target]
                cur = unit_code.get(p.target, src.code)
            else:
                return refuse("edit_precondition", f"PURE_REWRITE of unknown {p.target}")
            prob = patch_placement(src, cur, p, b3)
            if prob is not None:
                return refuse(prob[0], f"{p.target} @{p.host_off:#x}: {prob[1]}")
            new = bytearray(cur)
            new[p.host_off:p.host_off + len(p.new_bytes)] = p.new_bytes
            if p.target in variants:
                variants[p.target] = replace(variants[p.target], code=bytes(new),
                                             patches=variants[p.target].patches +
                                             ((pass_name, p),))
            else:
                unit_code[p.target] = bytes(new)
                rewrites[p.target] = rewrites.get(p.target, ()) + ((pass_name, p),)
                # keep every clone of this unit equal to "effective C + its own patches"
                a, b = p.host_off, p.host_off + len(p.old_bytes)
                for vid2, v2 in list(variants.items()):
                    if v2.src_uid != p.target:
                        continue
                    if bytes(v2.code[a:b]) != bytes(p.old_bytes):
                        return refuse("conflict", f"{p.target} @{a:#x}: the rewrite overlaps "
                                      f"a patch of variant {vid2}")
                    vc = bytearray(v2.code)
                    vc[a:b] = p.new_bytes
                    variants[vid2] = replace(v2, code=bytes(vc))
        new_state = B3State(MappingProxyType(exits), MappingProxyType(variants),
                            MappingProxyType(rewrites), MappingProxyType(unit_code),
                            MappingProxyType(facts),
                            max_growth if clones else self.max_code_growth_bytes,
                            max_per_c if clones else self.max_variants_per_c,
                            max_variants if clones else self.max_variants)
        if max_growth is not None:
            g = new_state.growth_bytes(units)
            if g > max_growth and g > self.growth_bytes(units):
                return refuse("growth_budget", f"{g} > {max_growth} bytes")
        # every SPEC slot must have its variant; every variant its SPEC slot
        for vid, v in variants.items():
            ok = any(s.variant == vid and s.kind == "SPEC" and (x.uid, s.slot_n) == v.pred
                     for x in exits.values() for s in x.slots)
            if not ok:
                return refuse("edit_precondition", f"variant {vid} has no SPEC slot")
        return new_state, None


# ---------------------------------------------------------------------------
# placement (manager side of V15; the validator re-checks on the written file)
# ---------------------------------------------------------------------------

def patch_placement(src: Any, cur: bytes, p: Patch, b3: B3Ctx) -> tuple[str, str] | None:
    """Why patch ``p`` may not be applied to bytes ``cur`` of unit/clone ``src``
    (``(code, detail)``) or None. Old bytes must equal the current bytes; the
    span covers whole instructions of the current code and of the captured code
    at its ends, lies in one guest insn's ``insn`` block (no prefix except the B4
    D2 store, no LDST block, exit stub, pad, pool, guard stub), and touches no
    fixup field or lookup tail."""
    from .analysis.hostops import build_hostops
    from .link import lookup_tails

    a, b = p.host_off, p.host_off + len(p.old_bytes)
    if b > len(cur) or bytes(cur[a:b]) != bytes(p.old_bytes):
        return "edit_precondition", "old bytes do not match the current bytes"
    end = src.pool_start if src.pool_start is not None else len(src.code)
    if b > end:
        return "edit_in_exit_stub", "pool"
    if src.stub_start is not None and b > src.stub_start:
        return "edit_in_exit_stub", "guard stub"
    for f in src.reloc.fixups:
        if a < f.field_off + f.width and f.field_off < b:
            return "edit_in_exit_stub", "fixup field"
    for t in lookup_tails(src):
        if a < t.end and t.off < b:
            return "edit_in_exit_stub", "lookup tail"
    h = b3.hostops(src.uid) if src.uid in b3.units() else build_hostops(src, b3.layout,
                                                                         b3.code_address)
    if not h.ok:
        return "unit_not_relocatable", "hostops pinned"
    kinds = {bl.kind for bl in h.blocks if bl.start < b and a < bl.end}
    if "prefix" in kinds:
        d2 = p.template_id == "b4_nop_store" and any(
            o.off == a and o.end == b and o.is_env_store and o.store_field == "neg.can_do_io"
            for o in h.ops)
        if p.template_id == "irq_entry_mem_compare":
            from .analysis.irq_entry import matches_irq_patch
            from .link import tb_ret_abs_of
            if bytes(cur) != src.code or not matches_irq_patch(src, p, b3.code_address, tb_ret_abs_of(b3.snapshot)):
                return "edit_in_prefix", "interrupt entry proof failed"
        elif not d2:
            return "edit_in_prefix", ""
    if kinds & {"ldst_fast", "ldst_slow"}:
        return "edit_inside_ldst_block", ""
    if any(lb.overlaps(a, b) for lb in h.log_blocks):
        return "edit_inside_log_block", "coverage LOG_REG block"
    if kinds & {"exit_stub", "pad"}:
        return "edit_in_exit_stub", ",".join(sorted(kinds))
    starts = {o.off for o in h.ops}
    ends = {o.end for o in h.ops}
    if not any(o.off <= a < o.end for o in h.ops) or not any(o.off < b <= o.end for o in h.ops):
        return "edit_on_insn_boundary", "outside decoded ops"
    # whole instructions of the captured code at the span's ends, or of the current code
    cur_bounds = _boundaries(cur, end)
    if not ((a in starts or a in cur_bounds) and (b in ends or b in cur_bounds)):
        return "edit_on_insn_boundary", "patch does not cover whole host instructions"
    ins = [i for i in src.insns if i.host_start < b and a < i.host_end]
    if len(ins) > 1 or (ins and not (ins[0].host_start <= a and b <= ins[0].host_end)):
        return "edit_on_insn_boundary", "crosses a guest instruction boundary"
    return None


def _boundaries(code: bytes, end: int) -> frozenset[int]:
    from .analysis.hconst import _md

    out = set()
    off = 0
    while off < end:
        got = False
        for ci in _md().disasm(bytes(code[off:end]), off):
            out.add(ci.address)
            out.add(ci.address + ci.size)
            off = ci.address + ci.size
            got = True
        if not got:
            break
        if off < end:
            break
    return frozenset(out)


# ---------------------------------------------------------------------------
# region view (what a region-scope pass sees)
# ---------------------------------------------------------------------------

def analysis_unit(src: Any, code: bytes) -> Any:
    """``src`` with ``code`` and its branch sites adjusted to the patched bytes, for the
    passes' own analyses (hostops/hcfg/hlive/hconst on a variant's current bytes): a
    LABEL_JCC whose jcc became a ``jmp`` becomes a LABEL_JMP at the new rel field; one
    whose jcc became a nop is dropped. The validators never use this (they use C's
    metadata and the declared patches)."""
    from .analysis.hconst import decode_one
    from .formats.enums import Encoding, SiteKind

    if bytes(code) == bytes(src.code):
        return src
    changed = [i for i in range(min(len(code), len(src.code))) if code[i] != src.code[i]]
    if not changed:
        return replace(src, code=bytes(code))
    lo, hi = changed[0], changed[-1] + 1
    sites = []
    for s in src.sites:
        if s.kind in (SiteKind.LABEL_JCC, SiteKind.LABEL_JMP) and \
                s.insn_off < hi and lo <= s.field_off + s.width and \
                bytes(code[s.insn_off:s.field_off + s.width]) != \
                bytes(src.code[s.insn_off:s.field_off + s.width]):
            # find the instruction covering the old branch's field in the new bytes
            moved = None
            for start in range(s.insn_off, s.insn_off + 2):
                ci = decode_one(code, start, 15)
                if ci is None:
                    continue
                if ci.mnemonic == "jmp" and ci.operands and ci.operands[0].type == 2:
                    w = 4 if ci.size >= 5 else 1
                    fo = start + ci.size - w
                    tgt_rel = int.from_bytes(code[fo:fo + w], "little", signed=True)
                    moved = replace(s, kind=SiteKind.LABEL_JMP, insn_off=start, field_off=fo,
                                    width=w, enc=Encoding.REL32 if w == 4 else s.enc,
                                    final_field=tgt_rel & ((1 << (8 * w)) - 1))
                    break
            if moved is not None:
                sites.append(moved)
            continue
        sites.append(s)
    return replace(src, code=bytes(code), sites=tuple(sites))


@dataclass
class VariantView:
    """A variant ``C'`` of the overlay as a region-scope pass sees it."""

    variant_id: str  # clone id (stable through the pipeline)
    plan: VariantPlan
    src: Any  # the captured TBUnit C
    layout: Any
    code_address: int
    _cache: dict = field(default_factory=dict, repr=False)

    @property
    def code(self) -> bytes:
        return self.plan.code

    @property
    def facts(self) -> dict[str, int]:
        return self.plan.fact_map

    @property
    def factrefs(self) -> tuple[FactRef, ...]:
        return self.plan.facts

    @property
    def weight(self) -> int:
        return self.plan.weight

    @property
    def unit(self) -> Any:
        if "unit" not in self._cache:
            self._cache["unit"] = analysis_unit(self.src, self.plan.code)
        return self._cache["unit"]

    def analyses(self) -> tuple[Any, Any, Any, Any]:
        """``(hostops, hcfg, hlive, hconst)`` of the current bytes under the facts."""
        if "an" not in self._cache:
            from .analysis.hcfg import build_hcfg
            from .analysis.hconst import build_hconst
            from .analysis.hlive import build_hlive
            from .analysis.hostops import build_hostops

            u = self.unit
            h = build_hostops(u, self.layout, self.code_address)
            g = build_hcfg(h)
            lv = build_hlive(h, g) if h.ok and g.ok else None
            hc = build_hconst(h, g, self.facts, code=u.code)
            self._cache["an"] = (h, g, lv, hc)
        return self._cache["an"]


class B3WorkingSetView:
    """The WorkingSetView a B3 pass gets: the B2 view plus the overlay (``b3``)."""

    def __init__(self, view: Any, state: B3State):
        self._v = view
        self.b3 = state

    def __getattr__(self, name: str) -> Any:
        return getattr(self._v, name)

    def variant_ids(self) -> tuple[str, ...]:
        return tuple(self.b3.variants)


def variant_weight(row: Any, c_uid: str) -> int:
    return next((int(c.count) for c in row.candidates if c.dst == c_uid), 0)


def hot_set_bytes(store: Any, units: Mapping[str, Any]) -> int | None:
    """Bytes of the 99 %-coverage hot set (``hotness@1``), for the growth budget."""
    try:
        hot = store.get("hotness", ScopeRef("snapshot", store.view.snapshot_id)).value
    except Exception:  # noqa: BLE001 - no profile: no growth budget
        return None
    if not hot.hot_order:
        return None
    return sum(units[u].end_offset - units[u].tb_offset for u in hot.hot_order if u in units)


# ---------------------------------------------------------------------------
# materialization
# ---------------------------------------------------------------------------

@dataclass
class Materialized:
    plan: Any  # scopt.link.B3Plan
    downgraded: list[dict[str, Any]]
    regions: list[dict[str, Any]]  # ADD_REGION records (manager-generated)
    variant_ids: dict[str, str]  # clone id -> final variant id
    pass_bytes: dict[str, int]  # per-pass byte delta (bytes changed by its patches)
    composed: dict[str, list[Patch]]


def _compose(src_code: bytes, final: bytes, h: Any, history: Sequence[tuple[str, Patch]],
             target: str) -> list[Patch]:
    """Final bytes as patches relative to the captured code: one per original host
    instruction that changed (template of the last pass that touched it)."""
    # Fusing the two IRQ-poll instructions creates one instruction. Splitting
    # this certificate at the former MOV boundary would cut its displacement.
    if len(history) == 1:
        _, patch = history[0]
        if patch.template_id == "irq_entry_mem_compare":
            a, b = patch.host_off, patch.host_off + len(patch.old_bytes)
            if src_code[a:b] == patch.old_bytes and src_code[:a] + patch.new_bytes + src_code[b:] == final:
                return [replace(patch, target=target)]
    out = []
    for o in h.ops:
        a, b = o.off, o.end
        if bytes(src_code[a:b]) == bytes(final[a:b]):
            continue
        tmpl = None
        for _pn, p in history:
            if p.host_off < b and a < p.host_off + len(p.new_bytes):
                tmpl = p.template_id
        out.append(Patch(target, a, bytes(src_code[a:b]), bytes(final[a:b]),
                         tmpl or "dho_nop", "composed"))
    return out


def materialize(state: B3State, b3: B3Ctx, *, register_slots: bool = True) -> Materialized:
    """The :class:`~scopt.link.B3Plan` of the final overlay (downgrade rule applied)."""
    from .guard import GuardTarget
    from .link import B3Plan, GuardedExitSpec, VariantSpec, variant_key

    units = b3.units()
    downgraded: list[dict[str, Any]] = []
    keep: dict[str, VariantPlan] = {}
    def eff(uid: str) -> bytes:
        return bytes(state.unit_code.get(uid, units[uid].code))

    for vid, v in state.variants.items():
        if bytes(v.code) == eff(v.src_uid):
            downgraded.append({"variant": vid, "src": v.src_uid, "pred": list(v.pred),
                               "reason": "no_simplification"})
        else:
            keep[vid] = v
    grow: dict[str, list[GuardedExitSpec]] = {}
    for (uid, toff), x in sorted(state.exits.items()):
        tg = []
        for s in x.slots:
            kind = s.kind if (s.kind != "SPEC" or s.variant in keep) else "GENERAL"
            tg.append(GuardTarget(s.vaddr, s.slot_n, kind, r0_zero=s.r0_zero and kind == "SPEC",
                                  compact=s.compact and len(x.slots) == 1))
        grow.setdefault(uid, []).append(GuardedExitSpec(toff, x.exit_class, tuple(tg),
                                                        x.key_flags, x.key_cs_base))
    final_ids: dict[str, str] = {}
    vspecs = []
    composed: dict[str, list[Patch]] = {}
    pass_bytes: Counter = Counter()
    for vid, v in sorted(keep.items(), key=lambda kv: (kv[1].pred, kv[0])):
        c = units[v.src_uid]
        h = b3.hostops(c.uid)
        ps = _compose(eff(c.uid), v.code, h, v.patches, vid)  # relative to the effective C
        fid = variant_key(c.uid, v.pred[0], v.exit_id, [f.to_json() for f in v.facts], ps)
        final_ids[vid] = fid
        ps = [replace(p, target=fid) for p in ps]
        composed[fid] = ps
        vspecs.append(VariantSpec(c.uid, v.pred[0], v.pred[1], tuple(ps), tuple(v.facts),
                                  key=fid, exit_id=v.exit_id))
    rewrites: dict[str, tuple[Patch, ...]] = {}
    for uid, hist in state.rewrites.items():
        u = units[uid]
        ps = _compose(u.code, state.unit_code[uid], b3.hostops(uid), hist, uid)
        if ps:
            rewrites[uid] = tuple(ps)
    # per-pass byte delta: bytes each pass changed (in its own patches, on kept code)
    for v in keep.values():
        for pn, p in v.patches:
            pass_bytes[pn] += sum(1 for x, y in zip(p.old_bytes, p.new_bytes) if x != y)
    for hist in state.rewrites.values():
        for pn, p in hist:
            pass_bytes[pn] += sum(1 for x, y in zip(p.old_bytes, p.new_bytes) if x != y)
    regions = [{"kind": "ADD_REGION", "r": v.pred[0], "slot": v.pred[1],
                "variant": final_ids[vid], "c": v.src_uid}
               for vid, v in sorted(keep.items(), key=lambda kv: kv[1].pred)]
    plan = B3Plan(grow={u: tuple(xs) for u, xs in grow.items()}, variants=tuple(vspecs),
                  rewrites=rewrites, register_slots=register_slots,
                  max_code_growth_bytes=state.max_code_growth_bytes,
                  max_variants_per_c=state.max_variants_per_c, max_variants=state.max_variants)
    return Materialized(plan, downgraded, regions, final_ids, dict(pass_bytes), composed)


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------

def optimize_b3(snapshot: Any, pipeline: Any, store: Any, *, env: B3Env,
                profiles: Iterable[Any] = (), params: Mapping[str, Mapping[str, Any]] | None = None,
                disable: Iterable[str] = (), only: str | None = None,
                candidate_id: str | None = None, **manager_kw: Any) -> dict[str, Any]:
    """PassManager (B3 mode) -> :func:`materialize` -> :func:`scopt.validate.package_b3`
    (hostsym-certified) or, for a candidate without B3/B4 edits, the B1/B2
    :class:`~scopt.validate.ValidateAndPackage` (V9 byte identity).

    Returns ``{"candidate", "materialized", "package", "outcome", "path"}``; ``outcome``
    is the artifact's outcome (``published`` or the §3.8 class of the rejection)."""
    from .manager import PassManager
    from .validate import ValidateAndPackage, package_b3

    profiles = tuple(profiles)
    pm = PassManager(snapshot, pipeline, profiles, params, disable=disable, only=only,
                     b3=env, **manager_kw)
    cand = pm.execute()
    b3 = pm.b3ctx
    st: B3State = cand.b3_state
    # Partial/unknown unit proposals are normal. An actual failed pipeline pass
    # cannot satisfy its requested composition or explicit coverage contract.
    failed = next((p for p in cand.passes if p.outcome == Outcome.FAILED), None)
    if failed is not None:
        from .ledger import Ledger
        from .validate import _reject_b3

        reason = failed.reason or "pass_failed"
        res = _reject_b3(store, candidate_id or cand.candidate_id, reason,
                         Ledger(cand.ledger), None, None, f"{failed.name}: {failed.detail}")
        return {"candidate": cand, "materialized": None, "package": res,
                "path": "b3", "outcome": "failed"}
    if st is None or st.empty:
        res = ValidateAndPackage(cand, store, profiles=profiles).run()
        return {"candidate": cand, "materialized": None, "package": res, "path": "v5_identity",
                "outcome": "published" if res.status == "published" else
                b3_reject_outcome(res.reason).value}
    m = materialize(st, b3)
    from .analysis.obs_contract import build_contract
    from .validate_sym.link_check import hostsym_inputs, summary_provenance

    lay_target = None
    if env.runtime is not None:
        from .formats.envlayout_v1 import find_envlayout

        lay_target = find_envlayout(env.runtime.get("qemu_sha256"))
    lay = lay_target or b3.layout
    rows = b3.summaries()
    hs = None
    if lay is not None:
        from .guard import GuardOffsets

        hs = hostsym_inputs(layout=lay, contract=build_contract(lay, env.runtime or
                                                                snapshot.runtime),
                            offsets=GuardOffsets.from_layout(lay), selftest=env.selftest,
                            provenance=summary_provenance(rows))
    hot = hot_set_bytes(pm.store, b3.units())
    cid = candidate_id or cand.candidate_id
    from .ledger import Ledger

    led = Ledger(cand.ledger)
    for d in m.downgraded:  # §3.4 downgrade rule, one row per variant
        led.emit("unit", d["pred"][0], "optimized", Outcome.SKIPPED_UNSUPPORTED.value,
                 "no_simplification", d["variant"])
    for r in m.regions:  # §3.7 ADD_REGION (manager-generated; rows written at packaging)
        led.emit("unit", r["r"], "optimized", Outcome.CANDIDATE.value,
                 f"ADD_REGION multi_tb slot {r['slot']}", r["variant"])
    res = package_b3(snapshot, m.plan, store, candidate_id=cid,
                     placement=cand.working_set if cand.has_layout_changes else None,
                     pipeline=cand.pipeline_provenance(), profile_ids=cand.profile_rows(),
                     runtime=env.runtime, v6_exes=env.v6_exes, summaries=rows, hostsym=hs,
                     hot_set_bytes=hot, passes_byte_delta=m.pass_bytes,
                     ledger_rows=led.rows,
                     sidecar_extra={"x_b3_manager": b3_report(cand, m)})
    return {"candidate": cand, "materialized": m, "package": res, "path": "b3",
            "outcome": "published" if res.status == "published" else
            b3_reject_outcome(res.reason).value}


def b3_report(cand: Any, m: Materialized) -> dict[str, Any]:
    """Per-pass B3 rows for the sidecar: outcome, byte delta, origins, deps added."""
    passes = []
    for p in cand.passes:
        eds = [e for e in cand.edits if e.pass_name == p.name]
        passes.append({
            "pass": p.name, "version": p.version, "outcome": p.outcome.value,
            "reason": p.reason, "byte_delta": int(m.pass_bytes.get(p.name, 0)),
            "edits": dict(Counter(e.kind.name for e in eds)),
            "origins": sorted({o for e in eds for o in e.affected_origins})[:256],
            "deps_added": sorted({str(d) for e in eds for d in e.dependency_additions})[:256],
            "stats": {k: v for k, v in p.stats.items()}})
    return {"passes": passes, "downgraded": m.downgraded, "regions": m.regions,
            "variant_ids": m.variant_ids,
            "digest": "sha256:" + hashlib.sha256(repr(sorted(m.variant_ids.items()))
                                                 .encode()).hexdigest()}

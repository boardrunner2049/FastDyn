"""``eliminate-state-sync@1``: B4 bounded executor-state synchronization elimination
(design §7, plan WP25). WP25.

The pass nop-fills, in place and same-length, every env store that
``syncdead@1`` proves dead (classes D1 flag/register stores kept only by TCG's
pseudo sync points, D2 ``can_do_io`` stores, D3 ``plugin_mem_value_*`` stores
under the RuntimeIdentity contract). Each store becomes **one** multi-byte NOP
(template ``b4_nop_store``). Nothing is moved, no instruction boundary, search
row, icount immediate, LDST block, exit stub or pool byte changes, so a B4-only
plan is a **v5** artifact (``B3Plan(rewrites=…, register_slots=False)``).

Params (``params_schema``):

* ``classes``: ``"D1|D2|D3"`` (default all; ``|`` or ``,`` separated). The
  per-class ablation of T-B4-11 (``--param classes=D1`` …). A store of an
  unselected class is reported as ``class_not_selected``.
* ``coalesce_nops``: replace adjacent, independently deletable
  stores in one guest instruction with a single NOP when no entry or
  recovery target lies inside the pair (default false).
* ``absorb_nops``: absorb an individual store's NOP into a wider encoding
  of an adjacent same-guest CPU-state MOV (default false).
* ``max_units``: the work budget (units edited, hot-first when the context
  offers unit weights, else slot order); the rest is ``budget_exhausted``-free
  ``no_change`` (reported in ``stats.units_over_budget``).

Outcomes: ``candidate`` (reason ``dead_stores``) with one PURE_REWRITE per dead
store; ``no_change`` (``no_dead_store``) when nothing qualifies;
``skipped_unknown`` (``observer_contract_unknown``) when the contract cannot be
resolved (no EnvLayout bound to the RuntimeIdentity).

Every edit is validated independently (V12 ``hostsym`` on the rewritten unit,
V15 placement and deletable class, V20 admission); see :func:`package_b4`.

The manager of B2 (``scopt.manager.PassManager``) does not enable B3/B4 edit
kinds yet (WP21 owns that change). :func:`propose_b4` runs this pass with a
:class:`~scopt.proposal.ProposalBuilder` that enables PURE_REWRITE only, and
:func:`package_b4` turns the edits into a ``B3Plan`` and packages it with
:func:`scopt.validate.package_b3`, certified by WP19's kernel.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..analysis.api import AnalysisResult, ScopeRef
from ..analysis.obs_contract import D2_FIELDS, D3_FIELDS, ObsContract, build_contract
from ..analysis.syncdead import (B4Reason, SyncDead, TrackedBytes, b4_pair_allowed,
                                 b4_pair_anchors, b4_widen_move, nop_bytes, syncdead_of)
from ..canon import canonical_json, sha256_id
from .base import Budgets, EditKind, Outcome, PassDescriptor, PassResult, Patch

__all__ = ["EliminateStateSync", "PASSES", "TEMPLATE", "parse_classes", "StrictCallContract",
           "strict_contract", "B4Context", "propose_b4", "plan_of", "package_b4", "B4Proposal",
           "class_ledger_rows", "PASS_NAME"]

PASS_NAME = "eliminate-state-sync"
TEMPLATE = "b4_nop_store"
PAIR_TEMPLATE = "b4_nop_pair"
ABSORB_TEMPLATE = "b4_absorb_nop"
ALL_CLASSES = ("D1", "D2", "D3")

log = logging.getLogger("scopt.pass.eliminate-state-sync")


def parse_classes(v: Any) -> tuple[str, ...]:
    """``"D1|D2"`` / ``"D1,D3"`` / ``["D1"]`` / None -> sorted class tuple (ValueError on junk)."""
    if v is None or v == "" or v == "all":
        return ALL_CLASSES
    items = v if isinstance(v, (list, tuple)) else str(v).replace(",", "|").split("|")
    out = sorted({str(x).strip().upper() for x in items if str(x).strip()})
    bad = [x for x in out if x not in ALL_CLASSES]
    if bad or not out:
        raise ValueError(f"classes must be a subset of D1|D2|D3, got {v!r}")
    return tuple(out)


class EliminateStateSync:
    """B4 (design §7.3): same-length NOP fill of dead executor-state stores."""

    descriptor = PassDescriptor(
        name=PASS_NAME, version=1, unit_scope="snapshot",
        requires=(("syncdead", 1),), capabilities=frozenset({EditKind.PURE_REWRITE}),
        budgets=Budgets(max_wall_s=900.0),
        preserves=frozenset({"envlayout", "obs-contract"}),
        invalidates=frozenset({"code"}),
        params_schema={"classes": str, "max_units": int,
                       "coalesce_nops": bool, "absorb_nops": bool})

    def run(self, snap: Any, ctx: Any, edits: Any) -> PassResult:
        try:
            classes = parse_classes(ctx.params.get("classes"))
        except ValueError as e:
            return PassResult(Outcome.FAILED, "invalid_params", str(e))
        max_units = ctx.params.get("max_units")
        uids = list(snap.unit_ids())
        weights = getattr(ctx, "unit_weights", None)
        if callable(weights):
            weights = weights()
        if weights:
            uids.sort(key=lambda u: (-int(weights.get(u, 0)), uids.index(u)))
        stats: Counter[str] = Counter()
        affected: list[str] = []
        n_edit_units = 0
        over_budget = 0
        sid = getattr(snap, "snapshot_id", None) or ctx.snapshot_id
        for uid in uids:
            ctx.budget()
            try:
                res = ctx.analysis("syncdead", ScopeRef("unit", uid))
            except KeyError as e:  # no EnvLayout / contract: fail closed
                return PassResult(Outcome.SKIPPED_UNKNOWN, B4Reason.OBSERVER_CONTRACT_UNKNOWN,
                                  str(e))
            sd: SyncDead = res.value
            if not sd.ok:
                stats[f"unit:{sd.reason}"] += 1
                continue
            for r in sd.refused:
                stats[f"{r.cls or '-'}:{r.reason}"] += 1
            take = [d for d in sd.dead if d.cls in classes]
            for d in sd.dead:
                if d.cls not in classes:
                    stats[f"{d.cls}:{B4Reason.CLASS_NOT_SELECTED}"] += 1
            if not take:
                stats["unit:no_dead_store"] += 1
                continue
            if max_units is not None and n_edit_units >= int(max_units):
                over_budget += 1
                continue
            n_edit_units += 1
            affected.append(uid)
            groups = list(_store_groups(snap, uid, take, coalesce=ctx.params.get("coalesce_nops", False)))
            absorbed = (_absorbed_moves(snap, uid, groups)
                        if ctx.params.get("absorb_nops", False) else {})
            for group in groups:
                first = group[0]
                patch = absorbed.get(first.off)
                if patch is not None:
                    stats["absorbed_nops"] += 1
                else:
                    old = b"".join(d.old for d in group)
                    new = nop_bytes(len(old))
                    assert new is not None  # the store/group has an exact one-NOP encoding
                    template = PAIR_TEMPLATE if len(group) == 2 else TEMPLATE
                    justification = "|".join(f"{d.cls}:{d.field}:{d.origin}" for d in group)
                    patch = Patch(uid, first.off, old, new, template, justification)
                edits.pure_rewrite(patch, reads=[res] if isinstance(res, AnalysisResult) else ())
                for d in group:
                    stats[f"{d.cls}:deleted"] += 1
                    stats[f"{d.cls}:deleted_bytes"] += d.size
                if len(group) == 2:
                    stats["coalesced_pairs"] += 1
        stats["units_edited"] = n_edit_units
        stats["units_over_budget"] = over_budget
        st = {k: float(v) for k, v in sorted(stats.items())}
        if not n_edit_units:
            return PassResult(Outcome.NO_CHANGE, B4Reason.NO_DEAD_STORE,
                              f"classes {'|'.join(classes)}: no dead store", stats=st)
        n = sum(stats[f"{c}:deleted"] for c in classes)
        return PassResult(Outcome.CANDIDATE, "dead_stores",
                          f"{n} stores in {n_edit_units} units (classes {'|'.join(classes)}; "
                          f"snapshot {str(sid)[:23]})", affected=tuple(affected), stats=st)


def _store_groups(snap, uid, stores, *, coalesce):
    if not coalesce or len(stores) < 2:
        yield from ((store,) for store in stores)
        return
    unit = snap.unit(uid)
    anchors = b4_pair_anchors(unit, snap.header.code_address)
    stores = sorted(stores, key=lambda store: store.off)
    index = 0
    while index < len(stores):
        first = stores[index]
        if index + 1 < len(stores):
            second = stores[index + 1]
            if (first.off + first.size == second.off
                    and b4_pair_allowed(unit, first.off, second.off + second.size, anchors)):
                yield first, second
                index += 2
                continue
        yield (first,)
        index += 1


def _absorbed_moves(snap, uid, groups):
    from ..decode.x86 import sweep

    unit = snap.unit(uid)
    end = unit.pool_start if unit.pool_start is not None else unit.tc_size
    decoded = sweep(unit.code[:end], 0)
    index_by_off = {op.address: i for i, op in enumerate(decoded)}
    used = {store.off for group in groups for store in group}
    anchors = b4_pair_anchors(unit, snap.header.code_address)
    output = {}
    for group in groups:
        if len(group) != 1:
            continue
        store = group[0]
        index = index_by_off[store.off]
        for j in (index - 1, index + 1):
            if not 0 <= j < len(decoded):
                continue
            other = decoded[j]
            if other.address in used:
                continue
            start, end = min(store.off, other.address), max(store.off + store.size, other.end)
            if not b4_pair_allowed(unit, start, end, anchors):
                continue
            wide = b4_widen_move(other.raw, store.size)
            if wide is None:
                continue
            side = "left" if store.off == start else "right"
            why = f"{store.cls}:{store.field}:{store.origin}:{side}"
            output[store.off] = Patch(uid, start, unit.code[start:end], wide, ABSORB_TEMPLATE, why)
            used.add(other.address)
            break
    return output


PASSES = (EliminateStateSync,)


# ---------------------------------------------------------------------------
# validator contract (survey §3c B4-2 (1): every call reads can_do_io)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StrictCallContract(ObsContract):
    """``obs-contract@1`` with the WP25 call rule for the validator: ``can_do_io``
    is observable at **every** call event (``hostsym`` gives pure calls the sync
    ``P`` and the lookup ``S3``), and ``plugin_mem_value_*`` also at ``S6``
    (udiv/sdiv, design §7.1 "treated as S4"). Everything else is the base contract."""

    def observable(self, fld: str, event: str, *, exit_kind: str | None = None) -> bool:
        if super().observable(fld, event, exit_kind=exit_kind):
            return True
        if fld in D2_FIELDS and event in ("P", "S3", "S6"):
            return True  # hostsym emits P only for pure calls
        if fld in D3_FIELDS and self.deletable_class(fld) == "D3" and event == "S6":
            return True
        return False


def strict_contract(c: ObsContract) -> StrictCallContract:
    sc = StrictCallContract(c.envlayout_id, c.tb_start_cb_possible,
                            c.mem_get_value_outside_mem_cb, dict(c.offsets), "")
    doc = {**sc.to_json(), "call_rule": "wp25: can_do_io read at every call; D3 at S4/S6"}
    return StrictCallContract(c.envlayout_id, c.tb_start_cb_possible,
                              c.mem_get_value_outside_mem_cb, dict(c.offsets),
                              sha256_id(canonical_json(doc)))


# ---------------------------------------------------------------------------
# driver (no B3 manager yet)
# ---------------------------------------------------------------------------

class B4Context:
    """A :class:`~scopt.passes.base.PassContext` for :func:`propose_b4`: computes
    ``syncdead@1`` directly against an explicit RuntimeIdentity (the snapshot's
    own identity may be v1, whose admission facts are unknown)."""

    def __init__(self, snapshot: Any, *, layout: Any, runtime: Mapping[str, Any] | None,
                 params: Mapping[str, Any] | None = None,
                 weights: Mapping[str, int] | None = None, **kw: Any):
        self.snapshot = snapshot
        self.layout = layout
        self.runtime = runtime
        self.bundle = getattr(snapshot, "bundle", None)
        self.snapshot_id = snapshot.snapshot_id
        self.params = dict(params or {})
        self.log = log
        self.contract = build_contract(layout, runtime)
        self.tracked = TrackedBytes.of(layout) if layout is not None else None
        self.kw = kw
        self._w = dict(weights or {})
        self._cache: dict[str, SyncDead] = {}
        self._units = {u.uid: u for u in snapshot.units()}

    def unit_weights(self) -> Mapping[str, int]:
        return self._w

    def analysis(self, name: str, scope: Any, min_version: int = 1) -> Any:
        if name != "syncdead":
            raise KeyError(f"B4Context provides only syncdead, not {name}")
        if self.layout is None:
            raise KeyError("no EnvLayout bound to the RuntimeIdentity")
        uid = scope.id
        r = self._cache.get(uid)
        if r is None:
            r = syncdead_of(self._units[uid], self.layout, self.snapshot.header.code_address,
                            self.contract, tracked=self.tracked, **self.kw)
            self._cache[uid] = r

        class _R:  # minimal AnalysisResult stand-in (value only)
            value = r
        return _R

    def fact(self, *a: Any, **k: Any) -> Any:
        raise KeyError("no facts in B4")

    def profile(self, phase: str | None) -> Any:
        return None

    def budget(self) -> None:
        return None

    def recorded_reads(self) -> tuple:
        return ()

    def recorded_facts(self) -> tuple:
        return ()

    def results(self) -> dict[str, SyncDead]:
        return dict(self._cache)


def _patch_classes(patch: Patch) -> tuple[str, ...]:
    parts = patch.justification.split("|") if patch.template_id == PAIR_TEMPLATE else (patch.justification,)
    return tuple(part.split(":", 1)[0] for part in parts)


@dataclass
class B4Proposal:
    result: PassResult
    patches: dict[str, tuple[Patch, ...]]
    classes: tuple[str, ...]
    contract: ObsContract
    syncdead: dict[str, SyncDead] = field(default_factory=dict)
    edit_digest: str | None = None
    coalesce_nops: bool = False
    absorb_nops: bool = False

    @property
    def n_patches(self) -> int:
        return sum(len(v) for v in self.patches.values())

    def by_class(self) -> dict[str, int]:
        c: Counter[str] = Counter()
        for ps in self.patches.values():
            for p in ps:
                c.update(_patch_classes(p))
        return {k: c.get(k, 0) for k in ALL_CLASSES}

    def need_d2(self) -> bool:
        return self.by_class()["D2"] > 0

    def need_d3(self) -> bool:
        return self.by_class()["D3"] > 0


def propose_b4(snapshot: Any, *, layout: Any, runtime: Mapping[str, Any] | None,
               classes: Any = None, max_units: int | None = None,
               weights: Mapping[str, int] | None = None, coalesce_nops: bool = False,
               absorb_nops: bool = False, **kw: Any) -> B4Proposal:
    """Run :class:`EliminateStateSync` on ``snapshot`` with PURE_REWRITE enabled."""
    from ..model.snapshot import WorkingSet
    from ..model.view import WorkingSetView
    from ..proposal import ProposalBuilder

    params: dict[str, Any] = {"coalesce_nops": coalesce_nops, "absorb_nops": absorb_nops}
    if classes is not None:
        params["classes"] = "|".join(parse_classes(classes))
    if max_units is not None:
        params["max_units"] = max_units
    view = WorkingSetView(WorkingSet.initial(snapshot))
    ctx = B4Context(snapshot, layout=layout, runtime=runtime, params=params, weights=weights,
                    **kw)
    p = EliminateStateSync()
    b = ProposalBuilder(p.descriptor, view, enabled={EditKind.PURE_REWRITE}, ctx=ctx)
    res = p.run(view, ctx, b)
    patches: dict[str, list[Patch]] = {}
    for e in b.edits:
        pt = e.payload
        patches.setdefault(pt.target, []).append(pt)
    return B4Proposal(res, {k: tuple(v) for k, v in patches.items()},
                      parse_classes(classes), ctx.contract, ctx.results(), b.digest(), coalesce_nops, absorb_nops)


def plan_of(prop: B4Proposal | Mapping[str, Sequence[Patch]]) -> Any:
    """The ``B3Plan`` of a B4-only proposal: rewrites only, v5 (no slot registered)."""
    from ..link import B3Plan

    patches = prop.patches if isinstance(prop, B4Proposal) else prop
    return B3Plan(rewrites={k: tuple(v) for k, v in patches.items()}, register_slots=False)


def class_ledger_rows(prop: B4Proposal, *, ledger: Any = None) -> Any:
    """Per-class ledger rows (T-B4-11): ``pass eliminate-state-sync@1/<class>``."""
    from ..ledger import Ledger

    led = ledger if ledger is not None else Ledger()
    for c in ALL_CLASSES:
        if c not in prop.classes:
            led.emit("pass", f"{PASS_NAME}@1/{c}", "optimized", Outcome.SKIPPED_UNSUPPORTED.value,
                     B4Reason.CLASS_NOT_SELECTED, None, counts={"stores": 0})
            continue
        records = [(uid, p, _patch_classes(p).count(c)) for uid, v in prop.patches.items() for p in v]
        matches = [(uid, p, count) for uid, p, count in records if count]
        ps = sorted([uid, p.host_off, p.old_bytes.hex()] for uid, p, _ in matches)
        ev = sha256_id(canonical_json(ps)) if ps else None
        led.emit("pass", f"{PASS_NAME}@1/{c}", "optimized",
                 Outcome.CANDIDATE.value if ps else Outcome.NO_CHANGE.value,
                 "dead_stores" if ps else B4Reason.NO_DEAD_STORE, ev,
                 counts={"stores": sum(count for _, _, count in matches), "units": len({x[0] for x in ps})})
    return led


def package_b4(snapshot: Any, prop: B4Proposal, store: Any, *, candidate_id: str,
               layout: Any, runtime: Mapping[str, Any] | None, selftest: Mapping[str, Any],
               v6_exes: Iterable[str] = (), env_abs: tuple[int, int] | None = None,
               **kw: Any) -> Any:
    """Link (v5), validate (V1-V23 with V12 from ``hostsym`` under the strict call
    contract, V15 deletable class, V20 with D2/D3 needs) and publish ``prop``.

    ``selftest`` is the V18 document (``scopt.validate_sym.selftest.load_or_run``)."""
    from ..validate import package_b3
    from ..validate_sym.link_check import hostsym_inputs

    plan = plan_of(prop)
    sc = strict_contract(prop.contract)
    hs = hostsym_inputs(layout=layout, contract=sc, selftest=selftest, env_abs=env_abs)
    params = {"classes": "|".join(prop.classes)}
    if prop.coalesce_nops:
        params["coalesce_nops"] = True
    if prop.absorb_nops:
        params["absorb_nops"] = True
    pipeline = [{"pass": PASS_NAME, "version": 1,
                 "params_sha256": sha256_id(canonical_json(params)),
                 "outcome": prop.result.outcome.value, "reason": prop.result.reason,
                 "affected": sorted(prop.patches), "edit_digest": prop.edit_digest,
                 "duration_s": 0.0, "x_classes": prop.by_class()}]
    return package_b3(snapshot, plan, store, candidate_id=candidate_id, pipeline=pipeline,
                      layout=layout, runtime=runtime, v6_exes=v6_exes, hostsym=hs,
                      need_d2=prop.need_d2(), need_d3=prop.need_d3(),
                      compile_only=True, **kw)

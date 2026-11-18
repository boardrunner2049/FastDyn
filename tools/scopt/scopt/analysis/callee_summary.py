"""``callee-summary@1``: one CalleeSummary row per return unit (B3/B4 design §3.3). WP20.

Joins, for every exit whose guest instruction is a return (``return-edges@1``
class ``return``, admitted or not):

* ``callee``: the guest ``STT_FUNC`` containing the return instruction
  (optional ELF; stripped or absent -> ``null``);
* ``patches[]``: every patch-plan entry (modifiers and virtuals) whose vaddr
  lies in the callee range, or in R's instructions when the range is
  unknown, fact-defining or not (``defines_fact``);
* ``facts[]``: the ``exit-facts@1`` facts at the exit whose provenance
  ``patch-join@1`` explains (``source = patch | guest_code``); the others go
  to ``refused[]`` with the provenance reason, together with the proposer's
  refusals and ``return-edges@1``'s ``fact_not_local_to_return_tb`` rows;
* ``effects[]``: R's ordered events from ``effects@1`` + HostOps;
* ``barriers[]``: the kills on the paths to the exit;
* ``deps``: R's identity, ``patch_plan_hash``, ``instrumentation_hash``,
  ``contract_id``, helper ids + flags, ``envlayout_id`` (no Tier-1
  ``ops_hash``: WP23 is not approved).

Certification (V14, WP19 ``hostsym``) is an optional callable; without it
every fact is ``status = proposed`` with ``cert_hash = null`` (an
``ATTACH_FACT`` then fails its "fact certified and current" precondition,
§3.7). With it, a certifier ``False`` moves the fact to ``refused`` as
``fact_derivation_mismatch`` (a tool bug, reported as failed, §3.2) and
``None`` as ``validation_inconclusive``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..decode.guest_symbols import GuestImage, unit_guest_bytes
from ..decode.thumb_sem import decode_unit
from ..formats.callee_summary_v1 import SCHEMA, VERSION, fact_id, factref, seal_row
from .api import AnalysisSpec, ScopeRef
from .effects import Effects, build_effects, call_class
from .exit_facts import ExitFactSet, FactRow, UnitExitFacts
from .hcfg import HCfg, build_hcfg
from .hostops import UnitHostOps
from .patch_join import PatchPlan, insn_vaddrs, join_fact
from .return_edges import ReturnEdges

__all__ = ["build_callee_summaries", "summary_row", "CALLEE_SUMMARY", "ANALYSES", "Certifier",
           "factrefs_of"]

#: ``(unit, exit fact set, fact) -> (certified?, cert_hash)``; ``None`` = inconclusive.
Certifier = Callable[[Any, ExitFactSet, FactRow], tuple[bool | None, str | None]]


def _hex(v: int | None) -> str | None:
    return None if v is None else f"0x{v:x}"


def _key(u: Any) -> dict[str, Any]:
    k = u.key
    return {"page_addr0": _hex(k.page_addr0), "page_addr1": _hex(k.page_addr1),
            "flags": _hex(k.flags), "cs_base": _hex(k.cs_base), "cflags": _hex(k.cflags)}


def _effects(h: UnitHostOps, e: Effects) -> list[dict[str, Any]]:
    out = []
    for i, kind, detail in e.events:
        o = h.ops[i]
        if kind == "guest_access":
            out.append({"kind": "guest_ld" if o.is_ld else "guest_st", "host_off": o.off,
                        "insn": o.insn, "oi": int(o.oi or 0), "faultable": True})
        elif kind == "call":
            cc = call_class(o.helper_name, o.helper_flags)
            out.append({"kind": "plugin_cb" if cc == "plugin" else "helper_call",
                        "host_off": o.off, "insn": o.insn, "helper_id": o.helper_id,
                        "helper": o.helper_name, "flags": o.helper_flags, "class": cc})
        elif kind == "host_store":
            out.append({"kind": "host_abs_store", "host_off": o.off, "insn": o.insn,
                        "abs": _hex(o.abs_addr)})
        elif kind == "exit":
            out.append({"kind": "exit", "host_off": o.off, "insn": o.insn, "exit": detail})
    return out


def summary_row(u: Any, row: Any, *, snapshot_id: str, h: UnitHostOps, g: HCfg | None,
                facts: UnitExitFacts | None, not_local: Sequence[Any], guest: GuestImage | None,
                plan: PatchPlan, bundle: Mapping[str, Any] | None,
                certify: Certifier | None = None, page_bits: int = 10) -> dict[str, Any]:
    vaddrs = insn_vaddrs(u, page_bits)
    ginsns = decode_unit(u, unit_guest_bytes(u, guest), page_bits)
    callee = guest.func_at(row.exit_vaddr) if guest is not None and row.exit_vaddr is not None \
        else None
    # facts -------------------------------------------------------------------------
    es: ExitFactSet | None = None
    if facts is not None and row.tail_off is not None:
        es = next((x for x in facts.exits if x.tail.off == row.tail_off), None)
    fact_rows: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    defining: set[tuple[str, int]] = set()  # (kind, index) of plan entries that define facts
    if es is not None:
        for f in es.facts:
            prov, _per = join_fact(f.field, f.value, f.defs, ginsns=ginsns, vaddrs=vaddrs,
                                   plan=plan, bundle=bundle)
            if not prov.ok:
                refused.append({"field": f.field, "reason": prov.reason, "detail": prov.detail,
                                "value": _hex(f.value)})
                continue
            status, cert = "proposed", None
            if certify is not None:
                ok, cert = certify(u, es, f)
                if ok is not True:
                    refused.append({"field": f.field, "value": _hex(f.value),
                                    "reason": "validation_inconclusive" if ok is None
                                    else "fact_derivation_mismatch",
                                    "detail": "certifier disagrees" if ok is False
                                    else "certifier inconclusive"})
                    continue
                status = "certified"
            fr: dict[str, Any] = {
                "fact_id": fact_id(u.uid, u.content_hash, row.exit_id, f.field, f.value, f.defs),
                "field": f.field, "value": _hex(f.value), "def_host_off": f.def_host_off,
                "def_insn": f.def_insn, "defs": [{"host_off": o, "insn": i} for o, i in f.defs],
                "source": prov.source, "cert_hash": cert, "status": status}
            if prov.plan_entry is not None:
                fr["plan_entry"] = prov.plan_entry.to_json()
                defining.add((prov.plan_entry.kind, prov.plan_entry.index))
            if prov.writer_insn is not None:
                fr["writer_insn"] = prov.writer_insn
            if prov.writer_vaddr is not None:
                fr["writer_vaddr"] = _hex(prov.writer_vaddr)
            fact_rows.append(fr)
        for r in es.refused:
            refused.append({"field": r.field, "reason": r.reason, "detail": r.detail})
    for n in not_local:
        refused.append({"field": n.field, "reason": "fact_not_local_to_return_tb",
                        "value": _hex(n.value),
                        "detail": f"defined in {n.defined_in} at 0x{n.def_vaddr:x}"})
    # patches -------------------------------------------------------------------------
    if callee is not None:
        ents, scope = plan.in_range(callee.start, callee.end), "callee"
    elif vaddrs is not None:
        vs = set(vaddrs)
        ents, scope = [e for e in plan.entries if e.vaddr in vs], "unit"
    else:
        ents, scope = [], "unknown"
    patches = [{**e.to_json(), "defines_fact": (e.kind, e.index) in defining} for e in ents]
    # effects -------------------------------------------------------------------------
    effects: list[dict[str, Any]] = []
    if h.ok:
        gg = g if g is not None else build_hcfg(h)
        effects = _effects(h, build_effects(h, gg))
    b = bundle or {}
    doc = {
        "schema": SCHEMA, "version": VERSION, "snapshot_id": snapshot_id,
        "return_unit": u.uid, "exit_id": row.exit_id, "exit_kind": row.exit_kind,
        "return_insn_vaddr": _hex(row.exit_vaddr), "return_insn": row.exit_text,
        "admitted": row.admitted,
        "exit_reasons": [{"code": c, **({"detail": d} if d else {})} for c, d in row.reasons],
        "callee": None if callee is None else callee.to_json(),
        "patches_scope": scope if plan.modifiers_known and plan.virtuals_status != "unavailable"
        else "unknown",
        "patches": patches, "facts": fact_rows, "refused": refused, "effects": effects,
        "barriers": [x.to_json() for x in (es.barriers if es is not None else ())],
        "deps": {
            "unit": {"uid": u.uid, "content_hash": u.content_hash,
                     "guest_hash": _hex(u.guest_hash), "key": _key(u)},
            "patch_plan_hash": b.get("patch_plan_hash"),
            "instrumentation_hash": b.get("instrumentation_hash"),
            "contract_id": b.get("contract_id"),
            "helpers": [{"id": x.id, "name": x.name, "flags": x.flags}
                        for x in sorted(u.bindings.helpers, key=lambda x: x.id)],
            "envlayout_id": h.envlayout_id,
        },
    }
    if facts is None or facts.status != "ok":
        doc["x_facts_status"] = {"status": "unavailable", "reasons": [] if facts is None else
                                 list(facts.reasons)[:8]}
    return seal_row(doc)


def build_callee_summaries(units: Sequence[Any], edges: ReturnEdges, *, snapshot_id: str,
                           hostops_of: Callable[[Any], UnitHostOps],
                           facts_of: Callable[[Any], UnitExitFacts],
                           hcfg_of: Callable[[Any], HCfg] | None = None,
                           guest: GuestImage | None, plan: PatchPlan,
                           bundle: Mapping[str, Any] | None,
                           certify: Certifier | None = None, page_bits: int = 10
                           ) -> list[dict[str, Any]]:
    by_uid = {u.uid: u for u in units}
    out = []
    for row in edges.exits:
        if row.exit_class != "return":
            continue
        u = by_uid[row.uid]
        h = hostops_of(u)
        out.append(summary_row(
            u, row, snapshot_id=snapshot_id, h=h,
            g=None if hcfg_of is None else hcfg_of(u),
            facts=facts_of(u) if h.ok else None,
            not_local=[n for n in edges.not_local_of(u.uid) if n.exit_id == row.exit_id],
            guest=guest, plan=plan, bundle=bundle, certify=certify, page_bits=page_bits))
    return out


def factrefs_of(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [factref(r, f) for r in rows for f in r["facts"]]


def _compute(ctx: Any, scope: ScopeRef) -> tuple[Mapping[str, Any], ...]:
    from .b3_inputs import guest_image_for, patch_plan_for

    bundle = ctx.bundle
    units = ctx.units()
    edges = ctx.analysis("return-edges", scope).value

    def hops(u):
        return ctx.analysis("hostops", ScopeRef("unit", u.uid)).value

    def facts(u):
        return ctx.analysis("exit-facts", ScopeRef("unit", u.uid)).value

    def cfg(u):
        return ctx.analysis("hcfg", ScopeRef("unit", u.uid)).value

    rows = build_callee_summaries(units, edges, snapshot_id=ctx.snapshot_id, hostops_of=hops,
                                  facts_of=facts, hcfg_of=cfg, guest=guest_image_for(ctx, bundle),
                                  plan=patch_plan_for(ctx, bundle), bundle=bundle,
                                  page_bits=ctx.header.target_page_bits)
    return tuple(rows)


CALLEE_SUMMARY = AnalysisSpec(
    name="callee-summary", version=1, scope="snapshot",
    requires=(("return-edges", 1), ("hostops", 1), ("exit-facts", 1), ("hcfg", 1)),
    inputs=frozenset({"code", "meta", "bundle"}), compute=_compute, result_schema=tuple)
ANALYSES = (CALLEE_SUMMARY,)

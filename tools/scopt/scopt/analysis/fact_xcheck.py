"""Two-sided proposer/certifier agreement on every lookup exit (WP20 integration).

``--certify`` (``callee_summary``) asks WP19's V14 certifier only about the facts the
proposer (``exit-facts@1``) *proposed*; a disagreement there is
``fact_derivation_mismatch``. This module also asks the other direction: for every
admissible lookup tail of every unit the proposer analysed, and for **every** fact
field (r0..r12, NF, ZF, CF, VF), it compares the proposer's verdict with the value
``hostsym`` derives on its own (one exploration per unit; the derivation is
:func:`scopt.validate_sym.equiv.certify_fact` with ``exit_pc = T + 9``, done here on a
shared exploration so a unit is explored once).

Outcomes per (unit, tail, field):

``agree_const``
    proposer ``const(v)`` and hostsym constant ``v`` on every path.
``fact_derivation_mismatch``
    proposer ``const(v)`` and hostsym another constant, or not constant (§3.2: a
    tool bug, never accepted).
``inconclusive``
    proposer ``const(v)`` and hostsym inconclusive / exit unreached
    (``validation_inconclusive``: the fact is refused, not a mismatch).
``agree_nonconst``
    proposer refused the field (``fact_not_constant``, ``fact_killed_by_helper``,
    ``fact_not_local_to_return_tb``) or did not track it (not stored in R, or killed
    and not stored), and hostsym finds no constant either (not constant,
    inconclusive or unreached).
``proposer_weaker``
    proposer refused or did not track the field, but hostsym derives a constant.
    Sound (the proposer only gives up a fact) but a disagreement, listed with its
    refusal reason.

``mismatch == 0`` is the acceptance "proposer and certifier agree everywhere";
``proposer_weaker`` is reported, not failed, because a refused fact can never be used.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any

from ..decode.thumb_sem import FACT_FIELDS

__all__ = ["xcheck_unit", "xcheck_snapshot", "OUTCOMES"]

OUTCOMES = ("agree_const", "agree_nonconst", "inconclusive", "proposer_weaker",
            "fact_derivation_mismatch")


def _hostsym_values(ex: Any, exit_pc: int, fld: tuple[int, int]) -> tuple[str, int | None]:
    """certify_fact's verdict on an existing exploration (same rule, no re-exploration)."""
    if not ex.complete:
        return "inconclusive", None
    vals = []
    for st in ex.paths:
        ev = st.events
        if len(ev) >= 2 and ev[-1].exit_kind == "goto_ptr" and ev[-2].kind == "call" and \
                ev[-2].cls == "lookup" and ev[-1].pc == exit_pc:
            vals.append(ev[-2].env.read(*fld))
    if not vals:
        return "unreached", None
    if all(isinstance(x, int) for x in vals) and len(set(vals)) == 1:
        return "certified", vals[0]
    return "not_constant", None


def xcheck_unit(u: Any, facts: Any, program: Any, layout: Any,
                explore: Callable[..., Any] | None = None) -> list[dict[str, Any]]:
    """One row per (tail, field) of unit ``u``; ``facts`` is its ``UnitExitFacts``."""
    from ..validate_sym.link_check import fact_field
    from ..validate_sym.paths import explore as _explore

    if facts.status != "ok" or not facts.exits:
        return []
    ex = (explore or _explore)(program, None, layout=layout)
    rows: list[dict[str, Any]] = []
    for es in facts.exits:
        epc = es.tail.off + 9
        refused = {r.field: r.reason for r in es.refused}
        for f in FACT_FIELDS:
            fr = es.fact(f)
            fld = fact_field(f, layout)
            width_mask = (1 << (8 * fld[1])) - 1
            hs, hv = _hostsym_values(ex, epc, fld)
            if fr is not None:
                if hs == "certified":
                    out = "agree_const" if hv == (fr.value & width_mask) else \
                        "fact_derivation_mismatch"
                elif hs == "not_constant":
                    out = "fact_derivation_mismatch"
                else:
                    out = "inconclusive"
                prop = f"const:0x{fr.value:x}"
            else:
                prop = refused.get(f, "untracked")
                out = "proposer_weaker" if hs == "certified" else "agree_nonconst"
            if out != "agree_nonconst" or prop != "untracked":
                rows.append({"uid": u.uid, "vaddr": f"0x{u.vaddrs[0]:x}" if u.vaddrs else None,
                             "tail_off": es.tail.off, "field": f, "proposer": prop,
                             "hostsym": hs if hv is None else f"{hs}:0x{hv:x}", "outcome": out})
            else:
                rows.append({"uid": u.uid, "field": f, "proposer": prop, "hostsym": hs,
                             "outcome": out, "tail_off": es.tail.off, "brief": True})
    return rows


def xcheck_snapshot(units: Iterable[Any], facts_of: Callable[[Any], Any], snapshot: Any,
                    layout: Any, uid_filter: Callable[[Any], bool] | None = None
                    ) -> dict[str, Any]:
    """Run :func:`xcheck_unit` over ``units``; returns tallies and the non-trivial rows."""
    from ..validate_sym.program import program_of, world_of

    w = world_of(snapshot, layout)
    tally: Counter = Counter()
    by_prop: Counter = Counter()
    detail: list[dict[str, Any]] = []
    n_units = n_exits = 0
    errors: list[dict[str, str]] = []
    for u in units:
        if uid_filter is not None and not uid_filter(u):
            continue
        fx = facts_of(u)
        if fx.status != "ok" or not fx.exits:
            continue
        try:
            rows = xcheck_unit(u, fx, program_of(u, w), layout)
        except Exception as e:  # recorded: a unit hostsym cannot model is inconclusive
            errors.append({"uid": u.uid, "error": f"{type(e).__name__}: {e}"})
            for es in fx.exits:
                for f in es.facts:
                    tally["inconclusive"] += 1
                    detail.append({"uid": u.uid, "tail_off": es.tail.off, "field": f.field,
                                   "proposer": f"const:0x{f.value:x}", "hostsym": "error",
                                   "outcome": "inconclusive"})
            continue
        n_units += 1
        n_exits += len(fx.exits)
        for r in rows:
            tally[r["outcome"]] += 1
            by_prop[(r["outcome"], r["proposer"].split(":")[0])] += 1
            if not r.pop("brief", False):
                detail.append(r)
    return {"units": n_units, "exits": n_exits,
            "tally": {k: tally.get(k, 0) for k in OUTCOMES},
            "by_proposer": {f"{o}/{p}": n for (o, p), n in sorted(by_prop.items())},
            "mismatch": tally.get("fact_derivation_mismatch", 0),
            "errors": errors, "rows": detail}

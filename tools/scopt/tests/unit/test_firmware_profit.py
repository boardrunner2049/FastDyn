"""Firmware-inline benefit/cost model (scopt.analysis.firmware_profit)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from scopt.analysis.firmware_profit import (
    DispatchCosts, ProfitError, ProfitIndex, ProfitModel, admission, estimate_region,
    resolve_entry_chain, resolve_residual_goto_tb,
)
from scopt.decode.search import CF_PCREL
from scopt.formats.enums import ExitKind

DATA = Path(__file__).parent / "data" / "firmware_profit_packet.json"


def _member(uid, depth=0, role="callee"):
    return SimpleNamespace(source_uid=uid, inline_context=("c",) * depth, role=role)


def _edge(src, exit_id, kind, dst, guarded=False):
    return SimpleNamespace(source_member=src, exit_id=exit_id, kind=kind, target_member=dst,
                           guarded=guarded)


def _plan(members, edges, *, trusted=True, call_exit="x0"):
    return SimpleNamespace(members=tuple(members), edges=tuple(edges), entry_member=0,
                           trusted=trusted, trusted_entry_offset=15 if trusted else 0,
                           callsite=SimpleNamespace(exit_id=call_exit))


def _x(id_, kind, slot=None):
    return SimpleNamespace(id=id_, kind=ExitKind(kind), slot=slot)


def _units(spec, cflags=0):
    return {uid: SimpleNamespace(tc_size=size, key=SimpleNamespace(cflags=cflags))
            for uid, size in spec.items()}


def _index(rows, execs):
    return ProfitIndex([dict(src=s, dst=d, kind=k, count=n) for s, d, k, n in rows],
                       lambda uid: execs.get(uid))


def _estimate(plan, units, exits, index, costs=DispatchCosts(), **kw):
    kw.setdefault("residual_goto_tb", "unchained")
    kw.setdefault("entry_chain", "chained")
    return estimate_region(plan, units.__getitem__, index, costs,
                           exits_of=lambda uid: exits[uid], tb_size=0, **kw)


def _crc_like(n=1000, cont_exit=("goto_tb", 0)):
    """caller -chain0-> head; 8-iteration body loop (chain0 back, chain1 out);
    tail returns (goto_ptr, guarded) to the continuation, whose only exit is
    ``cont_exit`` to code outside the region.  Per call: 1 call chain, 7
    loop back edges, 1 loop exit, 1 head->body, 1 guarded return."""
    members = [_member("caller", 0, "caller"), _member("head", 1), _member("cont", 0, "continuation"),
               _member("body", 1), _member("tail", 1)]
    edges = [_edge(0, "x0", "call", 1), _edge(1, "x0", "branch", 3),
             _edge(3, "x0", "branch", 3), _edge(3, "x1", "branch", 4),
             _edge(4, "x0", "return", 2, guarded=True)]
    exits = {"caller": (_x("x0", "goto_tb", 0),), "head": (_x("x0", "goto_tb", 0),),
             "body": (_x("x0", "goto_tb", 0), _x("x1", "goto_tb", 1)),
             "tail": (_x("x0", "goto_ptr"),),
             "cont": (_x("x0", *cont_exit),)}
    cont_kind = {"goto_tb": "chain0", "goto_ptr": "lookup_hit", "exit_null": "exit_null"}[cont_exit[0]]
    rows = [("caller", "head", "chain0", n), ("head", "body", "chain0", n),
            ("body", "body", "chain0", 7 * n), ("body", "tail", "chain1", n),
            ("tail", "cont", "lookup_hit", n), ("cont", "outside", cont_kind, n)]
    execs = {"caller": n, "head": n, "body": 8 * n, "tail": n, "cont": n}
    units = _units({"caller": 100, "head": 100, "body": 100, "tail": 100, "cont": 100})
    return _plan(members, edges), units, exits, _index(rows, execs)


def test_crc_like_leaf_with_unchained_continuation_is_not_profitable():
    plan, units, exits, index = _crc_like()
    est = _estimate(plan, units, exits, index)
    assert est.status == "exact"
    assert est.terms["internal_chain"] == pytest.approx(10_000)   # call + head + 7 back + exit
    assert est.terms["guarded_return"] == pytest.approx(1000)
    assert est.terms["residual_goto_tb_unchained"] == pytest.approx(1000)
    # saved/call: 10 chains*0.4 + entry poll 0.4 + return (7 + 0.4 - 1) = 10.8
    assert est.saved_ns / 1000 == pytest.approx(10.8)
    assert est.added_ns / 1000 == pytest.approx(40.0)
    assert est.net_ns_per_call < -29
    assert admission(est, DispatchCosts()) == "not_profitable"


def test_same_region_admitted_when_residual_goto_tb_chained(monkeypatch):
    plan, units, exits, index = _crc_like(n=100_000)
    est = _estimate(plan, units, exits, index, residual_goto_tb="chained")
    assert est.added_ns == 0 and est.net_ns > 0
    assert est.terms["residual_goto_tb_chained"] == pytest.approx(100_000)
    assert admission(est, DispatchCosts()) is None
    # auto follows the linker's plan-level predicate.
    plan.members[0].source_uid = "caller"
    assert resolve_residual_goto_tb("auto", plan) == "chained"
    import scopt.link as link
    monkeypatch.setattr(link, "region_exits_chainable", lambda plan, grown=(): False)
    assert resolve_residual_goto_tb("auto", plan) == "unchained"
    assert resolve_residual_goto_tb("chained", plan) == "chained"
    assert resolve_residual_goto_tb("auto", _plan(plan.members, plan.edges, trusted=False)) \
        == "chained"
    with pytest.raises(ProfitError):
        resolve_residual_goto_tb("x", plan)


@pytest.mark.parametrize("kind", [("goto_ptr", None), ("exit_null", None)])
def test_continuation_lookup_or_exit_null_exit_costs_nothing(kind):
    plan, units, exits, index = _crc_like(n=100_000, cont_exit=kind)
    est = _estimate(plan, units, exits, index)
    assert est.added_ns == 0
    assert est.terms["residual_dynamic"] == pytest.approx(100_000)
    assert admission(est, DispatchCosts()) is None


def test_poll_exits_are_cost_neutral_and_untrusted_plans_save_no_polls():
    plan, units, exits, index = _crc_like(n=10_000, cont_exit=("goto_ptr", None))
    exits = {**exits, "body": exits["body"] + (_x("x2", "exit_requested"),)}
    index = _index([("caller", "head", "chain0", 10_000), ("head", "body", "chain0", 10_000),
                    ("body", "body", "chain0", 70_000), ("body", "tail", "chain1", 10_000),
                    ("body", "body", "exit_requested", 500),
                    ("tail", "cont", "lookup_hit", 10_000), ("cont", "x", "lookup_hit", 10_000)],
                   {"caller": 10_000, "head": 10_000, "body": 80_000, "tail": 10_000,
                    "cont": 10_000})
    est = _estimate(plan, units, exits, index)
    assert est.added_ns == 0 and est.terms["residual_dynamic"] == pytest.approx(10_000)
    safe = _plan(plan.members, plan.edges, trusted=False)
    est = _estimate(safe, units, exits, index)
    # Only the guarded return saves: lookup - guard, no polls.
    assert est.saved_ns == pytest.approx(10_000 * 6.0)
    assert "entry_polls" not in est.terms or est.terms["entry_polls"] == 0


def test_trusted_exit_into_a_trusted_entry_is_never_chained():
    plan, units, exits, index = _crc_like(n=1000)
    index = _index([("caller", "head", "chain0", 1000), ("head", "body", "chain0", 1000),
                    ("body", "body", "chain0", 7000), ("body", "tail", "chain1", 1000),
                    ("tail", "cont", "lookup_hit", 1000), ("cont", "caller", "chain0", 600),
                    ("cont", "other", "chain0", 400)],
                   {"caller": 1000, "head": 1000, "body": 8000, "tail": 1000, "cont": 1000})
    est = _estimate(plan, units, exits, index, residual_goto_tb="chained")
    assert est.terms["residual_goto_tb_unchained"] == pytest.approx(600)
    est = _estimate(plan, units, exits, index, residual_goto_tb="chained",
                    trusted_entries={"other"})
    assert est.terms["residual_goto_tb_unchained"] == pytest.approx(1000)


def test_entry_chain_unchained_for_pcrel_trusted_capture():
    plan, units, exits, index = _crc_like(n=1000)
    pcrel = _units({"caller": 1}, cflags=CF_PCREL)
    assert resolve_entry_chain("auto", plan, pcrel.__getitem__) == "unchained"
    assert resolve_entry_chain("auto", plan, units.__getitem__) == "chained"
    assert resolve_entry_chain("auto", _plan(plan.members, plan.edges, trusted=False),
                               pcrel.__getitem__) == "chained"
    index.inn["caller"]["chain1"] += 250
    est = _estimate(plan, units, exits, index, residual_goto_tb="chained",
                    entry_chain="unchained")
    assert est.terms["entry_unchained"] == 250
    assert est.added_ns == pytest.approx(250 * 40.0)


def test_ambiguous_same_kind_exits_are_attributed_conservatively():
    plan, units, exits, index = _crc_like(n=1000, cont_exit=("goto_ptr", None))
    exits = {**exits, "tail": (_x("x0", "goto_ptr"), _x("x1", "goto_ptr"))}
    est = _estimate(plan, units, exits, index)
    assert est.status == "ambiguous"
    assert est.terms.get("guarded_return", 0) == 0
    assert est.terms["residual_dynamic"] == pytest.approx(1000)   # tail's lookups stay residual
    assert "cont" not in [m.source_uid for m, f in zip(plan.members, est.member_flow) if f]


def test_closed_loop_scc_is_clamped_to_observed_executions():
    members = [_member("a", 0, "caller"), _member("b", 1)]
    plan = _plan(members, [_edge(0, "x0", "call", 1), _edge(1, "x0", "branch", 0)])
    exits = {"a": (_x("x0", "goto_tb", 0),), "b": (_x("x0", "goto_tb", 0),)}
    index = _index([("a", "b", "chain0", 500), ("b", "a", "chain0", 500)], {"a": 500, "b": 500})
    est = _estimate(plan, _units({"a": 10, "b": 10}), exits, index)
    assert est.member_flow == (500.0, 500.0)
    assert est.status == "exact" and math.isfinite(est.net_ns)


def test_nested_contexts_get_separate_flows():
    # f is called from two sites of the caller; each inline context of f
    # gets the flow of its own call edge, not f's total executions.
    members = [_member("caller", 0, "caller"), _member("f", 1), _member("mid", 1),
               _member("f", 2), _member("cont", 0, "continuation")]
    plan = _plan(members, [_edge(0, "x0", "call", 1), _edge(1, "x0", "return", 2, True),
                           _edge(2, "x0", "call", 3), _edge(3, "x0", "return", 4, True)])
    exits = {"caller": (_x("x0", "goto_tb", 0),), "f": (_x("x0", "goto_ptr"),),
             "mid": (_x("x0", "goto_tb", 0), _x("x1", "goto_tb", 1)),
             "cont": (_x("x0", "goto_ptr"),)}
    index = _index([("caller", "f", "chain0", 100), ("f", "mid", "lookup_hit", 400),
                    ("mid", "f", "chain0", 30), ("mid", "elsewhere", "chain1", 70)],
                   {"caller": 100, "f": 400, "mid": 100, "cont": 100})
    est = _estimate(plan, _units({u: 1 for u in exits}), exits, index)
    assert est.member_flow[1] == pytest.approx(100)
    assert est.member_flow[3] == pytest.approx(30)


def test_captured_executions_reduce_entries():
    plan, units, exits, index = _crc_like(n=1000)
    est = _estimate(plan, units, exits, index, captured={"caller": 750})
    assert est.entries == pytest.approx(250)
    assert est.calls == pytest.approx(250)


def test_costs_from_params_reject_negative_nan_and_strings():
    assert DispatchCosts.from_params({"cost_dispatch_ns": 25, "cost_margin": 0.5}) == \
        DispatchCosts(dispatch_ns=25.0, margin=0.5)
    for bad in (-1.0, float("nan"), float("inf"), "1", True):
        with pytest.raises(ProfitError):
            DispatchCosts.from_params({"cost_poll_ns": bad})


def test_estimate_json_is_deterministic_and_rounded():
    plan, units, exits, index = _crc_like(n=12345)
    a = json.dumps(_estimate(plan, units, exits, index).to_json(), sort_keys=True)
    b = json.dumps(_estimate(plan, units, exits, index).to_json(), sort_keys=True)
    assert a == b
    doc = json.loads(a)
    assert doc["model"] == "firmware-dispatch-v1"
    assert all(round(v, 3) == v for v in doc["terms"].values())


def test_unknown_executions_refuse_the_root():
    plan, units, exits, index = _crc_like(n=1000)
    index._executions = lambda uid: None if uid == "body" else 1000
    est = _estimate(plan, units, exits, index)
    assert est.status == "unknown"
    assert admission(est, DispatchCosts()) == "profit_estimate_unknown"


# -- archived packet data (trebuchet 2026-11-25-seven-o4, config 7) ----------

def _archived():
    doc = json.loads(DATA.read_text())
    rows = []
    for uid, row in doc["profile"].items():
        for key, n in row["out"].items():
            kind, dst = key.split(">")
            rows.append((uid, dst, kind, n))
        if row["chain_in"]:
            rows.append(("outside", uid, "chain0", row["chain_in"]))
    index = _index(rows, {uid: row["executions"] for uid, row in doc["profile"].items()})
    units = {uid: SimpleNamespace(tc_size=u["tc_size"], key=SimpleNamespace(cflags=u["cflags"]))
             for uid, u in doc["units"].items()}
    exits = {uid: tuple(_x(x["id"], x["kind"], x["slot"]) for x in u["exits"])
             for uid, u in doc["units"].items()}
    roots = {}
    for root in doc["roots"]:
        members = [_member(m["uid"], m["context_depth"], m["role"]) for m in root["members"]]
        edges = [_edge(e["source_member"], e["exit_id"], e["kind"], e["target_member"],
                       e["guarded"]) for e in root["edges"]]
        plan = _plan(members, edges, call_exit=root["callsite_exit_id"])
        roots[root["insn_vaddr"], root["target_vaddr"]] = (plan, root)
    return roots, units, exits, index, doc["tb_size"]


def test_archived_packet_crc_root_refused_only_when_exits_are_unchainable():
    roots, units, exits, index, tb_size = _archived()
    plan, root = roots["0x4a70", "0x331c"]
    common = dict(exits_of=exits.__getitem__, tb_size=tb_size, entry_chain="chained")
    unchained = estimate_region(plan, units.__getitem__, index, DispatchCosts(),
                                residual_goto_tb="unchained", **common)
    # The per-byte CRC-CCITT update: about 22 skipped polls, two patched
    # lookups and one guarded return per call against one dispatcher trip.
    assert unchained.calls == root["archived_observed_call_edges"] == 2_317_210
    assert unchained.terms["internal_chain"] / unchained.calls == pytest.approx(22.0, abs=0.1)
    assert unchained.terms["residual_goto_tb_unchained"] == pytest.approx(unchained.calls)
    assert unchained.net_ns_per_call == pytest.approx(-9.6, abs=0.2)
    assert admission(unchained, DispatchCosts()) == "not_profitable"
    chained = estimate_region(plan, units.__getitem__, index, DispatchCosts(),
                              residual_goto_tb="chained", **common)
    assert chained.net_ns_per_call == pytest.approx(30.4, abs=0.2)
    assert admission(chained, DispatchCosts()) is None
    # With the 25% margin the refusal holds while a dispatcher trip costs
    # more than about 30.4 / 1.25 = 24 ns.
    assert admission(estimate_region(plan, units.__getitem__, index,
                                     DispatchCosts(dispatch_ns=24.0),
                                     residual_goto_tb="unchained", **common),
                     DispatchCosts()) is None


def test_archived_packet_root_with_lookup_continuation_is_kept_either_way():
    roots, units, exits, index, tb_size = _archived()
    plan, root = roots["0x82aee", "0x81afc"]
    for mode in ("unchained", "chained"):
        est = estimate_region(plan, units.__getitem__, index, DispatchCosts(),
                              residual_goto_tb=mode, entry_chain="chained",
                              exits_of=exits.__getitem__, tb_size=tb_size)
        assert est.calls == root["archived_observed_call_edges"]
        assert est.net_ns_per_call > 10
        assert admission(est, DispatchCosts()) is None


def test_profit_model_resolves_modes_per_plan():
    plan, units, exits, index = _crc_like(n=1000)
    snapshot = SimpleNamespace(unit=units.__getitem__, header=SimpleNamespace(tb_size=8))
    model = ProfitModel(index, DispatchCosts(), "auto", "auto", "steady")
    est = model.estimate(plan, snapshot, exits_of=exits.__getitem__)
    assert (est.residual_goto_tb, est.entry_chain) == ("chained", "chained")
    assert est.bytes == 5 * 100 + 5 * 8   # per-member descriptors once chainable
    assert model.to_json()["constants"]["dispatch_ns"] == 40.0
    # A member source grown by an earlier B3 pass prices the region unchained
    # (conservative, whatever the QEMU member-skip capability).
    grown = ProfitModel(index, DispatchCosts(), "auto", "auto", "steady",
                        b3_grown=frozenset({plan.members[1].source_uid}))
    assert grown.estimate(plan, snapshot, exits_of=exits.__getitem__).residual_goto_tb == \
        "unchained"

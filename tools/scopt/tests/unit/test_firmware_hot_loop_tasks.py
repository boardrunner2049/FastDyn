"""firmware-inline ``hot-loop-tasks``: hot-loop's roots plus the FirmLooper v3 task-loop
roots the profile did not nominate (O4 candidate A2a).

Discovery stays hot-loop's; v3 loops only nominate extra roots.  Hot and "both" roots
expand, are ordered and are priced exactly as under ``hot-loop``; v3 boundaries (with
the ISB side exit) apply only to static-only roots, which are priced after every hot
root (``PolicySelection.price_after``).
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import scopt.passes.firmware_inline as FI
from scopt.analysis.firmware_calls import discover_firmware_calls
from scopt.analysis.firmware_profit import (DispatchCosts, NestedProfitExpansion, ProfitIndex,
                                            ProfitModel, admission, estimate_region)
from scopt.analysis.rtos_boundaries import ExpansionAction, ExpansionSite
from scopt.decode.guest_symbols import GuestFunc
from scopt.ids import TBKey
from scopt.passes.firmware_inline import pricing_order
from scopt.passes.firmware_policy import (PolicyError, PolicyInput, PolicySelection,
                                          firmware_policy, resolve_selection)

from ..synth.builders import SynthUnit
from .test_firmlooper_v3 import _RET, _goto, _units_snapshot, asm
from .test_firmware_inline import _fake_view, _run_pass
from .test_firmware_inline_isb import _isb_branch_snapshot, _recording
from .test_firmware_profit import _archived

_KEY = TBKey(0x1000, None, 0x110, 0x800400, 0xff020000)
_ONE = {"jmp_reset_offset": (5, 0xffff), "jmp_insn_offset": (1, 0xffff)}
_CALLERS = {"worker": 0x1000, "other": 0x1040, "spin": 0x1080}
_RTOS = frozenset({"rtos_shared_public_lookup", "rtos_public_boundary"})


def _union_snapshot(barriers="dsb sy; isb sy"):
    """Three loops calling one helper; worker and other are tasks, spin is not::

        worker 0x1000: bl helper; nop; b worker      (task)
        other  0x1040: bl helper; nop; b other       (task)
        spin   0x1080: bl helper; nop; b spin        (not a task)
        helper 0x1100: str r1,[r0]; <barriers>  (goto_tb to the next insn)
               <ret>:  bx lr
    """
    callee = asm(0x1100, f"str r1,[r0]; {barriers}")
    ret = 0x1100 + len(callee)
    offsets = [0, *range(2, len(callee), 4)]
    insns = [((0x100 + off, 0, 0), k, k + 1) for k, off in enumerate(offsets)]
    insns[-1] = (insns[-1][0], len(offsets) - 1, 5)
    data = bytearray(ret + 2 - 0x1000)
    units, tb = [], 0x100
    for base in _CALLERS.values():
        units += [SynthUnit(_KEY, tb, b"\xe9\0\0\0\0", guest_bytes=asm(base, "bl 0x1100"),
                            vaddr=base, meta=_goto(0x1100), **_ONE),
                  SynthUnit(_KEY, tb + 0x100, b"\xe9\0\0\0\0", guest_bytes=b"\x00\xbf",
                            vaddr=base + 4, meta=_goto(base + 6), **_ONE)]
        tb += 0x200
        data[base - 0x1000:base - 0x1000 + 8] = asm(base, f"bl 0x1100; nop; b {base:#x}")
    units += [SynthUnit(_KEY, tb, b"\xe9\0\0\0\0", guest_bytes=callee, vaddr=0x1100,
                        icount=len(offsets),
                        meta={**_goto(ret, insn=len(offsets) - 1), "insns": tuple(insns)}, **_ONE),
              SynthUnit(_KEY, tb + 0x100, b"\xff\xe0", guest_bytes=asm(ret, "bx lr"), vaddr=ret,
                        meta=_RET)]
    data[0x100:ret - 0x1000] = callee
    data[ret - 0x1000:] = asm(ret, "bx lr")
    funcs = [GuestFunc(name, base, base + 8) for name, base in _CALLERS.items()]
    funcs.append(GuestFunc("helper", 0x1100, ret + 2))
    return _units_snapshot(units, data, funcs)


def _union_view(snapshot, worker=5000, other=14, spin=3000):
    """Each loop: caller -chain0-> helper -chain0-> ret -lookup_hit-> continuation -> caller."""
    uid = {u.vaddrs[0]: u.uid for u in snapshot.units()}
    ret = max(uid)
    rows = [(uid[0x1100], uid[ret], "chain0", worker + other + spin)]
    for base, n in zip(_CALLERS.values(), (worker, other, spin)):
        rows += [(uid[base], uid[0x1100], "chain0", n), (uid[ret], uid[base + 4], "lookup_hit", n),
                 (uid[base + 4], uid[base], "chain0", n)]
    return uid, _fake_view(snapshot, rows)


_TASKS = {"mode": "hot-loop-tasks", "min_call_count": 1000,
          "policy_options": {"task_entries": ["worker", "other"]}}
_HOT = {"mode": "hot-loop", "min_call_count": 1000}


def _inputs(snapshot, image, view, params):
    profit = ProfitModel(ProfitIndex.build(view, "steady"), DispatchCosts(), "auto", "auto",
                         "steady", frozenset())
    calls = discover_firmware_calls(snapshot, image).calls
    return PolicyInput(snapshot, image, tuple(calls), params, view, profit=profit)


def _root(calls, vaddr):
    return next(c for c in calls if c.insn_vaddr == vaddr)


# 1. descriptor --------------------------------------------------------------------------

def test_descriptor_flags():
    policy = firmware_policy("hot-loop-tasks")
    assert policy.requires_edges and policy.cost_model_default
    assert not policy.expand_nested and policy.discover_loops is None
    hot = firmware_policy("hot-loop")
    assert (policy.requires_edges, policy.expand_nested, policy.cost_model_default) == (
        hot.requires_edges, hot.expand_nested, hot.cost_model_default)


# 2. the union ---------------------------------------------------------------------------

def test_union_of_hot_and_task_loop_roots_without_duplicates():
    snapshot, image = _union_snapshot()
    _, view = _union_view(snapshot)
    inputs = _inputs(snapshot, image, view, _TASKS)
    worker, other, spin = (_root(inputs.calls, v) for v in _CALLERS.values())
    selection = firmware_policy("hot-loop-tasks").select(inputs)
    hot = firmware_policy("hot-loop").select(inputs)
    # Hot roots first, in hot-loop's order; then the static-only root.
    assert hot.roots == (worker.callsite_id, spin.callsite_id)
    assert selection.roots == hot.roots + (other.callsite_id,)
    assert len(set(selection.roots)) == len(selection.roots)
    assert not set(selection.roots) & selection.refusals.keys()
    assert selection.price_after == frozenset({other.callsite_id})
    assert selection.evidence["union"] == {"hot": 2, "static": 2, "static_only": 1, "both": 1}
    assert selection.evidence["hot_loop"] == dict(hot.evidence)
    assert selection.evidence["task_loops"]["analysis_revision"] == 2
    assert selection.evidence["firmlooper_v3"]["boundary_rules"]["isb_boundary"] is True
    ev = selection.site_evidence
    assert ev[worker.callsite_id]["o4_evidence"] == ["profile_hot_loop", "static_task_loop"]
    assert ev[spin.callsite_id]["o4_evidence"] == ["profile_hot_loop"]
    assert ev[spin.callsite_id]["static_refusal"] == "outside_persistent_task_loop"
    assert ev[other.callsite_id]["o4_evidence"] == ["static_task_loop"]
    assert ev[other.callsite_id]["observed_call_edges"] == 14
    assert ev[other.callsite_id]["boundary"]["action"] == "inline"
    admitted, residual = resolve_selection(inputs, selection)
    assert [c.callsite_id for c in admitted] == list(selection.roots) and not residual


def test_price_after_must_name_nominated_roots():
    snapshot, image = _union_snapshot()
    _, view = _union_view(snapshot)
    inputs = _inputs(snapshot, image, view, _TASKS)
    uid = inputs.calls[0].callsite_id
    for bad in (PolicySelection((), price_after=frozenset({uid})),
                PolicySelection((uid,), price_after={uid}),
                PolicySelection((uid,), flat_expansion="no")):
        with pytest.raises(PolicyError, match="price_after|flat_expansion"):
            resolve_selection(inputs, bad)


# 3. invalid parameters -------------------------------------------------------------------

@pytest.mark.parametrize("extra, message", [({"cost_model": "off"}, "cost model"),
                                            ({"nested": "public"}, "nested must be 'profit'")])
def test_unpriced_or_public_nesting_is_invalid_params(monkeypatch, extra, message):
    snapshot, image = _union_snapshot()
    _, view = _union_view(snapshot)
    result, detail, emitted, _ = _run_pass(monkeypatch, snapshot, image, {**_TASKS, **extra}, view)
    assert (result.outcome.value, result.reason) == ("failed", "invalid_params")
    assert message in detail and not emitted


# 4. expansion ------------------------------------------------------------------------------

def test_static_only_roots_get_v3_boundaries_and_hot_roots_hot_loops_decision():
    snapshot, image = _union_snapshot()
    uid, view = _union_view(snapshot)
    inputs = _inputs(snapshot, image, view, _TASKS)
    worker, other, spin = (_root(inputs.calls, v) for v in _CALLERS.values())
    selection = firmware_policy("hot-loop-tasks").select(inputs)
    reference = NestedProfitExpansion(inputs.profit, snapshot, image, inputs.calls, trusted=False)
    isb = ExpansionSite("branch", uid[0x1100], 0x1106, 0x110a)
    kernel = ExpansionSite("branch", uid[0x1000], 0x1000, 0x1500)   # no captured translation
    nested = ExpansionSite("call", uid[0x1100], 0x1100, 0x1100)     # not a captured call
    for root in (worker, spin):                    # hot-only and "both": hot-loop's decision
        for site in (isb, kernel, nested):
            assert selection.expansion(site, (root.callsite_id,)) == reference(site, ())
            assert selection.flat_expansion(site, (root.callsite_id,)) == reference(site, ())
    assert selection.expansion(isb, (worker.callsite_id,)) is ExpansionAction.INLINE
    late = (other.callsite_id,)
    assert selection.expansion(isb, late) == selection.flat_expansion(isb, late) == "isb_boundary"
    assert selection.expansion(kernel, late) is ExpansionAction.PUBLIC
    # Not a residual for v3: the decision falls through to the profit model.
    assert selection.expansion(nested, late) == reference(nested, ()) == "nested_call_unpriced"
    # Root sites (resolve_selection asks with no context) find their root by call site.
    for call in (worker, other, spin):
        site = ExpansionSite("root", call.source_uid, call.insn_vaddr, call.target_vaddr)
        assert selection.expansion(site, ()) is ExpansionAction.INLINE


def _named_g(snapshot_image, name):
    snapshot, image = snapshot_image
    after = max(u.vaddrs[0] for u in snapshot.units() if u.vaddrs[0] < 0x1200)
    funcs = (GuestFunc("worker", 0x1000, 0x1006), GuestFunc("f", 0x1100, after + 2),
             GuestFunc(name, 0x1200, 0x1202))
    return snapshot, replace(image, funcs=funcs)


def _branch_view(snapshot, n=5000):
    """test_firmware_inline_isb's loop: worker -> f -> g -> f tail -> worker."""
    uid = {u.vaddrs[0]: u.uid for u in snapshot.units()}
    return uid, _fake_view(snapshot, [
        (uid[0x1000], uid[0x1100], "chain0", n), (uid[0x1100], uid[0x1102], "chain0", n),
        (uid[0x1102], uid[0x1200], "chain0", n), (uid[0x1200], uid[0x1106], "lookup_hit", n),
        (uid[0x1106], uid[0x1004], "lookup_hit", n), (uid[0x1004], uid[0x1000], "chain0", n)])


def _plans_of(monkeypatch, snapshot, image, params, view, vaddr=0x1000):
    built = _recording(monkeypatch)
    result, receipt, emitted, calls = _run_pass(monkeypatch, snapshot, image, params, view)
    root = _root(calls.calls, vaddr)
    plans = {nested: plan for nested, _, plan in built
             if plan.callsite.callsite_id == root.callsite_id}
    monkeypatch.undo()
    return result, receipt, emitted, plans


def _edge_from(snapshot, plan, vaddr):
    edge, = [e for e in plan.edges
             if snapshot.unit(plan.members[e.source_member].source_uid).vaddrs[0] == vaddr]
    return edge


_BRANCH_TASKS = {"mode": "hot-loop-tasks", "policy_options": {"task_entries": ["worker"]}}


@pytest.mark.parametrize("g", ["g", "xQueueGenericSend"])
def test_static_only_root_keeps_isb_and_rtos_exits_in_recursive_and_flat_plans(monkeypatch, g):
    # worker's call is a task-loop root the profile does not nominate (5000 < 10000).
    snapshot, image = _named_g(_isb_branch_snapshot(), g)
    _, view = _branch_view(snapshot)
    params = {**_BRANCH_TASKS, "min_call_count": 10000}
    _, receipt, _, plans = _plans_of(monkeypatch, snapshot, image, params, view)
    assert receipt["policy"]["union"] == {"hot": 0, "static": 1, "static_only": 1, "both": 0}
    call = _edge_from(snapshot, plans[True], 0x1102)
    if g == "g":
        # g is inlined (depth 2), so the flat fallback plan is built too; both keep the
        # ISB side exit (flat_expansion).
        assert set(plans) == {True, False} and call.kind == "call"
    else:
        assert set(plans) == {True}
        assert call.kind == "side_exit" and call.refusal_reason in _RTOS
    for nested, plan in plans.items():
        isb = _edge_from(snapshot, plan, 0x1108)
        assert (isb.kind, isb.refusal_reason) == ("side_exit", "isb_boundary"), nested
    # The same root under hot-loop: no ISB exit, and the nested call is the profit
    # model's (inlined), whatever g's name.
    _, _, _, hot_plans = _plans_of(monkeypatch, snapshot, image,
                                   {"mode": "hot-loop", "min_call_count": 2}, view)
    assert set(hot_plans) == {True, False}
    for plan in hot_plans.values():
        isb = _edge_from(snapshot, plan, 0x1108)
        assert isb.kind == "branch" and isb.target_member is not None
    assert _edge_from(snapshot, hot_plans[True], 0x1102).kind == "call"


# 5 and 6. hot roots are embedded exactly as under hot-loop ----------------------------------

def _embedded(receipt):
    return {row["callsite_id"]: row for row in receipt["embedded"]}


@pytest.mark.parametrize("barriers, other", [("dsb sy; isb sy", 14), ("dsb sy", 900)])
def test_every_hot_root_is_embedded_as_under_hot_loop(monkeypatch, barriers, other):
    # other's call stays below min_call_count (1000) either way: a static-only root.
    snapshot, image = _union_snapshot(barriers)
    _, view = _union_view(snapshot, other=other)
    _, hot, hot_emitted, calls = _run_pass(monkeypatch, snapshot, image, _HOT, view)
    result, tasks, emitted, _ = _run_pass(monkeypatch, snapshot, image, _TASKS, view)
    worker, spin = _root(calls.calls, 0x1000), _root(calls.calls, 0x1080)
    a, b = _embedded(hot), _embedded(tasks)
    assert set(a) == {worker.callsite_id, spin.callsite_id}
    for cs, row in a.items():
        assert (b[cs]["region_id"], b[cs]["source_uids"], b[cs]["members"]) == (
            row["region_id"], row["source_uids"], row["members"])
        net, got = row["profit"]["net_ns"], b[cs]["profit"]["net_ns"]
        assert abs(got - net) <= max(1.0, 1e-6 * abs(net))
    by_id = {p.region_id: p for p in emitted}
    for p in hot_emitted:                                  # member and edge tuples
        q = by_id[p.region_id]
        assert [(m.source_uid, m.inline_context, m.role) for m in q.members] == [
            (m.source_uid, m.inline_context, m.role) for m in p.members]
        assert q.edges == p.edges
    assert tasks["policy"]["nested_decisions"] == hot["policy"]["nested_decisions"]
    assert tasks["policy"]["union"] == {"hot": 2, "static": 2, "static_only": 1, "both": 1}
    # The static-only root is priced (after the hot roots) and embedded when it pays:
    # 14 calls do not cover its footprint, 900 do.
    late_id = _root(calls.calls, _CALLERS["other"]).callsite_id
    late = b.get(late_id) or next(
        r for r in tasks["residual"] if r["callsite_id"] == late_id and "profit" in r)
    assert late["o4_evidence"] == ["static_task_loop"] and "profit" in late
    assert (late_id in b) == (other == 900)
    if other == 14:
        assert late["reason"] == "not_profitable"
    assert result.reason == "firmware_regions_emitted"


def test_nested_decisions_are_hot_loops(monkeypatch):
    # worker -> f -> g: hot-loop's roots are worker's and f's calls (min 2); worker's is
    # also a task-loop root ("both"), and f's call is inlined inside it as a nested call.
    snapshot, image = _named_g(_isb_branch_snapshot(), "g")
    _, view = _branch_view(snapshot)
    _, hot, _, _ = _run_pass(monkeypatch, snapshot, image,
                             {"mode": "hot-loop", "min_call_count": 2}, view)
    _, both, _, _ = _run_pass(monkeypatch, snapshot, image,
                              {**_BRANCH_TASKS, "min_call_count": 2}, view)
    assert both["policy"]["union"] == {"hot": 2, "static": 1, "static_only": 0, "both": 1}
    assert hot["policy"]["nested_decisions"]
    assert both["policy"]["nested_decisions"] == hot["policy"]["nested_decisions"]
    assert [r["region_id"] for r in both["embedded"]] == [r["region_id"] for r in hot["embedded"]]
    assert both["policy"]["nested_fallbacks"] == hot["policy"]["nested_fallbacks"]
    # A static-only root asks the same profit model, so a nested call it reaches gets the
    # row hot-loop records for that call (one model instance, one row per call site).
    _, late, _, _ = _run_pass(monkeypatch, snapshot, image,
                              {**_BRANCH_TASKS, "min_call_count": 10000}, view)
    assert late["policy"]["nested_decisions"] == hot["policy"]["nested_decisions"]


# 7. pricing order and trusted entries ------------------------------------------------------

def _call(cs, caller):
    return SimpleNamespace(callsite_id=cs, source_uid=caller)


def _members(*uids, complete=True):
    return SimpleNamespace(members=tuple(SimpleNamespace(source_uid=u) for u in uids),
                           complete=complete)


def test_pricing_order_puts_price_after_roots_last():
    # L's region contains A's and B's callers; B's region contains A's caller.
    calls = [_call("L", "l"), _call("A", "a"), _call("B", "b")]
    plans = [_members("l", "a", "b"), _members("a", "x"), _members("b", "a")]
    assert pricing_order(calls, plans) == [0, 2, 1]                    # L, B, A
    assert pricing_order(calls, plans, frozenset({"L"})) == [2, 1, 0]  # B, A, L
    # The roots outside price_after keep the order they have without L.
    assert [calls[1:][i].callsite_id for i in pricing_order(calls[1:], plans[1:])] == ["B", "A"]
    # An incomplete plan covers nothing.
    assert pricing_order(calls, [_members("l", "a", "b", complete=False)] + plans[1:]) == [0, 2, 1]


def test_hot_roots_are_priced_without_the_late_roots_callers(monkeypatch):
    import scopt.link as L

    seen = []
    estimate = ProfitModel.estimate

    def record(self, plan, snapshot, **kw):
        if "captured" in kw:                     # the pass's whole-region price
            seen.append((plan.callsite.insn_vaddr, frozenset(kw["trusted_entries"])))
        return estimate(self, plan, snapshot, **kw)

    monkeypatch.setattr(ProfitModel, "estimate", record)
    monkeypatch.setattr(L, "trusted_firmware_entry_problem", lambda unit: None)
    monkeypatch.setattr(L, "trusted_firmware_edge_problem", lambda unit, exit_id: None)
    monkeypatch.setattr(L, "trusted_return_guard_problem", lambda *args, **kw: None)
    snapshot, image = _union_snapshot("dsb sy")
    snapshot.run_info = SimpleNamespace(flags=0)
    uid, view = _union_view(snapshot)
    trusted = {"trust": "user", "interrupts": "disabled", "icount": "ignored"}
    for params in ({**_HOT, **trusted}, {**_TASKS, **trusted}):
        seen.clear()
        # The toy loop's continuation exits into the region's own trusted entry, which
        # is never chained, so nothing is admitted; every planned root is still priced.
        _run_pass(monkeypatch, snapshot, image, params, view)
        order = [vaddr for vaddr, _ in seen]
        entries = dict(seen)
        if params["mode"] == "hot-loop":
            hot_seen = dict(seen)
            assert sorted(order) == [0x1000, 0x1080]
            continue
        assert order[-1] == 0x1040 and sorted(order[:-1]) == [0x1000, 0x1080]
        for vaddr in (0x1000, 0x1080):           # exactly hot-loop's entries
            assert entries[vaddr] == hot_seen[vaddr]
            assert uid[0x1040] not in entries[vaddr]
        assert entries[0x1040] == {uid[0x1000], uid[0x1080]}


def test_archived_packet_hot_root_price_is_unchanged_by_a_late_root():
    """Design test 5 on tests/unit/data/firmware_profit_packet.json.  The file holds the
    archived plans and profile only (no guest image), so the policy cannot run on it; this
    prices the archived roots as the pass does (pricing_order, trusted entries, captured
    flow).  A late root whose region contains the CRC root's caller would capture its flow
    if it were priced first."""
    roots, units, exits, index, tb_size = _archived()
    crc_plan, _ = roots["0x4a70", "0x331c"]
    other_plan, _ = roots["0x82aee", "0x81afc"]
    crc, other = (SimpleNamespace(**vars(p), complete=True) for p in (crc_plan, other_plan))
    calls = {"late": _call("late", crc_plan.members[1].source_uid),
             "crc": _call("crc", crc_plan.members[0].source_uid),
             "other": _call("other", other_plan.members[0].source_uid)}
    plans = {"late": crc, "crc": crc, "other": other}        # the late region covers crc

    def price(names, price_after=frozenset()):
        planned = [calls[n] for n in names]
        early = {c.source_uid for c in planned if c.callsite_id not in price_after}
        captured, net, order = {}, {}, []
        for i in pricing_order(planned, [plans[n] for n in names], price_after):
            call = planned[i]
            entries = ({c.source_uid for c in planned} if call.callsite_id in price_after
                       else early) - {call.source_uid}
            est = estimate_region(plans[call.callsite_id], units.__getitem__, index,
                                  DispatchCosts(), residual_goto_tb="chained",
                                  entry_chain="chained", exits_of=exits.__getitem__,
                                  tb_size=tb_size, trusted_entries=entries, captured=captured)
            net[call.callsite_id] = est.net_ns
            order.append(call.callsite_id)
            if admission(est, DispatchCosts()) is None:
                for member, flow in zip(plans[call.callsite_id].members, est.member_flow):
                    captured[member.source_uid] = captured.get(member.source_uid, 0.0) + flow
        return net, order

    hot, hot_order = price(["crc", "other"])                       # hot-loop
    tasks, order = price(["late", "crc", "other"], frozenset({"late"}))
    assert order == hot_order + ["late"]
    assert hot["crc"] > 0
    for cs in hot:
        assert abs(tasks[cs] - hot[cs]) <= max(1.0, 1e-6 * abs(hot[cs]))
    # Without price_after the late region is priced first and captures the CRC caller's
    # flow, so the CRC root's price changes.
    plain, plain_order = price(["late", "crc", "other"])
    assert plain_order.index("late") < plain_order.index("crc")
    assert abs(plain["crc"] - hot["crc"]) > 1.0

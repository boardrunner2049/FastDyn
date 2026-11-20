"""WP25: syncdead@1, the eliminate-state-sync@1 pass and its contract (design §7).

Real-snapshot tests read the B1/B2 store snapshots and the WP17 RuntimeIdentity
v2 documents (read-only; skipped when absent). Rule tests change one real
unit's bytes in memory.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest

from scopt.analysis.hcfg import build_hcfg
from scopt.analysis.hostops import OpKind, build_hostops
from scopt.analysis.obs_contract import build_contract
from scopt.analysis.syncdead import (NOPS, SPECIAL_CFLAGS, B4Reason, TrackedBytes,
                                     census_syncdead, nop_bytes, syncdead_unit)
from scopt.decode.x86 import sweep
from scopt.formats.envlayout_v1 import find_envlayout
from scopt.passes.base import EditKind, Outcome
from scopt.passes.eliminate_state_sync import (EliminateStateSync, StrictCallContract,
                                               class_ledger_rows, parse_classes, plan_of,
                                               propose_b4, strict_contract)
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
SNAPS = {
    "minimal": ROOT / "runs/wp15a/minimal/store/snapshots/"
    "f1ed14d98db4bcae54a1c37f07b1d6fabcfcea345a56edf1df1846c4d0aaee44",
    "fault": ROOT / "runs/wp15a/fault/store/snapshots/"
    "4e404d5e7335399768249ef95453594fe8098647ef7927c1234c5a27dee16841",
    "blinky": ROOT / "runs/b2/blinky/store/snapshots/"
    "4879c8f7d4c10a5af3091555bba1f7744d06d95072bed87a219408dc8935ef2f",
    "compute": ROOT / "runs/b2/compute/store/snapshots/"
    "53b4132fc05526ff2925cd3acc610bf613e8b11e2315e15e520bff7fe080dab2",
}
IDENT = ROOT / "runs/wp17/admission"
#: map-state-sync-traffic §7.3/§7.4 (runs/b3-explore/judge-feasibility/cdi.json): static, all units
MODEL = {"D1": 469, "D2": 1665, "D2_prefix": 678, "D3": 5681}
_CACHE: dict[str, object] = {}


def snap(name: str):
    p = SNAPS[name]
    if not (p / "snapshot.json").is_file():
        pytest.skip(f"{p} not present")
    if name not in _CACHE:
        from scopt.importer.import_cache import open_snapshot

        _CACHE[name] = open_snapshot(p)
    return _CACHE[name]


def ident(name: str) -> dict:
    p = IDENT / f"{name}.runtime_identity.v2.json"
    if not p.is_file():
        pytest.skip(f"{p} not present")
    return json.loads(p.read_text())


def ctx(name: str, **adm):
    s = snap(name)
    ri = ident(name)
    if adm:
        ri = dict(ri, admission={**ri["admission"], **adm})
    lay = find_envlayout(ri["qemu_sha256"])
    assert lay is not None
    return s, lay, ri, build_contract(lay, ri)


def unit_at(s, vaddr: int):
    us = [u for u in s.units() if u.vaddrs and u.vaddrs[0] == vaddr and
          not u.key.cflags & SPECIAL_CFLAGS]
    if not us:
        raise KeyError(hex(vaddr))
    return us[0]


def sd(s, lay, c, u, **kw):
    h = build_hostops(u, lay, s.header.code_address)
    return syncdead_unit(u, h, build_hcfg(h), lay, c, **kw), h


def patch(u, off: int, new: bytes):
    code = bytearray(u.code)
    code[off:off + len(new)] = new
    return dataclasses.replace(u, code=bytes(code))


# -- small pieces -------------------------------------------------------------------------------

def test_nops_are_one_instruction_each():
    for n in NOPS:
        b = nop_bytes(n)
        xs = sweep(b, 0)
        assert len(b) == n and len(xs) == 1 and xs[0].mnemonic.startswith("nop"), n
    assert nop_bytes(12) is None


def test_parse_classes():
    assert parse_classes(None) == ("D1", "D2", "D3")
    assert parse_classes("D2|d1") == ("D1", "D2")
    assert parse_classes("D3,D1") == ("D1", "D3")
    assert parse_classes(["D2"]) == ("D2",)
    for bad in ("D4", "x", "|"):
        with pytest.raises(ValueError):
            parse_classes(bad)


def test_tracked_bytes_follow_the_envlayout():
    s, lay, ri, c = ctx("fault")
    tb = TrackedBytes.of(lay)
    assert tb.span["neg.can_do_io"] == (-4, 1)
    assert tb.span["neg.plugin_mem_value_high"] == (-0x10, 8)  # G21 (survey: −0x10, not −0x18)
    assert tb.span["neg.plugin_mem_value_low"] == (-0x18, 8)
    assert tb.span["NF"] == (0x218, 4) and tb.span["regs[0]"] == (0, 4)
    assert len(tb.index) == 19 * 4 + 1 + 16


def test_strict_contract_reads_can_do_io_at_every_call():
    s, lay, ri, c = ctx("fault")
    sc = strict_contract(c)
    assert isinstance(sc, StrictCallContract) and sc.contract_id != c.contract_id
    for ev in ("P", "S3", "S4", "S5", "S6"):
        assert sc.observable("neg.can_do_io", ev)
    assert not c.observable("neg.can_do_io", "P") and not c.observable("neg.can_do_io", "S3")
    assert sc.observable("neg.plugin_mem_value_low", "S6")
    assert not sc.observable("neg.plugin_mem_value_low", "P")  # D3: S4/S6 only
    assert not sc.observable("regs[0]", "P")  # D1 unchanged


# -- the dataflow on real units (fault: 48 units, no icount) ------------------------------------

@pytest.mark.real
def test_every_dead_store_is_a_placed_plain_store():
    s, lay, ri, c = ctx("fault")
    n = 0
    for u in s.units():
        r, h = sd(s, lay, c, u)
        for d in r.dead:
            o = h.ops[d.op]
            assert o.kind in (OpKind.ENV_STORE_IMM, OpKind.ENV_STORE_REG)
            assert c.deletable_class(d.field) == d.cls
            assert d.old == bytes(u.code[d.off:d.off + d.size])
            assert o.block == "insn" or (o.block == "prefix" and d.field == "neg.can_do_io")
            if o.block == "insn":
                assert any(i.host_start <= d.off and d.off + d.size <= i.host_end
                           for i in u.insns)
            n += 1
    assert n > 0


@pytest.mark.real
def test_special_cflags_units_are_skipped():
    s, lay, ri, c = ctx("fault")
    sp = [u for u in s.units() if u.key.cflags & SPECIAL_CFLAGS]
    assert sp
    for u in sp:
        r, _ = sd(s, lay, c, u)
        assert not r.ok and r.reason == B4Reason.SPECIAL_CFLAGS_UNIT
        r2, _ = sd(s, lay, c, u, skip_special=False)
        assert r2.reason != B4Reason.SPECIAL_CFLAGS_UNIT


@pytest.mark.real
def test_d3_needs_the_mem_get_value_fact():
    s, lay, ri, c = ctx("fault", mem_get_value_outside_mem_cb=None)
    assert c.deletable_class("neg.plugin_mem_value_low") is None
    for u in s.units():
        r, _ = sd(s, lay, c, u)
        assert not any(d.cls == "D3" for d in r.dead)


@pytest.mark.real
def test_direct_chain_rule_keeps_can_do_io_true_before_goto_tb():
    """R5 / D2b: with tb_start_cb_possible the last-insn ``can_do_io = 1`` before a
    direct-chain exit is needed; only D2a (prefix ``false`` stores) remain."""
    s, lay, ri, c = ctx("compute")
    ct = build_contract(lay, dict(ri, admission={**ri["admission"],
                                                 "tb_start_cb_possible": True}))
    u = unit_at(s, 0x1D5A)
    r0, h = sd(s, lay, c, u)
    r1, _ = sd(s, lay, ct, u)
    d2_0 = {(d.off, d.text) for d in r0.dead if d.cls == "D2"}
    d2_1 = {(d.off, d.text) for d in r1.dead if d.cls == "D2"}
    assert any(t.endswith(", 1") for _, t in d2_0)  # the last-insn true store (tbcb false)
    assert d2_1 and all(t.endswith(", 0") and h.op_at(o).block == "prefix" for o, t in d2_1)
    assert {(d.off, d.cls) for d in r0.dead if d.cls == "D1"} == \
        {(d.off, d.cls) for d in r1.dead if d.cls == "D1"}


@pytest.mark.real
def test_hot_tb_0x1d5a_shape():
    """map-state-sync-traffic §7.2: the first NF/ZF/CF/VF set of 0x1d5a is dead (D1),
    both can_do_io stores are dead (D2); TCG's own sync set (M0) kills nothing."""
    s, lay, ri, c = ctx("compute")
    u = unit_at(s, 0x1D5A)
    r, h = sd(s, lay, c, u)
    d1 = sorted((d.off, d.field) for d in r.dead if d.cls == "D1")
    assert [f for _, f in d1][:4] == ["NF", "ZF", "CF", "VF"]
    assert sum(d.cls == "D2" for d in r.dead) == 2
    m0, _ = sd(s, lay, c, u, mode="tcg")
    assert not any(d.cls == "D1" for d in m0.dead)


@pytest.mark.real
def test_reload_after_branch_keeps_the_store():
    """[R7] an env load of the field before its next store makes the store live
    (TCG reloads globals after a brcond/label): replace the second NF store of
    0x1d5a by a load of NF; the first NF store, dead before, becomes live."""
    s, lay, ri, c = ctx("compute")
    u = unit_at(s, 0x1D5A)
    r, h = sd(s, lay, c, u)
    nf = [o for o in h.ops if o.is_env_store and o.store_field == "NF"]
    first, second = nf[0], nf[1]
    assert any(d.off == first.off for d in r.dead)
    # mov r12d, [rbp+0x218] (7 bytes: 44 8b a5 18 02 00 00) replaces the second NF store
    u2 = patch(u, second.off, bytes.fromhex("448ba518020000"))
    r2, _ = sd(s, lay, c, u2)
    assert not any(d.off == first.off for d in r2.dead)
    assert any(x.off == first.off and x.reason == B4Reason.OBSERVED_AT_SYNC_POINT
               for x in r2.refused)


@pytest.mark.real
def test_every_call_reads_can_do_io():
    """Survey §3c B4-2 (1): with call_reads_all the store before the lookup call is kept."""
    s, lay, ri, c = ctx("compute")
    hit = 0
    for u in list(s.units())[:600]:
        r1, h = sd(s, lay, c, u)
        r0, _ = sd(s, lay, c, u, call_reads_all=False)
        a = {d.off for d in r1.dead if d.cls == "D2"}
        b = {d.off for d in r0.dead if d.cls == "D2"}
        assert a <= b
        hit += len(b - a)
    assert hit > 0


# -- T-B4-1 on compute ----------------------------------------------------------------------------

@pytest.mark.real
@pytest.mark.slow
def test_t_b4_1_model_counts_and_m0():
    s, lay, ri, c = ctx("compute")
    allu = census_syncdead(s.units(), lay, s.header.code_address, c, skip_special=False)
    for k in ("D1", "D2", "D3"):
        assert abs(allu["dead"][k] - MODEL[k]) <= 0.05 * MODEL[k], (k, allu["dead"][k])
    assert allu["d2_split"]["prefix"] == MODEL["D2_prefix"]
    m0 = census_syncdead(s.units(), lay, s.header.code_address, c, mode="tcg",
                         skip_special=False)
    assert m0["dead"]["D1"] == 0
    ct = build_contract(lay, dict(ri, admission={**ri["admission"],
                                                 "tb_start_cb_possible": True}))
    tr = census_syncdead(s.units(), lay, s.header.code_address, ct, skip_special=False)
    assert tr["dead"]["D2"] < allu["dead"]["D2"] and tr["dead"]["D1"] == allu["dead"]["D1"]


# -- the pass ----------------------------------------------------------------------------------

@pytest.mark.real
def test_propose_b4_per_class_and_ledger():
    s, lay, ri, c = ctx("fault")
    full = propose_b4(s, layout=lay, runtime=ri)
    assert full.result.outcome is Outcome.CANDIDATE
    only = propose_b4(s, layout=lay, runtime=ri, classes="D2")
    assert only.by_class()["D1"] == 0 and only.by_class()["D3"] == 0
    assert only.by_class()["D2"] == full.by_class()["D2"] > 0
    assert only.result.stats.get("D3:class_not_selected", 0) == full.by_class()["D3"]
    rows = {r.id: r for r in class_ledger_rows(only).rows}
    assert rows["eliminate-state-sync@1/D2"].verdict == "candidate"
    assert rows["eliminate-state-sync@1/D3"].reason == "class_not_selected"
    for uid, ps in full.patches.items():
        for p in ps:
            assert p.template_id == "b4_nop_store" and len(p.new_bytes) == len(p.old_bytes)
            assert len(sweep(bytes(p.new_bytes), 0)) == 1
    plan = plan_of(full)
    assert not plan.register_slots and not plan.grow and set(plan.rewrites) == set(full.patches)


@pytest.mark.real
def test_v1_identity_admits_no_d2_at_direct_exits_and_no_d3():
    s, lay, ri, c = ctx("fault")
    v1 = {k: v for k, v in ri.items() if k != "admission"}
    v1["schema"] = "scopt.runtime_identity/1"
    p = propose_b4(s, layout=lay, runtime=v1)
    assert p.contract.tb_start_cb_possible is True
    assert p.by_class()["D3"] == 0


@pytest.mark.real
def test_b2_manager_refuses_the_b4_pipeline():
    """Fail closed until WP21: PURE_REWRITE is not enabled by the B2 PassManager."""
    from scopt.manager import PassManager

    s = snap("fault")
    cand = PassManager(s, Path(__file__).parents[2] / "pipelines/b4.toml",
                       enable=("eliminate-state-sync",)).execute()
    rec = cand.passes[0]
    assert rec.outcome is Outcome.SKIPPED_UNSUPPORTED and rec.reason == "capability_not_enabled"
    assert not cand.edits


def test_descriptor():
    d = EliminateStateSync.descriptor
    assert d.name == "eliminate-state-sync" and d.capabilities == {EditKind.PURE_REWRITE}
    assert ("syncdead", 1) in d.requires


@pytest.mark.real
def test_v15_b4_deletion_class_check():
    """V15 (WP25 addition): a b4_nop_store patch must replace exactly one plain env
    store of a deletable class by NOPs."""
    from scopt.passes.base import Patch
    from scopt.validate import _b4_deletion_problems

    s, lay, ri, c = ctx("compute")
    u = unit_at(s, 0x1D5A)
    h = build_hostops(u, lay, s.header.code_address)

    def probs(o, new=None):
        old = bytes(u.code[o.off:o.end])
        p = Patch(u.uid, o.off, old, new or nop_bytes(o.size), "b4_nop_store")
        return _b4_deletion_problems(h, p, c)

    nf = next(o for o in h.ops if o.is_env_store and o.store_field == "NF")
    pc = next(o for o in h.ops if o.is_env_store and o.store_field == "regs[15]")
    ld = next(o for o in h.ops if o.kind == OpKind.ENV_LOAD)
    assert probs(nf) == []
    assert any("pc_store_required" in x for x in probs(pc))
    assert any("not a plain env store" in x for x in probs(ld))
    assert any("NOPs only" in x for x in probs(nf, b"\xcc" * nf.size))
    v1 = build_contract(lay, None)  # no admission facts: D3 is not deletable
    for u2 in s.units():
        h2 = build_hostops(u2, lay, s.header.code_address)
        o = next((o for o in h2.ops if o.is_env_store and
                  o.store_field == "neg.plugin_mem_value_low"), None)
        if o is not None:
            break
    p = Patch(u2.uid, o.off, bytes(u2.code[o.off:o.end]), nop_bytes(o.size), "b4_nop_store")
    assert _b4_deletion_problems(h2, p, c) == []
    assert any("observer_contract_unknown" in x for x in _b4_deletion_problems(h2, p, v1))

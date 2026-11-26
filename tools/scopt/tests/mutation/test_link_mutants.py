"""WP19 integration: the §9.3 guard/link mutants built with WP18's emitter and placement.

Every mutant starts from a NativeLinker v2 result (WP18 ``grow_unit`` stub,
``NativeLinkerV2`` placement and ext table) and is checked with
``scopt.validate_sym.link_check.certify_link`` (the V12/V13/V14 inputs of
``validate_b3``). Real-snapshot tests skip when the B2 compute snapshot is absent.
"""

from __future__ import annotations

import pytest

from scopt.validate_sym import check_guarded_chain, patched
from scopt.validate_sym.asm import nop
from scopt.validate_sym.mutants import (MINI_LAYOUT, OFF_CDIO, OFF_R, OFF_THUMB, GuardSpec,
                                        SynthContract, SynthUnit)
from tests.synth import b3 as H

CF = 0xFF022000


# ---------------------------------------------------------------------------
# synthetic: can_do_io survives a returning LDST (pop {..., pc} shape)
# ---------------------------------------------------------------------------

def _unit_pop(guard: GuardSpec | None = None, uid: str = "synth:pop"):
    """R: ``pop {pc}`` — can_do_io = 1, a guest load of the return address, pc/thumb
    from the loaded value, lookup tail (or the reference guard)."""
    u = SynthUnit(uid, icount=1)
    a = u.a
    u.prefix(cdio0=False)
    u.rec("cdio1", lambda: a.mov_env_imm(OFF_CDIO, 1, w=1))
    a.mov_r_env("r12", OFF_R[13])
    u.ldst_block(0, is_ld=True)

    def effects() -> None:
        a.alu_r_r("mov", "rbx", "r13")
        a.alu_r_r("mov", "r14", "rbx")
        a.alu_r_imm("and", "r14", 0xFFFFFFFE)
        a.mov_env_r(OFF_R[15], "r14")
        a.alu_r_imm("and", "rbx", 1)
        a.mov_env_r(OFF_THUMB, "rbx", w=1)
    u.tail_or_jump(guard, effects)
    return u.build(guard=guard), u


def test_cdio_after_ldst_guarded_chain_equivalent():
    """The lookup sets can_do_io = 1 (original side); R stored 1 before its last insn's
    load. No access helper that returns writes can_do_io, so R' at the slot still shows
    1 under tb_start_cb_possible (a pre-fix kernel havocked it: a false mismatch)."""
    r, _ = _unit_pop()
    rp, _ = _unit_pop(GuardSpec(), uid="synth:pop'")
    v = check_guarded_chain(r, rp, SynthContract(True), layout=MINI_LAYOUT)
    assert v.ok, v.to_json()


def test_cdio_store_deleted_before_ldst_rejected():
    r, _ = _unit_pop()
    rp, ru = _unit_pop(GuardSpec(), uid="synth:pop'")
    o, n = ru.at["cdio1"]
    v = check_guarded_chain(r, patched(rp, [(o, nop(n))]), SynthContract(True),
                            layout=MINI_LAYOUT)
    assert v.status == "mismatch", v.to_json()
    # without tb_start_cb_possible the slot does not observe can_do_io, but the guest
    # access does (S5: an MMIO access with can_do_io false io-recompiles)
    v2 = check_guarded_chain(r, patched(rp, [(o, nop(n))]), SynthContract(False),
                             layout=MINI_LAYOUT)
    assert v2.status == "mismatch" and "guest:S5" in v2.detail, v2.to_json()


# ---------------------------------------------------------------------------
# real units (compute), WP18 emitter and placement
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ctx():
    from scopt.analysis.obs_contract import build_contract

    s = H.snap("compute")
    lay = H.layout_for(s)
    return s, lay, build_contract(lay, s.runtime), H.offsets_for(s), H.guest_bytes(s)


def _at(s, v):
    return next(u for u in s.units() if u.vaddrs and u.vaddrs[0] == v and u.key.cflags == CF)


def _items(ctx, n, classes=("return", "static")):
    from scopt.validate_sym.link_mutants import growable

    s, _, _, _, gb = ctx
    its = growable(s, gb, classes)
    rets = [x for x in its if x[2] == "return"]
    stat = [x for x in its if x[2] == "static"]
    return (rets[:n // 2] + stat[:n - n // 2]) if n else its


def _link(s, plan, off):
    from scopt.link import NativeLinkerV2

    res = NativeLinkerV2(s, plan, offsets=off, derive_captures=False).link()
    assert res.ok, res.failures
    return res


@pytest.mark.parametrize("variant", ["v6_registered", "v5_unregistered", "kf2_imm64"])
def test_guard_mutants_wp18_emitter(ctx, variant):
    from scopt.validate_sym.link_mutants import general_plan, guard_mutants

    s, lay, c, off, _ = ctx
    items = _items(ctx, 24)
    plan = general_plan(s, items, kf2_high=variant == "kf2_imm64")
    if variant == "v5_unregistered":
        plan.register_slots = False
    res = _link(s, plan, off)
    if variant == "kf2_imm64":
        assert all(st.kf2 >= 1 << 31 for g in res.grown.values() for _, st in g.stubs)
    suite = guard_mutants(s, res, layout=lay, contract=c, offsets=off)
    sm = suite.summary()
    assert sm["controls_ok"], sm["controls_failed"]
    assert sm["controls"] == len(items)
    assert sm["all_rejected"] and sm["all_mismatch"], [r.to_json() for r in suite.results
                                                       if not r.mismatch][:3]
    kinds = set(sm["by_class"])
    assert {"drop_g_bp", "drop_g_thumb", "drop_g_cond", "drop_g_flags", "drop_g_flags2",
            "drop_g_pc[0]", "guard_stub_store", "guard_before_effect"} <= kinds
    # each atom drop is rejected because a declared atom is not entailed (or, for g_pc
    # with two targets, the trace differs); never only inconclusive
    for r in suite.results:
        if r.name.startswith("drop_g_") and r.name != "drop_g_pc[1]":
            assert any("guard_contract_violation" in x or "validation_mismatch" in x
                       for x in r.reasons), r.to_json()


def _spec_1ede(s, r, c, n=0):
    from scopt.guard import GuardTarget
    from scopt.link import GuardedExitSpec, lookup_tails

    t = lookup_tails(r)[0]
    return GuardedExitSpec(t.off, "return", (GuardTarget(c.vaddrs[0], n, "SPEC"),),
                           c.key.flags, c.key.cs_base)


def _patch(u, off, new, tmpl):
    from scopt.passes.base import Patch

    return Patch(u.uid, off, bytes(u.code[off:off + len(new)]), new, tmpl)


def _t1(c):
    return (_patch(c, 0x15, bytes.fromhex("0f1f00"), "dho_nop"),
            _patch(c, 0x18, bytes.fromhex("6690"), "dho_nop"),
            _patch(c, 0x1A, bytes.fromhex("660f1f440000"), "bf_jcc_to_nop"))


def test_falsified_fact_link_0x1ede(ctx):
    """F-B3-1 shape on compute: R = 0x1ede (movs r0,#0; bx lr), C = 0x24f0 (cbz r0)."""
    from scopt.link import B3Plan, VariantSpec
    from scopt.validate_sym.link_check import certify_link
    from scopt.validate_sym.link_mutants import synthetic_fact

    s, lay, c, off, _ = ctx
    R, C = _at(s, 0x1EDE), _at(s, 0x24F0)
    good = _link(s, B3Plan(grow={R.uid: (_spec_1ede(s, R, C),)}, variants=(
        VariantSpec(C.uid, R.uid, 0, patches=_t1(C), facts=(synthetic_fact("r0", 0, "g"),)),)),
        off)
    lc = certify_link(s, good, layout=lay, contract=c, offsets=off)
    assert not lc.rejected, lc.failures()
    assert lc.facts[0]["cert"].certified and lc.facts[0]["cert"].value == 0
    # C' folded for r0 == 1 (cbz never taken) claiming r0 == 1: V14 refuses the link
    nz = (_patch(C, 0x1A, bytes.fromhex("e91d00000090"), "bf_jcc_to_jmp"),)
    bad = _link(s, B3Plan(grow={R.uid: (_spec_1ede(s, R, C),)}, variants=(
        VariantSpec(C.uid, R.uid, 0, patches=nz, facts=(synthetic_fact("r0", 1, "b"),)),)), off)
    lb = certify_link(s, bad, layout=lay, contract=c, offsets=off)
    assert lb.rejected
    assert any("fact_derivation_mismatch" in x for x in lb.failures()), lb.failures()
    # the same nz-fold variant claiming the true fact r0 == 0 fails V12 on C'
    wrong = _link(s, B3Plan(grow={R.uid: (_spec_1ede(s, R, C),)}, variants=(
        VariantSpec(C.uid, R.uid, 0, patches=nz, facts=(synthetic_fact("r0", 0, "w"),)),)),
        off)
    lw = certify_link(s, wrong, layout=lay, contract=c, offsets=off)
    assert any(v.status == "mismatch" for _, v in lw.equiv), lw.failures()


def test_shared_variant_0x1ede_suffix_0x1ee0(ctx):
    """F-B3-20 shape: 0x1ee0 is the suffix TB of 0x1ede (bx lr only); r0 == 0 holds
    at 0x1ede's slot but not at 0x1ee0's. Sharing 0x1ede's variant is refused."""
    from scopt.link import B3Plan, VariantSpec
    from scopt.validate_sym.link_check import certify_link
    from scopt.validate_sym.link_mutants import add_link, synthetic_fact

    s, lay, c, off, _ = ctx
    R, RS, C = _at(s, 0x1EDE), _at(s, 0x1EE0), _at(s, 0x24F0)
    f0 = synthetic_fact("r0", 0, "full")
    plan = B3Plan(grow={R.uid: (_spec_1ede(s, R, C),), RS.uid: (_spec_1ede(s, RS, C),)},
                  variants=(VariantSpec(C.uid, R.uid, 0, patches=_t1(C), facts=(f0,)),
                            VariantSpec(C.uid, RS.uid, 0)))
    good = _link(s, plan, off)
    lc = certify_link(s, good, layout=lay, contract=c, offsets=off)
    assert not lc.rejected, lc.failures()
    owner = {lu.uid: lu.new_tb_offset for lu in good.units}
    si = next(i for i, x in enumerate(good.ext.slots) if x.src_tb_offset == owner[RS.uid])
    vf = next(v.index for v in good.variants if v.pred_uid == R.uid)
    shared = add_link(good, si, vf)
    ls = certify_link(s, shared, layout=lay, contract=c, offsets=off)
    assert ls.rejected
    assert any(f["cert"].status == "not_constant" for f in ls.facts), ls.failures()
    # the plan form: a second copy of the same specialized variant for the suffix
    dup = _link(s, B3Plan(grow=plan.grow, variants=(
        VariantSpec(C.uid, R.uid, 0, patches=_t1(C), facts=(f0,)),
        VariantSpec(C.uid, RS.uid, 0, patches=_t1(C), facts=(f0,)))), off)
    ld = certify_link(s, dup, layout=lay, contract=c, offsets=off)
    assert ld.rejected and any("not_constant" in x for x in ld.failures())
    # and the V12 side: C' folded under r0 == 0 entered with r0 unknown
    from scopt.validate_sym import check_equiv
    from scopt.validate_sym.link_check import c_prime, c_reference
    from scopt.validate_sym.program import world_of

    w = world_of(s, lay)
    v = next(v for v in dup.variants if v.pred_uid == RS.uid)
    assert check_equiv(c_reference(dup, v, s, w, offsets=off), c_prime(dup, v, s, w), None,
                       None, c, layout=lay).status == "mismatch"


def test_variant_of_a_grown_continuation(ctx):
    """NativeLinker v2 clones the effective (grown) C; the kernel compares C' with the
    grown C (its stub and slots), not with the captured C."""
    from scopt.validate_sym.link_check import certify_link
    from scopt.validate_sym.link_mutants import _spec_c_pool, _spec_plan

    s, lay, c, off, _ = ctx
    items = _items(ctx, 0)
    pool = {u.uid for u in _spec_c_pool(s)}
    citem = next(x for x in items if x[0].uid in pool and x[2] == "return")
    r = next(x for x in items if x[0].uid != citem[0].uid and x[2] == "return")
    other = next(u for u in _spec_c_pool(s) if u.uid not in (citem[0].uid, r[0].uid))
    res = _link(s, _spec_plan([(*r, citem[0], ()), (*citem, other, ())]), off)
    v = next(v for v in res.variants if v.src_uid == citem[0].uid)
    assert v.tc_size > citem[0].tc_size  # the clone carries C's stub
    lc = certify_link(s, res, layout=lay, contract=c, offsets=off)
    assert not lc.rejected, lc.failures()


def test_summary_certifier_0x1ede(ctx):
    from scopt.analysis.exit_facts import propose_exit_facts
    from scopt.analysis.hostops import build_hostops
    from scopt.validate_sym.link_check import summary_certifier

    s, lay, _, _, _ = ctx
    R = _at(s, 0x1EDE)
    es = propose_exit_facts(build_hostops(R, lay, s.header.code_address)).exits[0]
    f = es.fact("r0")
    assert f is not None and f.value == 0
    cert = summary_certifier(s, lay)
    ok, h = cert(R, es, f)
    assert ok is True and h.startswith("sha256:")

    class F:
        field, value = "r0", 1
    assert cert(R, es, F()) == (False, None)


def test_validate_b3_runs_hostsym_callable(ctx, tmp_path, monkeypatch):
    """validate_b3(hostsym=callable) certifies the link it validates: V12/V13/V14
    pass for the good 0x1ede plan and V14 fails for the falsified one."""
    from scopt.link import B3Plan, VariantSpec
    from scopt.validate import validate_b3, write_b3_files
    from scopt.validate_sym.link_check import hostsym_inputs
    from scopt.validate_sym.link_mutants import synthetic_fact

    s, lay, c, off, _ = ctx
    R, C = _at(s, 0x1EDE), _at(s, 0x24F0)
    ri6 = H.v6_identity(monkeypatch)
    hs = hostsym_inputs(layout=lay, contract=c, offsets=off)
    for name, val, want in (("good", 0, True), ("falsified", 1, False)):
        res = _link(s, B3Plan(grow={R.uid: (_spec_1ede(s, R, C),)}, variants=(
            VariantSpec(C.uid, R.uid, 0, patches=_t1(C) if val == 0 else (),
                        facts=(synthetic_fact("r0", val, name),)),)), off)
        d = tmp_path / name
        write_b3_files(d, s, res)
        v, _ = validate_b3(s, res, d, runtime=ri6, semantic_exes=[H.QINT_V6_SHA], hostsym=hs,
                           skip_reimport=True)
        rows = {(r["code"], r["check"].split(":")[0]): r["result"] for r in v.rows
                if r["code"] in ("V12", "V13", "V14")}
        assert rows[("V12", "equiv")] == "pass"
        assert rows[("V13", "guard_entailment")] == "pass"
        assert rows[("V14", "certified")] == "pass"
        assert (rows[("V14", "proposer_equal")] == "pass") is want


def test_equivalent_mutants_are_listed_not_counted():
    """Mutants of a unit whose unmutated control has an unreachable slot (vacuous guard)
    are equivalent mutants: listed in the summary, excluded from the 100 % denominator."""
    from scopt.validate_sym.link_mutants import LinkMutant, LinkSuite

    su = LinkSuite()
    su.results = [LinkMutant("drop_g_bp", "u1", True, True, ["x"], ""),
                  LinkMutant("drop_g_bp", "u2", False, False, [], "", equivalent="vacuous")]
    su.controls = [{"name": "c", "unit": "u2", "ok": True, "failures": [],
                    "slots_unreached": [0]}]
    sm = su.summary()
    assert sm["all_rejected"] and sm["mutants"] == 1
    assert sm["equivalent_mutants"] == 1 and sm["equivalent_rejected"] == 0
    assert sm["equivalent_units"] == ["u2"] and sm["vacuous_controls"]
    su.results.append(LinkMutant("drop_g_pc[0]", "u3", False, False, [], ""))
    assert not su.summary()["all_rejected"]

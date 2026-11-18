"""The helper effect table (``scopt.analysis.helper_effects``) and its consumers.

``TCG_CALL_NO_RWG`` only promises that a helper leaves the TCG globals alone;
``rebuild_hflags_m32[_newel]`` is ``NO_RWG`` and writes ``env->hflags``
(design/helper-inlining-design.md F13; notes/no-rwg-soundness.md). Each helper's env
read/write sets, host-memory effect and fault behaviour now come from a per-executable
table, fail closed for helpers without a row. These tests check:

* the packaged tables (bound to the gate and WP-QINT binaries) cover every helper the
  published snapshots call, with the declared TCG flags;
* the F13 counterexamples (M13a-c, a V14 hflags fact across the rebuild) are rejected by
  hostsym, and would be accepted by the old "NO_RWG = env-pure" model;
* an unknown NO_RWG helper (no row, flags disagreeing with the row, or no table bound) is
  fail closed in hostsym, syncdead, hconst (CP/BF), exit_facts and the effects map;
* on the real compute snapshot, the B4 proposal still reproduces the published artifact
  byte for byte, and V12 still passes on its units that call a rebuild helper.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scopt.analysis import helper_effects as HE
from scopt.analysis.helper_effects import (FAIL_CLOSED, BoundEffects, EnvSet, HelperEffect,
                                           HelperEffectsError, HelperEffectTable, call_class,
                                           effects_for_layout, find_helper_effects,
                                           read_helper_effects)
from scopt.analysis.hostops import OpKind
from scopt.formats.envlayout_v1 import find_envlayout
from scopt.validate_sym.equiv import certify_fact, check_equiv
from scopt.validate_sym.mutants import (MINI_LAYOUT, OFF_FLAGS, OFF_FLAGS2, OFF_R,
                                        SynthContract, mini_effects, nrwg_cases, unit_nrwg)
from scopt.validate_sym.program import HelperRef

ROOT = Path(os.environ.get("FASTDYN_ROOT", "/data/extra_copy/fastdyn"))
GATE = "sha256:56d8bfb32c91c970fe0a16237b962c02791ce0a7fcc52bf6246343fbef430d90"
QINT = "sha256:8864c72671971cf4488b5afa76eeee02a3e2a472c45af81ec1314464e6035f41"

#: every helper (name, TCG flags) called from TB code in the 90 published snapshots of
#: runs/{wp28,wp29,closeout,b5} (MEASURED census, notes/no-rwg-soundness.md §2.2), plus the
#: unnamed plugin callbacks (flags 0 and 1)
CENSUS = [("lookup_tb_ptr", 6), ("v7m_msr", 0), ("v7m_mrs", 0), ("shl_cc", 0),
          ("exception_internal", 8), ("exception_with_syndrome", 8),
          ("rebuild_hflags_m32_newel", 1), ("rebuild_hflags_m32", 1), ("udiv", 1),
          ("sdiv", 1), (None, 0), (None, 1)]

C_B3 = SynthContract(True)


def c_b4():
    from scopt.passes.eliminate_state_sync import strict_contract

    return strict_contract(C_B3.c)


def eq(o, n, pre=None, c=None, **kw):
    return check_equiv(o, n, pre, None, c or C_B3, layout=MINI_LAYOUT, **kw)


def old_model() -> BoundEffects:
    """The pre-table model, as a table: NO_RWG rebuild helpers write nothing."""
    gate = find_helper_effects(GATE)
    rows = [replace(r, env_writes=EnvSet("none")) if r.helper.startswith("rebuild_hflags")
            else r for r in gate.rows]
    return BoundEffects(HelperEffectTable.build(MINI_LAYOUT.exe_sha256, rows), MINI_LAYOUT)


# ---------------------------------------------------------------------------
# the packaged tables
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exe", [GATE, QINT])
def test_packaged_table_covers_every_helper_seen(exe):
    t = find_helper_effects(exe)
    assert t is not None and t.exe_sha256 == exe
    p = [x for x in HE.packaged_helper_effects() if x.name == exe.split(":")[1] + ".json"]
    assert len(p) == 1 and read_helper_effects(p[0]).table_id == t.table_id
    for name, flags in CENSUS:
        assert t.row(name, flags) is not None, (name, flags)
    lay = find_envlayout(exe)
    assert lay is not None
    fx = effects_for_layout(lay)
    assert fx.table is t
    for name, flags in CENSUS:
        r = fx.resolved(name, flags)
        assert r.rowed and r.effect is not FAIL_CLOSED, name


@pytest.mark.parametrize("exe", [GATE, QINT])
def test_rows_resolve_to_the_source_effects(exe):
    lay = find_envlayout(exe)
    fx = effects_for_layout(lay)
    lo, hi = lay.offset("hflags.flags"), lay.offset("hflags.flags2") + 8
    assert (lo, hi) == (0x150, 0x160)
    for name in ("rebuild_hflags_m32_newel", "rebuild_hflags_m32"):
        r = fx.resolved(name, 1)
        assert sorted(r.writes.bytes) == list(range(lo, hi)) and not r.writes.neg
        assert not r.effect.hostmem_writes and not r.writes_globals
        assert fx.call_class(name, 1) == "pure"
        # reads no D-class byte (regs[0..14], NZCV, can_do_io, plugin_mem_value)
        for f in ("regs[0]", "regs[14]", "CF", "ZF", "neg.can_do_io",
                  "neg.plugin_mem_value_low"):
            assert not r.reads(lay.offset(f)), f
        assert r.reads(lay.offset("v7m.control[0]"))
    for name in ("udiv", "sdiv"):
        r = fx.resolved(name, 1)
        assert r.writes.empty and not r.effect.hostmem_writes and r.effect.may_fault
        assert fx.call_class(name, 1) == "raising"
    cdio = lay.offset("neg.can_do_io")
    for name, flags in (("v7m_msr", 0), ("v7m_mrs", 0), ("shl_cc", 0), (None, 0), (None, 1)):
        r = fx.resolved(name, flags)
        assert not r.keep(lay.offset("regs[3]")) and r.keep(cdio)  # all but can_do_io
        assert r.effect.hostmem_writes and r.writes_globals
    lk = fx.resolved("lookup_tb_ptr", 6)
    assert lk.keep(lay.offset("regs[15]")) and lk.keep(lay.offset("CF"))
    assert not lk.keep(cdio) and not lk.writes_globals
    assert not lk.reads(cdio) and lk.reads(lay.offset("regs[15]"))
    for name in ("exception_internal", "exception_with_syndrome"):
        assert fx.resolved(name, 8).keep is None and fx.call_class(name, 8) == "noreturn"


def test_call_class_needs_a_row_for_pure_and_raising():
    fx = mini_effects()
    none = BoundEffects(None, MINI_LAYOUT)
    for f in (fx, none, None):
        assert call_class("lookup_tb_ptr", 6, f) == "lookup"
        assert call_class(None, 1, f) == "plugin"
        assert call_class("exception_internal", 8, f) == "noreturn"
        assert call_class("<ldst_slow_path>", None, f) == "ldst_slow"
        assert call_class("v7m_msr", 0, f) == "global"
        assert call_class("helper_without_row", 1, f) == "global"
    assert call_class("rebuild_hflags_m32_newel", 1, fx) == "pure"
    assert call_class("udiv", 1, fx) == "raising"
    # flags that disagree with the row (rebuild_hflags is NO_RWG): fail closed
    assert call_class("rebuild_hflags_m32_newel", 5, fx) == "global"
    assert fx.resolved("rebuild_hflags_m32_newel", 5).effect is FAIL_CLOSED
    # no table bound: every NO_RWG helper is global
    assert call_class("rebuild_hflags_m32_newel", 1, none) == "global"
    assert call_class("udiv", 1, None) == "global"


def test_unknown_helper_resolves_fail_closed():
    fx = mini_effects()
    r = fx.resolved("helper_without_row", 1)
    assert not r.rowed and r.effect is FAIL_CLOSED
    assert r.keep is None and r.reads_all and r.writes_globals
    assert r.effect.hostmem_writes and r.effect.may_fault
    assert not fx.keeps_globals("helper_without_row", 3)
    assert not BoundEffects(None, MINI_LAYOUT).keeps_globals("lookup_tb_ptr", 6)
    # a layout bound to no table: everything fail closed
    assert effects_for_layout(None).table is None


def test_rows_are_checked():
    ok = dict(helper="h", tcg_flags=1, env_reads="all", env_writes="none",
              hostmem_reads=False, hostmem_writes=False, may_fault=False, returns=True,
              src=["x.c:1"])
    HelperEffectTable.build(GATE, [HelperEffect.from_json(ok)])
    bad = [dict(ok, env_writes="all"),  # NO_RWG cannot write every env byte
           dict(ok, env_writes={"all_except": ["@can_do_io"]}),  # ... nor the globals
           dict(ok, returns=False),  # disagrees with TCG_CALL_NO_RETURN
           dict(ok, src=[]),
           dict(ok, tcg_flags=None)]
    for b in bad:
        with pytest.raises(HelperEffectsError):
            HelperEffectTable.build(GATE, [HelperEffect.from_json(b)])
    with pytest.raises(HelperEffectsError):
        HelperEffect.from_json({k: v for k, v in ok.items() if k != "src"})
    doc = HelperEffectTable.build(GATE, [HelperEffect.from_json(ok)]).to_json()
    with pytest.raises(HelperEffectsError):
        read_helper_effects(dict(doc, rows=[dict(ok, may_fault=True)]))  # id mismatch


def test_a_span_the_layout_disagrees_with_fails_closed():
    row = replace(find_helper_effects(GATE).row("rebuild_hflags_m32", 1),
                  env_writes=EnvSet("span", ("hflags.flags", "hflags.flags2"), 24))
    fx = BoundEffects(HelperEffectTable.build(MINI_LAYOUT.exe_sha256, [row]), MINI_LAYOUT)
    assert fx.resolved("rebuild_hflags_m32", 1).effect is FAIL_CLOSED


def test_a_nrwg_row_whose_fields_cover_a_global_fails_closed():
    # _check_row cannot see this without a layout: a NO_RWG row that lists a TCG
    # global (regs[0]) among its written fields contradicts its own flags.
    row = replace(find_helper_effects(GATE).row("rebuild_hflags_m32", 1),
                  env_writes=EnvSet("fields", ("hflags.flags", "regs[0]")))
    fx = BoundEffects(HelperEffectTable.build(MINI_LAYOUT.exe_sha256, [row]), MINI_LAYOUT)
    r = fx.resolved("rebuild_hflags_m32", 1)
    assert r.effect is FAIL_CLOSED and not r.rowed
    assert call_class("rebuild_hflags_m32", 1, fx) == "global"
    assert not fx.keeps_globals("rebuild_hflags_m32", 1)


# ---------------------------------------------------------------------------
# hostsym: the F13 counterexamples
# ---------------------------------------------------------------------------

def _wrong_cases(helper=None):
    return [(n, o, x, pre) for n, o, x, pre, good in nrwg_cases(helper) if not good]


@pytest.mark.parametrize("contract", ["b3", "b4"])
@pytest.mark.parametrize("name", ["writeback", "read_hoisted", "const_after_call"])
def test_m13_rewrites_around_a_rebuild_are_rejected(name, contract):
    c = C_B3 if contract == "b3" else c_b4()
    [(_, o, n, pre)] = [x for x in _wrong_cases() if x[0] == name]
    v = eq(o, n, pre, c)
    assert not v.ok and v.reason == "validation_mismatch", v.detail
    # the old model (rebuild writes nothing) accepted every one of them (F13)
    assert eq(o, n, pre, c, effects=old_model()).ok


@pytest.mark.parametrize("name", ["writeback", "read_hoisted", "const_after_call"])
def test_m13_rewrites_are_rejected_with_a_flags0_helper_too(name):
    h = HelperRef(3, "v7m_msr", 0)
    [(_, o, n, pre)] = [x for x in _wrong_cases(h) if x[0] == name]
    assert not eq(o, n, pre).ok


def test_identity_around_a_rebuild_stays_equivalent():
    [(_, o, n, pre, _)] = [x for x in nrwg_cases() if x[4]]
    assert eq(o, n, pre).ok and eq(o, n, pre, c_b4()).ok


def test_v14_does_not_certify_hflags_across_a_rebuild():
    r = unit_nrwg("nrwg_r", lambda a: (a.mov_rdi_rbp(), a.call_pool("p_rb")), tail="lookup")
    pre = {(OFF_FLAGS, 4): 0x110}
    cert = certify_fact(r, None, (OFF_FLAGS, 4), layout=MINI_LAYOUT, pre=pre)
    assert not cert.certified and cert.status == "not_constant"
    old = certify_fact(r, None, (OFF_FLAGS, 4), layout=MINI_LAYOUT, pre=pre,
                       effects=old_model())
    assert old.certified and old.value == 0x110  # the F13 defect
    # a fact field the rebuild does not write is still certified across it
    r0 = unit_nrwg("nrwg_r0", lambda a: (a.mov_env_imm(OFF_R[0], 5), a.mov_rdi_rbp(),
                                         a.call_pool("p_rb")), tail="lookup")
    c0 = certify_fact(r0, None, (OFF_R[0], 4), layout=MINI_LAYOUT)
    assert c0.certified and c0.value == 5


def _main_exit(ex):
    """The ``exit_null`` path (the other one is the prefix's exit-request stub)."""
    return [s for s in ex.exits() if s.end[1] == "exit_null"]


def test_rebuild_havocs_flags2_and_the_padding_but_nothing_else():
    from scopt.validate_sym.paths import explore

    p = unit_nrwg("nrwg_x", lambda a: (a.mov_env_imm(OFF_FLAGS2, 7), a.mov_env_imm(0x154, 1),
                                        a.mov_env_imm(0x160, 3), a.mov_rdi_rbp(),
                                        a.call_pool("p_rb")))
    [st] = _main_exit(explore(p, layout=MINI_LAYOUT))
    env = st.events[-1].env
    assert all(isinstance(env.byte(b), tuple) and env.byte(b)[0] == "hv"
               for b in range(0x150, 0x160))
    assert env.byte(0x160) == 3  # the byte after CPUARMTBFlags is kept
    assert st.hver == 0  # no host-memory write


@pytest.mark.parametrize("atom", ["g_flags", "g_flags2"])
def test_v13_guard_atom_on_hflags_not_entailed_across_a_rebuild(atom):
    from scopt.validate_sym.equiv import check_guarded_chain
    from scopt.validate_sym.mutants import GuardSpec, unit_ret

    g = GuardSpec()
    pre = {(OFF_FLAGS, 4): g.kf, (OFF_FLAGS2, 8): g.kf2}
    r, _ = unit_ret(rebuild=True, uid="synth:retrb")
    full, _ = unit_ret(rebuild=True, guard=g, uid="synth:retrb'")
    drop, _ = unit_ret(rebuild=True, guard=GuardSpec(drop=frozenset({atom})), uid="synth:retrb'")
    assert check_guarded_chain(r, full, C_B3, layout=MINI_LAYOUT, pre=pre).ok
    v = check_guarded_chain(r, drop, C_B3, layout=MINI_LAYOUT, pre=pre)
    assert not v.ok and v.reason == "guard_contract_violation" and atom in v.detail
    # the old model took the precondition's hflags across the rebuild: the atom was
    # "entailed" without a compare in the stub
    assert check_guarded_chain(r, drop, C_B3, layout=MINI_LAYOUT, pre=pre,
                               effects=old_model()).ok


def test_m13d_deleting_the_rebuild_after_msr_is_rejected():
    def o(a):
        a.mov_r_imm("rsi", 0x814)
        a.mov_rdi_rbp()
        a.call_pool("p_msr")
        a.mov_rdi_rbp()
        a.call_pool("p_rb")

    def n(a):
        a.mov_r_imm("rsi", 0x814)
        a.mov_rdi_rbp()
        a.call_pool("p_msr")
        a.nop(6 + 3)

    assert not eq(unit_nrwg("m13d", o), unit_nrwg("m13d'", n)).ok


# ---------------------------------------------------------------------------
# fail closed: a NO_RWG helper without a row, or no table bound
# ---------------------------------------------------------------------------

UNKNOWN = HelperRef(3, "nrwg_helper_without_row", 1)


def _d1(helper=None):
    def dead(a):
        a.mov_env_imm(OFF_R[1], 7)
        a.mov_rdi_rbp()
        a.call_pool("p_rb")
        a.mov_env_imm(OFF_R[1], 9)

    def deleted(a):
        a.mov_rdi_rbp()
        a.call_pool("p_rb")
        a.mov_env_imm(OFF_R[1], 9)
    return unit_nrwg("d1", dead, helper=helper), unit_nrwg("d1'", deleted, helper=helper)


def test_unknown_nrwg_helper_is_fail_closed_in_hostsym():
    for name, o, n, pre in _wrong_cases(UNKNOWN):
        v = eq(o, n, pre)
        assert not v.ok, name
    # D1 is observed at an unknown helper (S4), not at a rowed pure one (P)
    o, n = _d1()
    assert eq(o, n).ok
    o, n = _d1(UNKNOWN)
    v = eq(o, n)
    assert not v.ok and "call:global" in v.detail
    # its host-memory effect: hver is bumped
    from scopt.validate_sym.paths import explore

    [st] = _main_exit(explore(o, layout=MINI_LAYOUT))
    assert st.hver == 1 and st.events[0].cls == "global"


def test_no_table_bound_is_fail_closed_in_hostsym():
    none = BoundEffects(None, MINI_LAYOUT)
    for name, o, n, pre in _wrong_cases():
        assert not eq(o, n, pre, effects=none).ok, name
    o, n = _d1()
    assert not eq(o, n, effects=none).ok


def test_an_unrowed_helper_reads_are_compared_at_the_call():
    """A rowed helper whose row reads a D1 byte makes that byte observable at its (pure)
    call event; the contract alone would not compare it at P."""
    gate = find_helper_effects(GATE)
    rows = [replace(r, env_reads=EnvSet("all")) if r.helper == "rebuild_hflags_m32_newel"
            else r for r in gate.rows]
    fx = BoundEffects(HelperEffectTable.build(MINI_LAYOUT.exe_sha256, rows), MINI_LAYOUT)
    o, n = _d1()
    assert eq(o, n).ok
    v = eq(o, n, effects=fx)
    assert not v.ok and "call:pure" in v.detail


# ---------------------------------------------------------------------------
# the other consumers
# ---------------------------------------------------------------------------

def _gate_fx():
    return effects_for_layout(find_envlayout(GATE))


def _call_op(name, flags, off=0x40):
    from scopt.analysis.hostops import HostOp

    return HostOp(off=off, size=6, text="call", kind=OpKind.CALL, block="insn", insn=0,
                  helper_id=3, helper_name=name, helper_flags=flags)


def test_syncdead_observers_follow_the_table():
    from scopt.analysis.obs_contract import build_contract
    from scopt.analysis.syncdead import TrackedBytes, _observers

    lay = find_envlayout(GATE)
    tb = TrackedBytes.of(lay)
    con = build_contract(lay, None)
    D1, D2, D3 = (tb.cls_mask[k] for k in ("D1", "D2", "D3"))
    calls = [("rebuild_hflags_m32_newel", 1), ("udiv", 1), ("nrwg_helper_without_row", 1),
             ("lookup_tb_ptr", 6), ("v7m_msr", 0), (None, 1)]
    h = SimpleNamespace(ops=[_call_op(n, f, 8 * i) for i, (n, f) in enumerate(calls)])
    g = SimpleNamespace(exits={})
    fx = _gate_fx()
    obs = _observers(h, g, tb, con, mode="b4", call_reads_all=True, effects=fx)
    assert obs[0] == D2  # pure: can_do_io only (call_reads_all)
    assert obs[1] == D1 | D2 | D3  # raising: S6
    assert obs[2] == D1 | D2 | D3  # no row: fail closed (S4, reads everything)
    assert obs[3] == D1 | D2  # lookup: S3
    assert obs[4] == obs[5] == D1 | D2 | D3
    none = _observers(h, g, tb, con, mode="b4", call_reads_all=True,
                      effects=BoundEffects(None, lay))
    assert none[0] == none[1] == none[2] == D1 | D2 | D3
    assert none[3] == D1 | D2 | D3  # even the lookup reads everything without a table
    # mode="tcg" is TCG's own rule (flags only), whatever the table says
    tcg = _observers(h, g, tb, con, mode="tcg", call_reads_all=True, effects=fx)
    assert not tcg[0] & D1 and not tcg[1] & D1 and not tcg[2] & D1
    assert tcg[3] & D1 and tcg[4] & D1 and tcg[5] & D1


def test_syncdead_observes_a_rowed_helpers_d_reads():
    from scopt.analysis.obs_contract import build_contract
    from scopt.analysis.syncdead import TrackedBytes, _observers

    lay = find_envlayout(GATE)
    gate = find_helper_effects(GATE)
    rows = [replace(r, env_reads=EnvSet("fields", ("regs[3]",)))
            if r.helper == "rebuild_hflags_m32" else r for r in gate.rows]
    fx = BoundEffects(HelperEffectTable.build(GATE, rows), lay)
    tb = TrackedBytes.of(lay)
    h = SimpleNamespace(ops=[_call_op("rebuild_hflags_m32", 1)])
    [m] = _observers(h, SimpleNamespace(exits={}), tb, build_contract(lay, None), mode="b4",
                     call_reads_all=False, effects=fx)
    assert m == tb.mask(lay.offset("regs[3]"), 4)


def test_hconst_kills_facts_unless_the_row_rules_out_a_global_write():
    from scopt.analysis.hconst import _State, _step

    fx = _gate_fx()

    def facts_after(name, flags, f=fx):
        h = SimpleNamespace(ops=[_call_op(name, flags)])
        st = _State.of({"r0": 1, "r5": 2}, {}, {})
        out, _ = _step(h, 0, b"", st, frozenset(), f)
        return dict(out.d()[0])

    assert facts_after("lookup_tb_ptr", 6) == {"r0": 1, "r5": 2}  # NO_WG with a row
    assert facts_after("nrwg_helper_without_row", 2) == {}  # NO_WG, no row: fail closed
    assert facts_after("lookup_tb_ptr", 6, BoundEffects(None, None)) == {}
    assert facts_after("rebuild_hflags_m32", 1) == {}  # no NO_WG bit (stricter than TCG)
    assert facts_after("v7m_msr", 0) == {}


def test_exit_facts_kill_unless_the_row_rules_out_a_global_write():
    from scopt.analysis.exit_facts import _transfer

    fx = _gate_fx()

    def killed(name, flags, f=fx):
        env = {"regs[0]": ("c", 1, ())}
        bl: list = []
        _transfer(_call_op(name, flags), {}, env, bl, f)
        return env["regs[0]"][0] == "k"

    assert not killed("rebuild_hflags_m32_newel", 1)  # row: writes hflags only
    assert not killed("udiv", 1)
    assert not killed("lookup_tb_ptr", 6)
    assert killed("nrwg_helper_without_row", 1)  # no row: fail closed
    assert killed("rebuild_hflags_m32_newel", 1, BoundEffects(None, None))
    assert killed("v7m_msr", 0)
    assert killed(None, 1)  # plugin callback


def test_effects_map_classes_follow_the_table():
    from scopt.analysis.effects import build_effects

    calls = [("rebuild_hflags_m32_newel", 1), ("udiv", 1), ("nrwg_helper_without_row", 1)]
    h = SimpleNamespace(uid="u", ops=[_call_op(n, f, 8 * i) for i, (n, f) in enumerate(calls)],
                        envlayout_id=find_envlayout(GATE).envlayout_id)
    g = SimpleNamespace(exits={}, reachable_atomic=set())
    e = build_effects(h, g)
    assert [s[1] for s in e.sync if s[1] != "S1"] == ["S6", "S4"]
    assert [d.split(":")[0] for _, k, d in e.events if k == "call"] == \
        ["pure", "raising", "global"]


def test_hlive_call_uses_and_defs_do_not_depend_on_the_helper():
    from scopt.analysis.hlive import _uses_defs

    a = _uses_defs(SimpleNamespace(ops=[_call_op("rebuild_hflags_m32_newel", 1)]), 0, None)
    b = _uses_defs(SimpleNamespace(ops=[_call_op("nrwg_helper_without_row", 1)]), 0, None)
    assert a == b and {"rdi", "rsi", "rdx", "rcx", "r8", "r9", "rbp"} <= a[0]


def test_the_synthetic_suite_has_the_nrwg_mutants():
    from scopt.validate_sym.mutants import DETECTORS, STATIC_ONLY, synthetic_suite

    d = synthetic_suite().to_json()
    names = {r["name"]: r for r in d["results"]}
    for k in ("nrwg_hflags_writeback", "nrwg_hflags_read_hoisted",
              "nrwg_hflags_const_after_call", "nrwg_hflags_fact_after_call",
              "nrwg_unknown_helper_writeback", "nrwg_drop_g_flags_after_call",
              "nrwg_drop_g_flags2_after_call"):
        assert names[k]["rejected"], k
        assert k in DETECTORS and k in STATIC_ONLY
    ctl = {c["name"]: c["ok"] for c in d["controls"]}
    assert ctl["nrwg_identity_equivalent"]
    assert ctl["nrwg_guarded_chain_after_rebuild_equivalent"]
    assert ctl["nrwg_d1_store_before_rebuild_deletable"]
    assert ctl["nrwg_d1_store_before_unknown_helper_observed"]
    assert d["all_rejected"] and d["controls_ok"]


# ---------------------------------------------------------------------------
# real: the published compute B4 artifact is reproduced (notes/no-rwg-impact)
# ---------------------------------------------------------------------------

COMPUTE_ART = ROOT / ("runs/wp29/compute/store/artifacts/"
                      "461675f1d38acc220ef3f6cb5d84e867aa7352205bf6aa355895a94c1eead6de")
COMPUTE_SNAP = ROOT / ("runs/wp29/compute/store/snapshots/"
                       "da3f40c358b4876f25cdfc07473c6c46bfe897fc0b4bb4e82d673a47e2183490")


@pytest.mark.real
def test_compute_b4_artifact_reproduced_and_rebuild_units_still_certified():
    if not (COMPUTE_ART / "cache.sc").is_file() or not (COMPUTE_SNAP / "snapshot.json").is_file():
        pytest.skip("published compute B4 artifact not present")
    from scopt.formats import artifact as A
    from scopt.formats.sc_v5 import read_sc
    from scopt.importer.import_cache import open_snapshot
    from scopt.analysis.obs_contract import build_contract
    from scopt.passes.eliminate_state_sync import propose_b4, strict_contract
    from scopt.validate_sym.program import program_of, world_of

    info = A.read_artifact(COMPUTE_ART)
    ri = info.runtime_identity
    assert ri["qemu_sha256"] == GATE
    s = open_snapshot(COMPUTE_SNAP)
    lay = find_envlayout(GATE)
    prop = propose_b4(s, layout=lay, runtime=ri, classes="D1|D2|D3")
    assert prop.by_class() == {"D1": 334, "D2": 1207, "D3": 3409}  # the published x_classes
    out = read_sc(COMPUTE_ART / "cache.sc", versions=(5, 6))
    recs = {uid: (out.records[i], bytes(out.bodies[i]))
            for uid, i in zip(out.unit_ids(), out.valid_slots)}
    units = {u.uid: u for u in s.units()}
    assert set(prop.patches) <= set(recs)
    rebuild_units = []
    for uid, (rec, body) in recs.items():
        u = units[uid]
        code = bytearray(u.code)
        for pt in prop.patches.get(uid, ()):
            code[pt.host_off:pt.host_off + len(pt.new_bytes)] = pt.new_bytes
        assert bytes(code) == body[:rec.tc_size], uid  # byte for byte
        if uid in prop.patches and any(
                (o.helper_name or "").startswith("rebuild_hflags")
                for o in _calls(u, lay, s.header.code_address)):
            rebuild_units.append(uid)
    # notes/no-rwg-impact: 18 rewritten units of this artifact call a rebuild helper;
    # V12 (B4 strict contract) under the effect table still certifies every one
    assert len(rebuild_units) == 18
    w = world_of(s, lay)
    con = strict_contract(build_contract(lay, ri))
    ca = s.header.code_address
    for uid in rebuild_units:
        rec, body = recs[uid]
        o = program_of(units[uid], w)
        n = replace(o, uid=uid + "'", code=body[:rec.tc_size], base=ca + rec.tc_offset,
                    struct_base=ca + rec.tb_offset, own_end=ca + rec.end_offset)
        v = check_equiv(o, n, None, None, con, layout=lay)
        assert v.ok, (uid, v.detail)


def _calls(u, lay, ca):
    from scopt.analysis.hostops import build_hostops

    return [o for o in build_hostops(u, lay, ca).ops if o.kind == OpKind.CALL]


def test_census_table_json_is_canonical_order():
    for p in HE.packaged_helper_effects():
        doc = json.loads(p.read_text())
        assert [r["helper"] for r in doc["rows"]] == sorted(r["helper"] for r in doc["rows"])

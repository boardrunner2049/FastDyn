"""WP19: the ``hostsym`` validator kernel (B3/B4 design §9.2, T-B3-4, T-B3-5).

Real-snapshot tests use the B2 compute store snapshot (read-only, skipped
when absent): the prototype tests T1-T6 of runs/b3-explore/design-C
(proto-results.txt) are reproduced on the same units, T6 now analysed with
the LDST abstraction.
"""

from __future__ import annotations

import dataclasses
import os
import struct
from pathlib import Path

import pytest

from scopt.validate import Validation, v12_rows, v13_entailment_rows, v14_rows, v18_rows
from scopt.validate_sym import (certify_fact, check_equiv, check_guarded_chain, explore, patched,
                                program_of, world_of)
from scopt.validate_sym.asm import Asm, nop
from scopt.validate_sym.mutants import (ENV_ABS, MINI_LAYOUT, OFF_R, RAM_PTR, GuardSpec,
                                        SynthContract, SynthUnit, grow_guarded, unit_cdio,
                                        unit_cont, unit_mem, unit_ret)
from scopt.validate_sym.state import ByteMem, binop, lo, zx
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
COMPUTE = ROOT / "runs/b2/compute/store/snapshots/" \
    "53b4132fc05526ff2925cd3acc610bf613e8b11e2315e15e520bff7fe080dab2"
COMPUTE_PROFILE = ROOT / "runs/b2/compute/store/profiles/" \
    "d09e70a617896b1504d425d79947e95fc26ffdae7180d8cb34a661b26a65be27/profile.json"
CF_ICOUNT = 0xFF022000
_C: dict[str, object] = {}


def compute():
    if not (COMPUTE / "snapshot.json").is_file():
        pytest.skip(f"{COMPUTE} not present")
    if "s" not in _C:
        from scopt.analysis.obs_contract import build_contract
        from scopt.formats.envlayout_v1 import find_envlayout
        from scopt.importer.import_cache import open_snapshot

        s = open_snapshot(COMPUTE)
        lay = find_envlayout(s.runtime["qemu_sha256"])
        _C.update(s=s, lay=lay, w=world_of(s, lay), c=build_contract(lay, s.runtime))
    return _C["s"], _C["lay"], _C["w"], _C["c"]


def prog_at(vaddr: int):
    s, lay, w, _ = compute()
    for u in s.units():
        if u.vaddrs and u.vaddrs[0] == vaddr and u.key.cflags == CF_ICOUNT:
            return program_of(u, w)
    raise KeyError(hex(vaddr))


# -- terms and memory ----------------------------------------------------------------------------

def test_term_folding():
    assert binop("add", 4, 0xFFFFFFFF, 1) == 0
    assert binop("sub", 4, ("x",), 0) == ("x",)
    assert binop("and", 4, ("x",), ("x",)) == ("x",)
    assert binop("xor", 4, ("x",), ("x",)) == 0
    assert binop("add", 4, ("a",), ("b",)) == binop("add", 4, ("b",), ("a",))
    assert lo(4, zx(4, ("t",))) == ("t",)
    assert binop("sar", 4, 0x80000000, 4) == 0xF8000000


def test_bytemem_partial_overlap_is_exact():
    a, b = ByteMem("env"), ByteMem("env")
    a.write(0, 4, ("x",))
    a.write(1, 1, 7)
    b.write(0, 4, ("x",))
    b.write(1, 1, 8)
    assert a.read(0, 4) != b.read(0, 4)  # no "envmix" collapse
    assert a.read(1, 1) == 7
    c = ByteMem("env")
    c.write(0, 4, ("x",))
    assert c.read(0, 4) == ("x",)
    assert c.read(0, 1) == lo(1, ("x",))


def test_havoc_keeps_differences_and_agreement():
    a, b = ByteMem("env"), ByteMem("env")
    a.write(0x18, 8, ("v",))  # a store the other side deleted
    for m in (a, b):
        m.havoc(("call", 0), lambda x: x < 0x40)
    assert a.byte(0x18) != b.byte(0x18)  # a deleted store never becomes invisible
    assert a.byte(0x20) == b.byte(0x20)
    a.write(0, 4, 5)
    b.write(0, 4, 5)
    a.havoc(("call", 1), None)
    b.havoc(("call", 1), None)
    assert a.read(0, 4) == b.read(0, 4)


def test_precondition_bytes():
    from scopt.validate_sym.paths import pre_bytes

    m = ByteMem("env", pre_bytes({(0, 4): 0x01020304}))
    assert m.read(0, 4) == 0x01020304
    assert m.read(1, 2) == 0x0203
    assert m.read(4, 4) == ("env0", 4, 4)


# -- the prototype tests T1-T6 on the compute snapshot (T-B3-4) ---------------------------------

T1_PATCH = [(0x15, bytes.fromhex("0f1f00")), (0x18, bytes.fromhex("6690")),
            (0x1A, bytes.fromhex("660f1f440000"))]


@pytest.mark.real
def test_t1_fold_under_fact_equivalent():
    _, lay, _, c = compute()
    p = prog_at(0x24F0)
    v = check_equiv(p, patched(p, T1_PATCH), {(0, 4): 0}, None, c, layout=lay)
    assert v.status == "equivalent", v.detail
    assert (v.o_paths, v.n_paths) == (2, 2)


@pytest.mark.real
def test_t2_fold_without_fact_rejected():
    _, lay, _, c = compute()
    p = prog_at(0x24F0)
    v = check_equiv(p, patched(p, T1_PATCH), None, None, c, layout=lay)
    assert v.status == "mismatch" and v.reason == "validation_mismatch"
    assert (v.o_paths, v.n_paths) == (3, 2)


@pytest.mark.real
def test_t3_wrong_fold_rejected():
    _, lay, _, c = compute()
    p = prog_at(0x24F0)
    v = check_equiv(p, patched(p, [(0x1A, bytes.fromhex("e91d00000090"))]), {(0, 4): 0}, None, c,
                    layout=lay)
    assert v.status == "mismatch", v.detail
    assert "goto_tb" in v.detail


@pytest.mark.real
def test_t4_fact_certified_on_return_unit():
    _, lay, _, _ = compute()
    cert = certify_fact(prog_at(0x1EDE), None, (0, 4), layout=lay)
    assert cert.certified and cert.value == 0, cert.detail
    assert cert.cert_hash.startswith("sha256:")
    assert certify_fact(prog_at(0x1EDE), None, (4, 4), layout=lay).status == "not_constant"


@pytest.mark.real
def test_t5_prefix_can_do_io_store_deletion_equivalent():
    _, lay, _, c = compute()
    p = prog_at(0x1EDE)
    v = check_equiv(p, patched(p, [(0x12, bytes.fromhex("0f1f4000"))]), None, None, c,
                    layout=lay)
    assert v.status == "equivalent", v.detail


@pytest.mark.real
def test_t6_effectful_return_analysed_with_ldst_abstraction():
    _, lay, _, c = compute()
    p = prog_at(0x1F7C)
    ex = explore(p, layout=lay)
    assert ex.complete and not ex.unsupported
    kinds = [(e.kind, e.cls) for e in ex.paths[0].events]
    assert kinds == [("guest", "S5")] * 3 + [("call", "lookup"), ("exit", "goto_ptr")] or \
        kinds == [("exit", "exit_requested")]
    full = [s for s in ex.paths if len(s.events) == 5][0]
    g = [e for e in full.events if e.kind == "guest"]
    assert [e.data[2] for e in g] == ["ld", "ld", "st"]
    assert check_equiv(p, p, None, None, c, layout=lay).ok


@pytest.mark.real
@pytest.mark.slow
def test_coverage_compute_steady_weight():
    """Reported outcome (target >= 99 % of compute steady weight analysable)."""
    from scopt.validate_sym.coverage import coverage, phase_weights

    s, lay, _, c = compute()
    if not COMPUTE_PROFILE.is_file():
        pytest.skip(f"{COMPUTE_PROFILE} not present")
    doc = coverage(s, lay, weights=phase_weights(str(COMPUTE_PROFILE)), contract=c)
    assert doc["weight_total"] == 826110048
    assert doc["weight_coverage"] >= 0.99, doc["reasons"]
    assert doc["max_paths"] <= 256


# -- guarded chain, certification ----------------------------------------------------------------

def test_guarded_chain_reference_equivalent_and_fact_at_slot():
    r, rp = grow_guarded(GuardSpec(targets=((0, "SPEC", 0x24F0), (1, "GENERAL", 0x2500))))
    v = check_guarded_chain(r, rp, SynthContract(True), layout=MINI_LAYOUT)
    assert v.ok, v.detail
    assert v.data["slots_reached"] == [0, 1]
    cert = certify_fact(rp, 0, (OFF_R[0], 4), layout=MINI_LAYOUT)
    assert cert.certified and cert.value == 0


def test_guarded_chain_requires_declared_slots():
    r, _ = unit_ret()
    v = check_guarded_chain(r, r, SynthContract(True), layout=MINI_LAYOUT)
    assert v.status == "mismatch" and v.reason == "guard_contract_violation"


def test_guarded_chain_can_do_io_rule():
    """The lookup's can_do_io = 1 is applied to the original side only: R' without
    its last-insn can_do_io = 1 store fails under tb_start_cb_possible, passes without."""
    r, _ = unit_ret()
    _, rpu = unit_ret(guard=GuardSpec(), uid="synth:ret'")
    rp, _ = unit_ret(guard=GuardSpec(), uid="synth:ret'")
    # the can_do_io = 1 store is the one after the r0 store
    off = rpu.at["r0"][0] + rpu.at["r0"][1]
    a = Asm()
    a.mov_env_imm(-4, 1, w=1)
    k = len(a.buf)
    assert rp.code[off:off + k] == bytes(a.buf)
    rp2 = patched(rp, [(off, nop(k))])
    r2 = patched(r, [(off, nop(k))], uid=r.uid)
    bad = check_guarded_chain(r, rp2, SynthContract(True), layout=MINI_LAYOUT)
    assert bad.status == "mismatch" and "can_do_io" in bad.detail
    # without a TB-start callback, can_do_io is unobservable at the slot (but the
    # fail path still runs the original tail, so compare against R with the same edit)
    ok = check_guarded_chain(r2, rp2, SynthContract(False), layout=MINI_LAYOUT)
    assert ok.ok, ok.detail


# -- verdict classes -----------------------------------------------------------------------------

def test_relocated_clone_is_equivalent():
    """C' ~ C: the same unit at another arena offset (own struct, raddr and pool
    references compare modulo relocation; external targets by absolute address)."""
    from scopt.validate_sym.mutants import UNIT_TB

    m, _ = unit_mem()
    m2, _ = unit_mem(tb=UNIT_TB + 0x12340)
    assert m.base != m2.base and m.code != m2.code
    v = check_equiv(m, m2, None, None, SynthContract(True), layout=MINI_LAYOUT)
    assert v.ok, v.detail
    m3, _ = unit_mem(tb=UNIT_TB + 0x12340, update_ptr=RAM_PTR + 8)
    assert not check_equiv(m, m3, None, None, SynthContract(True), layout=MINI_LAYOUT).ok


def test_unsupported_instruction_is_inconclusive():
    c, cu = unit_cont()
    off = cu.at["ld_r0"][0]
    v = check_equiv(c, patched(c, [(off, b"\xc3" + nop(cu.at["ld_r0"][1] - 1))]))
    assert v.status == "inconclusive" and v.reason == "validation_inconclusive"


def test_path_budget_is_inconclusive():
    u = SynthUnit("synth:budget", icount=1)
    a = u.a
    u.prefix()
    for i in range(9):
        a.cmp_env_imm(OFF_R[i], i)
        a.jcc("e", f"n{i}")
        a.label(f"n{i}")
    u.goto_tb(0, 0x10)
    p = u.build()
    v = check_equiv(p, p)
    assert v.status == "inconclusive" and "budget" in v.detail


def test_host_abs_unbound_pointer_without_env_bounds():
    m, mu = unit_mem()
    um = mu.at["update_mem"]
    n = patched(m, [(um[0] + 2, struct.pack("<Q", 0x7FFFC0000000))])
    v = check_equiv(m, n, None, None, SynthContract(True), layout=MINI_LAYOUT)
    assert v.reason == "host_abs_aliases_env", v.detail
    ok = check_equiv(m, m, None, None, SynthContract(True), layout=MINI_LAYOUT,
                     env_abs=(ENV_ABS - 11200, ENV_ABS + 0x3C00))
    assert ok.ok  # RAM_PTR (abs.guest_ram) and RING (abs.plugin_data) are allowed
    assert RAM_PTR in m.abs_classes


def test_contract_none_is_strict():
    """No contract: every env byte is observable everywhere (fail closed)."""
    cd, cdu = unit_cdio()
    s1 = cdu.at["cdio1"]
    n = patched(cd, [(s1[0], nop(s1[1]))])
    assert check_equiv(cd, n, None, None, SynthContract(False), layout=MINI_LAYOUT).ok
    assert check_equiv(cd, n).status == "mismatch"
    # the prefix false store is overwritten before any event: dead even when strict
    s0 = cdu.at["cdio0"]
    assert check_equiv(cd, patched(cd, [(s0[0], nop(s0[1]))])).ok


# -- V18 self-test --------------------------------------------------------------------------------

def test_native_selftest_passes():
    from scopt.validate_sym.selftest import native_check

    r = native_check(trials=16)
    if r["status"] == "not_run":
        pytest.skip(r["reason"])
    assert r["status"] == "pass", r["failures"][:3]
    assert r["forms"] >= 300
    assert {"add", "sub", "cmp", "test", "shl", "sar", "setcc", "cmovcc", "movsx",
            "lea"} <= set(r["mnemonics"])


def test_native_selftest_detects_a_wrong_model(monkeypatch):
    from scopt.validate_sym import selftest, semantics

    orig = semantics._arith_flags

    def wrong(st, name, w, a, b, r):
        orig(st, name, w, a, b, r)
        if isinstance(st.flags["CF"], int) and name == "add":
            st.flags["CF"] ^= 1

    monkeypatch.setattr(semantics, "_arith_flags", wrong)
    r = selftest.native_check(trials=4, only=[bytes.fromhex("01c8")])
    if r["status"] == "not_run":
        pytest.skip(r["reason"])
    assert r["status"] == "fail"


def test_v18_rows(tmp_path):
    from scopt.validate_sym.selftest import kernel_id, load_or_run

    path = tmp_path / "selftest.json"
    doc = load_or_run(str(path), trials=4)
    assert doc["kernel_id"] == kernel_id()
    if doc["native"].get("status") == "not_run":
        pytest.skip(doc["native"]["reason"])
    v = Validation()
    assert v18_rows(v, doc), v.rows
    assert doc["mutants"]["all_rejected"]
    again = load_or_run(str(path), trials=4)
    assert again == doc  # cached for the same kernel
    v = Validation()
    assert not v18_rows(v, dict(doc, kernel_id="sha256:00"))
    assert v.fails()[0]["reason"] == "hostsym_selftest_stale"
    v = Validation()
    assert not v18_rows(v, None)
    assert v.fails()[0]["reason"] == "hostsym_selftest_missing"


def test_v12_v13_v14_rows():
    c, cu = unit_cont()
    good = check_equiv(c, c)
    bad = check_equiv(c, patched(c, [(cu.at["jcc"][0], nop(6))]))
    v = Validation()
    assert not v12_rows(v, [("u1", good), ("u2", bad)])
    assert [r["result"] for r in v.rows] == ["pass", "fail"]
    assert v.rows[1]["reason"] == "validation_mismatch"
    v = Validation()
    v12_rows(v, [])
    assert v.rows[0]["result"] == "skip"
    r, rp = grow_guarded(GuardSpec(drop=frozenset({"g_bp"})))
    v = Validation()
    assert not v13_entailment_rows(v, [("R'", check_guarded_chain(r, rp, SynthContract(True),
                                                                  layout=MINI_LAYOUT))])
    assert v.rows[0]["reason"] == "guard_contract_violation"
    r, rp = grow_guarded()
    cert = certify_fact(rp, 0, (0, 4), layout=MINI_LAYOUT)
    v = Validation()
    assert v14_rows(v, [{"label": "R'/0", "cert": cert, "proposed": 0, "provenance": "patch"}])
    v = Validation()
    assert not v14_rows(v, [{"label": "R'/0", "cert": cert, "proposed": 1, "provenance": None}])
    assert {r["reason"] for r in v.fails()} == {"fact_derivation_mismatch",
                                                "provenance_unexplained"}


def test_v14_suffix_unit_not_certified():
    _, sp = grow_guarded(store_r0=None)
    cert = certify_fact(sp, 0, (0, 4), layout=MINI_LAYOUT)
    v = Validation()
    assert not v14_rows(v, [{"label": "Rs", "cert": cert, "proposed": 0, "provenance": "x"}])
    assert v.fails()[0]["reason"] == "fact_not_constant"


def test_synthetic_units_decode_as_assembled():
    """Encoding slips of the mini assembler would show up here."""
    from scopt.validate_sym.mutants import synthetic_units

    for name, p in synthetic_units().items():
        ex = explore(p, layout=MINI_LAYOUT)
        assert ex.complete, (name, ex.unsupported)
        assert all(s.end and s.end[0] == "exit" for s in ex.paths), name


def test_cli_registered():
    from click.testing import CliRunner

    from scopt.cli import cli, load_commands

    r = CliRunner().invoke(load_commands(cli), ["hostsym", "--help"])
    assert r.exit_code == 0 and "selftest" in r.output and "coverage" in r.output


def test_contract_override_dataclass():
    from scopt.analysis.obs_contract import build_contract

    c = build_contract(None, None)
    assert c.tb_start_cb_possible is True  # v1/no identity: fail closed
    c2 = dataclasses.replace(c, tb_start_cb_possible=False)
    assert not c2.observable("neg.can_do_io", "S2", exit_kind="goto_tb")

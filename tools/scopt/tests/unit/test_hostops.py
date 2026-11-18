"""WP17: HostOps, hcfg, hlive, effects (design §7.1, §7.3, §8.1).

Real-snapshot tests use the B1/B2 store snapshots (read-only, skipped when
absent); synthetic cases mutate a real unit's bytes/metadata in memory.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from scopt.analysis.effects import build_effects, call_class
from scopt.analysis.hcfg import build_hcfg, exit_class
from scopt.analysis.hlive import build_hlive
from scopt.analysis.hostops import (OpKind, build_hostops, census, tier0_prefix_plugin_calls)
from scopt.formats.envlayout_v1 import find_envlayout
from scopt.formats.enums import OpMarkKind
from scopt.model.unit import OpMark

ROOT = Path(os.environ.get("FASTDYN_ROOT", "/data/extra_copy/fastdyn"))
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
_CACHE: dict[str, object] = {}


def snap(name: str):
    p = SNAPS[name]
    if not (p / "snapshot.json").is_file():
        pytest.skip(f"{p} not present")
    if name not in _CACHE:
        from scopt.importer.import_cache import open_snapshot

        _CACHE[name] = open_snapshot(p)
    return _CACHE[name]


def layout_of(s):
    lay = find_envlayout(s.runtime["qemu_sha256"])
    assert lay is not None
    return lay


def unit_at(s, vaddr: int, cflags: int | None = None):
    for u in s.units():
        if u.vaddrs and u.vaddrs[0] == vaddr and (cflags is None or u.key.cflags == cflags):
            return u
    raise KeyError(hex(vaddr))


def hops(s, u):
    return build_hostops(u, layout_of(s), s.header.code_address)


def patch(u, off: int, new: bytes):
    code = bytearray(u.code)
    code[off:off + len(new)] = new
    return replace(u, code=bytes(code))


# -- real snapshots: the WP17 acceptance numbers --------------------------------------------------

EXPECTED = {  # (units, env stores) per snapshot; all stores explained, no rbp operand unclassified
    "minimal": (163, 1440),
    "fault": (48, 494),
}


@pytest.mark.real
@pytest.mark.parametrize("name", ["minimal", "fault"])
def test_census_small(name):
    s = snap(name)
    c = census(s.units(), layout_of(s), s.header.code_address)
    n, stores = EXPECTED[name]
    assert c["units"] == n
    assert c["status"] == {"ok": n}
    assert c["env_stores"] == stores
    assert c["unexplained_env_stores"] == 0
    assert c["unclassified_rbp_operands"] == 0
    assert c["prefix_plugin_calls"] == 0


@pytest.mark.real
@pytest.mark.slow
def test_census_compute_matches_state_sync_map():
    """compute: 16,495 TCG-global stores, 5,681 plugin_mem_value stores and 3,942
    can_do_io stores, the static counts of notes/b3/map-state-sync-traffic.md §7.2."""
    s = snap("compute")
    c = census(s.units(), layout_of(s), s.header.code_address)
    o = c["env_store_origins"]
    assert c["status"] == {"ok": 2222}
    assert c["unexplained_env_stores"] == 0 and c["unclassified_rbp_operands"] == 0
    assert o["global_sync"] == 16495
    assert o["plugin_mem_value"] == 5681
    assert o["cdio_prefix"] + o["cdio_insn"] == 3942
    assert o["icount_entry"] == 2222
    assert c["fields_stored"]["thumb"] == 164
    assert c["fields_stored"]["condexec_bits"] == 29


@pytest.mark.real
def test_typed_ops_ldst_and_plugin_call():
    s = snap("minimal")
    u = unit_at(s, 0x8003972)  # 3 guest stores + one plugin callback + lookup exit
    h = hops(s, u)
    assert h.ok, h.reasons
    kinds = [o.kind for o in h.ops]
    assert kinds.count(OpKind.GUEST_ACCESS) == 3
    ga = [o for o in h.ops if o.kind == OpKind.GUEST_ACCESS]
    assert [o.ldst for o in ga] == [0, 1, 2] and all(o.is_ld is False for o in ga)
    calls = [o for o in h.ops if o.kind == OpKind.CALL]
    assert {o.helper_name for o in calls} >= {None, "lookup_tb_ptr", "<ldst_slow_path>"}
    plugin = next(o for o in calls if o.helper_name is None)
    assert plugin.block == "insn" and plugin.insn == 0
    assert any(o.kind == OpKind.GOTO_PTR for o in h.ops)
    blocks = {b.kind for b in h.blocks}
    assert {"prefix", "insn", "ldst_fast", "ldst_slow", "exit_stub", "pool"} <= blocks
    # can_do_io = 1 before the last insn_start
    cd = [o for o in h.ops if o.store_field == "neg.can_do_io"]
    assert [o.imm for o in cd] == [0, 1] and cd[0].origin == "cdio_prefix"
    # the plugin_mem_value store follows each guest store in the same insn
    pmv = [o for o in h.ops if o.origin == "plugin_mem_value"]
    assert len(pmv) == 3


@pytest.mark.real
def test_store_through_constant_register_is_explained():
    """`mov byte [rbp-4], r12b` with r12 = 1 set before an LDST (callee-saved across the
    slow path) is a can_do_io store, not an unexplained one."""
    s = snap("minimal")
    u = s.unit("tb:6b667b9b2fedd5326f428806cf8b61cc#0")
    h = hops(s, u)
    assert h.ok, h.reasons
    o = h.op_at(0x68)
    assert o.kind == OpKind.ENV_STORE_REG and o.imm == 1 and o.origin == "cdio_insn"


# -- fail closed (synthetic mutations of a real unit) -------------------------------------------------

def _store_unit(s):
    """A unit with a `mov dword [rbp+disp32], reg` store we can retarget."""
    for u in s.units():
        h = hops(s, u)
        for o in h.ops:
            if o.kind == OpKind.ENV_STORE_REG and o.size == 7 and o.store_field == "NF":
                return u, o
    pytest.skip("no NF store with disp32 found")


@pytest.mark.real
def test_unclassified_env_access_pins():
    s = snap("minimal")
    u, o = _store_unit(s)
    raw = bytearray(u.code[o.off:o.off + o.size])
    raw[3:7] = (0x14E).to_bytes(4, "little")  # the hole after thumb
    h = hops(s, patch(u, o.off, bytes(raw)))
    assert not h.ok
    assert any(r.startswith("unclassified_env_access@") for r in h.reasons)
    assert h.unclassified == (o.off,)


@pytest.mark.real
def test_store_to_tlb_field_is_unexplained():
    s = snap("minimal")
    u, o = _store_unit(s)
    raw = bytearray(u.code[o.off:o.off + o.size])
    raw[3:7] = (-0x110 & 0xFFFFFFFF).to_bytes(4, "little")  # neg.tlb.f[1].mask
    h = hops(s, patch(u, o.off, bytes(raw)))
    assert any(r.startswith("unexplained_env_store@") for r in h.reasons)
    assert h.op_at(o.off).origin == "unexplained"


@pytest.mark.real
def test_unbound_layout_pins_everything():
    s = snap("fault")
    h = build_hostops(s.units()[0], None, s.header.code_address)
    assert h.status == "pinned" and h.reasons == ("guard_layout_unbound",) and not h.ops


@pytest.mark.real
def test_branch_without_site_pins():
    s = snap("fault")
    u = s.units()[0]
    h0 = hops(s, u)
    t = next(o for o in h0.ops if o.block == "prefix" and o.kind == OpKind.FLAGS and o.size == 2)
    h = hops(s, patch(u, t.off, b"\xeb\x00"))  # jmp $+2: no capture site explains it
    assert any(r.startswith("unexplained_branch@") for r in h.reasons)


# -- hcfg ------------------------------------------------------------------------------------------

@pytest.mark.real
@pytest.mark.parametrize("name", ["minimal", "fault"])
def test_hcfg_well_formed(name):
    s = snap(name)
    for u in s.units():
        h = hops(s, u)
        g = build_hcfg(h)
        assert g.ok, (u.uid, g.reasons)
        assert not g.cyclic
        ps = g.paths(max_paths=256)
        assert ps is not None and ps
        for p in ps:
            assert p[0] == 0 and p[-1] in g.exits
            assert all(h.ops[i].block not in ("ldst_slow", "pad") for i in p)
        # every body op is reachable in the atomic view, except goto_tb fallback stubs
        for i, o in enumerate(h.ops):
            if o.block in ("prefix", "insn", "ldst_fast"):
                assert i in g.reachable_atomic, (u.uid, hex(o.off))


@pytest.mark.real
def test_hcfg_exits_and_views():
    s = snap("minimal")
    u = unit_at(s, 0x8018740)  # one guest load, lookup exit
    h = hops(s, u)
    g = build_hcfg(h)
    kinds = sorted(exit_class(k) for k in g.exits.values())
    assert kinds == ["exit_requested", "goto_ptr"]
    jne = next(i for i, o in enumerate(h.ops) if "LDST_FAST_JCC" in o.sites)
    # atomic: only the fall-through; full: also the slow path
    assert len(g.successors(jne, atomic=True)) == 1
    full = g.successors(jne, atomic=False)
    assert len(full) == 2 and any(h.ops[j].block == "ldst_slow" for j in full)
    # the slow path returns to raddr in the full view
    ret = next(i for i, o in enumerate(h.ops) if "LDST_RET_JMP" in o.sites)
    raddr = g.successors(ret, atomic=False)[0]
    assert h.ops[raddr].off == u.ldst[0].raddr_off
    # entry check edge to the exit-request stub
    jl = next(i for i, o in enumerate(h.ops) if o.block == "prefix" and o.kind == OpKind.JCC)
    assert "entry_check" in {g.edge_kinds[(jl, j)] for j in g.successors(jl)}


@pytest.mark.real
def test_hcfg_goto_tb_slots():
    s = snap("fault")
    for u in s.units():
        h = hops(s, u)
        g = build_hcfg(h)
        slots = sorted(int(k.split(":")[1]) for k in g.exits.values() if k.startswith("goto_tb"))
        assert slots == sorted(x.slot for x in u.static_exits if x.slot in (0, 1))


# -- hlive ------------------------------------------------------------------------------------------

@pytest.mark.real
@pytest.mark.parametrize("name", ["minimal", "fault"])
def test_hlive_no_dead_pure_op_in_tcg_output(name):
    """TCG's own liveness leaves no dead register-only op; hlive must agree (no false 'dead')."""
    s = snap(name)
    for u in s.units():
        h = hops(s, u)
        g = build_hcfg(h)
        lv = build_hlive(h, g)
        for i, o in enumerate(h.ops):
            if o.kind in (OpKind.OPAQUE, OpKind.CONST, OpKind.COPY, OpKind.FLAGS) and \
                    i in g.reachable_atomic:
                assert not lv.dead_after(i), (u.uid, hex(o.off), o.text)


@pytest.mark.real
def test_hlive_detects_dead_test_after_branch_removed():
    s = snap("minimal")
    for u in s.units():
        h = hops(s, u)
        for i, o in enumerate(h.ops[:-1]):
            nx = h.ops[i + 1]
            if o.kind == OpKind.FLAGS and nx.kind == OpKind.JCC and nx.size == 6 and \
                    "LABEL_JCC" in nx.sites and nx.block == "insn":
                pu = patch(u, nx.off, bytes.fromhex("660f1f440000"))  # 6-byte nop
                h2 = hops(s, pu)
                g2 = build_hcfg(h2)
                lv = build_hlive(h2, g2)
                assert lv.dead_after(i), (u.uid, hex(o.off))
                lv0 = build_hlive(h, build_hcfg(h))
                assert not lv0.dead_after(i)
                return
    pytest.skip("no test+jcc rel32 pair found")


# -- effects ----------------------------------------------------------------------------------------

@pytest.mark.real
def test_effects_sync_points():
    s = snap("minimal")
    u = unit_at(s, 0x8003972)
    h = hops(s, u)
    e = build_effects(h, build_hcfg(h))
    c = e.counts
    assert c["S1"] == 1 and c["S5"] == 3 and c["S3"] == 1 and c["S4"] == 1
    assert c["S2"] == 2  # goto_ptr + the exit-request stub
    assert c["ev:guest_access"] == 3


def test_call_class():
    assert call_class("lookup_tb_ptr", 6) == "lookup"
    assert call_class(None, 1) == "plugin"
    assert call_class("udiv", 1) == "raising"
    assert call_class("rebuild_hflags_m32", 1) == "pure"
    assert call_class("v7m_msr", 0) == "global"
    assert call_class("exception_internal", 8) == "noreturn"
    assert call_class("<ldst_slow_path>", None) == "ldst_slow"


# -- Tier-0 TB-start callback evidence ----------------------------------------------------------------

@pytest.mark.real
def test_tier0_prefix_plugin_calls():
    s = snap("minimal")
    units = list(s.units())
    assert tier0_prefix_plugin_calls(units) == []
    u = units[0]
    fake = replace(u, opmarks=(OpMark(1, OpMarkKind.CALL, 0, 99),) + tuple(u.opmarks))
    hits = tier0_prefix_plugin_calls([fake] + units[1:])
    assert hits == [(u.uid, 1, 99)]


# -- AnalysisStore integration --------------------------------------------------------------------------

@pytest.mark.real
def test_analysis_store_chain():
    from scopt.analysis.store import AnalysisStore

    s = snap("fault")
    st = AnalysisStore(s)
    uid = s.units()[0].uid
    r = st.get("hlive", uid)
    assert r.value.uid == uid
    assert st.get("hostops", uid).value.ok
    assert st.get("effects", uid).value.counts["S1"] == 1
    lay = st.get("envlayout", s.snapshot_id).value
    assert lay["bound"]
    oc = st.get("obs-contract", s.snapshot_id).value
    assert oc.tb_start_cb_possible is True  # v1 RuntimeIdentity: unknown => true

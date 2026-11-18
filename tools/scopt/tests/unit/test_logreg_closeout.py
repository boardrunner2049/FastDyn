"""Close-out: the coverage LOG_REG block as one host event (design/closeout-logreg.md §A).

* the matcher (:mod:`scopt.decode.logreg`) on the real bytes of the four coverage
  snapshots (F-B4-10, F-B3-23; icount and noicount): 238/238 blocks, one triple;
* every deviation of the block (each byte outside the holes, wrong / env-aliasing base,
  an extra store, a changed loop target, the metadata, the pointer classes) is a
  ``NoMatch`` and the unit falls back to the pre-close-out fail-closed treatment
  (HostOps pinned, hcfg cyclic, syncdead skipped, hostsym ``backward_branch``);
* HostOps / hcfg / hlive / effects / hconst / syncdead on matched units;
* hostsym: the ``host_log`` event, relocation, the structural 1:1 rule and the
  ``abs.logreg_ring`` rule.
"""

from __future__ import annotations

import dataclasses
import json
import os
from functools import lru_cache
from pathlib import Path

import pytest

from scopt.decode import logreg as L
from scopt.validate_sym.asm import nop
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
COVER = {
    ("b4-10", "icount"): ROOT / "runs/wp29/fx/icount/b4-10/snap.json",
    ("b4-10", "noicount"): ROOT / "runs/wp29/fx/noicount/b4-10/snap.json",
    ("23", "icount"): ROOT / "runs/wp28/fx/icount/23/snap.json",
    ("23", "noicount"): ROOT / "runs/wp28/fx/noicount/23/snap.json",
}
TRIPLE = (0x7FFF63F6E570, 0x7FFF43DA945C, 0x7FFF68000B70)


# ---------------------------------------------------------------------------
# real snapshots
# ---------------------------------------------------------------------------

@lru_cache(maxsize=8)
def _load(key):
    sj = COVER[key]
    if not sj.is_file():
        pytest.skip(f"{sj} not present")
    from scopt.analysis.obs_contract import build_contract
    from scopt.formats.bundle_v1 import (admission_facts, extend_runtime_identity,
                                         read_runtime_identity)
    from scopt.formats.envlayout_v1 import find_envlayout
    from scopt.importer.import_cache import open_snapshot

    d = json.loads(sj.read_text())
    snap = Path(d["snapshot"])
    if not snap.is_dir():
        pytest.skip(f"{snap} not present")
    s = open_snapshot(snap)
    ri = read_runtime_identity(snap / "runtime_identity.json")
    lay = find_envlayout(ri["qemu_sha256"])
    man = json.loads((snap / "manifest.json").read_text())
    adm = admission_facts(manifest=man, qemu_sha256=ri["qemu_sha256"], envlayout=lay,
                          config_text=Path(d["base_toml"]).read_text())
    con = build_contract(lay, extend_runtime_identity(ri, adm))
    return s, lay, con


def _block_unit(key=("b4-10", "icount")):
    s, lay, con = _load(key)
    for u in s.units():
        m = L.meta_of_unit(u, lay, s.header.code_address)
        if L.match_logreg(u.code, s.header.code_address + u.tc_offset, m):
            return s, lay, con, u, m
    raise AssertionError("no block unit")


def _mut(u, patches):
    code = bytearray(u.code)
    for off, b in patches:
        code[off:off + len(b)] = b
    return dataclasses.replace(u, code=bytes(code))


@pytest.mark.real
@pytest.mark.parametrize("key", sorted(COVER))
def test_real_blocks_all_matched_one_triple(key):
    from scopt.analysis.hcfg import build_hcfg
    from scopt.analysis.hostops import build_hostops, census

    s, lay, _ = _load(key)
    ca = s.header.code_address
    n_units = n_block = 0
    for u in s.units():
        n_units += 1
        h = build_hostops(u, lay, ca)
        if not h.log_blocks:
            assert u.key.cflags & 0x1000, "only CF_MEMI_ONLY io-recompile TBs have no block"
            continue
        n_block += 1
        lb = h.log_blocks[0]
        assert lb.start == u.insn0_start and lb.triple == TRIPLE
        assert h.ok, h.reasons
        g = build_hcfg(h)
        assert g.ok and not g.cyclic and len(g.spin_edges) == 1
        je, st = g.spin_edges[0]
        assert h.ops[je].off == lb.spin_jcc and h.ops[st].off == lb.start
        assert st in g.successors(je, atomic=False) and st not in g.successors(je, atomic=True)
        ring = h.op_at(lb.ring_store)
        assert ring.kind == "HOST_ABS_STORE" and ring.abs_addr == TRIPLE[2] and ring.log == 0
        assert sum(1 for o in h.ops if o.log is not None) == 15
    assert n_block == {"b4-10": 62, "23": 57}[key[0]] and n_units - n_block == 9
    cz = census(s.units(), lay, ca)
    assert cz["log_blocks_matched"] == n_block and cz["logreg_triple_consistent"]
    assert cz["logreg_unmatched"] == {} and cz["status"] == {"ok": n_units}
    assert L.snapshot_triples(s.units(), lay, ca).keys() == {TRIPLE}


@pytest.mark.real
@pytest.mark.slow
@pytest.mark.parametrize("key,want", [(("b4-10", "icount"), (17, 45, 129)),
                                      (("b4-10", "noicount"), (17, 45, 129)),
                                      (("23", "icount"), (8, 39, 122))])
def test_real_syncdead_predicted_deletions(key, want):
    """F-B4-10 gets F-B4-1's deletions (D1 17 / D2 45 / D3 129, design §A.4)."""
    from collections import Counter

    from scopt.analysis.hcfg import build_hcfg
    from scopt.analysis.hostops import build_hostops
    from scopt.analysis.syncdead import TrackedBytes, syncdead_unit

    s, lay, con = _load(key)
    tb = TrackedBytes.of(lay)
    dead: Counter[str] = Counter()
    ok = 0
    for u in s.units():
        h = build_hostops(u, lay, s.header.code_address)
        if not h.log_blocks:
            continue
        sd = syncdead_unit(u, h, build_hcfg(h), lay, con, tracked=tb)
        ok += sd.ok
        dead.update(d.cls for d in sd.dead)
        assert all(not h.log_block_at(d.off) for d in sd.dead)
    assert ok == {"b4-10": 62, "23": 57}[key[0]]
    assert (dead["D1"], dead["D2"], dead["D3"]) == want


@pytest.mark.real
def test_real_block_bytes_are_the_canonical_form():
    s, lay, _, u, m = _block_unit()
    lb = L.match_logreg(u.code, s.header.code_address + u.tc_offset, m)
    blk = bytes(u.code[lb.start:lb.end])
    re = L.encode_block(s.header.code_address + u.tc_offset + lb.start, *TRIPLE, lb.value_off)
    assert blk == re
    assert lb.value_off == lay.offset("regs[15]") == 0x3C


# ---------------------------------------------------------------------------
# negative: every deviation is a NoMatch and the unit stays fail-closed
# ---------------------------------------------------------------------------

def _hole_bytes():
    return {b for h, _e, _r in L.RIP_HOLES for b in range(h, h + 4)}


@pytest.mark.real
def test_every_byte_flip_outside_the_holes_is_nomatch():
    s, lay, _, u, m = _block_unit()
    base = s.header.code_address + u.tc_offset
    S = u.insn0_start
    holes = _hole_bytes()
    n = 0
    for k in range(L.LOGREG_LEN):
        if k in holes:
            continue
        for x in (0x01, 0x80):
            code = bytearray(u.code)
            code[S + k] ^= x
            r = L.match_logreg(bytes(code), base, m)
            assert not r, (k, x)
            assert r.rule == ("field" if k == L.FIELD_HOLE else "bytes"), (k, r)
            n += 1
    assert n == 2 * (L.LOGREG_LEN - 12)


@pytest.mark.real
@pytest.mark.parametrize("hole", [0x03, 0x17, 0x2A])
def test_retargeted_hole_is_nomatch(hole):
    s, lay, _, u, m = _block_unit()
    base = s.header.code_address + u.tc_offset
    S = u.insn0_start
    code = bytearray(u.code)
    d = int.from_bytes(code[S + hole:S + hole + 4], "little", signed=True)
    code[S + hole:S + hole + 4] = (d + 0x40).to_bytes(4, "little", signed=True)
    r = L.match_logreg(bytes(code), base, m)
    assert not r and r.rule == "metadata"


@pytest.mark.real
@pytest.mark.parametrize("name,patch,rule", [
    # env-aliasing base: add r13, rbp (the ring pointer built from env)
    ("env_aliasing_base", (0x2E, "4c03ed"), "bytes"),
    # wrong base register for the ring store: mov [r12], r14d
    ("wrong_store_base", (0x35, "45893424"), "bytes"),
    # an extra store: add r12d,4 -> mov [rbp+0x20],r12d (a regs[8] store in the block)
    ("extra_store", (0x39, "44896520"), "bytes"),
    # changed loop target: je S+7
    ("loop_target", (0x23, "e3"), "bytes"),
    # rel32 je (the long form)
    ("je_rel32", (0x22, "0f84"), "bytes"),
    # movabs instead of the rip lea
    ("movabs_idx", (0x00, "48bb"), "bytes"),
    # logger form: the spin removed
    ("no_spin", (0x22, "6690"), "bytes"),
    # the value read from regs[14] instead of regs[15]
    ("value_field", (0x34, "38"), "field"),
])
def test_block_mutants_nomatch(name, patch, rule):
    s, lay, _, u, m = _block_unit()
    off, hx = patch
    mu = _mut(u, [(u.insn0_start + off, bytes.fromhex(hx))])
    r = L.match_logreg(mu.code, s.header.code_address + u.tc_offset, m)
    assert not r and r.rule == rule, r


def _meta(m, **kw):
    return dataclasses.replace(m, **kw)


@pytest.mark.real
def test_metadata_rules():
    s, lay, _, u, m = _block_unit()
    base = s.header.code_address + u.tc_offset
    S = u.insn0_start
    code = bytes(u.code)
    assert L.match_logreg(code, base, m)
    # not at insn0_start
    r = L.match_logreg(code, base, _meta(m, insn0_start=S + 1))
    assert not r and r.rule == "position"
    # an extra site inside the block
    extra = L.LogSite(S + 0x24, "HELPER_CALL", 0x1234, S + 0x25, 4)
    r = L.match_logreg(code, base, _meta(m, sites=m.sites + (extra,)))
    assert not r and r.rule == "metadata"
    # an OpMark inside the block
    r = L.match_logreg(code, base, _meta(m, marks=m.marks + (S + 0x31,)))
    assert not r and r.rule == "metadata"
    # the spin site targets elsewhere
    sites = tuple(dataclasses.replace(x, value=x.value + 7) if x.off == S + L.SPIN_JCC else x
                  for x in m.sites)
    r = L.match_logreg(code, base, _meta(m, sites=sites))
    assert not r and r.rule == "metadata"
    # a MOVI_CONST site value that disagrees with the bytes
    sites = tuple(dataclasses.replace(x, value=x.value + 8) if x.off == S + 0x27 else x
                  for x in m.sites)
    r = L.match_logreg(code, base, _meta(m, sites=sites))
    assert not r and r.rule == "metadata"
    # a second branch into the block
    br = L.LogSite(S + 0x60, "LABEL_JCC", base + S + 0x24, S + 0x62, 4)
    r = L.match_logreg(code, base, _meta(m, sites=m.sites + (br,)))
    assert not r and r.rule == "entry"
    br = L.LogSite(S + 0x60, "LABEL_JMP", base + S, S + 0x61, 4)
    r = L.match_logreg(code, base, _meta(m, sites=m.sites + (br,)))
    assert not r and r.rule == "entry"
    # an LDST fast path overlapping the block
    r = L.match_logreg(code, base, _meta(m, busy=m.busy + ((S + 0x30, S + 0x50, "ldst_fast"),)))
    assert not r and r.rule == "position"
    # regs[15] not bound
    r = L.match_logreg(code, base, _meta(m, value_field=None))
    assert not r and r.rule == "layout"
    # rule 7: another expected triple
    r = L.match_logreg(code, base, m, expect=(TRIPLE[0], TRIPLE[1], TRIPLE[2] + 4))
    assert not r and r.rule == "triple"
    assert L.match_logreg(code, base, m, expect=TRIPLE)


@pytest.mark.real
@pytest.mark.parametrize("which,cls,anchor", [
    (2, "abs.qemu_data", "qemu-system-arm+0x100"),  # BUF in a module image
    (2, "arena.abs", "arena+0x100"),
    (2, "abs.guest_ram", "ramblock:mps.ram+0x0"),
    (2, "abs.other", "[stack]@0x7ffffffde000+0x10"),  # not an anonymous mapping
    (2, "abs.other", "/usr/lib/x.so@0x7fff00000000+0x10"),
    (0, "abs.other", "anon@0x7fff68000000+0x0"),  # IDX not plugin data
    (1, "abs.plugin_data", "libother.so+0x10"),  # CONS in another module
    (0, "abs.plugin_data", "libother.so+0x10"),
])
def test_pointer_class_rules(which, cls, anchor):
    s, lay, _, u, m = _block_unit()
    base = s.header.code_address + u.tc_offset
    v = TRIPLE[which]
    ptrs = tuple((val, cls, anchor) if val == v else (val, c, a) for val, c, a in m.pointers)
    r = L.match_logreg(bytes(u.code), base, _meta(m, pointers=ptrs))
    assert not r and r.rule == "pointers", r
    # unbound
    ptrs = tuple(x for x in m.pointers if x[0] != v)
    r = L.match_logreg(bytes(u.code), base, _meta(m, pointers=ptrs))
    assert not r and r.rule == "pointers"


@pytest.mark.real
@pytest.mark.parametrize("cls,anchor", [
    ("abs.heap", "heap+0xb70"),  # [heap] holds env on the gate binary (B4-gate.md)
    ("abs.heap", "heap+0x0"),
    ("abs.guest_ram", "ramblock:mps.ram+0xb70"),
    ("abs.qemu_data", "qemu-system-arm+0xb70"),
    ("abs.plugin_data", "libfastdyn.so+0x24f6f000"),
    ("abs.other", "[stack]@0x7ffffffde000+0xb70"),
    ("arena.abs", "arena+0xb70"),
    ("const", "0x7fff68000b70"),
])
def test_ring_base_that_may_alias_env_is_rejected(cls, anchor):
    """closeout2: rule 5 fails closed on the ring base. ``abs.heap`` was accepted before; on
    the gate binary env lives in [heap] and gate runs pass no env_abs, so neither the matcher
    nor V12 could tell a ring pointer from an env pointer. Only a nameless anonymous mapping
    (the tracer arena, every real block) is a ring base; the NoMatch reason is stable."""
    s, lay, _, u, m = _block_unit()
    base = s.header.code_address + u.tc_offset
    assert L.match_logreg(bytes(u.code), base, m).ring_class == L.RING_BASE_CLASS
    ptrs = tuple((val, cls, anchor) if val == TRIPLE[2] else (val, c, a)
                 for val, c, a in m.pointers)
    r = L.match_logreg(bytes(u.code), base, _meta(m, pointers=ptrs))
    assert not r and r.rule == "pointers", r
    assert r.detail.startswith(L.RING_BASE_REFUSED + ":"), r.detail
    assert f"BUF class {cls} anchor {anchor}" in r.detail


@pytest.mark.real
def test_heap_ring_unit_is_treated_as_before():
    """A unit whose ring base is bound as abs.heap is not a LOG_REG block anywhere: HostOps
    keeps the ring store pinned (logreg_unmatched{pointers}), hcfg stays cyclic, syncdead
    skips it, and hostsym has no log block and ends inconclusive / mismatch (never
    equivalent through the LOG_REG abstraction)."""
    from scopt.analysis.hcfg import build_hcfg
    from scopt.analysis.hostops import build_hostops
    from scopt.analysis.syncdead import syncdead_unit
    from scopt.validate_sym import check_equiv, program_of, world_of

    s, lay, con, u, _m = _block_unit()
    hp = [dataclasses.replace(p, cls="abs.heap", anchor="heap+0xb70")
          if int(u.sites[p.site].value) == TRIPLE[2] else p
          for p in u.bindings.unchecked_host_pointers]
    hu = dataclasses.replace(u, bindings=dataclasses.replace(
        u.bindings, unchecked_host_pointers=tuple(hp)))
    m = L.meta_of_unit(hu, lay, s.header.code_address)
    r = L.match_logreg(bytes(hu.code), s.header.code_address + hu.tc_offset, m)
    assert not r and r.rule == "pointers" and r.detail.startswith(L.RING_BASE_REFUSED)
    h = build_hostops(hu, lay, s.header.code_address)
    assert h.log_blocks == () and h.logreg_nomatch == "pointers"
    assert not h.ok and any(x.startswith("unexplained_host_store@") for x in h.reasons)
    assert all(o.log is None and not o.spin for o in h.ops)
    g = build_hcfg(h)
    assert not g.spin_edges
    sd = syncdead_unit(hu, h, g, lay, con)
    assert not sd.ok and not sd.dead
    p = program_of(hu, world_of(s, lay))
    assert not p.logblocks
    v = check_equiv(p, p, None, None, con, layout=lay)
    assert v.status in ("inconclusive", "mismatch"), v


@pytest.mark.real
@pytest.mark.parametrize("patch", [(0x35, "45893424"), (0x23, "e3"), (0x39, "44896520"),
                                   (0x2E, "4c03ed")])
def test_unmatched_unit_is_treated_as_before(patch):
    """A block the matcher refuses: no LOG_REG typing; the unit is pinned (or its loop is a
    cycle), syncdead skips it and hostsym ends ``backward_branch`` (inconclusive)."""
    from scopt.analysis.hcfg import build_hcfg
    from scopt.analysis.hostops import build_hostops
    from scopt.analysis.syncdead import syncdead_unit
    from scopt.validate_sym import check_equiv, program_of, world_of

    s, lay, con, u, _m = _block_unit()
    off, hx = patch
    mu = _mut(u, [(u.insn0_start + off, bytes.fromhex(hx))])
    h = build_hostops(mu, lay, s.header.code_address)
    assert not h.log_blocks and all(o.log is None and not o.spin for o in h.ops)
    assert h.logreg_nomatch == "bytes"
    g = build_hcfg(h)
    assert not h.ok or g.cyclic
    assert not g.spin_edges
    sd = syncdead_unit(mu, h, g, lay, con)
    assert not sd.ok and not sd.dead
    p = program_of(mu, world_of(s, lay))
    assert not p.logblocks
    v = check_equiv(p, p, None, None, con, layout=lay)
    assert v.status in ("inconclusive", "mismatch")
    assert "backward_branch" in v.detail or v.reason == "host_abs_aliases_env", v.detail


@pytest.mark.real
def test_snapshot_triples_two_rings():
    s, lay, _, u, m = _block_unit()
    ca = s.header.code_address
    # move BUF of one unit (bytes + site + binding agree): a second ring
    S = u.insn0_start
    d = int.from_bytes(u.code[S + 0x2A:S + 0x2E], "little", signed=True)
    code = bytearray(u.code)
    code[S + 0x2A:S + 0x2E] = (d + 0x100).to_bytes(4, "little", signed=True)
    k = next(i for i, x in enumerate(u.sites) if x.insn_off == S + 0x27)
    sites = list(u.sites)
    sites[k] = dataclasses.replace(sites[k], value=sites[k].value + 0x100)
    u2 = dataclasses.replace(u, code=bytes(code), sites=tuple(sites), uid=u.uid + "~")
    tr = L.snapshot_triples([*s.units(), u2], lay, ca)
    assert len(tr) == 2 and tr[(TRIPLE[0], TRIPLE[1], TRIPLE[2] + 0x100)] == [u2.uid]


# ---------------------------------------------------------------------------
# dataflow on a matched unit
# ---------------------------------------------------------------------------

@pytest.mark.real
def test_hlive_effects_hconst_on_block():
    from scopt.analysis.effects import build_effects
    from scopt.analysis.hcfg import build_hcfg
    from scopt.analysis.hconst import build_hconst
    from scopt.analysis.hlive import build_hlive
    from scopt.analysis.hostops import build_hostops

    s, lay, _, u, _m = _block_unit()
    h = build_hostops(u, lay, s.header.code_address)
    lb = h.log_blocks[0]
    g = build_hcfg(h)
    lv = build_hlive(h, g)
    je = h.index_of(lb.spin_jcc)
    st = h.index_of(lb.start)
    # the spin edge keeps the loop's registers live: rbx (IDX) and r12 (idx) after the je
    assert {"rbx", "r12"} <= lv.live_out[je]
    assert je in g.predecessors(st, atomic=False)
    ev = build_effects(h, g)
    stores = [d for i, k, d in ev.events if k == "host_store" and h.ops[i].log is not None]
    assert stores == [f"abs={TRIPLE[2]:#x}", f"abs={TRIPLE[0]:#x}"]
    # facts survive the block (the ring/index stores are outside env); nothing in it folds
    hc = build_hconst(h, g, {"r0": 0, "r1": 5}, code=u.code)
    after = h.index_of(lb.end)
    assert hc.env_at(after) == {"r0": 0, "r1": 5}
    assert not any(h.ops[i].log is not None for i in hc.decided)
    assert not any(h.ops[i].log is not None for i in hc.foldable)


@pytest.mark.real
def test_hconst_unmatched_host_store_still_kills_facts():
    from scopt.analysis.hcfg import build_hcfg
    from scopt.analysis.hconst import build_hconst
    from scopt.analysis.hostops import build_hostops

    s, lay, _, u, _m = _block_unit()
    h = build_hostops(u, lay, s.header.code_address)
    lb = h.log_blocks[0]
    i = h.index_of(lb.idx_store)
    ops = list(h.ops)
    ops[i] = dataclasses.replace(ops[i], log=None)  # an index store not owned by a block
    h2 = dataclasses.replace(h, ops=tuple(ops), log_blocks=())
    hc = build_hconst(h2, build_hcfg(h), {"r0": 0}, code=u.code)
    assert hc.env_at(i + 1) == {}


@pytest.mark.real
def test_hcfg_malformed_marking():
    from scopt.analysis.hcfg import build_hcfg
    from scopt.analysis.hostops import build_hostops

    s, lay, _, u, _m = _block_unit()
    h = build_hostops(u, lay, s.header.code_address)
    lb = h.log_blocks[0]
    ops = list(h.ops)
    k = h.index_of(lb.start + 0x07)
    ops[k] = dataclasses.replace(ops[k], log=None)  # a hole in the block's ops
    g = build_hcfg(dataclasses.replace(h, ops=tuple(ops)))
    assert not g.ok and any(r.startswith("logreg_block_malformed") for r in g.reasons)
    ops = list(h.ops)
    j = h.index_of(lb.end)
    ops[j] = dataclasses.replace(ops[j], spin=True)  # a spin outside the block
    g = build_hcfg(dataclasses.replace(h, ops=tuple(ops)))
    assert not g.ok


@pytest.mark.real
def test_syncdead_refuses_a_store_inside_the_block():
    from scopt.analysis.hcfg import build_hcfg
    from scopt.analysis.hostops import build_hostops
    from scopt.analysis.syncdead import B4Reason, syncdead_unit

    s, lay, con, u, _m = _block_unit()
    h = build_hostops(u, lay, s.header.code_address)
    g = build_hcfg(h)
    base = syncdead_unit(u, h, g, lay, con)
    assert base.ok and base.dead
    d = base.dead[0]
    ops = list(h.ops)
    ops[d.op] = dataclasses.replace(ops[d.op], log=0)  # pretend the store is in the block
    sd = syncdead_unit(u, dataclasses.replace(h, ops=tuple(ops)), g, lay, con)
    assert d.off not in {x.off for x in sd.dead}
    assert any(r.off == d.off and r.reason == B4Reason.EDIT_INSIDE_LOG_BLOCK for r in sd.refused)
    assert B4Reason.EDIT_INSIDE_LOG_BLOCK in B4Reason.FAILED


@pytest.mark.real
def test_hostops_digest_unchanged_without_block():
    """HostOp.to_json writes log/spin only when set (non-coverage cache keys unchanged)."""
    from scopt.analysis.hostops import build_hostops

    s, lay, _, u, _m = _block_unit()
    h = build_hostops(u, lay, s.header.code_address)
    plain = [o for o in h.ops if o.log is None]
    assert plain and all("log" not in o.to_json() and "spin" not in o.to_json() for o in plain)
    assert all("log" in o.to_json() for o in h.ops if o.log is not None)


# ---------------------------------------------------------------------------
# hostsym
# ---------------------------------------------------------------------------

@pytest.mark.real
def test_hostsym_host_log_event_and_relocation():
    from scopt.validate_sym import check_equiv, program_of, world_of
    from scopt.validate_sym.paths import explore

    s, lay, con, u, _m = _block_unit()
    w = world_of(s, lay)
    p = program_of(u, w)
    lb = p.logblocks[u.insn0_start]
    ex = explore(p, layout=lay)
    assert ex.complete
    reached = [st for st in ex.paths if st.end[1] != "exit_requested"]
    assert reached and all(not any(e.kind == "host_log" for e in st.events)
                           for st in ex.paths if st.end[1] == "exit_requested")
    for st in reached:
        logs = [e for e in st.events if e.kind == "host_log"]
        assert len(logs) == 1 and logs[0].data[0] == TRIPLE[2]
        assert ("logreg_spin_exit", lb.start) in st.assumes
        idx = [e for e in st.events if e.kind == "host_store" and e.data[0] == TRIPLE[0]]
        assert len(idx) == 1 and idx[0].data[1] == 2
    # a relocated copy (the C' identity offset map): rel32 displacements adjusted
    delta = 0x40000
    code = bytearray(p.code)
    for h, _e, _r in L.RIP_HOLES:
        o = lb.start + h
        d = int.from_bytes(code[o:o + 4], "little", signed=True) - delta
        code[o:o + 4] = d.to_bytes(4, "little", signed=True)
    q = dataclasses.replace(p, uid=p.uid + "@", code=bytes(code), base=p.base + delta)
    assert q.logblocks and q.logblocks[lb.start].canon == lb.canon
    # relocation of the whole unit needs its other rip operands too: compare the block only
    from scopt.validate_sym.equiv import log_blocks_identical
    assert log_blocks_identical(p, q) is None
    v = check_equiv(p, p, None, None, con, layout=lay)
    assert v.ok, v.detail


@pytest.mark.real
def test_hostsym_logged_value_is_compared():
    """A candidate that changes env.regs[15] before the block logs another value (V12)."""
    from scopt.validate_sym import check_equiv, patched, program_of, world_of
    from scopt.validate_sym.equiv import compare_traces, _Obs
    from scopt.validate_sym.paths import explore

    s, lay, con, u, _m = _block_unit(("b4-10", "noicount"))
    p = program_of(u, world_of(s, lay))
    # the prefix can_do_io store (c6 45 fc 00) -> mov [rbp+0x3c],ebx (89 5d 3c) + nop
    off = next(o for o in range(u.insn0_start) if p.code[o:o + 4] == bytes.fromhex("c645fc00"))
    n = patched(p, [(off, bytes.fromhex("895d3c") + nop(1))])
    assert n.logblocks.keys() == p.logblocks.keys()
    eo, en = explore(p, layout=lay), explore(n, layout=lay)
    d = compare_traces(eo.paths[0].events, en.paths[0].events, _Obs(None, lay))
    assert d is not None
    v = check_equiv(p, n, None, None, con, layout=lay)
    assert v.status == "mismatch"


@pytest.mark.real
@pytest.mark.parametrize("off,hx", [(L.RING_STORE, "0f1f4000"), (L.IDX_STORE, "0f1f4000"),
                                    (0x3C, "08"), (L.SPIN_JCC, "6690"), (0x34, "38")])
def test_hostsym_block_edit_is_failed(off, hx):
    from scopt.validate_sym import check_equiv, patched, program_of, world_of

    s, lay, con, u, _m = _block_unit()
    p = program_of(u, world_of(s, lay))
    n = patched(p, [(u.insn0_start + off, bytes.fromhex(hx))])
    v = check_equiv(p, n, None, None, con, layout=lay)
    assert v.status == "mismatch" and v.reason == "edit_inside_log_block", v.detail
    # and the reverse: a candidate may not gain a block
    v = check_equiv(n, p, None, None, con, layout=lay)
    assert v.status == "mismatch" and v.reason == "edit_inside_log_block"


@pytest.mark.real
def test_hostsym_ring_rules():
    from scopt.validate_sym import check_equiv, program_of, world_of
    from scopt.validate_sym.equiv import abs_class

    s, lay, con, u, _m = _block_unit()
    p = program_of(u, world_of(s, lay))
    assert abs_class(p, TRIPLE[2]) == "abs.logreg_ring"
    assert abs_class(p, TRIPLE[0]) == "abs.plugin_data"
    assert p.abs_classes[TRIPLE[2]] == "abs.other"  # the importer class stays
    v = check_equiv(p, p, None, None, con, layout=lay,
                    env_abs=(TRIPLE[2] + 0xFFFF, TRIPLE[2] + 0x20000))
    assert v.reason == "host_abs_aliases_env" and "intersects env" in v.detail
    v = check_equiv(p, p, None, None, con, layout=lay,
                    env_abs=(TRIPLE[2] + 0x10003, TRIPLE[2] + 0x20000))
    assert v.ok, v.detail


def test_synthetic_logreg_mutants():
    """V18: L1-L8 on the synthetic LOG_REG unit are all rejected as a static failure."""
    from scopt.validate_sym.mutants import synthetic_suite

    doc = synthetic_suite().to_json()
    lr = {r["name"]: r for r in doc["results"] if r["name"].startswith("logreg_")}
    assert set(lr) == {"logreg_ring_store_nop", "logreg_index_store_nop", "logreg_index_add8",
                       "logreg_spin_nop", "logreg_spin_retarget", "logreg_buf_retarget",
                       "logreg_value_field", "logreg_d1_store_hoisted",
                       "logreg_ring_aliases_env"}
    for name, r in lr.items():
        assert r["rejected"] and r["verdict"] == "mismatch", r
        assert r["reason"] == ("host_abs_aliases_env" if name == "logreg_ring_aliases_env"
                               else "edit_inside_log_block"), r
    ctl = {c["name"]: c["ok"] for c in doc["controls"] if c["name"].startswith("logreg_")}
    assert ctl == {"logreg_block_matched": True, "logreg_identity_equivalent": True,
                   "logreg_d1_after_block_observed": True,
                   "logreg_unmatched_block_is_backward_branch": True}


def test_kernel_id_covers_the_matcher():
    import inspect

    from scopt.validate_sym import selftest

    assert "logreg.py" in inspect.getsource(selftest.kernel_id)

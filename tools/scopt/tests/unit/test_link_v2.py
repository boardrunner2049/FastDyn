"""WP18: NativeLinker v2, the guard template, typed payloads, and T-B3-2 / T-B3-3.

Design §2, §3.4, §3.7, §4.1, §5.2-5.3, §9.1 (V1-V9 relaxed, V7 v6 clause, V9',
V13 structural, V15, V16, V17, V21). Real snapshots are read-only (skipped when
absent); compute sweeps are marked ``real`` and ``slow``.
"""

from __future__ import annotations

import json
import struct
from dataclasses import replace
from pathlib import Path

import pytest

from scopt.canon import fnv1a64
from scopt.formats.enums import Encoding, OpMarkKind, SiteKind
from scopt.formats.sc_records import ScRecord
from scopt.guard import (CS_CONDEXEC_MASK, CS_THUMB, NOTCACHED2, GuardError, GuardOffsets,
                         GuardTarget, decode_guard, emit_guard, guard_atoms, guard_key)
from scopt.link import (B3Plan, GuardedExitSpec, LinkError, NativeLinkerV2, VariantSpec,
                        apply_pure_rewrite, exit_class_of, free_slots, grow_unit,
                        lookup_tails, rewrite_unit, tail_problem)
from scopt.passes.base import (B3Reason, Clone, EditKind, FactRef, GuardedSlot, Patch,
                               PassDescriptor, PayloadError, SpecEntry)
from scopt.validate import V_ROWS, validate_b3, write_b3_files
from tests.synth import b3 as H

TAIL = bytes.fromhex("488bfdff1513000000ffe0")


# ---------------------------------------------------------------------------
# guard template
# ---------------------------------------------------------------------------

def _emit(targets, kf=0x10, kf2=0x400, **kw):
    return emit_guard(stub_off=112, tail=TAIL, pool_slot_off=104, lookup_helper_id=0,
                      lookup_func=0x5555_5555_0000, kf=kf, kf2=kf2, targets=targets,
                      tc_abs=0x7FFF_0000_00C0, struct_abs=0x7FFF_0000_0000,
                      tb_ret_abs=0x7FFF_0000_0018 - 0x1000, **kw)


@pytest.mark.parametrize("kf2", [0x400, 0x8000_0400])
def test_guard_emit_decode_roundtrip(kf2):
    st = _emit((GuardTarget(0x1ede, 0, "SPEC"), GuardTarget(0x2000, 1, "GENERAL")), kf2=kf2)
    reg = {s.slot_n: (s.field_off, s.reset_off) for s in st.slots}
    dg = decode_guard(st.code.rjust(112 + len(st.code), b"\x90"), 112, tc_abs=0x7FFF_0000_00C0,
                      struct_off=-0xC0, offsets=GuardOffsets(), registered=reg)
    assert [a for a, _, _ in dg.atoms] == ["g_bp", "g_thumb", "g_cond", "g_flags", "g_flags2",
                                           "g_pc", "g_pc"]
    assert (dg.kf, dg.kf2) == (0x10, kf2)
    assert dg.vaddrs == (0x1ede, 0x2000)
    assert [s.reset_kind for s in dg.slots] == ["jmp_F", "exit_tb"]
    assert [s.kind for s in dg.slots] == ["SPEC", "GENERAL"]
    assert dg.f_bytes[:5] == TAIL[:5] and dg.f_bytes[9:] == TAIL[9:]
    assert dg.pool_slot_off == 104
    assert dg.end == 112 + len(st.code)
    for s in st.slots:  # rel32 fields 4-aligned, 0 on disk, reset right after
        assert s.field_off % 4 == 0 and s.reset_off == s.field_off + 4
        assert st.code[s.field_off - 112:s.field_off - 112 + 4] == b"\0" * 4
    kinds = sorted(SiteKind(k).name for (_f, _i, _w, _e, k, _a, _v) in st.sites)
    assert kinds.count("GOTO_TB") == 2 and "HELPER_CALL" in kinds and "GOTO_PTR" in kinds
    assert len(st.fixups) == 1 and st.fixups[0][2] == "rel_minus_delta"


def test_guard_unregistered_resets_to_F():
    st = _emit((GuardTarget(0x1ede, 0, "GENERAL", registered=False),))
    dg = decode_guard(b"\x90" * 112 + st.code, 112, tc_abs=0, struct_off=-0xC0)
    assert dg.slots[0].reset_kind == "jmp_F" and not dg.slots[0].registered
    assert not any(SiteKind(k) == SiteKind.GOTO_TB for (*_x, k, _a, _v) in st.sites)


def test_guard_errors():
    with pytest.raises(GuardError) as e:
        _emit((GuardTarget(1, 0, "GENERAL"), GuardTarget(2, 1, "GENERAL"),
               GuardTarget(3, 1, "GENERAL")))
    assert e.value.reason == "slot_budget"
    with pytest.raises(GuardError) as e:
        _emit((GuardTarget(1, 0, "SPEC", registered=False),))
    assert e.value.reason == "slot_kind"
    with pytest.raises(GuardError) as e:
        emit_guard(stub_off=0, tail=b"\x90" * 11, pool_slot_off=0, lookup_helper_id=0,
                   lookup_func=0, kf=0, kf2=0, targets=(GuardTarget(1, 0, "GENERAL"),),
                   tc_abs=0, struct_abs=0, tb_ret_abs=0)
    assert e.value.reason == "exit_not_goto_ptr"
    with pytest.raises(GuardError) as e:
        guard_key(0, 0)  # THUMB clear
    assert e.value.reason == "continuation_key_mismatch"
    with pytest.raises(GuardError):
        guard_key(0, CS_THUMB | (1 << 24))  # CONDEXEC set


def test_guard_key_refuses_ss_and_masks_notcached():
    with pytest.raises(GuardError):
        guard_key(1 << 2, CS_THUMB)
    kf, kf2 = guard_key(0x10, CS_THUMB | 0x400)
    assert kf2 == 0x400 and not kf2 & NOTCACHED2  # THUMB masked (rework 2: FP bits refused)
    atoms = guard_atoms(GuardOffsets(), kf, kf2, [0x1ede])
    assert [a["atom"] for a in atoms] == ["g_bp", "g_thumb", "g_cond", "g_flags", "g_flags2",
                                         "g_pc"]


def test_decode_rejects_deviations():
    st = _emit((GuardTarget(0x1ede, 0, "GENERAL"),))
    code = bytearray(b"\x90" * 112 + st.code)
    reg = {0: (st.slots[0].field_off, st.slots[0].reset_off)}
    bad = bytearray(code)
    bad[112 + 9] = 0x84  # jne -> je on g_bp (48 83 bd d32 00 | 0f 85)
    with pytest.raises(GuardError) as e:
        decode_guard(bytes(bad), 112, tc_abs=0, struct_off=-0xC0, registered=reg)
    assert e.value.reason == "guard_contract_violation"
    with pytest.raises(GuardError):  # wrong EnvLayout displacement
        decode_guard(bytes(code), 112, tc_abs=0, struct_off=-0xC0, registered=reg,
                     offsets=replace(GuardOffsets(), thumb=0x14E))
    with pytest.raises(GuardError):  # exit_tb reset on an unregistered field
        decode_guard(bytes(code), 112, tc_abs=0, struct_off=-0xC0, registered={})


# ---------------------------------------------------------------------------
# tails, growth, rewrites (real minimal snapshot)
# ---------------------------------------------------------------------------

def _first_host(s):
    hs = H.r_hosts(s)
    assert hs
    return hs[0]


def test_lookup_tails_and_problems():
    s = H.snap("minimal")
    u = _first_host(s)
    (t,) = lookup_tails(u)
    assert u.code[t.off:t.end] == t.raw and t.raw[3:5] == b"\xff\x15"
    assert tail_problem(u, t, s.header.code_address) is None
    # a captured label into (T, T+11) -> tail_is_branch_target
    s0 = next(x for x in u.sites if x.kind in (SiteKind.LABEL_JMP, SiteKind.LABEL_JCC)) \
        if any(x.kind in (SiteKind.LABEL_JMP, SiteKind.LABEL_JCC) for x in u.sites) else None
    if s0 is not None:
        tc_abs = s.header.code_address + u.tc_offset
        bad = replace(u, sites=tuple(replace(x, value=tc_abs + t.off + 5) if x is s0 else x
                                     for x in u.sites))
        assert tail_problem(bad, t, s.header.code_address) == "tail_is_branch_target"
    assert exit_class_of(u, t, H.guest_bytes(s).get(u.uid)) in ("return", "static",
                                                                 "indirect_multiway")


def test_grow_unit_metadata():
    s = H.snap("minimal")
    u = _first_host(s)
    (t,) = lookup_tails(u)
    spec = GuardedExitSpec(t.off, "static", (GuardTarget(0x100, 0, "GENERAL"),
                                             GuardTarget(0x200, 1, "SPEC")),
                           u.key.flags & ~4, (u.key.cs_base & ~CS_CONDEXEC_MASK) | CS_THUMB)
    g = grow_unit(u, [spec], code_address=s.header.code_address,
                  tb_ret_abs=s.header.code_address + s.run_info.tb_ret_off,
                  offsets=H.offsets_for(s))
    w = g.unit
    assert w.uid == u.uid and w.stub_start == len(u.code) == u.tc_size
    assert w.code[:t.off] == u.code[:t.off] and w.code[t.end:u.tc_size] == u.code[t.end:]
    assert w.code[t.off:t.end][:1] == b"\xe9" and w.code[t.off + 5:t.end] == b"\xcc" * 6
    assert w.search == u.search and w.end_offset == w.tc_offset + w.tc_size + len(w.search)
    rec = ScRecord.from_bytes(w.record)
    assert rec.checksum_ok and rec.tc_size == w.tc_size
    for si in g.stubs[0][1].slots:
        assert rec.jmp_insn_offset[si.slot_n] == si.field_off
        assert rec.jmp_reset_offset[si.slot_n] == si.field_off + 4
    # the tail's call/goto_ptr sites moved to F, the pool row follows the call
    st = g.stubs[0][1]
    hc = [x for x in w.sites if x.kind == SiteKind.HELPER_CALL and x.aux == t.helper_id
          and x.enc == Encoding.POOL_INDIRECT]
    assert any(x.field_off == st.f_off + 5 for x in hc)
    assert not any(x.field_off == t.off + 5 for x in w.sites)
    assert any(p.user_field_off == st.f_off + 5 for p in w.pool)
    assert any(m.kind == OpMarkKind.CALL and m.host_off == st.f_off for m in w.opmarks)
    # the tail call's recovery point is gone, the others stay
    assert len(w.recovery) == len(u.recovery) - 1
    assert all(p.host_return_offset != t.off + 9 for p in w.recovery)
    # one new fixup: the GENERAL reset's jmp tb_ret
    assert len(w.reloc.fixups) == len(u.reloc.fixups) + 1
    assert w.content_hash != u.content_hash
    with pytest.raises(LinkError) as e:  # no free slot left on a grown unit
        grow_unit(w, [spec], code_address=s.header.code_address, tb_ret_abs=0)
    assert e.value.reason == "exit_not_goto_ptr"
    with pytest.raises(LinkError) as e:
        grow_unit(u, [replace(spec, tail_off=t.off + 1)], code_address=0, tb_ret_abs=0)
    assert e.value.reason == "exit_not_goto_ptr"


def test_grow_unit_no_free_slot():
    s = H.snap("minimal")
    u = next(x for x in s.units() if lookup_tails(x) and len(free_slots(x)) == 1)
    (t,) = lookup_tails(u)[:1]
    taken = 1 - free_slots(u)[0]
    spec = GuardedExitSpec(t.off, "static", (GuardTarget(0x100, taken, "GENERAL"),),
                           u.key.flags & ~4, (u.key.cs_base & ~CS_CONDEXEC_MASK) | CS_THUMB)
    with pytest.raises(LinkError) as e:
        grow_unit(u, [spec], code_address=s.header.code_address, tb_ret_abs=0)
    assert e.value.reason == "no_free_slot"


class _P:
    def __init__(self, off, old, new):
        self.host_off, self.old_bytes, self.new_bytes = off, old, new


def test_apply_pure_rewrite_rules():
    code = bytes(range(32))
    assert apply_pure_rewrite(code, [_P(4, code[4:6], b"\x90\x90")])[4:6] == b"\x90\x90"
    for p, why in ((_P(4, code[4:6], b"\x90"), "patch_length"),
                   (_P(4, b"\x00\x00", b"\x90\x90"), "patch_old_bytes"),
                   (_P(30, code[30:32] + b"\0", b"\x90" * 3), "patch_length")):
        with pytest.raises(LinkError) as e:
            apply_pure_rewrite(code, [p])
        assert e.value.reason == why
    with pytest.raises(LinkError) as e:
        apply_pure_rewrite(code, [_P(4, code[4:8], b"\x90" * 4), _P(6, code[6:8], b"\x90" * 2)])
    assert e.value.reason == "patch_overlap"
    with pytest.raises(LinkError) as e:
        apply_pure_rewrite(code, [_P(4, code[4:8], b"\x90" * 4)], forbid=[(6, 10)])
    assert e.value.reason == "edit_on_fixup"


def test_rewrite_unit_protects_tails_and_fixups():
    s = H.snap("minimal")
    u = _first_host(s)
    (t,) = lookup_tails(u)
    with pytest.raises(LinkError) as e:
        rewrite_unit(u, [_P(t.off, u.code[t.off:t.off + 3], b"\x90" * 3)])
    assert e.value.reason == "edit_on_fixup"


# ---------------------------------------------------------------------------
# NativeLinker v2 (minimal)
# ---------------------------------------------------------------------------

def test_linker_v5_unregistered_and_v6_registered():
    s = H.snap("minimal")
    specs = H.dummy_specs(s, limit=4)
    r5 = NativeLinkerV2(s, B3Plan(grow=specs, register_slots=False),
                        offsets=H.offsets_for(s), derive_captures=False).link()
    assert r5.ok and r5.version == 7 and r5.ext is None
    for uid in specs:
        rec = ScRecord.from_bytes(r5.by_uid()[uid].record)
        assert rec.jmp_insn_offset == ScRecord.from_bytes(s.unit(uid).record).jmp_insn_offset
    r6 = NativeLinkerV2(s, B3Plan(grow=specs), offsets=H.offsets_for(s),
                        derive_captures=False).link()
    assert r6.ok and r6.version == 7 and len(r6.ext.slots) == len(specs)
    assert not r6.ext.variants
    lim_end = max(ScRecord.from_bytes(u.record).end_offset for u in s.units())
    for uid in specs:
        lu = r6.by_uid()[uid]
        assert lu.new_tb_offset >= lim_end and lu.new_tb_offset % 64 == 0


def test_linker_failures():
    s = H.snap("minimal")
    hosts = H.r_hosts(s)
    cs = [u for u in s.units() if H.spec_ok(u)]
    grow, vs = H.spec_pairs(s, hosts[:1], cs[:2])
    off = H.offsets_for(s)
    # a SPEC slot without its variant -> link_missing
    r = NativeLinkerV2(s, B3Plan(grow=grow, variants=()), offsets=off,
                       derive_captures=False).link()
    assert not r.ok and r.failures[0].reason == "link_missing"
    # a variant whose predecessor slot is not registered SPEC -> variant_pred_slot
    r = NativeLinkerV2(s, B3Plan(grow=grow, variants=tuple(vs) + (VariantSpec(
        cs[3].uid, hosts[1].uid, 0),)), offsets=off, derive_captures=False).link()
    assert not r.ok and r.failures[0].reason == "variant_pred_slot"
    # budgets: three hosts whose SPEC slots all target cs[0] -> 3 variants of one C
    # (max_variants_per_c = 2); every slot is bound to cs[0]'s key, so the key
    # binding check passes and the budget is what refuses the plan
    assert len(hosts) >= 3
    grow3, vs3 = H.spec_pairs(s, hosts[:3], [cs[0]] * 3)
    assert len(vs3) == 3 and {v.src_uid for v in vs3} == {cs[0].uid}
    grow2 = {v.pred_uid: grow3[v.pred_uid] for v in vs3[:2]}
    r = NativeLinkerV2(s, B3Plan(grow=grow2, variants=tuple(vs3[:2])), offsets=off,
                       derive_captures=False).link()
    assert r.ok, r.failures  # control: two variants of one C are within budget
    r = NativeLinkerV2(s, B3Plan(grow=grow3, variants=tuple(vs3)), offsets=off,
                       derive_captures=False).link()
    assert not r.ok and r.failures[0].reason == "context_budget"
    assert "variants of one C" in r.failures[0].detail
    r = NativeLinkerV2(s, B3Plan(grow=grow3, variants=tuple(vs3), max_variants=2),
                       offsets=off, derive_captures=False).link()
    assert not r.ok and r.failures[0].reason == "context_budget"
    assert "3 variants > 2" in r.failures[0].detail
    # unregistered SPEC is impossible (v5 artifact)
    r = NativeLinkerV2(s, B3Plan(grow=grow, variants=tuple(vs), register_slots=False),
                       offsets=off, derive_captures=False).link()
    assert not r.ok and r.failures[0].reason in ("slot_kind", "variant_pred_slot")


# ---------------------------------------------------------------------------
# packaged artifacts: V-rows on minimal
# ---------------------------------------------------------------------------

def _artifact(tmp_path, s, plan, *, runtime=None, v6_exes=(), **kw):
    res = NativeLinkerV2(s, plan, offsets=H.offsets_for(s)).link()
    assert res.ok, res.failures
    d = tmp_path / "art"
    write_b3_files(d, s, res)
    v, rr = validate_b3(s, res, d, plan=plan, runtime=runtime, v6_exes=v6_exes, **kw)
    return res, v, rr, d


def _codes(v):
    return v.codes()


def test_registry_has_every_v_row():
    assert sorted(V_ROWS, key=lambda c: int(c[1:])) == [f"V{i}" for i in range(1, 25)]


def test_v6_import_rejection_reasons(tmp_path):
    """A v6 file whose extension the loader refuses is import rejection
    ``sc6_extension``; a version-6 header without the extension (EXT_TABLE
    clear) keeps B1-9's stable reason ``version`` (test_b1_9_rejections)."""
    from scopt.formats.sc_semantic import EXT_OFFSET
    from scopt.importer.import_cache import ImportRejected
    from scopt.validate import reimport

    s = H.snap("minimal")
    plan = B3Plan(grow=dict(list(H.dummy_specs(s, limit=2).items())))
    res = NativeLinkerV2(s, plan, offsets=H.offsets_for(s)).link()
    assert res.ok and res.version == 7, res.failures
    d = tmp_path / "art"
    write_b3_files(d, s, res)
    assert reimport(d, s).ext is not None  # control: the written file imports
    sc = d / "cache.sc"
    b = bytearray(sc.read_bytes())
    b[EXT_OFFSET + 56] ^= 1  # ext checksum
    sc.write_bytes(bytes(b))
    with pytest.raises(ImportRejected) as e:
        reimport(d, s)
    assert e.value.reason == "cache_extension" and "ext_checksum" in e.value.detail


def test_minimal_spec_and_general_artifact(tmp_path, monkeypatch):
    s = H.snap("minimal")
    hosts = H.r_hosts(s)
    cs = [u for u in s.units() if H.spec_ok(u) and u not in hosts[:2]]
    grow, vs = H.spec_pairs(s, hosts[:2], cs)
    gen = {k: v for k, v in H.dummy_specs(s, limit=8).items() if k not in grow}
    grow.update(dict(list(gen.items())[:3]))
    plan = B3Plan(grow=grow, variants=tuple(vs))
    ri6 = H.v6_identity(monkeypatch)
    res, v, rr, d = _artifact(tmp_path, s, plan, runtime=ri6, v6_exes=[H.QINT_V6_SHA])
    c = _codes(v)
    for k in ("V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8", "V11", "V13", "V15", "V16",
              "V17", "V19", "V21", "V23"):
        assert c[k] == "pass", (k, [r for r in v.rows if r["code"] == k and r["result"] != "pass"])
    assert c["V20"] == "pass" or c["V20"] == "skip"
    for k in ("V12", "V14", "V18", "V22"):
        assert c[k] == "skip"
    assert len(rr.variants) == len(vs) and all(x["ok"] for x in rr.variants)
    # V7: the same artifact with the qint (v5-only) identity is refused
    v2, _ = validate_b3(s, res, d, plan=plan, runtime=H.v2_identity(), skip_reimport=True)
    row = next(r for r in v2.rows if r["check"] == "v6_capable_runtime")
    assert row["result"] == "fail" and row["reason"] == "v6_runtime_required"


def test_v9_identity_v5_and_identity_v6(tmp_path):
    s = H.snap("minimal")
    _res, v, _rr, _d = _artifact(tmp_path / "a", s, B3Plan())
    row = next(r for r in v.rows if r["code"] == "V9")
    assert row["check"] == "identity_sha256" and row["result"] == "pass", row
    _res, v6, _rr, _d = _artifact(tmp_path / "b", s, B3Plan(force_semantic_table=True))
    row = next(r for r in v6.rows if r["code"] == "V9")
    assert row["check"] == "identity_v6" and row["result"] == "pass", row


def test_v13_rejects_a_tampered_stub(tmp_path):
    s = H.snap("minimal")
    plan = B3Plan(grow=H.dummy_specs(s, limit=1))
    res = NativeLinkerV2(s, plan, offsets=H.offsets_for(s)).link()
    uid = next(iter(plan.grow))
    st = res.grown[uid].stubs[0][1]
    lu = res.by_uid()[uid]
    body = bytearray(lu.body)
    i = st.code.index(b"\x81\xbd" + struct.pack("<i", 0x150))  # g_flags compare
    body[st.off + i + 6] ^= 0x01  # change Kf
    units = tuple(replace(x, body=bytes(body)) if x.uid == uid else x for x in res.units)
    ext = res.ext
    sl = list(ext.slots)
    sl[0] = replace(sl[0], guard_fnv64=fnv1a64(bytes(body[st.off:st.off + st.guard_len])))
    res2 = replace(res, units=units, ext=replace(ext, slots=tuple(sl)), derived_scms=())
    d = tmp_path / "t"
    write_b3_files(d, s, res2)
    v, _ = validate_b3(s, res2, d, plan=plan, skip_reimport=True)
    row = next(r for r in v.rows if r["code"] == "V13" and r["check"] == "guard_template")
    assert row["result"] == "fail" and row["reason"] == "guard_contract_violation"
    assert "Kf" in row["detail"]


def _nop_patch(c, where: str, s=None):
    """A whole-instruction NOP patch of ``c``: inside an insn body (``body``), in the
    prefix (``prefix``) or in the pool (``pool``)."""
    from scopt.analysis.hostops import OpKind, build_hostops

    s = s or H.snap("minimal")
    h = build_hostops(c, H.layout_for(s), s.header.code_address)
    if where == "pool":
        o = c.pool_start
        return Patch("x", o, c.code[o:o + 8], b"\x90" * 8, "dho_nop")
    for op in h.ops:
        inside = c.insn0_start <= op.off and op.end <= c.body_end
        if where == "prefix" and op.end <= c.insn0_start and op.size >= 2:
            return Patch("x", op.off, c.code[op.off:op.end], b"\x90" * op.size, "dho_nop")
        if where == "body" and inside and op.kind in (OpKind.COPY, OpKind.CONST) and \
                op.block == "insn":
            return Patch("x", op.off, c.code[op.off:op.end], b"\x90" * op.size, "dho_nop")
    return None


@pytest.mark.parametrize("where,ok,reason", [("body", True, None),
                                             ("prefix", False, "edit_in_prefix"),
                                             ("pool", False, "edit_in_exit_stub")])
def test_v15_placement_on_variant_patches(tmp_path, where, ok, reason):
    s = H.snap("minimal")
    hosts = H.r_hosts(s)
    cs = [u for u in s.units() if H.spec_ok(u) and u not in hosts[:1]]
    c = next(x for x in cs if _nop_patch(x, where) is not None)
    grow, vs = H.spec_pairs(s, hosts[:1], [c])
    p = _nop_patch(c, where)
    vs = [replace(vs[0], patches=(p,))]
    plan = B3Plan(grow=grow, variants=tuple(vs))
    res = NativeLinkerV2(s, plan, offsets=H.offsets_for(s)).link()
    assert res.ok, res.failures
    d = tmp_path / "p"
    write_b3_files(d, s, res)
    v, _ = validate_b3(s, res, d, plan=plan, skip_reimport=True)
    row = next(r for r in v.rows if r["code"] == "V15")
    assert (row["result"] == "pass") == ok, row
    if reason:
        assert reason in row["detail"]
    v16 = next(r for r in v.rows if r["code"] == "V16")
    assert v16["result"] == "pass", v16  # bytes == relocate(C) + declared patches


def test_v16_detects_an_undeclared_variant_byte(tmp_path):
    s = H.snap("minimal")
    hosts = H.r_hosts(s)
    cs = [u for u in s.units() if H.spec_ok(u) and u not in hosts[:1]]
    grow, vs = H.spec_pairs(s, hosts[:1], cs[:1])
    plan = B3Plan(grow=grow, variants=tuple(vs))
    res = NativeLinkerV2(s, plan, offsets=H.offsets_for(s)).link()
    vl = res.variants[0]
    body = bytearray(vl.body)
    body[1] ^= 0x10
    vl2 = replace(vl, body=bytes(body))
    ext = res.ext
    ve = replace(ext.variants[0], code_fnv64=fnv1a64(bytes(body)))
    res2 = replace(res, variants=(vl2,), ext=replace(ext, variants=(ve,)), derived_scms=())
    d = tmp_path / "v"
    write_b3_files(d, s, res2)
    v, _ = validate_b3(s, res2, d, plan=plan, skip_reimport=True)
    assert v.codes()["V16"] == "fail"


# ---------------------------------------------------------------------------
# typed payloads (design §3.7) and the builder
# ---------------------------------------------------------------------------

SHA = "sha256:" + "ab" * 32


def test_payload_validation():
    from scopt.ids import TBKey

    k = TBKey.from_raw(0x9001ede, -1 & (2 ** 64 - 1), 0x10, CS_THUMB, 0x80000)
    g = GuardedSlot("tb:" + "0" * 32 + "#0", "x1", 0, "SPEC", "return", 0x1ede, k,
                    variant_ref=SHA)
    assert g.to_json()["target_vaddr"] == "0x1ede"
    for bad in (dict(slot_n=2), dict(kind="BOTH"), dict(target_vaddr=0x1edf),
                dict(variant_ref=None)):
        with pytest.raises(PayloadError):
            replace(g, **bad)
    f = FactRef(SHA, "r0", 0, 1, "guest_code", SHA, SHA)
    with pytest.raises(PayloadError):
        replace(f, field="pc")
    Clone("tb:" + "0" * 32 + "#0", SHA, (f,))
    with pytest.raises(PayloadError):
        SpecEntry(SHA, ("u", 2))
    with pytest.raises(PayloadError):
        Patch("x", 0, b"\x90", b"\x90\x90", "dho_nop")
    with pytest.raises(PayloadError):
        Patch("x", 0, b"\x90", b"\x90", "no_such_template")
    assert B3Reason.outcome("validation_mismatch").value == "failed"
    assert B3Reason.outcome("validation_inconclusive").value == "skipped_unknown"
    assert B3Reason.outcome("no_free_slot").value == "skipped_unsupported"
    assert {"guard_layout_unbound", "unsupported_guest", "tail_is_branch_target"} <= \
        B3Reason.all()


def test_builder_typed_b3_edits():
    from scopt.proposal import CapabilityNotEnabled, EditPreconditionError, ProposalBuilder

    s = H.snap("minimal")

    class View:
        def unit(self, uid):
            return s.unit(uid)

        def content_hash(self, uid):
            return s.unit(uid).content_hash

    u = s.units()[0]
    d = PassDescriptor("dead-host-ops", 1, capabilities=frozenset(
        {EditKind.PURE_REWRITE, EditKind.CLONE, EditKind.ADD_ENTRY, EditKind.ADD_RECOVERY}))
    b = ProposalBuilder(d, View(), enabled=frozenset(EditKind))
    e = b.pure_rewrite(Patch(u.uid, 0, u.code[:1], b"\x90", "dho_nop"))
    assert e.payload.template_id == "dho_nop" and e.args["payload"]["host_off"] == 0
    with pytest.raises(EditPreconditionError):  # template of another pass
        b.pure_rewrite(Patch(u.uid, 0, u.code[:1], b"\x90", "bf_jcc_to_jmp"))
    with pytest.raises(EditPreconditionError):  # unknown variant target
        b.pure_rewrite(Patch(SHA, 0, b"\x90", b"\x90", "dho_nop"))
    b.clone(Clone(u.uid, SHA))
    b.pure_rewrite(Patch(SHA, 0, b"\x90", b"\x90", "dho_nop"))
    b.add_entry(SpecEntry(SHA, (u.uid, 0)))
    with pytest.raises(EditPreconditionError):
        b.add_entry(SpecEntry("sha256:" + "cd" * 32, (u.uid, 0)))
    with pytest.raises(CapabilityNotEnabled):
        b.add_recovery()
    with pytest.raises(EditPreconditionError):
        b.clone(SpecEntry(SHA, (u.uid, 0)))  # wrong payload type


# ---------------------------------------------------------------------------
# T-B3-2 / T-B3-3 on compute
# ---------------------------------------------------------------------------

def _must_pass(v, codes):
    c = v.codes()
    bad = {k: [r["detail"][:300] for r in v.rows if r["code"] == k and r["result"] == "fail"]
           for k in codes if c.get(k) != "pass"}
    assert not bad, bad


@pytest.mark.real
@pytest.mark.slow
@pytest.mark.parametrize("register", [True, False], ids=["semantic", "public"])
def test_t_b3_2_grow_every_return_static_unit(tmp_path, monkeypatch, register):
    """T-B3-2: grow every relocatable compute return/static unit with a dummy
    guard; V1-V5, V13 (structural), V15, V16, V17 pass; the derived .scm
    re-imports (V8)."""
    s = H.snap("compute")
    specs = H.dummy_specs(s)
    assert len(specs) >= 500
    plan = B3Plan(grow=specs, register_slots=register)
    ri6 = H.v6_identity(monkeypatch)
    res, v, rr, _d = _artifact(tmp_path, s, plan, runtime=ri6 if register else None,
                               v6_exes=[H.QINT_V6_SHA])
    assert res.version == 7
    assert (res.ext is not None) == register
    _must_pass(v, ["V1", "V2", "V3", "V4", "V5", "V6", "V8", "V13", "V15", "V16", "V17",
                   "V19", "V23"] + (["V7", "V11"] if register else []))
    assert all(u.stub_start is not None for u in rr.snapshot.units() if u.uid in specs)


@pytest.mark.real
@pytest.mark.slow
def test_t_b3_3_clone_relocatable_units(tmp_path, monkeypatch):
    """T-B3-3 (one batch in pytest; the full sweep is runs/wp18/run_acceptance.py):
    clone relocatable compute units as variants behind SPEC slots; V16 holds."""
    s = H.snap("compute")
    hosts = H.r_hosts(s)
    cs = [u for u in s.units() if H.spec_ok(u) and u not in hosts]
    grow, vs = H.spec_pairs(s, hosts[:60], cs)
    plan = B3Plan(grow=grow, variants=tuple(vs), max_variants=len(vs))
    res, v, rr, _d = _artifact(tmp_path, s, plan, runtime=H.v6_identity(monkeypatch),
                               v6_exes=[H.QINT_V6_SHA])
    assert len(res.variants) == len(vs) >= 100
    _must_pass(v, ["V1", "V2", "V3", "V4", "V5", "V7", "V8", "V11", "V13", "V15", "V16",
                   "V17", "V19"])


def test_derived_capture_v2_and_variant_chunks():
    """Grown units get TB chunk v2 (stub_start); each variant one VARIANT chunk
    before END; both decode and dump."""
    from scopt.formats import scm_v1 as S

    s = H.snap("minimal")
    hosts = H.r_hosts(s)
    cs = [u for u in s.units() if H.spec_ok(u) and u not in hosts[:1]]
    grow, vs = H.spec_pairs(s, hosts[:1], cs)
    res = NativeLinkerV2(s, B3Plan(grow=grow, variants=tuple(vs)),
                         offsets=H.offsets_for(s)).link()
    name, data = res.derived_scms[0]
    f = S.parse_scm(data)
    assert not f.rejected_chunks and f.end is not None
    assert len(f.variants) == len(vs)
    assert f.chunks[-1].type == int(S.ChunkType.END)
    v2 = [tb for tb in f.tbs.values() if tb.tb_chunk_version == 2]
    assert len(v2) == len(grow) and all(tb.stub_start is not None for tb in v2)
    assert all(tb.stub_start == tb.pool_end < tb.code_size for tb in v2)
    for ch in f.chunks:
        S.chunk_to_json(ch)
    vc = f.variants[0]
    assert S.VariantChunk.decode(vc.encode())[0] == vc
    assert vc.variant_key.hex() == res.variants[0].key.split(":")[1]


def test_v19_checks_cited_facts_against_callee_summaries(tmp_path):
    """V19: a variant citing a FactRef is checked against the WP20 CalleeSummary rows
    (current -> pass; the row changed -> stale_fact)."""
    from scopt.formats.callee_summary_v1 import factref, seal_row
    from tests.unit.test_wp20_return_edges import _row

    s = H.snap("minimal")
    hosts = H.r_hosts(s)
    cs = [u for u in s.units() if H.spec_ok(u) and u not in hosts[:1]]
    grow, vs = H.spec_pairs(s, hosts[:1], cs[:1])
    row = _row(facts=[{**_row()["facts"][0], "cert_hash": "sha256:" + "4" * 64,
                       "status": "certified"}])
    ref = factref(row, row["facts"][0])
    f = FactRef(ref["fact_id"], ref["field"], int(ref["value"], 16), ref["def_insn"],
                ref["source"], ref["cert_hash"], ref["summary_row_hash"])
    plan = B3Plan(grow=grow, variants=(replace(vs[0], facts=(f,)),))
    res = NativeLinkerV2(s, plan, offsets=H.offsets_for(s)).link()
    assert res.ok and res.ext.variants[0].flags == 1  # facts_present
    d = tmp_path / "f"
    write_b3_files(d, s, res)
    v, _ = validate_b3(s, res, d, plan=plan, summaries=[row])
    r19 = next(r for r in v.rows if r["check"] == "callee_summaries")
    assert r19["result"] == "pass", r19
    stale = seal_row({**row, "return_insn": "pop {pc}"})
    v2, _ = validate_b3(s, res, d, plan=plan, summaries=[stale])
    r19 = next(r for r in v2.rows if r["check"] == "callee_summaries")
    assert r19["result"] == "fail" and r19["reason"] == "stale_fact", r19


# ---------------------------------------------------------------------------
# review fixes: SPEC key binding, SS_ACTIVE, tb_ret, must-store convergence,
# fail-closed packaging
# ---------------------------------------------------------------------------

def test_guard_key_refuses_ss_active():
    with pytest.raises(GuardError, match="SS_ACTIVE"):
        guard_key(1 << 1, CS_THUMB)  # TBFLAG_ANY SS_ACTIVE (target/arm/cpu.h:2957)
    with pytest.raises(GuardError, match="PSTATE__SS"):
        guard_key(1 << 2, CS_THUMB)


def _spec_case(s):
    hosts = H.r_hosts(s)
    cs = [u for u in s.units() if H.spec_ok(u) and u not in hosts[:2]]
    grow, vs = H.spec_pairs(s, hosts[:1], cs)
    uid = next(iter(grow))
    return uid, grow[uid][0], vs


def _key_mutations(spec):
    t0 = spec.targets[0]
    out = {"cs_base_handler": replace(spec, key_cs_base=spec.key_cs_base ^ 1),
           "flags_be_data": replace(spec, key_flags=spec.key_flags ^ (1 << 3)),
           "pc_plus_page": replace(spec, targets=(replace(t0, vaddr=t0.vaddr + 0x400),)
                                   + spec.targets[1:])}
    if len(spec.targets) == 2:
        t1 = spec.targets[1]
        out["swapped"] = replace(spec, targets=(replace(t0, vaddr=t1.vaddr),
                                                replace(t1, vaddr=t0.vaddr)))
    return out


def test_linker_binds_spec_slots_to_c_key():
    s = H.snap("minimal")
    uid, spec, vs = _spec_case(s)
    base = NativeLinkerV2(s, B3Plan(grow={uid: (spec,)}, variants=tuple(vs)),
                          offsets=H.offsets_for(s)).link()
    assert base.ok, base.failures
    for name, m in _key_mutations(spec).items():
        res = NativeLinkerV2(s, B3Plan(grow={uid: (m,)}, variants=tuple(vs)),
                             offsets=H.offsets_for(s)).link()
        assert not res.ok, name
        assert res.failures[0].reason == "continuation_key_mismatch", (name, res.failures)


@pytest.mark.parametrize("mut", ["cs_base_handler", "flags_be_data", "pc_plus_page"])
def test_v13_spec_key_binding_from_bytes(tmp_path, monkeypatch, mut):
    """With the linker's check disabled (a manager/linker bug), V13 still fails the
    artifact from the written bytes and ext table alone."""
    import scopt.link as L

    s = H.snap("minimal")
    uid, spec, vs = _spec_case(s)
    m = _key_mutations(spec)[mut]
    monkeypatch.setattr(L, "spec_binding_problem", lambda *a, **k: None)
    plan = B3Plan(grow={uid: (m,)}, variants=tuple(vs))
    res = NativeLinkerV2(s, plan, offsets=H.offsets_for(s)).link()
    assert res.ok, res.failures
    d = tmp_path / mut
    write_b3_files(d, s, res)
    ri6 = H.v6_identity(monkeypatch)
    v, _ = validate_b3(s, res, d, plan=plan, runtime=ri6, v6_exes=[H.QINT_V6_SHA],
                       skip_reimport=True)
    row = next(r for r in v.rows if r["code"] == "V13" and r["check"] == "spec_key_binding")
    assert row["result"] == "fail" and row["reason"] == "guard_contract_violation", row
    assert not v.accepted


def test_v13_spec_key_binding_passes_on_valid(tmp_path, monkeypatch):
    s = H.snap("minimal")
    uid, spec, vs = _spec_case(s)
    plan = B3Plan(grow={uid: (spec,)}, variants=tuple(vs))
    res = NativeLinkerV2(s, plan, offsets=H.offsets_for(s)).link()
    d = tmp_path / "ok"
    write_b3_files(d, s, res)
    v, _ = validate_b3(s, res, d, plan=plan, runtime=H.v6_identity(monkeypatch),
                       v6_exes=[H.QINT_V6_SHA], skip_reimport=True)
    row = next(r for r in v.rows if r["code"] == "V13" and r["check"] == "spec_key_binding")
    assert row["result"] == "pass", row


def test_v13_general_reset_targets_tb_ret(tmp_path):
    s = H.snap("minimal")
    plan = B3Plan(grow=H.dummy_specs(s, limit=1))
    from scopt.link import tb_ret_abs_of

    res = NativeLinkerV2(s, plan, offsets=H.offsets_for(s),
                         tb_ret_abs=tb_ret_abs_of(s) + 0x40).link()
    assert res.ok, res.failures
    d = tmp_path / "r"
    write_b3_files(d, s, res)
    v, _ = validate_b3(s, res, d, plan=plan, skip_reimport=True)
    row = next(r for r in v.rows if r["code"] == "V13" and r["check"] == "guard_template")
    assert row["result"] == "fail" and "tb_ret_addr" in row["detail"], row


def test_must_stores_fails_closed_without_convergence():
    from scopt.validate import _must_stores

    s = H.snap("minimal")
    lay = H.layout_for(s)
    ca = s.header.code_address
    u, t = next((u, lookup_tails(u)[0]) for u in H.r_hosts(s)
                if _must_stores(u, lay, ca, ["regs[15]"], lookup_tails(u)[0].off)[0])
    ok, det = _must_stores(u, lay, ca, ["regs[15]"], t.off, max_rounds=1)
    assert not ok and "did not converge" in det
    # discrimination: a field that is never stored before the tail
    ok2, det2 = _must_stores(u, lay, ca, ["regs[15]", "regs[3]"], t.off)
    assert not ok2 and "regs[3]" in det2


def test_package_b3_refuses_uncertified_and_maps_ext_codes(tmp_path, monkeypatch):
    import scopt.link as L
    from scopt.validate import package_b3

    s = H.snap("minimal")
    uid, spec, vs = _spec_case(s)
    plan = B3Plan(grow={uid: (spec,)}, variants=tuple(vs))
    ri6 = H.v6_identity(monkeypatch)
    r = package_b3(s, plan, tmp_path / "s1", candidate_id="sha256:" + "31" * 32,
                   runtime=ri6, v6_exes=[H.QINT_V6_SHA])
    assert r.status == "rejected" and r.reason == "certification_not_run", r.reason
    r2 = package_b3(s, plan, tmp_path / "s2", candidate_id="sha256:" + "32" * 32,
                    runtime=ri6, v6_exes=[H.QINT_V6_SHA], allow_uncertified=True)
    assert r2.status == "published", r2.reason
    import json
    from pathlib import Path

    side = json.loads((Path(r2.artifact_dir) / "cache.sc.scopt.json").read_text())
    # the grown R' has guarded (SPEC) slots: V13's entailment part is certifying too
    assert side["x_uncertified"] == ["V12", "V13", "V14", "V18"]
    # a table the loader would refuse is rejected with its ext_problem code
    sw = replace(spec, targets=(replace(spec.targets[0], vaddr=spec.targets[1].vaddr),
                                replace(spec.targets[1], vaddr=spec.targets[0].vaddr)))
    monkeypatch.setattr(L, "spec_binding_problem", lambda *a, **k: None)
    r3 = package_b3(s, B3Plan(grow={uid: (sw,)}, variants=tuple(vs)), tmp_path / "s3",
                    candidate_id="sha256:" + "33" * 32, runtime=ri6,
                    v6_exes=[H.QINT_V6_SHA], allow_uncertified=True)
    assert r3.status == "rejected" and r3.reason == "failed:link_page_offset", r3.reason


def test_v20_fails_closed_without_runtime_identity(tmp_path):
    s = H.snap("minimal")
    plan = B3Plan(grow=H.dummy_specs(s, limit=1))
    res = NativeLinkerV2(s, plan, offsets=H.offsets_for(s)).link()
    d = tmp_path / "n"
    write_b3_files(d, s, res)
    s2 = replace(s, runtime=None) if hasattr(s, "__dataclass_fields__") else None
    if s2 is None:
        pytest.skip("snapshot type is not a dataclass")
    v, _ = validate_b3(s2, res, d, plan=plan, skip_reimport=True)
    row = next(r for r in v.rows if r["code"] == "V20")
    assert row["result"] == "fail", row


# ---------------------------------------------------------------------------
# rework 2: review minors
# ---------------------------------------------------------------------------

def _selftest_doc():
    import capstone

    from scopt.validate_sym.selftest import kernel_id

    return {"kernel_id": kernel_id(), "capstone_version": capstone.__version__,
            "native": {"status": "pass", "forms": 1, "trials": 1, "n_failures": 0},
            "mutants": {"all_rejected": True, "controls_ok": True, "rejected": 1,
                        "mutants": 1}}


class _L:  # a link stand-in for uncertified_rows: rewritten units only (B4 shape)
    grown: dict = {}
    ext = None
    variants: tuple = ()

    def __init__(self, *uids):
        self.rewritten = {u: () for u in uids}


def test_uncertified_rows_against_wp19_rows():
    """CERTIFYING_ROWS / uncertified_rows re-checked against WP19's row bodies:
    stubs, V12 "no changed unit" skip, coverage of the link's changed units,
    V14 "no SPEC link" skip, V18 missing."""
    from scopt.validate import (Validation, stub_rows, uncertified_rows, v12_rows, v14_rows,
                                v18_rows)
    from scopt.validate_sym.equiv import FactCert, Verdict

    v = Validation()
    stub_rows(v, ["V12", "V14", "V18"], "not_run: x")
    assert uncertified_rows(v, _L("u")) == ["V12", "V14", "V18"]
    assert uncertified_rows(v) == ["V12", "V13", "V14", "V18"]  # link unknown
    eq = Verdict("equivalent", None, "ok", 1, 1)
    # all certified: the one changed unit U':u has its verdict
    v = Validation()
    v12_rows(v, [("U':u", eq)])
    v14_rows(v, [])
    v18_rows(v, _selftest_doc())
    assert v.accepted and uncertified_rows(v, _L("u")) == []
    # a second changed unit without a verdict: V12 is not certified
    assert uncertified_rows(v, _L("u", "w")) == ["V12"]
    # verdicts for units the link does not change never count
    v = Validation()
    v12_rows(v, [("U':other", eq), ("u", eq), ("R':u", eq)])
    v14_rows(v, [])
    v18_rows(v, _selftest_doc())
    assert uncertified_rows(v, _L("u")) == ["V12"]
    # V12 vacuous skip never certifies an edited plan
    v = Validation()
    v12_rows(v, [])
    v14_rows(v, [])
    v18_rows(v, _selftest_doc())
    assert [r["result"] for r in v.rows if r["code"] == "V12"] == ["skip"]
    assert uncertified_rows(v, _L("u")) == ["V12"]
    assert uncertified_rows(v) == ["V12", "V13", "V14"]  # link unknown: fail closed
    # a V14 fact for a label the link does not have is not required (and harmless)
    v = Validation()
    v12_rows(v, [("U':u", eq)])
    v14_rows(v, [{"label": "l", "cert": FactCert((0, 4), "certified", 7, 1), "proposed": 7,
                  "provenance": {"row": 1}}])
    v18_rows(v, _selftest_doc())
    assert v.accepted and uncertified_rows(v, _L("u")) == []
    # V18 without a document fails (hostsym_selftest_missing) and is uncertified
    v = Validation()
    v18_rows(v, None)
    assert not v.accepted and v.fails()[0]["reason"] == "hostsym_selftest_missing"
    assert "V18" in uncertified_rows(v, _L("u"))


def _full_hostsym(link, *, facts=True, drop=(), extra=()):
    """Synthetic verdicts covering every label ``link`` requires (minus ``drop``)."""
    from scopt.validate_sym.equiv import FactCert, Verdict
    from scopt.validate_sym.link_check import required_hostsym

    req = required_hostsym(link)
    eq = Verdict("equivalent", None, "synthetic", 1, 1)
    hs = {"equiv": [(lab, eq) for lab in req.equiv if lab not in drop] +
          [(lab, eq) for lab in extra],
          "entailment": [(lab, eq) for lab in req.entailment if lab not in drop],
          "facts": [{"label": lab, "cert": FactCert((0, 4), "certified", 0, 1),
                     "proposed": 0, "provenance": {"synthetic": True}}
                    for lab in req.facts if facts and lab not in drop],
          "selftest": _selftest_doc()}
    return hs


def test_package_b3_with_hostsym_rows_and_refusals(tmp_path, monkeypatch):
    """validate_b3 runs the WP19 bodies when given ``hostsym`` inputs, and then
    package_b3 publishes without allow_uncertified (synthetic verdicts: this
    tests the wiring, not the kernel) — only with a verdict for EVERY changed unit
    (R' and both variants C'), the R' guard entailment and every SPEC-link fact.
    skip_reimport is refused."""
    from scopt.validate import package_b3
    from scopt.validate_sym.equiv import Verdict

    s = H.snap("minimal")
    uid, spec, vs = _spec_case(s)
    plan = B3Plan(grow={uid: (spec,)}, variants=tuple(vs))
    ri6 = H.v6_identity(monkeypatch)
    link = NativeLinkerV2(s, plan, offsets=H.offsets_for(s)).link()
    hs = _full_hostsym(link)
    labels = [lab for lab, _ in hs["equiv"]]
    assert labels == [f"R':{uid}"] + [f"C':{v.src_uid}@v{i}<-{uid}|{v.slot_n}"
                                      for i, v in enumerate(vs)]
    assert [lab for lab, _ in hs["entailment"]] == [uid]
    r = package_b3(s, plan, tmp_path / "s1", candidate_id="sha256:" + "41" * 32,
                   runtime=ri6, v6_exes=[H.QINT_V6_SHA], hostsym=hs)
    assert r.status == "published", r.reason
    c = r.validation.codes()
    assert c["V12"] == c["V13"] == c["V18"] == "pass"
    assert c["V14"] in ("pass", "skip")  # the spec_pairs variants declare no fact
    side = json.loads((Path(r.artifact_dir) / "cache.sc.scopt.json").read_text())
    assert "x_uncertified" not in side
    # a hostsym mismatch rejects with its failed code
    bad = dict(hs, equiv=[(lab, Verdict("mismatch", "validation_mismatch", "x", 1, 1))
                          if lab == f"R':{uid}" else (lab, v) for lab, v in hs["equiv"]])
    r2 = package_b3(s, plan, tmp_path / "s2", candidate_id="sha256:" + "42" * 32,
                    runtime=ri6, v6_exes=[H.QINT_V6_SHA], hostsym=bad)
    assert r2.status == "rejected" and r2.reason == "V12:validation_mismatch", r2.reason
    with pytest.raises(ValueError, match="skip_reimport"):
        package_b3(s, plan, tmp_path / "s3", candidate_id="sha256:" + "43" * 32,
                   runtime=ri6, v6_exes=[H.QINT_V6_SHA], allow_uncertified=True,
                   skip_reimport=True)


def test_guard_key_refuses_m_profile_fp_bits():
    from scopt.guard import CS_M_FP_BITS

    ok = guard_key(0x110, CS_THUMB | 0x400)
    assert ok == (0x110, 0x400)
    for bit in range(2, 6):
        assert CS_M_FP_BITS & (1 << bit)
        with pytest.raises(GuardError) as e:
            guard_key(0x110, CS_THUMB | 0x400 | (1 << bit))
        assert e.value.reason == "continuation_key_mismatch" and "FP/MVE" in e.value.detail


def test_spec_binding_vc_exact():
    """A Vc with bit 0 set is not C's pc (exact compare, linker and V13)."""
    from scopt.link import spec_binding_problem

    s = H.snap("minimal")
    uid, spec, vs = _spec_case(s)
    c = s.unit(vs[0].src_uid)
    assert spec_binding_problem(spec, vs[0].slot_n, c) is None
    t0 = spec.targets[0]
    odd = replace(spec, targets=(replace(t0, vaddr=t0.vaddr | 1),) + spec.targets[1:])
    p = spec_binding_problem(odd, t0.slot_n, c)
    assert p is not None and "not a virtual address of C" in p


def test_v7_loader_requirement_wrong_sidecar_value(tmp_path):
    s = H.snap("minimal")
    plan = B3Plan(grow=H.dummy_specs(s, limit=1))
    res = NativeLinkerV2(s, plan, offsets=H.offsets_for(s)).link()
    assert res.ok and res.version == 7
    d = tmp_path / "a"
    write_b3_files(d, s, res)
    for lr, want in (("qemu-sc-v5-unmodified", "fail"), (None, "fail"), ("qemu-sc-v6", "pass")):
        v, _ = validate_b3(s, res, d, plan=plan, sidecar={"loader_requirement": lr},
                           skip_reimport=True)
        row = next(r for r in v.rows if r["check"] == "loader_requirement")
        assert row["result"] == want, (lr, row)
    v, _ = validate_b3(s, res, d, plan=plan, skip_reimport=True)
    row = next(r for r in v.rows if r["check"] == "loader_requirement")
    assert row["result"] == "skip"


def test_multi_tb_region_is_failed_outcome():
    from scopt.passes.base import Outcome

    assert B3Reason.outcome(B3Reason.MULTI_TB_REGION) is Outcome.FAILED


@pytest.mark.parametrize("kf2", [0x400, 0x8000_0400])
def test_compact_single_guard_retains_all_failure_paths(kf2):
    from scopt.formats.enums import Encoding
    from scopt.guard import COMPACT_GUARD

    target = GuardTarget(0x1ede, 0, "GENERAL")
    original = _emit((target,), kf2=kf2)
    short = _emit((replace(target, compact=True),), kf2=kf2)
    assert short.template_id == COMPACT_GUARD and len(short.code) < len(original.code)
    slot, = short.slots
    reg = {slot.slot_n: (slot.field_off, slot.reset_off)}
    code = bytes(short.off) + short.code
    decoded = decode_guard(code, short.off, tc_abs=0x7FFF_0000_00C0,
                           struct_off=-0xc0, registered=reg, offsets=short.offsets)
    assert decoded.f_off == short.f_off and decoded.vaddrs == (target.vaddr,)
    assert decoded.slots[0].reset_kind == "exit_tb"
    checks = [s for s in short.sites if s[3] == Encoding.REL8]
    assert len(checks) == 6
    # V13 must reject each inverted condition and each failure accepting any slot.
    for field_off, insn_off, width, *_ in checks:
        assert code[insn_off] == 0x75 and width == 1
        for where, new_byte in [(insn_off, 0x74), (field_off, slot.jmp_off-field_off-1)]:
            corrupted = bytearray(code)
            corrupted[where] = new_byte & 255
            with pytest.raises(GuardError):
                decode_guard(corrupted, short.off, tc_abs=0x7FFF_0000_00C0,
                             struct_off=-0xc0, registered=reg, offsets=short.offsets)
    with pytest.raises(GuardError):
        decode_guard(code, short.off, tc_abs=0x7FFF_0000_00C0,
                     struct_off=-0xc0, registered={}, offsets=short.offsets)


def test_reversed_general_comparisons_preserve_slot_identity_and_fallback():
    targets = (GuardTarget(0x1800, 0, "GENERAL"), GuardTarget(0x1c00, 1, "GENERAL"))
    regular = _emit(targets)
    reverse = _emit(tuple(replace(t, reverse_comparisons=True) for t in targets))
    regs = {s.slot_n: (s.field_off, s.reset_off) for s in regular.slots}
    assert regs == {s.slot_n: (s.field_off, s.reset_off) for s in reverse.slots}
    for stub, order in ((regular, targets), (reverse, targets[::-1])):
        guard = decode_guard(bytes(112) + stub.code, 112, tc_abs=0x7fff_0000_00c0,
                             struct_off=-0xc0, offsets=GuardOffsets(), registered=regs)
        assert [v for atom, _, v in guard.atoms if atom == "g_pc"] == [t.vaddr for t in order]
        assert [(s.vaddr, s.slot_n) for s in guard.slots] == [(t.vaddr, t.slot_n) for t in targets]
    first = min(s.label_off for s in regular.slots) - 112
    assert regular.code[first:] == reverse.code[first:]
    with pytest.raises(GuardError):
        _emit((replace(targets[0], reverse_comparisons=True),))
    with pytest.raises(GuardError):
        _emit((replace(targets[0], reverse_comparisons=True),
               replace(targets[1], kind="SPEC", reverse_comparisons=True)))

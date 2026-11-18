"""WP20: return edges, exit facts, patch join, CalleeSummary, U-COND (design §3.1-3.3, T-B3-42).

Synthetic tests build HostOps / guest bytes by hand; the ``real`` tests use the
B2 compute and the WP15A minimal store snapshots (read-only, skipped when absent).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from scopt.analysis.exit_facts import compare_with_certifier, propose_exit_facts
from scopt.analysis.hcfg import build_hcfg
from scopt.analysis.hostops import EnvAccess, HostOp, OpKind, UnitHostOps
from scopt.analysis.patch_join import (EMPTY_SHA256, PatchPlan, PatchPlanError, insn_vaddrs,
                                       join_def, join_fact, parse_modifier)
from scopt.analysis.return_edges import (CONDEXEC_SHIFT, THUMB_BIT, continuation_key_ok,
                                         ucond_elf_scan)
from scopt.decode.guest_symbols import GuestFunc, GuestImage
from scopt.decode.thumb_sem import (ItState, classify_last, decode_at, decode_one, imm_defs,
                                    thumb_expand_imm)
from scopt.formats.callee_summary_v1 import (CalleeSummaryError, check_factref, factref,
                                             read_callee_summaries, seal_row, validate_row,
                                             write_callee_summaries)
from scopt.ids import TBKey
from scopt.passes.base import StaleFact
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()

# ---------------------------------------------------------------------------
# Thumb semantics
# ---------------------------------------------------------------------------

T = {
    "bx lr": b"\x70\x47", "pop {r4,pc}": b"\x10\xbd", "pop.w": b"\xbd\xe8\xf0\x81",
    "ldr pc,[sp],#4": b"\x5d\xf8\x04\xfb", "ldm.w r0,{r1,pc}": b"\x90\xe8\x02\x80",
    "ldr.w pc,[r1]": b"\xd1\xf8\x00\xf0", "tbb": b"\xdf\xe8\x03\xf0", "bx r3": b"\x18\x47",
    "blx r3": b"\x98\x47", "bl": b"\x00\xf0\x00\xf8", "b": b"\x00\xe0", "beq": b"\x00\xd0",
    "movs r0,#1": b"\x01\x20", "movs r0,#0": b"\x00\x20", "adds r0,#1": b"\x01\x30",
    "str r3,[r2]": b"\x13\x60", "nop": b"\x00\xbf", "ldmia sp!,{r4,pc}": b"\xbd\xe8\x10\x80",
}


def one(name: str, words1: int | None = None):
    g = decode_at(T[name], 0x1000, words1=None if words1 is None else [words1])
    assert g is not None and len(g) == 1
    return g[0]


@pytest.mark.parametrize("name,cls", [
    ("bx lr", ("return", "bx_lr")), ("pop {r4,pc}", ("return", "pop_pc")),
    ("pop.w", ("return", "pop_pc")), ("ldr pc,[sp],#4", ("return", "ldr_pc_sp")),
    ("ldmia sp!,{r4,pc}", ("return", "pop_pc")),
    ("ldm.w r0,{r1,pc}", ("indirect_multiway", "ldm_rX_pc")),
    ("ldr.w pc,[r1]", ("indirect_multiway", "ldr_pc_rX")),
    ("tbb", ("indirect_multiway", "tbb")), ("bx r3", ("indirect_multiway", "bx_reg")),
    ("blx r3", ("indirect_multiway", "blx_reg")), ("bl", ("static", "bl")),
    ("b", ("static", "b")), ("beq", ("static", "beq")), ("nop", ("fallthrough", "nop")),
])
def test_classify_last(name, cls):
    assert classify_last(one(name)) == cls


def test_imm_defs_forms():
    assert imm_defs(one("movs r0,#1")) == {"r0": 1, "NF": 1, "ZF": 1}
    # inside an IT block (words[1] low nibble != 0) the T1 encoding is MOV<c>: no flags,
    # and with a non-AL condition it is conditional -> not an immediate def at all
    assert imm_defs(one("movs r0,#1", words1=0xE8)) == {"r0": 1}  # cond AL (0xe) in IT
    assert imm_defs(one("movs r0,#1", words1=0x08)) is None  # cond EQ
    assert imm_defs(one("adds r0,#1")) is None
    # mov.w r1,#0x100 (T2, S=0), movs.w r1,#0xff000000 (rotated, S=1: CF written)
    g = decode_at(b"\x4f\xf4\x80\x71", 0x1000)[0]
    assert imm_defs(g) == {"r1": 0x100}
    g = decode_at(b"\x5f\xf0\x7f\x41", 0x1000)[0]
    assert imm_defs(g) == {"r1": 0xff000000, "NF": 0xff000000, "ZF": 0xff000000, "CF": None}
    g = decode_at(b"\x6f\xf0\x00\x02", 0x1000)[0]  # mvn r2,#0
    assert imm_defs(g) == {"r2": 0xffffffff}
    g = decode_at(b"\x41\xf2\x34\x23", 0x1000)[0]  # movw r3,#0x1234
    assert imm_defs(g) == {"r3": 0x1234}
    assert thumb_expand_imm(0x1ff) == (0x00ff00ff, False)


def test_decode_at_checks_lengths():
    code = T["movs r0,#1"] + T["str r3,[r2]"] + T["bx lr"]
    assert [g.text for g in decode_at(code, 0x1eb4, lengths=[2, 2, 2])] == \
        ["movs r0, #1", "str r3, [r2]", "bx lr"]
    assert decode_at(code, 0x1eb4, lengths=[2, 4]) is None


def test_itstate_matches_qemu():
    it = decode_one(b"\x08\xbf", 0x1000)  # it eq
    assert it.is_it and it.it_cond_mask == 0x08
    st = ItState(0)
    st.step(it)
    assert st.bits == 0x08 and st.mask  # one conditional insn pending (cond eq)
    st.step(one("bx lr"))
    assert st.bits == 0 and st.mask == 0  # IT block ended at the return
    st = ItState(0)
    st.step(decode_one(b"\x04\xbf", 0x1000))  # itt eq
    st.step(one("bx lr"))
    assert st.mask != 0  # a return that is not last in its IT block


def _img(code: bytes, base: int = 0x1000) -> GuestImage:
    return GuestImage("synthetic", "sha256:" + "0" * 64, ((base, code),),
                      ((base, base + len(code)),), (GuestFunc("f", base, base + len(code)),),
                      ((base, "t"),))


def test_ucond_elf_scan_synthetic():
    good = b"\x08\xbf" + T["bx lr"] + T["movs r0,#1"] + T["bx lr"]  # it eq; bxeq lr; ...
    r = ucond_elf_scan(_img(good))
    assert r["pass"] and r["return_insns"] == 2 and r["in_it_block"] == 1
    bad = b"\x04\xbf" + T["bx lr"] + T["movs r0,#1"] + T["bx lr"]  # itt eq; bxeq lr; moveq
    r = ucond_elf_scan(_img(bad))
    assert not r["pass"] and r["counterexamples"][0]["vaddr"] == "0x1002"


def test_guest_image_func_at():
    img = _img(T["movs r0,#1"] + T["bx lr"])
    assert img.func_at(0x1002).name == "f" and img.func_at(0x1004) is None
    assert img.read(0x1000, 4) == T["movs r0,#1"] + T["bx lr"] and img.read(0x1002, 4) is None


def test_continuation_key():
    r = TBKey(0x1c00, None, 0x110, 0x800400, 0xff022000)
    assert continuation_key_ok(r, TBKey(0x2400, None, 0x110, 0x800400, 0xff022000))
    assert not continuation_key_ok(r, TBKey(0x2400, None, 0x111, 0x800400, 0xff022000))
    assert not continuation_key_ok(r, TBKey(0x2400, None, 0x110, 0x800400, 0xff022001))
    assert not continuation_key_ok(r, TBKey(0x2400, None, 0x110, 0x400, 0xff022000))  # thumb 0
    it = 0x800400 | (0x18 << CONDEXEC_SHIFT)
    assert not continuation_key_ok(r, TBKey(0x2400, None, 0x110, it, 0xff022000))
    # R itself may start inside an IT block; only C's condexec must be 0
    assert continuation_key_ok(TBKey(0x1c00, None, 0x110, it, 0xff022000),
                               TBKey(0x2400, None, 0x110, THUMB_BIT | 0x400, 0xff022000))


# ---------------------------------------------------------------------------
# exit-facts@1 on hand-made HostOps
# ---------------------------------------------------------------------------

class _B:
    """Tiny HostOps builder: ops are laid out back to back from offset 0x16."""

    def __init__(self):
        self.ops: list[HostOp] = []
        self.off = 0x16

    def add(self, kind, size=4, text="op", insn=0, **kw) -> int:
        self.ops.append(HostOp(off=self.off, size=size, text=text, kind=kind, block="insn",
                               insn=insn, **kw))
        self.off += size
        return self.ops[-1].off

    def st_imm(self, field, imm, insn=0, width=4):
        d = {"regs[0]": 0, "regs[1]": 4, "NF": 0x218, "ZF": 0x21c}[field]
        return self.add(OpKind.ENV_STORE_IMM, 7, f"mov dword ptr [rbp + {d}], {imm}", insn,
                        env=(EnvAccess(field, d, width, "w", width == 4),), imm=imm)

    def st_reg(self, field, reg, insn=0):
        return self.add(OpKind.ENV_STORE_REG, 4, f"mov [rbp], {reg}", insn,
                        env=(EnvAccess(field, 0, 4, "w", True),), src_reg=reg)

    def const(self, reg, imm):
        return self.add(OpKind.CONST, 5, f"mov {reg}, {imm}", dst_reg=reg, imm=imm,
                        regs_written=frozenset({"rax" if reg == "eax" else reg}))

    def call(self, name, flags, hid=7):
        return self.add(OpKind.CALL, 5, "call x", helper_id=hid, helper_name=name,
                        helper_flags=flags)

    def tail(self, insn=0):
        self.add(OpKind.COPY, 3, "mov rdi, rbp", insn, dst_reg="rdi", src_reg="rbp",
                 regs_written=frozenset({"rdi"}))
        self.add(OpKind.CALL, 6, "call qword ptr [rip + 0x13]", insn, helper_id=0,
                 helper_name="lookup_tb_ptr", helper_flags=6)
        self.ops.append(HostOp(off=self.off, size=2, text="jmp rax", kind=OpKind.GOTO_PTR,
                               block="exit_stub", insn=insn, exit="goto_ptr:0"))
        self.off += 2

    def build(self) -> UnitHostOps:
        return UnitHostOps("tb:" + "a" * 32 + "#0", "ok", (), tuple(self.ops), (), 0x16,
                           self.off, self.off, None, self.off, 1, 0xff022000, None)


def _facts(b: _B, effects=None):
    f = propose_exit_facts(b.build(), effects=effects)
    assert f.status == "ok" and len(f.exits) == 1
    return f.exits[0]


def test_fact_constant_store_and_flags():
    b = _B()
    b.st_imm("NF", 0)
    b.st_imm("ZF", 0)
    b.st_imm("regs[0]", 0)
    b.tail(1)
    e = _facts(b)
    assert [(f.field, f.value) for f in e.facts] == [("r0", 0), ("NF", 0), ("ZF", 0)]
    assert e.fact("r0").defs == ((0x16 + 14, 0),)
    assert not e.refused and not e.barriers and e.tail.length == 11


def test_fact_from_tracked_register():
    b = _B()
    b.const("eax", 0x1_0000_0003)  # 32-bit write zero-extends
    b.st_reg("regs[0]", "eax")
    b.tail()
    assert _facts(b).fact("r0").value == 3


def _gate_effects():
    from scopt.analysis.helper_effects import effects_for_layout
    from scopt.formats.envlayout_v1 import find_envlayout

    return effects_for_layout(find_envlayout(
        "sha256:56d8bfb32c91c970fe0a16237b962c02791ce0a7fcc52bf6246343fbef430d90"))


@pytest.mark.parametrize("name,flags,table,killed", [
    ("v_virtual", 0, True, True),  # FastDyn virtual: flags-0 call
    (None, 1, True, True),  # plugin callback: killed whatever its flags
    ("udiv", 1, True, False),  # NO_RWG helper with a row that writes no global
    ("rebuild_hflags_m32", 1, True, False),  # NO_RWG, writes hflags only
    ("udiv", 1, False, True),  # no helper effect table bound: fail closed
    ("helper_nowg", 2, True, True),  # NO_WG helper without a row: fail closed
    ("lookup_tb_ptr", 6, True, False),  # NO_WG with a row (all env but the globals)
])
def test_fact_killed_by_helper(name, flags, table, killed):
    b = _B()
    b.st_imm("regs[0]", 1)
    b.call(name, flags)
    b.tail()
    e = _facts(b, _gate_effects() if table else None)
    if killed:
        assert e.fact("r0") is None
        assert [(r.field, r.reason) for r in e.refused] == [("r0", "fact_killed_by_helper")]
        assert e.barriers[0].kind == ("plugin_cb" if name is None else "call_global")
    else:
        assert e.fact("r0").value == 1 and not e.barriers


def test_fact_store_after_call_survives():
    b = _B()
    b.call("v_virtual", 0)
    b.st_imm("regs[0]", 5)
    b.tail()
    e = _facts(b)
    assert e.fact("r0").value == 5 and len(e.barriers) == 1


def test_fact_not_constant_unknown_register():
    b = _B()
    b.st_reg("regs[0]", "ebx")
    b.tail()
    e = _facts(b)
    assert e.fact("r0") is None and e.refused[0].reason == "fact_not_constant"


def test_fact_partial_store_is_not_constant():
    b = _B()
    b.st_imm("regs[0]", 1, width=1)
    b.tail()
    assert _facts(b).refused[0].reason == "fact_not_constant"


def _diamond(v_then: int | None, v_else: int | None) -> UnitHostOps:
    b = _B()
    b.add(OpKind.FLAGS, 2, "test ebx, ebx")
    jcc = len(b.ops)
    b.add(OpKind.JCC, 2, "je L")
    if v_then is not None:
        b.st_imm("regs[0]", v_then)
    jmp = len(b.ops)
    b.add(OpKind.JMP, 2, "jmp J")
    lab = b.off
    if v_else is not None:
        b.st_imm("regs[0]", v_else)
    join = b.off
    b.tail()
    ops = list(b.ops)
    ops[jcc] = HostOp(**{**_asdict(ops[jcc]), "target": lab})
    ops[jmp] = HostOp(**{**_asdict(ops[jmp]), "target": join})
    b.ops = ops
    return b.build()


def _asdict(o: HostOp) -> dict:
    return {s: getattr(o, s) for s in HostOp.__slots__}


def test_fact_meet_over_paths():
    h = _diamond(7, 7)
    assert build_hcfg(h).ok
    e = propose_exit_facts(h).exits[0]
    assert e.fact("r0").value == 7 and len(e.fact("r0").defs) == 2
    e = propose_exit_facts(_diamond(7, 8)).exits[0]
    assert e.fact("r0") is None and e.refused[0].reason == "fact_not_constant"
    e = propose_exit_facts(_diamond(7, None)).exits[0]
    assert e.refused[0].reason == "fact_not_local_to_return_tb"


def test_tail_is_branch_target_and_shape():
    b = _B()
    b.st_imm("regs[0]", 0)
    b.tail()
    h = b.build()
    ops = list(h.ops)
    ops[0] = HostOp(**{**_asdict(ops[0]), "kind": OpKind.JCC, "target": ops[-2].off,
                       "env": (), "text": "je T+3"})
    from dataclasses import replace

    f = propose_exit_facts(replace(h, ops=tuple(ops)))
    assert f.exits == ()  # the tail is refused (tail_is_branch_target)
    from scopt.analysis.exit_facts import find_lookup_tails

    assert [r for _, _, r in find_lookup_tails(replace(h, ops=tuple(ops)))] == \
        ["tail_is_branch_target"]


def test_certifier_comparison():
    b = _B()
    b.st_imm("regs[0]", 0)
    b.tail()
    e = _facts(b)
    assert compare_with_certifier(e, lambda f, v: True)[0]["certified"]
    assert compare_with_certifier(e, lambda f, v: False)[0]["reason"] == \
        "fact_derivation_mismatch"
    assert compare_with_certifier(e, lambda f, v: None)[0]["reason"] == \
        "validation_inconclusive"


def test_pinned_hostops_gives_no_facts():
    from dataclasses import replace

    b = _B()
    b.tail()
    f = propose_exit_facts(replace(b.build(), status="pinned", reasons=("x",)))
    assert f.status == "unavailable" and f.exits == ()


# ---------------------------------------------------------------------------
# patch-join@1
# ---------------------------------------------------------------------------

def _manifest(mod_sha: str | None, virt_sha: str | None = None, fuzz=None) -> dict:
    cf = {"modifiers": None if mod_sha is None else {"sha256": mod_sha, "size": 1},
          "virtuals": None if virt_sha is None else {"sha256": virt_sha, "size": 1},
          "fuzzing_schema": fuzz, "device_config": {"sha256": "0" * 64, "size": 2}}
    return {"version": 1, "inputs": {"content_files": cf}}


def _plan(tmp_path, mods: str, *, fuzz=None) -> PatchPlan:
    p = tmp_path / "modifiers.txt"
    p.write_text(mods)
    sha = hashlib.sha256(mods.encode()).hexdigest()
    return PatchPlan.load(_manifest(sha, fuzz=fuzz), modifiers=p)


def test_parse_modifier_forms():
    e = parse_modifier("0x10002 r0 <- 0", 0)
    assert (e.vaddr, e.form, e.field, e.imm) == (0x10002, "reg_imm", "r0", 0)
    assert parse_modifier("0x2a04 r3 <- 0xff", 0).imm == 0xff
    assert parse_modifier("0x10 r1 := r2", 0).form == "reg_reg"
    assert parse_modifier("0x10 [r1] <- 0x42", 0).form == "deref"
    assert parse_modifier("0x10 r15 <- 0x2000", 0).form == "pc"
    assert parse_modifier("0x10 0x20000000 <- 1", 0).form == "mem"


def test_plan_load_checks_content(tmp_path):
    plan = _plan(tmp_path, "0x10002 r0 <- 0\n")
    assert plan.modifiers_status == "verified" and len(plan.at(0x10002)) == 1
    p = tmp_path / "other.txt"
    p.write_text("0x10002 r0 <- 1\n")
    with pytest.raises(PatchPlanError) as ei:
        PatchPlan.load(_manifest(hashlib.sha256(b"x").hexdigest()), modifiers=p)
    assert ei.value.code == "patch_plan_content_mismatch"
    assert PatchPlan.load(_manifest(EMPTY_SHA256)).modifiers_status == "empty"
    assert PatchPlan.load(_manifest("1" * 64)).modifiers_status == "unavailable"
    with pytest.raises(PatchPlanError) as ei:
        PatchPlan.load(_manifest(EMPTY_SHA256), bundle={"patch_plan_hash": "sha256:" + "0" * 64})
    assert ei.value.code == "patch_plan_hash_mismatch"


def _g(*names, base=0x10000):
    code = b"".join(T[n] for n in names)
    return decode_at(code, base), [base + sum(len(T[x]) for x in names[:i])
                                   for i in range(len(names))]


def test_join_guest_code_and_patch(tmp_path):
    empty = PatchPlan.load(_manifest(EMPTY_SHA256))
    gi, vs = _g("movs r0,#0", "bx lr")
    p = join_def("r0", 0, 1, ginsns=gi, vaddrs=vs, plan=empty)
    assert (p.source, p.reason, p.writer_insn) == ("guest_code", None, 0)
    p = join_def("r0", 1, 1, ginsns=gi, vaddrs=vs, plan=empty)
    assert p.reason == "patch_join_mismatch"
    # F-B3-1 shape: adds r0,r0,#1; bx lr with modifier r0 <- 0 at the bx lr
    gi, vs = _g("adds r0,#1", "bx lr")
    plan = _plan(tmp_path, f"0x{vs[1]:x} r0 <- 0\n")
    p = join_def("r0", 0, 1, ginsns=gi, vaddrs=vs, plan=plan)
    assert (p.source, p.reason) == ("patch", None) and p.plan_entry.text.endswith("r0 <- 0")
    assert join_def("r0", 5, 1, ginsns=gi, vaddrs=vs, plan=plan).reason == "patch_join_mismatch"
    # a guest write after the modifier (same vaddr: the modifier runs first) wins
    gi2, vs2 = _g("movs r0,#1", "bx lr")
    plan2 = _plan(tmp_path, f"0x{vs2[0]:x} r0 <- 0\n")
    p = join_def("r0", 1, 1, ginsns=gi2, vaddrs=vs2, plan=plan2)
    assert p.source == "guest_code"
    # computed writer, no writer
    assert join_def("r0", 1, 0, ginsns=gi, vaddrs=vs, plan=empty).reason == \
        "provenance_unexplained"
    gi3, vs3 = _g("nop", "bx lr")
    assert join_def("r0", 1, 1, ginsns=gi3, vaddrs=vs3, plan=empty).reason == \
        "provenance_unexplained"


def test_join_fail_closed_conditions(tmp_path):
    gi, vs = _g("adds r0,#1", "bx lr")
    deref = _plan(tmp_path, f"0x{vs[1]:x} [r0] <- 0\n")
    assert join_def("r0", 0, 1, ginsns=gi, vaddrs=vs, plan=deref).reason == \
        "unsupported_contract_fact_source"
    unk = PatchPlan.load(_manifest("1" * 64))
    p = join_def("r0", 0, 1, ginsns=gi, vaddrs=vs, plan=unk)
    assert p.reason == "provenance_unexplained" and p.detail == "patch_plan_unavailable"
    empty = PatchPlan.load(_manifest(EMPTY_SHA256))
    assert join_def("r0", 0, 1, ginsns=None, vaddrs=vs, plan=empty).detail == \
        "guest_bytes_unavailable"
    gi0, vs0 = _g("movs r0,#0", "bx lr")
    b = {"image_hash": "sha256:" + "1" * 64, "original_image_hash": "sha256:" + "2" * 64}
    p, per = join_fact("r0", 0, [(42, 0)], ginsns=gi0, vaddrs=vs0, plan=empty, bundle=b)
    assert p.reason == "no_patch_bindings" and per[0].reason == "no_patch_bindings"
    fz = _plan(tmp_path, "", fuzz={"sha256": "3" * 64, "size": 1})
    assert join_fact("r0", 0, [(42, 0)], ginsns=gi0, vaddrs=vs0, plan=fz)[0].reason == \
        "unsupported_contract_fact_source"


# ---------------------------------------------------------------------------
# CalleeSummary v1
# ---------------------------------------------------------------------------

def _row(**over) -> dict:
    row = {
        "schema": "fastdyn.callee_summary", "version": 1, "snapshot_id": "sha256:" + "1" * 64,
        "return_unit": "tb:" + "b" * 32 + "#0", "exit_id": "x0", "exit_kind": "goto_ptr",
        "return_insn_vaddr": "0x1ee0", "return_insn": "bx lr", "admitted": True,
        "exit_reasons": [], "callee": {"name": "prvOpIs", "start": "0x1ebe", "end": "0x1ee2",
                                       "source": "elf_symtab"},
        "patches_scope": "callee", "patches": [],
        "facts": [{"fact_id": "sha256:" + "2" * 64, "field": "r0", "value": "0x0",
                   "def_host_off": 42, "def_insn": 0, "defs": [{"host_off": 42, "insn": 0}],
                   "source": "guest_code", "cert_hash": None, "status": "proposed"}],
        "refused": [], "effects": [{"kind": "exit", "host_off": 85, "insn": 1,
                                    "exit": "goto_ptr"}],
        "barriers": [],
        "deps": {"unit": {"uid": "tb:" + "b" * 32 + "#0", "content_hash": "sha256:" + "3" * 64,
                          "guest_hash": "0x1", "key": {"page_addr0": "0x1c00",
                                                       "page_addr1": None, "flags": "0x110",
                                                       "cs_base": "0x800400",
                                                       "cflags": "0xff022000"}},
                 "patch_plan_hash": None, "instrumentation_hash": None, "contract_id": None,
                 "helpers": [{"id": 0, "name": "lookup_tb_ptr", "flags": 6}],
                 "envlayout_id": None}}
    row.update(over)
    return seal_row(row)


def test_callee_summary_roundtrip_and_staleness(tmp_path):
    r = _row()
    res = write_callee_summaries(tmp_path / "summaries/callee_summaries.jsonl", [r])
    assert res["rows"] == 1 and res["facts"] == 1
    back = read_callee_summaries(tmp_path / "summaries/callee_summaries.jsonl")
    assert back == [r]
    ref = factref(r, r["facts"][0])
    assert set(ref) == {"fact_id", "field", "value", "def_insn", "source", "cert_hash",
                        "summary_row_hash"}
    assert check_factref(ref, back)["field"] == "r0"
    r2 = _row(facts=[{**r["facts"][0], "value": "0x1"}])  # unit/plan changed -> new row hash
    with pytest.raises(StaleFact):
        check_factref(ref, [r2])
    bad = dict(r)
    bad["admitted"] = False
    with pytest.raises(CalleeSummaryError) as ei:
        validate_row(bad)
    assert ei.value.code == "callee_summary_row_hash"
    with pytest.raises(CalleeSummaryError):
        _row(facts=[{**r["facts"][0], "field": "pc"}])


# ---------------------------------------------------------------------------
# real snapshots
# ---------------------------------------------------------------------------

SNAPS = {
    "compute": ROOT / "runs/b2/compute/store/snapshots/"
    "53b4132fc05526ff2925cd3acc610bf613e8b11e2315e15e520bff7fe080dab2",
    "minimal": ROOT / "runs/wp15a/minimal/store/snapshots/"
    "f1ed14d98db4bcae54a1c37f07b1d6fabcfcea345a56edf1df1846c4d0aaee44",
}
PREP = {"compute": ROOT / "runs/b2/compute/prep", "minimal": ROOT / "runs/wp15a/minimal/prep"}
PROFILE = {"compute": ROOT / "runs/b2/compute/store/profiles/"
           "d09e70a617896b1504d425d79947e95fc26ffdae7180d8cb34a661b26a65be27/profile.json"}
_CTX: dict[str, object] = {}


def ctx(name: str):
    p = SNAPS[name]
    if not (p / "snapshot.json").is_file():
        pytest.skip(f"{p} not present")
    if name not in _CTX:
        from scopt.commands.b3_edges import Ctx

        prep = PREP[name] if (PREP[name] / "work/virtuals").is_dir() else None
        prof = PROFILE.get(name)
        _CTX[name] = Ctx(str(p), profile=str(prof) if prof and prof.is_file() else None,
                         elf=None, prep=str(prep) if prep else None, modifiers=None,
                         virtuals=None)
    return _CTX[name]


def _units_at(c, vaddr):
    return [u for u in c.s.units() if u.vaddrs and u.vaddrs[0] == vaddr]


@pytest.mark.real
@pytest.mark.slow
def test_compute_prvopis_natural_edge():
    """WP20 acceptance (unit): the natural prvOpIs edge (0x1ede) proposed with source=guest_code."""
    c = ctx("compute")
    assert c.image is not None and c.plan.modifiers_status == "empty"
    (u,) = [u for u in _units_at(c, 0x1ede) if u.key.cflags == 0xff022000]
    e = c.edges()
    (x,) = [x for x in e.exits_of(u.uid) if x.exit_kind == "goto_ptr"]
    assert x.admitted and x.exit_class == "return" and x.exit_vaddr == 0x1ee0
    assert x.candidates and x.candidates[0].vaddr == 0x24f0 and x.candidates[0].admitted
    rows = [r for r in c.summaries() if r["return_unit"] == u.uid]
    assert len(rows) == 1 and rows[0]["callee"]["name"] == "prvOpIs"
    f = {x["field"]: x for x in rows[0]["facts"]}
    assert f["r0"]["value"] == "0x0" and f["r0"]["source"] == "guest_code"
    assert f["r0"]["writer_vaddr"] == "0x1ede"


@pytest.mark.real
@pytest.mark.slow
def test_compute_suffix_units_of_0x1eb8():
    """WP20 acceptance (unit): every suffix unit of 0x1eb8 lacking movs r0,#1 is refused."""
    c = ctx("compute")
    e = c.edges()
    rows = [x for x in e.exits if x.exit_vaddr == 0x1eb8 and x.exit_class == "return"]
    full = [x for x in rows if c.s.unit(x.uid).vaddrs[0] <= 0x1eb4]
    suffix = [x for x in rows if c.s.unit(x.uid).vaddrs[0] > 0x1eb4]
    assert full and suffix
    sums = {r["return_unit"]: r for r in c.summaries()}
    for x in full:
        f = {y["field"]: y for y in sums[x.uid]["facts"]}
        assert f["r0"]["value"] == "0x1" and f["r0"]["source"] == "guest_code"
    for x in suffix:
        refused = {(r["field"], r["reason"]) for r in sums[x.uid]["refused"]}
        assert ("r0", "fact_not_local_to_return_tb") in refused
        assert not any(y["field"] == "r0" for y in sums[x.uid]["facts"])


@pytest.mark.real
@pytest.mark.slow
def test_compute_ucond_and_admission_census():
    c = ctx("compute")
    e = c.edges()
    assert e.ucond and all(r.ok for r in e.ucond)
    assert all(r.decode_replay == "match" for r in e.ucond)
    st = e.stats
    assert st["exits:goto_ptr"] == 575 and st["guest_undecoded_units"] == 0
    # every goto_ptr exit with a captured static off-page destination is class static
    assert all(x.exit_class == "static" for x in e.exits if x.static_dest is not None)
    # handler-mode returns leave through exit_tb(NULL): never admitted
    assert not any(x.admitted for x in e.exits if x.exit_kind == "exit_null")


@pytest.mark.real
@pytest.mark.slow
def test_minimal_patch_source_fact():
    """A real modifier (minimal: ``0x800397e r0 <- 3``, before ``bl``) gives source=patch."""
    from scopt.decode.guest_symbols import unit_guest_bytes
    from scopt.decode.thumb_sem import decode_unit

    c = ctx("minimal")
    if c.plan.modifiers_status != "verified":
        pytest.skip("minimal prep modifiers.txt not present")
    e = c.edges()
    hits = []
    for u in c.s.units():
        vs = insn_vaddrs(u) or []
        if 0x800397e not in vs:
            continue
        gi = decode_unit(u, unit_guest_bytes(u, c.image))
        for es in c.facts(u).exits:
            fr = es.fact("r0")
            assert fr is not None and fr.value == 3
            p, _ = join_fact("r0", fr.value, fr.defs, ginsns=gi, vaddrs=vs, plan=c.plan,
                             bundle=c.s.bundle)
            hits.append((u.uid, p))
        (x,) = [x for x in e.exits_of(u.uid) if x.exit_kind == "goto_ptr"]
        assert x.exit_class == "static" and x.static_dest == 0x80068b0
    assert hits
    for _uid, p in hits:
        assert p.source == "patch" and p.plan_entry.text == "0x800397e r0 <- 3"


@pytest.mark.real
@pytest.mark.slow
def test_analysis_store_wiring_minimal():
    """The analyses run through the AnalysisStore (registry, declared inputs)."""
    p = SNAPS["minimal"]
    if not (p / "snapshot.json").is_file():
        pytest.skip("minimal snapshot not present")
    from scopt.analysis.api import ScopeRef
    from scopt.analysis.store import AnalysisStore
    from scopt.importer.import_cache import open_snapshot

    s = open_snapshot(p)
    st = AnalysisStore(s, bundle_id=s.bundle["bundle_id"])
    sid = ScopeRef("snapshot", s.snapshot_id)
    e = st.get("return-edges", sid).value
    assert e.exits and e.snapshot_id == s.snapshot_id
    rows = st.get("callee-summary", sid).value
    assert len(rows) == len(e.return_units())
    for r in rows:
        validate_row(r)
    u = next(x for x in e.exits if x.exit_kind == "goto_ptr")
    pj = st.get("patch-join", ScopeRef("unit", u.uid)).value
    assert pj.uid == u.uid
    assert json.dumps(e.to_json())  # serialisable


def test_edge_stability_synthetic():
    from scopt.analysis.return_edges import Candidate, ExitRow, ReturnEdges, edge_stability

    def edges(counts):
        cands = tuple(Candidate(d, 0x2000 + i, n, "exact", 0.0, n, i + 1, ())
                      for i, (d, n) in enumerate(counts))
        x = ExitRow("tb:" + "c" * 32 + "#0", "x0", "goto_ptr", 0, 0x35, 0x2c, "return",
                    "bx_lr", 0x1000, "bx lr", None, (0, 1), (), cands)
        return ReturnEdges("sha256:" + "0" * 64, "p", "steady", 1.0, None, {}, (x,), (), ())

    train = edges([("a", 90), ("b", 9), ("c", 1)])
    held = edges([("b", 50), ("a", 30), ("d", 20)])
    r = edge_stability(train, held, k=2)["classes"]["return"]
    assert r["traffic"] == 100 and r["covered_topk"] == 0.8 and r["seen_in_train"] == 0.8
    assert r["top1_agree"] == 0.0
    assert edge_stability(train, train, k=1)["classes"]["return"]["top1_agree"] == 1.0


# ---------------------------------------------------------------------------
# proposer vs WP19 certifier (hostsym V14) on a real snapshot
# ---------------------------------------------------------------------------

@pytest.mark.real
@pytest.mark.slow
def test_minimal_summaries_certified_by_hostsym():
    """Every proposed fact of the minimal snapshot is certified by WP19's V14 certifier
    (``fact_derivation_mismatch`` = 0), and a deliberately wrong value is refused as
    ``fact_derivation_mismatch`` (the agreement is not vacuous)."""
    import dataclasses

    c = ctx("minimal")
    cert = c.certifier()
    rows = c.summaries(certify=cert)
    facts = [f for r in rows for f in r["facts"]]
    assert facts and cert.tally == {"certified": len(facts)}
    for r in rows:
        validate_row(r)
        for f in r["facts"]:
            assert f["status"] == "certified" and f["cert_hash"].startswith("sha256:")

    inner = c.certifier()

    def wrong(u, es, f):  # the certifier must disagree with value + 1
        return inner(u, es, dataclasses.replace(f, value=(f.value + 1) & 0xFFFFFFFF))
    bad = c.summaries(certify=wrong)
    assert not any(r["facts"] for r in bad)
    mism = [x for r in bad for x in r["refused"] if x["reason"] == "fact_derivation_mismatch"]
    assert len(mism) == len(facts) and inner.tally == {"fact_derivation_mismatch": len(facts)}


@pytest.mark.real
@pytest.mark.slow
def test_cli_summaries_certify_minimal(tmp_path):
    from click.testing import CliRunner

    from scopt.cli import cli, load_commands

    load_commands()

    p = SNAPS["minimal"]
    if not (p / "snapshot.json").is_file():
        pytest.skip("minimal snapshot not present")
    args = ["return-edges", "summaries", str(p), "--certify", "--out-dir", str(tmp_path)]
    if (PREP["minimal"] / "work/virtuals").is_dir():
        args += ["--prep", str(PREP["minimal"])]
    r = CliRunner().invoke(cli, args)
    assert r.exit_code == 0, r.output
    doc = json.loads(r.output)
    assert doc["facts"] > 0 and doc["certifier"] == {"certified": doc["facts"]}
    assert doc["status"] == {"certified": doc["facts"]}
    assert (tmp_path / "summaries/callee_summaries.jsonl").is_file()

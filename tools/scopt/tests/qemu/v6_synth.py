#!/usr/bin/env python3
"""Hand-made v7 semantic-extension files for the WP22 loader unit tests (design §5.2-5.4, §4.1).

This is a **test generator**, not the WP18 writer (``scopt/formats/sc_semantic.py``):
it builds the semantic table directly inside a copy of a public cache.
The independent table encoding exercises the runtime loader.

What it can build from an imported snapshot (``open_snapshot``):

* **identity-semantic**: the public v7 file with the semantic marker and ``EXT_TABLE`` and an extension table with zero entries;
* **guarded links**: for an R unit whose static exit leaves the page through
  ``lookup_and_goto_ptr`` (``StaticExit.slot == 0xFF``) and a C unit at its
  destination, R is moved (NativeLinker v1 ``relocate_unit``) past ``max_end``
  and grown: the 11-byte lookup tail becomes ``jmp S; int3 x6`` and the stub
  ``S`` of §4.1 (g_bp, g_thumb, g_cond, g_flags, g_flags2, g_pc, the slot
  jump, its reset path and the original tail ``F``) is appended after the pool
  (rows follow it). Kind ``spec`` adds a variant ``C'``: a relocated byte clone
  of C (no specialization: it must behave exactly like C) plus a LinkEntry;
  kind ``general`` adds a GENERAL slot whose reset path is ``exit_tb(R|n)``;
* **mutants** for the fail-loud tests: every mutation names the loader reason
  it must trigger (``MUTATIONS``).

Env offsets (G21) were re-read on the qint-v6 binary with ``gdb -nx`` (WP22
unit evidence); they are parameters here (``EnvOffsets``).

CLI::

    v6_synth.py --snapshot SNAPDIR --base-sc BASE.sc --out OUT.sc \
        [--identity] [--spec R_VADDR] [--general R_VADDR] [--mutate NAME]
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WT))
from scopt.canon import fnv1a64  # noqa: E402
from scopt.formats.sc_records import (RECORD_SIZE, SC_CODE_OFFSET, ScHeader, ScRecord,  # noqa: E402
                                 read_sc, sparse_copy)
from scopt.formats.layouts import SC_VERSION
from scopt.formats.sc_semantic import SC7_SEMANTIC_MARKER
from scopt.link import relocate_unit  # noqa: E402

EXT_OFFSET = 0x100
ARRAYS_OFFSET = 0x140
EXT_MAGIC = b"QSCX6EXT"
FEAT_EXT_TABLE, FEAT_VARIANTS, FEAT_GUARDED_SLOTS, FEAT_REWRITTEN = 1, 2, 4, 8
KIND_GENERAL, KIND_SPEC = 1, 2
EXIT_RETURN, EXIT_STATIC = 1, 2
JMP_INVALID = 0xFFFF
TB_STRUCT = 192  # ROUND_UP(sizeof(TranslationBlock) = 152, 64)

EXT_HDR = struct.Struct("<8sIIIIIHHHHIQ8sQ")      # 64
VARIANT = struct.Struct("<QQQQQQIIQ")             # 64
SLOT = struct.Struct("<QBBBBIIIQ")                # 32
LINK = struct.Struct("<IIQ")                      # 16
assert (EXT_HDR.size, VARIANT.size, SLOT.size, LINK.size) == (64, 64, 32, 16)

# hflags bits that the lookup ORs into a local copy only (G17, §4.1 NOTCACHED2)
NOTCACHED2 = (1 << 23) | (0xFF << 24) | (0xF << 2)
PSTATE_SS = 1 << 2


@dataclass(frozen=True)
class EnvOffsets:
    """G21 (offsets from env = %rbp), re-checked on qint-v6 with gdb -nx."""
    breakpoints: int = -0x2980
    thumb: int = 0x14D
    condexec: int = 0x228
    hflags_flags: int = 0x150
    hflags_flags2: int = 0x158
    regs15: int = 0x3C


# --------------------------------------------------------------------------
# A tiny x86-64 assembler (rel32 forms only, so lengths are fixed)
# --------------------------------------------------------------------------

def _d32(v: int) -> bytes:
    return struct.pack("<i", v)


def _u32(v: int) -> bytes:
    return struct.pack("<I", v & 0xFFFFFFFF)


class Asm:
    def __init__(self, base: int) -> None:
        self.base = base          # offset of byte 0 from the TB's tc
        self.buf = bytearray()
        self.labels: dict[str, int] = {}
        self.fix: list[tuple[int, str]] = []    # (field pos, label) rel32 to label

    @property
    def pos(self) -> int:
        return len(self.buf)

    def label(self, name: str) -> None:
        self.labels[name] = self.pos

    def raw(self, b: bytes) -> None:
        self.buf += b

    def jcc(self, cc: int, lab: str) -> None:      # 0f 8x rel32
        self.buf += bytes([0x0F, 0x80 | cc])
        self.fix.append((self.pos, lab))
        self.buf += b"\0\0\0\0"

    def jmp(self, lab: str) -> None:
        self.buf += b"\xe9"
        self.fix.append((self.pos, lab))
        self.buf += b"\0\0\0\0"

    def finish(self) -> bytes:
        for p, lab in self.fix:
            self.buf[p:p + 4] = _d32(self.labels[lab] - (p + 4))
        return bytes(self.buf)


JNE, JE = 0x5, 0x4


@dataclass
class Guard:
    """The §4.1 stub for one exit; offsets relative to R's tc."""
    off: int
    code: bytes
    slot_field: int      # jmp_insn_offset (rel32 field of the slot jump)
    reset: int           # jmp_reset_offset
    f_off: int


def build_guard(*, s_off: int, kf: int, kf2: int, vc: int, kind: int, slot_n: int,
                pool_abs: int, tc_abs: int, struct_abs: int, tb_ret_abs: int,
                env: EnvOffsets = EnvOffsets()) -> Guard:
    a = Asm(s_off)
    a.raw(b"\x48\x83\xbd" + _d32(env.breakpoints) + b"\x00")       # cmp qword [rbp-bp],0
    a.jcc(JNE, "F")
    a.raw(b"\x80\xbd" + _d32(env.thumb) + b"\x01")                  # cmp byte [rbp+thumb],1
    a.jcc(JNE, "F")
    a.raw(b"\x83\xbd" + _d32(env.condexec) + b"\x00")               # cmp dword [rbp+cond],0
    a.jcc(JNE, "F")
    a.raw(b"\x81\xbd" + _d32(env.hflags_flags) + _u32(kf))          # cmp dword [rbp+fl],Kf
    a.jcc(JNE, "F")
    if kf2 < 1 << 31:
        a.raw(b"\x48\x81\xbd" + _d32(env.hflags_flags2) + _u32(kf2))  # cmp qword [..],imm32
    else:
        a.raw(b"\x48\xb8" + struct.pack("<Q", kf2))                   # mov rax,imm64
        a.raw(b"\x48\x39\x85" + _d32(env.hflags_flags2))              # cmp qword [..],rax
    a.jcc(JNE, "F")
    a.raw(b"\x81\xbd" + _d32(env.regs15) + _u32(vc))                # cmp dword [rbp+r15],Vc
    a.jcc(JE, "L1")
    a.jmp("F")
    a.label("L1")
    # nop pad: the rel32 field must be 4-aligned (tc is 4-aligned)
    while (s_off + a.pos + 1) % 4:
        a.raw(b"\x90")
    a.raw(b"\xe9")
    slot_field = s_off + a.pos
    a.raw(b"\0\0\0\0")                                              # slot jump, rel32 = 0
    reset = s_off + a.pos
    if kind == KIND_SPEC:
        a.jmp("F")                                                  # reset path SPEC
    else:
        here = tc_abs + s_off + a.pos + 7
        a.raw(b"\x48\x8d\x05" + _d32(struct_abs + slot_n - here))   # lea rax,[rip->R|n]
        here = tc_abs + s_off + a.pos + 5
        a.raw(b"\xe9" + _d32(tb_ret_abs - here))                    # jmp tb_ret_addr
    a.label("F")
    f_off = s_off + a.pos
    a.raw(b"\x48\x8b\xfd\xff\x15")                                  # mov rdi,rbp; call [rip+d']
    a.raw(_d32(pool_abs - (tc_abs + f_off + 9)))
    a.raw(b"\xff\xe0")                                              # jmp rax
    return Guard(s_off, a.finish(), slot_field, reset, f_off)


# --------------------------------------------------------------------------
# The v6 model
# --------------------------------------------------------------------------

@dataclass
class V6Model:
    base_sc: str
    header: ScHeader
    features: int
    ext: dict[str, Any]
    variants: list[dict[str, Any]] = field(default_factory=list)
    slots: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    arena: list[tuple[int, bytes]] = field(default_factory=list)   # (arena offset, bytes)
    records: dict[int, bytes] = field(default_factory=dict)         # file slot -> record
    notes: list[dict[str, Any]] = field(default_factory=list)
    fix_checksum: bool = True
    post: list[Callable[[bytearray], None]] = field(default_factory=list)  # raw ext edits

    def ext_bytes(self) -> bytes:
        arrays = b"".join(VARIANT.pack(v["general_tb_offset"], v["tb_offset"], v["tc_offset"],
                                       v["tc_size"], v["end_offset"], v["code_fnv64"],
                                       v["variant_id"], v["flags"], v["reserved"])
                          for v in self.variants)
        arrays += b"".join(SLOT.pack(s["src_tb_offset"], s["slot_n"], s["kind"],
                                     s["exit_class"], s["reserved"], s["target_vaddr"],
                                     s["guard_off"], s["guard_len"], s["guard_fnv64"])
                           for s in self.slots)
        arrays += b"".join(LINK.pack(l["slot_index"], l["variant_index"], l["reserved"])
                           for l in self.links)
        e = self.ext
        hdr = EXT_HDR.pack(e["magic"], e["ext_version"], e["ext_header_size"],
                           e.get("n_variants", len(self.variants)),
                           e.get("n_slots", len(self.slots)), e.get("n_links", len(self.links)),
                           e["variant_entry_size"], e["slot_entry_size"], e["link_entry_size"],
                           e["reserved0"], e["reserved1"],
                           e.get("variants_max_end",
                                 max((v["end_offset"] for v in self.variants), default=0)),
                           e["artifact_id8"], 0)
        if self.fix_checksum:
            ck = fnv1a64(arrays, fnv1a64(hdr[:56]))
        else:
            ck = e.get("checksum", 0)
        out = bytearray(hdr[:56] + struct.pack("<Q", ck) + arrays)
        for p in self.post:
            p(out)
        return bytes(out)

    def write(self, out: str | os.PathLike) -> dict[str, Any]:
        out = os.fspath(out)
        sparse_copy(self.base_sc, out, fsync=False)
        ext = self.ext_bytes()
        if ARRAYS_OFFSET + len(ext) - 64 > SC_CODE_OFFSET:
            raise ValueError("ext table too large for the gap")
        hdr = self.header.replace(version=SC_VERSION, reserved=SC7_SEMANTIC_MARKER | self.features).with_checksum()
        fd = os.open(out, os.O_WRONLY)
        try:
            os.pwrite(fd, hdr.to_bytes(), 0)
            os.pwrite(fd, ext, EXT_OFFSET)
            for off, b in self.arena:
                os.pwrite(fd, b, SC_CODE_OFFSET + off)
            for slot, rb in self.records.items():
                os.pwrite(fd, rb, SC_CODE_OFFSET + self.header.code_size + slot * RECORD_SIZE)
        finally:
            os.close(fd)
        return {"path": out, "features": self.features, "ext_len": len(ext),
                "n_variants": len(self.variants), "n_slots": len(self.slots),
                "n_links": len(self.links), "notes": self.notes}


def identity_model(base_sc: str, artifact_id8: bytes = b"WP22TEST") -> V6Model:
    sc = read_sc(base_sc)
    if not sc.loadable or sc.problems or sc.skipped or sc.header.reserved:
        raise ValueError("fixture construction requires a valid public-only SuperCache")
    return V6Model(base_sc=base_sc, header=sc.header, features=FEAT_EXT_TABLE, ext={
        "magic": EXT_MAGIC, "ext_version": 1, "ext_header_size": 64,
        "variant_entry_size": 64, "slot_entry_size": 32, "link_entry_size": 16,
        "reserved0": 0, "reserved1": 0, "artifact_id8": artifact_id8})


def _round_up(v: int, a: int) -> int:
    return (v + a - 1) // a * a


def _find_tail(code: bytes, goto_ptr_off: int) -> int:
    """Start of the 11-byte lookup tail ending with the GOTO_PTR ``jmp rax``."""
    t = goto_ptr_off - 9
    if not (code[t:t + 3] in (b"\x48\x8b\xfd", b"\x48\x89\xef") and code[t + 3:t + 5] == b"\xff\x15"
            and code[goto_ptr_off:goto_ptr_off + 2] == b"\xff\xe0"):
        raise ValueError(f"no lookup tail before +{goto_ptr_off:#x}: {code[t:t + 11].hex()}")
    return t


def candidates(snap: Any) -> list[dict[str, Any]]:
    """R units with an off-page static exit through the lookup and a C unit at its target."""
    units = snap.units()
    by_vaddr: dict[int, list[Any]] = {}
    for u in units:
        if u.vaddrs:
            by_vaddr.setdefault(u.vaddrs[0], []).append(u)
    out = []
    for u in units:
        rec = ScRecord.from_bytes(u.record)
        free = [n for n in (0, 1) if rec.jmp_insn_offset[n] == JMP_INVALID
                and rec.jmp_reset_offset[n] == JMP_INVALID]
        gptr = [s for s in u.sites if int(s.kind) == 14]          # SiteKind.GOTO_PTR
        for se in u.static_exits:
            if se.slot != 0xFF or len(gptr) != 1 or not free:
                continue
            cs = [c for c in by_vaddr.get(se.dest_vaddr, ())
                  if c.key.page_addr1 is None and c.key.flags == u.key.flags
                  and c.key.cflags == u.key.cflags]
            if len(cs) != 1:
                continue
            out.append({"r": u, "c": cs[0], "vc": se.dest_vaddr, "slot_n": free[0],
                        "goto_ptr": gptr[0].field_off})
    return out


def add_link(m: V6Model, snap: Any, cand: dict[str, Any], kind: int, cursor: int,
             env: EnvOffsets = EnvOffsets()) -> int:
    """Grow R (moved to ``cursor``) with a guarded slot; for SPEC also clone C. Returns the
    next free arena offset."""
    r, c, vc, n = cand["r"], cand["c"], cand["vc"], cand["slot_n"]
    base = m.header.code_address
    new_tb = _round_up(cursor, 64)
    link = relocate_unit(r, new_tb)
    rec = ScRecord.from_bytes(link.record)
    code = bytearray(link.body[:r.tc_size])
    search = link.body[r.tc_size:]
    t = _find_tail(bytes(code), cand["goto_ptr"])
    tc_abs = base + rec.tc_offset
    pool_abs = tc_abs + t + 9 + struct.unpack_from("<i", code, t + 5)[0]
    tb_ret = [s.value for s in r.sites if int(s.kind) == 7]       # EXIT_TB_RET_JMP
    if not tb_ret:
        raise ValueError(f"{r.uid}: no exit_tb return jump to learn tb_ret_addr from")
    ckey = c.key
    kf = ckey.flags & ~PSTATE_SS
    kf2 = ckey.cs_base & ~NOTCACHED2
    s_off = r.tc_size
    g = build_guard(s_off=s_off, kf=kf, kf2=kf2, vc=vc, kind=kind, slot_n=n, pool_abs=pool_abs,
                    tc_abs=tc_abs, struct_abs=base + rec.tb_offset, tb_ret_abs=tb_ret[0], env=env)
    code[t:t + 11] = b"\xe9" + _d32(s_off - (t + 5)) + b"\xcc" * 6
    new_code = bytes(code) + g.code
    body = new_code + search
    jio = list(rec.jmp_insn_offset)
    jro = list(rec.jmp_reset_offset)
    jio[n], jro[n] = g.slot_field, g.reset
    rec2 = rec.replace(tc_size=len(new_code), end_offset=rec.tc_offset + len(body),
                       jmp_insn_offset=tuple(jio), jmp_reset_offset=tuple(jro)).with_checksum()
    m.records[r.slot] = rec2.to_bytes()
    m.arena.append((rec2.tc_offset, body))
    slot = {"src_tb_offset": rec2.tb_offset, "slot_n": n, "kind": kind,
            "exit_class": EXIT_STATIC, "reserved": 0, "target_vaddr": vc,
            "guard_off": s_off, "guard_len": len(g.code), "guard_fnv64": fnv1a64(g.code)}
    m.slots.append(slot)
    note = {"r_uid": r.uid, "r_vaddr": hex(r.vaddrs[0]), "c_uid": c.uid, "vc": hex(vc),
            "kind": "spec" if kind == KIND_SPEC else "general", "slot_n": n,
            "r_old_tb": hex(r.tb_offset), "r_new_tb": hex(rec2.tb_offset),
            "r_tc_abs": hex(base + rec2.tc_offset),
            "guard_off": hex(s_off), "guard_len": len(g.code), "slot_field": hex(g.slot_field),
            "reset": hex(g.reset), "kf": hex(kf), "kf2": hex(kf2), "tail": hex(t)}
    nxt = rec2.end_offset
    m.features |= FEAT_GUARDED_SLOTS | FEAT_REWRITTEN
    if kind == KIND_SPEC:
        vtb = _round_up(nxt, 64)
        vl = relocate_unit(c, vtb)
        vrec = ScRecord.from_bytes(vl.record)
        m.arena.append((vrec.tc_offset, vl.body))
        m.variants.append({"general_tb_offset": c.tb_offset, "tb_offset": vrec.tb_offset,
                           "tc_offset": vrec.tc_offset, "tc_size": vrec.tc_size,
                           "end_offset": vrec.end_offset,
                           "code_fnv64": fnv1a64(vl.body[:vrec.end_offset - vrec.tc_offset]),
                           "variant_id": len(m.variants), "flags": 0, "reserved": 0})
        m.links.append({"slot_index": len(m.slots) - 1, "variant_index": len(m.variants) - 1,
                        "reserved": 0})
        m.features |= FEAT_VARIANTS
        note.update({"variant_tb": hex(vrec.tb_offset), "variant_tc_abs": hex(base + vrec.tc_offset),
                     "c_tc_abs": hex(base + c.tc_offset), "variant_id": len(m.variants) - 1})
        nxt = vrec.end_offset
    m.notes.append(note)
    return nxt


def guarded_model(base_sc: str, snap: Any, spec: Iterable[int] = (), general: Iterable[int] = (),
                  *, artifact_id8: bytes = b"WP22TEST") -> V6Model:
    m = identity_model(base_sc, artifact_id8)
    cands = {c["r"].vaddrs[0]: c for c in candidates(snap)}
    sc = read_sc(base_sc)
    cursor = _round_up(sc.max_end, 64)
    for kind, vaddrs in ((KIND_SPEC, spec), (KIND_GENERAL, general)):
        for v in vaddrs:
            if v not in cands:
                raise ValueError(f"R vaddr {v:#x} is not an eligible candidate")
            cursor = add_link(m, snap, cands[v], kind, cursor)
    limit = sc.header.code_size - sc.header.host_page_size - 1024
    if cursor >= limit:
        raise ValueError("synthetic units exceed the arena limit")
    return m


# --------------------------------------------------------------------------
# Mutations: name -> (function on the model, reason substring the loader prints)
# --------------------------------------------------------------------------

def _ext(**kw: Any) -> Callable[[V6Model], None]:
    def f(m: V6Model) -> None:
        m.ext.update(kw)
    return f


def _feat(v: Callable[[int], int]) -> Callable[[V6Model], None]:
    def f(m: V6Model) -> None:
        m.features = v(m.features)
    return f


def _var(**kw: Any) -> Callable[[V6Model], None]:
    def f(m: V6Model) -> None:
        m.variants[0].update(kw)
    return f


def _slot(**kw: Any) -> Callable[[V6Model], None]:
    def f(m: V6Model) -> None:
        m.slots[0].update(kw)
    return f


def _no_checksum(m: V6Model) -> None:
    m.fix_checksum = False
    m.ext["checksum"] = 0x1234


def _flip_variant_code(m: V6Model) -> None:
    off, b = next((o, b) for o, b in m.arena if o == m.variants[0]["tc_offset"])
    i = m.arena.index((off, b))
    m.arena[i] = (off, bytes([b[0] ^ 0xFF]) + b[1:])


def _flip_guard(m: V6Model) -> None:
    s = m.slots[0]
    tc = s["src_tb_offset"] + TB_STRUCT
    i, (off, b) = next((i, x) for i, x in enumerate(m.arena) if x[0] == tc)
    p = s["guard_off"] + 1
    m.arena[i] = (off, b[:p] + bytes([b[p] ^ 0x01]) + b[p + 1:])


def _overlap_record(m: V6Model) -> None:
    sc = read_sc(m.base_sc)
    r = sc.units()[0].record
    v = m.variants[0]
    d = r.tb_offset - v["tb_offset"]
    for k in ("tb_offset", "tc_offset", "end_offset"):
        v[k] += d


def _dup_slot(m: V6Model) -> None:
    m.slots.append(dict(m.slots[0], kind=KIND_GENERAL))


def _link_general(m: V6Model) -> None:
    gi = next(i for i, s in enumerate(m.slots) if s["kind"] == KIND_GENERAL)
    m.links[0]["slot_index"] = gi


def _orphan_variant(m: V6Model) -> None:
    m.links.clear()
    for s in m.slots:
        s["kind"] = KIND_GENERAL


def _variant_no_slot(m: V6Model) -> None:
    m.links.clear()
    m.slots[0]["kind"] = KIND_GENERAL


#: name -> (needs a SPEC link (s) / a SPEC + a GENERAL slot (sg) / identity (i), mutation,
#: reason substring of "supercache: invalid v6 extension: <reason>")
MUTATIONS: dict[str, tuple[str, Callable[[V6Model], None], str]] = {
    "unknown_feature_bit": ("i", _feat(lambda f: f | 0x10), "unknown feature bits 0x10"),
    "ext_table_clear": ("i", _feat(lambda f: f & ~FEAT_EXT_TABLE), "feature EXT_TABLE clear"),
    "bad_ext_magic": ("i", _ext(magic=b"QSCX6EXX"), "bad ext magic"),
    "bad_ext_version": ("i", _ext(ext_version=2), "bad ext_version 2"),
    "bad_ext_header_size": ("i", _ext(ext_header_size=72), "bad ext_header_size 72"),
    "bad_entry_sizes": ("i", _ext(slot_entry_size=40), "bad entry sizes 64/40/16"),
    "reserved_nonzero": ("i", _ext(reserved1=1), "reserved header fields not zero"),
    "ext_too_large": ("i", _ext(n_variants=2000), "ext table too large"),
    "bad_checksum": ("i", _no_checksum, "ext checksum mismatch"),
    "artifact_id_zero": ("i", _ext(artifact_id8=b"\0" * 8), "artifact id is zero"),
    "features_disagree": ("s", _feat(lambda f: f & ~FEAT_VARIANTS),
                          "feature VARIANTS disagrees with n_variants 1"),
    "variants_max_end": ("s", _ext(variants_max_end=0x40), "variants_max_end 0x40 != "),
    "variant_general_missing": ("s", _var(general_tb_offset=0x80),
                                "variant 0: general_tb_offset 0x80 names no valid record"),
    "variant_misaligned": ("s", lambda m: [m.variants[0].update({k: m.variants[0][k] + 8})
                                           for k in ("tb_offset", "tc_offset", "end_offset")],
                           "not 64-aligned"),
    "variant_tc_size": ("s", lambda m: m.variants[0].update(
        tc_size=m.variants[0]["tc_size"] + 1), "variant 0: tc_size"),
    "variant_end": ("s", lambda m: m.variants[0].update(
        end_offset=m.variants[0]["end_offset"] + 64),
        "variant 0: end_offset does not match C's extent"),
    "variant_overlaps_record": ("s", _overlap_record, "variant 0: overlaps record at tb_offset"),
    "variant_flags": ("s", _var(flags=2), "variant 0: unknown flags 0x2"),
    "variant_id": ("s", _var(variant_id=5), "variant 0: variant_id 5 is not dense"),
    "variant_code_hash": ("s", _flip_variant_code, "variant 0: code hash mismatch"),
    "slot_src_missing": ("s", _slot(src_tb_offset=0x80),
                         "slot 0: src_tb_offset 0x80 names no valid record"),
    "slot_n": ("s", _slot(slot_n=2), "slot 0: slot_n 2"),
    "slot_kind": ("s", _slot(kind=3), "slot 0: bad kind 3"),
    "slot_exit_class": ("s", _slot(exit_class=0), "slot 0: bad exit_class 0"),
    "slot_guard_range": ("s", lambda m: m.slots[0].update(guard_len=0x10000),
                         "outside R's code"),
    "slot_jump_outside_guard": ("s", lambda m: m.slots[0].update(
        guard_len=m.slots[0]["guard_len"] - 40), "outside the guard"),
    "slot_unregistered": ("s", lambda m: m.slots[0].update(slot_n=1 - m.slots[0]["slot_n"]),
                          "R has no registered jump"),
    "slot_duplicate": ("s", _dup_slot, "slot 1: duplicates slot 0"),
    "slot_guard_hash": ("s", _flip_guard, "slot 0: guard hash mismatch"),
    "link_slot_range": ("s", lambda m: m.links[0].update(slot_index=7),
                        "link 0: slot_index 7 out of range"),
    "link_variant_range": ("s", lambda m: m.links[0].update(variant_index=9),
                           "link 0: variant_index 9 out of range"),
    "link_reserved": ("s", lambda m: m.links[0].update(reserved=1), "link 0: reserved not zero"),
    "link_to_general": ("sg", _link_general, "is not SPEC"),
    "link_target_offset": ("s", lambda m: m.slots[0].update(
        target_vaddr=m.slots[0]["target_vaddr"] + 2), "is not at C's page offset"),
    "spec_slot_no_link": ("s", lambda m: m.links.clear(), "slot 0: SPEC slot has no link"),
    "variant_no_link": ("s", _variant_no_slot, "variant 0: has no link"),
}


def main(argv: list[str] | None = None) -> int:
    from scopt.importer.import_cache import open_snapshot

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--snapshot", type=Path, help="imported snapshot directory")
    ap.add_argument("--base-sc", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--spec", type=lambda s: int(s, 0), action="append", default=[])
    ap.add_argument("--general", type=lambda s: int(s, 0), action="append", default=[])
    ap.add_argument("--mutate", choices=sorted(MUTATIONS))
    ap.add_argument("--list", action="store_true", help="list eligible R units and exit")
    a = ap.parse_args(argv)
    snap = open_snapshot(a.snapshot) if a.snapshot else None
    if a.list:
        for c in candidates(snap):
            print(f"R {c['r'].vaddrs[0]:#x} -> C {c['vc']:#x} slot {c['slot_n']}")
        return 0
    if a.spec or a.general:
        m = guarded_model(str(a.base_sc), snap, a.spec, a.general)
    else:
        m = identity_model(str(a.base_sc))
    if a.mutate:
        MUTATIONS[a.mutate][1](m)
    print(json.dumps(m.write(a.out), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

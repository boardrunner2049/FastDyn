"""WP16 E-COST probe, B3 side: v5 copies with GENERAL guarded slots (design §4.1, §15 E-COST).

Labelled ``experimental_unsound_without_v6_tlb_hook`` (design §2, §16 D-B3-3):
the stock loader has no TLB-flush disarm hook, so a linked guarded slot stays
linked across an MPU write or a memory remap. Admitted configuration for every
run of these copies: no MPU writes, no memory remap, no gdbstub; each timed
run needs a clean diag twin (0 ``tlb_flush*``/``tcg_commit_cpu`` calls after
``benchmark_start``; plan "Runtime rules").

What a copy contains (only the redirected return units R change):

* ``[0, T)`` of R unchanged (relocated when R must move), the 11-byte lookup
  tail ``mov rdi,rbp; call [rip+d]; jmp rax`` at T replaced by
  ``jmp rel32 S`` + 6 x ``int3``;
* the stub S appended after the pool (§4.1 canonical template): g_bp, g_thumb,
  g_cond, g_flags, g_flags2, then g_pc per target; each target jumps to a
  4-aligned ``jmp rel32`` whose rel32 field becomes R's free registered slot n
  (``jmp_insn_offset[n]``); the slot's reset path is GENERAL
  ``lea rax,[R_struct+n]; jmp tb_ret`` (exit_tb(R|n): the stock main loop links
  it after its own lookup, cpu-exec.c:1007-1009); F is the original tail with
  the call displacement re-resolved to the same pool slot;
* search data moved behind the grown code, record ``tc_size``/``end_offset``/
  ``jmp_*_offset`` rewritten (v5 writer reused).

Variants for the calibration (all with the identical placement):

* ``gce``       guards as above (copy A of the plan);
* ``gce-fail``  same bytes but every g_pc immediate is ``Vc | 1`` (an odd PC never
  equals ``regs[15]`` in Thumb state), so every execution takes F: measures the
  guard + layout overhead without removing any lookup;
* ``lk2x``      ``gce-fail`` whose F calls ``helper_lookup_tb_ptr`` twice (the
  first result is discarded; the helper is idempotent here: ``can_do_io = 1``,
  empty breakpoint list, jump-cache hit), so ``t(lk2x) - t(gce-fail)`` is one
  lookup hit per F execution: the direct measurement of ``c_lookup``.

The design's v6 artifact differs only in registering SPEC/GENERAL slots with
the loader; nothing here is ever published.
"""

from __future__ import annotations

import argparse
import bisect
import json
import struct
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from scopt.decode.search import decode_search, encode_search, is_pcrel, SearchRow
from scopt.decode.x86 import sweep
from scopt.formats.sc_v5 import ScRecord, read_sc, write_sc
from scopt.link import apply_fixup, round_up

LABEL = "experimental_unsound_without_v6_tlb_hook"
GATE_EXE_SHA256 = "sha256:8864c72671971cf4488b5afa76eeee02a3e2a472c45af81ec1314464e6035f41"

# EnvLayout of the qint binary (design G21; re-checked with gdb -nx ptype in WP16).
ENV = {
    "bp_list": -0x2980, "icount_decr": -0x8, "can_do_io": -0x4,
    "regs15": 0x3C, "thumb": 0x14D, "hflags_flags": 0x150, "hflags_flags2": 0x158,
    "condexec": 0x228,
}
TB_STRUCT = 192  # ROUND_UP(sizeof(TranslationBlock)=152, 64)
SLOT_FREE = 0xFFFF
CF_COUNT_MASK, CF_MEMI_ONLY, CF_NOIRQ = 0x1FF, 0x1000, 0x10000
M32_HANDLER = 1
THUMB_BIT = 1 << 23
NOTCACHED2 = (0xFF << 24) | THUMB_BIT | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5)
PSTATE_SS = 1 << 2
PAGE_MASK = 0x3FF  # target_page_bits 10

TAIL_MOV = bytes.fromhex("488bfd")
TAIL_JMP = bytes.fromhex("ffe0")


class ProbeError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


# ---------------------------------------------------------------------------
# Generic unit rebuild: move, uniform prefix shift, same-length patches, append
# ---------------------------------------------------------------------------

@dataclass
class Rebuilt:
    """A rebuilt unit: new record + body ``[tc', end')`` and what changed."""

    uid: str
    record: bytes
    body: bytes
    old_tb: int
    new_tb: int
    shift: int = 0  # D: bytes at old offset >= prefix_len now sit at +D
    prefix_len: int = 0
    appended_at: int | None = None  # offset of the appended bytes in the new code
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def rec(self) -> ScRecord:
        return ScRecord.from_bytes(self.record)


def code_refs(u) -> list[tuple[int, int, int, str]]:
    """``(insn_addr, field_off, target_off, kind)`` of every rel32/riprel32 field in ``[0, pool)``.

    Offsets are relative to ``tc``; ``kind`` is ``branch`` or ``rip``. rel8 branches are
    returned with field width 1 (they must stay intra-region)."""
    end = u.pool_start if u.pool_start is not None else u.tc_size
    out = []
    sx = lambda v: v - (1 << 64) if v >= 1 << 63 else v  # targets are computed mod 2**64
    for i in sweep(u.code, 0, end):
        if i.bad:
            raise ProbeError("undecodable", f"{u.uid} @{i.address:#x}")
        if i.branch_target is not None and i.imm_size:
            out.append((i.address, i.address + i.imm_offset, sx(i.branch_target), "branch", i.imm_size))
        elif i.rip_mem is not None:
            out.append((i.address, i.address + i.disp_offset, sx(i.rip_target), "rip", i.disp_size))
    return out


def rebuild_unit(u, new_tb: int, *, prefix: bytes | None = None, prefix_len: int = 0,
                 patches: Iterable[tuple[int, bytes, bytes]] = (), append: bytes = b"",
                 append_align: int = 16, jmp_override: Mapping[int, tuple[int, int]] | None = None,
                 code_size: int | None = None) -> Rebuilt:
    """Rebuild ``u`` at ``new_tb``.

    * ``prefix``/``prefix_len``: replace ``code[0:prefix_len]`` by ``prefix``; every later
      byte shifts by ``D = len(prefix) - prefix_len`` (must be a multiple of 8 so the
      goto_tb fields stay 4-aligned and the pool 8-aligned). References from the shifted
      part to the own struct or outside the unit are re-encoded; references into
      ``[0, prefix_len)`` are refused (``ref_into_prefix``).
    * ``patches``: same-length ``(old_off, old_bytes, new_bytes)`` in old offsets
      (refused inside the prefix, or when the old bytes differ).
    * ``append``: bytes placed after the pool at ``ROUND_UP(tc_size', append_align)``
      (int3 padding); the search data follows them.
    * ``jmp_override``: slot n -> (insn_offset, reset_offset) in **new** offsets.
    """
    d = new_tb - u.tb_offset
    D = 0 if prefix is None else len(prefix) - prefix_len
    if D % 8:
        raise ProbeError("shift_misaligned", f"D={D}")
    if prefix is not None and prefix_len > u.insn0_start:
        raise ProbeError("prefix_past_insn0", f"{prefix_len} > {u.insn0_start}")
    code = bytearray(u.code)
    # whole-unit move: B2 fixups (all rel_minus_delta to targets outside the unit)
    fix_fields = set()
    for f in u.reloc.fixups:
        apply_fixup(code, f, d, uid=u.uid)
        fix_fields.add(f.field_off)
    if D:
        for insn, fo, tgt, kind, width in code_refs(u):
            if insn < prefix_len:
                continue  # the replaced prefix is re-encoded by the caller
            intra = 0 <= tgt < u.tc_size
            if intra and tgt >= prefix_len:
                continue  # both ends shift by D
            if intra:
                raise ProbeError("ref_into_prefix", f"{u.uid} @{insn:#x} -> {tgt:#x}")
            if width != 4:
                raise ProbeError("rel8_out_of_region", f"{u.uid} @{insn:#x}")
            own_struct = -TB_STRUCT <= tgt < 0
            if not own_struct and fo not in fix_fields:
                raise ProbeError("unfixed_external_ref", f"{u.uid} @{insn:#x} -> {tgt:#x}")
            v = int.from_bytes(code[fo:fo + 4], "little", signed=True) - D
            code[fo:fo + 4] = v.to_bytes(4, "little", signed=True)
        code = bytearray(prefix) + code[prefix_len:]
    for off, old, new in patches:
        if len(old) != len(new):
            raise ProbeError("patch_not_same_length", f"{u.uid} @{off:#x}")
        if prefix is not None and off < prefix_len:
            raise ProbeError("patch_in_prefix", f"{u.uid} @{off:#x}")
        o = off + (D if off >= prefix_len else 0)
        if bytes(code[o:o + len(old)]) != old:
            raise ProbeError("patch_old_bytes", f"{u.uid} @{off:#x}: "
                             f"{bytes(code[o:o + len(old)]).hex()} != {old.hex()}")
        code[o:o + len(new)] = new
    appended_at = None
    if append:
        a = round_up(len(code), append_align)
        code += b"\xcc" * (a - len(code))
        appended_at = len(code)
        code += append
    rec = ScRecord.from_bytes(u.record)
    rows = decode_search(u.search, u.icount, is_pcrel(rec.cflags))
    if D:
        rows = [SearchRow(r.words, r.host_end + D) for r in rows]
    search = encode_search(rows, is_pcrel(rec.cflags))
    if not D:
        search = u.search  # keep the original bytes (incl. any trailing padding)
    tc = rec.tc_offset + d
    jri, jii = list(rec.jmp_reset_offset), list(rec.jmp_insn_offset)
    for n in (0, 1):
        if jii[n] != SLOT_FREE and jii[n] >= prefix_len:
            jii[n] += D
            jri[n] += D
    for n, (ins, rst) in (jmp_override or {}).items():
        jii[n], jri[n] = ins, rst
    if any(x != SLOT_FREE and x >= SLOT_FREE for x in jii + jri):
        raise ProbeError("slot_offset_range", u.uid)
    new = rec.replace(tb_offset=new_tb, tc_offset=tc, tc_size=len(code),
                      end_offset=tc + len(code) + len(search),
                      jmp_reset_offset=tuple(jri), jmp_insn_offset=tuple(jii))
    raw = new.to_bytes(fix_checksum=True)
    if code_size is not None:
        p = ScRecord.from_bytes(raw).problem(code_size, 152)
        if p:
            raise ProbeError("record_bounds", f"{u.uid}: {p}")
    return Rebuilt(u.uid, raw, bytes(code) + search, u.tb_offset, new_tb, D, prefix_len,
                   appended_at)


class Placer:
    """In place when the grown unit still ends before the next unit's struct, else at the end."""

    def __init__(self, units, header):
        self.starts = sorted(u.tb_offset for u in units)
        self.max_end = max(u.end_offset for u in units)
        self.cursor = round_up(self.max_end, 64)
        self.limit = header.code_size - header.host_page_size - 1024
        self.moved: list[str] = []

    def place(self, u, new_len: int) -> int:
        """``new_len`` = grown ``end - tb``."""
        i = bisect.bisect_right(self.starts, u.tb_offset)
        nxt = self.starts[i] if i < len(self.starts) else self.cursor
        if u.tb_offset + new_len <= nxt:
            return u.tb_offset
        tb = round_up(self.cursor, 64)
        self.cursor = tb + new_len
        if self.cursor >= self.limit:
            raise ProbeError("place_out_of_range", u.uid)
        self.moved.append(u.uid)
        return tb


def write_copy(path: str | Path, snapshot, rebuilt: Mapping[str, Rebuilt]) -> dict[str, Any]:
    """v5 writer reuse: every unit in slot order, rebuilt ones replaced."""
    parts = []
    for u in snapshot.units():
        r = rebuilt.get(u.uid)
        parts.append((r.record, r.body) if r else (u.record, u.code + u.search))
    hdr = write_sc(path, snapshot.header, parts)
    sc = read_sc(path, strict=True)
    if len(sc.valid_slots) != len(parts):
        raise ProbeError("write_lost_records", f"{len(sc.valid_slots)} != {len(parts)}")
    return {"records": len(parts), "rebuilt": len(rebuilt), "max_end": sc.max_end,
            "header_checksum": f"0x{hdr.checksum:x}"}


# ---------------------------------------------------------------------------
# Guest ELF (exit classification)
# ---------------------------------------------------------------------------

class GuestElf:
    def __init__(self, path: str | Path):
        from elftools.elf.elffile import ELFFile
        import capstone
        with open(path, "rb") as f:
            e = ELFFile(f)
            self.segs = [(s["p_vaddr"], s["p_paddr"], s.data()) for s in e.iter_segments()
                         if s["p_type"] == "PT_LOAD" and s["p_filesz"]]
        self.md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)

    def read(self, addr: int, n: int) -> bytes:
        for va, pa, data in self.segs:
            for base in (va, pa):
                if base <= addr < base + len(data):
                    return data[addr - base:addr - base + n]
        return b""

    def insn(self, addr: int) -> tuple[str, str, int]:
        for _a, size, m, o in self.md.disasm_lite(self.read(addr, 4), addr, count=1):
            return m, o, size
        return "?", "", 2


def last_insn_vaddr(u) -> int:
    """Guest vaddr of R's last instruction (one page per admitted TB, CF_PCREL rows)."""
    return (u.vaddrs[0] & ~PAGE_MASK) | (u.insns[-1].words[0] & PAGE_MASK)


def exit_class(m: str, o: str) -> str:
    base, ops = m.split(".")[0], o.replace(" ", "")
    if base == "bx":
        return "return" if ops == "lr" else "indirect_multiway"
    if base.startswith("pop") and "pc" in ops:
        return "return"
    if base.startswith("ldm") and "pc" in ops:
        return "return" if ops.startswith("sp!") else "indirect_multiway"
    if base.startswith("ldr") and ops.startswith("pc,"):
        return "return" if ops.startswith("pc,[sp]") else "indirect_multiway"
    return "indirect_multiway"


# ---------------------------------------------------------------------------
# Admission (design §3.1 rules 1-5) and target choice
# ---------------------------------------------------------------------------

@dataclass
class Site:
    uid: str
    tail: int  # T
    pool_slot: int  # offset of the lookup helper pointer in R's code
    exit_class: str  # return | static
    insn: str
    free_slots: tuple[int, ...]
    weight: int  # steady lookup_hit count out of R (merged profile)
    targets: list[dict[str, Any]] = field(default_factory=list)  # chosen (<= 2)
    guarded_weight: int = 0
    reason: str | None = None


def find_tail(u) -> tuple[int, int]:
    """(T, pool slot offset) of R's lookup tail, else ProbeError(exit_not_goto_ptr)."""
    gp = [m for m in u.opmarks if int(m.kind) == 6]  # OpMarkKind.GOTO_PTR
    if len(gp) != 1:
        raise ProbeError("exit_not_goto_ptr", f"{len(gp)} GOTO_PTR marks")
    T = gp[0].host_off - 9
    c = u.code
    if not (c[T:T + 3] == TAIL_MOV and c[T + 3:T + 5] == b"\xff\x15" and c[T + 9:T + 11] == TAIL_JMP):
        raise ProbeError("exit_not_goto_ptr", f"tail bytes {bytes(c[T:T + 11]).hex()}")
    calls = [m for m in u.opmarks if int(m.kind) == 1 and m.host_off == T]
    hid = calls[0].helper_id if calls else None
    names = {h.id: (h.name or h.symbol) for h in u.bindings.helpers}
    if names.get(hid) != "lookup_tb_ptr":
        raise ProbeError("exit_not_goto_ptr", f"call at T is helper {hid} {names.get(hid)}")
    disp = int.from_bytes(c[T + 5:T + 9], "little", signed=True)
    slot = T + 9 + disp
    if u.pool_start is None or not u.pool_start <= slot < u.tc_size:
        raise ProbeError("exit_not_goto_ptr", "lookup pointer not in the own pool")
    for ins in sweep(u.code, 0, u.pool_start):
        if ins.branch_target is not None and T < ins.branch_target < T + 11:
            raise ProbeError("tail_is_branch_target", f"@{ins.address:#x}")
    return T, slot


def admit(snapshot, profile: Mapping[str, Any], elf: GuestElf, *, steady_phase: int = 1,
          max_targets: int = 2) -> tuple[list[Site], dict[str, Any]]:
    units = {u.uid: u for u in snapshot.units()}
    out_lookup: dict[str, Counter] = defaultdict(Counter)
    total_lookup = 0
    for e in profile["edges"]:
        if e["phase"] == steady_phase and e["kind"] == "lookup_hit":
            out_lookup[e["src"]][e["dst"]] += e["count"]
            total_lookup += e["count"]
    sites, reasons = [], Counter()
    rweight = Counter()
    for u in snapshot.units():
        if not any(int(m.kind) == 6 for m in u.opmarks):
            continue
        w = sum(out_lookup[u.uid].values())
        try:
            T, slot = find_tail(u)
            k = u.key
            if k.cs_base & M32_HANDLER:
                raise ProbeError("handler_mode_return")
            if k.cflags & (CF_COUNT_MASK | CF_NOIRQ | CF_MEMI_ONLY):
                raise ProbeError("special_cflags_unit")
            if u.meta_status != "ok" or not u.reloc.relocatable:
                raise ProbeError("unit_not_relocatable")
            if u.origin == "ram_resident":
                raise ProbeError("ram_resident")
            rec = ScRecord.from_bytes(u.record)
            free = tuple(n for n in (0, 1) if rec.jmp_insn_offset[n] == SLOT_FREE)
            if not free:
                raise ProbeError("no_free_slot")
            static = [x for x in u.static_exits if x.slot == 0xFF]
            m, o, _ = elf.insn(last_insn_vaddr(u))
            if static:
                cls = "static"
            else:
                cls = exit_class(m, o)
                if cls != "return":
                    raise ProbeError(cls, f"{m} {o}")
        except ProbeError as e:
            reasons[e.reason] += 1
            rweight[e.reason] += w
            continue
        s = Site(u.uid, T, slot, cls, f"{m} {o}", free, w)
        for dst, n in out_lookup[u.uid].most_common():
            c = units.get(dst)
            why = None
            if c is None:
                why = "continuation_not_in_snapshot"
            elif (c.key.flags != k.flags or (c.key.cs_base & ~NOTCACHED2) != (k.cs_base & ~NOTCACHED2)
                  or not c.key.cs_base & THUMB_BIT or (c.key.cs_base >> 24) & 0xFF
                  or c.key.cflags != k.cflags or c.key.flags & PSTATE_SS):
                why = "continuation_key_mismatch"
            elif c.key.page_addr1 is not None:
                why = "two_page_target"
            elif static and c.vaddrs[0] != static[0].dest_vaddr:
                why = "static_target_mismatch"
            if why:
                reasons[f"target:{why}"] += 1
                continue
            if len(s.targets) < min(max_targets, len(free)):
                s.targets.append({"dst": dst, "vaddr": c.vaddrs[0], "count": n,
                                  "flags": c.key.flags, "cs_base": c.key.cs_base})
        if not s.targets:
            reasons["target_not_profiled"] += 1
            continue
        s.guarded_weight = sum(t["count"] for t in s.targets)
        sites.append(s)
    sites.sort(key=lambda s: -s.weight)
    stats = {"steady_lookup_hits": total_lookup, "refused_units": dict(reasons),
             "refused_weight": dict(rweight),
             "admissible_units": len(sites), "admissible_weight": sum(s.weight for s in sites)}
    return sites, stats


def top_set(sites: list[Site], coverage: float = 0.99) -> list[Site]:
    """Smallest prefix (by R weight) covering ``coverage`` of the admissible lookup weight."""
    tot = sum(s.weight for s in sites)
    out, acc = [], 0
    for s in sites:
        if s.weight <= 0 or acc >= coverage * tot:
            break
        out.append(s)
        acc += s.weight
    return out


# ---------------------------------------------------------------------------
# Stub encoder (§4.1)
# ---------------------------------------------------------------------------

def _rel32(src_end: int, dst: int) -> bytes:
    v = dst - src_end
    if not -(1 << 31) <= v < (1 << 31):
        raise ProbeError("rel32_range")
    return struct.pack("<i", v)


def build_stub(*, stub_off: int, tc_abs: int, tb_abs: int, tb_ret_abs: int, pool_slot: int,
               targets: list[tuple[int, int]], kf: int, kf2: int, mode: str = "gce"
               ) -> tuple[bytes, dict[int, tuple[int, int]], dict[str, Any]]:
    """Encode S..F at code offset ``stub_off``. ``targets`` = [(slot n, Vc)].

    Returns (bytes, {n: (jmp_insn_offset, jmp_reset_offset)}, layout). Offsets are
    relative to tc. All branches use rel32 so the size is fixed per target count."""
    if mode not in ("gce", "gce-fail", "lk2x"):
        raise ValueError(mode)
    out = bytearray()
    fixes: list[tuple[int, str]] = []  # (pos of rel32 field, label)

    def here() -> int:
        return stub_off + len(out)

    def jcc(op: bytes, label: str) -> None:
        out.extend(op)
        fixes.append((len(out), label))
        out.extend(b"\0\0\0\0")

    disp = lambda v: struct.pack("<i", v)
    # g_bp: cmp qword [rbp-0x2980], 0 ; jne F
    out += b"\x48\x83\xbd" + disp(ENV["bp_list"]) + b"\x00"
    jcc(b"\x0f\x85", "F")
    # g_thumb: cmp byte [rbp+0x14d], 1
    out += b"\x80\xbd" + disp(ENV["thumb"]) + b"\x01"
    jcc(b"\x0f\x85", "F")
    # g_cond: cmp dword [rbp+0x228], 0
    out += b"\x83\xbd" + disp(ENV["condexec"]) + b"\x00"
    jcc(b"\x0f\x85", "F")
    # g_flags: cmp dword [rbp+0x150], imm32
    out += b"\x81\xbd" + disp(ENV["hflags_flags"]) + struct.pack("<I", kf & 0xFFFFFFFF)
    jcc(b"\x0f\x85", "F")
    # g_flags2: cmp qword [rbp+0x158], simm32
    if kf2 >= 1 << 31:
        raise ProbeError("kf2_imm64_unsupported", hex(kf2))
    out += b"\x48\x81\xbd" + disp(ENV["hflags_flags2"]) + struct.pack("<i", kf2)
    jcc(b"\x0f\x85", "F")
    for i, (_n, vc) in enumerate(targets):
        imm = (vc | 1) if mode in ("gce-fail", "lk2x") else vc
        out += b"\x81\x7d" + bytes([ENV["regs15"]]) + struct.pack("<I", imm & 0xFFFFFFFF)
        jcc(b"\x0f\x84", f"L{i}")
    out += b"\xe9"
    fixes.append((len(out), "F"))
    out += b"\0\0\0\0"
    labels: dict[str, int] = {}
    slots: dict[int, tuple[int, int]] = {}
    for i, (n, _vc) in enumerate(targets):
        labels[f"L{i}"] = here()
        # pad so the rel32 field (after the e9 opcode) is 4-aligned in absolute terms
        while (tc_abs + here() + 1) % 4:
            out += b"\x90"
        out += b"\xe9"
        ins = here()
        out += b"\0\0\0\0"  # reset state: jump to the next instruction (the reset path)
        rst = here()
        slots[n] = (ins, rst)
        # reset path GENERAL: lea rax,[rip -> tb + n] ; jmp tb_ret
        out += b"\x48\x8d\x05" + _rel32(tc_abs + here() + 7, tb_abs + n)
        out += b"\xe9" + _rel32(tc_abs + here() + 5, tb_ret_abs)
    labels["F"] = here()
    ncall = 2 if mode == "lk2x" else 1
    for _ in range(ncall):
        out += TAIL_MOV + b"\xff\x15" + _rel32(here() + 9, pool_slot)  # call ends at mov+9
    out += TAIL_JMP
    for pos, lab in fixes:
        out[pos:pos + 4] = _rel32(stub_off + pos + 4, labels[lab])
    layout = {"stub_off": stub_off, "F_off": labels["F"], "len": len(out),
              "slots": {str(n): {"insn": a, "reset": b} for n, (a, b) in slots.items()}}
    return bytes(out), slots, layout


def tail_patch(T: int, stub_off: int) -> bytes:
    """``jmp rel32 S`` + 6 x int3 (11 bytes, replaces the lookup tail)."""
    return b"\xe9" + _rel32(T + 5, stub_off) + b"\xcc" * 6


def guard_keys(snapshot, site: Site) -> tuple[int, int]:
    u = snapshot.unit(site.uid)
    t = site.targets[0]
    kf = t["flags"] & ~PSTATE_SS
    kf2 = t["cs_base"] & ~NOTCACHED2
    for x in site.targets[1:]:
        if (x["flags"] & ~PSTATE_SS) != kf or (x["cs_base"] & ~NOTCACHED2) != kf2:
            raise ProbeError("targets_disagree_on_key", site.uid)
    if (u.key.flags & ~PSTATE_SS) != kf:
        raise ProbeError("continuation_key_mismatch", site.uid)
    return kf, kf2


def tb_ret_abs(snapshot) -> int:
    vals = {f.target_abs for u in snapshot.units() for f in u.reloc.fixups
            if f.target_class == "prologue.tb_ret"}
    if len(vals) != 1:
        raise ProbeError("tb_ret_ambiguous", str(vals))
    return vals.pop()


def grow_site(snapshot, site: Site, placer: Placer, *, mode: str, tb_ret: int) -> Rebuilt:
    """Grow R for ``site`` (relocating it when the stub does not fit in place)."""
    u = snapshot.unit(site.uid)
    hdr = snapshot.header
    kf, kf2 = guard_keys(snapshot, site)
    targets = [(n, t["vaddr"]) for n, t in zip(site.free_slots, site.targets)]
    stub_off = round_up(u.tc_size, 16)
    # size probe (stub bytes do not depend on the absolute placement except nop pads,
    # which are <= 3 per target): reserve the maximum
    # the reserve is the lk2x size in every mode, so gce / gce-fail / lk2x get the
    # identical placement (their timing differences are then code, not layout)
    probe, _, _ = build_stub(stub_off=stub_off, tc_abs=0, tb_abs=0, tb_ret_abs=0, pool_slot=0,
                             targets=targets, kf=kf, kf2=kf2, mode="lk2x")
    reserve = len(probe) + 3 * len(targets)
    new_len = TB_STRUCT + stub_off + reserve + len(u.search)
    new_tb = placer.place(u, new_len)
    tc_new = new_tb + TB_STRUCT
    stub, slots, layout = build_stub(stub_off=stub_off, tc_abs=hdr.code_address + tc_new,
                                     tb_abs=hdr.code_address + new_tb, tb_ret_abs=tb_ret,
                                     pool_slot=site.pool_slot, targets=targets, kf=kf, kf2=kf2,
                                     mode=mode)
    stub = stub + b"\xcc" * (reserve - len(stub))  # keep the reserved size (placement)
    old_tail = bytes(u.code[site.tail:site.tail + 11])
    rb = rebuild_unit(u, new_tb, patches=[(site.tail, old_tail, tail_patch(site.tail, stub_off))],
                      append=stub, append_align=16, jmp_override=slots, code_size=hdr.code_size)
    if rb.appended_at != stub_off:
        raise ProbeError("stub_offset_drift", f"{rb.appended_at} != {stub_off}")
    rb.notes.update({"mode": mode, "tail": site.tail, "stub": layout, "kf": hex(kf), "kf2": hex(kf2),
                     "targets": [{"slot": n, "vaddr": hex(v)} for n, v in targets],
                     "moved": new_tb != u.tb_offset})
    return rb


# ---------------------------------------------------------------------------
# Static checks of a grown unit (a mini V13/V15/V16 for the experiment)
# ---------------------------------------------------------------------------

def check_grown(snapshot, site: Site, rb: Rebuilt) -> list[str]:
    """Problems (empty = ok): R's bytes before T equal the relocated original, rows equal,
    stub decodes and tiles, slot fields 4-aligned and pointing at reset paths, F == tail."""
    from scopt.link import relocate_unit
    u = snapshot.unit(site.uid)
    hdr = snapshot.header
    probs = []
    rec = rb.rec
    code = rb.body[:rec.tc_size]
    ref = relocate_unit(u, rb.new_tb).body[:u.tc_size]
    if code[:site.tail] != ref[:site.tail]:
        probs.append("prefix_body_changed")
    if code[site.tail + 11:u.tc_size] != ref[site.tail + 11:u.tc_size]:
        probs.append("post_tail_changed")
    if rb.body[rec.tc_size:] != u.search:
        probs.append("search_changed")
    rows_old = decode_search(u.search, u.icount)
    rows_new = decode_search(rb.body[rec.tc_size:], rec.icount)
    if list(rows_old) != list(rows_new):
        probs.append("rows_changed")
    st = rb.notes["stub"]["stub_off"]
    ins = sweep(code[st:], st, len(code))
    if any(i.bad for i in ins):
        probs.append("stub_undecodable")
    # every stub instruction is a cmp/jcc/jmp/lea/mov rdi,rbp/call/int3/nop: no store, no LDST
    allowed = {"cmp", "jne", "je", "jmp", "lea", "mov", "call", "int3", "nop"}
    for i in ins:
        if i.mnemonic not in allowed:
            probs.append(f"stub_insn:{i.mnemonic}")
        if i.mnemonic == "mov" and i.op_str != "rdi, rbp":
            probs.append(f"stub_mov:{i.op_str}")
        if i.mnemonic == "call" and not (i.rip_mem and i.rip_target == site.pool_slot):
            probs.append("stub_call_not_lookup")
    for n in (0, 1):
        if rec.jmp_insn_offset[n] == SLOT_FREE:
            continue
        a = rec.jmp_insn_offset[n]
        if (hdr.code_address + rec.tc_offset + a) % 4:
            probs.append(f"slot{n}_misaligned")
        if code[a - 1] != 0xE9 or code[a:a + 4] != b"\0\0\0\0":
            probs.append(f"slot{n}_not_reset_jmp")
        if rec.jmp_reset_offset[n] != a + 4:
            probs.append(f"slot{n}_reset_offset")
        # reset path: lea rax,[rip+x] == tb + n ; jmp tb_ret
        r = rec.jmp_reset_offset[n]
        lea = code[r:r + 7]
        tgt = rec.tc_offset + r + 7 + int.from_bytes(lea[3:7], "little", signed=True)
        if lea[:3] != b"\x48\x8d\x05" or tgt != rec.tb_offset + n:
            probs.append(f"slot{n}_reset_lea")
    F = rb.notes["stub"]["F_off"]
    if code[F:F + 3] != TAIL_MOV or code[F + 3:F + 5] != b"\xff\x15":
        probs.append("F_not_tail")
    jt = code[site.tail:site.tail + 11]
    if jt[0] != 0xE9 or site.tail + 5 + int.from_bytes(jt[1:5], "little", signed=True) != st \
            or jt[5:] != b"\xcc" * 6:
        probs.append("tail_patch")
    return probs


# ---------------------------------------------------------------------------
# CLI: build the three B3-side copies
# ---------------------------------------------------------------------------

def build_copies(snapshot, profile, elf_path, out_dir: Path, *, coverage: float = 0.99,
                 modes=("gce", "gce-fail", "lk2x")) -> dict[str, Any]:
    rt = snapshot.runtime or {}
    if rt.get("qemu_sha256") != GATE_EXE_SHA256:
        raise ProbeError("guard_layout_unbound", f"snapshot runtime {rt.get('qemu_sha256')}")
    elf = GuestElf(elf_path)
    sites, stats = admit(snapshot, profile, elf)
    top = top_set(sites, coverage)
    tb_ret = tb_ret_abs(snapshot)
    nruns = profile["collection"].get("repetitions") or len(profile["collection"].get("runs", [])) or 1
    model = {
        "label": "MODEL (train steady profile, merged runs; not time)",
        "merged_runs": nruns,
        "steady_lookup_hits": stats["steady_lookup_hits"],
        "admissible_weight": stats["admissible_weight"],
        "top_set_units": len(top), "top_set_coverage_target": coverage,
        "top_set_weight": sum(s.weight for s in top),
        "guarded_weight": sum(s.guarded_weight for s in top),
        "guarded_by_class": {c: sum(s.guarded_weight for s in top if s.exit_class == c)
                             for c in ("return", "static")},
        "units_by_class": dict(Counter(s.exit_class for s in top)),
        "targets_per_unit": dict(Counter(len(s.targets) for s in top)),
    }
    model["removed_lookup_share_of_all_lookups"] = model["guarded_weight"] / max(1, stats["steady_lookup_hits"])
    model["removed_lookups_per_run"] = model["guarded_weight"] / nruns
    model["fail_path_execs_per_run"] = (model["top_set_weight"] - model["guarded_weight"]) / nruns
    model["stub_execs_per_run"] = model["top_set_weight"] / nruns
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"label": LABEL, "admission": stats, "model": model, "copies": {}}
    for mode in modes:
        placer = Placer(snapshot.units(), snapshot.header)
        rebuilt, problems = {}, {}
        for s in top:
            rb = grow_site(snapshot, s, placer, mode=mode, tb_ret=tb_ret)
            p = check_grown(snapshot, s, rb)
            if p:
                problems[s.uid] = p
            rebuilt[s.uid] = rb
        if problems:
            raise ProbeError("static_check_failed", json.dumps(problems)[:2000])
        path = out_dir / mode / "cache.sc"
        path.parent.mkdir(parents=True, exist_ok=True)
        w = write_copy(path, snapshot, rebuilt)
        result["copies"][mode] = {
            "sc": str(path), "label": LABEL, **w, "moved_units": len(placer.moved),
            "in_place_units": len(rebuilt) - len(placer.moved),
            "stub_bytes": sum(r.notes["stub"]["len"] for r in rebuilt.values()),
            "units": {uid: {**r.notes, "new_tb": hex(r.new_tb), "old_tb": hex(r.old_tb)}
                      for uid, r in rebuilt.items()},
        }
    result["sites"] = [{"uid": s.uid, "class": s.exit_class, "insn": s.insn, "weight": s.weight,
                        "guarded_weight": s.guarded_weight, "free_slots": list(s.free_slots),
                        "targets": [{**t, "vaddr": hex(t["vaddr"]), "flags": hex(t["flags"]),
                                     "cs_base": hex(t["cs_base"])} for t in s.targets],
                        "in_top_set": s in top} for s in sites]
    return result


def main(argv: list[str] | None = None) -> int:
    from scopt.importer.import_cache import open_snapshot
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--elf", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--coverage", type=float, default=0.99)
    ap.add_argument("--modes", default="gce,gce-fail,lk2x")
    a = ap.parse_args(argv)
    snap = open_snapshot(a.snapshot)
    prof = json.loads(Path(a.profile).read_text())
    if prof.get("snapshot_id") != snap.snapshot_id:
        raise ProbeError("profile_snapshot_mismatch")
    res = build_copies(snap, prof, a.elf, Path(a.out), coverage=a.coverage,
                       modes=tuple(a.modes.split(",")))
    (Path(a.out) / "gce_probe.json").write_text(json.dumps(res, indent=1, sort_keys=True) + "\n")
    print(json.dumps({"model": res["model"], "admission": res["admission"],
                      "copies": {k: {kk: vv for kk, vv in v.items() if kk != "units"}
                                 for k, v in res["copies"].items()}}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

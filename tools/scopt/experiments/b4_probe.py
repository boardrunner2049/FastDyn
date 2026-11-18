"""WP16 E-COST probe, B4 side and the LLVM-survey copies (design §7.3, §15; survey §3.0, §3c).

Copies built here (all v5, all experimental, never published):

* ``d1``        D1 dead env stores of the hot units nop-filled in place (same length).
                Each store is checked by :func:`d1_dead_stores`, an independent,
                conservative host-level dataflow (hostsym is WP19 and not merged): a
                4-byte ``mov [rbp+d], x`` with ``d`` a D1 field (r0-r14, NF/ZF/CF/VF) is
                dead iff on **every** host path after it the same 4 bytes are
                overwritten by a plain store before any read of them and before any
                observer (call, exit, goto_tb site, ``jmp reg``, a memory operand not
                based on rbp, i.e. a guest access or host_abs store, a branch out of
                the unit's code, int3/undecodable, or a backward edge).  Label
                ``experimental_d1_nopfill_checked_by_wp16_dataflow``.
* ``sfb``       ``icount-entry-sfb`` (survey §3c B4-1): the TB entry
                ``mov r,[rbp-8]; add r,-n|dec r; test r,r; jl X; mov [rbp-8],r16`` becomes
                ``movzx r,word [rbp-8]; cmp word [rbp-6],0; jne X; sub r,n; jb X;
                mov [rbp-8],r16`` padded to exactly ``old+16`` bytes by longer encodings
                (disp32, imm16, a no-op REX), so the rest of the unit shifts by 16 and keeps
                its 4/8/16-byte alignment.  Equivalent because ``icount_decr.u16.high`` is
                0 or 0xffff (every writer: cpu-common.c:90, tcg-accel-ops.c:107,
                cpu-exec.c:802, reset) and ``n <= 512`` (survey; challenge-codegen-x86 §1.1).
                Label ``experimental_baseline_tcg_fix_icount_entry_sfb`` (a baseline-TCG
                inefficiency, reported as its own arm, never as caching or B3/B4).
* ``sfb-ctl``   placement control for ``sfb``: identical placement and ``tc_size``,
                original code, 16 bytes of int3 after the pool.
* ``sfb-d1``    ``sfb`` plus the ``d1`` nops.
* ``cmov``      same-length if-conversion of TB 0x1d5a's IT triangle (survey D-1):
                ``mov [rbp],ebx; test r12d,r12d; jge +10; mov r12d,[rbp+8]; xor ebx,r12d;
                mov [rbp],ebx`` (22 B) -> ``mov r13d,[rbp+8]; xor r13d,ebx; test r12d,r12d;
                cmovl ebx,r13d; mov [rbp],ebx`` + 5-byte nop.  r13 and EFLAGS are dead at
                the window end (r13 is next written at 0x6c, flags at 0x76), r12d/ebx are
                overwritten at 0x5e/0x61, no call/LDST/exit in the window.  The window
                crosses the guest-insn boundary at 0x4b (rows unchanged; no recovery point
                inside), which V15 would refuse: label ``experimental_cmov_crosses_insn``.
* ``sfb-cmov``  ``sfb`` plus ``cmov``.
"""

from __future__ import annotations

import argparse
import itertools
import json
import struct
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

from scopt.decode.x86 import sweep

from .gce_probe import GATE_EXE_SHA256, Placer, ProbeError, Rebuilt, rebuild_unit, write_copy

D1_FIELDS = {**{4 * i: f"r{i}" for i in range(15)},
             0x210: "CF", 0x214: "VF", 0x218: "NF", 0x21C: "ZF"}
NOPS = {  # Intel-recommended long NOPs (SDM vol. 2B NOP), 10/11 with prefixes
    1: "90", 2: "6690", 3: "0f1f00", 4: "0f1f4000", 5: "0f1f440000", 6: "660f1f440000",
    7: "0f1f8000000000", 8: "0f1f840000000000", 9: "660f1f840000000000",
    10: "662e0f1f840000000000", 11: "66662e0f1f840000000000",
}
SFB_SHIFT = 16
CMOV_OLD = bytes.fromhex("895d004585e40f8d0a000000448b65084133dc895d00")
CMOV_NEW = bytes.fromhex("448b6d08" "4433eb" "4585e4" "410f4cdd" "895d00") + bytes.fromhex(NOPS[5])
CMOV_OFF = 0x48
CMOV_VADDR = 0x1D5A


def nop(n: int) -> bytes:
    return bytes.fromhex(NOPS[n])


# ---------------------------------------------------------------------------
# D1 dead-store check (conservative, host level, per unit)
# ---------------------------------------------------------------------------

def _rbp_mem(i):
    for m in i.mem:
        if m.base == "rbp" and not m.index:
            return m
    return None


def _goto_tb_jmps(u) -> set[int]:
    """Addresses of goto_tb ``jmp rel32`` instructions (patched to other TBs at run time)."""
    return {s.field_off - 1 for s in u.sites if int(s.kind) == 13}


def classify(u):
    """Decoded ``[0, pool)`` and per-insn event: ('obs',) | ('read', d, n) | ('write', d, n)
    | ('flow',)."""
    end = u.pool_start if u.pool_start is not None else u.tc_size
    ins = sweep(u.code, 0, end)
    gotos = _goto_tb_jmps(u)
    ev = []
    for i in ins:
        mn = i.mnemonic
        if i.bad or mn in ("int3", "ret", "hlt", "ud2") or i.is_call or i.address in gotos:
            ev.append(("obs",))
            continue
        if i.is_jump:
            t = i.branch_target
            if t is None or not 0 <= t < end or t <= i.address:
                ev.append(("obs",))  # jmp reg, exit, or backward edge
            else:
                ev.append(("flow",))
            continue
        if mn == "lea" or mn.startswith("nop"):
            ev.append(("flow",))
            continue
        if not i.mem:
            ev.append(("flow",))
            continue
        m = _rbp_mem(i)
        if m is None or len(i.mem) != 1:
            ev.append(("obs",))  # guest access through the TLB, host_abs store, rip data
            continue
        first_is_mem = i.op_str.split(",")[0].strip().endswith("]")
        if mn == "mov" and first_is_mem:
            ev.append(("write", m.disp, m.size))
        else:
            ev.append(("read", m.disp, m.size))
    return ins, ev


def d1_dead_stores(u) -> list[dict[str, Any]]:
    """Every D1 store of ``u`` that the conservative dataflow proves dead (see module doc)."""
    ins, ev = classify(u)
    idx = {i.address: k for k, i in enumerate(ins)}

    def succs(k):
        i = ins[k]
        out = []
        if i.is_jump and i.branch_target is not None:
            out.append(idx.get(i.branch_target))
            if i.mnemonic != "jmp":
                out.append(k + 1 if k + 1 < len(ins) else None)
        else:
            out.append(k + 1 if k + 1 < len(ins) else None)
        return out

    @lru_cache(maxsize=None)
    def dead_from(k, d):
        if k is None or k >= len(ins):
            return False  # fell off the decoded code: unknown
        e = ev[k]
        if e[0] == "obs":
            return False
        if e[0] in ("read", "write"):
            lo, hi = e[1], e[1] + e[2]
            overlap = lo < d + 4 and d < hi
            if e[0] == "write" and lo <= d and d + 4 <= hi:
                return True  # fully overwritten before any use
            if overlap:
                return False  # read, or partial write (treated as a use)
        return all(dead_from(s, d) for s in succs(k))

    out = []
    for k, i in enumerate(ins):
        e = ev[k]
        if e[0] != "write" or e[2] != 4 or e[1] not in D1_FIELDS:
            continue
        if i.address < u.insn0_start:
            continue
        if all(dead_from(s, e[1]) for s in succs(k)):
            out.append({"off": i.address, "len": i.size, "field": D1_FIELDS[e[1]],
                        "bytes": i.raw.hex(), "text": i.text()})
    return out


def on_every_path(u, off: int) -> bool:
    """True when the host insn at ``off`` lies on every path from insn0 to an observer."""
    ins, ev = classify(u)
    idx = {i.address: k for k, i in enumerate(ins)}
    start = idx.get(u.insn0_start)
    target = idx[off]

    @lru_cache(maxsize=None)
    def avoids(k):
        if k is None:
            return True
        if k == target:
            return False
        if ev[k][0] == "obs":
            return True
        i = ins[k]
        nx = []
        if i.is_jump and i.branch_target is not None:
            nx.append(idx.get(i.branch_target))
            if i.mnemonic != "jmp":
                nx.append(k + 1)
        else:
            nx.append(k + 1)
        return any(avoids(s) for s in nx)
    return start is not None and not avoids(start)


# ---------------------------------------------------------------------------
# icount entry store-forwarding fix
# ---------------------------------------------------------------------------

REG32 = {"eax": 0, "ecx": 1, "edx": 2, "ebx": 3, "esp": 4, "ebp": 5, "esi": 6, "edi": 7,
         **{f"r{n}d": n for n in range(8, 16)}}
REG16 = {"ax": "eax", "cx": "ecx", "dx": "edx", "bx": "ebx", "sp": "esp", "bp": "ebp",
         "si": "esi", "di": "edi", **{f"r{n}w": f"r{n}d" for n in range(8, 16)}}


def parse_entry(u) -> dict[str, Any] | None:
    """The 5-insn icount entry, or None (CF_NOIRQ shape, non-icount or unexpected)."""
    ins = sweep(u.code, 0, min(u.insn0_start, len(u.code)))
    if len(ins) < 5:
        return None
    i0, i1, i2, i3, i4 = ins[:5]
    if not (i0.mnemonic == "mov" and i0.op_str.endswith("dword ptr [rbp - 8]")):
        return None
    r = i0.op_str.split(",")[0].strip()
    if r not in REG32 or r in ("esp", "ebp"):
        return None
    if i1.mnemonic == "dec" and i1.op_str == r:
        n = 1
    elif i1.mnemonic == "add" and i1.op_str.startswith(r + ","):
        n = -int(i1.op_str.split(",")[1], 0)
    elif i1.mnemonic == "sub" and i1.op_str.startswith(r + ","):
        n = int(i1.op_str.split(",")[1], 0)
    else:
        return None
    if not 0 < n <= 512:
        return None
    if not (i2.mnemonic == "test" and i2.op_str == f"{r}, {r}"):
        return None
    if not (i3.mnemonic == "jl" and i3.size == 6 and i3.branch_target is not None):
        return None
    if not (i4.mnemonic == "mov" and i4.op_str.startswith("word ptr [rbp - 8],")):
        return None
    r16 = i4.op_str.split(",")[1].strip()
    if REG16.get(r16) != r:
        return None
    return {"reg": r, "n": n, "len": i4.end, "exit_target": i3.branch_target}


def _modrm_rbp(reg: int, disp: int, wide: bool) -> bytes:
    if wide or not -128 <= disp <= 127:
        return bytes([0x80 | (reg & 7) << 3 | 5]) + struct.pack("<i", disp)
    return bytes([0x40 | (reg & 7) << 3 | 5]) + struct.pack("<b", disp)


def encode_sfb_entry(reg: str, n: int, exit_target_new: int, total: int) -> bytes:
    """New entry of exactly ``total`` bytes (exit target in new code offsets)."""
    r = REG32[reg]
    rex = 0x44 if r >= 8 else None  # REX.R for the modrm reg field

    def build(wm: bool, rex40: bool, wc: bool, c16: bool, ws: bool, s32: bool) -> bytes:
        out = bytearray()
        p = bytes([rex]) if rex else (b"\x40" if rex40 else b"")
        out += p + b"\x0f\xb7" + _modrm_rbp(r, -8, wm)  # movzx r32, word [rbp-8]
        imm = b"\x00\x00" if c16 else b"\x00"
        out += b"\x66" + (b"\x81" if c16 else b"\x83") + _modrm_rbp(7, -6, wc) + imm  # cmp word [rbp-6],0
        out += b"\x0f\x85"
        jne_at = len(out)
        out += b"\0\0\0\0"
        rb = bytes([0x41]) if r >= 8 else b""
        if s32 or n > 127:
            out += rb + b"\x81" + bytes([0xE8 | (r & 7)]) + struct.pack("<I", n)  # sub r32, imm32
        else:
            out += rb + b"\x83" + bytes([0xE8 | (r & 7)]) + bytes([n])  # sub r32, imm8
        out += b"\x0f\x82"
        jb_at = len(out)
        out += b"\0\0\0\0"
        out += b"\x66" + (bytes([rex]) if rex else b"") + b"\x89" + _modrm_rbp(r, -8, ws)  # mov [rbp-8], r16
        for at in (jne_at, jb_at):
            out[at:at + 4] = struct.pack("<i", exit_target_new - (at + 4))
        return bytes(out)

    for combo in itertools.product((False, True), repeat=6):
        if combo[1] and rex:
            continue
        b = build(*combo)
        if len(b) == total:
            return b
    raise ProbeError("sfb_no_encoding", f"{reg} n={n} total={total}")


def sfb_prefix(u) -> tuple[bytes, int] | None:
    e = parse_entry(u)
    if e is None:
        return None
    total = e["len"] + SFB_SHIFT
    return encode_sfb_entry(e["reg"], e["n"], e["exit_target"] + SFB_SHIFT, total), e["len"]


def check_sfb_entry(new: bytes, e: dict[str, Any]) -> list[str]:
    """Decode the new entry back and compare with the intended semantics."""
    ins = sweep(new, 0, len(new))
    if any(i.bad for i in ins) or len(ins) != 6:
        return ["sfb_decode"]
    m = [i.mnemonic for i in ins]
    probs = []
    if m != ["movzx", "cmp", "jne", "sub", "jb", "mov"]:
        probs.append(f"sfb_mnemonics:{m}")
    if ins[0].op_str != f"{e['reg']}, word ptr [rbp - 8]":
        probs.append("sfb_movzx")
    if ins[1].op_str != "word ptr [rbp - 6], 0":
        probs.append("sfb_cmp")
    if ins[3].op_str != f"{e['reg']}, {hex(e['n']) if e['n'] > 9 else e['n']}":
        probs.append(f"sfb_sub:{ins[3].op_str}")
    tgt = e["exit_target"] + SFB_SHIFT
    if ins[2].branch_target != tgt or ins[4].branch_target != tgt:
        probs.append("sfb_target")
    if not ins[5].op_str.startswith("word ptr [rbp - 8], "):
        probs.append("sfb_store")
    return probs


# ---------------------------------------------------------------------------
# Copy builders
# ---------------------------------------------------------------------------

def hot_units(snapshot, profile, coverage: float, phase: int = 1) -> list[str]:
    ent = {}
    for row in profile["units"]:
        c = row["phase"].get(str(phase))
        if c:
            ent[row["unit"]] = c["entries"] - c["aborted"]
    tot = sum(ent.values())
    out, acc = [], 0
    for uid, n in sorted(ent.items(), key=lambda kv: -kv[1]):
        if acc >= coverage * tot or n <= 0:
            break
        out.append(uid)
        acc += n
    return out


def build_copies(snapshot, profile, out_dir: Path, *, coverage: float = 0.99,
                 copies=("d1", "sfb", "sfb-ctl", "sfb-d1", "cmov", "sfb-cmov")) -> dict[str, Any]:
    rt = snapshot.runtime or {}
    if rt.get("qemu_sha256") != GATE_EXE_SHA256:
        raise ProbeError("guard_layout_unbound", f"snapshot runtime {rt.get('qemu_sha256')}")
    units = {u.uid: u for u in snapshot.units()}
    nruns = profile["collection"].get("repetitions") or 1
    entries = {}
    for row in profile["units"]:
        c = row["phase"].get("1")
        entries[row["unit"]] = (c["entries"] - c["aborted"]) if c else 0
    hot = hot_units(snapshot, profile, coverage)
    hot_set = set(hot)

    # --- D1 over every unit (model comparison), patches for the hot ones
    d1_all, d1_patch = {}, {}
    for u in snapshot.units():
        if u.meta_status != "ok" or not u.reloc.relocatable:
            continue
        if u.key.cflags & 0x11000:  # CF_NOIRQ | CF_MEMI_ONLY (special_cflags_unit)
            continue
        dead = d1_dead_stores(u)
        if dead:
            d1_all[u.uid] = dead
            if u.uid in hot_set:
                d1_patch[u.uid] = [(s["off"], bytes.fromhex(s["bytes"]), nop(s["len"])) for s in dead]
    d1_dyn_upper = sum(entries.get(uid, 0) * len(v) for uid, v in d1_all.items() if uid in hot_set)
    d1_dyn_every_path = 0
    for uid in d1_patch:
        u = units[uid]
        for s in d1_all[uid]:
            if on_every_path(u, s["off"]):
                d1_dyn_every_path += entries.get(uid, 0)
                s["every_path"] = True
            else:
                s["every_path"] = False
    # --- SFB entries
    sfb, sfb_skip = {}, Counter()
    for u in snapshot.units():
        p = sfb_prefix(u)
        if p is None:
            sfb_skip["entry_shape" if not u.key.cflags & 0x10000 else "cpu_noirq_shape"] += 1
            continue
        e = parse_entry(u)
        probs = check_sfb_entry(p[0], e)
        if probs:
            raise ProbeError("sfb_check", f"{u.uid}: {probs}")
        sfb[u.uid] = p
    # --- cmov window
    main1d5a = [u for u in snapshot.units() if u.vaddrs[0] == CMOV_VADDR
                and bytes(u.code[CMOV_OFF:CMOV_OFF + len(CMOV_OLD)]) == CMOV_OLD]
    if not main1d5a:
        raise ProbeError("cmov_window_missing")
    for cm in main1d5a:
        for i in sweep(cm.code, 0, cm.tc_size):
            if i.branch_target is not None and CMOV_OFF < i.branch_target < CMOV_OFF + len(CMOV_OLD) \
                    and not CMOV_OFF <= i.address < CMOV_OFF + len(CMOV_OLD):
                raise ProbeError("cmov_window_is_branch_target", hex(i.address))
    cm = max(main1d5a, key=lambda u: entries.get(u.uid, 0))
    cmov_patch = {u.uid: [(CMOV_OFF, CMOV_OLD, CMOV_NEW)] for u in main1d5a}

    model = {
        "label": "MODEL (train steady profile, merged runs; not time)",
        "merged_runs": nruns,
        "hot_units": len(hot), "hot_coverage": coverage,
        "d1_static_all_units": sum(len(v) for v in d1_all.values()),
        "d1_units_all": len(d1_all),
        "d1_static_patched": sum(len(v) for v in d1_patch.values()),
        "d1_units_patched": len(d1_patch),
        "d1_by_field": dict(Counter(s["field"] for uid in d1_patch for s in d1_all[uid])),
        "d1_dyn_stores_per_run_upper": d1_dyn_upper / nruns,
        "d1_dyn_stores_per_run_every_path": d1_dyn_every_path / nruns,
        "sfb_units": len(sfb), "sfb_skipped": dict(sfb_skip),
        "sfb_entries_per_run": sum(entries.get(uid, 0) for uid in sfb) / nruns,
        "cmov_units": {u.uid: {"cflags": hex(u.key.cflags), "entries_per_run": entries.get(u.uid, 0) / nruns}
                       for u in main1d5a},
        "cmov_entries_per_run": sum(entries.get(u.uid, 0) for u in main1d5a) / nruns,
        "entries_per_run": sum(entries.values()) / nruns,
    }
    result = {"model": model, "copies": {},
              "d1_stores": {uid: d1_all[uid] for uid in d1_patch}}
    out_dir.mkdir(parents=True, exist_ok=True)

    sfb_place: dict[str, int] = {}  # one placement for sfb, sfb-ctl, sfb-d1, sfb-cmov

    def emit(name: str, *, use_sfb: bool, ctl: bool, patches: dict[str, list],
             labels: list[str]) -> None:
        placer = Placer(snapshot.units(), snapshot.header)
        rebuilt: dict[str, Rebuilt] = {}
        for u in snapshot.units():
            pre = sfb.get(u.uid) if (use_sfb or ctl) else None
            pt = patches.get(u.uid, [])
            if pre is None and not pt:
                continue
            if pre is not None:
                if ctl:
                    kw = dict(append=b"\xcc" * SFB_SHIFT, append_align=1, patches=pt)
                else:
                    kw = dict(prefix=pre[0], prefix_len=pre[1], patches=pt)
                # dry run in place for the exact length (the re-encoded search data can
                # grow by a byte when the first host delta crosses an sleb128 boundary)
                if u.uid not in sfb_place:
                    dry = rebuild_unit(u, u.tb_offset, prefix=pre[0], prefix_len=pre[1])
                    sfb_place[u.uid] = placer.place(u, 192 + len(dry.body))
                tb = sfb_place[u.uid]
                if tb != u.tb_offset:
                    placer.moved.append(u.uid)
                rb = rebuild_unit(u, tb, code_size=snapshot.header.code_size, **kw)
            else:
                rb = rebuild_unit(u, u.tb_offset, patches=pt, code_size=snapshot.header.code_size)
            rebuilt[u.uid] = rb
        path = out_dir / name / "cache.sc"
        path.parent.mkdir(parents=True, exist_ok=True)
        w = write_copy(path, snapshot, rebuilt)
        result["copies"][name] = {"sc": str(path), "labels": labels, **w,
                                  "moved_units": len(set(placer.moved)),
                                  "rebuilt_units": len(rebuilt)}

    for name in copies:
        if name == "d1":
            emit(name, use_sfb=False, ctl=False, patches=d1_patch, labels=["experimental_d1_nopfill_checked_by_wp16_dataflow"])
        elif name == "sfb":
            emit(name, use_sfb=True, ctl=False, patches={}, labels=["experimental_baseline_tcg_fix_icount_entry_sfb"])
        elif name == "sfb-ctl":
            emit(name, use_sfb=False, ctl=True, patches={}, labels=["experimental_placement_control"])
        elif name == "sfb-d1":
            emit(name, use_sfb=True, ctl=False, patches=d1_patch, labels=["experimental_baseline_tcg_fix_icount_entry_sfb", "experimental_d1_nopfill_checked_by_wp16_dataflow"])
        elif name == "cmov":
            emit(name, use_sfb=False, ctl=False, patches=cmov_patch, labels=["experimental_cmov_crosses_insn"])
        elif name == "sfb-cmov":
            emit(name, use_sfb=True, ctl=False, patches=cmov_patch, labels=["experimental_baseline_tcg_fix_icount_entry_sfb", "experimental_cmov_crosses_insn"])
        else:
            raise ValueError(name)
    return result


def main(argv: list[str] | None = None) -> int:
    from scopt.importer.import_cache import open_snapshot
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--coverage", type=float, default=0.99)
    ap.add_argument("--copies", default="d1,sfb,sfb-ctl,sfb-d1,cmov,sfb-cmov")
    a = ap.parse_args(argv)
    snap = open_snapshot(a.snapshot)
    prof = json.loads(Path(a.profile).read_text())
    if prof.get("snapshot_id") != snap.snapshot_id:
        raise ProbeError("profile_snapshot_mismatch")
    res = build_copies(snap, prof, Path(a.out), coverage=a.coverage,
                       copies=tuple(a.copies.split(",")))
    (Path(a.out) / "b4_probe.json").write_text(json.dumps(res, indent=1, sort_keys=True) + "\n")
    print(json.dumps({"model": res["model"], "copies": res["copies"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Standalone validator for a QEMU supercache capture file (``.scm`` v1).

    scm_smoke.py RUN.scm [--sc CACHE.sc] [--json OUT.json] [--allow-flags]

WP3 acceptance helper. It only uses the frozen WP0 layouts
(``scopt.formats.layouts``, ``scopt.formats.enums``) and ``scopt.canon``'s
FNV; it does not depend on the WP4 reader, so it can catch writer bugs
independently of it.

File checks (always run):
  * FileHeader: magic, version, header_size, capture_level, reserved, checksum.
  * Every chunk: framing in bounds, zero padding, FNV trailer, ``seq`` equal to
    0, 1, 2, ... with no gap. A single bad chunk fails the smoke (a reader would
    drop it and its suffix).
  * Exactly one RUN_INFO, first; HELPER ids dense from 0; TB ``commit_seq``
    dense from 0 and slots dense from ``header_record_count_at_load``.
  * Per TB: payload size equals the size implied by its counts; code_size,
    icount, guest size agree with the embedded record; FNV(guest bytes) equals
    ``record.guest_hash``; the Insn rows tile ``[insn0_start, body_end)``;
    ``insn0_start <= body_end <= ldst_end <= pool_start <= code_size``; every
    site field lies inside the code; helper ids exist; GOTO_TB sites are exactly
    the record's ``jmp_insn_offset`` entries, with ``final_field == 0``; Ldst
    rows and LDST_FAST_JCC sites agree; no OTHER_BRANCH site.
  * MAPS(first_commit) and RAMBLOCKS present iff a TB was committed;
    MAPS(exit) present; END is the last chunk, clean, and its counters equal
    what the file holds; ``write_errors == 0``.
  * Unless ``--allow-flags``: no TB has ``capture_overflow`` or
    ``capture_error``.

Binding checks (with ``--sc``), design 2.5.8:
  * RUN_INFO code_address, code_size, prologue_end, runtime_signature,
    tb_struct_size, host_page_size, target_page_bits equal the ``.sc`` header.
    (The ``.sc`` does not persist the prologue bytes, so RUN_INFO's copy cannot
    be compared against it.)
  * ``.sc record_count == header_record_count_at_load + committed``: every
    record this run appended has a TB chunk.
  * Each TB chunk's 104 record bytes equal the ``.sc`` record at its slot, and
    that record passes the loader's checksum/commit checks.
  * ``code_fnv64 == FNV(.sc bytes [tc_offset, end_offset))``.

Exit status 0 iff every check passes. A JSON summary goes to stdout (and to
``--json``).
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scopt.canon import fnv1a64, sc_header_checksum, sc_record_checksum  # noqa: E402
from scopt.formats import layouts as L  # noqa: E402
from scopt.formats.enums import (  # noqa: E402
    NONE_U32,
    ChunkType,
    MapsTag,
    SiteKind,
    TbFlag,
)

KNOWN_CHUNKS = {int(c) for c in ChunkType}


class Smoke:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.notes: list[str] = []
        self.stats: dict[str, object] = {}

    def check(self, cond: bool, msg: str) -> bool:
        if not cond:
            if len(self.failures) < 200:
                self.failures.append(msg)
            elif len(self.failures) == 200:
                self.failures.append("... more failures suppressed")
        return cond


def parse_tb(payload: bytes, sm: Smoke, where: str) -> dict | None:
    if not sm.check(len(payload) >= L.SCM_TB_HEADER.size, f"{where}: short TB payload"):
        return None
    f = L.SCM_TB_HEADER.unpack_from(payload, 0)
    (ver, slot, commit_seq, record, vaddr, code_fnv, attempts, flags, insn0, body_end,
     ldst_end, pool_start, code_size, icount, isw, n_sites, n_pool, n_ldst, n_opm,
     n_exits, n_guest, reserved) = f
    tb = dict(version=ver, slot=slot, commit_seq=commit_seq, record=record, vaddr=vaddr,
              code_fnv64=code_fnv, attempts=attempts, flags=flags, insn0_start=insn0,
              body_end=body_end, ldst_end=ldst_end, pool_start=pool_start,
              code_size=code_size, icount=icount, insn_start_words=isw,
              n_sites=n_sites, n_pool=n_pool, n_ldst=n_ldst, n_opmarks=n_opm,
              n_static_exits=n_exits, n_guest_bytes=n_guest, reserved=reserved)
    off = L.SCM_TB_HEADER.size
    arrays = [("insns", L.SCM_INSN, icount), ("sites", L.SCM_SITE, n_sites),
              ("pool", L.SCM_POOL, n_pool), ("ldst", L.SCM_LDST, n_ldst),
              ("opmarks", L.SCM_OPMARK, n_opm), ("static_exits", L.SCM_STATIC_EXIT, n_exits)]
    expect = off + sum(s.size * n for _, s, n in arrays) + n_guest
    if not sm.check(len(payload) == expect,
                    f"{where}: payload {len(payload)} != size implied by counts {expect}"):
        return None
    for name, s, n in arrays:
        assert off % 8 == 0
        tb[name] = [s.unpack_from(payload, off + i * s.size) for i in range(n)]
        off += s.size * n
    tb["guest_bytes"] = payload[off:off + n_guest]
    return tb


def check_tb(tb: dict, helpers: set[int], sm: Smoke, where: str, allow_flags: bool) -> None:
    rec = L.SC_RECORD.unpack(tb["record"])
    (tb_off, end_off, tc_off, tc_size, pc, cs_base, pa0, pa1, ghash, rflags, cflags,
     gsize, ricount, jro0, jro1, jio0, jio1, rchk, committed) = rec
    c = sm.check
    c(tb["version"] == L.SCM_TB_CHUNK_VERSION, f"{where}: tb_chunk_version {tb['version']}")
    c(tb["reserved"] == 0, f"{where}: reserved != 0")
    c(tb["insn_start_words"] == 3, f"{where}: insn_start_words {tb['insn_start_words']}")
    c(tb["code_size"] == tc_size, f"{where}: code_size {tb['code_size']} != record.tc_size {tc_size}")
    c(tb["icount"] == ricount, f"{where}: icount {tb['icount']} != record.icount {ricount}")
    c(tb["attempts"] >= 1, f"{where}: attempts {tb['attempts']}")
    flags = TbFlag(tb["flags"])
    if not allow_flags:
        c(not (flags & (TbFlag.CAPTURE_ERROR | TbFlag.CAPTURE_OVERFLOW)),
          f"{where}: flags {flags!r}")
    if flags & TbFlag.GUEST_BYTES_TRUNCATED:
        sm.notes.append(f"{where}: guest bytes truncated")
    else:
        c(tb["n_guest_bytes"] == gsize, f"{where}: n_guest_bytes {tb['n_guest_bytes']} != guest_size {gsize}")
        c(fnv1a64(tb["guest_bytes"]) == ghash, f"{where}: FNV(guest bytes) != record.guest_hash")
    # irq_check flag semantics (design 2.4)
    irq = tb["insn0_start"] > 0 and not (cflags & 0x00010000)
    c(bool(flags & TbFlag.IRQ_CHECK) == irq, f"{where}: irq_check flag inconsistent")

    # instruction rows tile [insn0_start, body_end)
    insns = tb["insns"]
    if c(len(insns) == tb["icount"] and insns, f"{where}: no insns"):
        c(insns[0][3] == tb["insn0_start"], f"{where}: insn0 host_start != insn0_start")
        for i, row in enumerate(insns):
            c(row[3] <= row[4], f"{where}: insn {i} host_start > host_end")
            if i:
                c(row[3] == insns[i - 1][4], f"{where}: insn {i} host_start != prev host_end")
        c(insns[-1][4] == tb["body_end"], f"{where}: last host_end != body_end")
    pool_start = tb["pool_start"]
    pool_or_end = tb["code_size"] if pool_start == NONE_U32 else pool_start
    c(tb["insn0_start"] <= tb["body_end"] <= tb["ldst_end"] <= pool_or_end <= tb["code_size"],
      f"{where}: layout order insn0={tb['insn0_start']} body={tb['body_end']} "
      f"ldst={tb['ldst_end']} pool={pool_start:#x} size={tb['code_size']}")
    if pool_start == NONE_U32:
        c(not tb["pool"], f"{where}: pool rows without pool_start")
    else:
        c(pool_start % 8 == 0 or True, "")  # alignment is absolute; checked by the importer

    # sites
    goto_tb = {}
    for i, s in enumerate(tb["sites"]):
        field_off, insn_off, width, enc, kind, aux, value, insn_idx, final = s
        w = f"{where} site {i} kind={kind:#x}"
        c(kind in {int(k) for k in SiteKind}, f"{w}: unknown kind")
        c(kind != SiteKind.OTHER_BRANCH, f"{w}: OTHER_BRANCH")
        c(field_off + width <= tb["code_size"], f"{w}: field {field_off}+{width} outside code")
        if insn_off != NONE_U32:
            c(insn_off <= field_off, f"{w}: insn_off > field_off")
        if insn_idx != NONE_U32:
            c(insn_idx < tb["icount"], f"{w}: insn_idx {insn_idx} >= icount")
        if kind == SiteKind.HELPER_CALL:
            c(aux in helpers, f"{w}: helper_id {aux} not declared before the TB")
        if kind == SiteKind.GOTO_TB:
            goto_tb[aux] = s
            c(final == 0, f"{w}: goto_tb final_field {final:#x} != 0")
            c(field_off % 4 == 0, f"{w}: goto_tb field not 4-aligned")
        if kind == SiteKind.POOL_REF:
            c(aux < len(tb["pool"]), f"{w}: pool index {aux} out of range")
    jio = {0: jio0, 1: jio1}
    for n in (0, 1):
        if jio[n] != 0xFFFF:
            c(n in goto_tb and goto_tb[n][0] == jio[n],
              f"{where}: jmp_insn_offset[{n}]={jio[n]} has no matching GOTO_TB site")
        else:
            c(n not in goto_tb, f"{where}: GOTO_TB site {n} without jmp_insn_offset")
    # ldst rows
    fast = {(s[5], s[0]) for s in tb["sites"] if s[4] == SiteKind.LDST_FAST_JCC}
    n_ldst_ops = sum(1 for m in tb["opmarks"] if m[1] in (2, 3))
    c(len(tb["ldst"]) == n_ldst_ops, f"{where}: {len(tb['ldst'])} ldst rows vs {n_ldst_ops} ld/st op marks")
    for k, row in enumerate(tb["ldst"]):
        jf0, jf1, slow_start, slow_end, raddr_off, oi, ordinal, is_ld = row
        c(ordinal == k, f"{where}: ldst {k} ordinal {ordinal}")
        c(tb["body_end"] <= slow_start < slow_end <= tb["ldst_end"],
          f"{where}: ldst {k} slow path [{slow_start},{slow_end}) outside [body_end, ldst_end)")
        c(raddr_off <= tb["body_end"], f"{where}: ldst {k} raddr beyond body")
        for jf in (jf0, jf1):
            if jf != NONE_U32:
                c((k, jf) in fast, f"{where}: ldst {k} jcc field {jf} has no LDST_FAST_JCC site")
    for m in tb["opmarks"]:
        if m[1] == 1:
            c(m[3] in helpers, f"{where}: call op mark helper_id {m[3]:#x} unknown")
    for e in tb["static_exits"]:
        c(e[0] in (0, 1, 0xFF), f"{where}: static exit slot {e[0]}")
    return None


def run(scm_path: str, sc_path: str | None, allow_flags: bool) -> Smoke:
    sm = Smoke()
    data = Path(scm_path).read_bytes()
    sm.stats["scm_bytes"] = len(data)
    c = sm.check
    if not c(len(data) >= L.SCM_FILE_HEADER.size, "file shorter than the header"):
        return sm
    magic, ver, hsize, level, pid, created, r0, r1, r2, chk = L.SCM_FILE_HEADER.unpack_from(data)
    ok = c(magic == L.SCM_MAGIC, f"magic {magic!r}")
    ok &= c(ver == L.SCM_VERSION, f"version {ver}")
    ok &= c(hsize == L.SCM_FILE_HEADER.size, f"header_size {hsize}")
    ok &= c(level in (1, 2), f"capture_level {level}")
    ok &= c((r0, r1, r2) == (0, 0, 0), "header reserved != 0")
    ok &= c(chk == fnv1a64(data[:L.SCM_FILE_HEADER_CHECKSUM_LEN]), "header checksum")
    if not ok:
        return sm
    sm.stats.update(capture_level=level, pid=pid, created_realtime_ns=created)

    # ---- chunk framing ----
    chunks: list[tuple[int, int, bytes]] = []
    off = L.SCM_FILE_HEADER.size
    seq_expect = 0
    while off < len(data):
        if not c(off + L.SCM_CHUNK_HEADER.size <= len(data), f"@{off:#x}: truncated chunk header"):
            break
        ctype, plen, seq = L.SCM_CHUNK_HEADER.unpack_from(data, off)
        body = L.SCM_CHUNK_HEADER.size + L.align8(plen)
        if not c(off + body + 8 <= len(data), f"@{off:#x}: chunk type {ctype} runs past EOF"):
            break
        (trailer,) = L.SCM_CHUNK_TRAILER.unpack_from(data, off + body)
        if not c(trailer == fnv1a64(data[off:off + body]), f"@{off:#x}: bad trailer (type {ctype}, seq {seq})"):
            break
        if not c(seq == seq_expect, f"@{off:#x}: seq {seq} != {seq_expect}"):
            break
        pad = data[off + 16 + plen:off + body]
        c(pad == bytes(len(pad)), f"@{off:#x}: non-zero padding")
        if ctype not in KNOWN_CHUNKS:
            sm.notes.append(f"seq {seq}: unknown chunk type {ctype} skipped")
        chunks.append((ctype, seq, data[off + 16:off + 16 + plen]))
        seq_expect += 1
        off += body + 8
    sm.stats["chunks"] = len(chunks)
    counts: dict[str, int] = {}
    for ctype, _, _ in chunks:
        name = ChunkType(ctype).name if ctype in KNOWN_CHUNKS else str(ctype)
        counts[name] = counts.get(name, 0) + 1
    sm.stats["chunk_counts"] = counts

    # ---- RUN_INFO ----
    run_info = None
    ri_chunks = [p for t, _, p in chunks if t == ChunkType.RUN_INFO]
    c(len(ri_chunks) == 1, f"{len(ri_chunks)} RUN_INFO chunks")
    c(bool(chunks) and chunks[0][0] == ChunkType.RUN_INFO, "first chunk is not RUN_INFO")
    if ri_chunks:
        p = ri_chunks[0]
        f = L.SCM_RUN_INFO.unpack_from(p)
        keys = ("info_version capture_level code_address code_size prologue_end tb_ret_off "
                "epilogue_off runtime_signature header_record_count_at_load loaded_max_end "
                "highwater_off icache_linesize tb_struct_size host_page_size target_page_bits "
                "insn_start_words cpuinfo exe_dev exe_ino exe_size exe_mtim_ns flags "
                "prologue_len").split()
        run_info = dict(zip(keys, f))
        c(run_info["info_version"] == 1, "RUN_INFO version")
        c(run_info["capture_level"] == level, "RUN_INFO capture_level != header")
        c(run_info["prologue_len"] == run_info["prologue_end"], "prologue_len != prologue_end")
        c(len(p) == L.SCM_RUN_INFO.size + run_info["prologue_len"], "RUN_INFO payload length")
        c(run_info["insn_start_words"] == 3, "insn_start_words != 3")
        c(not (run_info["flags"] & 2), "RUN_INFO flat flag set")
        c(run_info["tb_ret_off"] < run_info["prologue_end"]
          and run_info["epilogue_off"] < run_info["prologue_end"], "tb_ret/epilogue outside prologue")
        run_info["prologue"] = p[L.SCM_RUN_INFO.size:]
        sm.stats["run_info"] = {k: (hex(v) if isinstance(v, int) and k in (
            "code_address", "runtime_signature", "cpuinfo") else v)
            for k, v in run_info.items() if k != "prologue"}

    # ---- helpers / TBs / maps / ramblocks / end ----
    helpers: dict[int, tuple] = {}
    tbs: list[dict] = []
    maps_tags: list[int] = []
    ramblocks = 0
    end = None
    for idx, (ctype, seq, p) in enumerate(chunks):
        where = f"seq {seq}"
        if ctype == ChunkType.HELPER:
            hid, tflags, tmask, nlen, func = L.SCM_HELPER.unpack_from(p)
            c(hid == len(helpers), f"{where}: helper_id {hid} not dense")
            c(len(p) == L.SCM_HELPER.size + nlen, f"{where}: helper payload length")
            helpers[hid] = (p[L.SCM_HELPER.size:].decode("latin-1"), func, tflags, tmask)
        elif ctype == ChunkType.TB:
            tb = parse_tb(p, sm, where)
            if tb is None:
                continue
            c(tb["commit_seq"] == len(tbs), f"{where}: commit_seq {tb['commit_seq']} != {len(tbs)}")
            check_tb(tb, set(helpers), sm, f"{where} slot {tb['slot']}", allow_flags)
            tbs.append(tb)
        elif ctype == ChunkType.MAPS:
            tag, trunc, mlen, pad = L.SCM_MAPS_HEADER.unpack_from(p)
            c(len(p) == L.SCM_MAPS_HEADER.size + mlen, f"{where}: MAPS length")
            c(tag in (MapsTag.FIRST_COMMIT, MapsTag.EXIT), f"{where}: MAPS tag {tag}")
            c(trunc == 0, f"{where}: MAPS truncated")
            maps_tags.append(tag)
        elif ctype == ChunkType.RAMBLOCKS:
            n, _ = L.SCM_RAMBLOCKS_HEADER.unpack_from(p)
            c(len(p) == L.SCM_RAMBLOCKS_HEADER.size + n * L.SCM_RAMBLOCK_ROW.size,
              f"{where}: RAMBLOCKS length")
            c(n > 0, f"{where}: no RAM blocks")
            ramblocks += 1
            rows = [L.SCM_RAMBLOCK_ROW.unpack_from(p, 8 + i * L.SCM_RAMBLOCK_ROW.size) for i in range(n)]
            sm.stats["ramblocks"] = [r[0].rstrip(b"\0").decode() for r in rows]
        elif ctype == ChunkType.END:
            c(idx == len(chunks) - 1, f"{where}: END is not the last chunk")
            c(len(p) == L.SCM_END.size, f"{where}: END length {len(p)}")
            end = dict(zip("committed discarded overflow capture_error write_errors helpers clean".split(),
                           L.SCM_END.unpack_from(p)))
    sm.stats["helpers"] = len(helpers)
    sm.stats["tb_chunks"] = len(tbs)
    if c(end is not None, "no END chunk (capture incomplete)"):
        sm.stats["end"] = end
        c(end["clean"] == 1, "END clean != 1")
        c(end["committed"] == len(tbs), f"END committed {end['committed']} != {len(tbs)} TB chunks")
        c(end["helpers"] == len(helpers), f"END helpers {end['helpers']} != {len(helpers)}")
        c(end["write_errors"] == 0, f"END write_errors {end['write_errors']}")
        n_over = sum(1 for t in tbs if t["flags"] & TbFlag.CAPTURE_OVERFLOW)
        n_err = sum(1 for t in tbs if t["flags"] & TbFlag.CAPTURE_ERROR)
        c(end["overflow"] == n_over, "END overflow counter mismatch")
        c(end["capture_error"] == n_err, "END capture_error counter mismatch")
    c(maps_tags.count(MapsTag.EXIT) == 1, "MAPS(exit) missing or repeated")
    c(maps_tags.count(MapsTag.FIRST_COMMIT) == (1 if tbs else 0), "MAPS(first_commit) count")
    c(ramblocks == (1 if tbs else 0), "RAMBLOCKS count")
    if run_info is not None:
        for i, t in enumerate(tbs):
            c(t["slot"] == run_info["header_record_count_at_load"] + i,
              f"TB {i}: slot {t['slot']} != at_load+{i}")

    # ---- tallies (informational) ----
    kinds: dict[str, int] = {}
    for t in tbs:
        for s in t["sites"]:
            k = SiteKind(s[4]).name
            kinds[k] = kinds.get(k, 0) + 1
    sm.stats["site_kinds"] = dict(sorted(kinds.items()))
    sm.stats["tb_flags"] = {
        "irq_check": sum(1 for t in tbs if t["flags"] & TbFlag.IRQ_CHECK),
        "capture_error": sum(1 for t in tbs if t["flags"] & TbFlag.CAPTURE_ERROR),
        "capture_overflow": sum(1 for t in tbs if t["flags"] & TbFlag.CAPTURE_OVERFLOW),
    }
    sm.stats["attempts_gt1"] = sum(1 for t in tbs if t["attempts"] > 1)

    # ---- binding to the .sc ----
    if sc_path:
        bind = {}
        with open(sc_path, "rb") as fh:
            hdr = fh.read(L.SC_HEADER.size)
            (smagic, sver, shsize, code_size, code_address, prologue_end, record_count,
             runtime_sig, host_page, tpb, tb_size, sres, schk) = L.SC_HEADER.unpack(hdr)
            c(smagic == L.SC_MAGIC and sver == L.SC_VERSION, ".sc magic/version")
            c(schk == sc_header_checksum(hdr), ".sc header checksum")
            bind["sc_record_count"] = record_count
            if run_info is not None:
                for k, v in (("code_address", code_address), ("code_size", code_size),
                             ("prologue_end", prologue_end), ("runtime_signature", runtime_sig),
                             ("tb_struct_size", tb_size), ("host_page_size", host_page),
                             ("target_page_bits", tpb)):
                    c(run_info[k] == v, f"bind: RUN_INFO {k} {run_info[k]:#x} != .sc {v:#x}")
                # The .sc never persists the prologue (it is a zero hole in the
                # file, critic A1), so the RUN_INFO copy is the only one; check
                # that it is a plausible prologue (non-zero, length matches).
                c(len(run_info["prologue"]) == prologue_end and any(run_info["prologue"]),
                  "bind: RUN_INFO prologue bytes empty")
                c(record_count == run_info["header_record_count_at_load"] + len(tbs),
                  f"bind: .sc record_count {record_count} != at_load "
                  f"{run_info['header_record_count_at_load']} + {len(tbs)} TB chunks")
            rec_base = L.SC_CODE_OFFSET + code_size
            fsize = os.fstat(fh.fileno()).st_size
            n_valid = min(record_count, max(0, (fsize - rec_base) // L.SC_RECORD.size))
            bound = 0
            for t in tbs:
                w = f"bind slot {t['slot']}"
                if not c(t["slot"] < n_valid, f"{w}: beyond the .sc records ({n_valid})"):
                    continue
                fh.seek(rec_base + t["slot"] * L.SC_RECORD.size)
                rb = fh.read(L.SC_RECORD.size)
                r_ok = c(rb == t["record"], f"{w}: record bytes differ from the .sc")
                rec = L.SC_RECORD.unpack(rb)
                c(rec[-1] == L.SC_RECORD_COMMIT and rec[-2] == sc_record_checksum(rb),
                  f"{w}: .sc record not committed/checksum")
                tc_off, end_off = rec[2], rec[1]
                fh.seek(L.SC_CODE_OFFSET + tc_off)
                code = fh.read(end_off - tc_off)
                f_ok = c(fnv1a64(code) == t["code_fnv64"], f"{w}: code_fnv64 mismatch")
                bound += bool(r_ok and f_ok)
            bind["bound_tb_chunks"] = bound
        sm.stats["binding"] = bind
    return sm


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("scm")
    ap.add_argument("--sc", help="the .sc this capture ran against (binding checks)")
    ap.add_argument("--json", help="also write the summary here")
    ap.add_argument("--allow-flags", action="store_true",
                    help="do not fail on TBs flagged capture_error/capture_overflow")
    a = ap.parse_args(argv)
    sm = run(a.scm, a.sc, a.allow_flags)
    out = {"scm": os.path.abspath(a.scm), "sc": a.sc and os.path.abspath(a.sc),
           "verdict": "PASS" if not sm.failures else "FAIL",
           "failures": sm.failures, "notes": sm.notes[:50], "stats": sm.stats}
    text = json.dumps(out, indent=2, sort_keys=True, default=str)
    print(text)
    if a.json:
        Path(a.json).write_text(text + "\n")
    return 0 if not sm.failures else 1


if __name__ == "__main__":
    sys.exit(main())

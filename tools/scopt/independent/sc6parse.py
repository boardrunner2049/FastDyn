#!/usr/bin/env python3
"""Independent parser of `.sc` v6 files (B3/B4 design V11; WP18).

Written from the C loader of `wt/qemu-v6` (tcg/supercache.c: valid_header,
load_records, sc6_load_ext, sc6_verify_hashes; commit 4671d5ceda), NOT from
scopt/formats/sc_v6.py. Standalone: no scopt import, stdlib only.

usage: sc6parse.py FILE.sc OUT.json [--host-page N]

OUT.json = {"header": {...}, "records": [...], "loaded": [...tb_offsets...],
            "ext": {...} | null, "variants": [...], "slots": [...], "links": [...],
            "die": "<first sc6_die message>" | null, "verdict": "v5" | "v6_ok" | "v6_die" |
            "invalid_header"}

`die` is the text the loader prints after "supercache: invalid v6 extension: "
(the first failing check, in the C order; hash checks run only when the
table itself passed). Records are loaded as load_records does (committed,
checksum, bounds; the first record_count that fit in the file).
"""
import json
import struct
import sys

FNV_OFF = 1469598103934665603  # SUPERCACHE_FNV_OFFSET as written in supercache.c (decimal)
FNV_P = 0x100000001B3
M64 = (1 << 64) - 1


def fnv(b, h=FNV_OFF):
    for x in b:
        h = ((h ^ x) * FNV_P) & M64
    return h


HDR = struct.Struct('<8sIIQQQQQIIIIQ')               # SupercacheHeader, 80 bytes
REC = struct.Struct('<QQQQQQQQQIIHHHHHHII4x')        # SupercacheRecord, 104 bytes
EXT = struct.Struct('<8sIIIIIHHHHIQ8sQ')             # Sc6ExtHeader, 64
VAR = struct.Struct('<QQQQQQIIQ')                    # Sc6VariantEntry, 64
SLT = struct.Struct('<QBBBBIIIQ')                    # Sc6SlotEntry, 32
LNK = struct.Struct('<IIQ')                          # Sc6LinkEntry, 16
assert (HDR.size, REC.size, EXT.size, VAR.size, SLT.size, LNK.size) == (80, 104, 64, 64, 32, 16)
CODE_OFF = 0x10000
EXT_OFF = 0x100
ARR_OFF = 0x140
COMMIT = 0x53435231
NONE16 = 0xFFFF
KNOWN = 0xF


class Die(Exception):
    pass


class File:
    """Sparse-friendly random access: data[a:b] reads only that range."""

    def __init__(self, path):
        self.f = open(path, 'rb')
        self.f.seek(0, 2)
        self.size = self.f.tell()

    def __len__(self):
        return self.size

    def __getitem__(self, sl):
        a, b = sl.start, min(sl.stop, self.size)
        if b <= a:
            return b''
        self.f.seek(a)
        return self.f.read(b - a)


def main(argv):
    path, out = argv[1], argv[2]
    host_page = 4096
    if '--host-page' in argv:
        host_page = int(argv[argv.index('--host-page') + 1], 0)
    data = File(path)
    doc = {"path": path, "die": None, "ext": None, "variants": [], "slots": [], "links": [],
           "records": [], "loaded": []}
    if len(data) < 80:
        doc["verdict"] = "invalid_header"
        json.dump(doc, open(out, 'w'))
        return 0
    hb = data[0:80]
    (magic, ver, hsz, code_size, code_addr, prol_end, rcount, sig, hps, tpb, tbsz, feat,
     ck) = HDR.unpack_from(hb, 0)
    doc["header"] = dict(magic=magic.decode('latin-1'), version=ver, header_size=hsz,
                         code_size=code_size, code_address=code_addr, prologue_end=prol_end,
                         record_count=rcount, runtime_signature=sig, host_page_size=hps,
                         target_page_bits=tpb, tb_size=tbsz, feature_flags=feat, checksum=ck,
                         checksum_ok=fnv(hb[:72]) == ck)
    semantic_v7 = ver == 7 and bool(feat & (1 << 31))
    if magic != b'QSCACHE1' or not semantic_v7 or \
            hsz != 80 or fnv(hb[:72]) != ck:
        doc["verdict"] = "invalid_header"
        json.dump(doc, open(out, 'w'))
        return 0
    # load_records
    roff = CODE_OFF + code_size
    avail = max(0, (len(data) - roff) // 104) if len(data) >= roff else 0
    count = min(rcount, avail)
    loaded = []
    blob = data[roff:roff + count * 104]
    for i in range(count):
        raw = blob[i * 104:(i + 1) * 104]
        f = REC.unpack(raw)
        (tb, end, tc, tcs, pc, csb, pa0, pa1, gh, fl, cf, gs, ic, jr0, jr1, ji0, ji1, rck,
         com) = f
        h = fnv(raw[:92])
        ok = com == COMMIT and ((h ^ (h >> 32)) & 0xFFFFFFFF) == rck
        if ok:
            ok = not (code_size < tbsz or tb > code_size - tbsz or tb >= tc or tc >= end or
                      tcs > end - tc or end > code_size)
        r = dict(slot=i, tb_offset=tb, end_offset=end, tc_offset=tc, tc_size=tcs, pc=pc,
                 cs_base=csb, page_addr0=pa0, page_addr1=pa1, flags=fl, cflags=cf,
                 guest_size=gs, icount=ic, jmp_reset_offset=[jr0, jr1],
                 jmp_insn_offset=[ji0, ji1], valid=ok)
        doc["records"].append(r)
        if ok:
            loaded.append(r)
    doc["loaded"] = [r["tb_offset"] for r in loaded]
    try:
        vs, ss, rs = load_ext(doc, data, loaded, code_size, prol_end,
                              feat & 0xf, tbsz, tpb,
                              host_page)
        verify_hashes(data, vs, ss, rs)
        doc["verdict"] = "v7_semantic_ok"
    except Die as e:
        doc["die"] = str(e)
        doc["verdict"] = "v7_semantic_die"
    json.dump(doc, open(out, 'w'))
    return 0


def load_ext(doc, data, loaded, code_size, prol_end, feat, tbsz, tpb, host_page):
    if feat & ~KNOWN:
        raise Die("unknown feature bits 0x%x" % (feat & ~KNOWN))
    if not feat & 1:
        raise Die("feature EXT_TABLE clear")
    xb = data[EXT_OFF:EXT_OFF + 64]
    if len(xb) < 64:
        raise Die("cannot read the extension header")
    (xm, xv, xhs, nv, ns, nl, ves, ses, les, r0, r1, vmax, aid, xck) = EXT.unpack(xb)
    doc["ext"] = dict(magic=xm.decode('latin-1'), ext_version=xv, ext_header_size=xhs,
                      n_variants=nv, n_slots=ns, n_links=nl, variant_entry_size=ves,
                      slot_entry_size=ses, link_entry_size=les, variants_max_end=vmax,
                      artifact_id8=aid.hex(), checksum=xck)
    if xm != b'QSCX6EXT':
        raise Die("bad ext magic")
    if xv != 1:
        raise Die("bad ext_version %d" % xv)
    if xhs != 64:
        raise Die("bad ext_header_size %d" % xhs)
    if (ves, ses, les) != (64, 32, 16):
        raise Die("bad entry sizes %d/%d/%d" % (ves, ses, les))
    if r0 or r1:
        raise Die("reserved header fields not zero")
    asz = nv * 64 + ns * 32 + nl * 16
    if ARR_OFF + asz > CODE_OFF:
        raise Die("ext table too large")
    arr = data[ARR_OFF:ARR_OFF + asz]
    if len(arr) != asz:
        raise Die("cannot read the extension arrays")
    if fnv(arr, fnv(xb[:56])) != xck:
        raise Die("ext checksum mismatch")
    if bool(feat & 2) != bool(nv):
        raise Die("feature VARIANTS disagrees with n_variants %d" % nv)
    if bool(feat & 4) != bool(ns):
        raise Die("feature GUARDED_SLOTS disagrees with n_slots %d" % ns)
    if aid == b'\0' * 8:
        raise Die("artifact id is zero")
    by_tb = {}
    for r in loaded:
        if r["tb_offset"] in by_tb:
            raise Die("duplicate record tb_offset 0x%x" % r["tb_offset"])
        by_tb[r["tb_offset"]] = r
    tb_struct = (tbsz + 63) // 64 * 64
    floor = (prol_end + 63) // 64 * 64
    limit = code_size - host_page - 1024
    variants = []
    max_end = 0
    for i in range(nv):
        (g, vtb, vtc, vtcs, vend, vfnv, vid, vfl, vres) = VAR.unpack_from(arr, i * 64)
        v = dict(general_tb_offset=g, tb_offset=vtb, tc_offset=vtc, tc_size=vtcs,
                 end_offset=vend, code_fnv64=vfnv, variant_id=vid, flags=vfl, reserved=vres)
        doc["variants"].append(v)
        c = by_tb.get(g)
        if c is None:
            raise Die("variant %d: general_tb_offset 0x%x names no valid record" % (i, g))
        if vid != i:
            raise Die("variant %d: variant_id %d is not dense" % (i, vid))
        if vfl & ~1:
            raise Die("variant %d: unknown flags 0x%x" % (i, vfl))
        if vres:
            raise Die("variant %d: reserved not zero" % i)
        if vtb % 64:
            raise Die("variant %d: tb_offset 0x%x not 64-aligned" % (i, vtb))
        if vtc < vtb or vtc - vtb != tb_struct:
            raise Die("variant %d: tc_offset - tb_offset != %d" % (i, tb_struct))
        if vtcs != c["tc_size"]:
            raise Die("variant %d: tc_size 0x%x != C tc_size 0x%x" % (i, vtcs, c["tc_size"]))
        if vend != vtc + (c["end_offset"] - c["tc_offset"]):
            raise Die("variant %d: end_offset does not match C's extent" % i)
        if vtb < floor:
            raise Die("variant %d: below the prologue" % i)
        if vend >= limit:
            raise Die("variant %d: end_offset 0x%x out of range" % (i, vend))
        for r in loaded:
            if vtb < r["end_offset"] and r["tb_offset"] < vend:
                raise Die("variant %d: overlaps record at tb_offset 0x%x" % (i, r["tb_offset"]))
        for k, w in enumerate(variants):
            if vtb < w["end_offset"] and w["tb_offset"] < vend:
                raise Die("variant %d: overlaps variant %d" % (i, k))
        variants.append(v)
        max_end = max(max_end, vend)
    if vmax != max_end:
        raise Die("variants_max_end 0x%x != 0x%x" % (vmax, max_end))
    slots = []
    slot_recs = []
    seen = set()
    for i in range(ns):
        (src, n, kind, xc, sres, vc, goff, glen, gfnv) = SLT.unpack_from(arr, nv * 64 + i * 32)
        s = dict(src_tb_offset=src, slot_n=n, kind=kind, exit_class=xc, reserved=sres,
                 target_vaddr=vc, guard_off=goff, guard_len=glen, guard_fnv64=gfnv)
        doc["slots"].append(s)
        r = by_tb.get(src)
        if r is None:
            raise Die("slot %d: src_tb_offset 0x%x names no valid record" % (i, src))
        if n > 1:
            raise Die("slot %d: slot_n %d" % (i, n))
        if kind not in (1, 2):
            raise Die("slot %d: bad kind %d" % (i, kind))
        if xc not in (1, 2, 3):
            raise Die("slot %d: bad exit_class %d" % (i, xc))
        if xc == 3 and kind != 2:
            raise Die("slot %d: ordinary direct edge requires a private variant" % i)
        if sres:
            raise Die("slot %d: reserved not zero" % i)
        jmp, rst = r["jmp_insn_offset"][n], r["jmp_reset_offset"][n]
        if jmp == NONE16 or rst == NONE16:
            raise Die("slot %d: R has no registered jump %d" % (i, n))
        if glen == 0 or goff >= r["tc_size"] or goff + glen > r["tc_size"]:
            raise Die("slot %d: guard [0x%x, +0x%x) outside R's code" % (i, goff, glen))
        gend = goff + glen
        if jmp < goff or jmp + 4 > gend or rst < goff or rst >= gend:
            raise Die("slot %d: jump %d (insn 0x%x, reset 0x%x) outside the guard"
                      % (i, n, jmp, rst))
        if (r["tc_offset"] + jmp) % 4:
            raise Die("slot %d: jump field not 4-aligned" % i)
        if (src, n) in seen:
            k = next(j for j, t in enumerate(slots) if (t["src_tb_offset"], t["slot_n"]) == (src, n))
            raise Die("slot %d: duplicates slot %d" % (i, k))
        seen.add((src, n))
        slots.append(s)
        slot_recs.append(r)
    slot_link = [None] * ns
    var_link = [None] * nv
    pmask = (1 << tpb) - 1
    for i in range(nl):
        (si, vi, lres) = LNK.unpack_from(arr, nv * 64 + ns * 32 + i * 16)
        doc["links"].append(dict(slot_index=si, variant_index=vi, reserved=lres))
        if lres:
            raise Die("link %d: reserved not zero" % i)
        if si >= ns:
            raise Die("link %d: slot_index %d out of range" % (i, si))
        if vi >= nv:
            raise Die("link %d: variant_index %d out of range" % (i, vi))
        if slots[si]["kind"] != 2:
            raise Die("link %d: slot %d is not SPEC" % (i, si))
        if slot_link[si] is not None:
            raise Die("slot %d: more than one link" % si)
        if var_link[vi] is not None:
            raise Die("variant %d: more than one link" % vi)
        c = by_tb[variants[vi]["general_tb_offset"]]
        if (slots[si]["target_vaddr"] & pmask) != (c["page_addr0"] & pmask):
            raise Die("link %d: target 0x%x is not at C's page offset"
                      % (i, slots[si]["target_vaddr"]))
        slot_link[si] = i
        var_link[vi] = i
    for i, s in enumerate(slots):
        if s["kind"] == 2 and slot_link[i] is None:
            raise Die("slot %d: SPEC slot has no link" % i)
    for i in range(nv):
        if var_link[i] is None:
            raise Die("variant %d: has no link" % i)
    return variants, slots, slot_recs


def verify_hashes(data, variants, slots, slot_recs):
    for i, v in enumerate(variants):
        b = data[CODE_OFF + v["tc_offset"]: CODE_OFF + v["end_offset"]]
        b = b + b'\0' * (v["end_offset"] - v["tc_offset"] - len(b))
        if fnv(b) != v["code_fnv64"]:
            raise Die("variant %d: code hash mismatch" % i)
    for i, (s, r) in enumerate(zip(slots, slot_recs)):
        a = CODE_OFF + r["tc_offset"] + s["guard_off"]
        b = data[a:a + s["guard_len"]]
        b = b + b'\0' * (s["guard_len"] - len(b))
        if fnv(b) != s["guard_fnv64"]:
            raise Die("slot %d: guard hash mismatch" % i)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

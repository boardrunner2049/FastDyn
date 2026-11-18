"""WP16 value-probe builders (experiments/gce_probe.py, experiments/b4_probe.py).

Unit part of WP16's acceptance for the probe *builders*: the stub encoder, the
uniform-shift rebuild, the icount-entry SFB rewrite and the cmov window are
checked statically and, where the bytes are self-contained, by executing them
natively (ctypes + mmap(PROT_EXEC), the V18 idea) against the original bytes.
The real-snapshot tests use the B2 compute snapshot when it is present.
"""

from __future__ import annotations

import ctypes
import json
import mmap
import os
import random
import struct
from pathlib import Path

import pytest

from experiments import b4_probe as B4
from experiments import gce_probe as G
from scopt.decode.x86 import sweep
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
SNAP = ROOT / "runs/b2/compute/store/snapshots/53b4132fc05526ff2925cd3acc610bf613e8b11e2315e15e520bff7fe080dab2"
PROF = ROOT / "runs/b2/compute/prof/merged.profile.json"
ELF = ROOT / ("fixtures/freertos-mps2-compute/FreeRTOS/FreeRTOS/Demo/CORTEX_MPS2_QEMU_IAR_GCC/"
              "build/gcc/output/RTOSDemo.out")
real = pytest.mark.skipif(not (SNAP.is_dir() and PROF.is_file() and ELF.is_file()),
                          reason=f"{SNAP}, {PROF} or {ELF} not present")


# ---------------------------------------------------------------------------
# native execution helper
# ---------------------------------------------------------------------------

class Native:
    """Executable scratch page; ``call(code, rdi, rsi)`` runs ``code`` as a function."""

    def __init__(self, size: int = 1 << 16):
        self.m = mmap.mmap(-1, size, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
        self.addr = ctypes.addressof(ctypes.c_char.from_buffer(self.m))
        self.ftype = ctypes.CFUNCTYPE(ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64)

    def load(self, code: bytes):
        self.m.seek(0)
        self.m.write(code)
        return self.ftype(self.addr)


def _wrap_entry(entry: bytes, exit_off: int) -> bytes:
    """``f(env)``: rbp = env; run ``entry`` (whose exit branches target ``exit_off`` in the
    entry's own offsets); return 0 on fall-through, 1 when the exit was taken."""
    pro = bytes.fromhex("55" "53" "4154" "4889fd")  # push rbp, rbx, r12; mov rbp, rdi
    body = bytearray(entry)
    # fall-through: xor eax,eax ; jmp done
    ft = bytes.fromhex("31c0") + b"\xeb\x05"
    ex = bytes.fromhex("b801000000")  # mov eax, 1
    epi = bytes.fromhex("415c5b5dc3")  # pop r12, rbx, rbp; ret
    code = pro + bytes(body) + ft + ex + epi
    # exit_off (relative to the entry) must land on ``ex``
    assert exit_off == len(body) + len(ft), (exit_off, len(body))
    return code


# ---------------------------------------------------------------------------
# stub encoder
# ---------------------------------------------------------------------------

def _stub(mode="gce", targets=((0, 0x24F0), (1, 0x2520)), tc_abs=0x7FFF6C154900):
    return G.build_stub(stub_off=0x70, tc_abs=tc_abs, tb_abs=tc_abs - 192, tb_ret_abs=0x7FFF6C000018,
                        pool_slot=0x68, targets=list(targets), kf=0x110, kf2=0x400, mode=mode)


def test_stub_template_shape():
    b, slots, lay = _stub()
    ins = sweep(b, 0x70, 0x70 + len(b))
    assert not any(i.bad for i in ins)
    m = [i.mnemonic for i in ins if i.mnemonic != "nop"]
    assert m[:10] == ["cmp", "jne"] * 5  # g_bp g_thumb g_cond g_flags g_flags2
    assert m[10:14] == ["cmp", "je", "cmp", "je"] and m[14] == "jmp"
    texts = [i.text() for i in ins]
    assert "cmp qword ptr [rbp - 0x2980], 0" in texts
    assert "cmp byte ptr [rbp + 0x14d], 1" in texts
    assert "cmp dword ptr [rbp + 0x228], 0" in texts
    assert "cmp dword ptr [rbp + 0x150], 0x110" in texts
    assert "cmp qword ptr [rbp + 0x158], 0x400" in texts
    assert "cmp dword ptr [rbp + 0x3c], 0x24f0" in texts
    # every jne of the key atoms goes to F; F is the original tail
    F = lay["F_off"]
    assert all(i.branch_target == F for i in ins if i.mnemonic == "jne")
    assert b[F - 0x70:F - 0x70 + 3] == G.TAIL_MOV and b[-2:] == G.TAIL_JMP
    call = [i for i in ins if i.mnemonic == "call"]
    assert len(call) == 1 and call[0].rip_target == 0x68
    for n, (ins_off, rst) in slots.items():
        assert (0x7FFF6C154900 + ins_off) % 4 == 0  # atomic patch field
        assert rst == ins_off + 4
        assert b[ins_off - 0x70 - 1] == 0xE9 and b[ins_off - 0x70:ins_off - 0x70 + 4] == b"\0" * 4
        lea = [i for i in ins if i.address == rst][0]
        assert lea.mnemonic == "lea" and 0x7FFF6C154900 + lea.rip_target - (1 << 64) * (
            lea.rip_target >= 1 << 63) == 0x7FFF6C154900 - 192 + n
    # no store, no LDST-like memory write in the stub
    assert not any(i.mnemonic == "mov" and i.op_str.split(",")[0].endswith("]") for i in ins)


def test_stub_modes_differ_only_in_pc_and_F():
    a, sa, la = _stub("gce")
    f, sf, lf = _stub("gce-fail")
    k, sk, lk = _stub("lk2x")
    assert len(a) == len(f) and sa == sf and la["F_off"] == lf["F_off"]
    diff = [i for i in range(len(a)) if a[i] != f[i]]
    assert diff  # only the g_pc immediates (low byte becomes odd)
    ins = sweep(f, 0x70, 0x70 + len(f))
    assert "cmp dword ptr [rbp + 0x3c], 0x24f1" in [i.text() for i in ins]
    assert k[:la["F_off"] - 0x70] == f[:la["F_off"] - 0x70]
    assert k.count(b"\xff\x15") == 2 and len(k) == len(f) + 9


def test_tail_patch_traps():
    t = G.tail_patch(0x4C, 0x70)
    assert len(t) == 11 and t[0] == 0xE9 and t[5:] == b"\xcc" * 6
    assert 0x4C + 5 + struct.unpack("<i", t[1:5])[0] == 0x70


# ---------------------------------------------------------------------------
# icount entry SFB rewrite: exhaustive native equivalence
# ---------------------------------------------------------------------------

def _old_entry(reg: str, n: int) -> tuple[bytes, int]:
    """TCG's entry for ``reg`` (ebx or r12d), with the exit at ``len + 4`` (see _wrap_entry)."""
    if reg == "ebx":
        load, test, st = bytes.fromhex("8b5df8"), bytes.fromhex("85db"), bytes.fromhex("66895df8")
        add = bytes.fromhex("ffcb") if n == 1 else bytes.fromhex("83c3") + struct.pack("<b", -n)
    else:
        load, test, st = bytes.fromhex("448b65f8"), bytes.fromhex("4585e4"), bytes.fromhex("66448965f8")
        add = bytes.fromhex("41ffcc") if n == 1 else bytes.fromhex("4183c4") + struct.pack("<b", -n)
    pre = load + add + test
    total = len(pre) + 6 + len(st)
    jl = b"\x0f\x8c" + struct.pack("<i", (total + 4) - (len(pre) + 6))
    return pre + jl + st, total


def _run_entry(native, code, total, env, high, low):
    struct.pack_into("<HH", env, 0, low, high)  # env-8 is buffer offset 0
    f = native.load(_wrap_entry(code, total + 4))
    took = f(ctypes.addressof(env) + 8, 0)
    return took, struct.unpack_from("<HH", env, 0)


@pytest.mark.parametrize("reg,n", [("ebx", 1), ("ebx", 8), ("ebx", 2), ("r12d", 1), ("r12d", 9)])
def test_sfb_entry_equivalent_natively(reg, n):
    old, total = _old_entry(reg, n)
    # parse the old entry like the probe does
    class U:  # minimal unit stand-in
        code = old + b"\x90" * 8
        insn0_start = total
    e = B4.parse_entry(U)
    assert e is not None and e["reg"] == reg and e["n"] == n and e["len"] == total
    new = B4.encode_sfb_entry(reg, n, e["exit_target"] + B4.SFB_SHIFT, total + B4.SFB_SHIFT)
    assert len(new) == total + B4.SFB_SHIFT
    assert B4.check_sfb_entry(new, e) == []
    nat = Native()
    env = ctypes.create_string_buffer(64)
    lows = list(range(0, 1100)) + list(range(65536 - 600, 65536)) + random.Random(n).sample(range(65536), 2000)
    for high in (0, 0xFFFF):
        for low in lows:
            a = _run_entry(nat, old, total, env, high, low)
            b = _run_entry(nat, new, total + B4.SFB_SHIFT, env, high, low)
            assert a == b, (reg, n, high, low, a, b)


# ---------------------------------------------------------------------------
# cmov window: native equivalence on the observable outputs (env r0, ebx)
# ---------------------------------------------------------------------------

def _wrap_window(win: bytes) -> bytes:
    # f(env, packed): rbp=env; ebx=lo32(rsi); r12d=hi32(rsi); window; return ebx | env.r0<<32
    pro = bytes.fromhex("55" "53" "4154" "4155" "4889fd" "89f3" "4989f4" "49c1ec20")
    epi = bytes.fromhex("8b4500" "48c1e020" "4809d8" "415d" "415c" "5b" "5d" "c3")
    return pro + win + epi


def test_cmov_window_equivalent_natively():
    assert len(B4.CMOV_NEW) == len(B4.CMOV_OLD) == 22
    nat = Native()
    env = ctypes.create_string_buffer(16)
    rnd = random.Random(1)
    vals = [0, 1, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF, 0x1234] + [rnd.getrandbits(32) for _ in range(3000)]
    for i in range(len(vals)):
        ebx, r12, r0, r2 = vals[i], vals[-i - 1], vals[(i * 7) % len(vals)], vals[(i * 13) % len(vals)]
        outs = []
        for win in (B4.CMOV_OLD, B4.CMOV_NEW):
            struct.pack_into("<IIII", env, 0, r0, 0, r2, 0)
            f = nat.load(_wrap_window(win))
            outs.append(f(ctypes.addressof(env), ebx | r12 << 32))
        assert outs[0] == outs[1], (hex(ebx), hex(r12), hex(r2))


# ---------------------------------------------------------------------------
# D1 dataflow on synthetic units
# ---------------------------------------------------------------------------

class SynUnit:
    def __init__(self, code: bytes, insn0: int = 0):
        self.code = code
        self.tc_size = len(code)
        self.pool_start = None
        self.insn0_start = insn0
        self.sites = ()
        self.uid = "syn"


ST_NF = bytes.fromhex("44 89 a5 18 02 00 00".replace(" ", ""))  # mov [rbp+0x218], r12d
ST_NF2 = bytes.fromhex("44 89 ad 18 02 00 00".replace(" ", ""))  # mov [rbp+0x218], r13d
LD_NF = bytes.fromhex("8b9d18020000")  # mov ebx, [rbp+0x218]
CALL = bytes.fromhex("e800000000")
EXIT = bytes.fromhex("e9" "00100000")  # jmp out of the unit
RET_JMP = bytes.fromhex("ffe0")


def _dead(code):
    return [s["off"] for s in B4.d1_dead_stores(SynUnit(code))]


def test_d1_overwrite_is_dead():
    assert _dead(ST_NF + ST_NF2 + EXIT) == [0]


def test_d1_call_or_read_or_exit_keeps_it():
    assert _dead(ST_NF + CALL + ST_NF2 + EXIT) == []
    assert _dead(ST_NF + LD_NF + ST_NF2 + EXIT) == []
    assert _dead(ST_NF + EXIT + ST_NF2) == []
    assert _dead(ST_NF + RET_JMP) == []
    guest = bytes.fromhex("8b03")  # mov eax,[rbx]: a guest/host_abs access is an observer
    assert _dead(ST_NF + guest + ST_NF2 + EXIT) == []


def test_d1_both_branch_arms():
    # store; test; jge +7 ; store2 ; jmp +7 ; store2 ; exit
    jge = bytes.fromhex("85db") + b"\x7d\x09"  # skip store2(7) + jmp(2)
    both = ST_NF + jge + ST_NF2 + b"\xeb\x07" + ST_NF2 + EXIT
    assert _dead(both) == [0]
    one = ST_NF + bytes.fromhex("85db") + b"\x7d\x07" + ST_NF2 + EXIT
    assert _dead(one) == []  # the taken arm reaches the exit with NF live


def test_d1_not_in_prefix_and_not_partial():
    assert B4.d1_dead_stores(SynUnit(ST_NF + ST_NF2 + EXIT, insn0=7)) == []
    byte_store = bytes.fromhex("88 9d 18 02 00 00".replace(" ", ""))  # mov [rbp+0x218], bl
    assert _dead(ST_NF + byte_store + ST_NF2 + EXIT) == []


# ---------------------------------------------------------------------------
# real snapshot
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def compute():
    from scopt.importer.import_cache import open_snapshot
    return open_snapshot(SNAP), json.loads(PROF.read_text())


@real
def test_d1_static_count_matches_the_model(compute):
    snap, _ = compute
    n = sum(len(B4.d1_dead_stores(u)) for u in snap.units()
            if u.meta_status == "ok" and not u.key.cflags & 0x11000)
    # notes/b3/map-state-sync-traffic.md §7.3: M1a nosink = 469 static stores
    assert n == 469


@real
def test_sfb_shift_keeps_every_reference(compute):
    snap, _ = compute
    hdr = snap.header
    checked = 0
    for u in snap.units()[:400]:
        p = B4.sfb_prefix(u)
        if p is None:
            continue
        new_tb = u.tb_offset + 0x100000
        rb = G.rebuild_unit(u, new_tb, prefix=p[0], prefix_len=p[1], code_size=hdr.code_size)
        rec = rb.rec
        assert rec.tc_size == u.tc_size + 16 and rb.body[:rec.tc_size][p[1] + 16:] != b""
        old_abs = hdr.code_address + u.tc_offset
        new_abs = hdr.code_address + rec.tc_offset
        end_old = u.pool_start if u.pool_start is not None else u.tc_size
        oi = [i for i in sweep(u.code, 0, end_old) if i.address >= p[1]]
        ni = [i for i in sweep(rb.body[:rec.tc_size], 0, end_old + 16) if i.address >= p[1] + 16]
        assert [i.mnemonic for i in oi] == [i.mnemonic for i in ni]
        sx = lambda v: v - (1 << 64) if v >= 1 << 63 else v
        for a, b in zip(oi, ni):
            for get in (lambda i: i.branch_target, lambda i: i.rip_target):
                ta, tb = get(a), get(b)
                if ta is None:
                    assert tb is None
                    continue
                ta, tb = sx(ta), sx(tb)
                if 0 <= ta < u.tc_size:  # intra: shifts by 16
                    assert tb == ta + 16
                elif -192 <= ta < 0:  # own struct: still at tc - 192 + k
                    assert tb == ta
                else:  # external absolute target unchanged
                    assert new_abs + tb == old_abs + ta
        from scopt.decode.search import decode_search
        ro = decode_search(u.search, u.icount)
        rn = decode_search(rb.body[rec.tc_size:], rec.icount)
        assert [r.words for r in ro] == [r.words for r in rn]
        assert [r.host_end + 16 for r in ro] == [r.host_end for r in rn]
        assert rec.jmp_insn_offset == tuple(x + 16 if x != 0xFFFF else x for x in
                                            G.ScRecord.from_bytes(u.record).jmp_insn_offset)
        checked += 1
    assert checked > 300


@real
def test_gce_copies_static_checks(compute, tmp_path):
    snap, prof = compute
    res = G.build_copies(snap, prof, ELF, tmp_path, modes=("gce", "gce-fail"))
    m = res["model"]
    assert m["top_set_units"] > 0 and 0 < m["removed_lookup_share_of_all_lookups"] < 1
    assert res["admission"]["refused_units"].get("indirect_multiway", 0) >= 1  # tbb, bx rX
    from scopt.formats.sc_v5 import read_sc
    a = read_sc(res["copies"]["gce"]["sc"], strict=True)
    b = read_sc(res["copies"]["gce-fail"]["sc"], strict=True)
    assert len(a.valid_slots) == len(b.valid_slots) == len(snap.units())
    # identical placement; differing bytes only inside the stubs
    for ua, ub in zip(a.units(), b.units()):
        assert ua.record == ub.record
    # handler-mode and tbb units are never grown
    grown = set(res["copies"]["gce"]["units"])
    for s in res["sites"]:
        if s["uid"] in grown:
            assert s["class"] in ("return", "static")


# ---------------------------------------------------------------------------
# diag twin: gdb output parsing and the script it runs
# ---------------------------------------------------------------------------

def test_diag_twin_parse_and_script(tmp_path):
    from experiments import diag_twin as T
    T.gdb_script(tmp_path / "s.gdb")
    s = (tmp_path / "s.gdb").read_text()
    n = len(T.FLUSH_FUNCS)
    assert "break benchmark_start" in s and f"disable 3-{2 + n}" in s and f"enable 3-{2 + n}" in s
    assert s.rstrip().endswith("run")
    T.gdb_script(tmp_path / "c.gdb", count_from_start=True)
    assert "disable 3-" not in (tmp_path / "c.gdb").read_text()
    out = "\n".join([
        "WP16 benchmark_start bp_head=(nil) wp_head=(nil) singlestep=0",
        "WP16 benchmark_end bp_head=(nil) wp_head=0x5555 singlestep=0",
        "WP16 info-breakpoints-begin",
        "3       breakpoint     keep y   0x0000555555c7de60 in tlb_flush at ../accel/tcg/cputlb.c:421",
        "\tbreakpoint already hit 2 times",
        "21      breakpoint     keep y   0x0000555555bcf150 in tcg_commit_cpu at ../system/physmem.c:2758",
        "WP16 info-breakpoints-end",
    ])
    g = T.parse_gdb(out)
    assert g["counters"]["tlb_flush"] == 2 and g["counters"]["tcg_commit_cpu"] == 0
    assert g["counters_total"] == 2 and g["start"]["bp_head"] == "(nil)" and g["end"]["wp_head"] == "0x5555"

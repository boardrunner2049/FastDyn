"""WP2: meta-less classifier. Synthetic TBs, and the G17 tallies on minimal.sc."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from scopt.decode.search import SearchRow, encode_search
from scopt.decode.symbols import ElfImage, ElfSegment, ElfSymbol, MapEntry, Maps
from scopt.decode.x86 import tiles
from scopt.formats.sc_v5 import ScHeader, ScRecord, read_sc, write_sc
from scopt.formats.layouts import SC_MAGIC, SC_RECORD_COMMIT
from scopt.importer.metaless import (B0_FIRMWARE, B0_PLUGIN, B0_QEMU, G17,
                                     classify_sc, format_g17, infer_plugin_base,
                                     infer_prologue)
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
CA = 0x7FFF6C000000
LIB = 0x7FFF00000000  # an executable anonymous mapping standing in for a library


def _le32(v: int) -> bytes:
    return (v & 0xFFFFFFFF).to_bytes(4, "little")


def _synthetic_tb(tc: int, tb: int) -> bytes:
    """A TB exercising every class; offsets are relative to tc."""
    a = CA + tc
    c = bytearray()
    c += bytes.fromhex("8b5df8 85db".replace(" ", ""))  # 0x00 irq check
    c += bytes.fromhex("0f8c") + _le32(0x29 - 0x0B)  # 0x05 jl 0x29 (intra)
    c += b"\xe9" + _le32(0)  # 0x0b goto_tb[0]: field at 0x0c (4-aligned)
    c += bytes.fromhex("488d05") + _le32((CA + tb) - (a + 0x17))  # 0x10 lea rax, tb+0
    c += b"\xe9" + _le32((CA + 0x18) - (a + 0x1C))  # 0x17 jmp tb_ret (prologue+0x18)
    c += bytes.fromhex("ff15") + _le32(0x30 - 0x22)  # 0x1c call [rip -> pool 0x30]
    c += b"\xe8" + _le32(0x12345678)  # 0x22 call rel32 to nowhere -> unknown
    c += bytes.fromhex("ffe0")  # 0x27 jmp rax (goto_ptr)
    c += b"\xe9" + _le32((CA + 0x16) - (a + 0x2E))  # 0x29 jmp epilogue (prologue+0x16)
    c += b"\x90\x90"  # 0x2e pad
    assert len(c) == 0x30
    c += (LIB + 0x40).to_bytes(8, "little")  # 0x30 pool slot: function in LIB
    return bytes(c)


@pytest.fixture
def synth_sc(tmp_path):
    tb, tc = 0x40, 0x100
    code = _synthetic_tb(tc, tb)
    search = encode_search([SearchRow((0x10, 0, 0), 0x2E)], pcrel=True)
    body = code + search
    rec = ScRecord(tb, tc + len(body), tc, len(code), 0, 0x800400, 0x28080010,
                   0xFFFFFFFFFFFFFFFF, 0, 0x110, 0xFF020000, 2, 1, (0xFFFF, 0xFFFF),
                   (0x0C, 0xFFFF), 0, SC_RECORD_COMMIT).with_checksum()
    hdr = ScHeader(SC_MAGIC, 7, 80, 0x10000, CA, 0x2D, 1, 0, 4096, 10, 152, 0, 0)
    p = tmp_path / "synth.sc"
    write_sc(p, hdr, [(rec, body)], fsync=False)
    return p


def test_synthetic_classes(synth_sc):
    maps = Maps([MapEntry(LIB, LIB + 0x1000, "r-xp")])
    res = classify_sc(synth_sc, qemu=None, plugin=None, maps=maps)
    assert res.tb_ret_off == 0x18 and res.epilogue_off == 0x16
    assert "epilogue_off + 2 == tb_ret_off: ok" in res.prologue_notes
    (t,) = res.tbs
    assert t.pool_start == 0x30 and t.tiled and t.bad == [] and t.irq_check
    got = {r.insn_off: (r.cls, r.binding) for r in t.refs}
    assert got == {
        0x05: ("intra", ""),
        0x0B: ("goto_tb", ""),
        0x10: ("own_struct+0", ""),
        0x17: ("prologue.tb_ret", ""),
        0x1C: ("pool", "ext.lib"),
        0x22: ("unknown", ""),
        0x27: ("goto_ptr", ""),
        0x29: ("prologue.epilogue", ""),
    }
    assert t.problems == ["unexplained_reference@0x23"]
    goto = [r for r in t.refs if r.cls == "goto_tb"][0]
    assert goto.field_off == 0x0C and "chained" not in goto.detail
    doc = res.to_json(refs=True)
    assert doc["unknown"][0]["field_off"] == 0x23
    assert json.loads(json.dumps(doc)) == doc


def test_synthetic_overrides_and_misaligned_goto(synth_sc, tmp_path):
    # overriding the prologue offsets the other way round makes both jmps unknown
    maps = Maps([MapEntry(LIB, LIB + 0x1000, "r-xp")])
    res = classify_sc(synth_sc, qemu=None, plugin=None, maps=maps, tb_ret_off=0x20,
                      epilogue_off=0x10)
    cls = Counter(r.cls for r in res.tbs[0].refs)
    assert cls["prologue.tb_ret"] == 0 and cls["unknown"] == 3
    assert any("MISMATCH" in n for n in res.prologue_notes)


def test_infer_prologue_rules():
    jm = [(0x18, True)] * 5 + [(0x16, False)] * 2
    assert infer_prologue(jm, 0x2D)[:2] == (0x18, 0x16)
    tr, ep, notes = infer_prologue([(0x18, True)], 0x2D)
    assert (tr, ep) == (0x18, None)
    tr, ep, notes = infer_prologue([(0x30, True)], 0x2D)
    assert any("outside the prologue" in n for n in notes)


def test_infer_plugin_base():
    img = ElfImage("/x.so", (ElfSegment(0, 0x3000, 0, 0x3000, 5),),
                   (ElfSymbol(0x1123, 16, "f", "STT_FUNC"), ElfSymbol(0x2456, 16, "g", "STT_FUNC"),
                    ElfSymbol(0x2500, 16, "d", "STT_OBJECT")), True)
    base = 0x7FFF1F000000
    assert infer_plugin_base([base + 0x1123, base + 0x2456, base + 0x2456], img) == (base, 2, 2)
    assert infer_plugin_base([base + 0x2500], img) == (None, 0, 1)


# --------------------------------------------------------------------------
# Real data: minimal.sc (acceptance)
# --------------------------------------------------------------------------

def _need_real():
    for p in (B0_QEMU, B0_PLUGIN):
        if not os.path.exists(p):
            pytest.skip(f"{p} not present (the B0 build that wrote minimal.sc; $FASTDYN_ROOT)")


@pytest.fixture(scope="module")
def minimal_result(minimal_sc):
    _need_real()
    return classify_sc(minimal_sc, qemu=B0_QEMU, plugin=B0_PLUGIN,
                       firmware=B0_FIRMWARE if os.path.exists(B0_FIRMWARE) else None)


@pytest.mark.real
def test_minimal_g17_exact(minimal_result):
    g = minimal_result.g17()
    assert g == G17, (format_g17(g), g)
    assert format_g17(g) == "394 / 3 / 3 / 316 / 3 / 316 (+0:105, +1:50, +3:161) / 336 / 52 / 0"
    assert len(minimal_result.tbs) == 163
    assert minimal_result.tb_ret_off == 0x18 and minimal_result.epilogue_off == 0x16
    assert sum(t.irq_check for t in minimal_result.tbs) == 161
    # nothing is unexplained and every TB tiles [tc, pool_start) with no (bad)
    assert not any(t.problems for t in minimal_result.tbs)
    assert all(t.tiled and not t.bad for t in minimal_result.tbs)
    # review item A2 extra rows: 491 jmp (336 intra + 155 goto_tb), 544 jcc
    kc = minimal_result.kind_class_tallies()
    assert kc["jmp_rel32:intra"] + kc["jmp_rel32:goto_tb"] == 491
    assert kc["jcc_rel32:intra"] == 544


@pytest.mark.real
def test_minimal_helpers_match_nm(minimal_result):
    """The 11 pool-called helpers: each slot value - PIE base is an nm T symbol address."""
    nm = subprocess.run(["nm", "--defined-only", B0_QEMU], capture_output=True, text=True,
                        check=True).stdout
    funcs: dict[int, set[str]] = {}
    for line in nm.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] in "Tt":
            funcs.setdefault(int(parts[0], 16), set()).add(parts[2])
    names = Counter()
    for r in minimal_result.refs():
        if r.cls == "pool" and r.binding == "ext.helper":
            val = int(r.detail.split("= ")[1], 16)
            off = val - 0x555555554000
            assert off in funcs, r
            sym = r.symbol.split("!")[1]
            assert sym in funcs[off]
            names[sym] += 1
    assert len(names) == 11 and sum(names.values()) == 394
    assert names["helper_lookup_tb_ptr"] == 52 == minimal_result.g17()["goto_ptr"]
    assert all(n.startswith("helper_") for n in names)


@pytest.mark.real
def test_minimal_plugin_refs(minimal_result):
    plug = [m for m in minimal_result.modules if m["name"] == "libfastdyn.so"][0]
    assert plug["base"] == "0x7fff1f000000" and plug["source"].startswith("inferred: 3/3")
    calls = sorted(r.symbol for r in minimal_result.refs() if r.cls == "ext.plugin")
    assert calls == ["libfastdyn.so!benchmark_end", "libfastdyn.so!benchmark_start",
                     "libfastdyn.so!printreg"]
    leas = [r for r in minimal_result.refs() if r.cls == "ext.ptr_riprel"]
    assert len(leas) == 3 and all(r.binding == "abs.plugin_data" for r in leas)


@pytest.mark.real
def test_minimal_guest_binding(minimal_result):
    if not os.path.exists(B0_FIRMWARE):
        pytest.skip(f"{B0_FIRMWARE} not present")
    g = minimal_result.guest_summary
    assert g["delta"] == "0x20080000"
    assert g["elf_verified"] == 163 and g["thumb_lengths_ok"] == 163


@pytest.mark.real
@pytest.mark.slow
def test_minimal_boundaries_match_objdump(minimal_sc, tmp_path):
    """Independent decoder: capstone's insn boundaries equal objdump's on every TB."""
    from scopt.decode.x86 import sweep_to_pool
    sc = read_sc(minimal_sc)
    blob = tmp_path / "tb.bin"
    for u in sc.units():
        a = sc.header.code_address + u.record.tc_offset
        insns, pool = sweep_to_pool(u.code, a)
        assert tiles(insns, a, pool)
        blob.write_bytes(u.code[:pool - a])
        out = subprocess.run(["objdump", "-D", "-b", "binary", "-mi386:x86-64",
                              f"--adjust-vma={a:#x}", str(blob)], capture_output=True,
                             text=True, check=True).stdout
        starts = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0].strip().endswith(":"):
                starts.append(int(parts[0].strip()[:-1], 16))
                assert "(bad)" not in parts[2]
        assert starts == [i.address for i in insns], u.slot


@pytest.mark.real
def test_cli_inspect_metaless(minimal_sc):
    _need_real()
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    p = subprocess.run([sys.executable, "-m", "scopt", "inspect", "metaless", str(minimal_sc),
                        "--expect-g17"], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    assert "G17: 394 / 3 / 3 / 316 / 3 / 316 (+0:105, +1:50, +3:161) / 336 / 52 / 0" in p.stdout
    assert "MATCH" in p.stdout
    p = subprocess.run([sys.executable, "-m", "scopt", "inspect", "metaless", str(minimal_sc),
                        "--json"], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert doc["g17_match"] and len(doc["helpers"]) == 11 and doc["unknown"] == []

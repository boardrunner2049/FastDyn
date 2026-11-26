"""WP27 slice 0b: the in-place specialization copy (``experiments/wp27_inplace.py``).

Real input (read-only; the tests skip when absent): the F-B3-1 icount base snapshot on
the ``qint`` binary (WP27's ``runs/wp27/base/icount/1``, or the identical snapshot
``7b2d553c…`` of WP20's integration stage).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from experiments.wp27_inplace import (LABEL, LABEL_FILE, InPlaceError, SimplePatch,
                                      build_inplace_copy, find_units, parse_window_twin,
                                      window_twin_script)
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
SNAP_ID = "7b2d553cc9c895236c5ed2f037ad120367284334cb186769478a620f4499e959"
CANDIDATES = [ROOT / "runs/wp27/base/icount/1/store/snapshots" / SNAP_ID,
              ROOT / "runs/wp20-int/fx/icount/1/store/snapshots" / SNAP_ID]
SNAP_DIR = next((p for p in CANDIDATES if (p / "snapshot.json").is_file()), None)
R_RET = 0x10002  # f_b3_1_ret (bx lr) in fixtures/b3/b3.elf 3b029618…
VC = 0x2F4  # vc_b3_1
QINT = "sha256:8864c72671971cf4488b5afa76eeee02a3e2a472c45af81ec1314464e6035f41"

needs_snap = pytest.mark.skipif(SNAP_DIR is None, reason=f"F-B3-1 base snapshot not present: none of "
                                f"{', '.join(map(str, CANDIDATES))}")


@pytest.fixture(scope="module")
def snap():
    from scopt.importer.import_cache import open_snapshot

    return open_snapshot(SNAP_DIR)


@pytest.fixture(scope="module")
def layout():
    from scopt.formats.envlayout_v1 import find_envlayout

    return find_envlayout(QINT)


def _section35_patches(c):
    """§3.5's three sites in C (the r0 load, its test, the jne) and the slice-0a forms:
    nops, nop, nops (what CP+DHO, DHO and BF composed on F-B3-1)."""
    seq = bytes.fromhex("8b5d0085db0f85")
    i = c.code.find(seq)
    assert i >= 0 and c.code.find(seq, i + 1) < 0
    return [SimplePatch(i, bytes.fromhex("8b5d00"), bytes.fromhex("669090")),
            SimplePatch(i + 3, bytes.fromhex("85db"), bytes.fromhex("6690")),
            SimplePatch(i + 5, bytes(c.code[i + 5:i + 11]), bytes.fromhex("669066906690"))]


def test_public_fixture_emits_canonical_cache(tmp_path):
    from .test_composed_v7 import _grown_member_plan, _guard_layout

    snap, member, _ = _grown_member_plan(False)
    params = dict(r_vaddr=member.vaddrs[0], vc=snap.units()[1].vaddrs[0],
                  patches=(), layout=_guard_layout())
    checks = build_inplace_copy(snap, tmp_path, out_dir=tmp_path / "copy", **params)["checks"]
    assert checks["ok"]
    assert (checks["header_version"], checks["header_features"]) == (7, 0)

    snap.header = snap.header.replace(reserved=1)
    with pytest.raises(InPlaceError, match="requires_public_cache"):
        build_inplace_copy(snap, tmp_path, out_dir=tmp_path / "refused", **params)


@needs_snap
def test_find_units(snap):
    R, tail, C = find_units(snap, R_RET, VC)
    assert C.vaddrs[0] == VC and R.vaddrs[0] <= R_RET < R.vaddrs[0] + R.guest_size
    assert tail.end <= R.tc_size
    with pytest.raises(InPlaceError) as e:
        find_units(snap, R_RET, VC + 2)
    assert e.value.code == "ambiguous_c"


@needs_snap
def test_build_inplace_copy(snap, layout, tmp_path):
    from scopt.formats.sc_records import read_sc

    _, _, C = find_units(snap, R_RET, VC)
    pats = _section35_patches(C)
    d = build_inplace_copy(snap, SNAP_DIR, r_vaddr=R_RET, vc=VC, patches=pats,
                           out_dir=tmp_path / "copy", layout=layout)
    ch = d["checks"]
    assert ch["ok"], ch
    assert d["label"] == LABEL and d["published"] is False
    assert (ch["header_version"], ch["header_features"]) == (7, 0) and ch["header_problems"] == []
    assert ch["records"] == ch["base_records"] and not ch["other_units_identical"]["different"]
    assert ch["c_in_place"]["bytes_changed"] == 11 and ch["c_in_place"]["record_unchanged"]
    g = ch["r_grown"]
    assert g["decoded"]["slot_kind"] == "GENERAL" and g["decoded"]["registered"]
    assert g["decoded"]["reset_kind"] == "exit_tb" and g["decoded"]["targets"] == [hex(VC)]
    assert g["decoded"]["atoms"] == ["g_bp", "g_thumb", "g_cond", "g_flags", "g_flags2", "g_pc"]
    # the file is public-only: R's slot registered in its record
    sc = read_sc(tmp_path / "copy" / "cache.sc")
    assert sc.loadable
    rec = next(sc.records[i] for i in sc.valid_slots
               if sc.records[i].tb_offset == int(g["to_tb_offset"], 16))
    assert rec.jmp_insn_offset[g["slot_n"]] == g["jmp_insn_offset"] != 0xFFFF
    # never publishable: no sidecar, no manifest; labelled
    files = sorted(p.name for p in (tmp_path / "copy").iterdir())
    assert "cache.sc.scopt.json" not in files and "MANIFEST.sha256" not in files
    lab = json.loads((tmp_path / "copy" / LABEL_FILE).read_text())
    assert lab["label"] == LABEL and lab["cache_sc_sha256"].startswith("sha256:")
    assert [p["kind"] for p in lab["probes"]] == ["c", "stub", "fallback", "reset"]


@needs_snap
def test_refusals(snap, layout, tmp_path):
    _, _, C = find_units(snap, R_RET, VC)
    pats = _section35_patches(C)
    bad = [SimplePatch(pats[0].host_off, bytes.fromhex("8b5d01"), bytes.fromhex("669090"))]
    with pytest.raises(InPlaceError) as e:
        build_inplace_copy(snap, SNAP_DIR, r_vaddr=R_RET, vc=VC, patches=bad,
                           out_dir=tmp_path / "a", layout=layout)
    assert e.value.code == "link:patch_old_bytes"
    with pytest.raises(InPlaceError) as e:
        build_inplace_copy(snap, SNAP_DIR, r_vaddr=R_RET, vc=VC, patches=pats,
                           out_dir=tmp_path / "b", layout=layout, slot_n=7)
    assert e.value.code == "no_free_slot"
    with pytest.raises(InPlaceError) as e:
        build_inplace_copy(snap, SNAP_DIR, r_vaddr=R_RET, vc=VC | 1, patches=pats,
                           out_dir=tmp_path / "c", layout=layout)
    assert e.value.code in ("vc_thumb_bit", "ambiguous_c")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "x").write_text("x")
    with pytest.raises(InPlaceError) as e:
        build_inplace_copy(snap, SNAP_DIR, r_vaddr=R_RET, vc=VC, patches=pats,
                           out_dir=tmp_path / "d", layout=layout)
    assert e.value.code == "out_dir_not_fresh"


def test_window_twin_script_shape():
    lines = window_twin_script([0x1000, 0x2000])
    from scopt.run.diag import FLUSH_FUNCS

    n = len(FLUSH_FUNCS)
    assert lines[-1] == "run"
    assert f"disable 3-{2 + n}" in lines
    # flush breakpoints stop (no ignore count on them) and print the probe counts
    assert not any(x.startswith("ignore ") and int(x.split()[1]) <= 2 + n for x in lines)
    assert "hbreak *0x1000" in lines and f"ignore {3 + n} 1000000000" in lines
    assert "hbreak *0x2000" in lines and f"ignore {4 + n} 1000000000" in lines


def _ib(rows):
    out = ["SCOPT info-breakpoints-begin",
           "Num     Type           Disp Enb Address            What"]
    for k, (addr, hits) in enumerate(rows, start=30):
        out.append(f"{k}       hw breakpoint  keep y   0x{addr:016x} ")
        if hits:
            out.append(f"\tbreakpoint already hit {hits} times")
    out.append("SCOPT info-breakpoints-end")
    return out


def test_parse_window_twin():
    a, b = 0x1000, 0x2000
    text = "\n".join(["SCOPT benchmark_start bp_head=(nil) wp_head=(nil) singlestep=0",
                      "SCOPT flush bp=3", *_ib([(a, 1000), (b, 0)]),
                      "SCOPT benchmark_end bp_head=(nil) wp_head=(nil) singlestep=0",
                      *_ib([(a, 1000), (b, 0)])])
    d = parse_window_twin(text, [a, b])
    assert d["flush_stops"] == 1 and d["flushes"][0]["at"] == "tlb_flush"
    assert d["final_probe_hits"] == {hex(a): 1000, hex(b): 0}
    assert d["flushes_before_last_probe_hit"] == 0
    # a flush while C still executes afterwards is counted
    text2 = text.replace("already hit 1000 times", "already hit 400 times", 1)
    d2 = parse_window_twin(text2, [a, b])
    assert d2["flushes_before_last_probe_hit"] == 1
    # no benchmark_end: unknown final counts, never "clean"
    d3 = parse_window_twin("\n".join(text.splitlines()[:6]), [a, b])
    assert d3["benchmark_end_seen"] is False and d3["final_probe_hits"] is None

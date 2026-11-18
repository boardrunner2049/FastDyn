"""Unit tests for the self-contained .qprof checker in tests/qemu/qprof_smoke.py
(WP5). They build small synthetic .qprof v1 files from the frozen layouts."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from scopt.canon import fnv1a64
from scopt.formats import layouts as L

_SPEC = importlib.util.spec_from_file_location(
    "qprof_smoke", Path(__file__).resolve().parents[1] / "qemu" / "qprof_smoke.py")
smoke = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("qprof_smoke", smoke)
_SPEC.loader.exec_module(smoke)

NONE = 0xFFFF_FFFF_FFFF_FFFF
A, B = 0x100, 0x200


def build(edges=None, entries=None, flags=1, phases=(("boot", 0, 1, 0),), total=None,
          corrupt_trailer=False):
    # dispatch sequence A B A B: FIRST->A, A->B x2 (chain0), B->A x1 (chain1)
    entries = entries or {A: [2], B: [2]}
    edges = edges if edges is not None else [
        (NONE, A, 7, 0, 0, 1), (A, B, 1, 0, 0, 2), (B, A, 2, 0, 0, 1)]
    n_ph = len(phases)
    body = b""
    for i, (name, marker, reached, fdi) in enumerate(phases):
        body += L.QPROF_PHASE.pack(i, reached, marker, fdi, name.encode())
    for off, per in entries.items():
        body += L.QPROF_TB_ROW.pack(off, 0x08000000 + off, 0x08000000, NONE, 0, 0x110, 0xFF020000,
                                    4, 2, 0, 0, 0)
        for p in range(n_ph):
            body += L.QPROF_TB_PHASE.pack(per[p], 0, 0, 0)
    for e in edges:
        body += L.QPROF_EDGE.pack(*e)
    if total is None:
        total = sum(sum(v) for v in entries.values())
    hdr = bytearray(L.QPROF_HEADER.pack(L.QPROF_MAGIC, 1, 128, 0x7fff6c000000, 0x1234, 2, 0x400, 0x99,
                                        n_ph, flags, len(entries), len(edges), total, 0, 0, 0, 0, 0))
    hdr[120:128] = fnv1a64(bytes(hdr[:120])).to_bytes(8, "little")
    trailer = fnv1a64(body) ^ (1 if corrupt_trailer else 0)
    return bytes(hdr) + body + trailer.to_bytes(8, "little")


def failed(data, **kw):
    c, _ = smoke.check_qprof(smoke.parse_qprof(data), **kw)
    return {n for n, ok, _ in c.rows if not ok}


def test_valid_file_passes():
    c, stats = smoke.check_qprof(smoke.parse_qprof(build()))
    assert c.ok, c.report()
    assert stats["last_tb_offset"] == hex(B)
    assert stats["edge_kind_counts"] == {"CHAIN0": 2, "CHAIN1": 1, "FIRST": 1}


def test_broken_in_flow_detected():
    data = build(edges=[(NONE, A, 7, 0, 0, 1), (A, B, 1, 0, 0, 1), (B, A, 2, 0, 0, 1)])
    assert "flow_in" in failed(data)


def test_broken_out_flow_detected():
    # every in-edge correct for B, but A's out-edges come from B instead
    data = build(edges=[(NONE, A, 7, 0, 0, 1), (B, B, 1, 0, 0, 2), (B, A, 2, 0, 0, 1)])
    assert "flow_out" in failed(data)


def test_total_dispatch_mismatch_detected():
    assert "total_dispatches" in failed(build(total=5))


def test_unclean_and_overflow_flags():
    assert {"clean_exit"} <= failed(build(flags=0))
    assert {"no_overflow"} <= failed(build(flags=1 | 2))
    assert not failed(build(flags=0), require_clean=False)


def test_trailer_and_header_checksums():
    assert "trailer" in failed(build(corrupt_trailer=True))
    data = bytearray(build())
    data[16] ^= 1
    with pytest.raises(smoke.QprofError, match="header checksum"):
        smoke.parse_qprof(bytes(data))


def test_provisional_header_only_is_rejected():
    hdr = bytearray(L.QPROF_HEADER.pack(L.QPROF_MAGIC, 1, 128, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0,
                                        0, 0, 0, 0))
    hdr[120:128] = fnv1a64(bytes(hdr[:120])).to_bytes(8, "little")
    with pytest.raises(smoke.QprofError, match="provisional"):
        smoke.parse_qprof(bytes(hdr))


def test_phase_checks():
    two = (("boot", 0, 1, 0), ("steady", 0x08003972, 2, 2))
    data = build(phases=two, entries={A: [1, 1], B: [1, 1]},
                 edges=[(NONE, A, 7, 0, 0, 1), (A, B, 1, 0, 0, 1), (B, A, 2, 0, 1, 1),
                        (A, B, 1, 0, 1, 1)])
    assert not failed(data)
    # entries in a phase that was never reached
    bad = (("boot", 0, 1, 0), ("steady", 0x08003972, 0, 0))
    assert "unreached_phase_zero" in failed(build(phases=bad, entries={A: [1, 1], B: [1, 1]},
                                                 edges=[(NONE, A, 7, 0, 0, 1), (A, B, 1, 0, 0, 1),
                                                        (B, A, 2, 0, 1, 1), (A, B, 1, 0, 1, 1)]))

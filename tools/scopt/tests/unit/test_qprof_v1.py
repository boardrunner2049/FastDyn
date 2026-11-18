"""WP6: .qprof v1 reader/writer and the synthetic builder."""

from __future__ import annotations

import pytest

from scopt.canon import fnv1a64
from scopt.formats import layouts as L
from scopt.formats import qprof_v1 as Q
from scopt.formats.enums import PhaseMatch, QprofEdgeKind, QprofHeaderFlag
from tests.synth import builders as B

ROWS = [{"tb_offset": 0x40, "first_vaddr": 0x8000100, "size": 4, "icount": 2,
         "phase": [{"entries": 3, "aborted": 1, "longjmp": 0, "lookup_hits": 1},
                   {"entries": 2}]},
        {"tb_offset": 0x140, "first_vaddr": 0x8000104, "phase": {1: {"entries": 1}}}]
EDGES = [{"src": None, "dst": 0x40, "kind": "first", "phase": 0, "count": 1},
         {"src": 0x40, "dst": 0x40, "kind": QprofEdgeKind.CHAIN0, "phase": 0, "count": 2},
         {"src": 0x40, "dst": 0x40, "kind": "chain1", "flags": 1, "phase": 1, "count": 2},
         {"src": 0x40, "dst": 0x140, "kind": "lookup_hit", "phase": 1, "count": 1}]
PH = [{"name": "boot", "marker": 0}, {"name": "steady", "marker": 0x8000200, "reached": 2,
                                       "first_dispatch_index": 3}]


def _file(**hdr):
    return B.build_qprof(ROWS, EDGES, phases=PH, header=hdr or None)


def test_parse_and_roundtrip():
    data = _file()
    q = Q.parse_qprof(data)
    assert q.header.n_phases == 2 and q.header.n_tb_rows == 2 and q.header.n_edge_rows == 4
    assert q.header.total_dispatches == 6 and q.header.clean_exit and not q.header.overflow
    assert len(data) == 128 + 2 * 40 + 2 * (72 + 64) + 4 * 32 + 8
    assert q.phases[1].match is PhaseMatch.CONTAINED and q.phases[0].match is PhaseMatch.EXACT_START
    assert q.phases[1].name == "steady" and q.phases[1].marker_vaddr == 0x8000200
    assert q.tbs[0].phases[0] == Q.QprofTbPhase(3, 1, 0, 1) and q.tbs[0].phases[0].executions == 2
    assert q.tbs[1].phases[0].entries == 0 and q.tbs[1].phases[1].entries == 1
    assert q.edges[0].src == Q.NONE_SRC and q.edges[0].kind_name == "first"
    assert q.edges[2].flag_names == ["irq"]
    again = Q.encode_qprof(q.header, q.phases, q.tbs, q.edges)
    assert again == data
    assert q.summary()["entries_per_phase"] == [3, 3]


def test_header_checksum_and_trailer_cover_the_right_bytes():
    data = _file()
    assert int.from_bytes(data[120:128], "little") == fnv1a64(data[:120])
    assert int.from_bytes(data[-8:], "little") == fnv1a64(data[128:-8])


@pytest.mark.parametrize("mutate,reason", [
    (lambda d: d[:100], "short_file"),
    (lambda d: b"QSCPROF2" + d[8:], "magic"),
    (lambda d: B.build_qprof(ROWS, EDGES, phases=PH, header={"version": 2}), "version"),
    (lambda d: B.build_qprof(ROWS, EDGES, phases=PH, header={"header_size": 120}), "header_size"),
    (lambda d: B.build_qprof(ROWS, EDGES, phases=PH, header={"checksum": 5}), "header_checksum"),
    (lambda d: d[:-9], "size"),
    (lambda d: d + b"\0" * 8, "size"),
    (lambda d: B.build_qprof(ROWS, EDGES, phases=PH, header={"trailer": 1}), "trailer"),
    (lambda d: d[:200] + bytes([d[200] ^ 1]) + d[201:], "trailer"),
])
def test_rejections(mutate, reason):
    with pytest.raises(Q.QprofFormatError) as ei:
        Q.parse_qprof(mutate(_file()))
    assert ei.value.reason == reason


def test_provisional_header_is_rejected():
    # What QEMU leaves when it dies before atexit: valid header, flags 0, no body.
    hdr = Q.QprofHeader(L.QPROF_MAGIC, 1, 128, 0x7FFF6C000000, 1, 163, 0x1000, 2, 2, 0, 0, 0, 0,
                        1, (0, 0, 0), 0)
    with pytest.raises(Q.QprofFormatError) as ei:
        Q.parse_qprof(hdr.pack())
    assert ei.value.reason == "provisional"


def test_flags():
    q = Q.parse_qprof(_file(flags=int(QprofHeaderFlag.TB_OVERFLOW)))
    assert not q.header.clean_exit and q.header.tb_overflow and q.header.overflow
    q = Q.parse_qprof(_file(flags=int(QprofHeaderFlag.CLEAN_EXIT | QprofHeaderFlag.EDGE_OVERFLOW)))
    assert q.header.clean_exit and q.header.edge_overflow


def test_read_qprof_file(tmp_path):
    p = tmp_path / "x.qprof"
    B.build_qprof(ROWS, EDGES, phases=PH, path=str(p))
    q = Q.read_qprof(p)
    assert q.path == str(p) and len(q.sha256) == 64 and q.size == p.stat().st_size


def test_simulate_qprof_counts_like_scp():
    rows = [{"tb_offset": 0x40}, {"tb_offset": 0x80}]
    D = B.Dispatch
    data = B.simulate_qprof(rows, [D(0x40), D(0x80, exit="exit_requested"), D(0x40, exit="longjmp"),
                                   D(0x80)],
                            phases=PH[:1] + [{"name": "steady", "marker": 0x99}],
                            phase_starts={1: 3})
    q = Q.parse_qprof(data)
    assert [t.tb_offset for t in q.tbs] == [0x40, 0x80]
    assert q.tbs[1].phases[0] == Q.QprofTbPhase(1, 1, 0, 0)
    assert q.tbs[0].phases[0] == Q.QprofTbPhase(2, 0, 1, 0)
    assert q.tbs[1].phases[1].entries == 1
    kinds = {(e.src, e.dst, e.kind_name, e.flags, e.phase): e.count for e in q.edges}
    assert kinds == {(Q.NONE_SRC, 0x40, "first", 0, 0): 1, (0x40, 0x80, "chain0", 0, 0): 1,
                     (0x80, 0x40, "exit_requested", 0, 0): 1,
                     (0x40, 0x80, "exit_null", 4, 1): 1}
    assert q.phases[1].first_dispatch_index == 3 and q.phases[1].is_reached

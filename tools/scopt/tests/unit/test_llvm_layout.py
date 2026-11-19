from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from scopt.analysis.llvm_layout import OPERATION, build_cache_directed_request
from scopt.formats.enums import Encoding, SiteKind
from scopt.manager import load_pipeline
from scopt.passes.cluster import ClusterHot
from scopt.passes.llvm_layout import LLVMCacheLayout, _bridge_order


def site(kind, off, slot=0):
    return SimpleNamespace(kind=kind, insn_off=off, aux=slot,
                           enc=Encoding.GOTO_TB_REL32 if kind == SiteKind.GOTO_TB
                           else Encoding.NONE)


def unit(uid, *sites):
    return SimpleNamespace(uid=uid, sites=sites, tc_size=96, tb_offset=512, end_offset=720,
                           stub_start=None)


def edge(src, dst, kind, count, *, status="exact", flags=()):
    return dict(src=src, dst=dst, kind=kind, count=count, status=status, flags=flags)


def test_exact_transfer_offsets_and_missing_evidence():
    units = [unit("A", site(SiteKind.GOTO_TB, 31), site(SiteKind.GOTO_TB, 62, 1),
                  site(SiteKind.GOTO_PTR, 70), site(SiteKind.GOTO_PTR, 80)),
             unit("B", site(SiteKind.GOTO_PTR, 91)), unit("ZERO"), unit("UNKNOWN")]
    counts = {"A": 10, "B": 10, "ZERO": 0}
    edges = [edge("A", "B", "chain0", 3), edge("A", "B", "chain1", 4),
             edge("A", "B", "lookup_hit", 7), edge("B", "A", "lookup_hit", 9),
             edge("A", "ZERO", "chain1", 0), edge("A", "UNKNOWN", "chain0", 10),
             edge("A", "B", "chain0", 14, status="lower_bound"),
             edge("A", "B", "chain0", 15, flags=("irq",))]
    result, unlocated = build_cache_directed_request(
        units, counts, reversed(edges), ("chain0", "chain1", "lookup_hit"))
    assert result["nodes"] == [{"size": 256, "count": n} for n in (10, 10, 0)]
    assert result["edges"] == [
        {"src": 0, "dst": 1, "offset": 31, "count": 3},
        {"src": 0, "dst": 1, "offset": 62, "count": 4},
        {"src": 0, "dst": 2, "offset": 62, "count": 0},
        {"src": 1, "dst": 0, "offset": 91, "count": 9},
    ]
    assert unlocated == 1


def test_registered_pipeline_and_deterministic_fallback(monkeypatch):
    pipeline = load_pipeline("b2-llvm-layout")
    assert [p.name for p in pipeline.passes] == [
        "hotcold-partition", "llvm-cache-layout", "order-cold"]
    monkeypatch.delenv("SCOPT_HOST_INLINE_BRIDGE", raising=False)
    monkeypatch.delenv("SCOPT_LLVM_LAYOUT_BRIDGE", raising=False)
    ctx = SimpleNamespace(params={}, budget=lambda: None)
    movable = ["A", "C", "B"]
    edges = [edge("A", "B", "chain0", 3)]
    args = (None, ctx, movable, {"A": 4, "B": 3, "C": 2}, edges, ("chain0",))
    order, reason, _, _ = LLVMCacheLayout()._order(*args)
    assert reason == "pettis_hansen_fallback"
    assert order == ClusterHot()._order(*args)[0] == ["A", "B", "C"]


@pytest.mark.parametrize("indices", [[0, 0], [True, 0], [0, 2], [0], {"0": 0}])
def test_bridge_cannot_submit_invalid_permutation(monkeypatch, indices):
    response = {"version": 1, "status": "ok", "operation": OPERATION, "order": indices}
    monkeypatch.setattr(subprocess, "run",
                        lambda *args, **kw: SimpleNamespace(stdout=json.dumps(response)))
    with pytest.raises(ValueError, match="permutation"):
        _bridge_order(Path("/unused"), {"nodes": [{}, {}]}, 1)


def test_pinned_native_bridge_returns_deterministic_order():
    bridge = os.environ.get("SCOPT_LLVM_LAYOUT_BRIDGE")
    if not bridge:
        pytest.skip("SCOPT_LLVM_LAYOUT_BRIDGE names the optional pinned executable")
    request = {"version": 1, "operation": OPERATION,
               "nodes": [{"size": 128, "count": n} for n in (100, 5, 100)],
               "edges": [{"src": 0, "dst": 2, "offset": 84, "count": 90}]}
    assert _bridge_order(Path(bridge), request, 10) == [0, 2, 1]
    request["edges"][0]["offset"] = 128
    with pytest.raises(ValueError, match="refused"):
        _bridge_order(Path(bridge), request, 10)

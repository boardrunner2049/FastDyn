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


def test_transformed_lookup_accounting_conserves_distinct_routes():
    from scopt.analysis.llvm_layout import GuardedLookup, GuardTransfer, LayoutTraffic

    # Target C shares B's within-page offset at another address. Its identity
    # alone cannot prove that a virtual-PC guard missed (or hit).
    units = []
    for uid, pc in (("A", 0x110), ("B", 0x128), ("C", 0x528), ("D", 0x144), ("E", 0x168)):
        u = unit(uid)
        u.key = SimpleNamespace(page_addr0=pc)
        units.append(u)
    updates = {"A": GuardedLookup(89, 0x3ff, (
        GuardTransfer("B", 0x128, 48, "GENERAL", 0), GuardTransfer("E", 0x168, 64, "SPEC", 1)))}
    heat = {u.uid: 50 for u in units}
    edges = [edge("A", dst, "lookup_hit", n) for dst, n in (
        ("A", 1), ("B", 15), ("C", 4), ("D", 6), ("E", 7))]
    for replays, represented, unresolved in ((None, 6, 26), (2, 19, 11)):
        ledger = LayoutTraffic()
        req, missing = build_cache_directed_request(units, heat, edges, ["lookup_hit"],
                                                   guarded=updates, estimate_replays=replays,
                                                   traffic=ledger)
        assert missing == 3
        assert sum(row["count"] for row in req["edges"]) == represented
        assert ledger.stats()["llvm_traffic_total"] == 33
        assert ledger.weights["guard_unresolved"] == unresolved
        assert ledger.weights["forced_fallback"] == 6
        assert ledger.weights["self_excluded"] == 1
        assert ledger.weights["estimated_install"] == (2 if replays else 0)
        if replays:
            assert req["edges"] == [{"src": 0, "dst": 1, "offset": 48, "count": 13},
                                    {"src": 0, "dst": 3, "offset": 89, "count": 6}]


def test_private_variant_source_retains_unattributed_outgoing_weight():
    from scopt.analysis.llvm_layout import LayoutTraffic

    u = unit("A", site(SiteKind.GOTO_TB, 31))
    ledger = LayoutTraffic()
    req, missing = build_cache_directed_request([u], {"A": 12}, [edge("A", "A", "chain0", 12)],
                                               ["chain0"], private_sources={"A"},
                                               include_self_loops=True, traffic=ledger)
    assert req["edges"] == [] and missing == 1
    assert ledger.weights == {"private_source_unlocated": 12}


def test_matching_generation_profile_can_locate_the_unique_emitted_lookup():
    from scopt.analysis.llvm_layout import LayoutTraffic

    grown = unit("A", site(SiteKind.GOTO_PTR, 89), site(SiteKind.GOTO_TB, 48))
    grown.stub_start = 40
    edges = [edge("A", "B", "lookup_hit", 5)]
    for match in (False, True):
        req, missing = build_cache_directed_request([grown, unit("B")], {"A": 5, "B": 5},
                                                   edges, ["lookup_hit"], same_generation=match,
                                                   traffic=LayoutTraffic())
        assert missing == int(not match)
        assert [x["offset"] for x in req["edges"]] == ([89] if match else [])

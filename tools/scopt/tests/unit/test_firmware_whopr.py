"""GCC-style sealed-firmware WPA partitioning."""

import hashlib
from types import SimpleNamespace

from scopt.analysis.firmware_whopr import (
    FirmwareWhoprSummary, WhoprEdge, WhoprNode, balanced_partitions,
)
from scopt.manager import load_pipeline, pipelines_dir
from scopt.model.native import NativeFrameBody
from scopt.passes.host_inline import HostInline
from scopt.passes.host_inline_graph import HostInlineGraph


def test_balanced_partitions_keep_sccs_atomic_and_complete():
    nodes = tuple(WhoprNode(chr(97 + i), i, size, 1, False)
                  for i, size in enumerate((100, 80, 70, 60, 50)))
    edges = (
        WhoprEdge("ab", "a", "b"), WhoprEdge("bc", "b", "c"),
        WhoprEdge("cb", "c", "b"), WhoprEdge("cd", "c", "d"),
        WhoprEdge("de", "d", "e"),
    )
    summary = FirmwareWhoprSummary(
        "snapshot", nodes, edges, (("a",), ("b", "c"), ("d",), ("e",)),
        (0, 1, 2, 3), tuple(node.uid for node in nodes), ())
    partitions = balanced_partitions(summary, 3)
    assert sorted(i for partition in partitions for i in partition) == [0, 1, 2, 3]
    assert len(partitions) == 3
    assert sum(1 in partition for partition in partitions) == 1


def test_configuration_7_is_explicit_trusted_whopr_pipeline():
    pipeline = load_pipeline("sealed-whopr-7")
    assert pipeline.name == "sealed-whopr-7"
    helper_graph, firmware = pipeline.passes
    assert helper_graph.name == "host-inline"
    assert helper_graph.params == {
        "mode": "maximal", "trusted_all_helpers": True, "allow_partial": True,
    }
    assert not ({"max_sites", "max_sites_per_unit", "max_growth_per_unit",
                 "max_total_growth", "max_inline_depth"} & helper_graph.params.keys())
    assert HostInline.descriptor.budgets.max_wall_s is None
    assert HostInline.descriptor.budgets.max_code_growth_bytes is None
    assert firmware.name == "firmware-inline"
    assert firmware.params == {
        "mode": "exhaustive", "trust": "user", "interrupts": "disabled",
        "icount": "ignored", "planner": "whopr", "partitions": 4, "workers": 4,
    }

def test_sealed_pgo_selects_repeated_observed_calls_for_both_stages():
    helper, guest = load_pipeline("sealed-pgo-7").passes
    assert helper.name == "host-inline" and helper.params["hot_loop_only"]
    assert helper.params["min_site_calls"] == 1000 and helper.params["trusted_selected_root"]
    assert guest.name == "firmware-inline" and guest.params["mode"] == "hot-loop"
    assert guest.params["min_call_count"] == 1000 and guest.params["trust"] == "user"
    assert guest.params["interrupts"] == "disabled" and guest.params["icount"] == "ignored"


# -- sealed-whopr-7-bounded: configuration 7 without the inlined lookup ------

#: Captured source graph of helper_lookup_tb_ptr (root plus its direct-call
#: graph) and of the next largest helper, helper_v7m_msr, on the FirmBOLT -O4
#: packet, UART and CLI captures on QEMU aacca05 (round 2).
_LOOKUP_GRAPH_BYTES = 50_345
_NEXT_GRAPH_BYTES = 6_500
_BOUND = 32_768

#: sealed-whopr-7 and sealed-whopr-7-b3 are archived provenance: never edit
#: them, add a new pipeline instead.
_WHOPR7_TOML_SHA256 = "b98ef880815ce00df60f2d9260a8d99a0684f050068313a6fd4d0564c7742510"
_WHOPR7_ID = "sha256:217d45302c84488b6a0a0cc2e390ed83dfdc4aa27d2d9e2fb91e126d36bc5f8d"
_WHOPR7_B3_ID = "sha256:b3db04d53fd267ceaf09426c2fa7df2581286278c1eac2bad976d4c58ec5d2b5"


def _without_bound(pipeline):
    """(name, version, params) of each pass, with the graph bound removed."""
    return [(p.name, p.version,
             {k: v for k, v in p.params.items() if k != "trusted_max_source_graph_bytes"})
            for p in pipeline.passes]


def test_configuration_7_pipelines_stay_unchanged():
    toml = (pipelines_dir() / "sealed-whopr-7.toml").read_bytes()
    assert hashlib.sha256(toml).hexdigest() == _WHOPR7_TOML_SHA256
    assert load_pipeline("sealed-whopr-7").pipeline_id == _WHOPR7_ID
    assert load_pipeline("sealed-whopr-7-b3").pipeline_id == _WHOPR7_B3_ID


def test_bounded_configuration_7_only_adds_the_graph_bound():
    original = load_pipeline("sealed-whopr-7")
    bounded = load_pipeline("sealed-whopr-7-bounded")
    assert bounded.name == "sealed-whopr-7-bounded"
    assert bounded.pipeline_id != original.pipeline_id
    assert bounded.allowed_capabilities == original.allowed_capabilities
    helper, firmware = bounded.passes
    assert helper.name == "host-inline"
    assert helper.params == {
        "mode": "maximal", "trusted_all_helpers": True, "allow_partial": True,
        "trusted_max_source_graph_bytes": _BOUND,
    }
    assert not ({"max_sites", "max_sites_per_unit", "max_growth_per_unit",
                 "max_total_growth", "max_inline_depth", "trusted_rank_by",
                 "trusted_max_total_growth"} & helper.params.keys())
    assert _without_bound(bounded) == _without_bound(original)
    assert firmware.params == original.passes[1].params


def test_bounded_configuration_7_b3_is_sealed_whopr_7_b3_with_the_bound():
    composed = load_pipeline("sealed-whopr-7-b3")
    bounded = load_pipeline("sealed-whopr-7-bounded-b3")
    assert bounded.name == "sealed-whopr-7-bounded-b3"
    assert bounded.pipeline_id not in {composed.pipeline_id,
                                       load_pipeline("sealed-whopr-7-bounded").pipeline_id}
    assert bounded.allowed_capabilities == composed.allowed_capabilities
    assert _without_bound(bounded) == _without_bound(composed)
    assert [p.params.get("trusted_max_source_graph_bytes") for p in bounded.passes] == \
        [_BOUND] + [None] * (len(bounded.passes) - 1)
    assert bounded.passes[0].params == load_pipeline("sealed-whopr-7-bounded").passes[0].params


def _graph_binding(total: int):
    root = 64
    return SimpleNamespace(callee=SimpleNamespace(
        code=b"r" * root, frame_chain=(NativeFrameBody(0x1000, b"c" * (total - root)),)))


def test_bound_excludes_the_lookup_graph_and_admits_the_next_largest():
    params = dict(load_pipeline("sealed-whopr-7-bounded").passes[0].params)
    policy = HostInlineGraph()
    reason, detail = policy._policy_refusal(_graph_binding(_LOOKUP_GRAPH_BYTES), params)
    assert reason == "helper_graph_size_policy"
    assert f"{_LOOKUP_GRAPH_BYTES} bytes" in detail and str(_BOUND) in detail
    assert policy._policy_refusal(_graph_binding(_NEXT_GRAPH_BYTES), params) is None
    # sealed-whopr-7 is unbounded, and a 65536 bound would not exclude the lookup.
    unbounded = dict(load_pipeline("sealed-whopr-7").passes[0].params)
    assert policy._policy_refusal(_graph_binding(_LOOKUP_GRAPH_BYTES), unbounded) is None
    assert policy._policy_refusal(
        _graph_binding(_LOOKUP_GRAPH_BYTES),
        {**params, "trusted_max_source_graph_bytes": 65_536}) is None

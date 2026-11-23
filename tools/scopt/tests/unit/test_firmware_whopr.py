"""GCC-style sealed-firmware WPA partitioning."""

from scopt.analysis.firmware_whopr import (
    FirmwareWhoprSummary, WhoprEdge, WhoprNode, balanced_partitions,
)
from scopt.manager import load_pipeline
from scopt.passes.host_inline import HostInline


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

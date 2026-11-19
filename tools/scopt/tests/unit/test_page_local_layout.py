from types import SimpleNamespace

from scopt.analysis.llvm_layout import build_cache_directed_request
from scopt.analysis.page_layout import improve_page_layout
from scopt.formats.enums import Encoding, SiteKind


def edge(src, dst, offset=80, count=10):
    return {"src": src, "dst": dst, "offset": offset, "count": count}


def improve(edges, *, protected=None, nodes=2):
    return improve_page_layout(
        [64 + i * 128 for i in range(nodes)], [128] * nodes, [32] * nodes,
        edges, protected or {}, {0, 1}, page_size=256, max_rounds=3, max_swaps=32,
        check_budget=lambda: None,
    )


def test_adjacent_swap_reduces_profiled_self_loop_prefix_crossing():
    result = improve([edge(1, 1)])
    assert result.order == (1, 0)
    assert result.swaps == 1
    assert result.weighted_crossings == (10, 0)
    assert result.weighted_distance == (0, 0)


def test_original_directed_adjacency_protects_even_unlocated_transfer():
    result = improve([edge(1, 1)], protected={(0, 1): 1})
    assert result.order == (0, 1)
    assert result.swaps == 0
    assert result.protected_adjacency_weight == 1
    assert result.blocked_by_original_adjacency == 1


def test_page_gain_cannot_increase_weighted_inter_block_distance():
    result = improve([edge(1, 3)], nodes=4)
    assert result.order == (0, 1, 2, 3)
    assert result.swaps == 0
    assert result.weighted_crossings == (10, 10)
    assert result.weighted_distance == (1760, 1760)
    assert result.blocked_by_native_distance == 1


def test_use_exact_site_for_profitable_cross_block_swap_with_improved_distance():
    result = improve([edge(1, 3), edge(2, 0, offset=8, count=20)], nodes=4)
    assert result.order == (1, 0, 2, 3)
    assert result.weighted_crossings == (10, 0)
    assert result.weighted_distance[1] < result.weighted_distance[0]



def test_cache_directed_graph_can_select_unique_exact_self_loop_for_local_cost():
    exit_site = SimpleNamespace(insn_off=42, kind=SiteKind.GOTO_TB,
                                enc=Encoding.GOTO_TB_REL32, aux=0)
    unit = SimpleNamespace(uid="a", tc_size=80, tb_offset=64, end_offset=192,
                           sites=[exit_site])
    loop = {"src": "a", "dst": "a", "kind": "chain0", "status": "exact",
            "flags": [], "count": 50}
    plain, _ = build_cache_directed_request([unit], {"a": 51}, [loop], ["chain0"])
    expanded, unknown = build_cache_directed_request(
        [unit], {"a": 51}, [loop], ["chain0"], include_self_loops=True,
    )
    assert not plain["edges"]
    assert unknown == 0
    assert expanded["edges"] == [edge(0, 0, 42, 50)]

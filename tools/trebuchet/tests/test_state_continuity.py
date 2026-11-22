"""A bounded host CFG fixture exercises the census; it supplies no performance data."""
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
pytest.importorskip("scopt")
from state_continuity import boundary_accesses, match_edge
from scopt.analysis.hcfg import HCfg
from scopt.analysis.hostops import EnvAccess, HostOp, OpKind


def host(ops):
    return SimpleNamespace(ops=ops, ok=True, reasons=())


def graph(succ, exits):
    return HCfg("fixture", True, (), len(succ), tuple(succ), tuple(succ), {}, exits)


def plain(off, kind, **kw):
    return HostOp(off, 1, "fixture", kind, "insn", 0, **kw)


def store(off, width=4):
    return plain(off, OpKind.ENV_STORE_REG, src_reg="eax", env=(EnvAccess("regs[0]", 8, width, "w", True),))


def load(off, width=4):
    return plain(off, OpKind.ENV_LOAD, dst_reg="ebx", env=(EnvAccess("regs[0]", 8, width, "r", True),))


def test_conditional_store_fork_and_opaque_observation():
    source = host([plain(0, OpKind.JCC), store(1), plain(2, OpKind.CALL, regs_written=frozenset({"rax"})),
                   plain(3, OpKind.JMP)])
    old = boundary_accesses(source, graph([(1, 2), (2,), (3,), ()], {3: "goto_tb:0"}), "goto_tb:0")
    new = boundary_accesses(host([load(0), plain(1, OpKind.JMP)]), graph([(1,), ()], {1: "goto_tb:1"}))
    row, = match_edge({"count": 19, "status": "exact"}, old, new)
    assert row["store"]["paths"] == 1 and row["store"]["paths_analyzed"] == 2
    assert row["store"]["observers"] == [{"host_off": 2, "reason": "helper_or_callback"}]
    assert row["known_value_register_clobber"] and row["possible_pair_crossings_upper_bound"] == 19
    assert match_edge({"count": 19, "status": "lower_bound"}, old, new)[0]["possible_pair_crossings_upper_bound"] is None


def test_conflicting_width_blocks_an_earlier_wide_store():
    src = host([store(0), store(1, 1), plain(2, OpKind.JMP)])
    old = boundary_accesses(src, graph([(1,), (2,), ()], {2: "goto_tb:0"}), "goto_tb:0")
    new = boundary_accesses(host([load(0)]), graph([()], {0: "exit_null"}))
    assert len(old["sites"]) == 1 and old["sites"][0]["width"] == 1
    assert match_edge({"count": 4, "status": "exact"}, old, new) == []


def test_wrong_exit_slot_excludes_that_store():
    src = host([store(0)])
    old = boundary_accesses(src, graph([()], {0: "goto_tb:1"}), "goto_tb:0")
    assert not old["sites"] and old["reason"] == ["no_matching_path"]

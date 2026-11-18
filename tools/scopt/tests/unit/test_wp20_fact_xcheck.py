"""WP20 integration: two-sided proposer / hostsym agreement (scopt.analysis.fact_xcheck)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from scopt.analysis.exit_facts import ExitFactSet, FactRow, LookupTail, Refusal, UnitExitFacts
from scopt.analysis.fact_xcheck import OUTCOMES, xcheck_unit
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
MINIMAL = ROOT / ("runs/wp15a/minimal/store/snapshots/"
                  "f1ed14d98db4bcae54a1c37f07b1d6fabcfcea345a56edf1df1846c4d0aaee44")
FLAGS = {"NF": 0x40, "ZF": 0x44, "CF": 0x48, "VF": 0x4c}


class Layout:
    def has_field(self, name):
        return name.startswith("regs[") or name in FLAGS

    def offset(self, name):
        return 4 * int(name[5:-1]) if name.startswith("regs[") else FLAGS[name]


class Env:
    def __init__(self, vals):
        self.vals = vals

    def read(self, off, width):
        return self.vals.get(off, ("sym", off))  # not an int: a symbolic term


TAIL = LookupTail(copy_idx=3, call_idx=4, goto_idx=5, off=0x40, end=0x4b, insn=2)


def _path(vals, pc=TAIL.off + 9):
    return NS(events=[NS(kind="call", cls="lookup", env=Env(vals), exit_kind=None, pc=pc - 6),
                      NS(kind="exit", cls=None, env=None, exit_kind="goto_ptr", pc=pc)])


def _run(facts, refused, paths, complete=True):
    fx = UnitExitFacts("tb:x#0", "ok", (), (ExitFactSet(TAIL, tuple(facts), tuple(refused), ()),))
    ex = NS(complete=complete, paths=paths)
    rows = xcheck_unit(NS(uid="tb:x#0", vaddrs=(0x1000,)), fx, None, Layout(),
                       explore=lambda p, pre, layout=None: ex)
    return {r["field"]: r for r in rows}


def test_outcomes_classified():
    rows = _run([FactRow("r0", 0, ((0x10, 1),)), FactRow("r1", 1, ((0x14, 1),)),
                 FactRow("r2", 3, ((0x18, 1),))],
                [Refusal("r3", "fact_killed_by_helper")],
                [_path({0: 0, 4: 2, 16: 7})])
    assert len(rows) == 17
    assert rows["r0"]["outcome"] == "agree_const"
    assert rows["r1"]["outcome"] == "fact_derivation_mismatch"  # hostsym derives 2
    assert rows["r2"]["outcome"] == "fact_derivation_mismatch"  # hostsym: not constant
    assert rows["r3"]["outcome"] == "agree_nonconst" and \
        rows["r3"]["proposer"] == "fact_killed_by_helper"
    assert rows["r4"]["outcome"] == "proposer_weaker"  # untracked, hostsym constant 7
    assert rows["NF"]["outcome"] == "agree_nonconst" and rows["NF"]["proposer"] == "untracked"
    assert set(r["outcome"] for r in rows.values()) <= set(OUTCOMES)


def test_paths_disagree_and_incomplete():
    rows = _run([FactRow("r0", 0, ((0x10, 1),))], [], [_path({0: 0}), _path({0: 1})])
    assert rows["r0"]["outcome"] == "fact_derivation_mismatch"
    assert rows["r0"]["hostsym"] == "not_constant"
    rows = _run([FactRow("r0", 0, ((0x10, 1),))], [], [_path({0: 0})], complete=False)
    assert rows["r0"]["outcome"] == "inconclusive"
    # a path ending at another lookup exit does not count: the exit is unreached
    rows = _run([FactRow("r0", 0, ((0x10, 1),))], [], [_path({0: 0}, pc=0x99)])
    assert rows["r0"]["outcome"] == "inconclusive" and rows["r0"]["hostsym"] == "unreached"


@pytest.mark.real
@pytest.mark.slow
def test_minimal_xcheck_cli(tmp_path):
    """Real snapshot: no mismatch, and the proposed facts are among the agreements."""
    from click.testing import CliRunner

    from scopt.cli import cli, load_commands

    if not (MINIMAL / "snapshot.json").is_file():
        pytest.skip("minimal snapshot not present")
    load_commands()
    out = tmp_path / "x.json"
    r = CliRunner().invoke(cli, ["return-edges", "xcheck", str(MINIMAL), "--out", str(out)])
    assert r.exit_code == 0, r.output
    doc = json.loads(out.read_text())
    assert doc["mismatch"] == 0 and doc["units"] > 0
    assert doc["tally"]["agree_const"] > 0 and doc["tally"]["agree_nonconst"] > 0

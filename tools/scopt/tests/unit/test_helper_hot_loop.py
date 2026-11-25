import json
from types import SimpleNamespace

import pytest

from scopt.analysis.helper_hot_loop import (FIRMWARE_LOOP_KINDS, exact_loop_profile,
                                            load_site_counts, observed_loop_units,
                                            real_exit_null)
from scopt.canon import sha256_id
from scopt.formats.enums import SiteKind
from scopt.formats.profile_v1 import METHOD


def test_loop_selection_requires_the_reached_phase_of_the_same_capture():
    data = SimpleNamespace(snapshot_id="s", complete=True, method=METHOD,
                           collection=dict(overflow=False, chain_disabled=True),
                           phase_id=lambda phase: {"boot": 0, "steady": 1}.get(phase),
                           phase_reached=lambda phase_id: phase_id == 0)
    view = SimpleNamespace(profile=data)
    assert exact_loop_profile(view, "s", "boot")
    assert not exact_loop_profile(view, "other", "boot")
    assert not exact_loop_profile(view, "s", "steady")
    assert not exact_loop_profile(view, "s", "missing")
    view.target = SimpleNamespace(kind="helper_splice", snapshot_id="next", input_snapshot_ids=("s",))
    assert exact_loop_profile(view, "next", "boot")
    assert not exact_loop_profile(view, "unknown", "boot")
    view.target.kind = "artifact"
    assert not exact_loop_profile(view, "next", "boot")
    data.collection["overflow"] = True
    assert not exact_loop_profile(view, "s", "boot")


def test_observed_loop_members_exclude_entry_exit_and_interrupt_edges():
    def edge(src, dst, kind="chain1", count=1):
        return dict(src=src, dst=dst, count=count, kind=kind, status="exact", flags=[])

    assert observed_loop_units([edge("entry", "a"), edge("a", "b"), edge("b", "a"),
                                edge("b", "exit"), edge("s", "s"), edge("exit", "exit", count=0),
                                edge("exit", "entry", "exit_requested"),
                                dict(edge("exit", "entry"), flags=["exception"])]) == {"a", "b", "s"}


def test_measured_cold_is_distinct_from_missing(tmp_path):
    snap = SimpleNamespace(snapshot_id="test", units=lambda: [
        SimpleNamespace(uid="u", sites=[SimpleNamespace(kind=SiteKind.LDST_HELPER_CALL,
                                                        insn_off=18)])])
    path = tmp_path / "calls.json"
    doc = dict(schema="scopt.helper-call-profile/1", complete=True, snapshot_id="test",
               counts=[dict(unit="u", call_off=18, count=0)])
    path.write_text(json.dumps(doc))
    assert load_site_counts(path, snap, sha256_id(path.read_bytes())) == {("u", 18): 0}
    doc["counts"].clear()
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="site set"):
        load_site_counts(path, snap, sha256_id(path.read_bytes()))


def test_firmware_loop_kinds_include_real_exit_null_but_not_requested():
    def edge(src, dst, kind="chain0", flags=()):
        return dict(src=src, dst=dst, count=5, kind=kind, status="exact", flags=list(flags))

    def unit(handler):
        return SimpleNamespace(key=SimpleNamespace(cs_base=1 if handler else 0))

    units = {"a": unit(False), "b": unit(False), "isr": unit(True)}
    accept = real_exit_null(units.get)
    cycle = [edge("a", "b"), edge("b", "a", "exit_null")]
    assert observed_loop_units(cycle) == set()
    assert observed_loop_units(cycle, kinds=FIRMWARE_LOOP_KINDS, accept=accept) == {"a", "b"}
    # A re-dispatch after an aborted poll is not a transfer, flagged edges
    # are interrupts, and a handler-mode exit_tb(0) may be an EXC_RETURN.
    for rows in ([edge("a", "a", "exit_requested")],
                 [edge("a", "b"), edge("b", "a", "exit_null", ("irq",))],
                 [edge("a", "isr"), edge("isr", "a", "exit_null")],
                 [edge("isr", "b", "exit_null"), edge("b", "isr")],
                 [edge("a", "gone"), edge("gone", "a", "exit_null")]):
        assert observed_loop_units(rows, kinds=FIRMWARE_LOOP_KINDS, accept=accept) == set()

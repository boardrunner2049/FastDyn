import json
from types import SimpleNamespace

import pytest

from scopt.analysis.helper_hot_loop import exact_loop_profile, load_site_counts, observed_loop_units
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

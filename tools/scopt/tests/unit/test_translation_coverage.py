"""Coverage reports require an exact producer inventory and identity."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopt.canon import content_id, sha256_id
from scopt.formats import translation_coverage as coverage

_TRACE = """scopt-tcg-coverage-v1 800012c 8000134 8000100
0 800012c 802012c ffffffffffffffff 110 800400 ff020000 6 2
0 800012e 802012e ffffffffffffffff 110 800400 ff020000 4 2
0 8000130 8020130 ffffffffffffffff 110 800400 ff020000 2 1
0 8000132 8020132 ffffffffffffffff 110 800400 ff020000 2 1
1 800012c 802012c ffffffffffffffff 110 800400 ff020001 4 1
1 800012e 802012e ffffffffffffffff 110 800400 ff020001 2 1
1 8000130 8020130 ffffffffffffffff 110 800400 ff020001 2 1
1 8000132 8020132 ffffffffffffffff 110 800400 ff020001 2 1
end
"""


def test_modes_require_evidence_only_for_complete():
    assert coverage.select_coverage(object(), "partial", "/absent").reason == "explicit_partial"
    fallback = coverage.select_coverage(object(), "auto")
    assert (fallback.mode, fallback.reason) == ("partial", "missing_coverage")
    with pytest.raises(coverage.CoverageError, match="missing_coverage"):
        coverage.select_coverage(object(), "complete")
    with pytest.raises(coverage.CoverageError, match="coverage_mode"):
        coverage.select_coverage(object(), "whole")


def test_trace_accounts_for_second_halfword_and_two_exact_keys():
    start, end, trigger, rows = coverage._trace_rows(_TRACE)
    assert (start, end, trigger) == (0x800012C, 0x8000134, 0x8000100)
    assert len(rows) == 8
    at_start = [r for r in rows if r.pc == start]
    assert {r.guest_size for r in at_start} == {4, 6}
    assert len({r.key for r in at_start}) == 2
    assert [r.guest_size for r in rows if r.pc == start + 2] == [4, 2]
    texts = (
        (_TRACE.replace(_TRACE.splitlines()[2] + "\n", ""), "incomplete_context_inventory"),
        (_TRACE.replace("110 800400 ff020001", "111 800400 ff020001"), "context_mismatch"),
        (_TRACE.replace("\nend\n", "\n"), "bad_producer_trace"),
    )
    for text, reason in texts:
        with pytest.raises(coverage.CoverageError, match=reason):
            coverage._trace_rows(text)


@pytest.fixture
def captured(monkeypatch):
    start, end, trigger, rows = coverage._trace_rows(_TRACE)
    inventory = tuple(
        SimpleNamespace(uid=f"entry-{i}", vaddrs=(p.pc,), key=p.key, origin="elf_verified",
                        meta_status="ok", guest_size=p.guest_size, icount=p.icount)
        for i, p in enumerate(rows))
    root = SimpleNamespace(uid="reset", vaddrs=(trigger,), key=rows[0].key)
    snap = SimpleNamespace(units=lambda: (*inventory, root))
    identities = {name: sha256_id(name.encode())
                  for name in ("snapshot_id", "patch_plan_hash", "image_hash", "memory_map_hash")}
    monkeypatch.setattr(coverage, "_checked_metadata", lambda _: (identities, None))
    monkeypatch.setattr(coverage, "_check_fixture", lambda *_: (start + 6, ()))
    return snap, start, end, trigger, rows


@pytest.mark.parametrize("field", ["patch_plan_hash", "image_hash", "memory_map_hash", "snapshot_id"])
def test_stale_identity_refuses_complete_even_with_rehashed_document(captured, field):
    snap, *_ = captured
    doc = coverage.produce_coverage(snap, _TRACE)
    valid = coverage.select_coverage(snap, "complete", doc)
    assert len(valid.covered_units) == 8
    assert valid.roots == ("reset",)
    assert doc["entry"]["policy"] == "all-entries-general"
    assert "indirect-transfer" in doc["entry"]["residual"]

    doc["identities"] = {**doc["identities"], field: sha256_id(b"changed")}
    doc["coverage_id"] = content_id(doc, "coverage_id")
    with pytest.raises(coverage.CoverageError, match="stale_coverage"):
        coverage.select_coverage(snap, "complete", doc)
    assert coverage.select_coverage(snap, "auto", doc).reason == "stale_coverage"


def test_unexpected_context_inside_domain_invalidates_coverage(captured):
    snap, start, end, trigger, rows = captured
    inventory = snap.units()
    key = replace(rows[0].key, flags=rows[0].key.flags ^ 1)
    unaccounted = SimpleNamespace(uid="new-context", key=key, vaddrs=(start,))
    snap.units = lambda: (*inventory, unaccounted)
    with pytest.raises(coverage.CoverageError, match="unexpected_covered_key"):
        coverage._producer_inventory(snap, rows, trigger, start, end)


def test_missing_external_handler_requires_capture(captured):
    snap, start, end, trigger, rows = captured
    with pytest.raises(coverage.CoverageError, match="missing_external_root"):
        coverage._producer_inventory(snap, rows, trigger, start, end, (0x8000210,))

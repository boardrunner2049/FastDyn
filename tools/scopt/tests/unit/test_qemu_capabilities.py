"""The checked-in QEMU capability table (scopt/data/qemu_capabilities.json).

A digest the table marks incapable is removed from the matching allowlist whatever
names it (flag or environment variable): an explicit flag can never re-enable a loader
that rejects the feature.  The capable rows are admitted implicitly only by
``scopt optimize -O4``; for every other caller the allowlists are what they were, so an
existing command line keeps its bytes (sealed-pgo-7-b3 on aacca05 without
--v7-member-skip-exe still builds without member skip).
"""

from __future__ import annotations

import json
from types import MappingProxyType

import pytest

import scopt.validate as V
from scopt.validate import (
    QEMU_CAPABILITIES, known_capable_exes, known_incapable_exes, member_skip_capable_exes,
    member_slot_skip_decision, qemu_capability, qemu_capability_table,
    trusted_v7_capable_exes, v6_capable_exes,
)

AACCA05 = "951775b0300ea1421f32565af8f677c1eaee14e7e1de6238d32bdbe925eebc60"
A1BDEC6 = "026f8e8bc17a3364b15af49d597e48ac827515548308538a16d6244b75630295"
TREB_A79D340 = "ab3fcecd62055ffa54424e605375d6edc0d9576858bb23ff298c6558f7f9a63c"
QEMU_65E80CC = "a05b14138669aa5cc2124d87912a967512a621c128bb1f94930db0d78e2f64b2"
UNKNOWN = "cd" * 32


def _ri(exe):
    return {"qemu_sha256": "sha256:" + exe}


@pytest.fixture(autouse=True)
def _no_env_allowlists(monkeypatch):
    for name in ("SCOPT_V6_EXES", "SCOPT_V7_TRUSTED_EXES", "SCOPT_V7_MEMBER_SKIP_EXES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(V, "V6_GATE_BINARY_JSON", V.QEMU_CAPABILITIES_JSON.parent / "absent")


def _table(monkeypatch, rows):
    monkeypatch.setattr(V, "qemu_capability_table",
                        lambda: MappingProxyType({k: MappingProxyType(v) for k, v in rows.items()}))


def test_the_table_is_well_formed():
    doc = json.loads(V.QEMU_CAPABILITIES_JSON.read_text())
    assert doc["schema"] == "scopt.qemu-capabilities/1"
    table = qemu_capability_table()
    assert set(table) == set(doc["binaries"]) and table
    for exe, row in table.items():
        assert len(exe) == 64 and int(exe, 16) >= 0 and exe == exe.lower()
        assert row["commit"] and row["evidence"]
        assert {row[c] for c in QEMU_CAPABILITIES} <= {True, False, None}


def test_recorded_binaries():
    assert {c: qemu_capability(AACCA05, c) for c in QEMU_CAPABILITIES} == \
        {"v6": True, "trusted_v7": True, "member_skip": True}
    assert qemu_capability_table()[AACCA05]["counters_revision"] == 4
    assert qemu_capability("sha256:" + A1BDEC6, "member_skip") is False
    assert qemu_capability(A1BDEC6.upper(), "trusted_v7") is True
    assert qemu_capability(TREB_A79D340, "member_skip") is False
    assert qemu_capability(TREB_A79D340, "v6") is None
    # 65e80cc has no recorded capability: unknown, flags required.
    assert all(qemu_capability(QEMU_65E80CC, c) is None for c in QEMU_CAPABILITIES)
    assert qemu_capability(None, "v6") is None
    with pytest.raises(ValueError):
        qemu_capability(AACCA05, "v7")
    assert known_capable_exes("member_skip") == {AACCA05}
    assert known_incapable_exes("member_skip") == {A1BDEC6, TREB_A79D340}
    assert known_incapable_exes("v6") == known_incapable_exes("trusted_v7") == frozenset()


def test_capable_rows_are_not_implicit_outside_o4():
    # Without a flag the allowlists stay empty, exactly as before the table.
    assert member_skip_capable_exes() == trusted_v7_capable_exes() == frozenset()
    assert v6_capable_exes() == frozenset()
    assert member_slot_skip_decision("auto", _ri(AACCA05)) == (False, "not_allowlisted")
    assert member_slot_skip_decision("auto", _ri(AACCA05), (AACCA05,)) == (True, "allowlisted")


def test_a_flag_cannot_allowlist_a_known_incapable_member_skip_exe(monkeypatch):
    flags = ("sha256:" + A1BDEC6, TREB_A79D340, UNKNOWN)
    assert member_skip_capable_exes(flags) == {UNKNOWN}
    monkeypatch.setenv("SCOPT_V7_MEMBER_SKIP_EXES", f"{A1BDEC6.upper()}, sha256:{TREB_A79D340}")
    assert member_skip_capable_exes() == frozenset()
    for param in ("auto", "on"):
        assert member_slot_skip_decision(param, _ri(A1BDEC6), flags) == (False, "known_incapable")
        assert member_slot_skip_decision(param, _ri(TREB_A79D340), flags) == \
            (False, "known_incapable")
    assert member_slot_skip_decision("off", _ri(A1BDEC6), flags) == (False, "param_off")
    # Their other capabilities are untouched.
    assert A1BDEC6 in trusted_v7_capable_exes((A1BDEC6,))
    assert A1BDEC6 in v6_capable_exes((A1BDEC6,))


@pytest.mark.parametrize(("capability", "allowlist", "env"), [
    ("v6", v6_capable_exes, "SCOPT_V6_EXES"),
    ("trusted_v7", trusted_v7_capable_exes, "SCOPT_V7_TRUSTED_EXES"),
    ("member_skip", member_skip_capable_exes, "SCOPT_V7_MEMBER_SKIP_EXES"),
])
def test_each_capability_subtracts_its_own_incapable_rows(monkeypatch, capability, allowlist, env):
    row = {c: True for c in QEMU_CAPABILITIES}
    _table(monkeypatch, {UNKNOWN: {**row, capability: False, "evidence": "test"}})
    assert allowlist(("sha256:" + UNKNOWN, AACCA05)) == {AACCA05}
    monkeypatch.setenv(env, UNKNOWN)
    assert allowlist() == frozenset()
    others = [f for c, f in (("v6", v6_capable_exes), ("trusted_v7", trusted_v7_capable_exes),
                             ("member_skip", member_skip_capable_exes)) if c != capability]
    for other in others:
        assert other((UNKNOWN,)) == {UNKNOWN}


def test_a_malformed_table_is_refused(monkeypatch, tmp_path):
    def load(doc):
        path = tmp_path / "caps.json"
        path.write_text(json.dumps(doc))
        monkeypatch.setattr(V, "QEMU_CAPABILITIES_JSON", path)
        V.qemu_capability_table.cache_clear()
        try:
            return V.qemu_capability_table()
        finally:
            V.qemu_capability_table.cache_clear()

    ok = {"commit": "x", "v6": True, "trusted_v7": None, "member_skip": False, "evidence": "e"}
    assert set(load({"schema": "scopt.qemu-capabilities/1", "binaries": {UNKNOWN: ok}})) == {UNKNOWN}
    for doc in ({"schema": "other", "binaries": {}},
                {"schema": "scopt.qemu-capabilities/1", "binaries": {"AB" * 32: ok}},
                {"schema": "scopt.qemu-capabilities/1", "binaries": {"ab": ok}},
                {"schema": "scopt.qemu-capabilities/1", "binaries": {UNKNOWN: {**ok, "v6": "yes"}}},
                {"schema": "scopt.qemu-capabilities/1", "binaries": {UNKNOWN: {**ok, "evidence": ""}}}):
        with pytest.raises(ValueError):
            load(doc)


def test_firmware_inline_on_refuses_a_known_incapable_exe_even_when_flagged(monkeypatch):
    from .test_member_slot_skip_gate import _run_firmware_inline

    result, detail, emitted = _run_firmware_inline(
        monkeypatch, {"member_slot_skip": "on"}, runtime=_ri(A1BDEC6),
        member_skip_exes=(A1BDEC6,))
    assert (result.outcome.value, result.reason) == \
        ("failed", "member_slot_skip_unsupported_runtime")
    assert "known_incapable" in detail and "qemu_capabilities.json" in detail and not emitted
    result, receipt, emitted = _run_firmware_inline(
        monkeypatch, {}, runtime=_ri(A1BDEC6), member_skip_exes=(A1BDEC6,))
    assert result.outcome.value == "candidate" and emitted
    assert receipt["policy"]["member_slot_skip"]["reason"] == "known_incapable"
    assert result.stats["member_slot_skip"] == 0.0


def test_package_b3_refuses_a_flagged_known_incapable_exe(tmp_path):
    from scopt.link import B3Plan
    from scopt.validate import package_b3

    from ..synth.fake_snapshot import fake_snapshot

    res = package_b3(fake_snapshot(2), B3Plan(member_slot_skip=True), tmp_path / "store",
                     candidate_id="sha256:" + "42" * 32, runtime=_ri(A1BDEC6),
                     member_skip_exes=(A1BDEC6,))
    assert (res.status, res.reason) == ("rejected", "failed:member_slot_skip_unsupported_runtime")

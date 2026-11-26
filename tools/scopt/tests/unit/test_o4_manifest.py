"""The scopt O4 contract (docs/O4.md): every item Decided, O4 the Best, nothing left out.

* Decided: every registered pass, every firmware-inline mode (built-in or installed),
  every parameter of every pass O4 runs and every runtime setting QEMU reads has a
  decision with evidence in scopt/o4.py.  A new pass, mode or parameter fails here until
  it gets one.
* Inclusion: O4.toml carries exactly the included items, and contains the measured
  reference (sealed-pgo-7-b3) except where a decision supersedes it.
* Best: no configuration timed against the current revision is faster than O4
  (docs/o4-evidence.json, generated from pinned campaign outputs).
"""

from __future__ import annotations

import json
import os
import re
from fnmatch import fnmatch
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from scopt import o4
from scopt.manager import DEFAULT_DISABLED_PASSES, PIPELINES_DIR, default_pass_registry, load_pipeline
from scopt.passes import firmware_policy
from scopt.paths import fastdyn_root

DOCS = Path(__file__).parents[2] / "docs"


def _schema(reg, name):
    return reg.get(name, 1).descriptor.params_schema


def _o4():
    return {e.name: e for e in load_pipeline(PIPELINES_DIR / "O4.toml").passes}


def test_every_registered_pass_has_a_decision():
    assert set(default_pass_registry().names()) == set(o4.PASSES)


def test_every_firmware_mode_has_a_decision():                 # built-in and installed
    installed = {ep.name for ep in entry_points(group="scopt.firmware_inline_policies")}
    assert set(firmware_policy._BUILTINS) | installed == set(o4.FIRMWARE_MODES)


def test_every_param_of_an_included_pass_is_decided():          # a new speed knob fails CI
    reg = default_pass_registry()
    for name, d in o4.PASSES.items():
        for param in _schema(reg, name):
            key = (name, param)
            if d.status == o4.INCLUDED:
                assert (key in o4.KNOBS) != (key in o4.NON_SPEED), key
            else:                                               # decided at pass level
                assert key not in o4.KNOBS and key not in o4.NON_SPEED, key
    known = {(n, p) for n in o4.PASSES for p in _schema(reg, n)}
    assert set(o4.KNOBS) | set(o4.NON_SPEED) <= known
    assert all(reason for reason in o4.NON_SPEED.values())


def test_decisions_are_well_formed():
    for d in o4.all_decisions():
        assert d.status in (o4.INCLUDED, o4.EXCLUDED)
        assert d.evidence and d.marginal in o4.MARGINALS
        assert d.reason in (o4.INCLUDE_REASONS if d.status == o4.INCLUDED else o4.EXCLUDE_REASONS)
        assert d.reason not in ("unmeasured", "bundle") or d.arm        # bundles name their ablation arm
        assert d.reason != "measured_best" or d.marginal == "PROVEN_GAIN" or d.arm
        assert re.match(r"O4\.md#[a-z0-9-]+", d.evidence), d.evidence
    assert all(d.enabled == (n in DEFAULT_DISABLED_PASSES and d.status == o4.INCLUDED)
               for n, d in o4.PASSES.items())


def test_evidence_anchors_exist_in_the_note():
    text = (DOCS / "O4.md").read_text(encoding="utf-8")
    anchors = {re.sub(r"[^a-z0-9 -]", "", h.strip().lower()).replace(" ", "-")
               for h in re.findall(r"^#+ (.+)$", text, flags=re.M)}
    for d in o4.all_decisions():
        anchor = re.match(r"O4\.md#([a-z0-9-]+)", d.evidence).group(1)
        assert anchor in anchors, (anchor, sorted(anchors))


def test_o4_toml_is_the_current_revision():
    assert o4.REVISIONS[-1] == o4.REVISION
    alias = load_pipeline(PIPELINES_DIR / "O4.toml")
    rev = load_pipeline(PIPELINES_DIR / f"{o4.REVISION}.toml")
    assert alias.passes == rev.passes and alias.allowed_capabilities == rev.allowed_capabilities
    for name in o4.REVISIONS:                                   # every frozen revision stays loadable
        assert load_pipeline(PIPELINES_DIR / f"{name}.toml").name == name


def test_o4_carries_every_included_item_and_no_excluded_one():
    by = _o4()
    for name, d in o4.PASSES.items():
        if d.status == o4.INCLUDED:
            assert name in by and dict(d.params).items() <= dict(by[name].params).items(), name
            assert by[name].enabled == (name in DEFAULT_DISABLED_PASSES) == d.enabled, name
        else:
            assert name not in by, name
    fw = by["firmware-inline"].params.get("mode", "exhaustive")
    assert o4.FIRMWARE_MODES[fw].status == o4.INCLUDED
    assert [m for m, d in o4.FIRMWARE_MODES.items() if d.status == o4.INCLUDED] == [fw]
    for (p, k), d in o4.KNOBS.items():
        params = by[p].params if p in by else {}
        if d.status == o4.EXCLUDED:
            assert params.get(k) != d.params[k], (p, k)
        elif d.reason == "default":
            assert k not in params, (p, k)                      # "default" means the param is absent
        else:
            assert k in d.params and params.get(k) == d.params[k], (p, k)
    for name, entry in by.items():                              # nothing O4 sets is undecided
        for k in entry.params:
            assert (name, k) in o4.KNOBS and o4.KNOBS[(name, k)].reason != "default", (name, k)


def test_o4_contains_the_reference():
    o4p = _o4()
    for e in load_pipeline(PIPELINES_DIR / f"{o4.REFERENCE}.toml").passes:
        if e.name in o4.REMOVED:
            continue
        assert e.name in o4p
        for k, v in e.params.items():
            got = o4p[e.name].params.get(k)
            assert got == v or o4.KNOBS[(e.name, k)].status == o4.INCLUDED and o4.KNOBS[(e.name, k)].reason \
                in ("measured_best", "required"), (e.name, k)


def test_runtime_decisions_are_documented():
    text = (DOCS / "O4.md").read_text(encoding="utf-8")
    assert all(key in text for key in o4.RUNTIME), [k for k in o4.RUNTIME if k not in text]


def test_runtime_matches_qemu_source():                         # every QEMU_SC_* variable is decided
    root = Path(os.environ.get("SCOPT_QEMU_SRC") or fastdyn_root() / "third_party/common/qemu")
    dirs = [root / "tcg", root / "accel" / "tcg"]
    if not dirs[0].is_dir():
        pytest.skip("QEMU source not checked out (set SCOPT_QEMU_SRC to a checkout)")
    text = "".join(p.read_text(errors="replace") for d in dirs if d.is_dir()
                   for p in sorted(d.rglob("*.[ch]")))
    names = set(re.findall(r'getenv\("(QEMU_SC_[A-Z0-9_]+)"\)', text))
    assert names
    assert all(any(fnmatch(n, key) for key in o4.RUNTIME) for n in names), \
        sorted(n for n in names if not any(fnmatch(n, key) for key in o4.RUNTIME))


def test_no_recorded_arm_beats_o4():                            # "O4 remains the best"
    doc = json.loads((DOCS / "o4-evidence.json").read_text())
    assert doc["schema"] == "scopt.o4-evidence/2" and doc["generator"] == "scripts/o4_evidence.py"
    pinned = {s["sha256"] for s in doc["sources"]}
    for r in doc["rows"]:
        assert set(r["summary_sha256"].split(",")) <= pinned and r["decide_sha256"] in pinned, r
        assert r["revision"] in o4.REVISIONS and r["reference"] == "O4"
        assert r["verdict"] in ("faster_than_o4", "slower_than_o4", "not_shown", "same_bytes")
    current = [r for r in doc["rows"] if r["revision"] == o4.REVISION]
    assert not [r for r in current if r["verdict"] == "faster_than_o4"]
    for r in doc["rows"]:                                       # an arm that beat O4-rN is re-timed against O4-rN+1
        if r["verdict"] == "faster_than_o4" and r["revision"] != o4.REVISION:
            assert any(c["arm"] == r["arm"] and c["workload"] == r["workload"] for c in current), r
    for w in o4.WORKLOADS:
        assert any(r["workload"] == w and r["arm"] == "c7b3" for r in current), w

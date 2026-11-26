"""``scopt optimize -O4``: the packaged O4 pipeline, its preflight and its stage gate.

The preflight (O4-design.md 2.7) runs for every pipeline named ``O4`` or ``O4-rN`` and
refuses, in one usage error, everything O4 was not measured on: customization, missing
inputs, an inexact or unreached profile, icount-on or ordinary captures, and a QEMU the
capability table marks incapable or does not know (without flags).  The stage gate
rejects a build in which any O4 pass did not run to ``edited`` or ``no_change`` before
anything is published.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import scopt.analysis.helper_hot_loop as HHL
import scopt.analysis.store as AS
from scopt.commands import optimize as command
from scopt.manager import PIPELINES_DIR, PassRecord, load_pipeline
from scopt.passes.base import Outcome
from scopt.run.pass_policy import (
    EDITED, INCOMPLETE, MISSING, NO_CHANGE, POLICY_DISABLED, gate_status,
    required_stage_problems,
)

AACCA05 = "951775b0300ea1421f32565af8f677c1eaee14e7e1de6238d32bdbe925eebc60"
A1BDEC6 = "026f8e8bc17a3364b15af49d597e48ac827515548308538a16d6244b75630295"
UNKNOWN = "cd" * 32
O4_STAGES = [e.name for e in load_pipeline(PIPELINES_DIR / "O4.toml").passes]


def _receipt(name, outcome="candidate", reason="", n_edits=1):
    return {"pass": name, "outcome": outcome, "reason": reason, "n_edits": n_edits}


class _Env:
    """optimize_cmd with the snapshot, profiles, training and native prefix faked."""

    def __init__(self, tmp_path, monkeypatch, *, real_b3=False):
        for name in ("SCOPT_V6_EXES", "SCOPT_V7_TRUSTED_EXES", "SCOPT_V7_MEMBER_SKIP_EXES",
                     "SCOPT_PIPELINES_DIR", "SCOPT_B3_CALIBRATION"):
            monkeypatch.delenv(name, raising=False)
        # No workspace B3 calibration (runs/b3-explore/wp16/calibration.json).
        monkeypatch.setenv("FASTDYN_ROOT", str(tmp_path))
        self.tmp = tmp_path
        self.flags = 0
        self.cflags = 0
        self.exact = True
        self.match = 1.0
        self.profile_problems = []
        self.calls = []
        self.unit = SimpleNamespace(key=SimpleNamespace(cflags=0), inline_start=None)
        self.snap = SimpleNamespace(snapshot_id="sha256:" + "5" * 64, path=str(tmp_path),
                                    run_info=SimpleNamespace(flags=0), runtime=None,
                                    units=lambda: [self.unit], unit_ids=lambda: ("u0",))
        monkeypatch.setattr(command, "_open_snapshot", lambda *a, **k: self.snap)
        monkeypatch.setattr(command, "_o4_profiles",
                            lambda paths, snap: (["view"] * len(paths), list(self.profile_problems)))
        monkeypatch.setattr(command, "_profiles", lambda paths, snap: (["view"] * len(paths), []))
        monkeypatch.setattr(HHL, "exact_loop_profile", lambda view, sid, phase: self.exact)
        monkeypatch.setattr(AS, "profile_phase_info",
                            lambda view, phase, units=None: (True, True, self.match))
        monkeypatch.setattr(command, "_automatic_loop_pgo", lambda *a, **k: None)
        monkeypatch.setattr(command, "_helper_prefix_profiles", lambda views, src, snap: views)
        native = {"passes": [_receipt("host-inline", "no_change", "no_eligible_native_calls", 0)]}
        monkeypatch.setattr(command, "_splice_generation",
                            lambda snap, stage, store, views, params: (snap, native))

        def optimize_b3(snap, pl, store, views, notes, params, disable, only, ri_file,
                        v6_exes, v7_exes, trusted_v7_exes, *args, member_skip_exes=(), o4=None,
                        **kwargs):
            self.calls.append(SimpleNamespace(pl=pl, v6=v6_exes, trusted=trusted_v7_exes,
                                              skip=member_skip_exes, o4=o4, enable=kwargs.get("enable"),
                                              disable=disable))

        if not real_b3:
            monkeypatch.setattr(command, "_optimize_b3", optimize_b3)
        for name in ("prep", "store"):
            (tmp_path / name).mkdir()
        for name in ("fw.elf", "profile.json", "launch.json"):
            (tmp_path / name).write_text("{}")
        self.runtime(AACCA05)

    def runtime(self, exe):
        (self.tmp / "runtime.json").write_text(json.dumps({"qemu_sha256": "sha256:" + exe}))

    def args(self, *extra, profile=True, ri=True, prep=True, elf=True, launch=True):
        t = self.tmp
        out = [str(t), "--store", str(t / "store"), "--json"]
        out += ["--profile", str(t / "profile.json")] if profile else []
        out += ["--runtime-identity", str(t / "runtime.json")] if ri else []
        out += ["--prep", str(t / "prep")] if prep else []
        out += ["--elf", str(t / "fw.elf")] if elf else []
        out += ["--pgo-launch", str(t / "launch.json")] if launch else []
        return out + list(extra)

    def invoke(self, *extra, **kw):
        self.snap.run_info.flags = self.flags
        self.unit.key.cflags = self.cflags
        return CliRunner().invoke(command.optimize_cmd, self.args(*extra, **kw))


@pytest.fixture
def o4(tmp_path, monkeypatch):
    return _Env(tmp_path, monkeypatch)


def _refused(result, *needles):
    assert result.exit_code == 2, result.output
    assert "scopt O4 preflight failed" in result.output and "docs/O4.md" in result.output
    for needle in needles:
        assert needle in result.output, (needle, result.output)


def test_o4_resolves_to_the_packaged_pipeline_and_admits_the_table_exe(o4, monkeypatch, tmp_path):
    other = tmp_path / "pipelines"
    other.mkdir()
    (other / "O4.toml").write_text('name = "O4"\n')
    monkeypatch.setenv("SCOPT_PIPELINES_DIR", str(other))
    result = o4.invoke("-O4")
    assert result.exit_code == 0, result.output
    call, = o4.calls
    assert call.pl.source == str(PIPELINES_DIR / "O4.toml")
    # aacca05 is capable in scopt/data/qemu_capabilities.json: no allowlist flag needed.
    want = ("sha256:" + AACCA05,)
    assert (call.v6, call.trusted, call.skip) == (want, want, want)
    assert call.o4["runtime"]["capabilities"] == dict.fromkeys(
        ("v6", "trusted_v7", "member_skip"), "capability_table")
    assert call.o4["required_stages"] == O4_STAGES and call.o4["profile_match"] == 1.0
    assert call.o4["pgo_launch"] == "--pgo-launch"
    assert call.enable == () and call.disable == ()


def test_o4_is_exclusive_with_pipeline(o4):
    result = o4.invoke("-O4", "--pipeline", "O4")
    assert result.exit_code == 2 and "exactly one of --pipeline and -O4" in result.output
    result = o4.invoke()
    assert result.exit_code == 2 and "exactly one of --pipeline and -O4" in result.output
    result = o4.invoke("-O3")
    assert result.exit_code == 2 and "Invalid value for '-O'" in result.output
    assert not o4.calls


@pytest.mark.parametrize("name", ["O4", "O4-r1", str(PIPELINES_DIR / "O4-r1.toml")])
def test_any_o4_named_pipeline_runs_the_preflight(o4, name):
    result = o4.invoke("--pipeline", name, elf=False)
    _refused(result, "--elf is required")
    result = o4.invoke("--pipeline", name)
    assert result.exit_code == 0, result.output
    assert o4.calls[-1].o4["pipeline"] == load_pipeline(name).name


def _renamed(tmp_path, name):
    path = tmp_path / f"{name}.toml"
    path.write_text((PIPELINES_DIR / "O4-r1.toml").read_text()
                    .replace('name = "O4-r1"', f'name = "{name}"'))
    return str(path)


@pytest.mark.parametrize("name", ["O4-r01", "O4-r0", "O4-r2"])
def test_an_unpackaged_o4_revision_name_runs_the_preflight(o4, tmp_path, name):
    # design 2.7: every name matching ^O4(-r\d+)?$ runs the preflight.
    _refused(o4.invoke("--pipeline", _renamed(tmp_path, name)), "is not the packaged")
    assert not o4.calls


@pytest.mark.parametrize("name", ["O4x", "O4-r1x", "O4-r", "xO4", "O4-R1"])
def test_a_name_that_only_contains_o4_is_not_o4(o4, tmp_path, name):
    result = o4.invoke("--pipeline", _renamed(tmp_path, name), "--disable",
                       "constant-propagation", elf=False)
    assert result.exit_code == 0, result.output
    assert o4.calls[-1].o4 is None


def test_an_arm_pipeline_is_not_o4(o4, tmp_path):
    arm = tmp_path / "o4-chained.toml"
    text = (PIPELINES_DIR / "O4-r1.toml").read_text().replace('name = "O4-r1"', 'name = "o4-chained"')
    arm.write_text(text)
    result = o4.invoke("--pipeline", str(arm), "--disable", "constant-propagation", elf=False)
    assert result.exit_code == 0, result.output
    assert o4.calls[-1].o4 is None


def test_a_modified_file_named_o4_is_refused(o4, tmp_path):
    fake = tmp_path / "O4.toml"
    fake.write_text((PIPELINES_DIR / "O4.toml").read_text().replace("min_call_count = 1000",
                                                                     "min_call_count = 2"))
    _refused(o4.invoke("--pipeline", str(fake)), "is not the packaged", "O4 is fixed")
    same = tmp_path / "copy" / "O4.toml"
    same.parent.mkdir()
    same.write_text((PIPELINES_DIR / "O4.toml").read_text())
    assert o4.invoke("--pipeline", str(same)).exit_code == 0


@pytest.mark.parametrize("extra, flag", [
    (("--params", "params.json"), "--params"),
    (("--disable", "constant-propagation"), "--disable"),
    (("--enable", "chain-static"), "--enable"),
    (("--only", "firmware-inline"), "--only"),
    (("--pgo-phase", "boot"), "--pgo-phase boot"),
])
def test_o4_cannot_be_customized(o4, tmp_path, extra, flag):
    (tmp_path / "params.json").write_text('{"firmware-inline": {"min_call_count": 2}}')
    extra = tuple(str(tmp_path / x) if x == "params.json" else x for x in extra)
    _refused(o4.invoke("-O4", *extra), f"{flag}: O4 is fixed")
    assert not o4.calls


def test_every_missing_input_is_named_at_once(o4):
    result = o4.invoke("-O4", profile=False, ri=False, prep=False, elf=False, launch=False)
    _refused(result, "--runtime-identity is required", "--prep is required",
             "--elf is required", "--profile is required", "needs a training launch")


def test_an_inexact_or_unreached_profile_is_refused(o4):
    o4.exact = False
    _refused(o4.invoke("-O4"), "no --profile is an exact profile", "'steady' reached")
    o4.exact, o4.match = True, 0.9
    _refused(o4.invoke("-O4"), "profile match of phase 'steady' is 0.9 < 0.95")
    o4.match = None
    _refused(o4.invoke("-O4"), "is None < 0.95")
    o4.match, o4.profile_problems = 1.0, ["--profile x: incompatible with the snapshot (y)"]
    _refused(o4.invoke("-O4"), "incompatible with the snapshot")
    assert not o4.calls


def test_icount_on_and_ordinary_snapshots_are_refused(o4):
    from scopt.decode.search import is_pcrel
    from scopt.formats.enums import RunInfoFlag

    o4.flags = int(RunInfoFlag.ICOUNT_ENABLED)
    _refused(o4.invoke("-O4"), "captured with icount on", "SFB+B3 recipe")
    o4.flags = 0
    o4.cflags = next(bit for bit in (1 << n for n in range(32)) if is_pcrel(bit))
    _refused(o4.invoke("-O4"), "not a sealed whole-firmware capture", "SFB+B3 recipe")
    o4.unit.key.cflags = 0
    o4.snap.run_info = None
    result = CliRunner().invoke(command.optimize_cmd, o4.args("-O4"))
    _refused(result, "has no run info")
    assert not o4.calls


def test_a_known_incapable_qemu_is_refused_even_when_flagged(o4):
    o4.runtime(A1BDEC6)
    flags = [x for f in ("--v6-exe", "--v7-trusted-exe", "--v7-member-skip-exe")
             for x in (f, "sha256:" + A1BDEC6)]
    _refused(o4.invoke("-O4", *flags), f"QEMU {A1BDEC6} is marked incapable of member_skip",
             "no flag overrides that")
    assert not o4.calls


def test_an_unknown_qemu_needs_every_flag(o4, monkeypatch):
    o4.runtime(UNKNOWN)
    result = o4.invoke("-O4")
    _refused(result, "--v6-exe sha256:" + UNKNOWN, "SCOPT_V6_EXES",
             "--v7-trusted-exe sha256:" + UNKNOWN, "--v7-member-skip-exe sha256:" + UNKNOWN,
             "SCOPT_V7_MEMBER_SKIP_EXES", "counters revision 4")
    result = o4.invoke("-O4", "--v6-exe", UNKNOWN, "--v7-trusted-exe", UNKNOWN)
    _refused(result, "--v7-member-skip-exe sha256:" + UNKNOWN)
    assert "--v6-exe sha256:" not in result.output
    monkeypatch.setenv("SCOPT_V7_MEMBER_SKIP_EXES", UNKNOWN)
    result = o4.invoke("-O4", "--v6-exe", UNKNOWN, "--v7-trusted-exe", UNKNOWN)
    assert result.exit_code == 0, result.output
    call = o4.calls[-1]
    assert call.o4["runtime"]["capabilities"] == dict.fromkeys(
        ("v6", "trusted_v7", "member_skip"), "allowlisted")
    assert call.skip == ()                     # the environment allowlists it downstream


def _calibration(path, **costs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"constants": {k: {"value_ns": v} for k, v in costs.items()}}))
    return path


def test_an_active_b3_calibration_is_refused(o4, tmp_path, monkeypatch):
    # The environment variable ...
    monkeypatch.setenv("SCOPT_B3_CALIBRATION",
                       str(_calibration(tmp_path / "cal.json", c_lookup=30.0, c_guard=0.5)))
    _refused(o4.invoke("-O4"), "B3 cost calibration {'c_lookup': 30.0, 'c_guard': 0.5} is active",
             "measured with the built-in costs")
    # ... or the workspace file.
    monkeypatch.delenv("SCOPT_B3_CALIBRATION")
    _calibration(tmp_path / "runs/b3-explore/wp16/calibration.json", c_guard=2.0)
    _refused(o4.invoke("-O4"), "B3 cost calibration {'c_guard': 2.0} is active")
    assert not o4.calls
    # A calibration equal to the built-in costs changes nothing and is not refused.
    _calibration(tmp_path / "runs/b3-explore/wp16/calibration.json", c_lookup=7.6, c_guard=1.5)
    assert o4.invoke("-O4").exit_code == 0


def test_the_prep_launch_satisfies_the_training_requirement(o4, tmp_path, monkeypatch):
    _refused(o4.invoke("-O4", launch=False), "needs a training launch")
    (tmp_path / "prep" / "run.toml").write_text("")
    monkeypatch.setattr("shutil.which", lambda name: None)
    _refused(o4.invoke("-O4", launch=False), "needs a training launch")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/fastdyn")
    assert o4.invoke("-O4", launch=False).exit_code == 0
    assert o4.calls[-1].o4["pgo_launch"] == "prep run.toml"
    (tmp_path / "prep" / "launch.json").write_text("{}")
    assert o4.invoke("-O4", launch=False).exit_code == 0
    assert o4.calls[-1].o4["pgo_launch"] == "prep launch.json"


# ---------------------------------------------------------------------------
# the stage gate
# ---------------------------------------------------------------------------

#: Every stage of round 2's c7b3 receipts (/data/scfix-r2/{packet-w/opt,uart-w/arms,
#: cli-w/opt}/c7b3/optimize.json): pass, outcome, reason, n_edits.
_C7B3_ROUND2 = {
    "packet": [("host-inline", "candidate", "native_calls_spliced", 1),
               ("specialize-returns", "skipped_unsupported", "target_not_profiled", 0),
               ("constant-propagation", "no_change", "no_variants", 0),
               ("branch-folding", "no_change", "no_variants", 0),
               ("dead-host-ops", "no_change", "no_variants", 0),
               ("chain-returns", "candidate", "", 47), ("chain-static", "candidate", "", 256),
               ("firmware-inline", "candidate", "firmware_regions_emitted", 48)],
    "uart": [("host-inline", "no_change", "no_eligible_native_calls", 0),
             ("specialize-returns", "candidate", "", 50),
             ("constant-propagation", "candidate", "", 6), ("branch-folding", "candidate", "", 7),
             ("dead-host-ops", "candidate", "", 7), ("chain-returns", "candidate", "", 43),
             ("chain-static", "candidate", "", 50),
             ("firmware-inline", "candidate", "firmware_regions_emitted", 19)],
    "cli": [("host-inline", "no_change", "no_eligible_native_calls", 0),
            ("specialize-returns", "candidate", "", 6),
            ("constant-propagation", "candidate", "", 1), ("branch-folding", "candidate", "", 1),
            ("dead-host-ops", "candidate", "", 1), ("chain-returns", "candidate", "", 28),
            ("chain-static", "candidate", "", 43),
            ("firmware-inline", "candidate", "firmware_regions_emitted", 7)],
}


@pytest.mark.parametrize("workload", sorted(_C7B3_ROUND2))
def test_round2_c7b3_passes_the_gate(workload):
    rows = [_receipt(*row) for row in _C7B3_ROUND2[workload]]
    assert [r["pass"] for r in rows] == O4_STAGES
    assert {gate_status(r) for r in rows} <= {EDITED, NO_CHANGE}
    assert required_stage_problems(rows, O4_STAGES) == []


@pytest.mark.parametrize("outcome, reason, status", [
    ("skipped_unknown", "no_profitable_call", NO_CHANGE),
    ("skipped_unknown", "no_eligible_call", NO_CHANGE),
    ("skipped_unknown", "no_firmware_call", NO_CHANGE),
    ("skipped_unknown", "hot_loop_exact_profile_required", INCOMPLETE),
    ("skipped_unknown", "cost_model_exact_profile_required", INCOMPLETE),
    ("skipped_unknown", "profile_phase_not_reached", INCOMPLETE),
    ("skipped_unknown", "missing_profile", MISSING),
    ("skipped_unsupported", "ablation_disabled", POLICY_DISABLED),
])
def test_firmware_inline_nothing_to_emit_is_no_change(outcome, reason, status):
    row = _receipt("firmware-inline", outcome, reason, 0)
    assert gate_status(row) == status
    assert (required_stage_problems([row], ["firmware-inline"]) == []) == (status == NO_CHANGE)
    # Only firmware-inline gets the exception.
    assert gate_status({**row, "pass": "chain-static"}) != NO_CHANGE or status != NO_CHANGE


@pytest.mark.parametrize("stats, status", [
    ({"refused:not_profitable": 3.0}, NO_CHANGE),
    ({"refused:not_profitable": 3.0, "refused:profit_estimate_unknown": 0.0}, NO_CHANGE),
    ({"refused:profit_estimate_unknown": 2.0}, INCOMPLETE),
    ({"refused:not_profitable": 3.0, "refused:profit_estimate_unknown": 1.0}, INCOMPLETE),
])
def test_an_unpriced_root_is_not_nothing_to_emit(stats, status):
    # firmware-inline says no_profitable_call also when roots could not be priced
    # (executions_unknown, flow_not_converged): that is not "nothing worth a region".
    row = {**_receipt("firmware-inline", "skipped_unknown", "no_profitable_call", 0),
           "stats": stats}
    assert gate_status(row) == status
    assert (required_stage_problems([row], ["firmware-inline"]) == []) == (status == NO_CHANGE)


def test_degraded_stages_are_named():
    rows = [_receipt(*r) for r in _C7B3_ROUND2["uart"]]
    rows[6] = _receipt("chain-static", "skipped_unknown", "incomplete_lookup_profile", 0)
    rows[1] = _receipt("specialize-returns", "skipped_unsupported", "disabled_by_default", 0)
    assert required_stage_problems(rows[:-1], O4_STAGES) == [
        "specialize-returns: policy_disabled (disabled_by_default)",
        "chain-static: incomplete (incomplete_lookup_profile)",
        "firmware-inline: not_run"]


def _record(index, name, outcome, reason, n_edits):
    return PassRecord(index=index, name=name, version=1, params={}, params_sha256="sha256:0",
                      outcome=Outcome(outcome), reason=reason, n_edits=n_edits)


def _stub_manager(monkeypatch, records, packaged):
    import scopt.manager as M
    import scopt.manager_b3 as MB
    import scopt.validate as V

    cand = SimpleNamespace(passes=records, b3_state=SimpleNamespace(empty=False), ledger=[],
                           candidate_id="sha256:" + "7" * 64)

    class PM:
        def __init__(self, *args, **kwargs):
            self.b3ctx = SimpleNamespace()

        def execute(self):
            return cand

    monkeypatch.setattr(M, "PassManager", PM)
    monkeypatch.setattr(MB, "materialize", lambda *a: pytest.fail("a degraded O4 was linked"))
    monkeypatch.setattr(V, "package_b3", lambda *a, **k: packaged.append(1))


def test_optimize_b3_rejects_a_degraded_required_stage(tmp_path, monkeypatch):
    from scopt.manager_b3 import STAGE_DEGRADED, B3Env, optimize_b3

    rows = _C7B3_ROUND2["cli"][1:]
    records = [_record(i, *r) for i, r in enumerate(rows)]
    records[5] = _record(5, "chain-static", "skipped_unknown", "incomplete_lookup_profile", 0)
    packaged = []
    _stub_manager(monkeypatch, records, packaged)
    store = tmp_path / "store"
    r = optimize_b3(None, "pipeline", store, env=B3Env(), required_stages=O4_STAGES,
                    prior_stages=[_receipt(*_C7B3_ROUND2["cli"][0])])
    assert r["outcome"] == "failed" and r["package"].reason == STAGE_DEGRADED
    doc = json.loads(open(r["package"].rejection_path).read())
    assert doc["detail"] == "chain-static: incomplete (incomplete_lookup_profile)"
    assert not packaged and not (store / "artifacts").exists()
    # The host-inline native prefix is gated too.
    records[5] = _record(5, *rows[5])
    r = optimize_b3(None, "pipeline", store, env=B3Env(), required_stages=O4_STAGES,
                    prior_stages=[_receipt("host-inline", "skipped_unknown", "missing_profile", 0)])
    assert r["package"].reason == STAGE_DEGRADED and not packaged
    assert "host-inline: missing (missing_profile)" in \
        json.loads(open(r["package"].rejection_path).read())["detail"]


def test_o4_cli_exits_2_and_publishes_nothing_when_a_stage_degrades(monkeypatch, tmp_path):
    import scopt.analysis.b3_inputs as BI

    o4 = _Env(tmp_path, monkeypatch, real_b3=True)
    monkeypatch.setattr(BI, "resolve_for_snapshot", lambda *a, **k: None)
    rows = _C7B3_ROUND2["packet"][1:]
    records = [_record(i, *r) for i, r in enumerate(rows)]
    records[5] = _record(5, "chain-static", "skipped_unknown", "incomplete_lookup_profile", 0)
    packaged = []
    _stub_manager(monkeypatch, records, packaged)
    result = o4.invoke("-O4")
    assert result.exit_code == 2, result.output
    doc = json.loads(result.output)
    assert (doc["status"], doc["reason"], doc["outcome"]) == ("rejected", "o4_stage_degraded",
                                                              "failed")
    assert doc["artifact_dir"] is None and not packaged
    assert not (o4.tmp / "store" / "artifacts").exists()
    rows = {r["pass"]: r["status"] for r in doc["effective_configuration"]}
    assert rows["chain-static"] == INCOMPLETE and rows["host-inline"] == NO_CHANGE
    assert doc["o4"]["required_stages"] == O4_STAGES


def test_o4_cli_gates_the_native_prefix_and_publishes_an_all_good_build(monkeypatch, tmp_path):
    """host-inline runs as the native prefix, outside the B3 records: the gate must see
    its receipt, or every real -O4 build is rejected as "host-inline: not_run"."""
    import scopt.analysis.b3_inputs as BI
    import scopt.manager_b3 as MB

    o4 = _Env(tmp_path, monkeypatch, real_b3=True)
    monkeypatch.setattr(BI, "resolve_for_snapshot", lambda *a, **k: None)
    records = [_record(i, *r) for i, r in enumerate(_C7B3_ROUND2["uart"][1:])]
    seen = []

    def optimize_b3(snap, pl, store, *, required_stages=None, prior_stages=(), **kw):
        seen.append({"required_stages": required_stages, "prior_stages": list(prior_stages)})
        assert required_stage_problems([*prior_stages, *(p.to_json() for p in records)],
                                       required_stages) == []
        pk = SimpleNamespace(status="published", reason=None, artifact_id="sha256:" + "8" * 64,
                             artifact_dir=str(tmp_path / "store" / "artifacts" / "a"),
                             publish_status="new", rejection_path=None, validation=None)
        return {"candidate": SimpleNamespace(passes=records), "package": pk,
                "materialized": None, "path": "b3", "outcome": "published"}

    monkeypatch.setattr(MB, "optimize_b3", optimize_b3)
    result = o4.invoke("-O4")
    assert result.exit_code == 0, result.output
    kw, = seen
    assert [r["pass"] for r in kw["prior_stages"]] == ["host-inline"]
    assert kw["required_stages"] == O4_STAGES
    doc = json.loads(result.output)
    assert [r["pass"] for r in doc["effective_configuration"]] == O4_STAGES
    assert {r["status"] for r in doc["effective_configuration"]} <= {EDITED, NO_CHANGE}

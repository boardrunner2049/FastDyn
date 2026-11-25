"""``scopt profile run --sealed [--trusted]``: a steady profile of a sealed
exact-PC capture (configuration 6), so the B3 passes can run on it.

An ordinary (scrubbed) replay of a sealed snapshot keeps ``CF_PCREL`` and
misses every exact-PC record, so the joined profile is PROFILE_INCOMPATIBLE.
These tests cover the switch environment, the key-mode refusals and the CLI
plumbing without launching QEMU."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from scopt.run import launcher as LA
from scopt.run import profile_run as PR
from scopt.run.profile_run import ProfileRunError, profile_env, profile_run, target_key_mode
from tests.synth import builders as B


def _sc(path: Path, *, exact_pc: bool, n: int = 3, mixed: bool = False) -> Path:
    key = B.DEFAULT_KEY
    if exact_pc:
        key = replace(key, cflags=key.cflags & ~B.CF_PCREL)
    units = B.simple_units(n, key=key)
    if mixed:
        other = B.simple_units(1, start=units[-1].tb_offset + 4096,
                               key=replace(key, cflags=key.cflags ^ B.CF_PCREL,
                                           page_addr0=key.page_addr0 + 0x400))
        units = [*units, *other]
    B.build_sc(units, path=str(path))
    return path


def _docs(tmp: Path) -> tuple[Path, Path, Path]:
    base = tmp / "base.toml"
    base.write_text('[Machine]\nsupercache = "c/x.sc"\nsupercache_flat = false\n')
    (tmp / "b.json").write_text("{}")
    (tmp / "r.json").write_text("{}")
    return base, tmp / "b.json", tmp / "r.json"


def test_profile_env_sets_sealed_switches_after_the_scrub():
    env = profile_env("/r/run.qprof", "steady@0x1234", sealed=True, trusted=True)
    assert env == {"QEMU_SC_PROFILE": "/r/run.qprof", "QEMU_SC_PROFILE_PHASES": "steady@0x1234",
                   "QEMU_SC_SEALED": "1", "QEMU_SC_TRUSTED": "1"}
    assert profile_env("/q") == {"QEMU_SC_PROFILE": "/q"}
    assert "QEMU_SC_TRUSTED" not in profile_env("/q", sealed=True)
    # extra_env is applied after the scrub: stale QEMU_SC_* never leak, ours survive
    base = {"QEMU_SC_SEALED": "0", "QEMU_SC_V7_EXPERIMENT": "no-sealed-sc6",
            "QEMU_SC_SEALED_SKIP_IRQ": "1", "PATH": "/bin"}
    out, diff = LA.scrub_env(base, extra=profile_env("/q", sealed=True))
    assert out["QEMU_SC_SEALED"] == "1" and out["QEMU_SC_PROFILE"] == "/q"
    assert "QEMU_SC_V7_EXPERIMENT" not in out and "QEMU_SC_SEALED_SKIP_IRQ" not in out
    assert out["PATH"] == "/bin" and "QEMU_SC_SEALED_SKIP_IRQ" in diff["unset"]


def test_target_key_mode(tmp_path):
    assert target_key_mode(_sc(tmp_path / "p.sc", exact_pc=False)) == "pcrel"
    assert target_key_mode(_sc(tmp_path / "e.sc", exact_pc=True)) == "exact_pc"
    assert target_key_mode(_sc(tmp_path / "m.sc", exact_pc=True, mixed=True)) == "mixed"
    d = tmp_path / "art"
    d.mkdir()
    _sc(d / "cache.sc", exact_pc=True)
    assert target_key_mode(d) == "exact_pc"        # artifact/snapshot dir -> cache.sc
    (tmp_path / "empty.sc").write_bytes(b"")
    assert target_key_mode(tmp_path / "empty.sc") is None
    assert target_key_mode(tmp_path / "missing-dir") is None


@pytest.mark.parametrize("exact_pc, sealed, code", [
    (False, True, "sealed_needs_exact_pc"),
    (True, False, "exact_pc_needs_sealed"),
])
def test_key_mode_refusals_before_any_run(tmp_path, exact_pc, sealed, code):
    base, b, r = _docs(tmp_path)
    sc = _sc(tmp_path / "x.sc", exact_pc=exact_pc)
    with pytest.raises(ProfileRunError) as e:
        profile_run(base, sc, tmp_path / "o", bundle=b, runtime_identity=r, sealed=sealed)
    assert e.value.code == code
    assert not (tmp_path / "o").exists()


def test_sealed_run_launches_with_sealed_env_and_records_it(tmp_path, monkeypatch):
    from scopt.run import deploy as D

    base, b, r = _docs(tmp_path)
    sc = _sc(tmp_path / "x.sc", exact_pc=True)
    seen = []

    class _Rd:
        def __init__(self, root):
            self.root = Path(root)
            self.root.mkdir(parents=True)

    def fake_deploy_new(target, base_toml, rd_path, **kw):
        return _Rd(rd_path), {}

    def fake_launch(rd, *, timeout_s, extra_env, **kw):
        seen.append(dict(extra_env))
        log = rd.root / "stdout.log"
        log.write_text("sealed supercache miss at pc=0x0\n")
        return {"verdict": "FAIL", "exit_code": 1, "appended": 0, "output_norm_sha256": None,
                "guest_norm": [], "qemu": "q", "qemu_sha256": None, "stdout_log": str(log)}

    monkeypatch.setattr(D, "deploy_new", fake_deploy_new)
    monkeypatch.setattr(LA, "launch", fake_launch)
    doc = profile_run(base, sc, tmp_path / "o", bundle=b, runtime_identity=r,
                      phases=("steady@0x2990",), reps=2, sealed=True, trusted=True)
    assert len(seen) == 2
    for env in seen:
        assert env["QEMU_SC_SEALED"] == "1" and env["QEMU_SC_TRUSTED"] == "1"
        assert env["QEMU_SC_PROFILE_PHASES"] == "steady@0x2990"
        assert env["QEMU_SC_PROFILE"].endswith("run.qprof")
    assert doc["verdict"] == "FAIL"
    saved = json.loads((tmp_path / "o" / "profile_run.json").read_text())
    assert saved["sealed"] is True and saved["trusted"] is True
    assert saved["target_key_mode"] == "exact_pc"
    assert saved["replay_env"] == {"QEMU_SC_SEALED": "1", "QEMU_SC_TRUSTED": "1"}


def test_ordinary_run_records_no_sealed_switches(tmp_path, monkeypatch):
    from scopt.run import deploy as D

    base, b, r = _docs(tmp_path)
    sc = _sc(tmp_path / "x.sc", exact_pc=False)
    seen = []

    def fake_deploy_new(target, base_toml, rd_path, **kw):
        seen.append(None)
        raise D.DeployError("stop", "test")

    monkeypatch.setattr(D, "deploy_new", fake_deploy_new)
    doc = profile_run(base, sc, tmp_path / "o", bundle=b, runtime_identity=r)
    assert seen and doc["sealed"] is False and doc["trusted"] is False
    assert doc["target_key_mode"] == "pcrel" and doc["replay_env"] == {}


def test_cli_passes_sealed_and_trusted(tmp_path, monkeypatch):
    from scopt.cli import cli, load_commands

    load_commands()
    r = CliRunner().invoke(cli, ["profile", "run", "--help"])
    assert r.exit_code == 0 and "--sealed" in r.output and "--trusted" in r.output
    got = {}

    def fake_profile_run(base_toml, target, out, **kw):
        got.update(kw)
        raise ProfileRunError("stop", "test")

    monkeypatch.setattr(PR, "profile_run", fake_profile_run)
    base, _, _ = _docs(tmp_path)
    sc = _sc(tmp_path / "x.sc", exact_pc=True)
    args = ["profile", "run", "-c", str(base), "--snapshot", str(sc), "--out",
            str(tmp_path / "o"), "--phase", "steady@0x2990"]
    r = CliRunner().invoke(cli, [*args, "--sealed", "--trusted"])
    assert r.exit_code == 2 and "refused: stop" in r.output
    assert got["sealed"] is True and got["trusted"] is True
    got.clear()
    CliRunner().invoke(cli, args)
    assert got["sealed"] is False and got["trusted"] is False


def test_sealed_exact_pc_profile_admits_b3_passes(tmp_path):
    """A steady profile collected on an exact-PC (sealed) capture joins every
    record and meets the B3 passes' phase and match requirements."""
    from scopt.analysis.store import profile_phase_info
    from scopt.formats import profile_v1 as P
    from scopt.passes.chain_returns import PASSES as CHAIN
    from scopt.passes.chain_static import PASSES as STATIC
    from scopt.passes.specialize_returns import PASSES as SPEC
    from tests.synth.profile_world import PHASES, RUNTIME, SHA_BUNDLE, SHA_CONTRACT

    key = replace(B.DEFAULT_KEY, cflags=B.DEFAULT_KEY.cflags & ~B.CF_PCREL)
    units = B.simple_units(3, key=key)
    snap = str(_sc(tmp_path / "snap.sc", exact_pc=True))
    assert target_key_mode(snap) == "exact_pc"
    o = [u.tb_offset for u in units]
    D = B.Dispatch
    trace = [D(o[0]), D(o[1]), D(o[2], exit="lookup_hit"), D(o[0]), D(o[1]), D(o[2]), D(o[0])]
    qprof = str(tmp_path / "run.qprof")
    B.simulate_qprof(B.qprof_rows_for(units), trace, phases=PHASES, phase_starts={1: 3},
                     header=B.qprof_binding(snap), path=qprof)
    ids = dict(bundle_id=SHA_BUNDLE, contract_id=SHA_CONTRACT, runtime_identity=RUNTIME)
    # a sealed replay appends nothing: the staged copy is the snapshot itself
    prof = P.ingest(qprof, snap, snap, **ids)
    assert prof.complete and not prof.profile_only
    view = P.ProfileView.open(prof, P.ProfileTarget.from_any(snap, **ids))
    present, reached, match = profile_phase_info(view, "steady", view.target.unit_ids)
    assert present and reached and match == 1.0
    names = set()
    for cls in (*CHAIN, *STATIC, *SPEC):
        d = cls.descriptor
        names.add(d.name)
        assert d.requires_profile_phase == "steady" and match >= d.min_profile_match
    assert names == {"chain-returns", "chain-static", "specialize-returns"}

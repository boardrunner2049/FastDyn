"""WP14 additions: ``inspected`` ledger rows, report candidate->artifact join,
``scopt prepare`` / ``scopt profile run`` refusals, isolation hashing, the
compute workload of the T-B1-0 driver, and the CLI registration."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from scopt.formats import artifact as A
from scopt.ids import artifact_id
from scopt.report.collect import collect
from scopt.run.prepare import PrepareError, prepare
from scopt.run.profile_run import ProfileRunError, _diff, profile_run, tree_hashes
from tests.synth import rich_tb as RT
from tests.unit.test_wp10_link import make_snapshot, run

ROOT = Path(os.environ.get("FASTDYN_ROOT", "/data/extra_copy/fastdyn"))


# ---------------------------------------------------------------------------
# ledger: inspected
# ---------------------------------------------------------------------------

def test_identity_artifact_ledger_has_inspected_rows(tmp_path):
    units = RT.rich_units(3)
    snap = make_snapshot(tmp_path, units)
    res = run(snap, tmp_path / "store")
    assert res.published, res.reason
    rows = [json.loads(x) for x in (Path(res.artifact_dir) / A.LEDGER).read_text().splitlines()]
    unit_rows = [r for r in rows if r["subject"] == "unit" and r["stage"] == "inspected"]
    assert [r["id"] for r in unit_rows] == [u.uid for u in snap.units()]
    assert {r["verdict"] for r in unit_rows} == {"ok_relocatable"}
    assert all(r["evidence_ref"] == snap.snapshot_id for r in unit_rows)
    art = [r for r in rows if r["subject"] == "artifact"]
    assert [(r["stage"], r["verdict"]) for r in art] == [
        ("inspected", "pass"), ("compiled", "pass"), ("accepted", "pass")]
    assert art[0]["id"] == res.candidate_id
    assert "3 units" in art[0]["reason"] and "ok_relocatable 3" in art[0]["reason"]


def test_inspected_rows_carry_pin_reasons(tmp_path):
    from tests.synth import builders as B

    units = RT.rich_units(2)
    snap = make_snapshot(tmp_path, units)
    # a pinned unit: re-import with one capture site dropped (unexplained_reference)
    prep = tmp_path / "prep-s"
    scm = prep / "s.scm"
    scm.write_bytes(B.drop_site(scm.read_bytes(), 1, 3))
    from scopt.importer.import_cache import import_snapshot

    res0 = import_snapshot(str(prep), store=str(tmp_path / "store2"))
    from dataclasses import replace

    s2 = replace(res0.snapshot, bundle=snap.bundle, runtime=snap.runtime)
    res = run(s2, tmp_path / "store2")
    rows = [json.loads(x) for x in (Path(res.artifact_dir) / A.LEDGER).read_text().splitlines()]
    pin = [r for r in rows if r["stage"] == "inspected" and r["verdict"] == "pinned"]
    assert len(pin) == 1 and pin[0]["reason"].startswith("unexplained_reference@")


# ---------------------------------------------------------------------------
# report: one row per artifact, candidate id joined
# ---------------------------------------------------------------------------

def _fake_artifact(root: Path, cid: str) -> tuple[Path, str]:
    art = root / "store/artifacts/x"
    art.mkdir(parents=True)
    (art / "cache.sc").write_bytes(b"")
    (art / "cache.sc.scopt.json").write_text(json.dumps({"x_candidate_id": cid,
                                                         "move_map": {}, "pinned": []}))
    man = b"0" * 64 + b"  cache.sc\n"
    (art / "MANIFEST.sha256").write_bytes(man)
    return art, artifact_id(man)


def _row(subject, id_, stage, verdict, ts):
    return json.dumps({"subject": subject, "id": id_, "stage": stage, "verdict": verdict,
                       "reason": "", "evidence_ref": None, "ts": ts})


def test_report_joins_candidate_rows_to_the_artifact(tmp_path):
    cid = "sha256:" + "c" * 64
    art, aid = _fake_artifact(tmp_path, cid)
    (art / "ledger.jsonl").write_text("\n".join([
        _row("artifact", cid, "inspected", "pass", "2026-01-01T00:00:00.000Z"),
        _row("artifact", cid, "compiled", "pass", "2026-01-01T00:00:01.000Z"),
        _row("artifact", cid, "accepted", "pass", "2026-01-01T00:00:02.000Z")]) + "\n")
    (tmp_path / "v10").mkdir()
    (tmp_path / "v10/ledger.jsonl").write_text(
        _row("artifact", aid, "validated_dynamic", "pass", "2026-01-01T00:00:03.000Z") + "\n")
    model = collect([str(tmp_path)])
    rows = [t for t in model["ledger"]["table"] if t["subject"] == "artifact"]
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == aid and r["aliases"] == [cid]
    assert {k: v["verdict"] for k, v in r["stages"].items()} == {
        "inspected": "pass", "optimized": "not_run", "compiled": "pass", "accepted": "pass",
        "validated_dynamic": "pass", "measured": "not_run"}


def test_report_keeps_unpublished_candidates_apart(tmp_path):
    cid = "sha256:" + "d" * 64
    (tmp_path / "ledger.jsonl").write_text(
        _row("artifact", cid, "accepted", "fail", "2026-01-01T00:00:00.000Z") + "\n")
    model = collect([str(tmp_path)])
    rows = [t for t in model["ledger"]["table"] if t["subject"] == "artifact"]
    assert [r["id"] for r in rows] == [cid] and "aliases" not in rows[0]


# ---------------------------------------------------------------------------
# prepare / profile run refusals (no QEMU)
# ---------------------------------------------------------------------------

def _toml(tmp: Path, machine: str) -> Path:
    p = tmp / "base.toml"
    p.write_text(f"[Machine]\n{machine}\n")
    return p


def test_prepare_refuses_without_supercache(tmp_path):
    with pytest.raises(PrepareError) as e:
        prepare(_toml(tmp_path, 'platform = "x"'), tmp_path / "out")
    assert e.value.code == "no_supercache"
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("flat", ["", "supercache_flat = true"])
def test_prepare_refuses_unless_flat_is_false(tmp_path, flat):
    with pytest.raises(PrepareError) as e:
        prepare(_toml(tmp_path, f'supercache = "c/x.sc"\n{flat}'), tmp_path / "out")
    assert e.value.code == "unsupported_flat"


def test_profile_run_refusals(tmp_path):
    sc = tmp_path / "x.sc"
    sc.write_bytes(b"")
    base = _toml(tmp_path, 'supercache = "c/x.sc"\nsupercache_flat = false')
    with pytest.raises(ProfileRunError) as e:
        profile_run(base, sc, tmp_path / "o", reps=0)
    assert e.value.code == "bad_reps"
    with pytest.raises(ProfileRunError) as e:
        profile_run(base, sc, tmp_path / "o", input_split="dev")
    assert e.value.code == "bad_split"
    with pytest.raises(ProfileRunError) as e:  # a bare .sc needs --bundle
        profile_run(base, sc, tmp_path / "o")
    assert e.value.code == "no_bundle"
    (tmp_path / "b.json").write_text("{}")
    with pytest.raises(ProfileRunError) as e:
        profile_run(base, sc, tmp_path / "o", bundle=tmp_path / "b.json")
    assert e.value.code == "no_runtime_identity"
    (tmp_path / "o").mkdir()
    (tmp_path / "o/used").write_text("x")
    (tmp_path / "r.json").write_text("{}")
    with pytest.raises(ProfileRunError) as e:
        profile_run(base, sc, tmp_path / "o", bundle=tmp_path / "b.json",
                    runtime_identity=tmp_path / "r.json")
    assert e.value.code == "out_not_fresh"


def test_tree_hashes_detect_changes(tmp_path):
    d = tmp_path / "t"
    d.mkdir()
    (d / "a").write_text("1")
    (d / "b").write_text("2")
    h0 = tree_hashes([d])
    assert _diff(h0, tree_hashes([d])) == []
    (d / "a").write_text("3")
    (d / "c").write_text("4")
    (d / "b").unlink()
    diff = _diff(h0, tree_hashes([d]))
    assert diff == [f"removed {d / 'b'}", f"added {d / 'c'}", f"changed {d / 'a'}"]


def test_cli_registers_prepare_and_profile_run():
    from scopt.cli import cli, load_commands

    load_commands()
    r = CliRunner().invoke(cli, ["prepare", "--help"])
    assert r.exit_code == 0 and "QEMU_SC_CAPTURE" in r.output
    r = CliRunner().invoke(cli, ["profile", "run", "--help"])
    assert r.exit_code == 0 and "--phase" in r.output and "--watch" in r.output
    r = CliRunner().invoke(cli, ["validate", "--help"])
    assert "--inputs" in r.output
    r = CliRunner().invoke(cli, ["qualify-capture", "--help"])
    assert "--input" in r.output


# ---------------------------------------------------------------------------
# T-B1-0 driver: compute workload TOMLs
# ---------------------------------------------------------------------------

def test_t_b1_0_compute_tomls(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "t_b1_0", Path(__file__).resolve().parents[1] / "qemu/t_b1_0.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    src = tmp_path / "compute-sc.toml"
    src.write_text('[Machine]\nqemu_path = "/x/qemu"\nsupercache = "cache/c.sc"\n'
                   'supercache_flat = false\n')
    exp = tmp_path / "exp.norm"
    exp.write_text("exit_code=0\n")
    c = mod.Compute(tmp_path / "cfg", src=src, inp=tmp_path / "in.bin", expected=exp)
    q = tmp_path / "qemu-new"
    sc = c.sc_toml(q).read_text()
    assert f'qemu_path = "{q}"' in sc and 'supercache = "cache/c.sc"' in sc
    nc = c.nocache_toml(q).read_text()
    assert "supercache =" not in nc and "supercache_flat = false" in nc
    assert f'qemu_path = "{q}"' in nc
    assert c.inputs() == {"input": tmp_path / "in.bin"}

"""CLI behaviour found by a fresh-clone run of docs/getting-started.md: static
validate on artifacts it does not apply to, `rundir launch --inputs` and
`--reference-norm`, and RunDir paths given as str."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from scopt.cli import cli, load_commands
from scopt.formats import artifact as A
from scopt.validate import optimize, static_not_applicable
from tests.synth import rich_tb as RT
from tests.unit.test_wp10_link import make_snapshot

BASE_TOML = """\
[Memory]
[Memory.main]
id = "ram0"
base_address = "0x20000000"
memory_size = "64K"
backend = "file"
memory_file = "ram0.ram"

[[Memory.input]]
id = "input"
base_address = "0x30000000"
memory_size = "4K"
backend = "file"
memory_file = "input.bin"

[Machine]
platform = "generic-cortexm"
supercache = "cache/x.sc"
supercache_flat = false
qmp_socket = "qmp.sock"

[CPU]
[[CPU.cpu0]]
arch = "arm"
"""


def _info(side: dict, codes: dict | None):
    return A.ArtifactInfo(Path("/nonexistent"), None, side, None, None,
                          None if codes is None else {"codes": codes}, [])


def test_static_not_applicable_rules():
    v5 = {"loader_requirement": A.LOADER_V5}
    move_only = {f"V{i}": "pass" for i in range(1, 10)}
    assert static_not_applicable(_info(v5, move_only)) is None
    assert static_not_applicable(_info(v5, {**move_only, "V10": "pass"})) is None
    assert static_not_applicable(_info(v5, None)) is None  # V1-V9 then reports it
    b4 = {**move_only, "V12": "pass", "V20": "pass"}
    assert "V12" in static_not_applicable(_info(v5, b4))
    sfb = {"SFB1": "pass", "SFB2": "pass"}
    assert "SFB1" in static_not_applicable(_info(v5, sfb))
    # the icount-entry packager writes rows without a "codes" summary
    rows_only = A.ArtifactInfo(Path("/nonexistent"), None, v5, None, None,
                               {"rows": [{"code": "SFB1", "result": "pass"}]}, [])
    assert "SFB1" in static_not_applicable(rows_only)
    assert "v5 only" in static_not_applicable(_info({"loader_requirement": "qemu-sc-v6"},
                                                    move_only))


def test_static_validate_refuses_b3_b4_artifacts(tmp_path):
    """Exit 2 with a reason, never a traceback or a V2 'rejected' for edits that
    another validator certified; a move-only artifact is still validated."""
    snap = make_snapshot(tmp_path, RT.rich_units(2))
    res = optimize(snap, "identity", tmp_path / "store")
    assert res.published
    load_commands()
    r = CliRunner().invoke(cli, ["validate", res.artifact_dir, "--snapshot", snap.path])
    assert r.exit_code == 1 and "V7" in r.output  # validated (store copy lacks identity)

    art = tmp_path / "b4like"
    shutil.copytree(res.artifact_dir, art)
    os.chmod(art, 0o755)
    for f in art.iterdir():
        os.chmod(f, 0o644)
    val = json.loads((art / A.VALIDATION).read_text())
    val["codes"]["V12"] = "pass"
    (art / A.VALIDATION).write_text(json.dumps(val))
    r = CliRunner().invoke(cli, ["validate", str(art), "--snapshot", snap.path])
    assert r.exit_code == 2, r.output
    assert "static_not_applicable" in r.output and "scopt publish" in r.output

    side = json.loads((art / A.SIDECAR).read_text())
    side["loader_requirement"] = "qemu-sc-v6"
    (art / A.SIDECAR).write_text(json.dumps(side))
    r = CliRunner().invoke(cli, ["validate", str(art), "--snapshot", snap.path])
    assert r.exit_code == 2 and "v5 only" in r.output


@pytest.fixture
def run_dir(tmp_path, fake_runtime_qemu):
    from scopt.run.launcher import create_run_dir

    base = tmp_path / "base.toml"
    base.write_text(BASE_TOML)
    return create_run_dir(base, tmp_path / "rd")


def test_rundir_accepts_str(run_dir):
    from scopt.run.launcher import RunDir

    rd = RunDir(str(run_dir.root))
    assert isinstance(rd.root, Path) and rd.toml == run_dir.toml


def test_rundir_launch_inputs_and_reference_norm(run_dir, tmp_path, monkeypatch):
    from scopt.run import launcher as L

    seen = {}
    equal = {"v": True}

    def fake_launch(rd, **kw):
        seen.update(kw)
        doc = {"verdict": "PASS", "exit_code": 0, "appended": 0, "bench": None,
               "proof": {"failed": []}}
        if kw.get("reference_norm"):
            doc["reference_norm"] = {"equal": equal["v"]}
        return doc

    monkeypatch.setattr(L, "launch", fake_launch)
    inp = tmp_path / "in.bin"
    inp.write_bytes(b"\x01\x02")
    ref = tmp_path / "ref.norm"
    ref.write_text("x\n")
    load_commands()
    r = CliRunner().invoke(cli, ["rundir", "launch", str(run_dir.root), "--inputs", str(inp)])
    assert r.exit_code == 0, r.output
    assert seen["inputs"] == {"input": str(inp)}
    r = CliRunner().invoke(cli, ["rundir", "launch", str(run_dir.root), "--inputs", str(inp),
                                 "--input-bank", "other", "--reference-norm", str(ref)])
    assert r.exit_code == 0 and seen["inputs"] == {"other": str(inp)}
    equal["v"] = False
    r = CliRunner().invoke(cli, ["rundir", "launch", str(run_dir.root),
                                 "--reference-norm", str(ref)])
    assert r.exit_code == 1 and "differs from --reference-norm" in r.output
    assert seen["inputs"] is None


def test_launch_loads_input_after_ram_reset(run_dir, tmp_path):
    """The bank file holds the input after launch's RAM reset (load_inputs is the
    step `rundir launch --inputs` adds)."""
    from scopt.run.launcher import load_inputs, reset_ram

    bank = run_dir.root / "ram"
    reset_ram(run_dir)
    inp = tmp_path / "in.bin"
    inp.write_bytes(b"\xaa\xbb")
    info = load_inputs(run_dir, {"input": inp})
    data = Path(info["input"]["bank_file"]).read_bytes()
    assert Path(info["input"]["bank_file"]).parent == bank
    assert data[:2] == b"\xaa\xbb" and len(data) == 4096

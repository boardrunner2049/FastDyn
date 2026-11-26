"""WP13 acceptance on real B0 runs through WP8 (IMPLEMENTATION-PLAN.md, WP13).

Runs ``scopt run`` with the B0 binary (``src/qemu/build/qemu-system-arm``),
``configs/b0-sc.toml`` / ``b0-nocache.toml`` and the golden ``minimal.sc``
(deployed as a copy; the golden file is only read), then ``scopt report``.
Run with ``python -m pytest -m qemu tests/acceptance/test_wp13_arms_real.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from scopt.cli import cli, load_commands
from scopt.formats.sc_records import sha256_sparse
from scopt.run.launcher import B0_QEMU, FASTDYN_SRC
from scopt.paths import fastdyn_root

pytestmark = [pytest.mark.qemu, pytest.mark.real, pytest.mark.slow]

ROOT = fastdyn_root()
B0_SC_TOML = FASTDYN_SRC / "configs/b0-sc.toml"
B0_NOCACHE_TOML = FASTDYN_SRC / "configs/b0-nocache.toml"
MINIMAL = ROOT / "runs/b0/cache/minimal.sc"


@pytest.fixture(scope="module", autouse=True)
def _need_b0():
    for p in (B0_QEMU, B0_SC_TOML, B0_NOCACHE_TOML, MINIMAL):
        if not p.exists():
            pytest.skip(f"{p} not present")


def _cli(*args):
    load_commands()
    return CliRunner().invoke(cli, list(map(str, args)), catch_exceptions=False)


def test_b0_nocache_warm_bench_comparison(tmp_path):
    golden = sha256_sparse(MINIMAL)
    out = tmp_path / "b0"
    r = _cli("run", "-c", B0_SC_TOML, "--arms", "nocache,empty,warm", "--warm", MINIMAL,
             "--nocache-config", B0_NOCACHE_TOML, "--qemu", B0_QEMU, "--reps", 3, "--seed", 4,
             "--out", out)
    assert r.exit_code == 0, r.output
    camp = json.loads((out / "campaign.json").read_text())
    assert camp["status"] == "done" and camp["freeze_ok"] is True
    assert camp["n_runs"] == 9 and camp["n_valid"] == 9, r.output
    rows = [json.loads(x) for x in (out / "runs.jsonl").read_text().splitlines()]
    for row in rows:
        run = json.loads(Path(row["run_json"]).read_text())
        assert run["qemu_sha256"] == "sha256:" + __import__("hashlib").sha256(
            B0_QEMU.read_bytes()).hexdigest()
        assert "Reg 0:0x00000003" in run["guest_norm"]
        if row["arm"] == "warm":
            assert run["verdict"] == "PASS" and run["appended"] == 0
        else:
            assert run["verdict"] == "NOT_PROVED"
    assert sha256_sparse(MINIMAL) == golden  # golden untouched

    rr = _cli("report", out, "--out", tmp_path / "rep" / "report.md", "--bootstrap", 2000)
    assert rr.exit_code == 0, rr.output
    md = (tmp_path / "rep" / "report.md").read_text()
    assert "**BENCH comparison (paired speedup vs reference)**" in md
    assert "| nocache | warm | fixed | 3 |" in md
    assert "| empty | warm | fixed | 3 |" in md
    m = json.loads((tmp_path / "rep" / "report.json").read_text())
    g = m["groups"][0]
    c = {x["arm"]: x for x in g["comparisons"] if x["metric"] == "bench"}
    # warm reuse is much faster than no cache on minimal (B0: ~0.22 ms vs ~1.1 ms)
    assert c["nocache"]["median"] < 0.6
    assert {a["arm"]: a["status"] for a in m["arms"]}["warm"] == "measured"
    sizes = {s["label"].rsplit("/", 1)[1]: s for s in m["sc_metrics"]}
    assert sizes["warm"]["valid_records"] == 163
    assert sizes["warm"]["arena_capacity_bytes"] == 2 << 30


QINT = ROOT / "wt/qemu-int/build/qemu-system-arm"
COMPUTE = ROOT / "fixtures/freertos-mps2-compute"
INPUTS = ROOT / "fixtures/inputs"


def test_compute_fixture_input_set_qint(tmp_path):
    """The WP12 input contract on a real fixture with the integrated binary: a held-out
    input set from its MANIFEST, the warm cache seeded on a train input, each input
    written into the `input` RAM bank (the guest's 16 result registers equal the
    fixture's expected/<input>.icount.norm), outputs equal across arms per input."""
    tpl = COMPUTE / "configs/freertos-compute.toml.in"
    for p in (QINT, tpl, INPUTS / "heldout/freertos-mps2-compute/MANIFEST.json",
              INPUTS / "train/freertos-mps2-compute/train-1.bin"):
        if not p.exists():
            pytest.skip(f"{p} not present")
    base = tmp_path / "compute-sc.toml"
    base.write_text(tpl.read_text()
                    .replace("@SUPERCACHE@", 'supercache = "cache/freertos-compute.sc"\n'
                             "supercache_flat = false")
                    .replace("@ICOUNT@", "icount = { shift = 0, sleep = false, align = false }")
                    .replace("@QEMU@", str(QINT)).replace("@LOG_OPTIONS@", "none"))
    out = tmp_path / "ho"
    r = _cli("run", "-c", base, "--arms", "nocache,warm",
             "--input-set", INPUTS / "heldout/freertos-mps2-compute",
             "--seed-input", INPUTS / "train/freertos-mps2-compute/train-1.bin",
             "--qemu", QINT, "--reps", 1, "--seed", 2, "--timeout", 300, "--out", out)
    assert r.exit_code == 0, r.output
    camp = json.loads((out / "campaign.json").read_text())
    assert camp["campaign"]["split"] == "heldout"
    assert camp["n_runs"] == 6 and camp["n_valid"] == 6 and camp["freeze_ok"] is True
    assert camp["warm_auto"]["warm"]["seed_input"].endswith("train-1.bin")
    qsha = "sha256:" + __import__("hashlib").sha256(QINT.read_bytes()).hexdigest()
    assert camp["qemu_seen"] == [qsha]
    rows = [json.loads(x) for x in (out / "runs.jsonl").read_text().splitlines()]
    by_input: dict = {}
    for row in rows:
        run = json.loads(Path(row["run_json"]).read_text())
        name = Path(run["inputs"]["input"]["path"]).stem
        regs = [x for x in run["guest_norm"] if x.startswith("Reg")]
        exp = [x for x in (COMPUTE / "expected" / f"{name}.icount.norm").read_text()
               .splitlines() if x.startswith("Reg")]
        assert len(regs) == 16 and regs == exp, (row["arm"], name)
        by_input.setdefault(name, set()).add(row["guest_norm_sha256"])
        if row["arm"] == "warm":
            assert run["verdict"] == "PASS" and run["appended"] is not None
    assert sorted(by_input) == ["heldout-1", "heldout-2", "heldout-3"]
    assert all(len(v) == 1 for v in by_input.values())
    m = json.loads((out / "report.json").read_text())
    assert not [x for x in m["regressions"] if x["kind"] in ("output_mismatch", "mixed_qemu",
                                                             "invalid_runs")]

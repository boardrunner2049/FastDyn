"""WP25 unit-stage acceptance (T-B4-1, T-B4-2, T-B4-2b static, R10): B4 artifacts
packaged through V1-V23 with hostsym, and every B4 mutant rejected statically.

No QEMU run. The dynamic parts (F-B4-1…F-B4-10, compute V10, timing) are the
integration stage.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
FAULT = ROOT / ("runs/wp15a/fault/store/snapshots/"
                "4e404d5e7335399768249ef95453594fe8098647ef7927c1234c5a27dee16841")
COMPUTE = ROOT / ("runs/b2/compute/store/snapshots/"
                  "53b4132fc05526ff2925cd3acc610bf613e8b11e2315e15e520bff7fe080dab2")
IDENT = ROOT / "runs/wp17/admission"

pytestmark = pytest.mark.real


def _load(p: Path, name: str):
    if not (p / "snapshot.json").is_file():
        pytest.skip(f"{p} not present")
    ip = IDENT / f"{name}.runtime_identity.v2.json"
    if not ip.is_file():
        pytest.skip(f"{ip} not present")
    from scopt.formats.envlayout_v1 import find_envlayout
    from scopt.importer.import_cache import open_snapshot

    s = open_snapshot(p)
    ri = json.loads(ip.read_text())
    return s, find_envlayout(ri["qemu_sha256"]), ri


@pytest.fixture(scope="module")
def selftest(tmp_path_factory):
    from scopt.validate_sym.selftest import load_or_run

    cache = os.environ.get("SCOPT_SELFTEST_CACHE")
    return load_or_run(cache or str(tmp_path_factory.mktemp("st") / "selftest.json"))


def _rows(val, code):
    return [r for r in val.rows if r["code"] == code]


def test_fault_b4_artifact_certified(tmp_path, selftest):
    from scopt.passes.eliminate_state_sync import package_b4, propose_b4

    s, lay, ri = _load(FAULT, "fault")
    prop = propose_b4(s, layout=lay, runtime=ri)
    assert prop.n_patches > 0
    r = package_b4(s, prop, tmp_path, candidate_id="t-b4-fault", layout=lay, runtime=ri,
                   selftest=selftest)
    assert r.status == "published", r.reason
    v = r.validation
    v12 = _rows(v, "V12")
    assert all(x["result"] == "pass" for x in v12)
    # one verdict per rewritten unit, and the coverage row names every one of them
    assert len([x for x in v12 if x["check"].startswith("equiv:")]) == len(prop.patches)
    cov = next(x for x in v12 if x["check"] == "coverage")
    assert cov["required"] == [f"U':{u}" for u in sorted(prop.patches)]
    assert all(x["result"] == "pass" for x in _rows(v, "V15"))
    assert all(x["result"] != "fail" for x in _rows(v, "V20"))
    side = json.loads((Path(r.artifact_dir) / "cache.sc.scopt.json").read_text())
    assert side["loader_requirement"] == "qemu-sc-v5-unmodified"
    assert side["pipeline"][0]["x_classes"] == prop.by_class()


def test_fault_b4_mutants_rejected(tmp_path, selftest):
    from scopt.b4_mutants import check_mutant, find_mutants

    s, lay, ri = _load(FAULT, "fault")
    ms = find_mutants(s, lay, ri, per_kind=1)
    kinds = {m.kind for m in ms}
    assert {"guest_store_delete", "flag_store_before_ldst", "cdio_last_insn_direct_chain",
            "it_block_d4", "d1_before_exit", "d1_before_udiv", "d3_before_plugin_call",
            "d3_without_fact", "pc_store_delete"} <= kinds
    for m in ms:
        r = check_mutant(s, m, tmp_path, layout=lay, runtime=ri, selftest=selftest)
        assert r.rejected, (m.kind, r.status, r.reason)
        assert r.failed_rows, m.kind


def test_synthetic_b4_mutants_rejected():
    from scopt.b4_mutants import synthetic_b4

    d = synthetic_b4()
    assert d["all_rejected"] and d["controls_ok"]
    names = {r["name"] for r in d["results"]}
    assert {"update_mem_delete", "log_reg_delete", "host_abs_in_env"} <= names


@pytest.mark.slow
def test_compute_b4_artifact_and_mutants(tmp_path, selftest):
    from scopt.b4_mutants import check_mutant, find_mutants
    from scopt.passes.eliminate_state_sync import package_b4, propose_b4

    s, lay, ri = _load(COMPUTE, "compute")
    prop = propose_b4(s, layout=lay, runtime=ri)
    r = package_b4(s, prop, tmp_path / "store", candidate_id="t-b4-compute", layout=lay,
                   runtime=ri, selftest=selftest)
    assert r.status == "published", r.reason
    assert len([x for x in _rows(r.validation, "V12")
                if x["check"].startswith("equiv:")]) == len(prop.patches)
    for m in find_mutants(s, lay, ri, per_kind=1):
        if m.kind in ("brcond_reload", "flag_store_before_helper", "icount_immediate"):
            c = check_mutant(s, m, tmp_path / "mut", layout=lay, runtime=ri, selftest=selftest)
            assert c.rejected, (m.kind, c.reason)

import json
from pathlib import Path
import sys

import pytest

pytest.importorskip("scopt")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from candidates import build, deployment_identity, optimizer_args


def test_chaining_only_explicitly_disables_specialization(tmp_path):
    image = dict(profile="train", snapshot="ordinary", prep="capture", firmware="guest",
                 admission_runtime="identity", runtime=__file__)
    args, names, expected = optimizer_args("general", image, "parent", tmp_path, {}, ["published"])
    assert expected == ["chain-static", "chain-returns"]
    disabled = [args[i+1] for i, flag in enumerate(args) if flag == "--disable"]
    enabled = [args[i+1] for i, flag in enumerate(args) if flag == "--enable"]
    assert set(disabled) == set(names) - set(expected)
    assert sorted(enabled) == sorted(expected)
    assert args[args.index("--warm-stage") + 1] == "published"


def test_layout_runs_after_growth(tmp_path):
    image = dict(profile="train", snapshot="ordinary", prep="capture", firmware="guest",
                 admission_runtime="identity", runtime=__file__)
    _, names, _ = optimizer_args("b3-ph", image, "parent", tmp_path, {}, [])
    assert names.index("chain-static") < names.index("cluster-hot")
    assert names[-1] == "order-cold"


def test_identity_changes_with_guest_and_restore_metadata(tmp_path):
    for name in ("cache.sc", "bundle.json", "runtime_identity.json", "fw", "cfg", "qemu"):
        (tmp_path / name).write_bytes(name.encode())
    def digest():
        return deployment_identity(tmp_path, tmp_path/"cfg", tmp_path/"fw", tmp_path/"qemu", "contract")
    original = digest()
    (tmp_path/"cache.sc.restore-hints").write_bytes(b"hints")
    changed = digest()
    assert changed != original
    (tmp_path/"fw").write_bytes(b"other guest")
    assert digest() not in (original, changed)


def test_failed_image_records_all_candidates(tmp_path):
    admission = tmp_path / "admission.json"
    admission.write_text(json.dumps({"status": "admitted", "application": "test-only"}))
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(dict(admission=str(admission), application="test-only", scenario="fixture",
        objective=dict(name="test", useful_work="fixture", unit="ns", boundaries="test", direction="minimize"),
        contract=str(admission), configuration="fixture", images={"original": {"snapshot": "/missing"}},
        budget=dict(candidate_limit=4, offline_seconds=10))))
    reg = build(p, tmp_path / "registry")
    assert len(reg["candidates"]) == 4 and reg["state"] == "completed"
    assert all(r["status"] == "failed" and r["reason"] for r in reg["candidates"])


def test_native_mps2_reset_vector_without_fastdyn_override():
    from scopt.formats.bundle_v1 import BundleSchemaError, _vector_base
    assert _vector_base({"machine": "mps2-an385", "init_nsvtor": None}) == 0
    assert _vector_base({"machine": "mps2-an385", "init_nsvtor": "0x1000"}) == 0x1000
    with pytest.raises(BundleSchemaError):
        _vector_base({"machine": "unknown", "init_nsvtor": None})


@pytest.mark.parametrize("sites", [1, 2])
def test_native_recipe_binds_integer_site_limit(tmp_path, sites):
    image = dict(profile="train", snapshot="ordinary", prep="capture", firmware="guest",
                 admission_runtime="identity", runtime=__file__)
    stage = f"native-{sites}"
    args, _, _ = optimizer_args(stage, image, "parent", tmp_path, {}, [])
    parameters = json.loads(Path(args[args.index("--params") + 1]).read_text())
    assert parameters["host-inline"] == {"mode": "splice", "allow_faulting": False, "max_sites_per_unit": sites}
    with pytest.raises(ValueError, match="conflict"):
        optimizer_args(stage, image, "parent", tmp_path, {"host-inline": {"allow_faulting": True}}, [])

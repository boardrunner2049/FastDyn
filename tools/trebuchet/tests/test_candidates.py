import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("scopt")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import candidates
from candidates import build, deployment_identity, optimizer_args, resolved_recipe, stage_parameters


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


@pytest.fixture
def registry_builder(tmp_path, monkeypatch):
    """Exercise registry reuse and real byte identities without native execution.

    The fake materializer emits distinct bytes for distinct source/parameter
    combinations. It never emits performance observations or an executable cache.
    """
    admission = tmp_path / "admission.json"
    admission.write_text(json.dumps({"status": "admitted", "application": "unit-test-only"}))
    images = {}
    for name in ("original", "firmbolt"):
        directory = tmp_path / name
        directory.mkdir()
        image = {}
        for key in ("firmware", "config", "runtime", "profile", "prep", "manifest", "admission_runtime"):
            path = directory / key
            path.write_text(name + ":" + key)
            image[key] = str(path)
        snapshot = directory / "ordinary"
        snapshot.mkdir()
        (snapshot / "cache.sc").write_bytes((name + ":ordinary-not-an-executable-cache").encode())
        (snapshot / "bundle.json").write_text(json.dumps({"image_hash": candidates.file_sha256(image["firmware"])}))
        (snapshot / "runtime_identity.json").write_text(json.dumps({"qemu_sha256": candidates.file_sha256(image["runtime"])}))
        image["snapshot"] = str(snapshot)
        images[name] = image
    monkeypatch.setattr(candidates, "prepare_image", lambda image: (
        SimpleNamespace(snapshot_id=candidates.file_sha256(Path(image["snapshot"]) / "cache.sc")),
        SimpleNamespace(profile_id=candidates.file_sha256(image["profile"]), doc={})))
    monkeypatch.setattr(candidates, "read_bundle", lambda path: json.loads(Path(path).read_text()))
    monkeypatch.setattr(candidates, "hotset_metrics", lambda profile, cache: {})
    calls = []
    failures = set()

    def materialize(stage, previous, image, root, params, remaining, **options):
        call = dict(stage=stage, params=params, parent=previous["snapshot_id"],
                    directory=str(root), remaining=remaining, image=image["firmware"])
        calls.append(call)
        root.mkdir()
        if params.get("host-inline", {}).get("udiv_mode") in failures:
            raise ValueError("test materializer rejected this variant")
        token = candidates.canonical_json({"parent": previous["snapshot_id"], "stage": stage, "params": params})
        (root / "cache.sc").write_bytes(token)
        for filename in ("bundle.json", "runtime_identity.json"):
            shutil.copyfile(Path(previous["artifact"]) / filename, root / filename)
        output = candidates.sha256_id(token)
        record = dict(stage=stage, params=params, input=previous["snapshot_id"], output=output,
                      receipt=str(root / "stdout.json"), effective_passes=[])
        (root / "stdout.json").write_text(json.dumps(record))
        return dict(snapshot=str(root), snapshot_id=output, artifact=str(root),
                    ancestry=[*previous["ancestry"], str(root)],
                    generations=[*previous["generations"], record])

    monkeypatch.setattr(candidates, "materialize", materialize)
    counter = 0

    def run(recipes, nodes=None, params=None, both_images=False, limit=None, **build_options):
        nonlocal counter
        counter += 1
        plan = dict(admission=str(admission), application="unit-test-only", scenario="fixture",
                    configuration="fixture", contract=str(admission), recipes=recipes,
                    images=images if both_images else {"original": images["original"]},
                    objective=dict(name="test", useful_work="fixture", unit="ns", boundaries="test", direction="minimize"),
                    budget=dict(candidate_limit=limit if limit is not None else 2 * len(recipes), offline_seconds=30))
        if nodes is not None:
            plan["nodes"] = nodes
        if params is not None:
            plan["params"] = params
        path = tmp_path / f"plan-{counter}.json"
        path.write_text(json.dumps(plan))
        return build(path, tmp_path / f"registry-{counter}", **build_options)

    return SimpleNamespace(run=run, calls=calls, failures=failures)


def test_named_parameter_variants_share_only_identical_generation_prefixes(registry_builder):
    nodes = {name: {"stage": "native", "params": {"host-inline": {"udiv_mode": mode}}}
             for name, mode in (("guarded", "guarded"), ("cleanup", "cleanup"), ("partial", "partial-cleanup"))}
    nodes["same-guarded"] = {"stage": "native", "params": {"host-inline": {
        "allow_faulting": True, "udiv_mode": "guarded", "mode": "splice"}}}
    recipes = candidates.CORE + ["guarded", "cleanup", "partial", "guarded+b3", "cleanup+b3", "partial+b3",
                                  "same-guarded", "same-guarded+b3"]
    registry = registry_builder.run(recipes, nodes, params={"native": {"host-inline": {
        "mode": "splice", "allow_faulting": True, "max_sites": 7}}})
    rows = {row["id"].split("/", 1)[1]: row for row in registry["candidates"]}
    assert all(row["status"] in ("ready", "equivalent") for row in rows.values())
    native = [c for c in registry_builder.calls if c["stage"] == "native"]
    assert len(native) == 3
    assert {c["params"]["host-inline"]["udiv_mode"] for c in native} == {"guarded", "cleanup", "partial-cleanup"}
    assert all(c["params"]["host-inline"]["max_sites"] == 7 for c in native)
    assert len({c["directory"] for c in registry_builder.calls}) == len(registry_builder.calls) == 9
    assert len({rows[name]["artifact"] for name in ("guarded", "cleanup", "partial")}) == 3
    assert len({rows[name]["artifact"] for name in ("guarded+b3", "cleanup+b3", "partial+b3")}) == 3
    for alias, source in (("same-guarded", "guarded"), ("same-guarded+b3", "guarded+b3")):
        assert rows[alias]["artifact"] == rows[source]["artifact"]
        assert rows[alias]["status"] == "equivalent"
        assert rows[alias]["equivalent_to"] == "original/" + source
        assert rows[alias]["recipe_identity"] == rows[source]["recipe_identity"]
        assert rows[alias]["recipe_nodes"][0]["name"] != rows[source]["recipe_nodes"][0]["name"]
        assert rows[alias]["recipe_nodes"][0]["identity"] == rows[source]["recipe_nodes"][0]["identity"]
    for row in rows.values():
        assert [g["stage"] for g in row["generations"]] == row["recipe"]
        assert [g["params"] for g in row["generations"]] == [n["params"] for n in row["recipe_nodes"]]
    assert rows["partial+b3"]["recipe"] == ["native", "b3"]
    assert Path(rows["b3"]["artifact"]).name == "original.b3"
    assert Path(rows["sfb+b3"]["artifact"]).name == "original.sfb.b3"


def test_named_nodes_include_alias_constraints_in_effective_identity():
    nodes = {"one": {"stage": "native-1"}, "one-explicit": {"stage": "native-1", "params": {
        "host-inline": {"max_sites_per_unit": 1, "allow_faulting": False, "mode": "splice"}}},
        "two": {"stage": "native-2"}}
    one, = resolved_recipe({}, "one", nodes)
    explicit, = resolved_recipe({}, "one-explicit", nodes)
    two, = resolved_recipe({}, "two", nodes)
    assert one["identity"] == explicit["identity"]
    assert one["identity"] != two["identity"]
    assert one["params"]["host-inline"] == {"max_sites_per_unit": 1, "allow_faulting": False, "mode": "splice"}
    with pytest.raises(ValueError, match="conflict"):
        stage_parameters("native-1", {"host-inline": {"max_sites_per_unit": True}})


def test_named_node_parameters_reach_optimizer_and_package_arguments(tmp_path):
    image = dict(profile="train", snapshot="ordinary", prep="capture", firmware="guest",
                 admission_runtime="identity", runtime=__file__)
    nodes = {"partial": {"stage": "native", "params": {"host-inline": {"udiv_mode": "partial-cleanup"}}},
             "hot90": {"stage": "sfb-hot", "params": {"entry_coverage": 90}}}
    plan = {"params": {"native": {"host-inline": {"mode": "splice", "allow_faulting": True}},
                       "sfb-hot": {"max_growth_bytes": 8192, "max_moved_units": 128}}}
    partial, hot = resolved_recipe(plan, "partial+hot90", nodes)
    args, _, expected = optimizer_args(partial["stage"], image, "parent", tmp_path, partial["params"], [])
    assert expected == ["host-inline"]
    assert json.loads(Path(args[args.index("--params") + 1]).read_text()) == {
        "host-inline": {"mode": "splice", "allow_faulting": True, "udiv_mode": "partial-cleanup"}}
    args, _, _ = optimizer_args(hot["stage"], image, "parent", tmp_path, hot["params"], [])
    assert args[args.index("--entry-coverage") + 1] == "90"
    assert args[args.index("--max-growth-bytes") + 1] == "8192"
    assert args[args.index("--max-moved-units") + 1] == "128"
    with pytest.raises(ValueError, match="unsupported sfb package parameters"):
        optimizer_args("sfb", image, "parent", tmp_path, {"entry_coverage": 90}, [])


def test_same_parameters_reuse_does_not_cross_image_bindings(registry_builder):
    registry = registry_builder.run(candidates.CORE + ["guarded", "alias"], both_images=True, nodes={
        "guarded": {"stage": "native", "params": {"host-inline": {"udiv_mode": "guarded"}}},
        "alias": {"stage": "native", "params": {"host-inline": {"udiv_mode": "guarded"}}}})
    native = [c for c in registry_builder.calls if c["stage"] == "native"]
    assert len(native) == 2 and native[0]["image"] != native[1]["image"]
    assert native[0]["parent"] != native[1]["parent"]
    rows = {r["id"]: r for r in registry["candidates"]}
    assert rows["original/guarded"]["deployment_identity"] != rows["firmbolt/guarded"]["deployment_identity"]
    assert rows["original/alias"]["equivalent_to"] == "original/guarded"
    assert rows["firmbolt/alias"]["equivalent_to"] == "firmbolt/guarded"


def test_failed_variant_is_reused_as_failure_without_poisoning_other_parameters(registry_builder):
    registry_builder.failures.add("cleanup")
    registry = registry_builder.run(candidates.CORE + ["cleanup", "cleanup+b3", "alias+b3", "guarded"], nodes={
        "cleanup": {"stage": "native", "params": {"host-inline": {"udiv_mode": "cleanup"}}},
        "alias": {"stage": "native", "params": {"host-inline": {"udiv_mode": "cleanup"}}},
        "guarded": {"stage": "native", "params": {"host-inline": {"udiv_mode": "guarded"}}}})
    rows = {r["id"]: r for r in registry["candidates"]}
    assert all(rows["original/" + name]["status"] == "failed" for name in ("cleanup", "cleanup+b3", "alias+b3"))
    assert rows["original/guarded"]["status"] == "ready"
    assert sum(c["stage"] == "native" for c in registry_builder.calls) == 2


def test_named_nodes_preserve_candidate_budget_and_bounded_rebuild(registry_builder):
    nodes = {"guarded": {"stage": "native"}, "alias": {"stage": "native"}}
    with pytest.raises(ValueError, match="budget"):
        registry_builder.run(candidates.CORE + ["guarded", "alias"], nodes=nodes, limit=5)
    assert not registry_builder.calls
    registry = registry_builder.run(["warm", "guarded"], nodes=nodes, fixed_image="original", fixed_recipe="guarded")
    assert [r["id"] for r in registry["candidates"]] == ["original/warm", "original/guarded"]
    assert all(r["status"] == "ready" for r in registry["candidates"])


@pytest.mark.parametrize("nodes", [
    {"native": {"stage": "native"}}, {"warm": {"stage": "native"}},
    {"../escape": {"stage": "native"}}, {"variant+b3": {"stage": "native"}},
    {"variant": {"stage": "other-node"}}, {"variant": {"stage": {}}},
    {"variant": {"stage": "native", "params": []}},
])
def test_invalid_recipe_nodes_rejected_before_materialization(registry_builder, nodes):
    with pytest.raises(ValueError, match="recipe node"):
        registry_builder.run(candidates.CORE, nodes=nodes)
    assert not registry_builder.calls

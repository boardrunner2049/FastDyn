"""Materialize a bounded registry from an admitted application's prepared images.

Run with the scopt Python interpreter. All plan paths are absolute. A generation
is reusable only inside this invocation, for the same image and recipe prefix.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from scopt.formats.bundle_v1 import read_bundle
from scopt.formats.profile_v1 import ProfileView, read_profile
from scopt.formats.sc_v5 import sha256_sparse
from scopt.ids import canonical_json, runtime_identity_id, sha256_id
from scopt.importer.import_cache import ImportRejected, import_snapshot, open_snapshot
from scopt.importer.warm_origin import ordinary_warm
from scopt.manager import load_pipeline
from scopt.report.collect import hotset_metrics
from scopt.run.deploy import resolve_artifact
from scopt.run.launcher import file_sha256, write_json
from scopt.run.pass_policy import PolicyMismatch, check_requested, policy_arguments

B3 = "b3"
CHAINS = ("chain-static", "chain-returns")
PIPELINES = {
    "b3-ph": "b3-ph", "b3": B3, "static": B3, "returns": B3, "general": B3,
    "dominant": "dominant-return", "ph": "b2-layout", "slide": "b2-arena-slide",
    "pc": "exit-pc-fold", "native": "host-inline-splice",
    "scalar": "local-scalar", "global-region": "global-region",
    "native-1": "host-inline-splice", "native-2": "host-inline-splice",
}
PACKAGES = {"sfb", "sfb-hot", "b4", "b4-coalesce", "b4-absorb"}
CORE = ["warm", "b3", "sfb", "sfb+b3"]


def deployment_identity(artifact, config, firmware, runtime, contract_sha256):
    """Hash the logical deployed bytes, including import metadata and restore hints."""
    a = resolve_artifact(artifact)
    root = Path(a["path"])
    if not a["bundle"] or not a["runtime_identity"]:
        raise ValueError("deployment lacks checked bundle/runtime metadata")
    files = [Path(a["bundle"]), Path(a["runtime_identity"])]
    for extra in (root / "cache.sc.scopt.json", root / "cache.sc.restore-hints"):
        if extra.exists():
            files.append(extra)
    return sha256_id(canonical_json({
        "cache": "sha256:" + sha256_sparse(a["sc"]),
        "captures": sorted(file_sha256(path) for path in root.glob("*.scm")),
        "metadata": {path.name: file_sha256(path) for path in files},
        "config": file_sha256(config), "firmware": file_sha256(firmware),
        "qemu": file_sha256(runtime), "contract": contract_sha256,
    }))


def stage_parameters(stage, params):
    if stage not in ("native-1", "native-2"):
        return params
    fixed = {"mode": "splice", "allow_faulting": False, "max_sites_per_unit": int(stage[-1])}
    requested = params.get("host-inline", {})
    if any(key in requested and requested[key] != val for key, val in fixed.items()):
        raise ValueError(f"parameters conflict with {stage}")
    return {**params, "host-inline": {**requested, **fixed}}


def optimizer_args(stage, image, parent, root, params, ancestry, pipeline_dir=None):
    """Build one scopt invocation with an explicit policy and warm ancestor."""
    params = stage_parameters(stage, params)
    if stage in PACKAGES:
        family = stage.split("-")[0]
        args = [family, "package", str(parent), "--store", str(root)]
        if family == "b4":
            args += ["--runtime", image["admission_runtime"], "--enable", "eliminate-state-sync"]
            if stage != "b4":
                args.append("--" + stage.split("-")[1] + "-nops")
        elif stage == "sfb-hot":
            args += ["--profile", image["profile"], "--phase", "steady",
                     "--entry-coverage", str(params["entry_coverage"])]
            for key in ("max_growth_bytes", "max_moved_units"):
                if key in params:
                    args += ["--" + key.replace("_", "-"), str(params[key])]
        return args, [], []
    pipeline = PIPELINES[stage]
    if pipeline_dir is not None:
        pipeline = str(pipeline_dir / (pipeline + ".toml"))
    names = [p.name for p in load_pipeline(pipeline).passes]
    requested = {"static": [CHAINS[0]], "returns": [CHAINS[1]], "general": list(CHAINS)}.get(stage, names)
    args = ["optimize", str(parent), "--store", str(root), "--pipeline", pipeline, "--json",
            "--profile", image["profile"], "--warm-baseline", image["snapshot"], "--prep", image["prep"],
            "--elf", image["firmware"], "--runtime-identity", image["admission_runtime"],
            "--v6-exe", file_sha256(image["runtime"]), *policy_arguments(names, requested)]
    for path in ancestry:
        args += ["--warm-stage", path]
    if params:
        write_json(root / "params.json", params)
        args += ["--params", str(root / "params.json")]
    return args, names, requested


def prepare_image(image):
    snap = open_snapshot(image["snapshot"])
    profile = read_profile(image["profile"])
    ProfileView.open(profile, snap)
    bundle = read_bundle(Path(image["snapshot"]) / "bundle.json")
    if (profile.snapshot_id != snap.snapshot_id or profile.bundle_id != bundle["bundle_id"]
            or bundle["image_hash"] != file_sha256(image["firmware"])):
        raise ValueError("ordinary snapshot, training profile, and firmware disagree")
    origin = ordinary_warm(Path(image["snapshot"]), baseline=Path(image["snapshot"]), prep=Path(image["prep"]))
    if origin is None or origin.path.resolve() != Path(image["snapshot"]).resolve():
        raise ValueError("ordinary warm ancestry was not established")
    return snap, profile


def materialize(stage, previous, image, root, params, remaining, *, optimizer=None, pipeline_dir=None):
    root.mkdir()
    args, names, wanted = optimizer_args(stage, image, previous["snapshot"], root, params, previous["ancestry"], pipeline_dir)
    command = [*(optimizer or [str(Path(sys.executable).parent / "scopt")]), *args]
    write_json(root / "command.json", command)
    with (root / "stdout.json").open("w") as stdout, (root / "stderr.log").open("w") as stderr:
        subprocess.run(command, stdout=stdout, stderr=stderr, check=True, timeout=remaining)
    doc = json.loads((root / "stdout.json").read_text())
    if stage in PACKAGES:
        proposal = doc["proposal"]
        if proposal["outcome"] not in ("candidate", "no_change"):
            raise ValueError(f"{stage} did not run: {proposal}")
        changes = proposal["units"]
        if doc["status"] != "published" and not (doc["status"] == "no_change" and changes == 0):
            raise ValueError(f"{stage}: {doc.get('reason') or doc['status']}")
        effects = [{"pass": stage, "outcome": proposal["outcome"], "units": changes}]
    else:
        if doc.get("notes") or doc["status"] != "published":
            raise ValueError(f"{stage}: {doc.get('notes') or doc.get('reason') or doc['status']}")
        effects = check_requested(doc, wanted, stages=names)
    record = {"stage": stage, "params": stage_parameters(stage, params), "input": previous["snapshot_id"],
              "receipt": str(root / "stdout.json"), "effective_passes": effects}
    path = doc.get("artifact_dir") or doc.get("path")
    if doc["status"] == "no_change":
        return {**previous, "generations": [*previous["generations"], {**record, "output": previous["snapshot_id"]}]}
    artifact = Path(path).resolve(strict=True)
    snap = open_snapshot(previous["snapshot"])
    imported = import_snapshot(sc=str(artifact / "cache.sc"), scms=sorted(artifact.glob("cache*.scm")),
                               manifest=image["manifest"], bundle=snap.bundle, runtime_identity=snap.runtime,
                               store=root / "import")
    ancestry = [*previous["ancestry"], str(artifact)]
    origin = ordinary_warm(imported.store_dir, baseline=Path(image["snapshot"]),
                           prep=Path(image["prep"]), stages=list(map(Path, ancestry)))
    if origin is None or origin.path.resolve() != Path(image["snapshot"]).resolve():
        raise ValueError("materialized artifact lost its ordinary warm ancestor")
    output = imported.snapshot.snapshot_id
    return {"snapshot": str(imported.store_dir), "snapshot_id": output, "artifact": str(artifact),
            "ancestry": ancestry, "generations": [*previous["generations"], {**record, "output": output}]}


def build(plan_path, output, *, optimizer=None, pipeline_dir=None, fixed_image=None, fixed_recipe=None):
    plan = json.loads(plan_path.read_text())
    admission = json.loads(Path(plan["admission"]).read_text())
    if (admission["status"] != "admitted" or admission["application"] != plan["application"]
            or plan["application"] in {"compute", "fwbench", "kernbench", "libbench", "algobench"}):
        raise ValueError("requires a real application's completed admission record")
    recipes = plan.get("recipes", CORE)
    images = plan["images"]
    bounded_rebuild = fixed_recipe is not None and fixed_image is not None and (
        recipes == ["warm"] if fixed_recipe == "warm" else recipes == ["warm", fixed_recipe])
    valid_recipes = set(CORE) <= set(recipes) if fixed_recipe is None else bounded_rebuild
    if (not valid_recipes or len(set(recipes)) != len(recipes)
            or (fixed_image is None and "original" not in images)
            or (fixed_image is not None and set(images) != {fixed_image})
            or not set(images) <= {"original", "firmbolt"}):
        raise ValueError("invalid image variants or recipe list")
    for recipe in recipes:
        if recipe != "warm" and any(s not in PACKAGES | PIPELINES.keys() for s in recipe.split("+")):
            raise ValueError(f"unrecognized recipe {recipe}")
    recipes = ["warm", *(r for r in recipes if r != "warm")]
    budget = plan["budget"]
    if len(images) * len(recipes) > budget["candidate_limit"] or not 0 < budget["offline_seconds"] < float("inf"):
        raise ValueError("search exceeds the declared budget")
    objective = plan["objective"]
    if objective["direction"] not in ("minimize", "maximize") or not all(
            objective[k] for k in ("name", "useful_work", "unit", "boundaries")):
        raise ValueError("incomplete objective")
    output.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    registry = {k: plan[k] for k in ("application", "scenario", "configuration", "objective", "contract", "budget")}
    registry.update(schema="trebuchet.registry/1", state="running", plan_sha256=file_sha256(plan_path),
                    admission={"path": plan["admission"], "sha256": file_sha256(plan["admission"])},
                    contract_sha256=file_sha256(plan["contract"]), candidates=[])
    save = output / "registry.json"
    write_json(save, registry)
    seen = {}
    for variant, image in images.items():
        cache = {}
        error = None
        try:
            snap, profile = prepare_image(image)
            profile_doc = profile.doc
            cache[()] = dict(snapshot=image["snapshot"], snapshot_id=snap.snapshot_id,
                             artifact=image["snapshot"], ancestry=[], generations=[])
        except (ValueError, RuntimeError, ImportRejected, OSError, KeyError) as exc:
            error = str(exc)
        for recipe in recipes:
            stages = [] if recipe == "warm" else recipe.split("+")
            row = dict(id=f"{variant}/{recipe}", application=plan["application"], image_variant=variant,
                       recipe=stages, contract_sha256=registry["contract_sha256"])
            registry["candidates"].append(row)
            try:
                if error:
                    raise ValueError(error)
                row.update({k: image[k] for k in ("firmware", "config", "runtime", "profile")})
                row.update(firmware_sha256=file_sha256(image["firmware"]), config_sha256=file_sha256(image["config"]),
                           profile_id=profile.profile_id, ordinary_snapshot_id=snap.snapshot_id)
                for i, stage in enumerate(stages, 1):
                    prefix = tuple(stages[:i])
                    if prefix not in cache:
                        remaining = budget["offline_seconds"] - (time.monotonic() - start)
                        if remaining <= 0:
                            raise TimeoutError("offline time budget exhausted")
                        parent = cache[prefix[:-1]]
                        if isinstance(parent, Exception):
                            raise ValueError(f"parent generation failed: {parent}")
                        try:
                            cache[prefix] = materialize(stage, parent, image, output / (variant + "." + ".".join(prefix)),
                                                        plan.get("params", {}).get(stage, {}), remaining,
                                                        optimizer=optimizer, pipeline_dir=pipeline_dir)
                        except (OSError, ValueError, RuntimeError, ImportRejected, subprocess.SubprocessError, KeyError) as exc:
                            cache[prefix] = exc
                    if isinstance(cache[prefix], Exception):
                        raise cache[prefix]
                result = cache[tuple(stages)]
                artifact = result["artifact"]
                deployed = resolve_artifact(artifact)
                if read_bundle(deployed["bundle"])["image_hash"] != row["firmware_sha256"]:
                    raise ValueError("published artifact bound to wrong firmware")
                row.update(artifact=artifact, generations=result["generations"],
                           runtime_identity=runtime_identity_id(json.loads(Path(deployed["runtime_identity"]).read_text())))
                identity = deployment_identity(artifact, image["config"], image["firmware"], image["runtime"], row["contract_sha256"])
                row.update(deployment_identity=identity, status="ready")
                if identity in seen:
                    row.update(status="equivalent", equivalent_to=seen[identity])
                else:
                    seen[identity] = row["id"]
                    row["hot_footprint"] = hotset_metrics(profile_doc, deployed["sc"])
            except (TimeoutError, subprocess.TimeoutExpired) as exc:
                row.update(status="incomplete", reason=str(exc))
            except PolicyMismatch as exc:
                row.update(status="unsupported", reason=str(exc))
            except (KeyError, OSError, ValueError, RuntimeError, ImportRejected, subprocess.SubprocessError) as exc:
                row.update(status="failed", reason=str(exc))
            write_json(save, registry)
    registry.update(state="completed", offline_seconds=time.monotonic() - start)
    write_json(save, registry)
    return registry


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("output", type=Path)
    options = parser.parse_args()
    result = build(options.plan, options.output)
    sys.exit(0 if all(row["status"] in ("ready", "equivalent") for row in result["candidates"]) else 1)

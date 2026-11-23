"""``scopt optimize`` and ``scopt validate`` (WP10, design §5.6-5.8, §5.11)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from .select_warm import selection_timing_options


def register(group: click.Group) -> None:
    group.add_command(optimize_cmd)
    group.add_command(validate_cmd)


def _open_snapshot(spec: str, store: str | None):
    from ..importer.import_cache import is_store_snapshot, open_snapshot

    p = Path(spec)
    if not is_store_snapshot(p) and store is not None:
        p = Path(store) / "snapshots" / spec.split(":", 1)[-1]
    if not is_store_snapshot(p):
        raise click.UsageError(f"{spec}: not a store snapshot directory (or id in --store)")
    return open_snapshot(p)


def _splice_generation(snap, stage, store, views, params):
    """Produce and fully reimport the certified native generation consumed by B3."""
    from ..importer.import_cache import ImportRejected, import_snapshot, open_snapshot
    from ..manager import PipelineError
    from ..validate import optimize

    intermediate = Path(store) / "native-generations"
    res = optimize(snap, stage, intermediate, profiles=views, params=params)
    doc = res.to_json()
    doc["passes"] = [p.to_json() for p in res.candidate.passes]
    if not res.published or any(p["outcome"] == "failed" for p in doc["passes"]):
        raise PipelineError(f"native prefix rejected: {res.reason or doc['passes']}")
    cache = Path(res.artifact_dir)
    manifest = Path(snap.path) / "manifest.json"
    try:
        result = import_snapshot(sc=str(cache / "cache.sc"), scms=sorted(cache.glob("cache*.scm")),
                                 manifest=str(manifest) if manifest.is_file() else None,
                                 bundle=snap.bundle, runtime_identity=snap.runtime, store=intermediate)
    except ImportRejected as exc:
        raise PipelineError(f"native generation reimport failed: {exc}") from exc
    imported = open_snapshot(result.store_dir)
    return imported, {"snapshot_dir": str(result.store_dir), "input_snapshot": snap.snapshot_id, "output_snapshot": imported.snapshot_id,
                      "artifact_dir": str(cache), "artifact_id": res.artifact_id,
                      "passes": doc["passes"], "validation": doc["validation"]}


def _profiles(paths, snap):
    from ..formats.profile_v1 import ProfileIncompatible, ProfileView, read_profile

    views, notes = [], []
    for p in paths:
        prof = read_profile(p)
        try:
            views.append(ProfileView.open(prof, snap))
        except ProfileIncompatible as e:
            notes.append(f"profile {prof.profile_id}: incompatible ({e})")
    return views, notes


def _inline_pipeline(pipeline, enable, only):
    from ..manager import load_pipeline, split_host_inline_selector

    options = set()
    enable = tuple(split_host_inline_selector(name, options) for name in enable)
    only = split_host_inline_selector(only, options)
    return load_pipeline(pipeline, inline_options=options), enable, only


def _select_output(doc, original_snapshot, cases_path, out, timing=None):
    doc["warm_baseline"] = str(original_snapshot) if original_snapshot is not None else None
    doc["warm_baseline_sha256"] = (timing or {}).get("expected_warm_sha256")
    if cases_path is None:
        doc.update(selected_artifact=str(original_snapshot) if original_snapshot is not None else None,
                   selection_reason="candidate_unmeasured" if original_snapshot is not None
                   else "warm_baseline_unverified")
        return
    from .select_warm import _cases
    from ..run.warm_selection import select_warm_cache

    try:
        cases, excluded = _cases(cases_path)
        choice = select_warm_cache(original_snapshot, Path(doc["artifact_dir"]), out,
                                   inputs=cases, training_inputs=excluded, **(timing or {}))
    except (KeyError, ValueError, TypeError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    doc.update(selection=str(out.resolve() / "selection.json"), selection_status=choice["status"],
               selected_artifact=choice["selected_artifact"], selection_reason=choice["reason"])


@click.command("optimize")
@click.argument("snapshot")
@click.option("--pipeline", "pipeline", required=True,
              help="Pipeline name or TOML file. Combine host-inline options, e.g. host-inline=faulting,4-level. "
                   "N-level sets the maximum inline depth to a positive integer. Add extreme to "
                   "require full site coverage; maximal exhausts every supported certified "
                   "chain and keeps the proved subset.")
@click.option("--store", required=True, type=click.Path(file_okay=False),
              help="Store: artifacts/ and rejections/ are written here.")
@click.option("--profile", "profiles", multiple=True, type=click.Path(exists=True),
              help="Profile v1 file (profile.json); repeatable.")
@click.option("--disable", multiple=True, help="Ablation: skip this pass (repeatable).")
@click.option("--enable", multiple=True,
              help="Opt a pipeline pass back in (repeatable). Accepts host-inline=<options>.")
@click.option("--only", default=None,
              help="Restrict execution to one pass. Accepts host-inline=<options>. Disabled passes still need --enable.")
@click.option("--params", "params_file", type=click.Path(exists=True, dir_okay=False),
              help='JSON file {"<pass>": {<param>: <value>}} overlaid on the pipeline params.')
@click.option("--b3", "b3_mode", is_flag=True,
              help="B3/B4 mode (design §3.4-3.9, WP21): enable CLONE/REDIRECT_EXIT/ADD_ENTRY/"
                   "ATTACH_FACT/PURE_REWRITE and region-scope passes; package a canonical v7 "
                   "semantic artifact with "
                   "hostsym certification (implied by a pipeline that allows a B3 kind).")
@click.option("--runtime-identity", "ri_file", type=click.Path(exists=True, dir_okay=False),
              help="--b3: RuntimeIdentity the artifact targets (default: the snapshot's).")
@click.option("--v6-exe", "v6_exes", multiple=True,
              help="--b3: extra semantic-extension-capable exe sha256 (legacy option spelling; "
                   "all published artifacts are v7).")
@click.option("--v7-exe", "v7_exes", multiple=True,
              help="--b3: firmware-region-v7-capable exe sha256 (repeatable; separate from the "
                   "semantic-extension capability).")
@click.option("--v7-trusted-exe", "trusted_v7_exes", multiple=True,
              help="--b3: trusted firmware-region-v7 exe sha256 (distinct unsafe ABI).")
@click.option("--selftest", "selftest_file", type=click.Path(dir_okay=False),
              help="--b3: hostsym V18 self-test document (run and written when stale/absent).")
@click.option("--prep", type=click.Path(exists=True, file_okay=False),
              help="--b3: scopt prepare dir (patch-plan content for patch-join).")
@click.option("--elf", type=click.Path(exists=True, dir_okay=False),
              help="--b3: guest firmware ELF (sha256 must equal the bundle image_hash).")
@click.option("--warm-baseline", type=click.Path(path_type=Path, exists=True, file_okay=False),
              help="Ordinary warm store snapshot, including when it is in another store.")
@click.option("--warm-stage", multiple=True, type=click.Path(path_type=Path, exists=True, file_okay=False),
              help="Published intermediate artifact proving a missing reimport edge (repeatable).")
@click.option("--select-with", type=click.Path(path_type=Path, exists=True, dir_okay=False),
              help="After packaging, compare against the original warm snapshot using these timing cases.")
@click.option("--selection-out", type=click.Path(path_type=Path, file_okay=False),
              help="Fresh evidence directory for --select-with.")
@selection_timing_options
@click.option("--json", "as_json", is_flag=True)
def optimize_cmd(snapshot, pipeline, store, profiles, disable, enable, only, params_file, b3_mode,
                 ri_file, v6_exes, v7_exes, trusted_v7_exes, selftest_file, prep, elf, as_json,
                 select_with=None, selection_out=None,
                 qemu=None, pairs=12, seed=193483, timeout=90.0, alpha=0.05, warm_baseline=None, warm_stage=()):
    """Run PIPELINE on SNAPSHOT, link, validate (V1-V9) and publish the artifact.

    SNAPSHOT is a store snapshot directory or a SnapshotId in --store. Exit
    status 0 when an artifact is published, 2 when the candidate is rejected
    (store/rejections/<candidate>.json; nothing published).
    """
    from ..manager import PipelineError
    from ..validate import optimize

    snap = _open_snapshot(snapshot, store)
    if bool(select_with) != bool(selection_out):
        raise click.UsageError("Use --select-with and --selection-out together.")
    timing = dict(qemu=qemu, pairs=pairs, seed=seed, timeout_s=timeout, alpha=alpha)
    from ..formats.artifact import ArtifactError
    from ..importer.warm_origin import ordinary_warm

    try:
        origin = ordinary_warm(Path(getattr(snap, "path", None) or snapshot),
                                          baseline=warm_baseline, prep=prep, stages=warm_stage)
    except (OSError, KeyError, ValueError, ArtifactError) as exc:
        raise click.UsageError(f"Invalid ordinary warm baseline evidence: {exc}") from exc
    original_snapshot = origin.path if origin else None
    if origin:
        timing["expected_warm_sha256"] = origin.cache_sha256
    if origin is None and (warm_baseline or warm_stage or select_with):
        raise click.UsageError("Cannot establish the original warm baseline. Supply --warm-baseline "
                               "and its --prep directory for older imports, and --warm-stage for "
                               "any missing intermediate artifacts.")
    views, notes = _profiles(profiles, snap)
    params = json.loads(Path(params_file).read_text()) if params_file else None
    from ..passes.base import B3_EDIT_KINDS, NATIVE_EDIT_KINDS, EditKind

    try:
        pl, enable, only = _inline_pipeline(pipeline, enable, only)
        enable, disable, only, params = pl.resolve_pass_options(enable, disable, only, params)
        pl.validate_pass_options(enable, disable, only)
    except PipelineError as e:
        raise click.UsageError(str(e)) from e
    native_stage = None
    from ..manager import HOST_INLINE_ALIASES
    native_names = {"host-inline", *HOST_INLINE_ALIASES}
    if (pl.passes and pl.passes[0].name in native_names
            and pl.allowed_capabilities & B3_EDIT_KINDS):
        from dataclasses import replace
        from ..passes.host_inline import host_inline_mode

        splice, *rest = pl.passes
        if not rest or any(p.name in native_names for p in rest):
            raise click.UsageError("one native prefix followed by B3 passes is required")
        stage_params = {splice.name: params[splice.name]} if params and splice.name in params else {}
        standalone = replace(pl, name=splice.name, passes=(splice,),
                             allowed_capabilities=pl.allowed_capabilities & {EditKind.INLINE_CALL})
        if only == splice.name:
            pl = standalone
            params, b3_mode = stage_params, False
            disable = tuple(name for name in disable if name == splice.name)
            enable = tuple(name for name in enable if name == splice.name)
        else:
            if only is None and splice.name not in disable:
                if (splice.name == "host-inline" and
                        host_inline_mode({**splice.params, **stage_params.get(splice.name, {})}) == "island"):
                    raise click.UsageError("native_combination_unsupported: host-inline island mode "
                                           "requires a standalone pipeline; choose a splice mode for B3")
                try:
                    snap, native_stage = _splice_generation(snap, standalone, store, views, stage_params)
                except PipelineError as exc:
                    raise click.UsageError(str(exc)) from exc
                views, stage_notes = _profiles(profiles, snap)
                notes.extend(stage_notes)
            pl = replace(pl, passes=tuple(rest),
                         allowed_capabilities=pl.allowed_capabilities - {EditKind.INLINE_CALL})
            disable = tuple(name for name in disable if name != splice.name)
            enable = tuple(name for name in enable if name != splice.name)
            params = {k: v for k, v in (params or {}).items() if k != splice.name}
    native = bool(pl.allowed_capabilities & NATIVE_EDIT_KINDS) or any(
        unit.inline_start is not None for unit in snap.units())
    layout_caps = {EditKind.PIN, EditKind.LAYOUT_ORDER, EditKind.PLACE}
    if native and (b3_mode or pl.allowed_capabilities & (B3_EDIT_KINDS | layout_caps)):
        raise click.UsageError("native_combination_unsupported: native islands need a standalone pipeline")
    if b3_mode or (pl.allowed_capabilities & B3_EDIT_KINDS):
        _optimize_b3(snap, pl, store, views, notes, params, disable, only, ri_file,
                     v6_exes, v7_exes, trusted_v7_exes,
                     selftest_file, prep, elf, as_json, native_stage=native_stage,
                     selection=(original_snapshot, select_with, selection_out, timing), enable=enable)
        return
    try:
        res = optimize(snap, pl, store, profiles=views, params=params,
                       disable=disable, only=only, enable=enable)
    except PipelineError as e:
        raise click.UsageError(str(e)) from e
    doc = res.to_json()
    doc["passes"] = [p.to_json() for p in res.candidate.passes]
    doc["notes"] = notes
    if res.published:
        _select_output(doc, original_snapshot, select_with, selection_out, timing)
    if as_json:
        click.echo(json.dumps(doc, indent=2, sort_keys=True))
    else:
        for n in notes:
            click.echo(n)
        c = res.candidate
        for p in c.passes:
            click.echo(f"pass {p.name}@{p.version}: {p.outcome.value} ({p.reason})")
        if res.link is not None:
            lk = res.link
            click.echo(f"link: {'ok' if lk.ok else 'FAILED'}; moved "
                       f"{sum(1 for u in lk.units if u.moved)}/{len(lk.units)}; "
                       f"rollbacks {len(lk.rollbacks)}")
        if res.validation is not None:
            click.echo("validation: " + " ".join(f"{k}={v}" for k, v in
                                                 res.validation.codes().items()))
        if res.published:
            click.echo(f"artifact {res.artifact_id} ({res.publish_status})")
            click.echo(f"  {res.artifact_dir}")
            click.echo(f"  output snapshot {res.output_snapshot_id}")
        else:
            click.echo(f"REJECTED: {res.reason}")
            click.echo(f"  {res.rejection_path}")
    if doc.get("selection_reason") and not as_json:
        click.echo(f"deployment selection: {doc['selected_artifact'] or 'none'} ({doc['selection_reason']})")
    sys.exit(0 if res.published and doc.get("selection_status") != "error" else 2)


def _optimize_b3(snap, pl, store, views, notes, params, disable, only, ri_file,
                 v6_exes, v7_exes, trusted_v7_exes,
                 selftest_file, prep, elf, as_json, native_stage=None, selection=None, enable=()):
    from ..analysis.b3_inputs import resolve_for_snapshot
    from ..manager import PipelineError
    from ..manager_b3 import B3Env, optimize_b3
    from ..validate_sym.selftest import load_or_run

    try:
        resolve_for_snapshot(snap, elf=elf, prep=prep)
    except Exception as e:  # noqa: BLE001 - a mismatching input is a usage error
        raise click.UsageError(f"{getattr(e, 'code', type(e).__name__)}: {e}") from e
    ri = json.loads(Path(ri_file).read_text()) if ri_file else None
    if not selftest_file:  # cached per store; rerun when the kernel or capstone changed
        Path(store).mkdir(parents=True, exist_ok=True)
        selftest_file = str(Path(store) / "hostsym_selftest.json")
    env = B3Env(runtime=ri, v6_exes=tuple(v6_exes), v7_exes=tuple(v7_exes),
                trusted_v7_exes=tuple(trusted_v7_exes),
                selftest=load_or_run(selftest_file))
    try:
        r = optimize_b3(snap, pl, store, env=env, profiles=views, params=params,
                        disable=disable, only=only, enable=enable)
    except PipelineError as e:
        raise click.UsageError(str(e)) from e
    c, pk, m = r["candidate"], r["package"], r["materialized"]
    doc = {"path": r["path"], "outcome": r["outcome"], "status": pk.status, "reason": pk.reason,
           "artifact_id": getattr(pk, "artifact_id", None),
           "artifact_dir": getattr(pk, "artifact_dir", None),
           "publish_status": getattr(pk, "publish_status", None),
           "rejection_path": getattr(pk, "rejection_path", None),
           "passes": [p.to_json() for p in c.passes], "notes": notes,
           "downgraded": None if m is None else m.downgraded,
           "pass_byte_delta": None if m is None else m.pass_bytes,
           "validation": None if pk.validation is None else pk.validation.codes()}
    if native_stage:
        doc["native_stage"] = native_stage
        doc["passes"] = [*native_stage["passes"], *doc["passes"]]
    if selection and pk.status == "published":
        _select_output(doc, *selection)
    if as_json:
        click.echo(json.dumps(doc, indent=2, sort_keys=True, default=str))
    else:
        for n in notes:
            click.echo(n)
        for p in c.passes:
            click.echo(f"pass {p.name}@{p.version}: {p.outcome.value} ({p.reason})")
        if m is not None:
            click.echo(f"plan: {len(m.plan.grow)} grown units, {len(m.plan.variants)} variants, "
                       f"{len(m.downgraded)} downgraded (no_simplification); bytes per pass "
                       f"{m.pass_bytes}")
        if doc["validation"]:
            click.echo("validation: " + " ".join(f"{k}={v}" for k, v in doc["validation"].items()))
        if pk.status == "published":
            click.echo(f"artifact {doc['artifact_id']} ({doc['publish_status']})\n"
                       f"  {doc['artifact_dir']}")
        else:
            click.echo(f"REJECTED ({r['outcome']}): {pk.reason}\n  {doc['rejection_path']}")
    if doc.get("selection_reason") and not as_json:
        click.echo(f"deployment selection: {doc['selected_artifact'] or 'none'} ({doc['selection_reason']})")
    sys.exit(0 if pk.status == "published" and doc.get("selection_status") != "error" else 2)


@click.command("validate")
@click.argument("artifact", type=click.Path(exists=True, file_okay=False))
@click.option("--snapshot", "snapshot", default=None,
              help="Input snapshot directory (default: <store>/snapshots/<input id> next to "
                   "the artifact's store).")
@click.option("--dynamic", is_flag=True, help="V10: deploy and run (WP8 harness).")
@click.option("-c", "--config", "configs", multiple=True,
              type=click.Path(exists=True, dir_okay=False),
              help="--dynamic: base FastDyn TOML; repeat for several held-out inputs.")
@click.option("--out", "out_dir", type=click.Path(file_okay=False),
              help="--dynamic: directory for the run directories and dynamic.json.")
@click.option("--reps", type=int, default=1, show_default=True)
@click.option("--qemu", type=click.Path(exists=True, dir_okay=False),
              help="--dynamic: QEMU binary (default: the RuntimeIdentity's executable).")
@click.option("--baseline", type=click.Path(exists=True, file_okay=False),
              help="--dynamic: baseline artifact/snapshot dir (default: the input snapshot).")
@click.option("--inputs", "inputs", multiple=True, type=click.Path(exists=True, dir_okay=False),
              help="--dynamic: held-out input files for the input RAM bank (one pair each).")
@click.option("--input-bank", default="input", show_default=True)
@click.option("--timeout", "timeout_s", type=float, default=300.0, show_default=True)
@click.option("--oracles", default=None,
              help="--dynamic: B3/B4 oracles to evaluate, comma separated, from O1,O2a,O2b,"
                   "O3,O5,O6 (design §9.4; WP26). Without it --dynamic runs V10 as before.")
@click.option("--mode", type=click.Choice(["icount", "noicount"]), default=None,
              help="--oracles: O2b mode (default: from the TOML's icount key).")
@click.option("--fixture-expected", type=click.Path(exists=True, dir_okay=False),
              help="--oracles: fixtures/b3/expected.py (per-mode fixture oracle).")
@click.option("--fixture-input", "fixture_inputs", multiple=True,
              help="--oracles: fixture input name per --inputs entry (e.g. 7, 11-heldout).")
@click.option("--o3", "o3_json", default=None,
              help='--oracles: O3 requirements as JSON: {"stats": {counter: ">0"}, '
                   '"hits": {probe label: ">0"}}.')
@click.option("--hit-target", "hit_targets", multiple=True,
              help="--oracles: restrict the gdb hit counters to these probe labels.")
@click.option("--negative-control", "negative_control", is_flag=True,
              help="--oracles: ARTIFACT is a negative control: O4 mode (deploy admits "
                   "it; exit 0 only when the mutant is detected in every case).")
@click.option("--test-policy", "test_policies", multiple=True,
              help="--oracles: admit an artifact built under this test policy.")
@click.option("--json", "as_json", is_flag=True)
def validate_cmd(artifact, snapshot, dynamic, configs, out_dir, reps, qemu, baseline,
                 inputs, input_bank, timeout_s, oracles, mode, fixture_expected,
                 fixture_inputs, o3_json, hit_targets, negative_control, test_policies,
                 as_json):
    """Re-validate a published ARTIFACT (V1-V9, MANIFEST), or with --dynamic run V10.

    With --dynamic --oracles O1,O2b,... the B3/B4 dynamic oracles (design §9.4)
    run instead, for canonical v7 artifacts (scopt.run.oracles.dynamic_oracles);
    their verdict is pass, fail or incomplete (a requested oracle not run).
    With --negative-control the verdict is O4's (pass = mutant detected).
    Exit status 0 when every check passes, 1 otherwise (incomplete included),
    2 when refused (an artifact on a QEMU lacking its required v7 features; without
    --dynamic, an artifact the static V1-V9 check does not apply to: B3, B4
    and icount-entry artifacts, which `scopt publish` re-checks).
    """
    from ..formats.artifact import id_hex, read_artifact
    from ..validate import validate_artifact, validate_dynamic

    if dynamic and oracles:
        from ..run.oracles import OracleRefused, dynamic_oracles

        if not configs or not out_dir:
            raise click.UsageError("--dynamic needs -c BASE.toml and --out DIR")
        try:
            doc = dynamic_oracles(artifact, configs, out_dir, oracles=oracles.split(","),
                                  baseline=baseline, reps=reps, qemu=qemu, timeout_s=timeout_s,
                                  inputs=inputs, input_bank=input_bank, mode=mode,
                                  fixture_expected=fixture_expected,
                                  fixture_inputs=list(fixture_inputs) or None,
                                  o3_requirements=json.loads(o3_json) if o3_json else None,
                                  hit_targets=list(hit_targets) or None,
                                  allow_negative_control=negative_control,
                                  test_policies=test_policies)
        except OracleRefused as e:
            click.echo(f"REFUSED: {e.code}: {e.detail}", err=True)
            sys.exit(2)
        if as_json:
            click.echo(json.dumps(doc, indent=2, sort_keys=True, default=str))
        elif negative_control:
            what = {"pass": "mutant detected", "fail": "mutant NOT detected",
                    "incomplete": "unknown (control not proved to run)"}[doc["verdict"]]
            click.echo(f"negative control O4 {doc['verdict']} ({what}): "
                       f"{len(doc['cases'])} cases")
            for c in doc["cases"]:
                for r in c["rows"]:
                    click.echo(f"  {r['oracle']} {r['verdict']}: {r['detail'][:160]}")
        else:
            click.echo(f"oracles {doc['verdict']}: {len(doc['cases'])} cases")
            for c in doc["cases"]:
                for r in c["rows"]:
                    click.echo(f"  {r['oracle']} {r['verdict']}: {r['detail'][:160]}")
        sys.exit(0 if doc["verdict"] == "pass" else 1)
    if dynamic:
        if not configs or not out_dir:
            raise click.UsageError("--dynamic needs -c BASE.toml and --out DIR")
        doc = validate_dynamic(artifact, configs, out_dir, baseline=baseline, reps=reps,
                               qemu=qemu, timeout_s=timeout_s, inputs=inputs,
                               input_bank=input_bank)
        if as_json:
            click.echo(json.dumps(doc, indent=2, sort_keys=True))
        else:
            click.echo(f"V10 {doc['verdict']}: {len(doc['runs'])} pairs; appended "
                       f"{doc['appended']}")
            for f in doc["failures"]:
                click.echo(f"  {f}")
        sys.exit(0 if doc["verdict"] == "pass" else 1)
    from ..formats.sc_v5 import ScFormatError
    from ..validate import static_not_applicable

    info = read_artifact(artifact)
    why = static_not_applicable(info)
    if why is not None:
        click.echo(f"REFUSED: static_not_applicable: {why}. Re-check this artifact with "
                   "`scopt publish` and `scopt validate --dynamic`.", err=True)
        sys.exit(2)
    if snapshot is None:
        snapshot = str(Path(artifact).resolve().parent.parent / "snapshots" /
                       id_hex(info.input_snapshot_id))
    snap = _open_snapshot(snapshot, None)
    try:
        doc = validate_artifact(artifact, snap)
    except ScFormatError as e:
        click.echo(f"REFUSED: unreadable_sc: {e}", err=True)
        sys.exit(2)
    if as_json:
        click.echo(json.dumps(doc, indent=2, sort_keys=True))
    else:
        click.echo(f"{doc['verdict']}: " + " ".join(f"{k}={v}" for k, v in doc["codes"].items()))
        for r in doc["rows"]:
            if r["result"] == "fail":
                click.echo(f"  FAIL {r['code']} {r['check']}: {r['detail']}")
        click.echo(f"stored validation.json agrees: {doc['stored_codes_agree']}")
    sys.exit(0 if doc["verdict"] == "accepted" else 1)

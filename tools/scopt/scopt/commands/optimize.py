"""``scopt optimize`` and ``scopt validate`` (WP10, design §5.6-5.8, §5.11)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click


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


@click.command("optimize")
@click.argument("snapshot")
@click.option("--pipeline", "pipeline", required=True,
              help="Pipeline name (identity, b2-layout) or a pipeline TOML file.")
@click.option("--store", required=True, type=click.Path(file_okay=False),
              help="Store: artifacts/ and rejections/ are written here.")
@click.option("--profile", "profiles", multiple=True, type=click.Path(exists=True),
              help="Profile v1 file (profile.json); repeatable.")
@click.option("--disable", multiple=True, help="Ablation: skip this pass (repeatable).")
@click.option("--only", default=None, help="Ablation: run only this pass.")
@click.option("--params", "params_file", type=click.Path(exists=True, dir_okay=False),
              help='JSON file {"<pass>": {<param>: <value>}} overlaid on the pipeline params.')
@click.option("--b3", "b3_mode", is_flag=True,
              help="B3/B4 mode (design §3.4-3.9, WP21): enable CLONE/REDIRECT_EXIT/ADD_ENTRY/"
                   "ATTACH_FACT/PURE_REWRITE and region-scope passes; package as .sc v6 with "
                   "hostsym certification (implied by a pipeline that allows a B3 kind).")
@click.option("--runtime-identity", "ri_file", type=click.Path(exists=True, dir_okay=False),
              help="--b3: RuntimeIdentity the artifact targets (default: the snapshot's).")
@click.option("--v6-exe", "v6_exes", multiple=True,
              help="--b3: extra v6-capable exe sha256 (V7; default: runs/wp22/gate-binary.json).")
@click.option("--selftest", "selftest_file", type=click.Path(dir_okay=False),
              help="--b3: hostsym V18 self-test document (run and written when stale/absent).")
@click.option("--prep", type=click.Path(exists=True, file_okay=False),
              help="--b3: scopt prepare dir (patch-plan content for patch-join).")
@click.option("--elf", type=click.Path(exists=True, dir_okay=False),
              help="--b3: guest firmware ELF (sha256 must equal the bundle image_hash).")
@click.option("--json", "as_json", is_flag=True)
def optimize_cmd(snapshot, pipeline, store, profiles, disable, only, params_file, b3_mode,
                 ri_file, v6_exes, selftest_file, prep, elf, as_json):
    """Run PIPELINE on SNAPSHOT, link, validate (V1-V9) and publish the artifact.

    SNAPSHOT is a store snapshot directory or a SnapshotId in --store. Exit
    status 0 when an artifact is published, 2 when the candidate is rejected
    (store/rejections/<candidate>.json; nothing published).
    """
    from ..manager import PipelineError
    from ..validate import optimize

    snap = _open_snapshot(snapshot, store)
    views, notes = _profiles(profiles, snap)
    params = json.loads(Path(params_file).read_text()) if params_file else None
    from ..manager import load_pipeline
    from ..passes.base import B3_EDIT_KINDS, NATIVE_EDIT_KINDS, EditKind

    try:
        pl = load_pipeline(pipeline)
    except PipelineError as e:
        raise click.UsageError(str(e)) from e
    native = bool(pl.allowed_capabilities & NATIVE_EDIT_KINDS) or any(
        unit.inline_start is not None or unit.splice_applied for unit in snap.units())
    layout_caps = {EditKind.PIN, EditKind.LAYOUT_ORDER, EditKind.PLACE}
    if native and (b3_mode or pl.allowed_capabilities & (B3_EDIT_KINDS | layout_caps)):
        raise click.UsageError("native_combination_unsupported: native islands need a standalone pipeline")
    if b3_mode or (pl.allowed_capabilities & B3_EDIT_KINDS):
        _optimize_b3(snap, pl, store, views, notes, params, disable, only, ri_file, v6_exes,
                     selftest_file, prep, elf, as_json)
        return
    try:
        res = optimize(snap, pipeline, store, profiles=views, params=params,
                       disable=disable, only=only)
    except PipelineError as e:
        raise click.UsageError(str(e)) from e
    doc = res.to_json()
    doc["notes"] = notes
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
    sys.exit(0 if res.published else 2)


def _optimize_b3(snap, pl, store, views, notes, params, disable, only, ri_file, v6_exes,
                 selftest_file, prep, elf, as_json):
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
    env = B3Env(runtime=ri, v6_exes=tuple(v6_exes), selftest=load_or_run(selftest_file))
    try:
        r = optimize_b3(snap, pl, store, env=env, profiles=views, params=params,
                        disable=disable, only=only)
    except PipelineError as e:
        raise click.UsageError(str(e)) from e
    c, pk, m = r["candidate"], r["package"], r["materialized"]
    doc = {"path": r["path"], "outcome": r["outcome"], "status": pk.status, "reason": pk.reason,
           "artifact_id": getattr(pk, "artifact_id", None),
           "artifact_dir": getattr(pk, "artifact_dir", None),
           "rejection_path": getattr(pk, "rejection_path", None),
           "passes": [p.to_json() for p in c.passes], "notes": notes,
           "downgraded": None if m is None else m.downgraded,
           "pass_byte_delta": None if m is None else m.pass_bytes,
           "validation": None if pk.validation is None else pk.validation.codes()}
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
            click.echo(f"artifact {doc['artifact_id']}\n  {doc['artifact_dir']}")
        else:
            click.echo(f"REJECTED ({r['outcome']}): {pk.reason}\n  {doc['rejection_path']}")
    sys.exit(0 if pk.status == "published" else 2)


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
    run instead, for v5 and v6 artifacts (scopt.run.oracles.dynamic_oracles);
    their verdict is pass, fail or incomplete (a requested oracle not run).
    With --negative-control the verdict is O4's (pass = mutant detected).
    Exit status 0 when every check passes, 1 otherwise (incomplete included),
    2 when refused (a v6 artifact on a QEMU that cannot load v6; without
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

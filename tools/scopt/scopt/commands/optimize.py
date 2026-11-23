"""``scopt optimize``, ``scopt load`` and optional ``scopt audit``."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from .select_warm import selection_timing_options


def register(group: click.Group) -> None:
    group.add_command(optimize_cmd)
    group.add_command(load_cmd)
    group.add_command(validate_cmd, name="audit")
    group.add_command(validate_cmd, name="validate")  # deprecated explicit-audit alias


@click.command("load")
@click.argument("artifact", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--runtime-identity", "runtime_file",
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="RuntimeIdentity to check against the cache header.")
@click.option("--trusted", is_flag=True, help="Trust stored public record checksums.")
@click.option("--json", "as_json", is_flag=True)
def load_cmd(artifact: Path, runtime_file: Path | None, as_json: bool, trusted: bool) -> None:
    """Check only loader-required format, bounds, and runtime compatibility."""
    from contextlib import nullcontext
    from ..canon import hexaddr
    from ..formats.artifact import SIDECAR
    from ..formats.sc_v5 import read_sc, structural_findings, trusted_record_input

    cache = artifact / "cache.sc"
    if not cache.is_file():
        raise click.ClickException("artifact has no cache.sc")
    with trusted_record_input() if trusted else nullcontext():
        parsed = read_sc(cache, versions=(7,))
    problems = list(parsed.header_problems)
    problems.extend(f"slot {row.slot}: {row.reason}" for row in parsed.skipped[:8])
    problems.extend(f"{row.code}: {row.detail}" for row in
                    structural_findings(parsed, scan_zeros=False)
                    if row.code not in parsed.header_problems)
    runtime = json.loads(runtime_file.read_text()) if runtime_file else None
    if runtime is not None:
        for key in ("runtime_signature", "code_address", "code_size", "prologue_end"):
            expected = runtime.get(key)
            actual = hexaddr(getattr(parsed.header, key))
            if expected is not None and expected != actual:
                problems.append(f"runtime mismatch: {key} {expected} != {actual}")
        for key in ("tb_size", "host_page_size", "target_page_bits"):
            expected = runtime.get(key)
            actual = getattr(parsed.header, key)
            if expected is not None and expected != actual:
                problems.append(f"runtime mismatch: {key} {expected} != {actual}")
    sidecar = None
    if (artifact / SIDECAR).is_file():
        sidecar = json.loads((artifact / SIDECAR).read_text())
    artifact_runtime = None
    if (artifact / "runtime_identity.json").is_file():
        artifact_runtime = json.loads((artifact / "runtime_identity.json").read_text())
    if runtime is not None:
        if sidecar is not None and sidecar.get("runtime_identity_id") not in (
                None, runtime.get("runtime_identity_id")):
            problems.append("runtime mismatch: runtime_identity_id")
        if artifact_runtime is not None:
            for key in ("runtime_identity_id", "qemu_sha256", "plugin_sha256",
                        "normalized_config_sha256", "cpuinfo", "supercache_flat"):
                expected = runtime.get(key)
                actual = artifact_runtime.get(key)
                if expected is not None and actual is not None and expected != actual:
                    problems.append(f"runtime mismatch: {key} {expected} != {actual}")
    doc = {"loadable": not problems, "problems": problems, "user_trusted_input": trusted,
           "version": parsed.header.version, "units": len(parsed.valid_slots),
           "max_end": parsed.max_end,
           "assurance": None if sidecar is None else sidecar.get("x_assurance"),
           "audited": None if sidecar is None else sidecar.get("x_audited")}
    if as_json:
        click.echo(json.dumps(doc, indent=2, sort_keys=True))
    else:
        click.echo(("loadable" if not problems else "REFUSED") +
                   f": v{doc['version']}, {doc['units']} units, max_end={doc['max_end']:#x}")
        for problem in problems:
            click.echo(f"  {problem}", err=True)
    if problems:
        raise click.exceptions.Exit(2)


def _open_snapshot(spec: str, store: str | None, *, audit: bool = False):
    from ..importer.import_cache import (is_store_snapshot, open_compiler_snapshot,
                                        open_snapshot)

    p = Path(spec)
    if not is_store_snapshot(p) and store is not None:
        p = Path(store) / "snapshots" / spec.split(":", 1)[-1]
    if not is_store_snapshot(p):
        raise click.UsageError(f"{spec}: not a store snapshot directory (or id in --store)")
    return open_snapshot(p) if audit else open_compiler_snapshot(p)


def _splice_generation(snap, stage, store, views, params):
    """Compile the native prefix and pass its in-memory IR to the next stage."""
    from ..manager import PipelineError
    from ..validate import optimize

    intermediate = Path(store) / "native-generations"
    res = optimize(snap, stage, intermediate, profiles=views, params=params)
    doc = res.to_json()
    doc["passes"] = [p.to_json() for p in res.candidate.passes]
    if not res.published or any(p["outcome"] == "failed" for p in doc["passes"]):
        raise PipelineError(f"native prefix rejected: {res.reason or doc['passes']}")
    if res.output_snapshot is None:
        raise PipelineError("native generation did not return compiler IR")
    compiled = res.output_snapshot
    return compiled, {"snapshot_dir": None, "input_snapshot": snap.snapshot_id,
                      "output_snapshot": compiled.snapshot_id,
                      "artifact_dir": str(res.artifact_dir), "artifact_id": res.artifact_id,
                      "assurance": "compiler-produced/user-vouched", "audited": False,
                      "passes": doc["passes"], "validation": doc["validation"]}


def _helper_prefix_profiles(views, source, compiled):
    """Carry training counts across this invocation's host-only helper splice."""
    from ..formats.profile_v1 import ProfileView
    from ..formats.region_v1 import unit_exits

    if not views:
        return []
    if source.unit_ids() != compiled.unit_ids():
        raise click.UsageError("helper prefix changed the captured unit set or slot order")
    def transfers(unit):
        return [(e.id, e.kind, e.slot, e.static_dest_vaddr) for e in unit_exits(unit)]

    for old, new in zip(source.units(), compiled.units(), strict=True):
        if old.key != new.key or old.vaddrs != new.vaddrs:
            raise click.UsageError(f"helper prefix changed guest translation identity for {old.uid}")
        if new.splice_applied and transfers(old) != transfers(new):
            raise click.UsageError(f"helper prefix changed guest transfers for {old.uid}")
    return [ProfileView.open(view.profile, compiled, kind="helper_splice",
                             input_snapshot_ids=(source.snapshot_id,)) for view in views]


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


def _user_trusted_pipeline(pipeline, params, disable):
    return any(stage.name == "firmware-inline" and stage.name not in disable
               and (params or {}).get(stage.name, {}).get("trust", stage.params.get("trust")) == "user"
               for stage in pipeline.passes)


def _automatic_loop_pgo(snap, store, launch, prep, views, options, *, helpers, trusted=False):
    import shutil
    import subprocess
    import tempfile
    from ..canon import sha256_id
    from ..formats.profile_v1 import ProfileView, ingest, write_profile
    from ..run.helper_pgo import collect, collect_edges

    store = Path(store).resolve()
    store.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="helper-pgo-", dir=store))
    if launch is None:
        if not prep:
            raise click.UsageError("hot-loop-inline needs a training --helper-pgo-launch or --prep")
        launch = Path(prep) / "launch.json"
        if not launch.is_file():
            config = Path(prep) / "run.toml"
            fastdyn = shutil.which("fastdyn")
            if not config.is_file() or not fastdyn:
                raise click.UsageError("provide --helper-pgo-launch or a prep/run.toml and fastdyn on PATH")
            command = [fastdyn, "compile-launch", "-c", str(config), "-o", str(work / "baseline")]
            status = subprocess.run(command, stdout=sys.stderr).returncode
            if status:
                raise ValueError(f"training launch compilation exited {status}")
            launch = work / "baseline" / "launch.json"
    source = Path(snap.path)
    counts = work / "calls.json"
    edges = None if views else work / "training.qprof"
    if helpers:
        collect(source, Path(launch), counts, edge_profile_path=edges, trusted=trusted)
        options.update(site_profile_path=str(counts), site_profile_sha256=sha256_id(counts.read_bytes()))
    elif edges:
        collect_edges(source, Path(launch), edges, trusted=trusted)
    if edges:
        meta = json.loads((source / "snapshot.json").read_bytes())
        profile = ingest(edges, source / "cache.sc", source, bundle_id=meta["bundle_id"],
                         contract_id=meta["contract_id"], runtime_identity=snap.runtime)
        write_profile(work / "training-profile.json", profile)
        views.append(ProfileView.open(profile, snap))
        # An unmarked replay occupies QEMU's phase zero, named "boot", through exit.
        options.setdefault("phase", "boot")


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
@click.option("--trusted", "trust_input", is_flag=True,
              help="Use trusted-development record reads during compilation and training.")
@click.option("--profile", "profiles", multiple=True, type=click.Path(exists=True),
              help="Profile v1 file (profile.json); repeatable.")
@click.option("--pgo-launch", "--helper-pgo-launch", "helper_pgo_launch", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Ordinary compiled training launch.json for automatic hot-loop profiling.")
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
                   "semantic artifact using compiler/linker checks only (implied by a pipeline "
                   "that allows a B3 kind).")
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
              help="Deprecated optimize option; proof self-tests belong to explicit scopt audit.")
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
                 qemu=None, pairs=12, seed=193483, timeout=90.0, alpha=0.05, warm_baseline=None, warm_stage=(),
                 helper_pgo_launch=None, trust_input=False):
    """Run PIPELINE on SNAPSHOT, link, check loadability, and publish the artifact.

    SNAPSHOT is a store snapshot directory or a SnapshotId in --store. Exit
    status 0 when an artifact is published, 2 when the candidate is rejected
    (store/rejections/<candidate>.json; nothing published).
    """
    from ..manager import PipelineError
    from ..passes.base import B3_EDIT_KINDS, NATIVE_EDIT_KINDS, EditKind
    from ..validate import optimize

    snap = _open_snapshot(snapshot, store)
    params = json.loads(Path(params_file).read_text()) if params_file else None
    try:
        pl, enable, only = _inline_pipeline(pipeline, enable, only)
        enable, disable, only, params = pl.resolve_pass_options(enable, disable, only, params)
        pl.validate_pass_options(enable, disable, only)
    except PipelineError as e:
        raise click.UsageError(str(e)) from e
    trust_input = trust_input or _user_trusted_pipeline(pl, params, disable)
    if trust_input:
        from ..formats.sc_v5 import trusted_record_input
        click.get_current_context().with_resource(trusted_record_input())
    if bool(select_with) != bool(selection_out):
        raise click.UsageError("Use --select-with and --selection-out together.")
    timing = dict(qemu=qemu, pairs=pairs, seed=seed, timeout_s=timeout, alpha=alpha)
    origin = None
    if warm_baseline or warm_stage or select_with:
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
    auto_loop_phase = None
    for hot in pl.passes:
        policy = {**hot.params, **(params or {}).get(hot.name, {})}
        helpers = hot.name == "host-inline" and policy.get("hot_loop_only")
        firmware = hot.name == "firmware-inline" and policy.get("mode") == "hot-loop"
        if not (helpers or firmware) or hot.name in disable or only not in (None, hot.name):
            continue
        if notes:
            raise click.UsageError("; ".join(notes))
        if firmware and views:
            if auto_loop_phase is not None:
                params = dict(params or {})
                overrides = dict(params.get(hot.name, {}))
                overrides.setdefault("phase", auto_loop_phase)
                params[hot.name] = overrides
            continue
        params = dict(params or {})
        overrides = dict(params.get(hot.name, {}))
        collecting_edges = not views
        try:
            _automatic_loop_pgo(snap, store, helper_pgo_launch, prep, views, overrides,
                                helpers=helpers, trusted=trust_input)
        except (OSError, ValueError, RuntimeError) as exc:
            raise click.ClickException(f"training replay failed: {exc}") from exc
        params[hot.name] = overrides
        if collecting_edges:
            auto_loop_phase = overrides["phase"]
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
                    source = snap
                    snap, native_stage = _splice_generation(source, standalone, store, views, stage_params)
                except PipelineError as exc:
                    raise click.UsageError(str(exc)) from exc
                if any(p.name == "firmware-inline" and
                       (params or {}).get(p.name, {}).get("mode", p.params.get("mode")) == "hot-loop"
                       for p in rest):
                    views = _helper_prefix_profiles(views, source, snap)
                else:
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

    try:
        resolve_for_snapshot(snap, elf=elf, prep=prep)
    except Exception as e:  # noqa: BLE001 - a mismatching input is a usage error
        raise click.UsageError(f"{getattr(e, 'code', type(e).__name__)}: {e}") from e
    ri = json.loads(Path(ri_file).read_text()) if ri_file else None
    env = B3Env(runtime=ri, v6_exes=tuple(v6_exes), v7_exes=tuple(v7_exes),
                trusted_v7_exes=tuple(trusted_v7_exes),
                selftest=None)
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


@click.command("audit")
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
    """Explicitly audit a published ARTIFACT, or with --dynamic run V10.

    With --dynamic --oracles O1,O2b,... the B3/B4 dynamic oracles (design §9.4)
    run instead, for canonical v7 artifacts (scopt.run.oracles.dynamic_oracles);
    their verdict is pass, fail or incomplete (a requested oracle not run).
    With --negative-control the verdict is O4's (pass = mutant detected).
    Exit status 0 when every check passes, 1 otherwise (incomplete included),
    2 when refused (an artifact on a QEMU lacking its required v7 features; without
    --dynamic, an artifact the static V1-V9 check does not apply to: B3, B4
    and icount-entry artifacts, whose specialized audit inputs are separate).
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
        click.echo(f"REFUSED: static_not_applicable: {why}. This legacy static auditor "
                   "does not reconstruct specialized B3/firmware-region plans; use "
                   "`scopt audit --dynamic` for runtime differential checks.", err=True)
        sys.exit(2)
    if snapshot is None:
        snapshot = str(Path(artifact).resolve().parent.parent / "snapshots" /
                       id_hex(info.input_snapshot_id))
    snap = _open_snapshot(snapshot, None, audit=True)
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

"""``scopt profile``: ``.qprof`` ingest and Profile v1 tools (WP6).

The run itself (``scopt profile run``, §5.11) lives in ``commands/profile_run.py``;
either module's ``register`` installs the whole group, in any order.
"""

from __future__ import annotations

import json
import sys

import click


def register(group: click.Group) -> None:
    """Add ``profile`` with every subcommand, ``run`` included, so the result does
    not depend on which command module is loaded first."""
    from .profile_run import profile_run_cmd

    group.add_command(profile)
    profile.add_command(profile_run_cmd)


@click.group()
def profile() -> None:
    """Profile v1 (.qprof ingest, merge, flow check, compatibility)."""


def _runtime_arg(value: str):
    from ..canon import is_sha256_id

    if is_sha256_id(value):
        return value
    with open(value, "rb") as f:
        return json.load(f)


def _inputs(specs: tuple[str, ...]) -> list[dict]:
    from ..canon import sha256_file

    out = []
    for s in specs:
        split, _, path = s.partition(":")
        if split not in ("train", "heldout", "final") or not path:
            raise click.BadParameter(f"{s!r}: expected SPLIT:PATH with SPLIT train|heldout|final")
        import os
        out.append({"name": os.path.basename(path), "sha256": "sha256:" + sha256_file(path),
                    "split": split})
    return out


@profile.command("qprof-dump")
@click.argument("qprof", type=click.Path(exists=True, dir_okay=False))
def qprof_dump(qprof: str) -> None:
    """Print a summary of a .qprof (exit 2 if the file is rejected)."""
    from ..formats.qprof_v1 import QprofFormatError, read_qprof

    try:
        q = read_qprof(qprof)
    except QprofFormatError as e:
        click.echo(json.dumps({"path": qprof, "rejected": e.reason, "detail": e.detail}))
        raise SystemExit(2)
    click.echo(json.dumps(q.summary(), indent=2, sort_keys=True))


@profile.command("ingest")
@click.argument("qprof", type=click.Path(exists=True, dir_okay=False))
@click.option("--staged-sc", required=True, type=click.Path(exists=True, dir_okay=False),
              help="The run directory's .sc after the profile run.")
@click.option("--snapshot", required=True, type=click.Path(exists=True),
              help="The profiled snapshot/artifact: a .sc or a directory holding cache.sc.")
@click.option("--bundle-id", required=True)
@click.option("--contract-id", required=True)
@click.option("--runtime-identity", required=True,
              help="runtime_identity.json path, or a sha256: id.")
@click.option("--run-id", default=None)
@click.option("--input", "inputs", multiple=True, help="SPLIT:PATH of a workload input.")
@click.option("--time-dilation", type=float, default=None)
@click.option("--artifact", is_flag=True, help="Domain kind artifact_units.")
@click.option("--no-require-flow", is_flag=True,
              help="Do not refuse a complete profile that breaks flow conservation "
                   "(diagnostics only).")
@click.option("--out", type=click.Path(dir_okay=False), help="Write profile.json here.")
@click.option("--store", type=click.Path(file_okay=False),
              help="Publish to STORE/profiles/<hex of profile_id>/profile.json.")
def ingest_cmd(qprof, staged_sc, snapshot, bundle_id, contract_id, runtime_identity, run_id,
               inputs, time_dilation, artifact, no_require_flow, out, store) -> None:
    """Join a .qprof to its snapshot and write Profile v1 (exit 2 on rejection)."""
    from ..formats.profile_v1 import ProfileError, check_flow, ingest, publish_profile, write_profile
    from ..formats.qprof_v1 import QprofFormatError

    try:
        p = ingest(qprof, staged_sc, snapshot, bundle_id=bundle_id, contract_id=contract_id,
                   runtime_identity=_runtime_arg(runtime_identity), run_id=run_id,
                   inputs=_inputs(inputs), time_dilation=time_dilation,
                   domain_kind="artifact_units" if artifact else "snapshot_units",
                   require_flow=not no_require_flow)
    except (QprofFormatError, ProfileError) as e:
        click.echo(json.dumps({"rejected": e.reason, "detail": getattr(e, "detail", "")}))
        raise SystemExit(2)
    paths = []
    if out:
        paths.append(write_profile(out, p))
    if store:
        paths.append(publish_profile(store, p))
    flow = check_flow(p)
    click.echo(json.dumps({"profile_id": p.profile_id, "snapshot_id": p.snapshot_id,
                           "complete": p.complete, "units": len(p.units),
                           "profile_only": len(p.profile_only),
                           "transient_entries": p.transient["entries"],
                           "total_dispatches": sum(r["total_dispatches"]
                                                   for r in p.collection["runs"]),
                           "flow": flow.to_json(), "written": paths}, indent=2, sort_keys=True))


@profile.command("check")
@click.argument("profiles", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False))
def check_cmd(profiles) -> None:
    """Validate profile.json files (schema, profile_id, flow conservation); exit 1 on failure."""
    from ..formats.profile_v1 import ProfileError, check_flow, read_profile

    bad = False
    for path in profiles:
        try:
            p = read_profile(path)
        except ProfileError as e:
            click.echo(json.dumps({"path": path, "ok": False, "error": e.reason,
                                   "detail": e.detail}))
            bad = True
            continue
        f = check_flow(p)
        bad |= not f.ok
        click.echo(json.dumps({"path": path, "ok": f.ok, "profile_id": p.profile_id,
                               "complete": p.complete, "flow": f.to_json()}, sort_keys=True))
    if bad:
        raise SystemExit(1)


@profile.command("merge")
@click.argument("profiles", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--out", required=True, type=click.Path(dir_okay=False))
def merge_cmd(profiles, out) -> None:
    """Merge profiles of one snapshot into OUT (exit 2 if they cannot be merged)."""
    from ..formats.profile_v1 import ProfileError, merge, read_profile, write_profile

    try:
        p = merge([read_profile(x) for x in profiles])
    except ProfileError as e:
        click.echo(json.dumps({"rejected": e.reason, "detail": e.detail}))
        raise SystemExit(2)
    write_profile(out, p)
    click.echo(json.dumps({"profile_id": p.profile_id, "runs": len(p.collection["runs"]),
                           "complete": p.complete}, sort_keys=True))


@profile.command("show")
@click.argument("profile_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--target", type=click.Path(exists=True), default=None,
              help="Apply to this snapshot/artifact (.sc or directory) via ProfileView.open.")
@click.option("--phase", default="steady", show_default=True)
@click.option("--top", default=20, show_default=True)
@click.option("--bundle-id", default=None)
@click.option("--contract-id", default=None)
@click.option("--runtime-identity", default=None)
@click.option("--min-profile-match", type=float, default=0.95, show_default=True)
def show_cmd(profile_json, target, phase, top, bundle_id, contract_id, runtime_identity,
             min_profile_match) -> None:
    """Hottest units of PHASE, with status (exit 2 if incompatible with --target)."""
    from ..formats.profile_v1 import ProfileIncompatible, ProfileView, read_profile

    p = read_profile(profile_json)
    if target is None:
        # Apply to its own domain: the profiled snapshot's unit list.
        from ..formats.profile_v1 import ProfileTarget
        t = ProfileTarget(p.snapshot_id, tuple(u["unit"] for u in p.units), p.bundle_id,
                          p.contract_id, p.runtime_identity_id)
        view = ProfileView.open(p, t, phase=phase)
    else:
        kw = {"bundle_id": bundle_id, "contract_id": contract_id}
        if runtime_identity:
            rt = _runtime_arg(runtime_identity)
            kw["runtime_identity" if isinstance(rt, dict) else "runtime_identity_id"] = rt
        try:
            view = ProfileView.open(p, target, phase=phase, min_profile_match=min_profile_match,
                                    **kw)
        except ProfileIncompatible as e:
            click.echo(json.dumps({"verdict": e.reason, "detail": e.detail}))
            raise SystemExit(2)
    counts = view.phase(phase)
    rows = sorted(counts.items(), key=lambda kv: (-(kv[1].value or 0), kv[0]))[:top]
    out = sys.stdout
    out.write(json.dumps(view.verdict(), sort_keys=True) + "\n")
    for uid, c in rows:
        out.write(json.dumps({"unit": uid, "vaddrs": [hex(v) for v in view.vaddrs(uid)],
                              **c.to_json()}, sort_keys=True) + "\n")

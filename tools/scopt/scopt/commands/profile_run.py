"""``scopt profile run``: exact profile runs through the run harness (§5.11; WP14).

Adds ``run`` to the ``profile`` group of ``commands/profile.py``. Both modules'
``register`` install the group and this subcommand, so the result does not
depend on module import or registration order.
"""

from __future__ import annotations

import json
import sys

import click


def register(group: click.Group) -> None:
    """Install ``profile run`` whatever the module load order: the ``profile``
    group object is shared (``commands/profile.py``), so adding the group here
    when it is not registered yet, and the subcommand to it, is idempotent."""
    from .profile import profile

    prof = group.commands.get("profile")
    if prof is None:
        group.add_command(profile)
        prof = profile
    if isinstance(prof, click.Group):
        prof.add_command(profile_run_cmd)


@click.command("run")
@click.option("-c", "--config", "base_toml", required=True,
              type=click.Path(exists=True, dir_okay=False), help="Base FastDyn TOML.")
@click.option("--snapshot", "target", required=True, type=click.Path(exists=True),
              help="Store snapshot dir, artifact dir or .sc to profile.")
@click.option("--phase", "phases", multiple=True,
              help="Phase marker NAME@0xVADDR, in order (phase 0 is always boot).")
@click.option("--reps", type=int, default=1, show_default=True)
@click.option("--inputs", "inputs", multiple=True, type=click.Path(exists=True, dir_okay=False),
              help="Input files for the input RAM bank (each profiled --reps times).")
@click.option("--input-bank", default="input", show_default=True)
@click.option("--input-split", default="train", show_default=True,
              type=click.Choice(["train", "heldout", "final"]))
@click.option("--qemu", type=click.Path(exists=True, dir_okay=False),
              help="QEMU (default: the target RuntimeIdentity's executable).")
@click.option("--bundle", type=click.Path(exists=True, dir_okay=False),
              help="bundle.json (default: the target directory's).")
@click.option("--runtime-identity", type=click.Path(exists=True, dir_okay=False),
              help="runtime_identity.json (default: the target directory's).")
@click.option("--artifact", "artifact_domain", is_flag=True, help="Domain kind artifact_units.")
@click.option("--store", type=click.Path(file_okay=False),
              help="Publish the merged profile to STORE/profiles/<hex>/profile.json.")
@click.option("--watch", multiple=True, type=click.Path(exists=True),
              help="Extra files/dirs that must be unchanged after profiling (T-B1-7).")
@click.option("--out", required=True, type=click.Path(file_okay=False))
@click.option("--quiet/--no-quiet", "quiet", default=False, show_default=True)
@click.option("--timeout", "timeout_s", type=float, default=300.0, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def profile_run_cmd(base_toml, target, phases, reps, inputs, input_bank, input_split, qemu,
                    bundle, runtime_identity, artifact_domain, store, watch, out, quiet,
                    timeout_s, as_json):
    """Profile SNAPSHOT on unchained warm copies; ingest, merge, check isolation.

    Exit 0 when every run's proof passed, every .qprof ingested with flow
    conservation, and the target and --watch paths are unchanged; 1 otherwise;
    2 when refused before any run.
    """
    from ..run.profile_run import ProfileRunError, profile_run

    try:
        doc = profile_run(base_toml, target, out, phases=phases, reps=reps, inputs=inputs,
                          input_bank=input_bank, input_split=input_split, qemu=qemu,
                          bundle=bundle, runtime_identity=runtime_identity,
                          artifact_domain=artifact_domain, store=store, watch=watch,
                          quiet=quiet, timeout_s=timeout_s)
    except ProfileRunError as e:
        click.echo(f"refused: {e.code}: {e.detail}", err=True)
        sys.exit(2)
    if as_json:
        d = dict(doc)
        d["isolation"] = {k: v for k, v in d["isolation"].items() if k != "before"}
        click.echo(json.dumps(d, indent=2, sort_keys=True))
    else:
        click.echo(f"verdict: {doc['verdict']}")
        for r in doc["runs"]:
            click.echo(f"  {r['name']}: verdict={r.get('verdict')} appended={r.get('appended')} "
                       f"dispatches={r.get('total_dispatches')} profile_only="
                       f"{r.get('profile_only')} flow_ok={r.get('flow_ok')}")
        m = doc.get("merged")
        if m:
            click.echo(f"  merged: {m['profile_id']} runs={m['runs']} complete={m['complete']} "
                       f"-> {m['path']}")
        click.echo(f"  isolation: {doc['isolation']['files']} files, "
                   f"{'unchanged' if doc['isolation']['ok'] else 'CHANGED'}")
        for f in doc["failures"]:
            click.echo(f"  FAIL: {f}")
    sys.exit(0 if doc["verdict"] == "PASS" else 1)

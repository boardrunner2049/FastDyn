"""``scopt bundle``: ExecutionBundle v1 identity bundles and RuntimeIdentity (WP8, §4.2)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click


def register(group: click.Group) -> None:
    group.add_command(bundle)


def _write(doc, out) -> None:
    from ..canon import canonical_json

    if out:
        Path(out).write_bytes(canonical_json(doc) + b"\n")
    else:
        click.echo(json.dumps(doc, indent=2, sort_keys=True))


@click.group()
def bundle() -> None:
    """Execution bundles."""


@bundle.command("identity")
@click.option("-c", "--config", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--manifest", required=True, type=click.Path(exists=True, dir_okay=False),
              help="<cache>.sc.fastdyn.json written for this configuration (deploy oracle).")
@click.option("--out", type=click.Path(dir_okay=False))
def bundle_identity(config, manifest, out):
    """Build the identity bundle of CONFIG (path keys normalized away)."""
    from ..formats.bundle_v1 import BundleError, identity_bundle

    try:
        doc = identity_bundle(config, manifest)
    except BundleError as e:
        click.echo(f"{e.code}: {e.detail}", err=True)
        sys.exit(2)
    _write(doc, out)


@bundle.command("check")
@click.argument("expected", type=click.Path(exists=True, dir_okay=False))
@click.option("-c", "--config", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--manifest", required=True, type=click.Path(exists=True, dir_okay=False))
def bundle_check(expected, config, manifest):
    """Compare EXPECTED bundle with the identity bundle of CONFIG; exit 2 on bundle_mismatch."""
    from ..formats.bundle_v1 import BundleError, check_bundle, identity_bundle, read_bundle

    try:
        check_bundle(read_bundle(expected), identity_bundle(config, manifest))
    except BundleError as e:
        click.echo(f"{e.code}: {e.detail}", err=True)
        sys.exit(2)
    click.echo("ok")


@bundle.command("validate")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
def bundle_validate(path):
    """Validate an ExecutionBundle v1 (schema, schema_version, bundle_id)."""
    from ..formats.bundle_v1 import BundleError, read_bundle

    try:
        doc = read_bundle(path)
    except BundleError as e:
        click.echo(f"{e.code}: {e.detail}", err=True)
        sys.exit(2)
    click.echo(doc["bundle_id"])


@bundle.command("runtime-identity")
@click.option("--run-dir", required=True, type=click.Path(exists=True, file_okay=False),
              help="A deployed (or run) run directory.")
@click.option("--sc", type=click.Path(exists=True, dir_okay=False),
              help="The .sc whose header supplies the runtime fields (default: the run's cache).")
@click.option("--scm", type=click.Path(exists=True, dir_okay=False),
              help="A capture (.scm) of this runtime: its RUN_INFO supplies prologue_sha256, "
              "icache_linesize and cpuinfo, and must match the QEMU exe and the .sc header.")
@click.option("--out", type=click.Path(dir_okay=False))
def bundle_runtime_identity(run_dir, sc, scm, out):
    """RuntimeIdentity of a run directory (QEMU/plugin, build options, .sc runtime fields)."""
    from ..formats.bundle_v1 import BundleError, run_info_fields, runtime_identity
    from ..formats.sc_records import ScHeader
    from ..run.launcher import FASTDYN_SRC, RunDir

    rd = RunDir.open(run_dir)
    orec = json.loads((rd.root / "oracle.json").read_text())
    argv = orec["argv"]
    plugin = None
    if "--plugin" in argv:
        p = Path(argv[argv.index("--plugin") + 1].split(",", 1)[0])
        plugin = p if p.is_absolute() else (FASTDYN_SRC / p).resolve()
    sc = sc or rd.cache
    header = ScHeader.from_bytes(Path(sc).open("rb").read(80)) if sc and Path(sc).is_file() \
        else None
    ri = None
    if scm:
        from ..formats.scm_v1 import read_scm

        f = read_scm(scm)
        if f.run_info is None:
            click.echo(f"no_run_info: {scm} has no valid RUN_INFO chunk", err=True)
            sys.exit(2)
        ri = run_info_fields(f.run_info)
    try:
        doc = runtime_identity(qemu=argv[0], plugin=plugin, config_text=rd.toml.read_text(),
                               sc_header=header, run_info=ri, fastdyn_src=FASTDYN_SRC)
    except BundleError as e:
        click.echo(f"{e.code}: {e.detail}", err=True)
        sys.exit(2)
    _write(doc, out)

"""``scopt helper-effects``: show helper effect tables and rebind one to a rebuilt QEMU."""

from __future__ import annotations

import sys
from pathlib import Path

import click


def register(group: click.Group) -> None:
    group.add_command(helper_effects)


@click.group("helper-effects")
def helper_effects() -> None:
    """Per-helper effect tables (helper-effects/1), bound to one QEMU executable."""


def _sha_id(s: str) -> str:
    s = s.strip()
    return s if s.startswith("sha256:") else f"sha256:{s}"


@helper_effects.command("show")
@click.option("--exe-sha256", help="Look the table up on the search path.")
@click.option("--exe", type=click.Path(exists=True, dir_okay=False),
              help="Look up the table of this executable (hashes it).")
def show(exe_sha256, exe):
    """Print the bound table's id, source record and helper rows (one per line)."""
    from ..analysis.envlayout import sha256_file
    from ..analysis.helper_effects import find_helper_effects

    if not exe_sha256 and not exe:
        raise click.UsageError("give --exe-sha256 or --exe")
    sha = sha256_file(exe) if exe else _sha_id(exe_sha256)
    t = find_helper_effects(sha)
    if t is None:
        click.echo(f"no helper effect table for {sha}: every helper is fail-closed", err=True)
        sys.exit(2)
    click.echo(f"{t.table_id} exe {t.exe_sha256} rows {len(t.rows)}")
    for k in ("commit", "tree", "rebound_from"):
        if k in t.qemu_source:
            click.echo(f"  {k}: {t.qemu_source[k]}")
    for r in t.rows:
        click.echo(f"  {r.helper} flags={r.tcg_flags} fault={r.may_fault} "
                   f"hostmem_writes={r.hostmem_writes}")


@helper_effects.command("rebind")
@click.option("--from-exe-sha", "from_sha", required=True,
              help="sha256 of the executable whose table is copied (hex or sha256:hex).")
@click.option("--exe", required=True, type=click.Path(exists=True, dir_okay=False),
              help="The new QEMU executable the copy is bound to.")
@click.option("--qemu-src", type=click.Path(exists=True, file_okay=False),
              help="QEMU source tree EXE was built from (default: EXE's parent's parent "
                   "when EXE is in <tree>/build/).")
@click.option("--out", type=click.Path(dir_okay=False),
              help="Output file (default: <first $SCOPT_HELPER_EFFECTS_PATH dir>/<sha>.json).")
@click.option("--force", is_flag=True, help="Overwrite an existing, different output file.")
def rebind(from_sha, exe, qemu_src, out, force):
    """Copy the table of --from-exe-sha to EXE after checking its helper sources.

    Every file in the table's qemu_source must be byte-identical (sha256) in --qemu-src;
    otherwise nothing is written, the differing files are listed and the exit status is
    1. Exit 2 when the source table or the output location is missing.
    """
    from ..analysis.envlayout import sha256_file
    from ..analysis.helper_effects import (HelperEffectsError, SourceMismatch,
                                           default_table_path, find_helper_effects,
                                           helper_effects_text, rebind_table)
    from ..formats.envlayout_v1 import find_envlayout

    old = find_helper_effects(_sha_id(from_sha))
    if old is None:
        click.echo(f"no helper effect table for {_sha_id(from_sha)} on the search path",
                   err=True)
        sys.exit(2)
    new_sha = sha256_file(exe)
    if qemu_src is None:
        cand = Path(exe).resolve().parent.parent
        if not (cand / "include/tcg/tcg.h").is_file():
            click.echo("cannot infer the QEMU source tree from --exe: give --qemu-src",
                       err=True)
            sys.exit(2)
        qemu_src = str(cand)
    try:
        t = rebind_table(old, new_sha, qemu_src)
    except SourceMismatch as e:
        click.echo(f"helper_sources_differ: {qemu_src}", err=True)
        for p, (want, have) in sorted(e.differing.items()):
            click.echo(f"  {p}: table {want or 'unknown'}, tree {have or 'missing'}", err=True)
        click.echo("The rows must be reviewed against the new sources; nothing written.",
                   err=True)
        sys.exit(1)
    except HelperEffectsError as e:
        click.echo(f"{e.code}: {e}", err=True)
        sys.exit(2)
    path = Path(out) if out else default_table_path(new_sha)
    if path is None:
        click.echo("give --out or set SCOPT_HELPER_EFFECTS_PATH", err=True)
        sys.exit(2)
    text = helper_effects_text(t)
    if path.exists() and path.read_text(encoding="utf-8") != text and not force:
        click.echo(f"{path} exists with different content (use --force)", err=True)
        sys.exit(2)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    click.echo(f"{t.table_id} exe {new_sha} rows {len(t.rows)} -> {path}")
    if path.name != f"{new_sha.split(':', 1)[1]}.json":
        click.echo("note: the search path finds a table only as <exe sha256 hex>.json",
                   err=True)
    if find_envlayout(new_sha) is None:
        click.echo("note: no EnvLayout for this executable on SCOPT_ENVLAYOUT_PATH yet "
                   "(scopt envlayout produce --exe ...); without it the rows cannot be "
                   "resolved", err=True)

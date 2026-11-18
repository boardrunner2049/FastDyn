"""``scopt envlayout``, ``scopt hostops`` and ``scopt admission`` (B3/B4 WP17)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click


def register(group: click.Group) -> None:
    group.add_command(envlayout)
    group.add_command(hostops)
    group.add_command(admission)


def _out(doc, out: str | None) -> None:
    text = json.dumps(doc, indent=1, sort_keys=True, default=str)
    if out:
        Path(out).write_text(text + "\n")
    else:
        click.echo(text)


def _open(snapshot: str):
    from ..importer.import_cache import open_snapshot

    return open_snapshot(snapshot)


@click.group()
def envlayout() -> None:
    """EnvLayout v1: env offsets bound to one QEMU executable (design §0 G21)."""


@envlayout.command("produce")
@click.option("--exe", required=True, type=click.Path(exists=True, dir_okay=False),
              help="QEMU executable with DWARF (gdb -nx -batch ptype/o).")
@click.option("--out", type=click.Path(dir_okay=False), help="Write the canonical JSON here.")
def envlayout_produce(exe, out):
    """Produce the EnvLayout of EXE with gdb (cwd: a fresh temporary directory)."""
    from ..analysis.envlayout import GdbError, produce_envlayout

    try:
        lay = produce_envlayout(exe)
    except GdbError as e:
        click.echo(f"gdb_failed: {e}", err=True)
        sys.exit(2)
    if out:
        Path(out).write_bytes(lay.canonical_bytes())
    click.echo(f"{lay.envlayout_id} exe {lay.exe_sha256} fields {len(lay.fields)}")


@envlayout.command("show")
@click.option("--exe-sha256", help="Look the layout up on the search path.")
@click.option("--file", "path", type=click.Path(exists=True, dir_okay=False))
@click.argument("fields", nargs=-1)
def envlayout_show(exe_sha256, path, fields):
    """Print the offsets of FIELDS (default: the design G21 names)."""
    from ..canon import is_sha256_id
    from ..formats.envlayout_v1 import G21_NAMES, find_envlayout, read_envlayout

    if not path and not exe_sha256:
        raise click.UsageError("give --exe-sha256 or --file")
    sha = None
    if not path:
        # accept the bare hex digest (the produced file's name) as well as "sha256:<hex>"
        sha = exe_sha256.strip().lower()
        if not sha.startswith("sha256:"):
            sha = f"sha256:{sha}"
        if not is_sha256_id(sha):
            raise click.BadParameter(f"{exe_sha256!r} is not a sha256 (64 hex digits, "
                                     "optionally prefixed with 'sha256:')",
                                     param_hint="--exe-sha256")
    lay = read_envlayout(path) if path else find_envlayout(sha)
    if lay is None:
        click.echo(f"guard_layout_unbound: no EnvLayout for {sha} on the search path "
                   "(SCOPT_ENVLAYOUT_PATH, then the packaged layouts)", err=True)
        sys.exit(2)
    names = list(fields) or list(G21_NAMES.values())
    rows = {}
    for n in names:
        try:
            r = lay.field(n)
            rows[n] = {"off": r.off, "hex": f"{'-' if r.off < 0 else ''}{abs(r.off):#x}",
                       "size": r.size, "region": r.region}
        except KeyError:
            rows[n] = None
    _out({"envlayout_id": lay.envlayout_id, "exe_sha256": lay.exe_sha256, "fields": rows}, None)


@click.group()
def hostops() -> None:
    """HostOps typed view (design §8.1)."""


@hostops.command("census")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False))
@click.option("--json", "out", type=click.Path(dir_okay=False))
def hostops_census(snapshot, out):
    """HostOps over every unit: pins, ENV_STORE origins, unclassified rbp operands."""
    from ..analysis.hostops import census
    from ..formats.envlayout_v1 import find_envlayout

    s = _open(snapshot)
    lay = find_envlayout((s.runtime or {}).get("qemu_sha256"))
    c = census(s.units(), lay, s.header.code_address)
    c["snapshot_id"] = s.snapshot_id
    _out(c, out)


@hostops.command("dump")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False))
@click.argument("uid")
def hostops_dump(snapshot, uid):
    """Typed ops of one unit."""
    from ..analysis.hostops import build_hostops
    from ..formats.envlayout_v1 import find_envlayout

    s = _open(snapshot)
    lay = find_envlayout((s.runtime or {}).get("qemu_sha256"))
    h = build_hostops(s.unit(uid), lay, s.header.code_address)
    _out({**h.summary(), "ops": [o.to_json() for o in h.ops]}, None)


@click.group()
def admission() -> None:
    """RuntimeIdentity v2 admission block and V20 (design §9.1)."""


@admission.command("identity")
@click.option("--snapshot", required=True, type=click.Path(exists=True, file_okay=False),
              help="Store snapshot (runtime_identity.json + manifest.json).")
@click.option("--config", type=click.Path(exists=True, dir_okay=False), help="The run TOML.")
@click.option("--virtuals", type=click.Path(exists=True, dir_okay=False),
              help="The generated virtuals.txt (checked against the manifest hash).")
@click.option("--out", type=click.Path(dir_okay=False))
def admission_identity(snapshot, config, virtuals, out):
    """RuntimeIdentity v2 = the snapshot's identity + the admission facts."""
    import hashlib

    from ..formats.bundle_v1 import (BundleError, admission_facts, extend_runtime_identity,
                                     read_runtime_identity)
    from ..formats.envlayout_v1 import find_envlayout

    d = Path(snapshot)
    ri = read_runtime_identity(d / "runtime_identity.json")
    man = json.loads((d / "manifest.json").read_text()) if (d / "manifest.json").is_file() else None
    vtext = None
    if virtuals:
        raw = Path(virtuals).read_bytes()
        want = (((man or {}).get("inputs", {}).get("content_files", {}) or {})
                .get("virtuals") or {}).get("sha256")
        if want and hashlib.sha256(raw).hexdigest() != want:
            click.echo(f"virtuals_mismatch: {virtuals} is not the manifest's virtuals file",
                       err=True)
            sys.exit(2)
        vtext = raw.decode("utf-8", "replace")
    adm = admission_facts(manifest=man, qemu_sha256=ri.get("qemu_sha256"),
                          envlayout=find_envlayout(ri.get("qemu_sha256")),
                          config_text=Path(config).read_text() if config else None,
                          virtuals_text=vtext)
    try:
        doc = extend_runtime_identity(ri, adm)
    except BundleError as e:
        click.echo(f"{e.code}: {e.detail}", err=True)
        sys.exit(2)
    _out(doc, out)


@admission.command("check")
@click.argument("runtime_identity", type=click.Path(exists=True, dir_okay=False))
@click.option("--snapshot", type=click.Path(exists=True, file_okay=False),
              help="Store snapshot whose Tier-0 capture cross-checks tb_start_cb_possible.")
@click.option("--d3/--no-d3", default=False, help="Require the D3 plugin fact.")
@click.option("--json", "out", type=click.Path(dir_okay=False))
def admission_check(runtime_identity, snapshot, d3, out):
    """Run the V20 rows; exit 1 on any failure."""
    from ..formats.bundle_v1 import read_runtime_identity
    from ..validate import Validation, v20_rows

    ri = read_runtime_identity(runtime_identity)
    units = _open(snapshot).units() if snapshot else None
    v = Validation()
    ok = v20_rows(v, ri, units=units, need_d3=d3)
    _out({"accepted": ok, "rows": v.rows}, out)
    if not ok:
        sys.exit(1)

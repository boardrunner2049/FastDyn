"""Offline firmware-region origin recovery for v7 artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import click


def register(group: click.Group) -> None:
    group.add_command(firmware_region)


@click.group("firmware-region")
def firmware_region() -> None:
    """Inspect call-site-specific firmware executable regions."""


def _integer(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as exc:
        raise click.BadParameter("expected an integer (decimal or 0x-prefixed)") from exc


@firmware_region.command("recover")
@click.option("--artifact", required=True, type=click.Path(exists=True, path_type=Path),
              help="Published artifact directory or its cache.sc file.")
@click.option("--host-pc", type=_integer,
              help="Absolute native PC in the artifact's recorded RX mapping.")
@click.option("--native-offset", type=_integer,
              help="Native offset relative to the SuperCache arena.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def recover(artifact: Path, host_pc: int | None, native_offset: int | None,
            as_json: bool) -> None:
    """Map a private native address back to its source TB and inline context."""
    if (host_pc is None) == (native_offset is None):
        raise click.UsageError("provide exactly one of --host-pc or --native-offset")
    path = artifact / "cache.sc" if artifact.is_dir() else artifact
    from ..formats.sc_v5 import read_sc
    from ..formats.sc_v7 import decode_directory, read_sc7

    sc = read_sc(path, versions=(7,))
    directory = read_sc7(path)
    offset = native_offset if native_offset is not None else host_pc - sc.header.code_address
    rows = decode_directory(directory)
    matches = [origin for origin in rows["origins"]
               if origin["private_offset"] <= offset <
               origin["private_offset"] + origin["size"]]
    if len(matches) != 1:
        doc = {"status": "not_found" if not matches else "ambiguous",
               "artifact": str(path), "native_offset": offset,
               "host_pc": sc.header.code_address + offset, "matches": matches}
        click.echo(json.dumps(doc, sort_keys=True) if as_json else
                   f"{doc['status']}: native offset 0x{offset:x}")
        raise click.exceptions.Exit(1)
    origin = dict(matches[0])
    source_offset = offset - origin["private_offset"]
    record = next((rec for rec in sc.records if rec is not None and
                   rec.tb_offset == origin["source_tb_offset"]), None)
    doc = {"status": "recovered", "artifact": str(path),
           "directory_version": directory.directory_version,
           "trusted": directory.trusted, "artifact_id8": directory.artifact_id8.hex(),
           "native_offset": offset, "host_pc": sc.header.code_address + offset,
           **origin, "source_offset": source_offset,
           "source_native_offset": None if record is None else record.tc_offset + source_offset,
           "source_guest_pc": None if record is None else record.pc}
    if as_json:
        click.echo(json.dumps(doc, sort_keys=True))
    else:
        click.echo(
            f"region={doc['region_index']} member={doc['member_index']} "
            f"context={doc['context_id8']} source_tb=0x{doc['source_tb_offset']:x} "
            f"source+0x{source_offset:x}")

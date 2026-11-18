"""``scopt meta``: inspect ``.scm`` v1 capture files (WP4)."""

from __future__ import annotations

import json
import sys

import click


def register(group: click.Group) -> None:
    group.add_command(meta)


@click.group()
def meta() -> None:
    """Capture metadata (.scm v1) files."""


def _parse_types(types: tuple[str, ...]) -> set[int] | None:
    if not types:
        return None
    from ..formats.enums import ChunkType

    out = set()
    for t in types:
        out.add(int(t, 0) if t[:1].isdigit() else int(ChunkType[t.upper()]))
    return out


@meta.command("dump")
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--jsonl", "jsonl", is_flag=True,
              help="One canonical JSON object per line: file_header, chunks, rejected chunks, "
                   "summary.")
@click.option("--slot", "slots", multiple=True, type=int, help="Only TB chunks of these slots.")
@click.option("--type", "types", multiple=True,
              help="Only chunks of these types (name such as TB or a number).")
@click.option("--no-arrays", is_flag=True, help="Omit TB arrays and MAPS text.")
@click.option("--strict", is_flag=True,
              help="Exit 1 if any chunk was rejected or the capture is incomplete.")
def dump(files: tuple[str, ...], jsonl: bool, slots: tuple[int, ...], types: tuple[str, ...],
         no_arrays: bool, strict: bool) -> None:
    """Dump .scm FILES (summary by default; --jsonl for every chunk)."""
    from ..canon import canonical_json
    from ..formats.scm_v1 import ScmHeaderError, read_scm, scm_to_jsonl_rows

    bad = False
    out = sys.stdout.buffer
    for path in files:
        try:
            scm = read_scm(path)
        except ScmHeaderError as e:
            bad = True
            if jsonl:
                out.write(canonical_json({"kind": "file_error", "path": path,
                                          "error": str(e)}) + b"\n")
            else:
                click.echo(f"{path}: REJECTED ({e})", err=True)
            continue
        if scm.rejected_chunks or scm.incomplete:
            bad = True
        if jsonl:
            rows = scm_to_jsonl_rows(scm, arrays=not no_arrays,
                                     slots=set(slots) if slots else None,
                                     types=_parse_types(types))
            for r in rows:
                out.write(canonical_json(r) + b"\n")
        else:
            s = scm.summary()
            click.echo(json.dumps(s, indent=2, sort_keys=True))
            for r in scm.rejected_chunks:
                click.echo(f"  rejected @{r.offset}: seq={r.seq} type={r.type} {r.reason}"
                           f"{' (prefix)' if r.prefix else ''}")
    out.flush()
    if strict and bad:
        raise SystemExit(1)


@meta.command("merge-check")
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False))
def merge_check(files: tuple[str, ...]) -> None:
    """Union the TB chunks of several .scm FILES by slot; exit 1 on snapshot_merge_conflict."""
    from ..formats.scm_v1 import SnapshotMergeConflict, merge_scm_sources

    try:
        merged = merge_scm_sources(files)
    except SnapshotMergeConflict as e:
        click.echo(str(e), err=True)
        raise SystemExit(1)
    per_file: dict[str, int] = {}
    for _slot, (_tb, src) in merged.items():
        per_file[src.path or "?"] = per_file.get(src.path or "?", 0) + 1
    click.echo(json.dumps({"slots": len(merged),
                           "range": [min(merged), max(merged)] if merged else None,
                           "from": per_file}, indent=2, sort_keys=True))


def _parse_slots(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out.extend(range(int(a), int(b) + 1) if b else [int(a)])
    return out


@meta.command("slice")
@click.argument("src", type=click.Path(exists=True, dir_okay=False))
@click.argument("dst", type=click.Path(dir_okay=False))
@click.option("--slots", required=True, help="Slots to keep, e.g. 0-9,40,41.")
@click.option("--drop-maps", is_flag=True, help="Empty the MAPS text (marked truncated).")
def slice_cmd(src: str, dst: str, slots: str, drop_maps: bool) -> None:
    """Write a small valid .scm with only the TB chunks of SLOTS (test fixtures)."""
    from ..formats.scm_v1 import read_scm, slice_scm, write_scm

    out = slice_scm(read_scm(src), _parse_slots(slots), keep_maps=not drop_maps)
    write_scm(dst, out)
    click.echo(json.dumps(out.summary(), sort_keys=True))

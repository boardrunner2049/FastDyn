"""``scopt sc``: inspect ``.sc`` v5 files and make identity copies (WP1)."""

from __future__ import annotations

import json
import sys

import click


def register(group: click.Group) -> None:
    group.add_command(sc)


@click.group()
def sc() -> None:
    """``.sc`` v5 snapshot files."""


def _hex(v: int) -> str:
    return f"0x{v:x}"


@sc.command("dump")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--no-records", is_flag=True, help="Header and summary only.")
@click.option("--slot", type=int, multiple=True, help="Only these slots (repeatable).")
@click.option("--search", "with_search", is_flag=True,
              help="Also decode each valid record's search data.")
@click.option("--check", "with_check", is_flag=True,
              help="Also list structural (non-canonical) findings.")
def dump(path: str, as_json: bool, no_records: bool, slot: tuple[int, ...],
         with_search: bool, with_check: bool) -> None:
    """Print the header and records of PATH as QEMU's loader would see them.

    Exit status: 0 if QEMU would load the file with every record valid,
    1 if records were skipped or the file is truncated, 2 if the header is
    rejected.
    """
    from ..decode.search import SearchDecodeError, decode_search, is_pcrel
    from ..formats.sc_v5 import read_sc, structural_findings

    f = read_sc(path)
    doc = f.to_json(records=not no_records)
    if slot and "records" in doc:
        doc["records"] = [r for r in doc["records"] if r["slot"] in set(slot)]
    if with_search and "records" in doc:
        for row in doc["records"]:
            i = row["slot"]
            if not row["valid"]:
                continue
            rec = f.records[i]
            body = f.bodies[i]
            try:
                rows = decode_search(body[rec.tc_size:], rec.icount, pcrel=is_pcrel(rec.cflags),
                                     pc=rec.pc)
                row["search"] = {
                    "nbytes": rows.nbytes,
                    "rows": [{"words": [_hex(w) for w in r.words], "host_end": r.host_end}
                             for r in rows],
                }
            except SearchDecodeError as e:
                row["search"] = {"error": str(e)}
    if with_check:
        doc["structural_findings"] = [{"code": x.code, "detail": x.detail}
                                      for x in structural_findings(f)]
    rc = 2 if not f.loadable else (1 if (f.skipped or f.problems) else 0)
    if as_json:
        click.echo(json.dumps(doc, indent=1))
        sys.exit(rc)

    h = doc["header"]
    click.echo(f"file {path}  size {f.file_size}")
    click.echo("header " + " ".join(f"{k}={v}" for k, v in h.items()))
    click.echo(f"loadable={f.loadable} header_problems={list(f.header_problems)} "
               f"count={f.count} available={f.available_records} "
               f"valid={len(f.valid_slots)} skipped={len(f.skipped)} max_end={doc['max_end']}")
    for p in f.problems:
        click.echo(f"problem {p.code}: {p.detail}")
    for s in f.skipped:
        click.echo(f"skipped slot {s.slot}: {s.reason} ({s.detail})")
    if "snapshot_id" in doc:
        click.echo(f"snapshot_id {doc['snapshot_id']}")
    for x in doc.get("structural_findings", []):
        click.echo(f"finding {x['code']}: {x['detail']}")
    if "records" in doc:
        click.echo("slot ok   tb_offset   tc_offset  end_offset tc_size page_addr0  flags      "
                   "cflags     gsize icnt jmp_reset   jmp_insn    tb_uid")
        for r in doc["records"]:
            click.echo(
                f"{r['slot']:4d} {'ok ' if r['valid'] else 'BAD'} {r['tb_offset']:>10} "
                f"{r['tc_offset']:>11} {r['end_offset']:>11} {r['tc_size']:7d} "
                f"{r['page_addr0']:>11} {r['flags']:>10} {r['cflags']:>10} "
                f"{r['guest_size']:5d} {r['icount']:4d} "
                f"{','.join(map(str, r['jmp_reset_offset'])):>11} "
                f"{','.join(map(str, r['jmp_insn_offset'])):>11} {r['tb_uid']}")
            if "search" in r:
                s = r["search"]
                if "error" in s:
                    click.echo(f"     search error: {s['error']}")
                else:
                    for k, row in enumerate(s["rows"]):
                        click.echo(f"     insn {k:3d} words={','.join(row['words'])} "
                                   f"host_end={row['host_end']}")
    sys.exit(rc)


@sc.command("identity-copy")
@click.argument("src", type=click.Path(exists=True, dir_okay=False))
@click.argument("dst", type=click.Path(dir_okay=False))
@click.option("--require-identity", is_flag=True,
              help="Exit 3 when the input is not canonical (identity_canonicalized).")
def identity_copy_cmd(src: str, dst: str, require_identity: bool) -> None:
    """Read SRC and write it back to DST sparsely (design §5.8 V9, static half).

    Prints a JSON result. status=identity means SRC was canonical and
    sha256(DST) == sha256(SRC). status=identity_canonicalized means SRC had
    skipped records or other non-canonical bytes; DST is what QEMU would load,
    and the dropped slots are listed. Exit status: 0 identity or
    canonicalized, 1 fail (hash mismatch on canonical input), 2 SRC not
    loadable, 3 canonicalized with --require-identity.
    """
    from ..formats.sc_v5 import ScFormatError, identity_copy

    try:
        res = identity_copy(src, dst)
    except ScFormatError as e:
        click.echo(json.dumps({"status": "not_loadable", "error": str(e)}))
        sys.exit(2)
    click.echo(json.dumps(res.to_json(), indent=1))
    if res.status == "fail":
        sys.exit(1)
    if res.status == "identity_canonicalized" and require_identity:
        sys.exit(3)

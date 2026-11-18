"""``scopt b4`` (B4 eliminate-state-sync) and ``scopt sfb`` (icount-entry-sfb). WP25."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click


def register(group: click.Group) -> None:
    group.add_command(b4)
    group.add_command(sfb)


def _out(doc, out: str | None) -> None:
    text = json.dumps(doc, indent=1, sort_keys=True, default=str)
    if out:
        Path(out).write_text(text + "\n")
    else:
        click.echo(text)


def _load(snapshot: str, runtime: str | None):
    from ..formats.envlayout_v1 import find_envlayout
    from ..importer.import_cache import open_snapshot

    s = open_snapshot(snapshot)
    ri = json.loads(Path(runtime).read_text()) if runtime else \
        (dict(s.runtime) if s.runtime is not None else None)
    lay = find_envlayout((ri or {}).get("qemu_sha256"))
    if lay is None:
        click.echo("guard_layout_unbound: no EnvLayout for the RuntimeIdentity's QEMU", err=True)
        sys.exit(2)
    return s, lay, ri


_RT = click.option("--runtime", type=click.Path(exists=True, dir_okay=False),
                   help="RuntimeIdentity (v2 with the admission block) the artifact targets; "
                        "default: the snapshot's (a v1 identity admits D1 only and assumes "
                        "tb_start_cb_possible).")


@click.group()
def b4() -> None:
    """B4 eliminate-state-sync (design §7)."""


@b4.command("census")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False))
@_RT
@click.option("--mode", type=click.Choice(["b4", "tcg"]), default="b4", show_default=True,
              help="tcg: TCG's own sync set (S2-S5 + P); M0 = 0 expected for D1.")
@click.option("--all-units", is_flag=True,
              help="Also analyse special_cflags units (the model population).")
@click.option("--d3-all-calls", is_flag=True, help="Survey rule for D3 (every call reads).")
@click.option("--tb-start-cb", type=click.Choice(["identity", "true", "false"]),
              default="identity", show_default=True)
@click.option("--out", type=click.Path(dir_okay=False))
def census_cmd(snapshot, runtime, mode, all_units, d3_all_calls, tb_start_cb, out):
    """Static dead-store counts per class (T-B4-1)."""
    from ..analysis.obs_contract import build_contract
    from ..analysis.syncdead import census_syncdead

    s, lay, ri = _load(snapshot, runtime)
    c = build_contract(lay, ri)
    if tb_start_cb != "identity":
        import dataclasses

        c = dataclasses.replace(c, tb_start_cb_possible=tb_start_cb == "true")
    doc = census_syncdead(s.units(), lay, s.header.code_address, c, mode=mode,
                          skip_special=not all_units, d3_all_calls=d3_all_calls)
    doc.pop("_rows", None)
    doc["snapshot_id"] = s.snapshot_id
    _out(doc, out)


@b4.command("package")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False))
@click.option("--store", required=True, type=click.Path(file_okay=False))
@_RT
@click.option("--classes", default="D1|D2|D3", show_default=True)
@click.option("--candidate-id", default=None)
@click.option("--selftest", "selftest_path", type=click.Path(dir_okay=False),
              help="V18 document cache (scopt hostsym selftest --cache).")
@click.option("--out", type=click.Path(dir_okay=False))
def package_cmd(snapshot, store, runtime, classes, candidate_id, selftest_path, out):
    """Propose (eliminate-state-sync@1), link (v5), validate and publish a B4 artifact."""
    from ..passes.eliminate_state_sync import class_ledger_rows, package_b4, propose_b4
    from ..validate_sym.selftest import load_or_run

    s, lay, ri = _load(snapshot, runtime)
    prop = propose_b4(s, layout=lay, runtime=ri, classes=classes)
    Path(store).mkdir(parents=True, exist_ok=True)
    cid = candidate_id or f"b4-{'-'.join(prop.classes)}-{s.snapshot_id[7:19]}"
    doc = {"proposal": {"outcome": prop.result.outcome.value, "reason": prop.result.reason,
                        "detail": prop.result.detail, "by_class": prop.by_class(),
                        "units": len(prop.patches), "stats": dict(prop.result.stats)},
           "ledger": [r.to_json() for r in class_ledger_rows(prop).rows]}
    if prop.patches:
        st = load_or_run(selftest_path)
        r = package_b4(s, prop, store, candidate_id=cid, layout=lay, runtime=ri, selftest=st)
        doc.update(status=r.status, reason=r.reason, artifact_id=r.artifact_id, path=r.artifact_dir or r.rejection_path)
    else:
        doc.update(status="no_change")
    _out(doc, out)
    sys.exit(0 if doc["status"] in ("published", "no_change") else 1)


@b4.command("mutants")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False))
@click.option("--store", required=True, type=click.Path(file_okay=False))
@_RT
@click.option("--per-kind", default=1, show_default=True)
@click.option("--selftest", "selftest_path", type=click.Path(dir_okay=False))
@click.option("--out", type=click.Path(dir_okay=False))
def mutants_cmd(snapshot, store, runtime, per_kind, selftest_path, out):
    """Package every B4 mutant found in SNAPSHOT; each must be rejected statically."""
    from ..b4_mutants import check_mutant, find_mutants
    from ..validate_sym.selftest import load_or_run

    s, lay, ri = _load(snapshot, runtime)
    st = load_or_run(selftest_path)
    Path(store).mkdir(parents=True, exist_ok=True)
    ms = find_mutants(s, lay, ri, per_kind=per_kind)
    res = [dict(m.to_json(), **check_mutant(s, m, store, layout=lay, runtime=ri,
                                            selftest=st).to_json()) for m in ms]
    doc = {"snapshot_id": s.snapshot_id, "mutants": len(res),
           "rejected": sum(r["rejected"] for r in res),
           "all_rejected": bool(res) and all(r["rejected"] for r in res), "results": res}
    _out(doc, out)
    sys.exit(0 if doc["all_rejected"] else 1)


@click.group()
def sfb() -> None:
    """icount-entry-sfb: baseline-TCG fix of the TB entry (not B3/B4)."""


@sfb.command("package")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False))
@click.option("--store", required=True, type=click.Path(file_okay=False))
@click.option("--candidate-id", default=None)
@click.option("--native/--no-native", default=True, show_default=True,
              help="Execute every entry form natively for all 2x65536 icount values.")
@click.option("--out", type=click.Path(dir_okay=False))
def sfb_package_cmd(snapshot, store, candidate_id, native, out):
    """Propose, rebuild (+16 bytes per unit), validate (SFB1-SFB9) and publish."""
    from collections import Counter

    from ..importer.import_cache import open_snapshot
    from ..passes.icount_entry_sfb import package_sfb, propose_sfb

    s = open_snapshot(snapshot)
    prop = propose_sfb(s)
    Path(store).mkdir(parents=True, exist_ok=True)
    r = package_sfb(s, prop, store, candidate_id=candidate_id or f"sfb-{s.snapshot_id[7:19]}",
                    native=native)
    doc = {"proposal": {"outcome": prop.result.outcome.value, "units": len(prop.units),
                        "skips": dict(Counter(prop.skips.values()))},
           "status": r.status, "reason": r.reason, "artifact_id": r.artifact_id, "path": r.path,
           "moved": None if r.link is None else len(r.link.moved),
           "validation": None if r.validation is None else r.validation.to_json()["rows"]}
    _out(doc, out)
    sys.exit(0 if r.status in ("published", "no_change") else 1)

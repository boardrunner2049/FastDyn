"""``scopt hostsym``: the B3/B4 validator kernel from the command line (WP19)."""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import click


def register(group: click.Group) -> None:
    group.add_command(hostsym)


def _out(doc, out: str | None) -> None:
    text = json.dumps(doc, indent=1, sort_keys=True, default=str)
    if out:
        Path(out).write_text(text + "\n")
    else:
        click.echo(text)


def _ctx(snapshot: str, tb_start_cb: str):
    from ..analysis.obs_contract import build_contract
    from ..formats.envlayout_v1 import find_envlayout
    from ..importer.import_cache import open_snapshot

    s = open_snapshot(snapshot)
    lay = find_envlayout(s.runtime.get("qemu_sha256"))
    if lay is None:
        click.echo("guard_layout_unbound: no EnvLayout for the snapshot's QEMU", err=True)
        sys.exit(2)
    c = build_contract(lay, s.runtime)
    if tb_start_cb != "identity":
        c = dataclasses.replace(c, tb_start_cb_possible=tb_start_cb == "true")
    return s, lay, c


_TSC = click.option("--tb-start-cb", type=click.Choice(["identity", "true", "false"]),
                    default="identity", show_default=True,
                    help="tb_start_cb_possible: from the RuntimeIdentity or forced.")


@click.group()
def hostsym() -> None:
    """hostsym: host-level trace-equivalence validator (design §9.2-9.3)."""


@hostsym.command("selftest")
@click.option("--trials", default=48, show_default=True)
@click.option("--out", type=click.Path(dir_okay=False))
@click.option("--cache", type=click.Path(dir_okay=False),
              help="Reuse/write the V18 document here (kept while the kernel is unchanged).")
def selftest_cmd(trials, out, cache):
    """V18: native ctypes+mmap(PROT_EXEC) semantics check + synthetic mutation suite."""
    from ..validate_sym.selftest import load_or_run

    doc = load_or_run(cache, trials=trials)
    _out(doc, out)
    if out:
        m = doc.get("mutants", {})
        click.echo(f"{doc['status']}: native {doc['native'].get('status')} "
                   f"({doc['native'].get('forms')} forms, {doc['native'].get('trials')} trials); "
                   f"mutants {m.get('rejected')}/{m.get('mutants')} rejected")
    sys.exit(0 if doc["status"] == "pass" else 1)


@hostsym.command("coverage")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False))
@click.option("--profile", type=click.Path(exists=True, dir_okay=False),
              help="profile.json of the snapshot; weights = exact entries of --phase.")
@click.option("--phase", default="steady", show_default=True)
@_TSC
@click.option("--out", type=click.Path(dir_okay=False))
def coverage_cmd(snapshot, profile, phase, tb_start_cb, out):
    """Units (and profile weight) hostsym analyses without UNSUPPORTED (T-B3-4)."""
    from ..validate_sym.coverage import coverage, phase_weights

    s, lay, c = _ctx(snapshot, tb_start_cb)
    w = phase_weights(profile, phase) if profile else None
    doc = coverage(s, lay, weights=w, contract=c)
    doc.update({"snapshot_id": s.snapshot_id, "profile": profile, "phase": phase})
    _out(doc, out)


@hostsym.command("check")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False))
@click.argument("uid")
@click.option("--patch", "patches", multiple=True, help="OFF:HEXBYTES (same length).")
@click.option("--pre", "pres", multiple=True, help="OFF:WIDTH=VALUE precondition on env.")
@_TSC
def check_cmd(snapshot, uid, patches, pres, tb_start_cb):
    """V12 on one unit of SNAPSHOT against a same-length patched copy."""
    from ..validate_sym import check_equiv, patched, program_of, world_of

    s, lay, c = _ctx(snapshot, tb_start_cb)
    p = program_of(s.unit(uid), world_of(s, lay))
    ps = []
    for x in patches:
        o, h = x.split(":", 1)
        ps.append((int(o, 0), bytes.fromhex(h)))
    pre = {}
    for x in pres:
        k, val = x.split("=", 1)
        o, w = k.split(":", 1)
        pre[(int(o, 0), int(w, 0))] = int(val, 0)
    v = check_equiv(p, patched(p, ps), pre or None, None, c, layout=lay)
    _out(v.to_json(), None)
    sys.exit(0 if v.ok else 1)


@hostsym.command("mutants")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False), required=False)
@click.option("--limit", default=25, show_default=True, help="Per mutant kind (real units).")
@click.option("--out", type=click.Path(dir_okay=False))
def mutants_cmd(snapshot, limit, out):
    """The §9.3 mutation suite: synthetic units, plus same-length mutants of SNAPSHOT."""
    from ..analysis.hostops import build_hostops
    from ..validate_sym import program_of, world_of
    from ..validate_sym.mutants import real_mutants, synthetic_suite

    doc = {"synthetic": synthetic_suite().to_json()}
    if snapshot:
        s, lay, c = _ctx(snapshot, "identity")
        w = world_of(s, lay)
        progs = {u.uid: program_of(u, w) for u in s.units()}
        hops = {u.uid: build_hostops(u, lay, s.header.code_address) for u in s.units()}
        ct = dataclasses.replace(c, tb_start_cb_possible=True)
        doc["real"] = real_mutants(progs, hops, c, lay, limit_per_kind=limit,
                                   cdio_contract=ct).to_json()
        doc["snapshot_id"] = s.snapshot_id
    _out(doc, out)
    ok = doc["synthetic"]["all_rejected"] and doc.get("real", {}).get("rejected", 0) == \
        doc.get("real", {}).get("mutants", 0)
    sys.exit(0 if ok else 1)


@hostsym.command("link-mutants")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False))
@click.option("--limit", default=0, show_default=True,
              help="Units to grow (0: every growable return/static unit).")
@click.option("--out", type=click.Path(dir_okay=False))
def link_mutants_cmd(snapshot, limit, out):
    """§9.3 guard/link mutants built with WP18's emitter and placement (WP19 integration):
    atom drops, guard-before-effect, stub store, falsified-fact link, shared variant."""
    from ..guard import GuardOffsets
    from ..link import NativeLinkerV2, capture_join
    from ..validate_sym.link_mutants import (LinkSuite, fact_link_mutants, general_plan,
                                             growable, guard_mutants, shared_variant_mutants)

    s, lay, c = _ctx(snapshot, "identity")
    off = GuardOffsets.from_layout(lay)
    jr = capture_join(s)
    gb = {u.uid: jr.slots[u.slot].chunk.guest_bytes for u in s.units()
          if jr.slots.get(u.slot) is not None and jr.slots[u.slot].bound}
    items = growable(s, gb)
    if limit:
        items = items[:limit]
    suite = LinkSuite()
    for reg in (True, False):
        plan = general_plan(s, items)
        plan.register_slots = reg
        res = NativeLinkerV2(s, plan, offsets=off, derive_captures=False).link()
        if not res.ok:
            click.echo(f"link failed: {[f.to_json() for f in res.failures]}", err=True)
            sys.exit(2)
        suite.extend(guard_mutants(s, res, layout=lay, contract=c, offsets=off,
                                   source="v6" if reg else "v5"))
    suite.extend(fact_link_mutants(s, items, layout=lay, contract=c, offsets=off))
    suite.extend(shared_variant_mutants(s, items, layout=lay, contract=c, offsets=off))
    doc = suite.to_json()
    doc.update(snapshot_id=s.snapshot_id, growable=len(items))
    _out(doc, out)
    if out:
        sm = suite.summary()
        click.echo(f"{sm['rejected']}/{sm['mutants']} rejected ({sm['mismatch']} mismatch); "
                   f"controls {sm['controls']} ok={sm['controls_ok']}")
    sys.exit(0 if suite.all_rejected and suite.controls_ok else 1)

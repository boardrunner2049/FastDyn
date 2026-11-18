"""``scopt return-edges`` (B3/B4 WP20): exit admission, facts, provenance, CalleeSummary, U-COND."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import click


def register(group: click.Group) -> None:
    group.add_command(return_edges)


def _out(doc: Any, out: str | None) -> None:
    text = json.dumps(doc, indent=1, sort_keys=True, default=str)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text + "\n")
    else:
        click.echo(text)


class Ctx:
    """Everything the WP20 analyses need for one snapshot (memoised per unit)."""

    def __init__(self, snapshot: str, *, profile: str | None, elf: str | None, prep: str | None,
                 modifiers: str | None, virtuals: str | None, min_profile_match: float = 0.95):
        from ..analysis.b3_inputs import resolve_for_snapshot
        from ..formats.envlayout_v1 import find_envlayout
        from ..importer.import_cache import open_snapshot

        self.s = open_snapshot(snapshot)
        self.layout = find_envlayout((self.s.runtime or {}).get("qemu_sha256"))
        self.image, self.plan = resolve_for_snapshot(self.s, elf=elf, prep=prep,
                                                     modifiers=modifiers, virtuals=virtuals)
        self.pv = None
        if profile:
            from ..formats.profile_v1 import ProfileView, read_profile

            self.pv = ProfileView.open(read_profile(profile), self.s,
                                       min_profile_match=min_profile_match)
        self._h: dict[str, Any] = {}
        self._f: dict[str, Any] = {}
        self._edges = None

    @property
    def page_bits(self) -> int:
        return self.s.header.target_page_bits

    def hostops(self, u):
        from ..analysis.hostops import build_hostops

        if u.uid not in self._h:
            self._h[u.uid] = build_hostops(u, self.layout, self.s.header.code_address)
        return self._h[u.uid]

    def facts(self, u):
        from ..analysis.exit_facts import propose_exit_facts

        if u.uid not in self._f:
            self._f[u.uid] = propose_exit_facts(self.hostops(u))
        return self._f[u.uid]

    def edges(self):
        from ..analysis.return_edges import build_return_edges

        if self._edges is None:
            self._edges = build_return_edges(
                self.s.units(), snapshot_id=self.s.snapshot_id, hostops_of=self.hostops,
                facts_of=self.facts, guest=self.image, plan=self.plan, profile_view=self.pv,
                page_bits=self.page_bits)
        return self._edges

    def certifier(self):
        """WP19's V14 certifier (``hostsym``) as WP20's :data:`Certifier`, with a
        per-call tally: ``True`` certified, ``False`` fact_derivation_mismatch,
        ``None`` inconclusive."""
        from ..validate_sym.link_check import summary_certifier

        inner = summary_certifier(self.s, self.layout)
        tally: Counter = Counter()

        def certify(u, es, f):
            r = inner(u, es, f)
            tally[{True: "certified", False: "fact_derivation_mismatch"}.get(r[0],
                                                                        "inconclusive")] += 1
            return r
        certify.tally = tally  # type: ignore[attr-defined]
        return certify

    def summaries(self, certify=None):
        from ..analysis.callee_summary import build_callee_summaries

        return build_callee_summaries(self.s.units(), self.edges(),
                                      snapshot_id=self.s.snapshot_id, hostops_of=self.hostops,
                                      facts_of=self.facts, guest=self.image, plan=self.plan,
                                      bundle=self.s.bundle, certify=certify,
                                      page_bits=self.page_bits)


def _inputs(f):
    f = click.option("--virtuals", type=click.Path(exists=True, dir_okay=False),
                     help="Generated virtuals.txt (checked against the manifest sha256).")(f)
    f = click.option("--modifiers", type=click.Path(exists=True, dir_okay=False),
                     help="Generated modifiers.txt (checked against the manifest sha256).")(f)
    f = click.option("--prep", type=click.Path(exists=True, file_okay=False),
                     help="scopt prepare dir: takes work/virtuals/{modifiers,virtuals}.txt.")(f)
    f = click.option("--elf", type=click.Path(exists=True, dir_okay=False),
                     help="Guest firmware ELF (sha256 must equal the bundle image_hash); "
                     "default: the snapshot's recorded firmware path.")(f)
    f = click.option("--profile", type=click.Path(exists=True, dir_okay=False),
                     help="Profile v1 of the base snapshot (steady phase ranks candidates).")(f)
    return f


def _ctx(snapshot, profile, elf, prep, modifiers, virtuals) -> Ctx:
    try:
        return Ctx(snapshot, profile=profile, elf=elf, prep=prep, modifiers=modifiers,
                   virtuals=virtuals)
    except Exception as e:  # report the stable code where there is one
        click.echo(f"{getattr(e, 'code', type(e).__name__)}: {e}", err=True)
        sys.exit(2)


@click.group("return-edges")
def return_edges() -> None:
    """Return/static exit admission, facts, provenance and CalleeSummary (design §3.1-3.3)."""


@return_edges.command("analyze")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False))
@_inputs
@click.option("--out", type=click.Path(dir_okay=False), help="Write the return-edges@1 JSON.")
@click.option("--brief", is_flag=True, help="Print only the stats.")
def analyze(snapshot, profile, elf, prep, modifiers, virtuals, out, brief):
    """return-edges@1 of SNAPSHOT: one row per goto_ptr / exit_null exit."""
    c = _ctx(snapshot, profile, elf, prep, modifiers, virtuals)
    doc = c.edges().to_json()
    if out:
        _out(doc, out)
    click.echo(json.dumps(doc["stats"] if brief or out else doc, indent=1, sort_keys=True))


@return_edges.command("facts")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False))
@click.argument("uids", nargs=-1)
@click.option("--vaddr", "vaddrs", multiple=True, help="Units starting at this vaddr (hex).")
def facts(snapshot, uids, vaddrs):
    """exit-facts@1 (the proposer) of the given units."""
    c = _ctx(snapshot, None, None, None, None, None)
    want = set(uids)
    vs = {int(v, 0) for v in vaddrs}
    rows = [c.facts(u).to_json() for u in c.s.units()
            if u.uid in want or (u.vaddrs and u.vaddrs[0] in vs)]
    _out(rows, None)


@return_edges.command("summaries")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False))
@_inputs
@click.option("--out-dir", required=True, type=click.Path(file_okay=False),
              help="Writes OUT_DIR/summaries/callee_summaries.jsonl.")
@click.option("--certify", is_flag=True,
              help="Certify every proposed fact with WP19 hostsym (V14); a disagreement is "
              "refused as fact_derivation_mismatch and makes the exit status 1.")
def summaries(snapshot, profile, elf, prep, modifiers, virtuals, out_dir, certify):
    """CalleeSummary v1 rows for every return unit of SNAPSHOT."""
    from ..formats.callee_summary_v1 import DEFAULT_RELPATH, write_callee_summaries

    c = _ctx(snapshot, profile, elf, prep, modifiers, virtuals)
    cert = c.certifier() if certify else None
    rows = c.summaries(certify=cert)
    res = write_callee_summaries(Path(out_dir) / DEFAULT_RELPATH, rows)
    reasons = Counter(r["reason"] for row in rows for r in row["refused"])
    sources = Counter(f["source"] for row in rows for f in row["facts"])
    res.update(refused=dict(sorted(reasons.items())), sources=dict(sorted(sources.items())),
               callee_named=sum(1 for r in rows if r["callee"] is not None))
    if cert is not None:
        res["certifier"] = dict(sorted(cert.tally.items()))
        res["status"] = dict(sorted(Counter(f["status"] for row in rows
                                            for f in row["facts"]).items()))
    click.echo(json.dumps(res, indent=1, sort_keys=True))
    if cert is not None and cert.tally.get("fact_derivation_mismatch"):
        sys.exit(1)


@return_edges.command("xcheck")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False))
@click.option("--return-only", is_flag=True,
              help="Only units that have a return-class exit (the CalleeSummary rows).")
@click.option("--out", type=click.Path(dir_okay=False), help="Write the full JSON here.")
def xcheck(snapshot, return_only, out):
    """Two-sided proposer/hostsym agreement on every lookup exit and fact field.

    Exit 1 when any proposed fact disagrees with hostsym (fact_derivation_mismatch);
    'proposer_weaker' (hostsym derives a constant the proposer refused) is reported."""
    from ..analysis.fact_xcheck import xcheck_snapshot

    c = _ctx(snapshot, None, None, None, None, None)
    keep = None
    if return_only:
        ret = {r.uid for r in c.edges().exits if r.exit_class == "return"}
        keep = lambda u: u.uid in ret  # noqa: E731
    doc = xcheck_snapshot(c.s.units(), c.facts, c.s, c.layout, uid_filter=keep)
    doc["snapshot"] = c.s.snapshot_id
    if out:
        _out(doc, out)
    brief = {k: v for k, v in doc.items() if k != "rows"}
    brief["proposer_weaker_rows"] = [r for r in doc["rows"] if r["outcome"] == "proposer_weaker"][:20]
    brief["mismatch_rows"] = [r for r in doc["rows"]
                              if r["outcome"] == "fact_derivation_mismatch"][:20]
    click.echo(json.dumps(brief, indent=1, sort_keys=True))
    sys.exit(1 if doc["mismatch"] else 0)


@return_edges.command("ucond")
@click.argument("snapshots", nargs=-1, type=click.Path(exists=True, file_okay=False))
@click.option("--elf", "elfs", multiple=True, type=click.Path(exists=True, dir_okay=False),
              help="Static scan of a firmware ELF (the fixtures have no capture).")
@click.option("--out", type=click.Path(dir_okay=False))
def ucond(snapshots, elfs, out):
    """U-COND (T-B3-42): condexec == 0 at every return exit (metadata + decode; ELF scan)."""
    from ..analysis.return_edges import ucond_elf_scan
    from ..decode.guest_symbols import load_guest_image

    doc: dict[str, Any] = {"snapshots": [], "elfs": []}
    ok = True
    for sp in snapshots:
        c = _ctx(sp, None, None, None, None, None)
        e = c.edges()
        bad = [u.to_json() for u in e.ucond if u.ok is not True]
        doc["snapshots"].append({
            "snapshot": c.s.snapshot_id, "path": sp, "return_exits": len(e.ucond),
            "decode_replay": dict(Counter(u.decode_replay for u in e.ucond)),
            "counterexamples": bad, "pass": not bad and bool(e.ucond)})
        ok &= not bad and bool(e.ucond)
    for ep in elfs:
        r = ucond_elf_scan(load_guest_image(ep))
        doc["elfs"].append(r)
        ok &= r["pass"]
    doc["pass"] = ok
    _out(doc, out)
    if out:
        click.echo(json.dumps({"pass": ok}, sort_keys=True))
    sys.exit(0 if ok else 1)


@return_edges.command("stability")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False))
@click.option("--train", required=True, type=click.Path(exists=True, dir_okay=False),
              help="Train profile (ranks the candidates).")
@click.option("--heldout", required=True, type=click.Path(exists=True, dir_okay=False),
              help="Held-out profile of the same snapshot.")
@click.option("-k", default=2, show_default=True, help="Guarded targets per exit.")
@click.option("--elf", type=click.Path(exists=True, dir_okay=False))
@click.option("--prep", type=click.Path(exists=True, file_okay=False))
@click.option("--out", type=click.Path(dir_okay=False))
def stability(snapshot, train, heldout, k, elf, prep, out):
    """U-HELD (reported outcome): held-out stability of the train-ranked edges."""
    from ..analysis.return_edges import edge_stability

    a = _ctx(snapshot, train, elf, prep, None, None)
    b = _ctx(snapshot, heldout, elf, prep, None, None)
    doc = edge_stability(a.edges(), b.edges(), k=k)
    doc["train_coverage"] = a.pv.coverage if a.pv else None
    doc["heldout_coverage"] = b.pv.coverage if b.pv else None
    _out(doc, out)
    if out:
        click.echo(json.dumps(doc, indent=1, sort_keys=True))

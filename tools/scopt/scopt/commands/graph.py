"""``scopt graph export|import`` (design §4.4, §5.11). WP7."""

from __future__ import annotations

import json
import sys

import click


def register(group: click.Group) -> None:
    group.add_command(graph)


@click.group()
def graph() -> None:
    """Known control-flow edges of a snapshot (graph.json)."""


@graph.command("export")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False))
@click.option("--out", required=True, type=click.Path(dir_okay=False))
@click.option("--unit", "units", multiple=True, help="Export only these UnitIds (a subset).")
@click.option("--slot", "slots", multiple=True, type=int, help="Export only these slots.")
@click.option("--profile", "profiles", multiple=True, type=click.Path(exists=True),
              help="Profile v1 JSON whose edges become observed rows.")
def export_cmd(snapshot, out, units, slots, profiles):
    """Export SNAPSHOT (a store snapshot directory) to graph.json."""
    from ..graph import GraphError, export_graph, graph_bytes
    from ..importer.import_cache import ImportRejected, open_snapshot

    try:
        snap = open_snapshot(snapshot)
    except ImportRejected as e:
        click.echo(f"cannot open snapshot: {e}", err=True)
        sys.exit(2)
    sel = list(units) + [snap.by_slot(s).uid for s in slots]
    profs = [json.loads(open(p, encoding="utf-8").read()) for p in profiles]
    try:
        doc = export_graph(snap, units=sel or None, profiles=profs)
    except GraphError as e:
        click.echo(f"graph export failed: {e}", err=True)
        sys.exit(2)
    with open(out, "wb") as f:
        f.write(graph_bytes(doc))
    res = {}
    for e in doc["edges"]:
        res[e["resolution"]] = res.get(e["resolution"], 0) + 1
    click.echo(f"{len(doc['nodes'])} nodes, {len(doc['edges'])} edges: "
               + ", ".join(f"{k}={v}" for k, v in sorted(res.items())))


@graph.command("import")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--check", "check", type=click.Path(exists=True, file_okay=False),
              help="Snapshot directory to check the graph against.")
def import_cmd(path, check):
    """Validate PATH; exit 1 unless export(import(PATH)) == PATH (and --check passes)."""
    from ..graph import GraphError, check_graph, graph_bytes, load_graph

    data = open(path, "rb").read()
    try:
        doc = load_graph(data)
    except GraphError as e:
        click.echo(f"invalid graph: {e}", err=True)
        sys.exit(1)
    ok = graph_bytes(doc) == data
    click.echo(f"round trip: {'byte-identical' if ok else 'DIFFERS'}")
    errs: list[str] = []
    if check:
        from ..importer.import_cache import open_snapshot

        errs = check_graph(doc, open_snapshot(check))
        click.echo("check: " + ("ok" if not errs else "; ".join(errs[:6])))
    sys.exit(0 if ok and not errs else 1)

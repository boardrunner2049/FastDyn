"""``scopt report``: render a report from whatever measurements exist (WP13, §5.11, spec §6)."""

from __future__ import annotations

import sys
import traceback

import click


def register(group: click.Group) -> None:
    group.add_command(report_cmd)


@click.command("report")
@click.argument("workspace", nargs=-1, type=click.Path())
@click.option("--out", "out", type=click.Path(dir_okay=False),
              help="Markdown output (default: WORKSPACE/report.md). report.json and "
              "timing.csv are written next to it.")
@click.option("--profile", "profiles", multiple=True, type=click.Path(),
              help="Profile v1 file for the hot-set model (default: profiles found in "
              "WORKSPACE).")
@click.option("--ledger", "ledgers", multiple=True, type=click.Path(),
              help="Extra ledger.jsonl files.")
@click.option("--artifact", "artifacts", multiple=True, type=click.Path(),
              help="Extra artifact directories.")
@click.option("--sc", "scs", multiple=True, metavar="LABEL=PATH",
              help="Extra .sc files for the size and hot-set tables.")
@click.option("--reference", default=None, help="Reference arm (default: the campaign's, "
              "else warm).")
@click.option("--bootstrap", type=int, default=10000, show_default=True,
              help="Bootstrap resamples for the CIs.")
@click.option("--seed", type=int, default=0, show_default=True, help="Bootstrap seed.")
@click.option("--hot-coverage", type=float, default=0.90, show_default=True,
              help="Share of steady-phase entries the hot set must cover.")
@click.option("--phase", default="steady", show_default=True)
@click.option("--b5-campaign", "b5_campaign", type=click.Path(dir_okay=False), default=None,
              help="B5 campaign (campaigns/b5-*.toml, definition or frozen): add the B5 "
              "section (every campaign arm per split incl. skipped_unsupported; design "
              "10.2 metric rows). Default: a b5-campaign.toml in WORKSPACE.")
def report_cmd(workspace, out, profiles, ledgers, artifacts, scs, reference, bootstrap, seed,
               hot_coverage, phase, b5_campaign):
    """Build the report from WORKSPACE directories (campaigns, run dirs, ledgers,
    artifacts, profiles). Always succeeds: missing or unsupported parts become
    not_run/unsupported rows."""
    from pathlib import Path

    from ..report.collect import collect
    from ..report.render import write_report

    roots = list(workspace) or ["."]
    if out is None:
        base = Path(roots[0])
        out = str((base if base.is_dir() else base.parent) / "report.md")
    sc_map = {}
    for s in scs:
        label, _, path = s.partition("=")
        if not path:
            label, path = Path(s).name, s
        sc_map[label] = path
    try:
        model = collect(roots, profiles=profiles, ledgers=ledgers, artifacts=artifacts,
                        scs=sc_map, reference=reference, bootstrap=bootstrap, seed=seed,
                        hot_coverage=hot_coverage, phase=phase, b5_campaign=b5_campaign)
    except Exception as e:  # noqa: BLE001 - the report must always be produced
        model = {"schema": "scopt.report/1", "roots": roots,
                 "problems": [{"where": "collect", "problem": f"{type(e).__name__}: {e}",
                               "traceback": traceback.format_exc()[-2000:]}]}
    try:
        paths = write_report(model, out)
    except Exception as e:  # noqa: BLE001
        model.setdefault("problems", []).append(
            {"where": "render", "problem": f"{type(e).__name__}: {e}"})
        import json
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(f"# scopt report\n\nrendering failed: {type(e).__name__}: {e}\n"
                             "\nThe collected model is in report.json.\n")
        jp = Path(out).with_suffix(".json")
        jp.write_text(json.dumps(model, indent=1, sort_keys=True, default=str) + "\n")
        paths = {"markdown": out, "json": str(jp)}
    click.echo(f"report: {paths['markdown']}")
    for k in ("json", "csv"):
        if k in paths:
            click.echo(f"{k}: {paths[k]}")
    n = len(model.get("problems") or [])
    if n:
        click.echo(f"collection problems: {n} (listed in the report)")
    sys.exit(0)

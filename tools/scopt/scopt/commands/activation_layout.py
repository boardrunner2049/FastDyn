"""Propose bounded native TB placement from an ordinary training trace."""
from pathlib import Path

import click

from ..formats.artifact import write_json_file


def register(group: click.Group) -> None:
    group.add_command(plan_activation_layout_cmd)


_file = click.Path(exists=True, dir_okay=False, path_type=Path)


@click.command("plan-activation-layout")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--qemu", required=True, type=_file, help="Exact x86-64 QEMU ELF with debug information.")
@click.option("--config", required=True, type=_file, help="Frozen training launch configuration.")
@click.option("--receipt", required=True, type=_file, help="Verified ordinary -d exec trace receipt.")
@click.option("--protocol", required=True, type=_file, help="Frozen untimed training protocol.")
@click.option("--terminal-evidence", type=_file, help="Evidence for a chained benchmark_end marker.")
@click.option("--max-moves", type=click.IntRange(1, 8), default=8, show_default=True)
@click.option("--out", required=True, type=click.Path(dir_okay=False, path_type=Path),
              help="Placement parameters for optimize --pipeline activation-layout.")
@click.option("--report", required=True, type=click.Path(dir_okay=False, path_type=Path),
              help="Trace provenance and before/after model scores.")
def plan_activation_layout_cmd(snapshot, qemu, config, receipt, protocol, terminal_evidence,
                               max_moves, out, report) -> None:
    """Select up to eight placements using untimed ordinary restore and link events."""
    from ..analysis.activation_layout import plan_activation_layout
    from ..importer.import_cache import open_snapshot

    if out.resolve() == report.resolve():
        raise click.ClickException("parameter and report paths must be distinct")
    try:
        params, result = plan_activation_layout(
            open_snapshot(snapshot), qemu=qemu, config=config, receipt=receipt,
            protocol=protocol, terminal=terminal_evidence, max_moves=max_moves)
        write_json_file(out, params)
        write_json_file(report, result)
    except (OSError, ValueError) as error:
        raise click.ClickException(str(error)) from error

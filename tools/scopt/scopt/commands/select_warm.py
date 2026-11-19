"""Performance selection for a FastDyn warm-cache deployment."""
from __future__ import annotations

import json
from pathlib import Path

import click

from ..run.warm_selection import DEFAULT_ALPHA, TimingInput, select_warm_cache


def register(group):
    group.add_command(select_warm)


def _cases(path):
    if path is None:
        return [], []
    doc = json.loads(path.read_text())
    def resolve(value):
        return (path.parent / value).resolve(strict=True)
    cases = [TimingInput(row['name'], resolve(row['config']),
                         {bank: resolve(p) for bank, p in row.get('bank_inputs', {}).items()},
                         resolve(row['expected_guest'])) for row in doc['inputs']]
    return cases, [resolve(p) for p in doc.get('training_inputs', [])]


def selection_timing_options(command):
    """Use the same timing protocol on standalone and end-to-end optimization."""
    options = (
        click.option('--qemu', type=click.Path(path_type=Path, exists=True, dir_okay=False)),
        click.option('--pairs', type=click.IntRange(min=12), default=12, show_default=True),
        click.option('--seed', type=int, default=193483, show_default=True),
        click.option('--alpha', type=click.FloatRange(min=0, max=DEFAULT_ALPHA, min_open=True),
                     default=DEFAULT_ALPHA, show_default=True, help='Maximum one-sided p-value per timing case.'),
        click.option('--timeout', type=click.FloatRange(min=0, min_open=True), default=90.0),
    )
    for option in reversed(options):
        command = option(command)
    return command


@click.command('select-warm')
@click.argument('warm', type=click.Path(path_type=Path, exists=True))
@click.argument('candidate', type=click.Path(path_type=Path, exists=True))
@click.option('--cases', type=click.Path(path_type=Path, exists=True, dir_okay=False),
              help='JSON timing inputs, configurations, expected guest outputs and training inputs.')
@click.option('--out', required=True, type=click.Path(path_type=Path, file_okay=False),
              help='Fresh evidence directory. Its selection.json can be passed to scopt deploy.')
@selection_timing_options
@click.option('--json', 'as_json', is_flag=True)
def select_warm(warm, candidate, cases, out, qemu, pairs, seed, timeout, alpha, as_json):
    """Choose between the original WARM cache and a packaged CANDIDATE for deployment."""
    try:
        inputs, training = _cases(cases)
        result = select_warm_cache(warm, candidate, out, inputs=inputs, training_inputs=training,
                                   qemu=qemu, pairs=pairs, seed=seed, timeout_s=timeout, alpha=alpha)
    except (KeyError, ValueError, TypeError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(result, sort_keys=True, indent=2))
    else:
        click.echo(f"{result['reason']}  {result.get('selected_artifact') or ''}")
    if result['status'] == 'error':
        raise click.exceptions.Exit(2)

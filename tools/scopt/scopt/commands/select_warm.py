"""Performance selection for a FastDyn warm-cache deployment."""
from __future__ import annotations

import json
from pathlib import Path

import click

from ..run.warm_selection import DEFAULT_ALPHA, TimingInput, select_warm_cache


def register(group):
    group.add_command(select_warm)
    group.add_command(tune_warm)


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


@click.command('tune-warm')
@click.argument('warm', type=click.Path(path_type=Path, exists=True))
@click.argument('candidates', nargs=-1, required=True, type=click.Path(path_type=Path, exists=True))
@click.option('--training-cases', required=True, type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option('--cases', required=True, type=click.Path(path_type=Path, exists=True, dir_okay=False),
              help='Untouched confirmation cases, in the select-warm JSON format.')
@click.option('--training-pairs', type=click.IntRange(min=12), default=12, show_default=True)
@click.option('--out', required=True, type=click.Path(path_type=Path, file_okay=False))
@selection_timing_options
@click.option('--json', 'as_json', is_flag=True)
def tune_warm(warm, candidates, training_cases, cases, training_pairs, out, qemu, pairs, seed,
              timeout, alpha, as_json):
    """Rank complete CANDIDATES on training inputs, then confirm the winner against WARM."""
    from ..run.warm_tuning import tune_warm_cache

    try:
        training, train_excluded = _cases(training_cases)
        held_out, confirm_excluded = _cases(cases)
        result = tune_warm_cache(warm, candidates, out, training=training, confirmation=held_out,
                                 earlier_inputs=train_excluded, qemu=qemu, pairs=pairs,
                                 training_pairs=training_pairs, seed=seed, timeout_s=timeout, alpha=alpha,
                                 confirmation_exclusions=confirm_excluded)
    except (KeyError, ValueError, TypeError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, sort_keys=True, indent=2) if as_json else
               f"{result['reason']}  {result['selected_artifact'] or ''}\nselection: {result['selection']}")
    if result['status'] == 'error':
        raise click.exceptions.Exit(2)

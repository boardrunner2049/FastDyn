"""CLI for certified restore-hints artifacts."""
import json
from pathlib import Path

import click


def register(group):
    group.add_command(restore_hints)


@click.group("restore-hints")
def restore_hints():
    """Optional callback restore hints for unchanged v5 caches."""


@restore_hints.command("package")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--config", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--qemu", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--store", required=True, type=click.Path(file_okay=False, path_type=Path))
@click.option("--json", "as_json", is_flag=True)
def package(snapshot, config, qemu, store, as_json):
    """Certify SNAPSHOT and package its eligible restore nomination rows."""
    from ..formats.artifact import ArtifactError
    from ..importer.join import ImportRejected
    from ..run.restore_hints import package_restore_hints

    try:
        result = package_restore_hints(snapshot, config, qemu, store)
    except (ArtifactError, ImportRejected, KeyError, OSError, TypeError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    if as_json:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        click.echo(f"{result['artifact_dir']}  {result['admitted_count']}/{result['record_count']} hints")

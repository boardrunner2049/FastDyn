"""``scopt contracts``: show and self-check the contracts frozen by WP0."""

from __future__ import annotations

import json

import click


def register(group: click.Group) -> None:
    group.add_command(contracts)


@click.group()
def contracts() -> None:
    """Frozen binary layouts, enums and JSON schemas."""


@contracts.command("check")
def check() -> None:
    """Validate schema files and layout sizes; exit 1 on any problem."""
    from ..formats import layouts, schemas

    problems = []
    for name in schemas.SCHEMA_NAMES:
        try:
            schemas.validator(name)
        except Exception as e:  # noqa: BLE001 - report every failure
            problems.append(f"schema {name}: {e}")
    for name, size in layouts.EXPECTED_SIZES.items():
        if getattr(layouts, name).size != size:
            problems.append(f"layout {name}: {getattr(layouts, name).size} != {size}")
    for p in problems:
        click.echo(p, err=True)
    click.echo("contracts: " + ("FAIL" if problems else "ok"))
    if problems:
        raise SystemExit(1)


@contracts.command("show")
def show() -> None:
    """Print layout sizes and enum values as JSON."""
    from ..formats import enums, layouts

    out = {
        "layouts": {k: {"size": getattr(layouts, k).size, "format": getattr(layouts, k).format}
                    for k in layouts.EXPECTED_SIZES},
        "enums": {name: {m.name: int(m) for m in getattr(enums, name)}
                  for name in ("SiteKind", "Encoding", "OpMarkKind", "TbFlag", "ChunkType",
                               "QprofEdgeKind", "QprofEdgeFlag", "QprofHeaderFlag")},
    }
    click.echo(json.dumps(out, indent=2))

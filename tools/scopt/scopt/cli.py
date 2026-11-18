"""``scopt`` command line.

Subcommands are discovered from ``scopt/commands/*.py``: every module there that
defines ``register(group: click.Group) -> None`` gets to add its commands. Each
work package adds its own module (``commands/sc.py``, ``commands/meta.py``, …)
instead of editing this file, so parallel branches do not conflict.
"""

from __future__ import annotations

import importlib
import pkgutil

import click

from . import __version__


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="scopt")
def cli() -> None:
    """Offline optimizer for FastDyn QEMU supercache snapshots."""


_loaded = False


def load_commands(group: click.Group = cli) -> click.Group:
    global _loaded
    if _loaded:
        return group
    from . import commands as pkg

    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{pkg.__name__}.{info.name}")
        register = getattr(mod, "register", None)
        if register is not None:
            register(group)
    _loaded = True
    return group


def main(argv: list[str] | None = None) -> None:
    load_commands()
    cli.main(args=argv, prog_name="scopt")


if __name__ == "__main__":  # pragma: no cover
    main()

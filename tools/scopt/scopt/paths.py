"""Default locations of the FastDyn workspace and of the FastDyn checkout.

Two directories matter to scopt:

* the **workspace** (``$FASTDYN_ROOT``): where recorded runs, fixtures and the
  maintainer's QEMU builds live (``runs/``, ``fixtures/``, ``wt/``, ``src/``).
  scopt only reads optional evidence from it (for example the WP22 gate-binary
  record); every pipeline command takes its inputs and outputs as arguments.
* the **FastDyn checkout** (``$SCOPT_FASTDYN_SRC``): the directory that holds
  ``fastdyn-env/bin/fastdyn`` and ``build/libfastdyn.so``; FastDyn runs are
  launched with it as the working directory.

Without the environment variables the defaults are repository-relative: when
this package is the ``tools/scopt`` directory of a FastDyn checkout, the
workspace defaults to the directory that holds ``tools/`` and the FastDyn
checkout defaults to that same directory. Otherwise the workspace defaults to
``~/scopt-work`` and the checkout to ``$FASTDYN_ROOT/src/fastdyn``. Paths under
a default that does not exist are simply absent: callers report them as missing
(and tests skip), they never fall back to another machine's layout.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["SCOPT_TREE", "repo_root", "fastdyn_root", "fastdyn_src"]

#: The ``tools/scopt`` source tree (or the install prefix for a non-editable install).
SCOPT_TREE = Path(__file__).resolve().parents[1]


def repo_root() -> Path | None:
    """The directory that holds ``tools/scopt`` when this package runs from a
    source tree laid out as ``<root>/tools/scopt``; else ``None``."""
    if SCOPT_TREE.parent.name == "tools" and (SCOPT_TREE / "pyproject.toml").is_file():
        return SCOPT_TREE.parent.parent
    return None


def _is_fastdyn_checkout(p: Path) -> bool:
    return (p / "setup.sh").is_file() and (p / "src").is_dir() and (p / "Makefile").is_file()


def fastdyn_root() -> Path:
    """``$FASTDYN_ROOT``, else the repository-relative default (see module doc)."""
    env = os.environ.get("FASTDYN_ROOT")
    if env:
        return Path(env)
    r = repo_root()
    return r if r is not None else Path.home() / "scopt-work"


def fastdyn_src() -> Path:
    """``$SCOPT_FASTDYN_SRC``, else the FastDyn checkout that contains this
    package, else ``$FASTDYN_ROOT/src/fastdyn``."""
    env = os.environ.get("SCOPT_FASTDYN_SRC")
    if env:
        return Path(env)
    r = repo_root()
    if r is not None and _is_fastdyn_checkout(r):
        return r
    return fastdyn_root() / "src/fastdyn"

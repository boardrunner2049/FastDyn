"""Locate the FastDyn checkout and the files FastDyn uses by default."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

REPO_ROOT_ENV = "FASTDYN_REPO_ROOT"
SVD_DIR_RELATIVE = "third_party/common/cmsis-svd-data"
PLUGIN_LIBRARY_RELATIVE = "build/libfastdyn.so"

_PACKAGE_DIR = Path(__file__).resolve().parent


def repo_root() -> Optional[Path]:
    """Return the FastDyn checkout, or None when it cannot be determined.

    ``$FASTDYN_REPO_ROOT`` wins when set. Otherwise this is the checkout the
    package lives in (``src/fastdyn/../..``), recognised by its ``meson.build``
    and ``virtuals/``.
    """
    env_root = os.environ.get(REPO_ROOT_ENV)
    if env_root:
        return Path(env_root).expanduser().resolve()
    candidate = _PACKAGE_DIR.parents[1]
    if (candidate / "meson.build").is_file() and (candidate / "virtuals").is_dir():
        return candidate
    return None


def virtuals_dir() -> Optional[Path]:
    """Return the directory holding the virtual plugins, or None without a checkout."""
    root = repo_root()
    if root is None or not (root / "virtuals").is_dir():
        return None
    return root / "virtuals"


def default_svd_dir() -> str:
    """CMSIS-SVD data in the checkout, or the relative path used without one."""
    root = repo_root()
    return str(root / SVD_DIR_RELATIVE) if root is not None else SVD_DIR_RELATIVE


def default_plugin_library() -> str:
    """The FastDyn QEMU plugin in the checkout, or the relative path used without one."""
    root = repo_root()
    return str(root / PLUGIN_LIBRARY_RELATIVE) if root is not None else PLUGIN_LIBRARY_RELATIVE

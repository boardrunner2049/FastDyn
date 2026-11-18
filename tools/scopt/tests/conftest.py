"""Shared pytest fixtures. Add WP-specific fixtures in a conftest.py inside your
own test subdirectory rather than here, to keep merges conflict-free."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

FASTDYN_ROOT = Path(os.environ.get("FASTDYN_ROOT", "/data/extra_copy/fastdyn"))
MINIMAL_SC = FASTDYN_ROOT / "runs/b0/cache/minimal.sc"  # read-only golden (B0)
SCPARSE = Path(__file__).resolve().parents[1] / "independent/scparse.py"
TESTS_DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="session")
def fastdyn_root() -> Path:
    return FASTDYN_ROOT


@pytest.fixture(scope="session")
def minimal_sc() -> Path:
    """Path of the read-only B0 golden cache; skips if it is not present."""
    if not MINIMAL_SC.exists():
        pytest.skip(f"{MINIMAL_SC} not present")
    return MINIMAL_SC


@pytest.fixture(scope="session")
def scparse_path() -> Path:
    if not SCPARSE.exists():
        pytest.skip(f"{SCPARSE} not present")
    return SCPARSE


@pytest.fixture(scope="session")
def tests_data() -> Path:
    return TESTS_DATA

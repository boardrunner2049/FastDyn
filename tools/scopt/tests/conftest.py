"""Shared pytest fixtures. Add WP-specific fixtures in a conftest.py inside your
own test subdirectory rather than here, to keep merges conflict-free."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from scopt.paths import fastdyn_root

FASTDYN_ROOT = fastdyn_root()
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


@pytest.fixture
def fake_runtime_qemu(tmp_path_factory, monkeypatch) -> Path:
    """A stand-in for the integrated runtime QEMU, for tests that create run
    directories without naming a QEMU but never start one (fake launcher,
    refusals before launch). It is bound through ``SCOPT_RUNTIME_QEMU`` and
    ``SCOPT_RUNTIME_QEMU_SHA256``, so the sha256 check still runs; the
    maintainer's build under ``$FASTDYN_ROOT/wt/qemu-int`` is not needed."""
    import hashlib

    from scopt.run import runtime as R

    q = tmp_path_factory.mktemp("runtime") / "qemu-system-arm"
    q.write_bytes(b"\x7fELF stand-in for the integrated runtime (never executed)\n")
    monkeypatch.setenv(R.OVERRIDE_ENV, str(q))
    monkeypatch.setenv(R.OVERRIDE_SHA_ENV, hashlib.sha256(q.read_bytes()).hexdigest())
    return q

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
def minimal_sc(tmp_path_factory) -> Path:
    """Path of the read-only B0 golden cache; skips if it is not present."""
    if not MINIMAL_SC.exists():
        pytest.skip(f"{MINIMAL_SC} not present")
    # The immutable B0 record corpus predates v7. Build a canonical test
    # fixture from its known public records without changing the archive.
    from scopt.formats.layouts import SC_CODE_OFFSET, SC_VERSION
    from scopt.formats.sc_records import HEADER_SIZE, RECORD_SIZE, ScHeader, ScRecord
    from scopt.formats.supercache import write_public_cache

    with MINIMAL_SC.open("rb") as stream:
        header = ScHeader.from_bytes(stream.read(HEADER_SIZE))
        if header.version == SC_VERSION:
            return MINIMAL_SC
        if (header.version, header.checksum, header.record_count) != (5, 0x1A7C702B57653DE8, 163):
            pytest.fail("unrecognized minimal.sc fixture")
        stream.seek(header.records_offset)
        records = [stream.read(RECORD_SIZE) for _ in range(header.record_count)]
        parts = []
        for raw in records:
            record = ScRecord.from_bytes(raw)
            stream.seek(SC_CODE_OFFSET + record.tc_offset)
            parts.append((raw, stream.read(record.end_offset - record.tc_offset)))
    path = tmp_path_factory.mktemp("canonical-golden") / "minimal.sc"
    write_public_cache(path, header.replace(version=SC_VERSION), parts)
    return path


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

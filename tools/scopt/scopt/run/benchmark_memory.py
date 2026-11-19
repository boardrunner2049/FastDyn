"""Replay the RAM placement recorded by a fresh benchmark fixture."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import os
from pathlib import Path
import tomllib

from .toml_rewrite import memory_banks


def benchmark_ram_override(config: Path, default: str | None,
                           environ: Mapping[str, str]) -> str | None:
    """Return a default only for file-backed private banks with no explicit override."""
    if not default or "SCOPT_RAM_ROOT" in environ:
        return None
    banks = memory_banks(tomllib.loads(config.read_text(encoding="utf-8")))
    return default if banks and all(bank.is_file and not bank.share for bank in banks) else None


@contextmanager
def benchmark_ram(config: Path, default: str | None) -> Iterator[str | None]:
    """Apply the safe fallback for one benchmark operation and its subprocesses."""
    override = benchmark_ram_override(config, default, os.environ)
    if override is not None:
        os.environ["SCOPT_RAM_ROOT"] = override
    try:
        yield os.environ.get("SCOPT_RAM_ROOT") or None
    finally:
        if override is not None:
            os.environ.pop("SCOPT_RAM_ROOT", None)

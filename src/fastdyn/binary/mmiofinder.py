"""Compatibility adapter for hardcoded MMIO access discovery.

The reusable control-flow and constant-propagation analysis lives in
``fastdyn.host``. This module retains the historical range-filtered API.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from fastdyn.host.constants import KnownMemoryAccess, find_known_memory_accesses


MMIOAccess = KnownMemoryAccess


def find_mmio_accesses(*, blob: bytes, base_va: int, arch: str,
                       mmio_regions: List[Tuple[int, int]], mode: str = "thumb",
                       entry_points: Optional[Iterable[int]] = None) -> List[MMIOAccess]:
    """Return known accesses whose effective address is in an MMIO range."""
    accesses = find_known_memory_accesses(
        blob=blob, base_va=base_va, arch=arch, mode=mode,
        entry_points=entry_points,
    )
    return [access for access in accesses
            if any(start <= access.address <= end for start, end in mmio_regions)]

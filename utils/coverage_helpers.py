"""Shared parsing primitives for FastDyn's standalone coverage reports."""
from __future__ import annotations

import bisect
import re
from pathlib import Path
from collections.abc import Callable, Iterable

_INSN_RE = re.compile(r"^\s*([0-9a-fA-F]+):")
_GROWTH_RE = re.compile(r"\[TB\]\s+Unique blocks\s*=\s*(\d+)\s+at time\s+(\d+)")


def disassembly_pcs(lines: Iterable[str]) -> list[int]:
    """Return sorted, de-duplicated instruction PCs from objdump text."""
    return sorted({int(match.group(1), 16) & ~1 for line in lines
                   if (match := _INSN_RE.match(line))})


def load_disassembly_pcs(path: str | Path) -> list[int]:
    with Path(path).open("r", errors="replace") as stream:
        return disassembly_pcs(stream)


def executed_blocks(lines: Iterable[str], *, strict: bool = False) -> dict[int, int]:
    """Parse ``PC<TAB>instruction-count`` rows; the last duplicate wins."""
    blocks = {}
    for number, raw in enumerate(lines, 1):
        text = raw.strip()
        if not text:
            continue
        parts = text.split("\t")
        try:
            if len(parts) < 2:
                raise ValueError("missing instruction count")
            pc, count = int(parts[0], 16) & ~1, int(parts[1])
            if count < 0:
                raise ValueError("negative instruction count")
            blocks[pc] = count
        except ValueError as error:
            if strict:
                raise ValueError(f"malformed coverage row {number}: {error}") from error
    return blocks


def load_executed_blocks(path: str | Path, *, strict: bool = False) -> dict[int, int]:
    with Path(path).open("r", errors="replace") as stream:
        return executed_blocks(stream, strict=strict)


def expand_blocks(blocks: dict[int, int], pcs: list[int], *,
                  missing: Callable[[int], None] | None = None,
                  strict_bounds: bool = False) -> set[int]:
    """Expand block starts/counts without indexing beyond disassembly bounds."""
    executed = set()
    for block_pc, block_len in blocks.items():
        index = bisect.bisect_left(pcs, block_pc)
        if index >= len(pcs) or pcs[index] != block_pc:
            if missing:
                missing(block_pc)
            continue
        end = index + block_len
        if strict_bounds and end > len(pcs):
            raise ValueError(f"block at {block_pc:#x} extends beyond the disassembly")
        executed.update(pcs[index:min(end, len(pcs))])
    return executed


def coverage_growth(lines: Iterable[str]) -> dict[int, int]:
    """Return first timestamp for each unique-block count, normalized to zero."""
    result = {}
    for line in lines:
        match = _GROWTH_RE.search(line)
        if match:
            result.setdefault(int(match.group(1)), int(match.group(2)))
    if result:
        first = min(result.values())
        result = {count: timestamp - first for count, timestamp in result.items()}
    return result


def load_coverage_growth(path: str | Path) -> dict[int, int]:
    with Path(path).open("r", errors="ignore") as stream:
        return coverage_growth(stream)

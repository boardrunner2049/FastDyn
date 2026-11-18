"""Recovery-point oracle (design §2.7 step 7, T-B1-8; the B3 recovery oracle). WP7.

When generated code faults or calls a helper that unwinds (``GETPC()``), QEMU
maps the host return address back to a guest instruction with
``cpu_unwind_data_from_tb`` (translate-all.c:137-169), using only the TB's
search data. This module checks, independently of the search data, that the
instruction it finds is the one that issued the operation, as recorded at
emission time by the capture's OpMarks:

* **raddr** — for each ``Ldst`` with ordinal ``k``, the ``k``-th QEMU_LD /
  QEMU_ST OpMark's ``insn_idx`` must equal ``unwind(raddr_off)``. The slow-path
  helper receives ``ra = raddr``, so this also covers LDST_HELPER_CALL.
* **helper_call** — for each HELPER_CALL site, the return offset (the end of
  the call instruction: ``insn_off + 5`` for rel32, ``+ 6`` for a
  pool-indirect call) must satisfy ``unwind(ret_off) == insn_idx`` of its CALL
  OpMark (the last CALL OpMark at or before the call whose ``helper_id`` is
  the site's helper).

A mismatch gives reason ``recovery_mismatch`` (the importer pins the TB). An
OpMark ``insn_idx`` of 0xFFFF (before the first insn_start) is compared as
insn 0, which is where ``cpu_unwind_data_from_tb`` attributes such offsets.

Each check also yields a :class:`scopt.model.unit.RecoveryPoint` (one per
faultable host return offset), sorted by offset, ids ``r0``, ``r1``, ...
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..decode.search import SearchRow, unwind
from ..formats.enums import Encoding, OpMarkKind, SiteKind
from ..model.unit import Ldst, OpMark, RecoveryPoint, Site

__all__ = ["RecoveryCheck", "check_recovery", "RECOVERY_MISMATCH", "NONE_U16"]

RECOVERY_MISMATCH = "recovery_mismatch"
NONE_U16 = 0xFFFF


@dataclass(frozen=True, slots=True)
class RecoveryCheck:
    """One checked return offset."""

    source: str  # "raddr" | "helper_call"
    host_return_offset: int
    expected_insn: int | None  # from the OpMark (None: no OpMark found)
    unwound_insn: int | None  # from the search data (None: QEMU would return -1)
    words: tuple[int, ...] | None
    ref: int  # ldst index or site index

    @property
    def ok(self) -> bool:
        return self.expected_insn is not None and self.expected_insn == self.unwound_insn

    def detail(self) -> str:
        return (f"{self.source}[{self.ref}] ret_off={self.host_return_offset}: opmark insn "
                f"{self.expected_insn}, unwind insn {self.unwound_insn}")


def _opmark_insn(m: OpMark) -> int:
    return 0 if m.insn_idx == NONE_U16 else m.insn_idx


def _call_return_off(s: Site, insn_end: dict[int, int] | None) -> int:
    if insn_end is not None and s.insn_off in insn_end:
        return insn_end[s.insn_off]
    return s.insn_off + (6 if s.enc == Encoding.POOL_INDIRECT else 5)


def check_recovery(rows: Sequence[SearchRow], ldst: Sequence[Ldst], opmarks: Sequence[OpMark],
                   sites: Sequence[Site], *, insn_end: dict[int, int] | None = None,
                   stub_start: int | None = None
                   ) -> tuple[list[RecoveryCheck], list[RecoveryPoint]]:
    """Run the step-7 checks; return ``(checks, recovery points)``.

    ``insn_end`` maps a decoded host instruction's start offset to its end
    offset (both relative to tc); without it the return offsets are computed
    from the call encodings. ``stub_start`` (B3 grown units): HELPER_CALL sites
    at or after it are the ``lookup_tb_ptr`` calls of guard stubs (``F``),
    which never unwind with their return address (B3/B4 design §6.1); they
    give no recovery point and no check.
    """
    checks: list[RecoveryCheck] = []
    ldst_marks = [m for m in opmarks if m.kind in (OpMarkKind.QEMU_LD, OpMarkKind.QEMU_ST)]
    for i, d in enumerate(ldst):
        exp = _opmark_insn(ldst_marks[d.op_ordinal]) if 0 <= d.op_ordinal < len(ldst_marks) \
            else None
        u = unwind(rows, d.raddr_off)
        checks.append(RecoveryCheck("raddr", d.raddr_off, exp, None if u is None else u[0],
                                    None if u is None else tuple(u[1]), i))
    calls = sorted((m for m in opmarks if m.kind == OpMarkKind.CALL), key=lambda m: m.host_off)
    for si, s in enumerate(sites):
        if s.kind != SiteKind.HELPER_CALL:
            continue
        if stub_start is not None and s.field_off >= stub_start:
            continue
        mark = None
        for m in calls:
            if m.host_off > s.insn_off:
                break
            if m.helper_id == s.aux:
                mark = m
        ret = _call_return_off(s, insn_end)
        u = unwind(rows, ret)
        checks.append(RecoveryCheck("helper_call", ret, None if mark is None else
                                    _opmark_insn(mark), None if u is None else u[0],
                                    None if u is None else tuple(u[1]), si))
    points: list[RecoveryPoint] = []
    seen: set[int] = set()
    for c in sorted(checks, key=lambda c: (c.host_return_offset, c.source, c.ref)):
        if c.host_return_offset in seen or c.unwound_insn is None or c.words is None:
            continue
        seen.add(c.host_return_offset)
        points.append(RecoveryPoint(f"r{len(points)}", c.host_return_offset, c.unwound_insn,
                                    c.words, c.source))  # type: ignore[arg-type]
    return checks, points

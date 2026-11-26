"""Read-only views passes see (design §5.2, §5.6 step 3).

:class:`WorkingSetView` implements ``SnapshotView`` over one generation of a
:class:`~scopt.model.snapshot.WorkingSet`: ``units()`` are the base units with
the overlay annotations merged in, and ``layout()`` is WorkingSet-aware. The
PassManager builds a fresh view for every pass.

:class:`TBUnitView` implements ``UnitView`` for a ``tb_singleton`` unit (the
only region kind in B1/B2). Its ``entries``/``exits``/``origins`` follow the
CacheRegion v1 conventions (ids ``e0``, ``x0``, ``x1``…).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from ..formats.enums import STATIC_EXIT_OFFPAGE_SLOT, ExitKind, OpMarkKind, SiteKind
from ..ids import TBKey
from .snapshot import WorkingSet
from .unit import Entry, Exit, GuestOrigin, TBUnit

__all__ = ["WorkingSetView", "TBUnitView", "snapshot_view", "unit_exits"]

_NO_JMP = 0xFFFF
_TB_EXIT_REQUESTED = 3


def unit_exits(u: TBUnit) -> tuple[Exit, ...]:
    """Exits of a single TB from its captured metadata, in host-offset order.

    * ``goto_tb``: each StaticExit with slot 0/1; ``insn_offset``/``reset_offset``
      come from the record's ``jmp_insn_offset``/``jmp_reset_offset``;
    * ``goto_ptr``: each GOTO_PTR OpMark (``static_dest_vaddr`` from an
      off-page StaticExit (slot 0xFF) at the same insn, if any);
    * ``exit_requested``: each EXIT_TB OpMark whose MOVI_EXIT_TB_PTR site has
      idx 3 (TB_EXIT_REQUESTED);
    * ``exit_null``: every other EXIT_TB OpMark, except the chaining fallback
      of a goto_tb slot (MOVI idx 0/1 with a StaticExit for that slot), which
      belongs to that goto_tb exit. That covers ``exit_tb(NULL)`` (no MOVI site)
      and ``exit_tb(tb+idx)`` without a goto_tb: both return to the main loop
      with no static destination.

    This is the pass-facing summary; WP7's region builder is authoritative.
    """
    from ..formats.sc_records import ScRecord

    rec = ScRecord.from_bytes(u.record) if len(u.record) >= 104 else None
    rows: list[tuple[int, Exit]] = []
    offpage = {x.insn_idx: x for x in u.static_exits if x.slot == STATIC_EXIT_OFFPAGE_SLOT}
    goto_marks = sorted((m for m in u.opmarks if m.kind == OpMarkKind.GOTO_TB),
                        key=lambda m: m.host_off)
    for x in u.static_exits:
        if x.slot not in (0, 1):
            continue
        io = ro = None
        if rec is not None:
            io = rec.jmp_insn_offset[x.slot]
            ro = rec.jmp_reset_offset[x.slot]
            io = None if io == _NO_JMP else io
            ro = None if ro == _NO_JMP else ro
        at = io if io is not None else (goto_marks[x.slot].host_off
                                        if x.slot < len(goto_marks) else 0)
        rows.append((at, Exit("", 0, ExitKind.GOTO_TB, x.slot, x.insn_idx, x.dest_vaddr,
                              io, ro)))
    for m in u.opmarks:
        if m.kind == OpMarkKind.GOTO_PTR:
            x = offpage.get(m.insn_idx)
            rows.append((m.host_off, Exit("", 0, ExitKind.GOTO_PTR, None, m.insn_idx,
                                          None if x is None else x.dest_vaddr)))
    movi = {s.insn_off: s for s in u.sites if s.kind == SiteKind.MOVI_EXIT_TB_PTR}
    goto_slots = {x.slot for x in u.static_exits if x.slot in (0, 1)}
    for m in u.opmarks:
        if m.kind != OpMarkKind.EXIT_TB:
            continue
        s = movi.get(m.host_off)
        if s is not None and s.aux == _TB_EXIT_REQUESTED:
            kind = ExitKind.EXIT_REQUESTED
        elif s is not None and s.aux in goto_slots:
            continue  # chaining fallback of a goto_tb exit
        else:
            kind = ExitKind.EXIT_NULL
        rows.append((m.host_off, Exit("", 0, kind, None, m.insn_idx, None)))
    rows.sort(key=lambda r: (r[0], r[1].kind.value, r[1].slot if r[1].slot is not None else -1))
    return tuple(replace(e, id=f"x{i}") for i, (_, e) in enumerate(rows))


class TBUnitView:
    """``UnitView`` of one TB unit (a ``tb_singleton`` region)."""

    __slots__ = ("unit_id", "snapshot_id", "members", "_ws")

    def __init__(self, unit: TBUnit, snapshot_id: str, ws: WorkingSet | None = None):
        self.unit_id = unit.uid
        self.snapshot_id = snapshot_id
        self.members = (unit,)
        self._ws = ws

    @property
    def unit(self) -> TBUnit:
        return self.members[0]

    def entries(self) -> tuple[Entry, ...]:
        return (Entry("e0", "general", 0, 0),)

    def exits(self) -> tuple[Exit, ...]:
        return unit_exits(self.unit)

    def origins(self) -> tuple[GuestOrigin, ...]:
        u = self.unit
        return (GuestOrigin(0, u.key.page_addr0, u.guest_size, tuple(u.vaddrs), u.origin),)

    def effects(self) -> None:
        return None  # Tier 1 only

    # layout helpers (WorkingSet-aware)
    def tb_offset(self) -> int:
        return self.unit.tb_offset if self._ws is None else self._ws.tb_offset(self.unit_id)

    def pin_reasons(self) -> tuple[str, ...]:
        return () if self._ws is None else self._ws.pin_reasons(self.unit_id)

    def __repr__(self) -> str:
        return f"TBUnitView({self.unit_id})"


class WorkingSetView:
    """``SnapshotView`` of one WorkingSet generation (read-only)."""

    def __init__(self, ws: WorkingSet):
        self._ws = ws
        base = ws.base
        self.snapshot_id: str = ws.snapshot_id
        self.header = getattr(base, "header", None)
        self.runtime = getattr(base, "runtime", None)
        self.bundle = getattr(base, "bundle", None)
        self._units: tuple[TBUnit, ...] | None = None
        self._by_uid: dict[str, TBUnit] | None = None
        self._by_key: dict[TBKey, tuple[TBUnit, ...]] | None = None

    # -- SnapshotView ---------------------------------------------------------
    @property
    def working_set(self) -> WorkingSet:
        return self._ws

    @property
    def generation(self) -> int:
        return self._ws.generation

    @property
    def capture_level(self) -> int | None:
        return getattr(self._ws.base, "capture_level", None)

    def _materialize(self) -> None:
        if self._units is not None:
            return
        out = []
        for u in self._ws.units():
            ann = self._ws.annotations(u.uid)
            out.append(u if ann is u.annotations else replace(u, annotations=ann))
        self._units = tuple(out)
        self._by_uid = {u.uid: u for u in out}

    def units(self) -> tuple[TBUnit, ...]:
        self._materialize()
        return self._units  # type: ignore[return-value]

    def unit_ids(self) -> tuple[str, ...]:
        return self._ws.unit_ids()

    def unit(self, uid: str) -> TBUnit:
        self._materialize()
        try:
            return self._by_uid[uid]  # type: ignore[index]
        except KeyError:
            raise KeyError(f"unknown unit {uid}") from None

    def __contains__(self, uid: object) -> bool:
        return uid in self._ws

    def by_key(self, key: TBKey) -> tuple[TBUnit, ...]:
        if self._by_key is None:
            d: dict[TBKey, list[TBUnit]] = {}
            for u in self.units():
                d.setdefault(u.key, []).append(u)
            self._by_key = {k: tuple(v) for k, v in d.items()}
        return self._by_key.get(key, ())

    def layout(self) -> Mapping[str, int]:
        return self._ws.layout()

    # -- extras -------------------------------------------------------------
    def unit_view(self, uid: str) -> TBUnitView:
        return TBUnitView(self.unit(uid), self.snapshot_id, self._ws)

    def unit_views(self) -> tuple[TBUnitView, ...]:
        return tuple(TBUnitView(u, self.snapshot_id, self._ws) for u in self.units())

    def is_relocatable(self, uid: str) -> bool:
        return self._ws.is_relocatable(uid)

    def pin_reasons(self, uid: str) -> tuple[str, ...]:
        return self._ws.pin_reasons(uid)

    def is_pinned(self, uid: str) -> bool:
        return self._ws.is_pinned(uid)

    def section_of(self, uid: str) -> str | None:
        return self._ws.section_of(uid)

    def sections(self) -> Mapping[str, tuple[str, ...]]:
        return self._ws.sections

    def placement(self, uid: str) -> int | None:
        return self._ws.placement(uid)

    def annotations(self, uid: str) -> Mapping[str, Any]:
        return self._ws.annotations(uid)

    def content_hash(self, uid: str) -> str:
        return self._ws.content_hash(uid)

    def layout_hash(self, uid: str) -> str:
        return self._ws.layout_hash(uid)

    def annotation_hash(self, uid: str) -> str:
        return self._ws.annotation_hash(uid)

    def facet_hash(self, uid: str, like: str) -> str:
        return self._ws.facet_hash(uid, like)

    def __repr__(self) -> str:
        return f"WorkingSetView({self._ws!r})"


def snapshot_view(snapshot: Any) -> WorkingSetView:
    """A fresh view of a snapshot (generation 0) or of a WorkingSet."""
    if isinstance(snapshot, WorkingSetView):
        return WorkingSetView(snapshot.working_set)
    return WorkingSetView(WorkingSet.initial(snapshot))


"""NativeLinker: B2 placer, fixups and the derived ``.scm`` (design §4.6, §4.7, §5.6 step 6, §5.7). WP10.

Input: the final :class:`~scopt.model.snapshot.WorkingSet` of a
:class:`~scopt.manager.Candidate` (or a WorkingSet / base snapshot directly).
Output: a :class:`LinkResult` with, in **original slot order**, the record
bytes and arena bytes ``[tc', end')`` of every valid unit, the move map, and
the derived capture files. Nothing is written here; packaging and the atomic
publish are :mod:`scopt.validate` and :mod:`scopt.formats.artifact`.

Placement (§5.7)
----------------
* **Fixed obstacles:** every pinned unit (not ``ok_relocatable``, or a PIN
  edit) stays at its original offset. So does every relocatable unit that no
  layout edit mentions (neither ordered into a section nor PLACEd): B2 only
  moves what a pass asked to move.
* **Explicit placements** (PLACE, used by ``move-units`` and tests) are taken
  as given.
* **Sections:** the units of the ``hot``, ``warm``, ``unknown``, ``boot`` and
  ``cold`` sections, in that order and in each section's requested order, are
  placed with a cursor starting at ``ROUND_UP(prologue_end, 64)``:
  ``tb' = ROUND_UP(cursor, 64)``, bumped past every obstacle it would overlap,
  then ``cursor = tb' + (end - tb)``.
* Per unit ``d = tb' - tb``; ``tc' = tc + d``, ``end' = end + d`` (so
  ``tc - tb`` and the 64-byte alignment are preserved).

Per-unit checks (each failure is a :class:`LinkFailure`, reason code):

* ``place_misaligned`` (``tb' % 64``), ``place_below_prologue``,
  ``place_out_of_range`` (``end' >= code_size - host_page - 1024``, the
  §4.7 ``max_end`` bound), ``place_overlap`` (two final intervals overlap);
* ``rel32_range``: a ``rel_minus_delta`` field ``v - d`` outside int32;
* ``imm_range``: an ``abs_plus_delta`` field ``v + d`` that does not fit its
  width (unreachable while ``code_address >= 4 GiB``; such units are pinned);
* ``fixup_field``: a fixup outside the code, or a width the rules do not
  cover (fail closed).

Rollback (§5.6 step 6): when some units fail, each failed unit is put back at
its original offset if that interval is still free (no explicit placement of
another unit overlaps it), and the placer runs once more with those units as
fixed obstacles. They are reported as pinned with reason
``failed:<reason>``. If a failed unit's original slot is taken, or the second
run fails again, the link fails (``LinkResult.ok`` is false) and the caller
rejects the candidate; the input snapshot stays the reference.

Fixups (§4.7, from ``TBUnit.reloc.fixups``): ``rel_minus_delta`` gives
``field' = field - d`` (int32), ``abs_plus_delta`` gives ``field' = field +
d``. Every other byte of ``[tc, end)`` is copied unchanged.

Derived ``.scm`` (§4.6)
-----------------------
For each capture file the import used, the same v1 file is written again:
file header, RUN_INFO, HELPER, MAPS, RAMBLOCKS and END are copied; TB chunks
that do not bind to the snapshot (or bind through another file) are left out;
each bound TB chunk keeps its arrays, ``vaddr``, guest bytes and commit
sequence, but gets the relocated record bytes, ``code_fnv64`` over the new
``[tc', end')`` and rewritten sites: a ``value`` inside the unit's own
``[tb, end)`` (intra labels, own-struct pointers, raddr) moves by ``d``;
``final_field`` is re-read from the new bytes. The first file is
``cache.scm``, further ones ``cache.<k>.scm``. A capture that binds no slot
on its own (every slot it covers is bound through an earlier capture of the
union, §2.5.8) produces no derived file.
"""

from __future__ import annotations

import json
import os
import struct
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .canon import fnv1a64, hexaddr
from .formats import scm_v1 as S
from .formats.enums import Encoding, OpMarkKind, SiteKind
from .formats.sc_v5 import ScHeader, ScRecord, read_sc
from .model.snapshot import LAYOUT_SECTIONS, WorkingSet
from .model.unit import Fixup, Site, TBUnit

if TYPE_CHECKING:
    from .model.native import InlineCall

__all__ = [
    "NativeLinker", "LinkResult", "UnitLink", "LinkFailure", "LinkError", "int32_ok",
    "apply_fixup", "relocate_unit", "derive_scm", "capture_join", "TB_ALIGN", "arena_limit",
    "R", "transplant_native",
]

TB_ALIGN = 64
INT32_MIN = -(1 << 31)
INT32_MAX = (1 << 31) - 1


class R:
    """Link failure reason codes."""

    REL32_RANGE = "rel32_range"
    IMM_RANGE = "imm_range"
    FIXUP_FIELD = "fixup_field"
    MISALIGNED = "place_misaligned"
    BELOW_PROLOGUE = "place_below_prologue"
    OUT_OF_RANGE = "place_out_of_range"
    OVERLAP = "place_overlap"
    ROLLBACK_SLOT_TAKEN = "rollback_slot_taken"


class LinkError(ValueError):
    def __init__(self, reason: str, detail: str = "", uid: str | None = None):
        self.reason = reason
        self.detail = detail
        self.uid = uid
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class LinkFailure:
    uid: str
    reason: str
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"unit": self.uid, "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class UnitLink:
    """One unit as linked: original slot order, new record and arena bytes.

    ``slot`` is the unit's slot in the **output** ``.sc``. :func:`write_sc`
    packs only the valid records, so when the input had an invalid record
    (skipped by ``load_records``) before this unit, the output slot is lower
    than the input slot (``input_slot``). The derived ``.scm`` and the sidecar
    ``move_map`` use the output slot, since that is what they describe.
    """

    uid: str
    slot: int
    old_tb_offset: int
    new_tb_offset: int
    record: bytes  # 104 bytes, checksum recomputed when moved
    body: bytes  # [tc', end'): code then search data
    patched: tuple[tuple[int, int], ...] = ()  # (field_off, width) changed, relative to tc
    input_slot: int | None = None  # the slot in the input .sc (None: same as ``slot``)
    native_fragments: tuple[InlineCall, ...] = ()
    inline_start: int | None = None
    native_unit: TBUnit | None = field(default=None, repr=False)  # enlarged, before relocation
    retired_exit_chunk: S.TbChunk | None = field(default=None, repr=False)

    @property
    def in_slot(self) -> int:
        return self.slot if self.input_slot is None else self.input_slot

    @property
    def delta(self) -> int:
        return self.new_tb_offset - self.old_tb_offset

    @property
    def moved(self) -> bool:
        return self.delta != 0


@dataclass
class LinkResult:
    ok: bool
    header: ScHeader
    units: tuple[UnitLink, ...] = ()
    failures: tuple[LinkFailure, ...] = ()  # final failures (ok is False)
    rollbacks: tuple[LinkFailure, ...] = ()  # units put back at their original offset
    attempts: int = 1
    derived_scms: tuple[tuple[str, bytes], ...] = ()  # (file name, bytes)
    capture_sources: tuple[str, ...] = ()  # source capture names, same order

    @property
    def max_end(self) -> int:
        return max((ScRecord.from_bytes(u.record).end_offset for u in self.units), default=0)

    def move_map(self) -> dict[str, dict[str, Any]]:
        """§4.6 sidecar ``move_map``: moved units only."""
        return {u.uid: {"slot": u.slot, "old_tb_offset": hexaddr(u.old_tb_offset),
                        "new_tb_offset": hexaddr(u.new_tb_offset), "delta": u.delta}
                for u in self.units if u.moved}

    def by_uid(self) -> dict[str, UnitLink]:
        return {u.uid: u for u in self.units}

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "attempts": self.attempts,
                "moved": sum(1 for u in self.units if u.moved), "units": len(self.units),
                "max_end": hexaddr(self.max_end),
                "failures": [f.to_json() for f in self.failures],
                "rollbacks": [f.to_json() for f in self.rollbacks],
                "derived_scms": [n for n, _ in self.derived_scms],
                "capture_sources": list(self.capture_sources)}


# ---------------------------------------------------------------------------
# Fixups
# ---------------------------------------------------------------------------

def int32_ok(v: int) -> bool:
    return INT32_MIN <= v <= INT32_MAX


def round_up(v: int, a: int) -> int:
    return (v + a - 1) // a * a


def arena_limit(header: ScHeader) -> int:
    """``end'`` must stay below this (§4.7: ``max_end < code_size - host_page - 1024``)."""
    return header.code_size - header.host_page_size - 1024


def apply_fixup(code: bytearray, f: Fixup, d: int, *, uid: str = "") -> None:
    """Apply one fixup for a whole-TB move by ``d`` in place (raises :class:`LinkError`)."""
    if f.field_off < 0 or f.field_off + f.width > len(code):
        raise LinkError(R.FIXUP_FIELD, f"field @{f.field_off}+{f.width} outside the code", uid)
    raw = bytes(code[f.field_off:f.field_off + f.width])
    if f.rule == "rel_minus_delta":
        if f.width != 4:
            raise LinkError(R.FIXUP_FIELD, f"rel_minus_delta width {f.width} @{f.field_off}", uid)
        v = int.from_bytes(raw, "little", signed=True) - d
        if not int32_ok(v):
            raise LinkError(R.REL32_RANGE, f"field @{f.field_off} ({f.target_class}) would be "
                            f"{v:#x} after a move by {d:#x}", uid)
        code[f.field_off:f.field_off + 4] = v.to_bytes(4, "little", signed=True)
    elif f.rule == "abs_plus_delta":
        if f.width == 8:
            v = int.from_bytes(raw, "little") + d
            if not 0 <= v < 1 << 64:
                raise LinkError(R.IMM_RANGE, f"imm64 @{f.field_off} out of range", uid)
            code[f.field_off:f.field_off + 8] = v.to_bytes(8, "little")
        elif f.width == 4:
            # imm32 forms: zero-extended (mov r32) or sign-extended (mov r/m64); the
            # value must keep its meaning under both readings to be safe.
            u = int.from_bytes(raw, "little") + d
            if not 0 <= u <= INT32_MAX:
                raise LinkError(R.IMM_RANGE, f"imm32 @{f.field_off} would be {u:#x}", uid)
            code[f.field_off:f.field_off + 4] = u.to_bytes(4, "little")
        else:
            raise LinkError(R.FIXUP_FIELD, f"abs_plus_delta width {f.width} @{f.field_off}", uid)
    else:
        raise LinkError(R.FIXUP_FIELD, f"unknown fixup rule {f.rule!r}", uid)


def relocate_unit(u: TBUnit, new_tb: int) -> UnitLink:
    """Record and ``[tc', end')`` bytes of ``u`` moved to ``new_tb`` (raises :class:`LinkError`).

    The result's ``slot`` is the input slot; :meth:`NativeLinker.link` renumbers
    it to the output slot."""
    d = new_tb - u.tb_offset
    body = u.code + u.search
    if d == 0:
        return UnitLink(u.uid, u.slot, u.tb_offset, new_tb, bytes(u.record), body,
                        native_fragments=u.native_fragments, inline_start=u.inline_start)
    code = bytearray(u.code)
    patched = []
    for f in u.reloc.fixups:
        apply_fixup(code, f, d, uid=u.uid)
        patched.append((f.field_off, f.width))
    rec = ScRecord.from_bytes(u.record).replace(
        tb_offset=u.tb_offset + d, tc_offset=u.tc_offset + d, end_offset=u.end_offset + d)
    return UnitLink(u.uid, u.slot, u.tb_offset, new_tb, rec.to_bytes(fix_checksum=True),
                    bytes(code) + u.search, tuple(patched), native_fragments=u.native_fragments,
                    inline_start=u.inline_start)


def _branch_disp(start: int, end: int, uid: str) -> bytes:
    value = end - start
    if not int32_ok(value):
        raise LinkError(R.REL32_RANGE, f"native branch {start:#x} -> {end:#x}", uid)
    return value.to_bytes(4, "little", signed=True)


def retarget_private_descriptor_refs(code: bytearray, source: TBUnit, *,
                                     tc_offset: int, tb_offset: int,
                                     code_address: int
                                     ) -> tuple[tuple[int, int], ...]:
    """Bind EXIT_TB results in a private copy to its real descriptor.

    Ordinary whole-TB relocation preserves ``tc_offset - tb_offset``.  A
    firmware region deliberately stores all descriptors first and all code
    second, so that invariant does not hold.  Captured EXIT_TB pointer
    materializations must therefore be rebound after relocation; otherwise an
    icount/interrupt/exception exit returns a pointer into padding or code.
    """
    patched: list[tuple[int, int]] = []
    for site in source.sites:
        if site.kind != SiteKind.MOVI_EXIT_TB_PTR:
            continue
        if site.aux < 0 or site.aux >= len(source.record):
            raise LinkError("firmware_region_descriptor_ref",
                            f"{source.uid}: descriptor addend {site.aux:#x}", source.uid)
        target = code_address + tb_offset + site.aux
        field = site.field_off
        if site.enc == Encoding.RIPREL32 and site.width == 4:
            if field < 0 or field + 4 > len(code):
                raise LinkError("firmware_region_descriptor_ref",
                                f"{source.uid}: RIP field +{field:#x}", source.uid)
            disp = target - (code_address + tc_offset + field + 4)
            if not int32_ok(disp):
                raise LinkError(R.REL32_RANGE,
                                f"{source.uid}: descriptor field +{field:#x}", source.uid)
            struct.pack_into("<i", code, field, disp)
        elif site.enc == Encoding.IMM64 and site.width == 8:
            if field < 0 or field + 8 > len(code):
                raise LinkError("firmware_region_descriptor_ref",
                                f"{source.uid}: imm64 field +{field:#x}", source.uid)
            struct.pack_into("<Q", code, field, target)
        else:
            raise LinkError("firmware_region_descriptor_ref",
                            f"{source.uid}: unsupported {site.enc.name}/{site.width}", source.uid)
        patched.append((field, site.width))
    return tuple(patched)


CF_USE_ICOUNT = 0x00002000
TRUSTED_ENTRY_OFFSET = 15
TRUSTED_INSN0_START = 15


def trusted_firmware_entry_problem(u: TBUnit) -> str | None:
    """Why ``u`` cannot be entered after its exact non-icount IRQ poll."""
    if u.key.cflags & CF_USE_ICOUNT:
        return "icount_cflags"
    code = u.code
    if u.insn0_start != TRUSTED_INSN0_START or len(code) < TRUSTED_INSN0_START:
        return f"insn0_start={u.insn0_start}"
    if (code[:5] != bytes.fromhex("8b5df885db") or code[5:7] != b"\x0f\x8c" or
            code[11:TRUSTED_INSN0_START] not in
            (bytes.fromhex("c645fc00"), bytes.fromhex("c645fc01"))):
        return "trusted_irq_prefix_shape"
    return None


def trusted_firmware_edge_problem(u: TBUnit, exit_id: str) -> str | None:
    """Why a trusted private edge cannot be retargeted in ``u``.

    Trusted regions may patch either a captured rel32 exit field or the one
    verified ``lookup_tb_ptr`` tail associated with a GOTO_PTR exit.  Other
    exit shapes remain valid ordinary QEMU side exits, but must not be
    advertised as internal region edges and discovered only after packaging.
    """
    from .formats.region_v1 import unit_exits

    exit_ = next((row for row in unit_exits(u) if row.id == exit_id), None)
    if exit_ is None:
        return "exit_missing"
    field = exit_.insn_offset
    if field is not None:
        if 1 <= field and field + 4 <= len(u.code):
            return None
        return "rel32_field_out_of_bounds"
    if exit_.kind.value == "goto_ptr":
        tails = [tail for tail in lookup_tails(u) if tail.insn_idx == exit_.insn]
        if len(tails) == 1:
            return None
        return "lookup_tail_missing" if not tails else "lookup_tail_ambiguous"
    return "no_rel32_field"


def trusted_return_guard_problem(unit, exit_id, key, offsets, code_address) -> str | None:
    """Whether a private return can retain the original lookup on a guard miss."""
    from .formats.region_v1 import unit_exits
    from .guard import GuardError, guard_key

    if offsets is None or offsets.envlayout_id is None:
        return "return_guard_layout_unbound"
    exit_ = next((e for e in unit_exits(unit) if e.id == exit_id), None)
    if exit_ is None or exit_.kind.value != "goto_ptr":
        return "return_guard_requires_dynamic_exit"
    tails = [tail for tail in lookup_tails(unit) if tail.insn_idx == exit_.insn]
    if len(tails) != 1 or not free_slots(unit):
        return "return_guard_no_free_tail_or_slot"
    try:
        guard_key(key.flags, key.cs_base)
    except GuardError as exc:
        return exc.reason
    return tail_problem(unit, tails[0], code_address)


def transplant_native(u: TBUnit, *, code_address: int) -> TBUnit:
    """Materialize a unit's checked symbolic fragments at its pre-move offsets."""
    from .validate_sym.native_inline import verify_fragment

    if (not u.native_fragments or u.stub_start is not None or u.inline_start is not None
            or u.meta_status != "ok" or not u.reloc.relocatable):
        raise LinkError("native_layout_unsupported", "native growth needs an original relocatable TB",
                        u.uid)
    code = bytearray(u.code)
    inline_start = len(code)
    tc_abs = code_address + u.tc_offset
    drop_fields: set[int] = set()
    drop_sites: set[Site] = set()
    drop_marks = set()
    drop_recovery = set()
    entry_sites = []
    prior_calls: list[tuple[int, int]] = []
    for request in u.native_fragments:
        off, continuation = request.call_off, request.continuation_off
        if (len(request.call_bytes) not in (5, 6)
                or not 0 <= off < continuation < u.ldst_end
                or continuation != off + len(request.call_bytes)
                or u.code[off:continuation] != request.call_bytes
                or any(_overlaps(off, continuation, a, b) for a, b in prior_calls)):
            raise LinkError("native_callsite_mismatch", f"call bytes/range at +{off:#x}", u.uid)
        prior_calls.append((off, continuation))
        rows = [s for s in u.sites if s.insn_off == off and s.kind == SiteKind.HELPER_CALL]
        if (len(rows) != 1 or rows[0].aux != request.helper_id
                or rows[0].value != request.target_abs):
            raise LinkError("native_callsite_mismatch", f"helper capture at +{off:#x}", u.uid)
        site = rows[0]
        captured_pool = [p for p in u.pool if p.user_field_off == site.field_off]
        if request.pool_slot_off is None:
            correct_encoding = (site.enc == Encoding.REL32 and not captured_pool
                                and len(request.call_bytes) == 5 and request.call_bytes[0] == 0xE8)
        else:
            correct_encoding = (site.enc == Encoding.POOL_INDIRECT and len(captured_pool) == 1
                                and captured_pool[0].slot_off == request.pool_slot_off
                                and captured_pool[0].data0 == request.target_abs
                                and len(request.call_bytes) == 6
                                and request.call_bytes[:2] == b"\xff\x15")
        if not correct_encoding:
            raise LinkError("native_callsite_mismatch", f"encoding at +{off:#x}", u.uid)
        if any(tc_abs + off < s.value < tc_abs + continuation for s in u.sites):
            raise LinkError("native_callsite_mismatch", f"edge into call footprint at +{off:#x}", u.uid)
        marks = [m for m in u.opmarks if m.kind == OpMarkKind.CALL and m.host_off <= off]
        if marks:
            mark = max(marks, key=lambda m: m.host_off)
            if mark.helper_id == request.helper_id and mark.insn_idx == site.insn_idx:
                drop_marks.add(mark)
        drop_fields.add(site.field_off)
        drop_sites.add(site)
        drop_recovery.add(continuation)

        island_start = len(code)
        island = bytearray(request.fragment.code)
        for field_off in request.fragment.continuation_fields:
            if not 0 < field_off <= len(island) - 4 or island[field_off - 1] != 0xE9:
                raise LinkError("native_fragment_invalid", "invalid return relocation", u.uid)
            island[field_off:field_off + 4] = _branch_disp(
                island_start + field_off + 4, continuation, u.uid)
        try:
            verify_fragment(request, bytes(island), island_start=island_start)
        except ValueError as e:
            raise LinkError("native_fragment_invalid", str(e), u.uid) from e
        disp = _branch_disp(off + 5, island_start, u.uid)
        code[off:continuation] = b"\xe9" + disp + b"\x90" * (continuation - off - 5)
        entry_sites.append(Site(off + 1, off, 4, Encoding.REL32, SiteKind.LABEL_JMP, 0,
                                tc_abs + island_start, site.insn_idx,
                                int.from_bytes(disp, "little")))
        code.extend(island)

    pool, retained = [], {}
    for i, entry in enumerate(u.pool):
        if entry.user_field_off not in drop_fields:
            retained[i] = len(pool)
            pool.append(entry)
    sites = []
    for site in u.sites:
        if site in drop_sites or (site.kind == SiteKind.POOL_REF and site.field_off in drop_fields):
            continue
        if site.kind == SiteKind.POOL_REF:
            if site.aux not in retained:
                raise LinkError("native_callsite_mismatch", "retired pool user is still referenced",
                                u.uid)
            site = replace(site, aux=retained[site.aux])
        sites.append(site)
    sites.extend(entry_sites)
    sites.sort(key=lambda site: (site.insn_off, site.field_off))
    recovery = tuple(p for p in u.recovery
                     if p.source != "helper_call" or p.host_return_offset not in drop_recovery)
    code = bytes(code)
    end = u.tc_offset + len(code) + len(u.search)
    record = ScRecord.from_bytes(u.record).replace(tc_size=len(code), end_offset=end)
    raw_record = record.to_bytes(fix_checksum=True)
    return replace(u, code=code, tc_size=len(code), end_offset=end, record=raw_record,
                   sites=tuple(sites), pool=tuple(pool),
                   opmarks=tuple(mark for mark in u.opmarks if mark not in drop_marks),
                   recovery=tuple(replace(p, id=f"r{i}") for i, p in enumerate(recovery)),
                   reloc=replace(u.reloc, fixups=tuple(f for f in u.reloc.fixups
                                                        if f.field_off not in drop_fields)),
                   inline_start=inline_start,
                   content_hash=_content(raw_record, code, u.search,
                                         {"native": [c.to_json() for c in u.native_fragments]}))


# ---------------------------------------------------------------------------
# Placer
# ---------------------------------------------------------------------------

def _overlaps(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 < b1 and b0 < a1


class NativeLinker:
    """Place and relocate the units of a candidate (see the module docstring).

    ``source`` is a :class:`~scopt.manager.Candidate`, a
    :class:`~scopt.model.snapshot.WorkingSet` or a base snapshot.
    ``join_result`` (the :class:`~scopt.importer.join.JoinResult` of the
    import) supplies the capture chunks for the derived ``.scm``; when it is
    None it is rebuilt from the snapshot's store directory
    (:func:`capture_join`). ``derive_captures=False`` skips the derived files.
    """

    def __init__(self, source: Any, *, join_result: Any = None, derive_captures: bool = True):
        ws = getattr(source, "working_set", source)
        self.ws: WorkingSet = WorkingSet.initial(ws)
        self.base = self.ws.base
        self.header: ScHeader = self.base.header
        self.join_result = join_result
        self.derive_captures = derive_captures

    # -- placement ---------------------------------------------------------
    def plan(self, rolled_back: frozenset[str] = frozenset(), *,
             effective: Mapping[str, TBUnit] | None = None,
             append_unordered: frozenset[str] = frozenset()) -> dict[str, int]:
        """Place unit extents in section order around fixed arena obstacles.

        V2 supplies the grown extents and appends grown units without an
        explicit layout request. Unit IDs and their pins still come from ws.
        """
        ws = self.ws
        effective = effective or {}
        floor = round_up(self.header.prologue_end, TB_ALIGN)
        in_sections = {uid for s in LAYOUT_SECTIONS for uid in ws.sections[s]}
        out: dict[str, int] = {}
        obstacles: list[tuple[int, int]] = []
        flow: list[str] = []
        trailing: list[str] = []
        for original in ws.units():
            uid = original.uid
            u = effective.get(uid, original)
            if uid in rolled_back or ws.is_pinned(uid):
                out[uid] = u.tb_offset
            elif ws.placement(uid) is not None:
                out[uid] = int(ws.placement(uid))  # type: ignore[arg-type]
            elif uid in in_sections:
                continue
            elif uid in append_unordered:
                trailing.append(uid)
                continue
            else:
                out[uid] = u.tb_offset  # not mentioned by any layout edit: stays
            span = u.end_offset - u.tb_offset
            obstacles.append((out[uid], out[uid] + span))
        for s in LAYOUT_SECTIONS:
            for uid in ws.sections[s]:
                if uid in out or uid in rolled_back:
                    continue
                flow.append(uid)
        obstacles.sort()
        cursor = floor
        for uid in flow:
            u = effective[uid] if uid in effective else ws.unit(uid)
            span = u.end_offset - u.tb_offset
            tb = round_up(cursor, TB_ALIGN)
            moved = True
            while moved:
                moved = False
                for o0, o1 in obstacles:
                    if _overlaps(tb, tb + span, o0, o1):
                        tb = round_up(o1, TB_ALIGN)
                        moved = True
            out[uid] = tb
            cursor = tb + span
        if trailing:
            cursor = max(cursor, max((end for _, end in obstacles), default=floor))
            for uid in trailing:
                u = effective[uid]
                out[uid] = round_up(cursor, TB_ALIGN)
                cursor = out[uid] + u.end_offset - u.tb_offset
        return {uid: out[uid] for uid in ws.unit_ids()}

    def _check_plan(self, plan: Mapping[str, int],
                    effective: Mapping[str, TBUnit] | None = None) -> list[LinkFailure]:
        h = self.header
        floor = round_up(h.prologue_end, TB_ALIGN)
        limit = arena_limit(h)
        fails: list[LinkFailure] = []
        spans = []
        changed = set()
        effective = effective or {}
        for uid, tb in plan.items():
            original = self.ws.unit(uid)
            u = effective.get(uid, original)
            d = tb - u.tb_offset
            if d == 0 and u.end_offset == original.end_offset:
                spans.append((tb, u.end_offset, uid))
                continue
            changed.add(uid)
            end = u.end_offset + d
            if tb % TB_ALIGN:
                fails.append(LinkFailure(uid, R.MISALIGNED, f"tb' {tb:#x}"))
            elif tb < floor:
                fails.append(LinkFailure(uid, R.BELOW_PROLOGUE, f"tb' {tb:#x} < {floor:#x}"))
            elif end >= limit or tb < 0:
                fails.append(LinkFailure(uid, R.OUT_OF_RANGE,
                                         f"end' {end:#x} >= code_size - host_page - 1024 "
                                         f"({limit:#x})"))
            spans.append((tb, end, uid))
        spans.sort()
        bad = {f.uid for f in fails}
        # pairwise overlap; blame the moved unit(s)
        active: list[tuple[int, int, str]] = []
        for s0, s1, uid in spans:
            active = [a for a in active if a[1] > s0]
            for a0, a1, auid in active:
                for v in (uid, auid):
                    if v in changed and v not in bad:
                        other = auid if v == uid else uid
                        fails.append(LinkFailure(v, R.OVERLAP, f"overlaps {other}"))
                        bad.add(v)
            active.append((s0, s1, uid))
        return fails

    def _relocate(self, plan: Mapping[str, int]) -> tuple[list[UnitLink], list[LinkFailure]]:
        """Relocate every unit; ``UnitLink.slot`` is the output slot (index in the
        packed record array :func:`write_sc` writes), ``input_slot`` the input's."""
        links, fails = [], []
        for out_slot, u in enumerate(self.ws.units()):
            try:
                lu = relocate_unit(u, plan[u.uid])
            except LinkError as e:
                fails.append(LinkFailure(u.uid, e.reason, e.detail))
                lu = UnitLink(u.uid, u.slot, u.tb_offset, u.tb_offset, bytes(u.record),
                              u.code + u.search)
            links.append(replace(lu, slot=out_slot, input_slot=u.slot))
        return links, fails

    def _attempt(self, rolled_back: frozenset[str]
                 ) -> tuple[dict[str, int], list[UnitLink], list[LinkFailure]]:
        plan = self.plan(rolled_back)
        fails = self._check_plan(plan)
        bad = {f.uid for f in fails}
        links, rfails = self._relocate({k: (self.ws.unit(k).tb_offset if k in bad else v)
                                        for k, v in plan.items()})
        fails += [f for f in rfails if f.uid not in bad]
        return plan, links, fails

    def _link_native(self, units: Sequence[TBUnit]) -> LinkResult:
        """Append enlarged units outside every original TB while preserving slot order."""
        header = self.header
        growing = [u for u in units if u.native_fragments and u.inline_start is None
                   and not u.splice_applied]
        if ((header.version, header.reserved) != (7, 0)
                or any(u.stub_start is not None for u in units)):
            fail = LinkFailure(growing[0].uid, "native_layout_unsupported",
                               "native growth requires canonical public v7")
            return LinkResult(False, header, failures=(fail,))
        modes = {request.splice is not None for u in growing for request in u.native_fragments}
        if len(modes) > 1:
            fail = LinkFailure(growing[0].uid, "native_layout_unsupported",
                               "splice and island growth need separate pipelines")
            return LinkResult(False, header, failures=(fail,))
        join = self._join() if self.derive_captures else None
        if self.derive_captures:
            bound = set(join.bound()) if join is not None else set()
            for unit in growing:
                if unit.slot not in bound:
                    fail = LinkFailure(unit.uid, "native_metadata_missing", "no bound source capture")
                    return LinkResult(False, header, failures=(fail,))
        cursor = max((u.end_offset for u in units), default=header.prologue_end)
        links = []
        try:
            for slot, u in enumerate(units):
                if u.native_fragments and u.inline_start is None and not u.splice_applied:
                    if u.native_fragments[0].splice is not None:
                        from .splice_layout import materialize_splices
                        from .validate_sym.native_inline import NativeInlineError

                        try:
                            materialized = materialize_splices(
                                self.base.unit(u.uid), u.native_fragments,
                                header.code_address, trust_internal_graphs=True)
                        except NativeInlineError as exc:
                            raise LinkError(exc.reason, str(exc), u.uid) from exc
                    else:
                        materialized = transplant_native(u, code_address=header.code_address)
                    target = round_up(cursor, TB_ALIGN)
                    cursor = target + materialized.end_offset - u.tb_offset
                    if cursor >= arena_limit(header):
                        raise LinkError(R.OUT_OF_RANGE, f"enlarged TB ends at {cursor:#x}", u.uid)
                    link = replace(relocate_unit(materialized, target), native_unit=materialized)
                else:
                    link = relocate_unit(u, u.tb_offset)
                links.append(replace(link, slot=slot, input_slot=u.slot))
            result = LinkResult(True, header, tuple(links))
            files, names = (derive_scm(join, result.by_uid(), self.base)
                            if self.derive_captures else ([], []))
        except LinkError as e:
            return LinkResult(False, header, failures=(LinkFailure(e.uid, e.reason, e.detail),))
        except S.ScmEncodeError as e:
            fail = LinkFailure(growing[0].uid, "native_metadata_invalid", str(e))
            return LinkResult(False, header, failures=(fail,))
        result.derived_scms = tuple(files)
        result.capture_sources = tuple(names)
        return result

    def link(self) -> LinkResult:
        """Place, relocate, roll back once if needed, derive the captures."""
        units = self.ws.units()
        if self.ws.has_layout_edits and (
                native := next((u for u in units if u.native_fragments and not u.splice_applied), None)) is not None:
            fail = LinkFailure(native.uid, "native_layout_unsupported", "native islands with layout edits")
            return LinkResult(False, self.header, failures=(fail,))
        if any(u.native_fragments and u.inline_start is None and not u.splice_applied for u in units):
            return self._link_native(units)
        plan, links, fails = self._attempt(frozenset())
        attempts = 1
        rollbacks: tuple[LinkFailure, ...] = ()
        if fails:
            attempts = 2
            failed = {f.uid for f in fails}
            taken = []
            for f in fails:
                u = self.ws.unit(f.uid)
                for uid, tb in plan.items():
                    if uid in failed or self.ws.placement(uid) is None:
                        continue
                    o = self.ws.unit(uid)
                    if _overlaps(u.tb_offset, u.end_offset, tb, tb + o.end_offset - o.tb_offset):
                        taken.append(LinkFailure(f.uid, R.ROLLBACK_SLOT_TAKEN,
                                                 f"{f.reason}; original slot overlaps the "
                                                 f"placement of {uid}"))
                        break
            if taken:
                return LinkResult(False, self.header, tuple(links), tuple(fails + taken),
                                  attempts=attempts)
            rollbacks = tuple(fails)
            plan, links, fails2 = self._attempt(frozenset(failed))
            if fails2:
                return LinkResult(False, self.header, tuple(links), tuple(fails2), rollbacks,
                                  attempts)
        edits = [u for u in units if u.retired_exit_slots]
        if edits:
            from .ordinary_exit import capture, retire, targets

            try:
                if self.ws.has_layout_edits or not self.derive_captures:
                    raise ValueError("ordinary exits need same placement and a derived capture")
                join_result = self._join()
                epilogue, tb_ret = capture(self.base, join_result)
                ca = self.header.code_address
                entries = targets(units, ca)
                outputs = {link.uid: index for index, link in enumerate(links)}
                for unit in edits:
                    index = outputs[unit.uid]
                    lu = links[index]
                    binding = join_result.slots[unit.slot]
                    if not binding.bound:
                        raise ValueError("ordinary exit source capture is unbound")
                    body, chunk = retire(unit, binding.chunk, unit.retired_exit_slots, ca,
                                         epilogue, tb_ret, entries)
                    links[index] = replace(lu, record=chunk.record, body=body, retired_exit_chunk=chunk)
            except (ValueError, KeyError, OverflowError) as exc:
                return LinkResult(False, self.header, failures=(LinkFailure(
                    edits[0].uid, "ordinary_exit_shape", str(exc)),))
        res = LinkResult(True, self.header, tuple(links), (), rollbacks, attempts)
        if self.derive_captures:
            files, names = derive_scm(self._join(), res.by_uid(), self.base)
            res.derived_scms = tuple(files)
            res.capture_sources = tuple(names)
        return res

    def _join(self) -> Any:
        if self.join_result is None:
            self.join_result = capture_join(self.base)
        return self.join_result


# ---------------------------------------------------------------------------
# Derived capture (§4.6)
# ---------------------------------------------------------------------------

def capture_join(snapshot: Any) -> Any:
    """The import's :class:`~scopt.importer.join.JoinResult`, rebuilt from the
    snapshot's store directory (``cache.sc`` plus the stored captures). None
    when the snapshot has no store path or no captures."""
    from .importer.join import join

    path = getattr(snapshot, "path", None)
    if not path:
        return None
    d = Path(path)
    meta = json.loads((d / "snapshot.json").read_text())
    caps = [d / c for c in meta.get("files", {}).get("captures", [])]
    if not caps:
        return None
    return join(read_sc(d / "cache.sc", versions=(7,)), caps)


def _rewrite_tb(tb: S.TbChunk, link: UnitLink, code_address: int) -> S.TbChunk:
    """The TB chunk describing ``link``: the output slot, the new record, and (for a
    moved unit) ``code_fnv64`` and the sites rewritten for the move."""
    if link.retired_exit_chunk is not None:
        return replace(link.retired_exit_chunk, slot=link.slot)
    if link.native_unit is not None:
        from .formats.layouts import SCM_TB_CHUNK_VERSION_3, SCM_TB_CHUNK_VERSION_4

        u = link.native_unit
        row_version = SCM_TB_CHUNK_VERSION_4 if u.splice_applied else SCM_TB_CHUNK_VERSION_3
        span = u.native_fragments[0].splice.physical_start if u.splice_applied else u.inline_start
        tb = replace(tb, record=u.record, code_size=u.tc_size, sites=_scm_sites(u),
                     insn0_start=u.insn0_start, body_end=u.body_end, ldst_end=u.ldst_end,
                     pool_start=0xFFFF_FFFF if u.pool_start is None else u.pool_start,
                     insns=tuple(S.ScmInsn(i.words, i.host_start, i.host_end) for i in u.insns),
                     ldst=tuple(S.ScmLdst(tuple(0xFFFF_FFFF if f is None else f for f in d.jcc_fields),
                                          d.slow_start, d.slow_end, d.raddr_off,
                                          d.oi, d.op_ordinal, int(d.is_ld)) for d in u.ldst),
                     pool=tuple(S.ScmPool(p.user_field_off, p.slot_off, p.rtype,
                                          p.nlong, p.addend, p.data0) for p in u.pool),
                     opmarks=tuple(S.ScmOpMark(m.host_off, int(m.kind), m.insn_idx,
                                              0xFFFF_FFFF if m.helper_id is None else m.helper_id)
                                   for m in u.opmarks),
                     tb_chunk_version=row_version, reserved=span,
                     native_fragments=u.native_fragments, source=None)
    d = link.delta
    if d == 0:
        if tb.slot == link.slot and tb.record == link.record:
            return tb
        return replace(tb, slot=link.slot, record=link.record, source=None)
    old = ScRecord.from_bytes(tb.record)
    lo, hi = code_address + old.tb_offset, code_address + old.end_offset
    code = link.body
    sites = []
    for s in tb.sites:
        v = s.value + d if lo <= s.value < hi else s.value
        ff = s.final_field
        if s.width > 0 and s.field_off + min(s.width, 4) <= len(code):
            ff = int.from_bytes(code[s.field_off:s.field_off + min(s.width, 4)], "little")
        sites.append(replace(s, value=v, final_field=ff))
    return replace(tb, slot=link.slot, record=link.record, code_fnv64=fnv1a64(link.body),
                   sites=tuple(sites), source=None)


def derive_scm(join_result: Any, links: Mapping[str, UnitLink], snapshot: Any
               ) -> tuple[list[tuple[str, bytes]], list[str]]:
    """``([(name, bytes)], [source capture name])`` for the derived captures.

    The import's binding is keyed by **input** slot (``TBUnit.slot``); each
    rewritten chunk carries the unit's **output** slot (``UnitLink.slot``), so
    the derived files bind to the written ``.sc`` even when the input had
    invalid records that :func:`write_sc` dropped."""
    if join_result is None:
        return [], []
    by_slot = {u.slot: links[u.uid] for u in snapshot.units() if u.uid in links}
    code_address = snapshot.header.code_address
    files: list[tuple[str, bytes]] = []
    names: list[str] = []
    for f in join_result.captures:
        bodies: list[Any] = []
        n_tb = 0
        for ch in f.chunks:
            b = ch.body
            if isinstance(b, S.TbChunk):
                sb = join_result.slots.get(b.slot)
                if sb is None or not sb.bound or sb.source is not f or b.slot not in by_slot:
                    continue
                bodies.append(_rewrite_tb(b, by_slot[b.slot], code_address))
                n_tb += 1
            elif isinstance(b, (S.RunInfo, S.HelperInfo, S.MapsChunk, S.RamblocksChunk,
                                S.EndChunk)):
                bodies.append(b)
        if not n_tb:
            continue  # every slot it bound is bound through an earlier capture (union)
        scm = S.make_scm(bodies, f.header)
        name = "cache.scm" if not files else f"cache.{len(files)}.scm"
        files.append((name, S.encode_scm(scm)))
        names.append(os.path.basename(f.path) if f.path else "<memory>")
    return files, names


def tb_counts(files: Iterable[tuple[str, bytes]]) -> dict[str, int]:
    return {n: len(S.parse_scm(b).tbs) for n, b in files}


__all__ += ["tb_counts", "round_up"]


# ===========================================================================
# NativeLinker v2 (B3/B4 design §2, §3.4, §3.7, §4.1, §5.2-5.3; WP18)
# ===========================================================================
#
# Primitives:
#
# * :func:`lookup_tails` / :func:`tail_problem`: the 11-byte lookup tails
#   ``mov rdi,rbp; call [rip+d]; jmp rax`` of a unit (GOTO_PTR site preceded
#   by the ``lookup_tb_ptr`` HELPER_CALL), with exit admission rule 1 of §3.1
#   (``exit_not_goto_ptr``, ``tail_is_branch_target``).
# * :func:`grow_unit`: replace a tail by ``jmp S; int3 x6`` and append the
#   §4.1 stub(s) after the pool (rows, icount, cflags, key unchanged; search
#   bytes moved, byte-identical). Optionally register the slots (v6: record
#   ``jmp_insn_offset[n]``/``jmp_reset_offset[n]``); unregistered slots reset
#   to ``jmp F`` (see :mod:`scopt.guard`).
# * :func:`rewrite_unit` / :func:`apply_pure_rewrite`: same-length patches
#   with an old-bytes check (PURE_REWRITE).
# * :class:`NativeLinkerV2`: a :class:`B3Plan` (grown units, variants,
#   rewrites) -> records and arena bytes, the ext table (v6) and the derived
#   captures (TB chunk v2 for grown units, VARIANT chunks). Records stay in
#   slot order; units that are not grown keep their offsets; grown units move
#   to ``ROUND_UP(max_end, 64)`` and up (slot order), variants after them
#   (plan order). A plan without slots and variants gives a v5 output.

from .guard import (GUARD_TEMPLATE_ID, TAIL_LEN, GuardError, GuardOffsets, GuardStub,
                    GuardTarget, emit_guard, guard_key)

__all__ += [
    "LookupTail", "lookup_tails", "tail_problem", "GuardedExitSpec", "GrownUnit", "grow_unit",
    "apply_pure_rewrite", "rewrite_unit", "VariantSpec", "B3Plan", "VariantLink",
    "LinkResultV2", "NativeLinkerV2", "R2", "exit_class_of", "variant_key", "free_slots",
    "derive_scm_v2", "tb_ret_abs_of",
    "trusted_firmware_entry_problem", "trusted_firmware_edge_problem",
    "TRUSTED_ENTRY_OFFSET",
]


class R2:
    """NativeLinker v2 reason codes (B3/B4 design §3.8 where one exists)."""

    EXIT_NOT_GOTO_PTR = "exit_not_goto_ptr"
    TAIL_IS_BRANCH_TARGET = "tail_is_branch_target"
    NO_FREE_SLOT = "no_free_slot"
    UNIT_NOT_RELOCATABLE = "unit_not_relocatable"
    NO_META = "no_meta"
    PATCH_LENGTH = "patch_length"
    PATCH_OLD_BYTES = "patch_old_bytes"
    PATCH_OVERLAP = "patch_overlap"
    EDIT_ON_FIXUP = "edit_on_fixup"
    EDIT_IN_TAIL = "edit_in_tail"
    SPEC_UNREGISTERED = "spec_slot_unregistered"
    LINK_MISSING = "link_missing"
    EXT_TOO_LARGE = "ext_table_too_large"
    CONTEXT_BUDGET = "context_budget"
    VARIANT_PRED = "variant_pred_slot"
    CONTINUATION_KEY_MISMATCH = "continuation_key_mismatch"
    UNKNOWN_UNIT = "unknown_unit"


_RET_NONE = 0xFFFF


@dataclass(frozen=True)
class LookupTail:
    """One 11-byte lookup tail of a unit (offsets relative to tc)."""

    off: int  # T
    insn_idx: int | None  # the guest insn the tail belongs to
    helper_site: int  # index into u.sites of the lookup HELPER_CALL
    goto_ptr_site: int  # index into u.sites of the GOTO_PTR
    helper_id: int
    func: int
    pool_slot_off: int  # the pool slot the call reads
    reg: int  # GOTO_PTR register (aux)
    raw: bytes

    @property
    def end(self) -> int:
        return self.off + TAIL_LEN


def lookup_tails(u: TBUnit) -> tuple[LookupTail, ...]:
    """Every ``mov rdi,rbp; call [rip+d]; jmp rax`` whose call is a POOL_INDIRECT
    HELPER_CALL site of ``lookup_tb_ptr`` and whose jump is a GOTO_PTR site."""
    from .formats.enums import Encoding, SiteKind

    helpers = {h.id: h for h in u.bindings.helpers}
    out = []
    by_insn = {s.insn_off: (i, s) for i, s in enumerate(u.sites) if s.kind == SiteKind.HELPER_CALL}
    pool_by_user = {p.user_field_off: p for p in u.pool}
    for gi, g in enumerate(u.sites):
        if g.kind != SiteKind.GOTO_PTR:
            continue
        t = g.insn_off - 9
        hit = by_insn.get(t + 3)
        if t < 0 or hit is None:
            continue
        hi, h = hit
        hb = helpers.get(h.aux)
        name = hb.name if hb is not None else None
        raw = bytes(u.code[t:t + TAIL_LEN])
        if name != "lookup_tb_ptr" or h.enc != Encoding.POOL_INDIRECT or h.field_off != t + 5:
            continue
        if raw[:3] not in (b"\x48\x8b\xfd", b"\x48\x89\xef") or raw[3:5] != b"\xff\x15" or \
                raw[9:11] != b"\xff\xe0":
            continue
        p = pool_by_user.get(t + 5)
        if p is None:
            continue
        out.append(LookupTail(t, h.insn_idx, hi, gi, h.aux, h.value, p.slot_off, g.aux, raw))
    return tuple(out)


def tail_problem(u: TBUnit, t: LookupTail, code_address: int) -> str | None:
    """§3.1 rule 1 beyond the tail shape: no captured branch label targets an
    offset strictly inside ``(T, T+11)``; no fixup field inside the tail."""
    from .formats.enums import SiteKind

    tc_abs = code_address + u.tc_offset
    lab = (SiteKind.LABEL_JMP, SiteKind.LABEL_JCC, SiteKind.LDST_FAST_JCC,
           SiteKind.LDST_RET_JMP, SiteKind.MOVI_LDST_RA)
    for s in u.sites:
        if s.kind in lab and t.off < s.value - tc_abs < t.end:
            return R2.TAIL_IS_BRANCH_TARGET
    for f in u.reloc.fixups:
        if t.off <= f.field_off < t.end:
            return R2.EXIT_NOT_GOTO_PTR
    return None


def free_slots(u: TBUnit) -> tuple[int, ...]:
    """Slots ``n`` with ``jmp_insn_offset[n] == jmp_reset_offset[n] == 0xffff``."""
    r = ScRecord.from_bytes(u.record)
    return tuple(n for n in (0, 1) if r.jmp_insn_offset[n] == _RET_NONE
                 and r.jmp_reset_offset[n] == _RET_NONE)


def exit_class_of(u: TBUnit, t: LookupTail, guest_bytes: bytes | None, *,
                  page_bits: int = 10) -> str:
    """``return`` | ``static`` | ``indirect_multiway`` (§3.1 rule 2) for tail ``t``.

    ``static``: the tail is the off-page fallback of a static exit (StaticExit
    slot 0xFF at the tail's insn). ``return``: the guest insn of the tail is
    ``bx lr``, ``pop {…,pc}``, ``ldm sp!,{…,pc}`` or ``ldr pc,[sp],#k``
    (Thumb decode of ``guest_bytes``; without them only ``static`` is
    decidable)."""
    from .formats.enums import STATIC_EXIT_OFFPAGE_SLOT

    for x in u.static_exits:
        if x.slot == STATIC_EXIT_OFFPAGE_SLOT and x.insn_idx == t.insn_idx:
            return "static"
    if guest_bytes is None or t.insn_idx is None or t.insn_idx >= len(u.insns):
        return "indirect_multiway"
    from .decode.thumb import thumb_decoder

    pmask = (1 << page_bits) - 1
    starts = [i.words[0] & pmask for i in u.insns]
    base = starts[0]
    a = (starts[t.insn_idx] - base) & pmask
    b = (starts[t.insn_idx + 1] - base) & pmask if t.insn_idx + 1 < len(starts) else u.guest_size
    ins = list(thumb_decoder().disasm(bytes(guest_bytes[a:b]), 0))
    if len(ins) != 1:
        return "indirect_multiway"
    m, ops = ins[0].mnemonic, ins[0].op_str.replace(" ", "")
    if m == "bx" and ops == "lr":
        return "return"
    if m in ("pop", "pop.w") and "pc" in ops:
        return "return"
    if m in ("ldm", "ldm.w", "ldmia", "ldmia.w", "ldmfd") and ops.startswith("sp!") and "pc" in ops:
        return "return"
    if m in ("ldr", "ldr.w") and ops.startswith("pc,[sp],"):
        return "return"
    return "indirect_multiway"


@dataclass(frozen=True)
class GuardedExitSpec:
    """One guarded exit of R: the tail at ``tail_off`` and its 1-2 targets.
    ``key_flags``/``key_cs_base`` are the continuation key ``key_C`` (§4.1)."""

    tail_off: int
    exit_class: str  # "return" | "static"
    targets: tuple[GuardTarget, ...]
    key_flags: int
    key_cs_base: int


@dataclass(frozen=True)
class GrownUnit:
    unit: TBUnit  # the grown unit, at R's original offsets (end extended)
    source: TBUnit  # the unit before growth
    stubs: tuple[tuple[GuardedExitSpec, GuardStub], ...]
    registered: bool

    @property
    def stub_start(self) -> int:
        return self.unit.stub_start  # type: ignore[return-value]

    @property
    def stub_bytes(self) -> int:
        return self.unit.tc_size - self.source.tc_size


def tb_ret_abs_of(snapshot: Any) -> int | None:
    """``tb_ret_addr`` of the snapshot's run (RUN_INFO), else from any
    EXIT_TB_RET_JMP site value."""
    ri = getattr(snapshot, "run_info", None)
    if ri is not None:
        return ri.code_address + ri.tb_ret_off
    from .formats.enums import SiteKind

    for u in snapshot.units():
        for s in u.sites:
            if s.kind == SiteKind.EXIT_TB_RET_JMP:
                return s.value
    return None


def _content(record: bytes, code: bytes, search: bytes, tag: Mapping[str, Any]) -> str:
    import hashlib

    from .canon import canonical_json

    h = hashlib.sha256()
    h.update(record)
    h.update(code)
    h.update(search)
    h.update(canonical_json(dict(tag)))
    return "sha256:" + h.hexdigest()


def grow_unit(u: TBUnit, exits: Sequence[GuardedExitSpec], *, code_address: int,
              tb_ret_abs: int, offsets: GuardOffsets = GuardOffsets(),
              register: bool = True) -> GrownUnit:
    """Grow ``u``: each exit's tail becomes ``jmp S_k; int3 x6`` and its stub
    ``S_k`` is appended after the pool (§2, §4.1). Raises :class:`LinkError`
    (reason ``exit_not_goto_ptr``, ``tail_is_branch_target``,
    ``no_free_slot``, ``unit_not_relocatable``, ``no_meta`` or a
    :class:`~scopt.guard.GuardError` reason).

    The result is at R's original offsets with ``end_offset`` extended by the
    stub bytes; NativeLinker v2 always moves it. Metadata: the tail's
    HELPER_CALL/GOTO_PTR sites and OpMarks move to ``F``; the tail gets a
    LABEL_JMP site; each stub contributes its sites/OpMarks; the pool row of
    the tail call now names ``F``'s call; the tail call's recovery point is
    dropped (F's call never unwinds, §6.1); the GENERAL reset's ``jmp
    tb_ret`` is a new ``rel_minus_delta`` fixup. Search bytes are unchanged.
    """
    from .formats.enums import Encoding, OpMarkKind, SiteKind
    from .model.unit import OpMark, Site

    if u.meta_status == "no_meta":
        raise LinkError(R2.NO_META, "no capture metadata", u.uid)
    if not u.reloc.relocatable:
        raise LinkError(R2.UNIT_NOT_RELOCATABLE, ",".join(u.reloc.reasons), u.uid)
    if (u.native_fragments and not u.splice_applied) or u.inline_start is not None:
        raise LinkError("native_layout_unsupported", "native islands with guard growth", u.uid)
    if u.stub_start is not None:
        raise LinkError(R2.EXIT_NOT_GOTO_PTR, "unit is already grown", u.uid)
    if not exits:
        raise LinkError(R2.EXIT_NOT_GOTO_PTR, "no exit to guard", u.uid)
    tails = {t.off: t for t in lookup_tails(u)}
    rec = ScRecord.from_bytes(u.record)
    free = set(free_slots(u))
    used: set[int] = set()
    tc_abs = code_address + u.tc_offset
    struct_abs = code_address + u.tb_offset
    code = bytearray(u.code)
    stub_start = len(u.code)
    cursor = stub_start
    sites = list(u.sites)
    drop_sites: set[int] = set()
    new_sites: list[Site] = []
    marks = list(u.opmarks)
    drop_marks: set[int] = set()
    new_marks: list[OpMark] = []
    pool = list(u.pool)
    fixups = list(u.reloc.fixups)
    drop_rec: set[int] = set()
    jio, jro = list(rec.jmp_insn_offset), list(rec.jmp_reset_offset)
    stubs = []
    seen_tails: set[int] = set()
    native_spans = ()
    if u.splice_applied:
        from .splice_layout import splice_group_offsets

        native_offsets = splice_group_offsets(u.native_fragments)
        native_spans = tuple((native_offsets(c.splice.start_off), len(c.splice.code))
                             for c in u.native_fragments)
    for spec in exits:
        t = tails.get(spec.tail_off)
        if t is None:
            raise LinkError(R2.EXIT_NOT_GOTO_PTR, f"no lookup tail at +{spec.tail_off:#x}", u.uid)
        if t.off in seen_tails:
            raise LinkError(R2.EXIT_NOT_GOTO_PTR, f"tail +{t.off:#x} guarded twice", u.uid)
        if any(_overlaps(t.off, t.end, start, start + size) for start, size in native_spans):
            raise LinkError("native_guard_overlap", "guard tail overlaps native body", u.uid)
        seen_tails.add(t.off)
        prob = tail_problem(u, t, code_address)
        if prob:
            raise LinkError(prob, f"tail +{t.off:#x}", u.uid)
        for tg in spec.targets:
            if tg.slot_n not in free or tg.slot_n in used:
                raise LinkError(R2.NO_FREE_SLOT, f"slot {tg.slot_n} of +{t.off:#x}", u.uid)
            used.add(tg.slot_n)
        targets = tuple(replace(tg, registered=register) if
                        tg.registered != register else tg for tg in spec.targets)
        try:
            kf, kf2 = guard_key(spec.key_flags, spec.key_cs_base)
            st = emit_guard(stub_off=cursor, tail=t.raw, pool_slot_off=t.pool_slot_off,
                            lookup_helper_id=t.helper_id, lookup_func=t.func, kf=kf, kf2=kf2,
                            targets=targets, tc_abs=tc_abs, struct_abs=struct_abs,
                            tb_ret_abs=tb_ret_abs, goto_ptr_reg=t.reg, offsets=offsets)
        except GuardError as e:
            raise LinkError(e.reason, e.detail, u.uid) from None
        stubs.append((replace(spec, targets=targets), st))
        cursor = st.end
        code += st.code
        # the tail -> jmp S ; int3 x6
        rel = st.off - (t.off + 5)
        code[t.off:t.end] = b"\xe9" + rel.to_bytes(4, "little", signed=True) + b"\xcc" * 6
        drop_sites |= {t.helper_site, t.goto_ptr_site}
        ii = 0xFFFFFFFF if t.insn_idx is None else t.insn_idx
        new_sites.append(Site(t.off + 1, t.off, 4, Encoding.REL32, SiteKind.LABEL_JMP, 0,
                              tc_abs + st.off, t.insn_idx, rel & 0xFFFFFFFF))
        for k, m in enumerate(marks):
            if (m.kind == OpMarkKind.CALL and m.host_off == t.off and m.helper_id == t.helper_id) \
                    or (m.kind == OpMarkKind.GOTO_PTR and m.host_off == t.off + 9):
                drop_marks.add(k)
        mi = _RET_NONE if t.insn_idx is None else t.insn_idx
        for (off, kind, hid) in st.opmarks:
            new_marks.append(OpMark(off, OpMarkKind(kind), mi, hid))
        for (fo, io, w, enc, kind, aux, val) in st.sites:
            raw = int.from_bytes(code[fo:fo + w], "little") if w else 0
            new_sites.append(Site(fo, io, w, Encoding(enc), SiteKind(kind), aux, val,
                                  None if ii == 0xFFFFFFFF else ii, raw))
        pool = [replace(p, user_field_off=st.f_off + 5) if p.user_field_off == t.off + 5 else p
                for p in pool]
        for (fo, w, rule, sk, cls, tgt) in st.fixups:
            fixups.append(Fixup(fo, w, rule, sk, cls, tgt))  # type: ignore[arg-type]
        for k, p in enumerate(u.recovery):
            if p.source == "helper_call" and p.host_return_offset == t.off + 9:
                drop_rec.add(k)
        if register:
            for si in st.slots:
                jio[si.slot_n], jro[si.slot_n] = si.field_off, si.reset_off
    # fix up the final_field of the tail jumps (computed above) and assemble
    sites = [s for k, s in enumerate(sites) if k not in drop_sites] + new_sites
    marks = [m for k, m in enumerate(marks) if k not in drop_marks] + new_marks
    recovery = tuple(p for k, p in enumerate(u.recovery) if k not in drop_rec)
    recovery = tuple(replace(p, id=f"r{i}") for i, p in enumerate(recovery))
    code_b = bytes(code)
    end = u.tc_offset + len(code_b) + len(u.search)
    nrec = rec.replace(tc_size=len(code_b), end_offset=end, jmp_insn_offset=tuple(jio),
                       jmp_reset_offset=tuple(jro)).to_bytes(fix_checksum=True)
    templates = sorted({stub.template_id for _, stub in stubs}) or [GUARD_TEMPLATE_ID]
    tag = {"grown_from": u.content_hash, "template": templates[0] if len(templates) == 1 else templates,
           "stubs": [[st.off, len(st.code)] for _, st in stubs], "registered": register}
    g = replace(u, code=code_b, record=nrec, tc_size=len(code_b), end_offset=end,
                sites=tuple(sites), opmarks=tuple(marks), pool=tuple(pool),
                reloc=replace(u.reloc, fixups=tuple(sorted(fixups, key=lambda f: f.field_off))),
                recovery=recovery, stub_start=stub_start,
                content_hash=_content(nrec, code_b, u.search, tag))
    return GrownUnit(g, u, tuple(stubs), register)


def apply_pure_rewrite(code: bytes, patches: Iterable[Any], *,
                       forbid: Iterable[tuple[int, int]] = (), uid: str = "") -> bytes:
    """Apply same-length patches (``host_off``, ``old_bytes``, ``new_bytes``)
    with the old-bytes check; ``forbid`` are ``[a, b)`` ranges no patch may touch
    (fixup fields, lookup tails). Raises :class:`LinkError`."""
    out = bytearray(code)
    spans: list[tuple[int, int]] = []
    for p in sorted(patches, key=lambda p: p.host_off):
        old, new = bytes(p.old_bytes), bytes(p.new_bytes)
        a, b = p.host_off, p.host_off + len(old)
        if len(old) != len(new) or not old:
            raise LinkError(R2.PATCH_LENGTH, f"@{a}: {len(old)} -> {len(new)} bytes", uid)
        if a < 0 or b > len(out):
            raise LinkError(R2.PATCH_LENGTH, f"@{a}+{len(old)} outside the code", uid)
        if any(_overlaps(a, b, x, y) for x, y in spans):
            raise LinkError(R2.PATCH_OVERLAP, f"@{a}", uid)
        if bytes(code[a:b]) != old:
            raise LinkError(R2.PATCH_OLD_BYTES, f"@{a}: {bytes(code[a:b]).hex()} != {old.hex()}",
                            uid)
        if any(_overlaps(a, b, x, y) for x, y in forbid):
            raise LinkError(R2.EDIT_ON_FIXUP, f"@{a}+{len(old)} touches a protected field", uid)
        spans.append((a, b))
        out[a:b] = new
    return bytes(out)


def _protected(u: TBUnit) -> list[tuple[int, int]]:
    out = [(f.field_off, f.field_off + f.width) for f in u.reloc.fixups]
    out += [(t.off, t.end) for t in lookup_tails(u)]
    return out


def rewrite_unit(u: TBUnit, patches: Sequence[Any]) -> TBUnit:
    """``u`` with same-length patches applied in place (offsets unchanged)."""
    if u.native_fragments or u.splice_applied:
        from .splice_layout import caller_patch_problem, integer_certificate_ranges

        try:
            protected = integer_certificate_ranges(u)
        except ValueError as exc:
            raise LinkError("native_layout_unsupported", str(exc), u.uid) from exc
        for patch in patches:
            problem = caller_patch_problem(u, patch, protected)
            if problem:
                raise LinkError("native_caller_rewrite", problem, u.uid)
    code = apply_pure_rewrite(u.code, patches, forbid=_protected(u), uid=u.uid)
    tag = {"rewritten_from": u.content_hash,
           "patches": [[p.host_off, bytes(p.old_bytes).hex(), bytes(p.new_bytes).hex()]
                       for p in sorted(patches, key=lambda p: p.host_off)]}
    return replace(u, code=code, content_hash=_content(u.record, code, u.search, tag))


def variant_key(c_uid: str, r_uid: str, exit_id: str, facts: Iterable[Any],
                patches: Iterable[Any]) -> str:
    """``variant_id = sha256(C.uid, R.uid, E, sorted(F), patch set)`` (§3.4)."""
    from .canon import canonical_json, sha256_id

    fs = sorted(json.dumps(f if isinstance(f, (dict, list, str, int)) else
                           getattr(f, "to_json", lambda: repr(f))(), sort_keys=True)
                for f in facts)
    ps = sorted([p.host_off, bytes(p.old_bytes).hex(), bytes(p.new_bytes).hex()]
                for p in patches)
    return sha256_id(canonical_json({"C": c_uid, "R": r_uid, "E": exit_id, "facts": fs,
                                     "patches": ps}))


def spec_binding_problem(spec: GuardedExitSpec, slot_n: int, c: TBUnit) -> str | None:
    """Why the SPEC slot ``slot_n`` of ``spec`` may not enter a clone of ``c``
    (``None``: it may). The guard's constants must be ``c``'s own lookup key:
    ``(Kf, Kf2) == guard_key(C.flags, C.cs_base)`` (which also requires C's key
    to be admissible) and the slot's ``Vc`` must be one of C's virtual
    addresses (invariant 2; V13 re-checks this on the written bytes)."""
    from .guard import GuardError, guard_key

    tg = next((t for t in spec.targets if t.slot_n == slot_n), None)
    if tg is None:
        return f"no target with slot {slot_n}"
    if tg.kind != "SPEC":
        return f"slot {slot_n} is {tg.kind}"
    try:
        want = guard_key(c.key.flags, c.key.cs_base)
    except GuardError as e:
        return f"C's key: {e.detail}"
    try:
        got = guard_key(spec.key_flags, spec.key_cs_base)
    except GuardError as e:
        return f"guard key: {e.detail}"
    if got != want:
        return (f"guard Kf/Kf2 {got[0]:#x}/{got[1]:#x} != C's {want[0]:#x}/{want[1]:#x}")
    vas = {v & ~1 for v in (c.vaddrs or ())}
    if not vas:
        return "C has no known virtual address"
    if tg.vaddr not in vas:  # exact: a Vc with bit 0 set is not C's pc
        return f"Vc {tg.vaddr:#x} is not a virtual address of C"
    return None


@dataclass(frozen=True)
class VariantSpec:
    """A variant ``C'`` of ``src_uid`` entered only through ``(pred_uid, slot_n)``."""

    src_uid: str
    pred_uid: str
    slot_n: int
    patches: tuple[Any, ...] = ()  # PURE_REWRITE patches on the clone (relative to tc)
    facts: tuple[Any, ...] = ()
    key: str | None = None  # variant_id; default variant_key(...)
    exit_id: str = ""

    def variant_id(self) -> str:
        return self.key or variant_key(self.src_uid, self.pred_uid,
                                       self.exit_id or f"slot{self.slot_n}", self.facts,
                                       self.patches)


@dataclass
class B3Plan:
    """What NativeLinker v2 materializes (built by the manager from typed edits)."""

    grow: Mapping[str, tuple[GuardedExitSpec, ...]] = field(default_factory=dict)
    variants: tuple[VariantSpec, ...] = ()
    rewrites: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)
    register_slots: bool = True  # v6; False: v5 artifact (GENERAL slots not registered)
    force_v6: bool = False  # identity-v6 (empty ext table)
    max_variants_per_c: int = 2
    max_variants: int = 64
    max_code_growth_bytes: int | None = None
    max_total_growth_bytes: int | None = None
    order_private_by_predecessor: bool = False
    firmware_regions: tuple[Any, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.grow or self.variants or self.rewrites or self.firmware_regions)


@dataclass(frozen=True)
class VariantLink:
    index: int
    key: str
    src_uid: str
    pred_uid: str
    slot_n: int
    exit_class: int
    tb_offset: int
    tc_offset: int
    tc_size: int
    end_offset: int
    body: bytes  # [tc', end')
    patches: tuple[Any, ...]
    clone_patched: tuple[tuple[int, int], ...]  # fixup fields changed by the relocation
    facts: tuple[Any, ...] = ()

    @property
    def code(self) -> bytes:
        return self.body[:self.tc_size]


@dataclass(frozen=True)
class FirmwareMemberLink:
    """One private source-TB copy in a variable-size firmware region."""

    source_uid: str
    inline_context: tuple[str, ...]
    tb_offset: int
    tc_offset: int
    tc_size: int
    search_offset: int
    search_size: int
    descriptor: bytes
    code: bytes
    search: bytes
    source_hash: str
    guest_vaddr: int
    patched_edges: tuple[tuple[str, int, int], ...] = ()
    effective_unit: TBUnit | None = field(default=None, repr=False)


@dataclass(frozen=True)
class FirmwareRegionLink:
    region_id: str
    callsite_id: str
    entry_member: int
    members: tuple[FirmwareMemberLink, ...]
    source_units: tuple[str, ...]
    start_offset: int
    end_offset: int
    trusted: bool = False
    trusted_entry_offset: int = 0

    @property
    def emitted_size(self) -> int:
        return self.end_offset - self.start_offset


@dataclass
class LinkResultV2(LinkResult):
    version: int = 5
    ext: Any = None  # scopt.formats.sc_v6.ExtTable (v6)
    variants: tuple[VariantLink, ...] = ()
    grown: Mapping[str, GrownUnit] = field(default_factory=dict)
    rewritten: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)
    effective: Mapping[str, TBUnit] = field(default_factory=dict)  # uid -> unit before placement
    firmware_regions: tuple[FirmwareRegionLink, ...] = ()
    firmware_table_offset: int = 0
    firmware_table_size: int = 0

    @property
    def max_end(self) -> int:  # type: ignore[override]
        vm = max((v.end_offset for v in self.variants), default=0)
        rm = max((r.end_offset for r in self.firmware_regions), default=0)
        tm = self.firmware_table_offset + self.firmware_table_size
        return max(super().max_end, vm, rm, tm)

    def to_json(self) -> dict[str, Any]:
        d = super().to_json()
        d.update(version=self.version, grown=sorted(self.grown),
                 rewritten=sorted(self.rewritten),
                 variants=[{"index": v.index, "key": v.key, "src": v.src_uid,
                            "pred": v.pred_uid, "slot_n": v.slot_n,
                            "tb_offset": hexaddr(v.tb_offset), "patches": len(v.patches)}
                           for v in self.variants],
                 ext=None if self.ext is None else {
                     "n_variants": len(self.ext.variants), "n_slots": len(self.ext.slots),
                     "n_links": len(self.ext.links),
                     "artifact_id8": bytes(self.ext.artifact_id8).hex()},
                 firmware_regions=[{"region_id": r.region_id,
                                    "callsite_id": r.callsite_id,
                                    "members": len(r.members),
                                    "trusted": r.trusted,
                                    "trusted_entry_offset": r.trusted_entry_offset,
                                    "emitted_size": r.emitted_size,
                                    "source_identities": [m.source_hash
                                                          for m in r.members],
                                    "inline_contexts": [list(m.inline_context)
                                                        for m in r.members]}
                                   for r in self.firmware_regions])
        return d


class NativeLinkerV2:
    """Materialize a :class:`B3Plan` on ``snapshot`` (see the section comment)."""

    def __init__(self, snapshot: Any, plan: B3Plan, *, join_result: Any = None,
                 derive_captures: bool = True, offsets: GuardOffsets | None = None,
                 tb_ret_abs: int | None = None, placement: WorkingSet | None = None):
        self.snap = getattr(getattr(snapshot, "working_set", snapshot), "base", snapshot)
        self.plan = plan
        self.header: ScHeader = self.snap.header
        self.join_result = join_result
        self.derive_captures = derive_captures
        self.offsets = offsets or GuardOffsets()
        self.tb_ret_abs = tb_ret_abs if tb_ret_abs is not None else tb_ret_abs_of(self.snap)
        self.placement = placement
        if placement is not None and placement.base is not self.snap:
            raise ValueError("layout proposals must belong to the B3 input snapshot")

    def _fail(self, uid: str, reason: str, detail: str = "") -> LinkResultV2:
        return LinkResultV2(False, self.header, (), (LinkFailure(uid, reason, detail),))

    def _firmware_member_units(self, region: Any, by_uid: Mapping[str, TBUnit]
                               ) -> tuple[list[TBUnit], dict[tuple[int, str], int]]:
        """Build member preimages and bind each guard's rel32 to its original exit ID."""
        from .formats.region_v1 import unit_exits

        if region.trusted:
            for member in region.members:
                source = by_uid[member.source_uid]
                problem = trusted_firmware_entry_problem(source)
                if problem is not None:
                    raise LinkError("firmware_region_trusted_prefix",
                                    f"{source.uid}: {problem}", source.uid)
                lo = self.header.code_address + source.tb_offset
                hi = lo + self.header.tb_size
                bad_refs = [fixup for fixup in source.reloc.fixups
                            if fixup.target_abs is not None and lo <= fixup.target_abs < hi and
                            fixup.site_kind != int(SiteKind.MOVI_EXIT_TB_PTR)]
                if bad_refs:
                    raise LinkError("firmware_region_trusted_descriptor_ref",
                                    f"{source.uid}: unclassified descriptor reference",
                                    source.uid)
        out: list[TBUnit] = []
        guarded_fields: dict[tuple[int, str], int] = {}
        by_source: dict[int, list[Any]] = {}
        for edge in region.edges:
            if edge.target_member is not None and (not region.trusted or edge.kind == "return"):
                by_source.setdefault(edge.source_member, []).append(edge)
        for i, member in enumerate(region.members):
            source = by_uid[member.source_uid]
            if i not in by_source:
                out.append(source)
                continue
            exits = {x.id: x for x in unit_exits(source)}
            specs: list[GuardedExitSpec] = []
            tail_exits = {}
            free = iter(free_slots(source))
            tails = tuple(lookup_tails(source))
            for edge in by_source.get(i, ()):
                exit_ = exits.get(edge.exit_id)
                if exit_ is None or exit_.kind.value != "goto_ptr":
                    continue
                tail = next((t for t in tails if t.insn_idx == exit_.insn), None)
                if tail is None:
                    raise LinkError("firmware_region_exit_shape",
                                    f"{source.uid}/{edge.exit_id}: no lookup tail", source.uid)
                if tail.off in tail_exits:
                    raise LinkError("firmware_region_exit_shape",
                                    f"{source.uid}: two internal targets on one tail", source.uid)
                try:
                    slot = next(free)
                except StopIteration:
                    raise LinkError("firmware_region_slot_representation",
                                    f"{source.uid}: private guard slots exhausted", source.uid)
                target = region.members[edge.target_member]
                key = by_uid[target.source_uid].key
                specs.append(GuardedExitSpec(
                    tail.off, "return" if edge.kind == "return" else "static",
                    # A firmware-region edge is an unregistered private jump.
                    # GENERAL supplies a guard-and-fallback slot whose rel32
                    # field we patch to the copied destination; SPEC is a v6
                    # public variant contract and requires loader registration.
                    (GuardTarget(int(edge.guest_target), slot, "GENERAL"),),
                    key.flags, key.cs_base))
                tail_exits[tail.off] = edge.exit_id
            if specs:
                if self.tb_ret_abs is None:
                    raise LinkError("firmware_region_no_tb_ret", "no tb_ret_addr", region.region_id)
                if region.trusted and self.offsets.envlayout_id is None:
                    raise LinkError("guard_layout_unbound", "private return needs matching EnvLayout",
                                    source.uid)
                grown = grow_unit(source, specs, code_address=self.header.code_address,
                                  tb_ret_abs=self.tb_ret_abs, offsets=self.offsets,
                                  register=False)
                source = grown.unit
                for spec, stub in grown.stubs:
                    guarded_fields[i, tail_exits[spec.tail_off]] = stub.slots[0].field_off
            out.append(source)
        return out, guarded_fields

    def _link_firmware_regions(self, cursor: int, by_uid: Mapping[str, TBUnit]
                               ) -> tuple[tuple[FirmwareRegionLink, ...], int]:
        """Lay out private descriptors, contiguous code and out-of-line recovery data."""
        from .formats.region_v1 import unit_exits

        linked: list[FirmwareRegionLink] = []
        ca = self.header.code_address
        for plan in self.plan.firmware_regions:
            start = round_up(cursor, TB_ALIGN)
            effective, guarded_fields = self._firmware_member_units(plan, by_uid)
            descriptor_start = start
            descriptor_count = 1 if plan.trusted else len(effective)
            code_start = round_up(descriptor_start + descriptor_count * self.header.tb_size, 16)
            # Keep every executable member span contiguous.  Recovery tables
            # live after the complete code span and the v7 loader binds each
            # private TB to its explicit search_offset.  Interleaving search
            # bytes here would turn a legal fall-through into execution of
            # metadata and would defeat the defining property of this pass.
            tc_offsets: list[int] = []
            at = code_start
            for source in effective:
                tc_offsets.append(at)
                at += source.tc_size
            search_offsets: list[int] = []
            if plan.trusted:
                search_offsets = [at] * len(effective)
            else:
                for source in effective:
                    search_offsets.append(at)
                    at += len(source.search)
            member_links: list[FirmwareMemberLink] = []
            for i, (member, source) in enumerate(zip(plan.members, effective)):
                fake_tb = tc_offsets[i] - (source.tc_offset - source.tb_offset)
                relocated = relocate_unit(source, fake_tb)
                code = bytearray(relocated.body[:source.tc_size])
                member_tb = descriptor_start if plan.trusted else \
                    descriptor_start + i * self.header.tb_size
                retarget_private_descriptor_refs(
                    code, source, tc_offset=tc_offsets[i],
                    tb_offset=member_tb, code_address=ca)
                # Plans name exits in the captured source. Guard growth adds exits and
                # renumbers the fallback, so look up original IDs on that source.
                exits = {x.id: x for x in unit_exits(by_uid[member.source_uid])}
                patched: list[tuple[str, int, int]] = []
                for edge in plan.edges:
                    if edge.source_member != i or edge.target_member is None:
                        continue
                    exit_ = exits.get(edge.exit_id)
                    field = guarded_fields.get((i, edge.exit_id))
                    if field is None and edge.kind != "return" and exit_ is not None:
                        field = exit_.insn_offset
                    if field is None and plan.trusted and edge.kind != "return" and \
                            exit_ is not None and exit_.kind.value == "goto_ptr":
                        tails = [tail for tail in lookup_tails(source)
                                 if tail.insn_idx == exit_.insn]
                        if len(tails) == 1:
                            tail = tails[0]
                            code[tail.off:tail.end] = b"\xe9\0\0\0\0" + b"\x90" * 6
                            field = tail.off + 1
                    if field is None or field < 1 or field + 4 > len(code):
                        raise LinkError("firmware_region_exit_shape",
                                        f"{source.uid}/{edge.exit_id}: no rel32 field", source.uid)
                    target = tc_offsets[edge.target_member] + (
                        plan.trusted_entry_offset if plan.trusted else 0)
                    disp = target - (tc_offsets[i] + field + 4)
                    if not int32_ok(disp):
                        raise LinkError(R.REL32_RANGE,
                                        f"firmware edge {source.uid}+{field:#x}", source.uid)
                    struct.pack_into("<i", code, field, disp)
                    patched.append((edge.exit_id, field, edge.target_member))
                member_links.append(FirmwareMemberLink(
                    member.source_uid, member.inline_context,
                    member_tb, tc_offsets[i], len(code), search_offsets[i],
                    0 if plan.trusted else len(source.search),
                    b"\0" * self.header.tb_size, bytes(code),
                    b"" if plan.trusted else source.search,
                    member.source_hash, source.vaddrs[0],
                    tuple(patched), source))
            end = at
            linked.append(FirmwareRegionLink(
                plan.region_id, plan.callsite.callsite_id, plan.entry_member,
                tuple(member_links), plan.source_uids, start, end,
                plan.trusted, plan.trusted_entry_offset))
            cursor = end
        return tuple(linked), cursor

    def effective_units(self, units: Iterable[TBUnit]) -> tuple[dict[str, TBUnit],
                                                                dict[str, GrownUnit]]:
        """Apply the semantic edits at input offsets before placement."""
        plan = self.plan
        if plan.grow and self.tb_ret_abs is None:
            raise LinkError(R2.EXIT_NOT_GOTO_PTR, "no tb_ret_addr", next(iter(plan.grow)))
        effective, grown = {}, {}
        for unit in units:
            uid = unit.uid
            if uid in plan.rewrites:
                unit = rewrite_unit(unit, plan.rewrites[uid])
            if uid in plan.grow:
                result = grow_unit(unit, plan.grow[uid], code_address=self.header.code_address,
                                   tb_ret_abs=self.tb_ret_abs, offsets=self.offsets,
                                   register=plan.register_slots)
                grown[uid] = result
                unit = result.unit
            effective[uid] = unit
        return effective, grown

    def link(self) -> LinkResultV2:
        from .formats.sc_v6 import (EXIT_RETURN, EXIT_STATIC, KIND_GENERAL, KIND_SPEC,
                                    VARIANT_FACTS, ExtTable, LinkEntry, SlotEntry, VariantEntry,
                                    max_ext_entries)

        plan, h = self.plan, self.header
        ca = h.code_address
        units = list(self.snap.units())
        if plan.firmware_regions and (plan.grow or plan.variants or plan.rewrites
                                      or self.placement is not None):
            return self._fail("", "firmware_region_terminal_stage",
                              "firmware-inline must be the terminal isolated stage")
        cloned = {v.src_uid for v in plan.variants}
        for unit in units:
            if unit.native_fragments and (not unit.splice_applied or unit.uid in cloned):
                return self._fail(unit.uid, "native_layout_unsupported",
                                  "native islands and cloning a native certificate require a new proof")
        by_uid = {u.uid: u for u in units}
        for uid in list(plan.grow) + list(plan.rewrites) + \
                [v.src_uid for v in plan.variants] + [v.pred_uid for v in plan.variants]:
            if uid not in by_uid:
                return self._fail(uid, R2.UNKNOWN_UNIT)
        try:
            eff, grown = self.effective_units(units)
        except LinkError as e:
            return self._fail(e.uid or "", e.reason, e.detail)
        # -- placement ---------------------------------------------------------
        limit = arena_limit(h)
        cursor = round_up(max((u.end_offset for u in units), default=0), TB_ALIGN)
        new_tb: dict[str, int] = {}
        if self.placement is not None:
            placer = NativeLinker(self.placement, derive_captures=False)
            new_tb = placer.plan(effective=eff, append_unordered=frozenset(grown))
            failures = placer._check_plan(new_tb, eff)
            if failures:
                return LinkResultV2(False, h, (), tuple(failures))
            cursor = max((new_tb[u.uid] + u.end_offset - u.tb_offset for u in eff.values()),
                         default=h.prologue_end)
        else:
            for u in units:
                if u.uid in grown:
                    new_tb[u.uid] = round_up(cursor, TB_ALIGN)
                    cursor = new_tb[u.uid] + (eff[u.uid].end_offset - eff[u.uid].tb_offset)
        links: list[UnitLink] = []
        try:
            for out_slot, u in enumerate(units):
                e = eff[u.uid]
                lu = relocate_unit(e, new_tb.get(u.uid, e.tb_offset))
                if u.uid not in grown and not lu.moved:
                    lu = replace(lu, record=bytes(u.record))
                links.append(replace(lu, slot=out_slot, input_slot=u.slot))
        except LinkError as e:
            return self._fail(e.uid or "", e.reason, e.detail)
        if cursor >= limit:
            return self._fail(units[-1].uid, R.OUT_OF_RANGE, f"end {cursor:#x} >= {limit:#x}")
        out_tb = {lu.uid: lu.new_tb_offset for lu in links}
        # -- variants ------------------------------------------------------------
        per_c: Counter = Counter()
        vlinks: list[VariantLink] = []
        if len(plan.variants) > plan.max_variants:
            return self._fail(plan.variants[0].src_uid, R2.CONTEXT_BUDGET,
                              f"{len(plan.variants)} variants > {plan.max_variants}")
        ordered_specs = plan.variants
        if plan.order_private_by_predecessor:
            ordered_specs = sorted(ordered_specs, key=lambda vs: (out_tb[vs.pred_uid], vs.slot_n, vs.variant_id()))
        for i, vs in enumerate(ordered_specs):
            per_c[vs.src_uid] += 1
            if per_c[vs.src_uid] > plan.max_variants_per_c:
                return self._fail(vs.src_uid, R2.CONTEXT_BUDGET,
                                  f"more than {plan.max_variants_per_c} variants of one C")
            c = eff[vs.src_uid]
            if not c.reloc.relocatable or c.meta_status != "ok":
                return self._fail(vs.src_uid, R2.UNIT_NOT_RELOCATABLE, "variant source")
            vtb = round_up(cursor, TB_ALIGN)
            try:
                vl = relocate_unit(c, vtb)
                body = bytearray(vl.body)
                code = apply_pure_rewrite(bytes(body[:c.tc_size]), vs.patches,
                                          forbid=[(o, o + w) for o, w in vl.patched] +
                                          [(t.off, t.end) for t in lookup_tails(c)],
                                          uid=vs.src_uid)
            except LinkError as e:
                return self._fail(e.uid or vs.src_uid, e.reason, e.detail)
            body[:c.tc_size] = code
            rec = ScRecord.from_bytes(vl.record)
            g = grown.get(vs.pred_uid)
            xc = 0
            if g is not None:
                for spec, st in g.stubs:
                    if any(si.slot_n == vs.slot_n for si in st.slots):
                        xc = EXIT_RETURN if spec.exit_class == "return" else EXIT_STATIC
                        bp = spec_binding_problem(spec, vs.slot_n, c)
                        if bp is not None:
                            return self._fail(vs.pred_uid, R2.CONTINUATION_KEY_MISMATCH,
                                              f"slot {vs.slot_n} -> variant of {vs.src_uid}: "
                                              + bp)
            vlinks.append(VariantLink(i, vs.variant_id(), vs.src_uid, vs.pred_uid, vs.slot_n, xc,
                                      rec.tb_offset, rec.tc_offset, rec.tc_size, rec.end_offset,
                                      bytes(body), tuple(vs.patches), vl.patched,
                                      tuple(vs.facts)))
            cursor = rec.end_offset
        # -- variable-size firmware regions ------------------------------------
        firmware_links: tuple[FirmwareRegionLink, ...] = ()
        if plan.firmware_regions:
            try:
                firmware_links, cursor = self._link_firmware_regions(cursor, by_uid)
            except LinkError as e:
                return self._fail(e.uid or "", e.reason, e.detail)
            from .formats.sc_v7 import directory_size

            table_offset = round_up(cursor, 8)
            table_size = directory_size(firmware_links)
            cursor = table_offset + table_size
        else:
            table_offset = table_size = 0
        if cursor >= limit:
            if firmware_links:
                pending = [r.region_id for r in firmware_links]
                return self._fail(
                    "", "firmware_region_arena_exhausted",
                    canonical_json({"required_end": cursor, "arena_limit": limit,
                                    "pending_regions": pending}).decode())
            return self._fail(units[-1].uid, R.OUT_OF_RANGE, f"end {cursor:#x} >= {limit:#x}")
        # -- ext table -----------------------------------------------------------
        by_link = {lu.uid: lu for lu in links}
        slots: list[SlotEntry] = []
        slot_index: dict[tuple[str, int], int] = {}
        for u in units:
            g = grown.get(u.uid)
            if g is None or not g.registered:
                continue
            lu = by_link[u.uid]
            for spec, st in g.stubs:
                gb = lu.body[st.off:st.off + st.guard_len]
                for si in st.slots:
                    slot_index[(u.uid, si.slot_n)] = len(slots)
                    slots.append(SlotEntry(lu.new_tb_offset, si.slot_n,
                                           KIND_SPEC if si.kind == "SPEC" else KIND_GENERAL,
                                           EXIT_RETURN if spec.exit_class == "return"
                                           else EXIT_STATIC, si.vaddr, st.off, st.guard_len,
                                           fnv1a64(gb)))
        variants: list[VariantEntry] = []
        lks: list[LinkEntry] = []
        for v in vlinks:
            k = slot_index.get((v.pred_uid, v.slot_n))
            if k is None:
                return self._fail(v.pred_uid, R2.VARIANT_PRED,
                                  f"no registered slot {v.slot_n} for the variant of {v.src_uid}")
            if slots[k].kind != KIND_SPEC:
                return self._fail(v.pred_uid, R2.VARIANT_PRED, f"slot {v.slot_n} is not SPEC")
            if any(lk.slot_index == k for lk in lks):
                return self._fail(v.pred_uid, R2.VARIANT_PRED, "two variants on one slot (V17)")
            variants.append(VariantEntry(out_tb[v.src_uid], v.tb_offset, v.tc_offset, v.tc_size,
                                         v.end_offset, fnv1a64(v.body), v.index,
                                         VARIANT_FACTS if v.facts else 0))
            lks.append(LinkEntry(k, v.index))
        spec_unlinked = [i for i, s in enumerate(slots)
                         if s.kind == KIND_SPEC and not any(lk.slot_index == i for lk in lks)]
        if spec_unlinked:
            return self._fail(units[0].uid, R2.LINK_MISSING, f"SPEC slots {spec_unlinked}")
        if any(si.kind == "SPEC" for g in grown.values() if not g.registered
               for _, st in g.stubs for si in st.slots):
            return self._fail(next(iter(grown)), R2.SPEC_UNREGISTERED)
        if not max_ext_entries(len(variants), len(slots), len(lks)):
            return self._fail(units[0].uid, R2.EXT_TOO_LARGE)
        v6 = bool(slots or variants or plan.force_v6)
        ext = None
        if v6:
            ext = ExtTable(tuple(variants), tuple(slots), tuple(lks))
            ext = replace(ext, artifact_id8=_artifact_id8(ext, links, vlinks))
        # All newly published scopt artifacts use the canonical top-level v7.
        # ``ext`` and ``firmware_regions`` select its mutually exclusive payload.
        res = LinkResultV2(True, h, tuple(links), version=7, ext=ext,
                           variants=tuple(vlinks), grown=grown,
                           rewritten={k: tuple(v) for k, v in plan.rewrites.items()},
                           effective=eff, firmware_regions=firmware_links,
                           firmware_table_offset=table_offset,
                           firmware_table_size=table_size)
        if self.derive_captures:
            jr = self.join_result if self.join_result is not None else capture_join(self.snap)
            self.join_result = jr
            for uid in plan.rewrites:
                unit = by_uid[uid]
                if unit.splice_applied:
                    binding = jr.slots.get(unit.slot) if jr is not None else None
                    if not (binding and binding.bound and
                            binding.chunk.native_fragments == unit.native_fragments):
                        return self._fail(uid, "native_metadata_missing", "no matching bound splice capture")
            files, names = derive_scm_v2(jr, res, self.snap)
            res.derived_scms = tuple(files)
            res.capture_sources = tuple(names)
        return res


def _artifact_id8(ext: Any, links: Sequence[UnitLink], vlinks: Sequence[VariantLink]) -> bytes:
    """The ext table's 8-byte artifact id: sha256 over the table (id zeroed), every
    record and every variant body. (The ArtifactId proper is the sha256 of the
    MANIFEST, which covers cache.sc and so cannot be embedded in it; the sidecar
    carries this value as ``x_ext_artifact_id8`` for deploy to compare.)"""
    import hashlib

    hh = hashlib.sha256(b"scopt.sc6.artifact_id8/1")
    hh.update(replace(ext, artifact_id8=b"\0" * 8).encode())
    for lu in links:
        hh.update(lu.record)
        hh.update(hashlib.sha256(lu.body).digest())
    for v in vlinks:
        hh.update(hashlib.sha256(v.body).digest())
    d = hh.digest()[:8]
    return d if d != b"\0" * 8 else b"\0" * 7 + b"\1"


# ---------------------------------------------------------------------------
# derived captures v2
# ---------------------------------------------------------------------------

def _scm_sites(u: TBUnit) -> tuple[Any, ...]:
    from .formats.enums import NONE_U32

    return tuple(S.ScmSite(s.field_off, s.insn_off, s.width, int(s.enc), int(s.kind), s.aux,
                           s.value, NONE_U32 if s.insn_idx is None else s.insn_idx,
                           s.final_field) for s in u.sites)


def _grown_chunk(tb: S.TbChunk, g: GrownUnit) -> S.TbChunk:
    """The TB chunk (version 2) of grown unit ``g`` at its pre-move offsets."""
    from .formats import layouts as L
    from .formats.enums import NONE_U32

    u = g.unit
    old_marks = list(tb.opmarks)
    marks = []
    for m in u.opmarks:
        hid = NONE_U32 if m.helper_id is None else m.helper_id
        raw = next((x for x in old_marks if (x.host_off, x.kind, x.insn_idx, x.helper_id) ==
                    (m.host_off, int(m.kind), m.insn_idx, hid)), None)
        marks.append(raw if raw is not None else S.ScmOpMark(m.host_off, int(m.kind),
                                                             m.insn_idx, hid))
    pool = []
    old_pool = list(tb.pool)
    for p in u.pool:
        raw = next((x for x in old_pool if (x.slot_off, x.data0) == (p.slot_off, p.data0)
                    and x.user_field_off in (p.user_field_off,)), None)
        if raw is None:
            src = next(x for x in old_pool if (x.slot_off, x.data0) == (p.slot_off, p.data0))
            raw = replace(src, user_field_off=p.user_field_off)
        pool.append(raw)
    return replace(tb, record=bytes(u.record), code_size=u.tc_size,
                   code_fnv64=fnv1a64(u.code + u.search), sites=_scm_sites(u),
                   pool=tuple(pool), opmarks=tuple(marks),
                   tb_chunk_version=(L.SCM_TB_CHUNK_VERSION_5 if u.splice_applied
                                     else L.SCM_TB_CHUNK_VERSION_2),
                   reserved=u.stub_start & 0xFFFFFFFF, source=None)


def derive_scm_v2(join_result: Any, res: LinkResultV2, snapshot: Any
                  ) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Derived captures of a v2 link: as :func:`derive_scm`, with grown units as
    TB chunk v2 (``stub_start``, new sites/pool/OpMarks), rewritten units with
    their new ``code_fnv64``/``final_field``, and one VARIANT chunk per variant
    in the first derived file (before its END chunk)."""
    if join_result is None:
        return [], []
    links = res.by_uid()
    by_slot = {u.slot: links[u.uid] for u in snapshot.units() if u.uid in links}
    uid_of_slot = {u.slot: u.uid for u in snapshot.units()}
    out_slot = {lu.uid: lu.slot for lu in res.units}
    code_address = snapshot.header.code_address
    files: list[tuple[str, bytes]] = []
    names: list[str] = []
    for f in join_result.captures:
        bodies: list[Any] = []
        n_tb = 0
        for ch in f.chunks:
            b = ch.body
            if isinstance(b, S.TbChunk):
                sb = join_result.slots.get(b.slot)
                if sb is None or not sb.bound or sb.source is not f or b.slot not in by_slot:
                    continue
                uid = uid_of_slot[b.slot]
                lk = by_slot[b.slot]
                if uid in res.grown:
                    b = _grown_chunk(b, res.grown[uid])
                    nb = _rewrite_tb(b, lk, code_address)
                elif uid in res.rewritten:
                    nb = _rewrite_tb(b, lk, code_address)
                    code = lk.body
                    sites = tuple(replace(s, final_field=int.from_bytes(
                        code[s.field_off:s.field_off + min(s.width, 4)], "little"))
                        if s.width > 0 else s for s in nb.sites)
                    nb = replace(nb, code_fnv64=fnv1a64(lk.body), sites=sites, source=None)
                else:
                    nb = _rewrite_tb(b, lk, code_address)
                bodies.append(nb)
                n_tb += 1
            elif isinstance(b, S.EndChunk):
                if not files and res.variants:
                    bodies.extend(_variant_chunk(v, res, out_slot) for v in res.variants)
                bodies.append(b)
            elif isinstance(b, (S.RunInfo, S.HelperInfo, S.MapsChunk, S.RamblocksChunk)):
                bodies.append(b)
        if not n_tb:
            continue
        if not files and res.variants and not any(isinstance(x, S.VariantChunk) for x in bodies):
            bodies.extend(_variant_chunk(v, res, out_slot) for v in res.variants)
        scm = S.make_scm(bodies, f.header)
        name = "cache.scm" if not files else f"cache.{len(files)}.scm"
        files.append((name, S.encode_scm(scm)))
        names.append(os.path.basename(f.path) if f.path else "<memory>")
    return files, names


def _variant_chunk(v: VariantLink, res: LinkResultV2, out_slot: Mapping[str, int]) -> Any:
    key = bytes.fromhex(v.key.split(":", 1)[1])
    return S.VariantChunk(v.index, out_slot[v.src_uid], v.tb_offset, v.tc_offset, v.tc_size,
                          v.end_offset, fnv1a64(v.body), out_slot[v.pred_uid], v.slot_n,
                          v.exit_class, key,
                          tuple(S.VariantPatch(p.host_off, bytes(p.old_bytes),
                                               bytes(p.new_bytes))
                                for p in sorted(v.patches, key=lambda p: p.host_off)),
                          1 if v.facts else 0)

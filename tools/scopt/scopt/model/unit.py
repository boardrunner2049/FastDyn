"""Core unit types (design §5.2). Interfaces only — frozen by WP0.

All dataclasses are frozen with slots. Byte buffers are ``bytes``; mappings are
``MappingProxyType``. Offsets named ``*_off`` / ``host_*`` are relative to the
unit's ``tc_offset`` (i.e. to the first code byte) unless the name says
``tb_offset`` / ``tc_offset`` / ``end_offset`` (arena offsets).

The importer (WP7) builds these from ``.sc`` + ``.scm``; passes see them through
``UnitView`` / ``SnapshotView``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from ..formats.enums import Encoding, ExitKind, OpMarkKind, SiteKind
from .native import InlineCall
from ..ids import TBKey

if TYPE_CHECKING:  # pragma: no cover
    from ..formats.sc_records import ScHeader

__all__ = [
    "TBKey", "Insn", "Site", "PoolEntry", "Ldst", "OpMark", "StaticExit", "Fixup",
    "RelocInfo", "RecoveryPoint", "HelperBinding", "AnchoredPointer", "Bindings",
    "Entry", "Exit", "GuestOrigin", "TBUnit", "UnitView", "SnapshotView",
    "FixupRuleName", "OriginName", "MetaStatusName",
]

FixupRuleName = Literal["rel_minus_delta", "abs_plus_delta"]
OriginName = Literal["elf_verified", "ram_resident", "unknown"]
MetaStatusName = Literal["ok", "no_meta", "incomplete"]


def _empty_map() -> Mapping:
    return MappingProxyType({})


# ---------------------------------------------------------------------------
# Capture-derived records (one per .scm TB array element, §2.5.5)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Insn:
    """Guest insn ``index``: insn_start words and host code range ``[host_start, host_end)``."""

    index: int
    words: tuple[int, int, int]
    host_start: int
    host_end: int


@dataclass(frozen=True, slots=True)
class Site:
    """A captured native reference site (``.scm`` Site)."""

    field_off: int
    insn_off: int
    width: int
    enc: Encoding
    kind: SiteKind
    aux: int
    value: int
    insn_idx: int | None  # None = TB-start / none (0xFFFFFFFF)
    final_field: int


@dataclass(frozen=True, slots=True)
class PoolEntry:
    """One constant-pool user (``.scm`` Pool). ``slot_off`` is the deduplicated slot."""

    user_field_off: int
    slot_off: int
    rtype: int
    nlong: int
    addend: int
    data0: int


@dataclass(frozen=True, slots=True)
class Ldst:
    """A qemu_ld/st slow path (``.scm`` Ldst)."""

    jcc_fields: tuple[int | None, int | None]
    slow_start: int
    slow_end: int
    raddr_off: int
    oi: int
    op_ordinal: int
    is_ld: bool


@dataclass(frozen=True, slots=True)
class OpMark:
    host_off: int
    kind: OpMarkKind
    insn_idx: int
    helper_id: int | None


@dataclass(frozen=True, slots=True)
class StaticExit:
    slot: int  # 0, 1, or 0xFF (off-page goto_ptr fallback)
    direct: bool
    insn_idx: int
    dest_vaddr: int


# ---------------------------------------------------------------------------
# Relocation / recovery / bindings (importer output)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Fixup:
    field_off: int
    width: int
    rule: FixupRuleName
    site_kind: int
    target_class: str
    target_abs: int | None


@dataclass(frozen=True, slots=True)
class RelocInfo:
    relocatable: bool
    reasons: tuple[str, ...]
    fixups: tuple[Fixup, ...]


@dataclass(frozen=True, slots=True)
class RecoveryPoint:
    """One faultable host return offset (each raddr, each helper-call return)."""

    id: str
    host_return_offset: int
    insn: int
    words: tuple[int, ...]
    source: Literal["raddr", "helper_call"]


@dataclass(frozen=True, slots=True)
class HelperBinding:
    id: int
    name: str | None
    module: str | None
    symbol: str | None
    flags: int
    func: int | None = None  # observed absolute address; never an identity


@dataclass(frozen=True, slots=True)
class AnchoredPointer:
    """A process pointer described by anchor (module+offset, heap, ramblock:<id>+off)."""

    cls: str
    anchor: str
    site: int


@dataclass(frozen=True, slots=True)
class Bindings:
    helpers: tuple[HelperBinding, ...] = ()
    unchecked_host_pointers: tuple[AnchoredPointer, ...] = ()


# ---------------------------------------------------------------------------
# Region-level views
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Entry:
    id: str
    kind: Literal["general", "specialized"]
    member: int
    host_offset: int
    full_key: bool = True
    extra_guards: tuple = ()


@dataclass(frozen=True, slots=True)
class Exit:
    id: str
    member: int
    kind: ExitKind
    slot: int | None
    insn: int | None
    static_dest_vaddr: int | None
    insn_offset: int | None = None  # patchable goto_tb jmp field
    reset_offset: int | None = None


@dataclass(frozen=True, slots=True)
class GuestOrigin:
    member: int
    page_addr0: int
    size: int
    vaddrs: tuple[int, ...]
    origin: OriginName


@dataclass(frozen=True, slots=True)
class TBUnit:
    uid: str  # UnitId "tb:<hex>#k"
    key: TBKey
    slot: int
    guest_size: int
    icount: int
    guest_hash: int
    vaddrs: tuple[int, ...]
    origin: OriginName
    tb_offset: int
    tc_offset: int
    tc_size: int
    end_offset: int
    code: bytes  # [tc_offset, tc_offset + tc_size)
    search: bytes  # [tc_offset + tc_size, end_offset)
    record: bytes  # 104 raw record bytes
    insn0_start: int
    body_end: int
    ldst_end: int
    pool_start: int | None
    insns: tuple[Insn, ...]
    sites: tuple[Site, ...]
    pool: tuple[PoolEntry, ...]
    ldst: tuple[Ldst, ...]
    opmarks: tuple[OpMark, ...]
    static_exits: tuple[StaticExit, ...]
    reloc: RelocInfo
    recovery: tuple[RecoveryPoint, ...]
    bindings: Bindings
    meta_status: MetaStatusName
    content_hash: str  # sha256(record ‖ code ‖ search ‖ canonical meta)
    annotations: Mapping[str, object] = field(default_factory=_empty_map)
    # B3 (WP18): code offset of the guard stubs appended after the pool of a
    # grown unit (None: not grown). ``[pool_start, stub_start)`` is the pool.
    stub_start: int | None = None
    # Logical islands are published at the pass boundary. Their continuation
    # fields remain symbolic until the linker chooses an arena placement.
    native_fragments: tuple[InlineCall, ...] = ()
    inline_start: int | None = None
    splice_applied: bool = False
    # Virtual edits honored only during same-placement linking.
    retired_exit_slots: tuple[int, ...] = ()

    @property
    def pool_end(self) -> int:
        """End of the constant pool: ``stub_start`` for a grown unit, else ``tc_size``."""
        if self.inline_start is not None:
            return self.inline_start
        return self.tc_size if self.stub_start is None else self.stub_start


# ---------------------------------------------------------------------------
# What passes see
# ---------------------------------------------------------------------------

@runtime_checkable
class UnitView(Protocol):
    unit_id: str
    snapshot_id: str
    members: tuple[TBUnit, ...]

    def entries(self) -> tuple[Entry, ...]: ...
    def exits(self) -> tuple[Exit, ...]: ...
    def origins(self) -> tuple[GuestOrigin, ...]: ...
    def effects(self) -> object | None: ...  # EffectMap; None until Tier 1


@runtime_checkable
class SnapshotView(Protocol):
    snapshot_id: str
    header: "ScHeader"
    runtime: object  # RuntimeIdentity (scopt.formats.bundle_v1, WP8)
    bundle: object  # Bundle (scopt.formats.bundle_v1, WP8)

    def units(self) -> tuple[TBUnit, ...]: ...  # slot order
    def unit(self, uid: str) -> TBUnit: ...
    def by_key(self, key: TBKey) -> tuple[TBUnit, ...]: ...
    def layout(self) -> Mapping[str, int]: ...  # uid -> current tb_offset (WorkingSet-aware)

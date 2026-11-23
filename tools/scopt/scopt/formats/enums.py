"""Stable enumerations shared by the QEMU C side and Python (design §2.4, §3.5).

Frozen by WP0. The numeric values are part of the ``.scm`` v1 and ``.qprof`` v1
binary formats; never renumber. New members need a design-doc edit.
"""

from __future__ import annotations

from enum import IntEnum, IntFlag, StrEnum

# Sentinels used inside binary arrays.
NONE_U32 = 0xFFFF_FFFF  # Site.insn_idx (TB-start/none), Ldst.jcc_field unused, OpMark.helper_id, pool_start
NONE_U64 = 0xFFFF_FFFF_FFFF_FFFF  # .qprof edge src_tb_offset NONE
STATIC_EXIT_OFFPAGE_SLOT = 0xFF  # StaticExit.slot for the off-page gen_goto_ptr fallback


class SiteKind(IntEnum):
    """``.scm`` Site.kind (u16)."""

    LABEL_JMP = 0x01
    LABEL_JCC = 0x02
    LDST_FAST_JCC = 0x03
    LDST_RET_JMP = 0x04
    LDST_HELPER_CALL = 0x05
    HELPER_CALL = 0x06
    EXIT_TB_RET_JMP = 0x07
    EXIT_TB_EPI_JMP = 0x08
    MOVI_EXIT_TB_PTR = 0x09
    MOVI_LDST_RA = 0x0A
    MOVI_CONST = 0x0B
    POOL_REF = 0x0C
    GOTO_TB = 0x0D
    GOTO_PTR = 0x0E
    OTHER_BRANCH = 0x0F  # never expected -> pin


class Encoding(IntEnum):
    """``.scm`` Site.enc (u8)."""

    NONE = 0
    REL8 = 1
    REL32 = 2
    RIPREL32 = 3
    IMM32_ZX = 4
    IMM32_SX = 5
    IMM64 = 6
    XOR0 = 7
    POOL_INDIRECT = 8
    GOTO_TB_REL32 = 9


class OpMarkKind(IntEnum):
    """``.scm`` OpMark.kind (u16)."""

    CALL = 1
    QEMU_LD = 2
    QEMU_ST = 3
    EXIT_TB = 4
    GOTO_TB = 5
    GOTO_PTR = 6


class TbFlag(IntFlag):
    """``.scm`` TB chunk ``flags`` (u32)."""

    IRQ_CHECK = 1 << 0  # insn0_start > 0 and !CF_NOIRQ
    CAPTURE_OVERFLOW = 1 << 1
    CAPTURE_ERROR = 1 << 2
    OPS_PRESENT = 1 << 3  # Tier 1
    PLUGIN_CBS_PRESENT = 1 << 4  # Tier 1
    GUEST_BYTES_TRUNCATED = 1 << 5


class ChunkType(IntEnum):
    """``.scm`` chunk ``type`` (u32). 2 is reserved."""

    RUN_INFO = 1
    HELPER = 3
    TB = 4
    OPS = 5  # Tier 1
    PLUGIN_CBS = 6  # Tier 1
    MAPS = 7
    RAMBLOCKS = 8
    VARIANT = 9  # Python-only (derived captures of B3 artifacts, WP18); QEMU never writes it
    END = 10


CHUNK_TYPE_RESERVED = frozenset({2})


class CaptureLevel(IntEnum):
    """FileHeader / RUN_INFO ``capture_level``; ``QEMU_SC_CAPTURE_LEVEL``."""

    RELOC = 1  # Tier 0, default
    OPS = 2  # Tier 1, reserved


class RunInfoFlag(IntFlag):
    ICOUNT_ENABLED = 1 << 0
    FLAT = 1 << 1  # always 0


class MapsTag(IntEnum):
    FIRST_COMMIT = 1
    EXIT = 2


class QprofEdgeKind(IntEnum):
    """``.qprof`` edge row ``kind`` (u16)."""

    CHAIN0 = 1
    CHAIN1 = 2
    LOOKUP_HIT = 3
    LOOKUP_MISS = 4
    EXIT_NULL = 5
    EXIT_REQUESTED = 6
    FIRST = 7


class QprofEdgeFlag(IntFlag):
    """``.qprof`` edge row ``flags`` (u16)."""

    IRQ = 1 << 0
    EXCEPTION = 1 << 1
    LONGJMP = 1 << 2


class QprofHeaderFlag(IntFlag):
    """``.qprof`` header ``flags`` (u32)."""

    CLEAN_EXIT = 1 << 0
    TB_OVERFLOW = 1 << 1
    EDGE_OVERFLOW = 1 << 2


# Profile v1 JSON spellings of the edge kinds and flags (design §3.8 example uses
# lowercase, e.g. "chain1").
EDGE_KIND_NAMES: dict[QprofEdgeKind, str] = {k: k.name.lower() for k in QprofEdgeKind}
EDGE_FLAG_NAMES: dict[QprofEdgeFlag, str] = {f: f.name.lower() for f in QprofEdgeFlag}


class CountStatus(StrEnum):
    """Profile count status (§3.7). ``sampled`` is reserved; no v1 producer."""

    EXACT = "exact"
    LOWER_BOUND = "lower_bound"
    UNKNOWN = "unknown"
    SAMPLED = "sampled"


class UnknownReason(StrEnum):
    NOT_IN_DOMAIN = "not_in_domain"
    PHASE_NOT_REACHED = "phase_not_reached"
    NOT_COLLECTED = "not_collected"


class ProfileRowOrigin(StrEnum):
    """Join result of a ``.qprof`` TB row (§3.6)."""

    SNAPSHOT = "SNAPSHOT"
    PROFILE_ONLY = "PROFILE_ONLY"
    TRANSIENT = "TRANSIENT"


class PhaseMatch(StrEnum):
    EXACT_START = "exact_start"
    CONTAINED = "contained"


class RegionStatus(StrEnum):
    """CacheRegion v1 ``status``."""

    OK_RELOCATABLE = "ok_relocatable"
    PINNED = "pinned"


class MetaStatus(StrEnum):
    OK = "ok"
    NO_META = "no_meta"
    INCOMPLETE = "incomplete"


class GuestOriginKind(StrEnum):
    ELF_VERIFIED = "elf_verified"
    RAM_RESIDENT = "ram_resident"
    UNKNOWN = "unknown"


class FixupRule(StrEnum):
    REL_MINUS_DELTA = "rel_minus_delta"
    ABS_PLUS_DELTA = "abs_plus_delta"


class ExitKind(StrEnum):
    """CacheRegion v1 exit kinds."""

    GOTO_TB = "goto_tb"
    GOTO_PTR = "goto_ptr"
    EXIT_NULL = "exit_null"
    EXIT_REQUESTED = "exit_requested"
    GUARDED_SLOT = "guarded_slot"  # B3 (design §3.9): a slot of a grown unit's guard stub


class LoaderRequirement(StrEnum):
    SC_V5_UNMODIFIED = "qemu-sc-v5-unmodified"
    SC_V6 = "qemu-sc-v6"
    SC_V7_FIRMWARE_REGIONS = "qemu-sc-v7-firmware-regions"
    SC_V7_COMPOSITE_FIRMWARE_REGIONS = "qemu-sc-v7-composite-firmware-regions"


__all__ = [
    "NONE_U32", "NONE_U64", "STATIC_EXIT_OFFPAGE_SLOT", "SiteKind", "Encoding",
    "OpMarkKind", "TbFlag", "ChunkType", "CHUNK_TYPE_RESERVED", "CaptureLevel",
    "RunInfoFlag", "MapsTag", "QprofEdgeKind", "QprofEdgeFlag", "QprofHeaderFlag",
    "EDGE_KIND_NAMES", "EDGE_FLAG_NAMES", "CountStatus", "UnknownReason",
    "ProfileRowOrigin", "PhaseMatch", "RegionStatus", "MetaStatus", "GuestOriginKind",
    "FixupRule", "ExitKind", "LoaderRequirement",
]

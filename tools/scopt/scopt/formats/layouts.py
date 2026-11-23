"""Frozen binary layouts: ``.sc`` v5, ``.scm`` v1 (§2.5) and ``.qprof`` v1 (§3.5).

Frozen by WP0. These ``struct.Struct`` objects are the single Python statement
of the byte layouts. Readers and writers (WP1, WP4, WP6) and the QEMU smoke
scripts must use them, and the C structs in the capture/profile patches must
have the same sizes (``QEMU_BUILD_BUG_ON(sizeof(...) != N)``).

All little-endian, no implicit padding (``"<"``).

Erratum vs. design §3.5: the ``.qprof`` TB row fixed part is documented as 72
bytes, but the listed fields add up to 64. The frozen layout keeps the stated
size (72) and widens the trailing ``u64 reserved`` to ``u64 reserved[2]``; see
docs/contracts.md.
"""

from __future__ import annotations

import struct

# --------------------------------------------------------------------------
# Canonical .sc v7 public-record layout (qemu tcg/supercache.c)
# --------------------------------------------------------------------------
SC_MAGIC = b"QSCACHE1"
SC_VERSION = 7
SC_CODE_OFFSET = 0x10000  # SUPERCACHE_CODE_OFFSET (64 KiB)
SC_RECORD_COMMIT = 0x53435231  # "1RCS"

# magic, version, header_size, code_size, code_address, prologue_end,
# record_count, runtime_signature, host_page_size, target_page_bits, tb_size,
# reserved, checksum
SC_HEADER = struct.Struct("<8sIIQQQQQIIIIQ")
# tb_offset, end_offset, tc_offset, tc_size, pc, cs_base, page_addr0,
# page_addr1, guest_hash, flags, cflags, guest_size, icount,
# jmp_reset_offset[2], jmp_insn_offset[2], checksum, committed, pad[4]
SC_RECORD = struct.Struct("<9QII2H2H2HII4x")

# --------------------------------------------------------------------------
# .scm v1
# --------------------------------------------------------------------------
SCM_MAGIC = b"QSCMETA1"
SCM_VERSION = 1
SCM_ALIGN = 8

# magic, version, header_size, capture_level, pid, created_realtime_ns,
# reserved[3], checksum (FNV-1a-64 over [0, 56))
SCM_FILE_HEADER = struct.Struct("<8sIIIIQ3QQ")
SCM_FILE_HEADER_CHECKSUM_LEN = 56

# type, payload_len, seq ; then payload, zero pad to 8, u64 trailer
SCM_CHUNK_HEADER = struct.Struct("<IIQ")
SCM_CHUNK_TRAILER = struct.Struct("<Q")

# RUN_INFO fixed part, then u8 prologue[prologue_len]
SCM_RUN_INFO = struct.Struct(
    "<II"      # info_version, capture_level
    "Q"        # code_address
    "Q"        # code_size
    "Q"        # prologue_end
    "Q"        # tb_ret_off
    "Q"        # epilogue_off
    "Q"        # runtime_signature
    "Q"        # header_record_count_at_load
    "Q"        # loaded_max_end
    "Q"        # highwater_off
    "I"        # icache_linesize
    "I"        # tb_struct_size
    "I"        # host_page_size
    "I"        # target_page_bits
    "I"        # insn_start_words (= 3)
    "I"        # host cpuinfo bits
    "Q"        # exe st_dev
    "Q"        # exe st_ino
    "Q"        # exe st_size
    "Q"        # exe st_mtim (ns)
    "I"        # flags (RunInfoFlag)
    "I"        # prologue_len
)
SCM_RUN_INFO_VERSION = 1

# helper_id, tcg_flags, typemask, name_len, func ; then name bytes
SCM_HELPER = struct.Struct("<IIIIQ")

# TB chunk fixed header (tb_chunk_version 1)
SCM_TB_HEADER = struct.Struct(
    "<I"       # tb_chunk_version
    "I"        # slot
    "Q"        # commit_seq
    "104s"     # record (byte-exact)
    "Q"        # vaddr
    "Q"        # code_fnv64
    "I"        # attempts
    "I"        # flags (TbFlag)
    "I"        # insn0_start
    "I"        # body_end
    "I"        # ldst_end
    "I"        # pool_start (0xFFFFFFFF none)
    "I"        # code_size
    "H"        # icount
    "H"        # insn_start_words
    "I"        # n_sites
    "I"        # n_pool
    "I"        # n_ldst
    "I"        # n_opmarks
    "I"        # n_static_exits
    "I"        # n_guest_bytes
    "Q"        # reserved
)
SCM_TB_CHUNK_VERSION = 1
#: Derived-only TB chunk version (scopt writes it for grown units, B3/B4 design
#: §9.1 V19, WP18): same layout as v1; the low u32 of ``reserved`` is
#: ``stub_start`` (code offset of the guard stubs appended after the pool,
#: 0xFFFFFFFF = none), the high u32 is 0.
SCM_TB_CHUNK_VERSION_2 = 2
# Derived TB v3 (native islands): reserved[31:0] is the original code size;
# reserved[63:32] is the byte length of a canonical JSON trailer after guest_bytes.
SCM_TB_CHUNK_VERSION_3 = 3
# Derived TB v4 (native splice): same trailer framing, with the splice start
# in reserved[31:0] and one inline request describing the replaced span.
SCM_TB_CHUNK_VERSION_4 = 4
# Guarded imported splice: reserved low word is stub_start; splice origin is in the certificate.
SCM_TB_CHUNK_VERSION_5 = 5
SCM_TB_CHUNK_VERSIONS = (SCM_TB_CHUNK_VERSION, SCM_TB_CHUNK_VERSION_2,
                         SCM_TB_CHUNK_VERSION_3, SCM_TB_CHUNK_VERSION_4, SCM_TB_CHUNK_VERSION_5)
SCM_TB_NATIVE_MAX = 1 << 20

# VARIANT chunk (type 9, Python-only, WP18): a variant C' of a general record C.
# version, variant_index, general_slot, flags, tb_offset, tc_offset, tc_size,
# end_offset, code_fnv64, pred_slot, slot_n, exit_class, n_patches, variant_key[32]
SCM_VARIANT_HEADER = struct.Struct("<IIIIQQQQQIBBH32s")
SCM_VARIANT_PATCH = struct.Struct("<IHH")  # host_off, len, reserved; then old[len], new[len]

# Arrays after the TB header, in this order, each 8-aligned.
SCM_INSN = struct.Struct("<3QII")            # word[3], host_start, host_end
SCM_SITE = struct.Struct("<IIBBHIQII")       # field_off, insn_off, width, enc, kind, aux, value, insn_idx, final_field
SCM_POOL = struct.Struct("<IIHHiQ")          # user_field_off, slot_off, rtype, nlong, addend, data0
SCM_LDST = struct.Struct("<2IIIIIIB3x")      # jcc_field[2], slow_start, slow_end, raddr_off, oi, op_ordinal, is_ld, pad[3]
SCM_OPMARK = struct.Struct("<IHHII")         # host_off, kind, insn_idx, helper_id, reserved
SCM_STATIC_EXIT = struct.Struct("<BBHIQ")    # slot, direct, insn_idx, reserved, dest_vaddr
SCM_TB_ARRAYS_ORDER = ("insns", "sites", "pool", "ldst", "opmarks", "static_exits", "guest_bytes")

# MAPS: tag, truncated, len, pad ; then raw /proc/self/maps bytes
SCM_MAPS_HEADER = struct.Struct("<IIII")
# RAMBLOCKS: n, pad ; then rows
SCM_RAMBLOCKS_HEADER = struct.Struct("<II")
SCM_RAMBLOCK_ROW = struct.Struct("<64sQQQQ")  # idstr, ram_offset, used_length, max_length, host
# END: committed, discarded, overflow, capture_error, write_errors, helpers, clean
SCM_END = struct.Struct("<6QI")

# Static staging capacities (§2.5.7); the C side must use the same numbers.
SCM_CAP_SITES = 16384
SCM_CAP_INSNS = 512
SCM_CAP_POOL = 4096
SCM_CAP_LDST = 4096
SCM_CAP_OPMARKS = 8192
SCM_CAP_STATIC_EXITS = 4
SCM_CAP_GUEST_BYTES = 4096
SCM_CAP_CHUNK_BUF = 1 << 20
SCM_CAP_MAPS_BUF = 256 << 10
SCM_CAP_HELPERS = 2048

# --------------------------------------------------------------------------
# .qprof v1
# --------------------------------------------------------------------------
QPROF_MAGIC = b"QSCPROF1"
QPROF_VERSION = 1

QPROF_HEADER = struct.Struct(
    "<8s"      # magic
    "I"        # version
    "I"        # header_size = 128
    "Q"        # code_address
    "Q"        # runtime_signature
    "Q"        # loaded_record_count
    "Q"        # loaded_max_end
    "Q"        # loaded_header_checksum
    "I"        # n_phases
    "I"        # flags (QprofHeaderFlag)
    "Q"        # n_tb_rows
    "Q"        # n_edge_rows
    "Q"        # total_dispatches
    "Q"        # created_realtime_ns
    "3Q"       # reserved
    "Q"        # checksum = FNV-1a-64 over [0, 120)
)
QPROF_HEADER_CHECKSUM_LEN = 120

QPROF_PHASE = struct.Struct("<IIQQ16s")  # id, reached, marker_vaddr, first_dispatch_index, name[16]
QPROF_PHASE_NAME_LEN = 16

# TB row fixed part (72 bytes; see the erratum in the module docstring):
# tb_offset, first_vaddr, page_addr0, page_addr1, cs_base, flags, cflags,
# size, icount, pad, reserved[2]
QPROF_TB_ROW = struct.Struct("<5QIIHHI2Q")
# per phase: entries, aborted, longjmp, lookup_hits
QPROF_TB_PHASE = struct.Struct("<4Q")
# src_tb_offset (UINT64_MAX = NONE), dst_tb_offset, kind, flags, phase, count
QPROF_EDGE = struct.Struct("<QQHHIQ")
QPROF_TRAILER = struct.Struct("<Q")  # FNV-1a-64 over all bytes after the header

QPROF_CAP_TB_ROWS = 65536
QPROF_CAP_EDGE_ROWS = 262144


def qprof_tb_row_size(n_phases: int) -> int:
    return QPROF_TB_ROW.size + n_phases * QPROF_TB_PHASE.size


def align8(n: int) -> int:
    return (n + 7) & ~7


# Sizes stated by the design; checked at import so a typo can never ship.
_EXPECTED = {
    "SC_HEADER": (SC_HEADER, 80),
    "SC_RECORD": (SC_RECORD, 104),
    "SCM_FILE_HEADER": (SCM_FILE_HEADER, 64),
    "SCM_CHUNK_HEADER": (SCM_CHUNK_HEADER, 16),
    "SCM_RUN_INFO": (SCM_RUN_INFO, 144),
    "SCM_HELPER": (SCM_HELPER, 24),
    "SCM_TB_HEADER": (SCM_TB_HEADER, 200),
    "SCM_INSN": (SCM_INSN, 32),
    "SCM_SITE": (SCM_SITE, 32),
    "SCM_POOL": (SCM_POOL, 24),
    "SCM_LDST": (SCM_LDST, 32),
    "SCM_OPMARK": (SCM_OPMARK, 16),
    "SCM_STATIC_EXIT": (SCM_STATIC_EXIT, 16),
    "SCM_MAPS_HEADER": (SCM_MAPS_HEADER, 16),
    "SCM_RAMBLOCKS_HEADER": (SCM_RAMBLOCKS_HEADER, 8),
    "SCM_RAMBLOCK_ROW": (SCM_RAMBLOCK_ROW, 96),
    "SCM_END": (SCM_END, 52),
    "QPROF_HEADER": (QPROF_HEADER, 128),
    "QPROF_PHASE": (QPROF_PHASE, 40),
    "QPROF_TB_ROW": (QPROF_TB_ROW, 72),
    "QPROF_TB_PHASE": (QPROF_TB_PHASE, 32),
    "QPROF_EDGE": (QPROF_EDGE, 32),
}
for _name, (_s, _n) in _EXPECTED.items():
    if _s.size != _n:  # pragma: no cover - import-time contract check
        raise AssertionError(f"{_name}: size {_s.size} != frozen {_n}")
EXPECTED_SIZES = {k: n for k, (_s, n) in _EXPECTED.items()}
del _name, _s, _n

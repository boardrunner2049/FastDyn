"""TB search-data codec and ``unwind`` (design §2.7 step 3 and step 7).

QEMU appends a compressed table to every TB's host code, at
``tc_offset + tc_size`` (``encode_search``, accel/tcg/translate-all.c:101-135).
The logical table has one row per guest instruction: ``INSN_START_WORDS``
(= 3 on Arm) ``uint64_t`` insn_start words, then the host offset (relative to
``tc.ptr``) of the *end* of the host code for that insn. Each row is stored as
sleb128 deltas from the previous row. The seed row is
``{pc if !CF_PCREL else 0, 0, 0, host 0}``.

Arithmetic follows the C exactly: the deltas are ``curr - prev`` computed in
``uint64_t`` and then passed as ``int64_t``, and decoding accumulates into
``uint64_t`` (wrapping mod 2**64). The host column is accumulated the same way.

``unwind`` reimplements ``cpu_unwind_data_from_tb`` (translate-all.c:137-169):
``host_pc -= GETPC_ADJ`` (2), then the first row whose cumulative host end is
``> host_pc`` is the issuing insn.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "INSN_START_WORDS", "GETPC_ADJ", "CF_PCREL", "is_pcrel", "SearchRow", "SearchRows", "SearchDecodeError",
    "encode_sleb128", "decode_sleb128", "decode_search", "encode_search", "unwind",
]

INSN_START_WORDS = 3  # target/arm: TARGET_INSN_START_EXTRA_WORDS 2, + pc
GETPC_ADJ = 2  # accel/tcg/tb-internal.h
CF_PCREL = 0x00020000  # include/exec/translation-block.h

_U64 = (1 << 64) - 1


def is_pcrel(cflags: int) -> bool:
    """True when ``cflags`` has CF_PCREL (the search seed for word 0 is then 0)."""
    return bool(cflags & CF_PCREL)


class SearchDecodeError(ValueError):
    """The search data is truncated or malformed."""


@dataclass(frozen=True, slots=True)
class SearchRow:
    """One decoded row: the insn_start ``words`` and the cumulative ``host_end``.

    ``host_end`` is the offset (from the TB's first code byte) where the host
    code for this guest insn ends, i.e. ``tcg_ctx->gen_insn_end_off[i]``.
    """

    words: tuple[int, ...]
    host_end: int


class SearchRows(tuple):
    """A tuple of :class:`SearchRow` that also records how many bytes it used."""

    nbytes: int

    def __new__(cls, rows: Iterable[SearchRow], nbytes: int) -> "SearchRows":
        self = super().__new__(cls, rows)
        self.nbytes = nbytes
        return self

    def __reduce__(self):  # pragma: no cover - pickling support
        return (SearchRows, (tuple(self), self.nbytes))


# --------------------------------------------------------------------------
# sleb128 (translate-all.c encode_sleb128 / decode_sleb128)
# --------------------------------------------------------------------------

def _as_int64(v: int) -> int:
    v &= _U64
    return v - (1 << 64) if v >> 63 else v


def encode_sleb128(val: int) -> bytes:
    """Encode ``val`` (taken as int64) exactly like QEMU's ``encode_sleb128``."""
    val = _as_int64(val)
    out = bytearray()
    while True:
        byte = val & 0x7F
        val >>= 7  # arithmetic shift, as on int64_t
        more = not ((val == 0 and (byte & 0x40) == 0) or (val == -1 and (byte & 0x40) != 0))
        if more:
            byte |= 0x80
        out.append(byte)
        if not more:
            return bytes(out)


def decode_sleb128(buf: bytes | bytearray | memoryview, pos: int = 0) -> tuple[int, int]:
    """Decode one sleb128 value at ``buf[pos]``; return ``(int64 value, new pos)``.

    Mirrors QEMU's ``decode_sleb128`` including the 64-bit truncation of very
    long encodings. Raises :class:`SearchDecodeError` if ``buf`` ends mid-value.
    """
    val = 0
    shift = 0
    n = len(buf)
    while True:
        if pos >= n:
            raise SearchDecodeError(f"sleb128 runs past the end of the buffer at {pos}")
        byte = buf[pos]
        pos += 1
        if shift < 64:
            val |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    if shift < 64 and (byte & 0x40):
        val |= -(1 << shift)
    return _as_int64(val), pos


# --------------------------------------------------------------------------
# Table codec
# --------------------------------------------------------------------------

def _seed(pcrel: bool, pc: int, nwords: int) -> list[int]:
    seed = [0] * nwords
    if not pcrel:
        seed[0] = pc & _U64
    return seed


def decode_search(buf: bytes | bytearray | memoryview, icount: int, pcrel: bool = True, *,
                  pc: int = 0, words: int = INSN_START_WORDS) -> SearchRows:
    """Decode ``icount`` rows of search data from the start of ``buf``.

    ``buf`` normally starts at ``tc_offset + tc_size`` and may extend past the
    table (alignment padding is ignored). ``pc`` is the seed for word 0 when
    ``pcrel`` is false. The result's ``nbytes`` is the encoded table length.
    """
    if icount < 0:
        raise ValueError("icount must be >= 0")
    data = _seed(pcrel, pc, words)
    host = 0
    pos = 0
    rows = []
    for _ in range(icount):
        for j in range(words):
            d, pos = decode_sleb128(buf, pos)
            data[j] = (data[j] + d) & _U64
        d, pos = decode_sleb128(buf, pos)
        host = (host + d) & _U64
        rows.append(SearchRow(tuple(data), host))
    return SearchRows(rows, pos)


def encode_search(rows: Sequence[SearchRow] | Sequence[tuple], pcrel: bool = True, *,
                  pc: int = 0) -> bytes:
    """Encode rows exactly like ``encode_search``; inverse of :func:`decode_search`.

    ``rows`` may be :class:`SearchRow` objects or ``(words, host_end)`` pairs.
    """
    out = bytearray()
    prev_words: list[int] | None = None
    prev_host = 0
    for i, r in enumerate(rows):
        words, host_end = (r.words, r.host_end) if isinstance(r, SearchRow) else r
        words = [w & _U64 for w in words]
        if prev_words is None:
            prev_words = _seed(pcrel, pc, len(words))
        elif len(words) != len(prev_words):
            raise ValueError(f"row {i}: {len(words)} words, expected {len(prev_words)}")
        for j, w in enumerate(words):
            out += encode_sleb128(w - prev_words[j])
        out += encode_sleb128((host_end & _U64) - prev_host)
        prev_words = words
        prev_host = host_end & _U64
    return bytes(out)


def unwind(rows: Sequence[SearchRow], host_pc_off: int) -> tuple[int, tuple[int, ...]] | None:
    """Return ``(insn index, words)`` for the return address ``host_pc_off``.

    ``host_pc_off`` is relative to the TB's first code byte (``tc.ptr``), as a
    return address pushed by a call is. Like ``cpu_unwind_data_from_tb``, it
    subtracts ``GETPC_ADJ`` and picks the first row whose cumulative host end
    is strictly greater. Returns ``None`` where QEMU returns -1 (the address
    lies before the TB or after the last insn).
    """
    h = host_pc_off - GETPC_ADJ
    if h < 0:
        return None
    for i, r in enumerate(rows):
        if r.host_end > h:
            return i, r.words
    return None

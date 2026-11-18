"""Guest Thumb (Cortex-M) instruction-length check (design §2.7 step 9). WP2.

The guest instruction lengths of a TB are known two ways:

* from the search data: consecutive insn_start ``word0`` deltas (under
  CF_PCREL word0 is ``pc & ~TARGET_PAGE_MASK``, translate.c:7954-7958), with
  the last length ``guest_size`` minus the sum of the others
  (:func:`lengths_from_words`);
* from the guest bytes: a capstone decode with ``CS_ARCH_ARM``,
  ``CS_MODE_THUMB | CS_MODE_MCLASS`` (:func:`thumb_lengths`).

:func:`check_lengths` compares them. :func:`thumb2_halfword_len` is the
architectural rule (a first halfword whose top five bits are 0b11101,
0b11110 or 0b11111 starts a 32-bit encoding) and is used when capstone
cannot decode an instruction (e.g. an UNDEFINED encoding the guest would trap
on): the length is still well defined.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache

import capstone

__all__ = ["thumb_decoder", "thumb2_halfword_len", "thumb_lengths", "lengths_from_words",
           "check_lengths"]


@lru_cache(maxsize=1)
def thumb_decoder() -> capstone.Cs:
    return capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)


def thumb2_halfword_len(hw: int) -> int:
    """Length in bytes (2 or 4) of the Thumb instruction whose first halfword is ``hw``."""
    return 4 if (hw >> 11) in (0b11101, 0b11110, 0b11111) else 2


def thumb_lengths(code: bytes, addr: int = 0, count: int | None = None) -> list[int]:
    """Lengths of consecutive Thumb instructions in ``code`` (decoded at ``addr``).

    Decodes ``count`` instructions (default: until ``code`` is consumed).
    Where capstone fails, the architectural halfword rule gives the length.
    Stops early if fewer than 2 bytes remain or an instruction would run past
    the end of ``code``.
    """
    md = thumb_decoder()
    out: list[int] = []
    off = 0
    while (count is None or len(out) < count) and off + 2 <= len(code):
        n = None
        for _a, size, _m, _o in md.disasm_lite(code[off:off + 4], addr + off, count=1):
            n = size
        if n is None:
            n = thumb2_halfword_len(int.from_bytes(code[off:off + 2], "little"))
        if off + n > len(code):
            break
        out.append(n)
        off += n
    return out


def lengths_from_words(word0s: Sequence[int], guest_size: int, page_bits: int = 10) -> list[int]:
    """Guest insn lengths from consecutive insn_start word0 values.

    Deltas are taken modulo the target page size, so a PCREL TB that crosses
    into its second page (word0 wraps to 0) is handled. The last length is
    ``guest_size`` minus the sum of the earlier ones.
    """
    if not word0s:
        return []
    mask = (1 << page_bits) - 1
    out = [((b - a) & mask) for a, b in zip(word0s, word0s[1:])]
    out.append(guest_size - sum(out))
    return out


def check_lengths(code: bytes, expected: Sequence[int], addr: int = 0) -> list[str]:
    """Problems (empty if none) comparing a Thumb decode of ``code`` with ``expected``.

    ``code`` must be exactly the TB's guest bytes (``guest_size`` long).
    """
    got = thumb_lengths(code, addr, count=len(expected))
    probs: list[str] = []
    if sum(expected) != len(code):
        probs.append(f"guest_size {len(code)} != sum of expected lengths {sum(expected)}")
    for i, (g, e) in enumerate(zip(got, expected)):
        if g != e:
            probs.append(f"insn {i}: decoded length {g} != expected {e}")
            break
    if len(got) < len(expected) and not probs:
        probs.append(f"decoded {len(got)} insns, expected {len(expected)}")
    return probs

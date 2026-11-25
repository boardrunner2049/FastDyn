from __future__ import annotations

import pytest

from scopt.splice_layout import NO_JUMP_SLOT, remap_jump_offsets
from scopt.validate_sym.native_inline import NativeInlineError


def test_remap_keeps_empty_slots_and_moves_real_ones():
    assert remap_jump_offsets((0x40, NO_JUMP_SLOT), lambda x: x + 0x100) == (0x140, NO_JUMP_SLOT)


@pytest.mark.parametrize("grow", [0xFFFF - 0x40, 0x20000])
def test_remap_refuses_offsets_beyond_the_record_field(grow):
    with pytest.raises(NativeInlineError) as info:
        remap_jump_offsets((0x40, NO_JUMP_SLOT), lambda x: x + grow)
    assert info.value.reason == "splice_jump_offset_range"

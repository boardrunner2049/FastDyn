import pytest

from utils.coverage_helpers import (coverage_growth, disassembly_pcs,
                                    executed_blocks, expand_blocks)


def test_coverage_parsers_handle_malformed_duplicates_and_missing_pcs():
    blocks = executed_blocks(["100\t2\t0\n", "bad\n", "100\t3\t1\n"])
    assert blocks == {0x100: 3}
    missing = []
    assert expand_blocks({0x100: 2, 0x999: 1}, [0x100, 0x102],
                         missing=missing.append) == {0x100, 0x102}
    assert missing == [0x999]
    with pytest.raises(ValueError, match="beyond"):
        expand_blocks({0x100: 3}, [0x100, 0x102], strict_bounds=True)


def test_disassembly_and_growth_normalization():
    assert disassembly_pcs([" 101: nop\n", " 100: bx lr\n", "junk\n"]) == [0x100]
    growth = coverage_growth(["[TB] Unique blocks = 2 at time 50\n",
                              "[TB] Unique blocks = 3 at time 75\n",
                              "[TB] Unique blocks = 2 at time 99\n"])
    assert growth == {2: 0, 3: 25}

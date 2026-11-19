import pytest

from scopt.analysis.pc_update import fold_bytes
from scopt.decode.x86 import decoder
from scopt.validate_sym.paths import _Mem


@pytest.mark.parametrize("immediate", ["83c39c", "81c32c010000"])
def test_same_size_pc_update(immediate):
    old = bytes.fromhex("8b5d3c" + immediate + "895d3c")
    new = fold_bytes(old)
    (update,) = decoder().disasm(new, 0)
    assert len(old) == update.size == len(new)
    assert update.mnemonic == "add" and update.operands[0].size == 4
    assert update.reg_name(update.operands[0].mem.base) == "rbp"
    assert not _Mem._index(update, update.operands[0])
    assert update.operands[0].mem.disp == 60
    assert fold_bytes(old.replace(bytes.fromhex("895d3c"), bytes.fromhex("895d38"))) is None
    # Setting REX.X selects R12 as the SIB index. Treating that as no index
    # could allow a guest-dependent address to alias another environment field.
    (indexed,) = decoder().disasm(bytes([new[0] | 2]) + new[1:], 0)
    assert _Mem._index(indexed, indexed.operands[0]) == "r12"

"""Regression tests for the closed, register-carrying chain window."""
from pathlib import Path
import platform
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
pytest.importorskip("scopt")
from continuity_witness import check_execution, prove_window


SUFFIX = bytes.fromhex("44896d14 c645fc01 8b5d3c 83c3a4 895d3c 666690")
PREFIX = bytes.fromhex("8b5df8 83c3fe 85db 0f8c86000000 66895df8 c645fc00")
LOAD = bytes.fromhex("8b5d14")


@pytest.mark.skipif(platform.machine() != "x86_64", reason="executes System V AMD64 instructions")
def test_native_chain_and_independent_entry():
    assert prove_window(SUFFIX, PREFIX, LOAD) == bytes.fromhex("4489eb")
    assert check_execution(SUFFIX, PREFIX, LOAD)[0] == 42


@pytest.mark.parametrize(("suffix", "prefix", "load"), [
    (SUFFIX.replace(b"\x6d\x14", b"\x6d\x18"), PREFIX, LOAD),  # wrong reaching field
    (SUFFIX.replace(b"\x44\x89", b"\x66\x89"), PREFIX, LOAD),  # partial-width store
    (SUFFIX[:-3] + bytes.fromhex("4531ed"), PREFIX, LOAD),  # destroys carrier
    (SUFFIX[:-3] + bytes.fromhex("ff5500"), PREFIX, LOAD),  # indirect call
    (SUFFIX, PREFIX[:-4] + bytes.fromhex("c6451400"), LOAD),  # overlap before reload
    (SUFFIX, PREFIX.replace(b"\x0f\x8c", b"\x0f\x8d"), LOAD),  # changed IRQ bailout
    (SUFFIX, PREFIX, bytes.fromhex("8b5d3c")),  # mutable guest PC
])
def test_refuse_unproved_window(suffix, prefix, load):
    with pytest.raises(ValueError):
        prove_window(suffix, prefix, load)

from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopt.analysis.irq_entry import OLD, NEW, can_fuse_irq_entry, matches_irq_patch
from scopt.formats.enums import SiteKind
from scopt.manager_b3 import _compose
from scopt.passes.base import Patch


BASE, TC, RET, SLOW = 0x200000, 0x1000, 0x202000, 32


def unit():
    start = OLD + b"\x0f\x8c" + (SLOW-11).to_bytes(4, "little")
    fast = bytes.fromhex("c6 45 fc 00 8b 5d 04 31 db")
    body = bytearray((start + fast).ljust(SLOW, b"\x90"))
    body += bytes.fromhex("48 8d 05 00 00 00 00 e9") + (RET-BASE-TC-SLOW-12).to_bytes(4,"little")
    return SimpleNamespace(
        code=bytes(body), meta_status="ok", reloc=SimpleNamespace(relocatable=True, fixups=[]),
        native_fragments=[], body_end=len(body), tc_size=len(body), tc_offset=TC, pool_start=None,
        opmarks=[], recovery=[], insns=[], sites=[
            SimpleNamespace(insn_off=SLOW, kind=SiteKind.MOVI_EXIT_TB_PTR, value=BASE+TC),
            SimpleNamespace(insn_off=SLOW+7, kind=SiteKind.EXIT_TB_RET_JMP, value=RET)])


def test_fusion_and_materialization_preserve_one_certificate():
    u = unit()
    assert can_fuse_irq_entry(u, BASE, RET)
    p = Patch("unit", 0, OLD, NEW, "irq_entry_mem_compare", "checked")
    assert matches_irq_patch(u, p, BASE, RET)
    assert not matches_irq_patch(u, replace(p, new_bytes=NEW[:-1]+b"\x01"), BASE, RET)
    assert _compose(u.code, NEW+u.code[5:], None, [("irq-entry-mem-compare", p)], "unit") == [p]


@pytest.mark.parametrize("mutation", ["anchor", "scratch_live", "observer", "slow_relocated",
                                      "wrong_ret", "wrong_width", "invalid_field"])
def test_refuses_unproved_poll(mutation):
    u = unit()
    code = bytearray(u.code)
    if mutation == "anchor":
        u.opmarks.append(SimpleNamespace(host_off=3))
    elif mutation == "scratch_live":
        code[15:18] = bytes.fromhex("8b 5b 04")
    elif mutation == "observer":
        code[18] = 0x9f  # LAHF observes AF before its next definition.
    elif mutation == "slow_relocated":
        code[SLOW+8] ^= 1
    elif mutation == "wrong_ret":
        u.sites[1].value += 1
    elif mutation == "wrong_width":
        code[0] = 0x8a
    elif mutation == "invalid_field":
        code[2] += 1
    u.code = bytes(code)
    assert not can_fuse_irq_entry(u, BASE, RET)

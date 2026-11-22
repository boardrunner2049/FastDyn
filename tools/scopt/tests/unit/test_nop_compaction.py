"""Replay and perturb a captured direct exit from the Pye training guest."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from scopt.compact_nops import Ineligible, compact
from scopt.decode.search import decode_search, unwind
from scopt.decode.x86 import sweep
from scopt.formats.sc_v5 import ScRecord, ScUnit
from scopt.formats.scm_v1 import parse_scm

EVIDENCE = Path(__file__).resolve().parents[3] / "trebuchet/results/real-applications/pye-compaction"


@pytest.fixture
def captured():
    raw = json.loads((EVIDENCE / "source-unit.json").read_text())
    chunk = parse_scm(bytes.fromhex(raw["tb_chunk_scm"])).tbs[raw["slot"]]
    unit = ScUnit(raw["slot"], ScRecord.from_bytes(chunk.record), chunk.record, bytes.fromhex(raw["body"]))
    return unit, chunk, raw["arena"]


def test_real_exit_equivalence(captured):
    unit, chunk, base = captured
    result, output, plan = compact(unit, chunk, base)
    tc = base + unit.record.tc_offset
    before, after = sweep(unit.code, tc), sweep(result.code, tc)
    assert len(after) == len(before) - 1
    assert len(result.code) == len(unit.code) - 4
    assert result.code.hex() == json.loads((EVIDENCE / "original.json").read_text())["new_code"]
    original_rows, new_rows = [decode_search(u.search, u.record.icount) for u in (unit, result)]
    for first, second in zip((i for i in before if i.address != tc + plan.store + 3), after, strict=True):
        assert first.mnemonic == second.mnemonic
        assert second.address - tc == plan.offset(first.address - tc)
        assert unwind(original_rows, first.end - tc) == unwind(new_rows, second.end - tc)
        for a, b in ((first.branch_target, second.branch_target), (first.rip_target, second.rip_target)):
            if a is None or not tc <= a < tc + len(unit.code):
                assert a == b
            else:
                assert b == tc + plan.offset(a - tc)
        if first.branch_target is None and first.rip_target is None:
            assert first.op_str == second.op_str
    assert result.record.end_offset == unit.record.end_offset
    assert result.record.jmp_insn_offset[0] == 96
    assert result.record.jmp_reset_offset[0] == 100
    assert output.opmarks[0].host_off == 95
    assert output.sites[0].width == 1


def test_reject_removed_branch_target(captured):
    unit, chunk, base = captured
    code = bytearray(unit.body)
    code[10:14] = (96 - 14).to_bytes(4, "little")
    site = replace(chunk.sites[0], value=base + unit.record.tc_offset + 96, final_field=82)
    with pytest.raises(Ineligible, match="no eligible|removed padding"):
        compact(replace(unit, body=bytes(code)), replace(chunk, sites=(site, *chunk.sites[1:])), base)


def test_reject_opaque_offset_and_pool(captured):
    unit, chunk, base = captured
    bad = replace(chunk.insns[0], host_end=94)
    with pytest.raises(Ineligible, match="metadata enters"):
        compact(unit, replace(chunk, insns=(bad, *chunk.insns[1:])), base)
    with pytest.raises(Ineligible, match="pool-free"):
        compact(unit, replace(chunk, pool_start=124), base)


def test_reject_misaligned_patch_field(captured):
    unit, chunk, base = captured
    with pytest.raises(Ineligible, match="no eligible"):
        compact(replace(unit, record=unit.record.replace(jmp_insn_offset=(99, 65535))), chunk, base)

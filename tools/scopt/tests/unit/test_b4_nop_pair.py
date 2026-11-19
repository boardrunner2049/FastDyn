from dataclasses import replace
from types import SimpleNamespace as Row

import pytest

from scopt.analysis.hostops import OpKind
from scopt.analysis.syncdead import b4_pair_anchors, nop_bytes
from scopt.model.unit import Insn, RelocInfo
from scopt.passes.base import Patch
from scopt.passes.eliminate_state_sync import PAIR_TEMPLATE, _patch_classes, _store_groups
from scopt.validate import _b4_pair_problems


def _fixture():
    # mov [rbp-0x18],r12d; mov byte ptr [rbp-4],1; ret
    body = bytes.fromhex("448965e8c645fc01c3")
    unit = Row(code=body, meta_status="ok", reloc=RelocInfo(True, (), ()),
               pool_start=None, tc_size=len(body), tc_offset=0x10, sites=(), opmarks=(),
               recovery=(), insns=(Insn(0, (0, 0, 0), 0, 8),))
    classified = {"neg.plugin_mem_value_low": "D3", "neg.can_do_io": "D2"}

    def op(off, kind, field):
        return Row(off=off, end=off + 4, is_env_store=True, kind=kind, store_field=field,
                   env=[Row(field=field, rw="w", classified=True)])

    hostops = Row(ops=[op(0, OpKind.ENV_STORE_REG, "neg.plugin_mem_value_low"),
                       op(4, OpKind.ENV_STORE_IMM, "neg.can_do_io")])
    contract = Row(deletable_class=classified.get, not_deletable_reason=lambda f: None)
    patch = Patch("unit", 0, body[:8], nop_bytes(8), PAIR_TEMPLATE,
                  "D3:neg.plugin_mem_value_low:plugin_mem_value|D2:neg.can_do_io:cdio_insn")
    return unit, hostops, contract, patch


def test_consecutive_certified_stores_fold_to_one_nop():
    unit, hostops, contract, patch = _fixture()
    anchors = b4_pair_anchors(unit, 0)
    assert not _b4_pair_problems(unit, hostops, patch, contract, anchors)
    assert _patch_classes(patch) == ("D3", "D2")
    snapshot = Row(unit=lambda uid: unit, header=Row(code_address=0))
    stores = [Row(off=op.off, size=4) for op in hostops.ops]
    assert list(_store_groups(snapshot, "unit", stores, coalesce=False)) == [(s,) for s in stores]
    assert list(_store_groups(snapshot, "unit", stores, coalesce=True)) == [tuple(stores)]


@pytest.mark.parametrize("changes", [
    {"recovery": (Row(host_return_offset=4),)},
    {"opmarks": (Row(host_off=4),)},
    {"insns": (Insn(0, (0, 0, 0), 0, 4), Insn(1, (0, 0, 0), 4, 8))},
    {"code": bytes.fromhex("448965e8c645fc01ebfa"), "tc_size": 10}, # branch back to second mov
])
def test_pair_cannot_erase_a_known_internal_anchor(changes):
    unit, hostops, contract, patch = _fixture()
    vars(unit).update(changes)
    assert _b4_pair_problems(unit, hostops, patch, contract, b4_pair_anchors(unit, 0))


def test_patch_cannot_merge_non_deletable_store_or_claim_a_different_class():
    unit, hostops, contract, patch = _fixture()
    anchors = b4_pair_anchors(unit, 0)
    lie = replace(patch, justification=patch.justification.replace("D3:", "D2:"))
    assert any("classes do not match" in issue for issue in
               _b4_pair_problems(unit, hostops, lie, contract, anchors))
    contract.deletable_class = lambda name: "D2" if name == "neg.can_do_io" else None
    assert any("field_not_deletable_class" in issue for issue in
               _b4_pair_problems(unit, hostops, patch, contract, anchors))


@pytest.mark.parametrize(("short", "gap", "expected"), [
    ("895d0c", 3, "899d0c000000"),
    ("895dfc", 4, "40899dfcffffff"),
    ("448965fc", 3, "4489a5fcffffff"),
])
def test_widen_move_without_changing_effective_address(short, gap, expected):
    from scopt.analysis.syncdead import b4_widen_move

    assert b4_widen_move(bytes.fromhex(short), gap) == bytes.fromhex(expected)


@pytest.mark.parametrize(("raw", "gap"), [
    ("8865fc", 4),     # Bare REX would reinterpret AH as SPL.
    ("448965fc", 4),   # Can't insert a second REX.
    ("64895dfc", 3),   # Segment override.
    ("895c1dfc", 3),   # Indexed memory.
    ("894704", 3),     # Non-env memory.
])
def test_widen_move_rejects_unapproved_encodings(raw, gap):
    from scopt.analysis.syncdead import b4_widen_move

    assert b4_widen_move(bytes.fromhex(raw), gap) is None


def test_nop_absorption_rejects_different_mov_and_preserves_retained_store():
    from scopt.analysis.syncdead import b4_widen_move
    from scopt.passes.eliminate_state_sync import ABSORB_TEMPLATE

    unit, hostops, contract, patch = _fixture()
    # First store is deleted, second one remains with its original immediate,
    # despite the widened encoding and shifted first byte.
    rewritten = b4_widen_move(unit.code[4:8], 4)
    patch = replace(patch, template_id=ABSORB_TEMPLATE, new_bytes=rewritten,
                    justification="D3:neg.plugin_mem_value_low:plugin_mem_value:left")
    anchors = b4_pair_anchors(unit, 0)
    assert not _b4_pair_problems(unit, hostops, patch, contract, anchors)
    mutant = replace(patch, new_bytes=rewritten[:-1] + b"\x00")
    assert any("preserve" in p for p in _b4_pair_problems(unit, hostops, mutant, contract, anchors))

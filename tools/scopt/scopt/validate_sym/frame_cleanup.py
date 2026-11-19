"""Checked compaction of an audited, single-entry straight-line frame.

The deletion list is in unpadded, post-inline coordinates. No patch can
introduce a load, cross a call or change a relocation-bearing instruction.
"""

from ..decode.x86 import sweep, tiles
from ..model.native import NativeRelocation
from .native_inline import _require

_RELEASE = bytes.fromhex("488d642408")


def cleanup_frame(code, relocs):
    insns = sweep(code, 0)
    _require(tiles(insns, 0, len(code)) and all(not i.is_jump and not i.is_ret for i in insns),
             "frame_cleanup_cfg", "requires single-entry, linear fall-through")
    # In the expanded worker, the next outer LEAVE replaces RSP with RBP.
    # LEA writes no flags and does not access its nominal memory operand.
    drop = set()
    next_live = None
    for ins in reversed(insns):
        if ins.raw == b"\x90" or (ins.raw == _RELEASE and next_live == b"\xc9"):
            drop.add(ins.address)
        else:
            next_live = ins.raw
    mapping = {}
    result = bytearray()
    for ins in insns:
        mapping[ins.address] = len(result)
        if ins.address not in drop:
            result.extend(ins.raw)
    fixed = []
    for rel in relocs:
        ins = next((i for i in insns if i.end == rel.next_off), None)
        _require(ins is not None and ins.address not in drop and
                 ins.address <= rel.field_off <= ins.end - 4,
                 "frame_cleanup_relocation", "reference has no surviving instruction")
        delta = mapping[ins.address] - ins.address
        fixed.append(NativeRelocation(rel.field_off + delta, rel.next_off + delta, rel.target_abs))
    return result, fixed


def verify_cleanup(source, original_relocs, result, result_relocs):
    """Check retained operations and relocation targets independently in forward order.

    Only exact single-byte NOPs are unobservable. A skipped stack adjustment
    additionally requires LEAVE as the next retained instruction, with no call,
    read, fault point, or other intervening use of RSP.
    """
    before = sweep(bytes(source), 0)
    after = sweep(bytes(result), 0)
    _require(tiles(before, 0, len(source)) and tiles(after, 0, len(result)),
             "frame_cleanup_proof", "invalid instruction boundary")
    expected = []
    pos = 0
    stack_changed = False
    for op in before:
        matched = pos < len(after) and op.raw == after[pos].raw
        references = [r for r in original_relocs if r.next_off == op.end]
        if matched:
            _require(not stack_changed or op.raw == b"\xc9",
                     "frame_cleanup_proof", "an intervening operation can observe RSP")
            stack_changed = False
            for ref in references:
                expected.append(NativeRelocation(after[pos].end + ref.field_off - op.end,
                                                 after[pos].end, ref.target_abs))
            pos += 1
        else:
            _require(not references and op.raw in (b"\x90", _RELEASE),
                     "frame_cleanup_proof", "deleted an observable instruction or reference")
            stack_changed |= op.raw == _RELEASE
    _require(not stack_changed and pos == len(after) and len(original_relocs) == len(expected)
             and tuple(expected) == tuple(result_relocs),
             "frame_cleanup_proof", "changed continuation, instruction stream or symbolic target")

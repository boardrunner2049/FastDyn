from dataclasses import replace

from scopt.canon import sha256_id
from scopt.decode.x86 import sweep
from scopt.formats.scm_v1 import _native_metadata_matches
from scopt.model.native import (HELPER_GRAPH_KIND, InlineCall, NativeFragment,
                                NativeFrameBody)
from scopt.splice_layout import splice_group_offsets
from scopt.validate_sym.native_helper_graph import _build


def _request(code: bytes) -> InlineCall:
    return InlineCall(
        target="tb:test#0", unit_hash=sha256_id(b"unit"), call_off=3,
        call_bytes=bytes.fromhex("e800000000"), continuation_off=8,
        helper_id=7, target_abs=0x555550001000, pool_slot_off=None,
        guest_pcs=(0x1001,), image_id=sha256_id(b"qemu"), elf_address=0x1000,
        image_offset=0x1000, symbol="helper_root", callee_code=code,
        native_input_id=sha256_id(b"input"), fragment=NativeFragment(b"", (), ()),
        bridge_sha256=None, tcg_typemask=2)


def test_helper_graph_copies_nested_callee_without_a_depth_limit():
    root = bytes.fromhex("e8fb000000 83c001 c3")
    child = bytes.fromhex("b82a000000 c3")
    request = _request(root)
    splice = _build(request, (NativeFrameBody(0x1000, root),
                              NativeFrameBody(0x1100, child)))

    assert splice.kind == HELPER_GRAPH_KIND
    assert splice.depth == 0
    assert splice.frame_chain == (NativeFrameBody(0x1100, child),)
    assert not splice.relocations
    assert (len(splice.code) - len(request.call_bytes)) % 8 == 0
    insns = sweep(splice.code, 0)
    copied_call = next(insn for insn in insns if insn.address == 9)
    child_start = 9 + len(root)
    assert copied_call.branch_target == child_start


def test_helper_graph_represents_recursive_edges_once():
    root = bytes.fromhex("e8fb000000 c3")
    child = bytes.fromhex("e8fbfeffff c3")
    request = _request(root)
    splice = _build(request, (NativeFrameBody(0x1000, root),
                              NativeFrameBody(0x1100, child)))

    calls = [insn for insn in sweep(splice.code, 0) if insn.is_call]
    assert len(splice.frame_chain) == 1
    assert [insn.branch_target for insn in calls] == [9 + len(root), 9]


def test_helper_graph_retains_external_calls_as_exact_relocations():
    root = bytes.fromhex("e8fb0f0000 c3")  # ELF 0x1000 -> 0x2000
    request = _request(root)
    splice = _build(request, (NativeFrameBody(0x1000, root),))

    assert not splice.relocations
    call = next(insn for insn in sweep(splice.code, 0) if insn.address == 9)
    thunk = call.branch_target
    assert splice.code[thunk:thunk + 2] == bytes.fromhex("49bb")
    assert int.from_bytes(splice.code[thunk + 2:thunk + 10], "little") == (
        request.target_abs - request.elf_address + 0x2000)


def test_helper_graph_metadata_accepts_later_variable_size_splices():
    root = bytes.fromhex("b82a000000 c3")
    first = _request(root)
    first = replace(first, splice=_build(first, (NativeFrameBody(0x1000, root),)))
    second = replace(_request(root), call_off=20, continuation_off=25)
    second = replace(second, splice=_build(second, (NativeFrameBody(0x1000, root),)))
    requests = (first, second)
    offsets = splice_group_offsets(requests)

    assert offsets(second.call_off) != second.call_off
    original_size = 64
    final_size = original_size + sum(request.growth for request in requests)
    assert _native_metadata_matches(4, offsets(first.call_off), final_size, requests)

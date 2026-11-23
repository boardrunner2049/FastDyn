from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopt.canon import sha256_id
from scopt.decode.x86 import sweep
from scopt.formats.scm_v1 import _native_metadata_matches
from scopt.model.native import (HELPER_GRAPH_KIND, InlineCall, NativeFragment,
                                NativeFrameBody)
from scopt.splice_layout import splice_group_offsets
from scopt.passes.host_inline_graph import HostInlineGraph
from scopt.validate_sym.native_helper_graph import (_build, _leaf_fallthrough_code,
                                                     root_wrapper_refusal,
                                                     verify_helper_graph_splice_body)
from scopt.validate_sym.native_inline import NativeInlineError


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


def test_register_only_leaf_uses_true_fallthrough_without_push_or_ret():
    root = bytes.fromhex("b82a000000 c3")
    request = _request(root)
    splice = _build(request, (NativeFrameBody(0x1000, root),))

    assert splice.code == root[:-1]
    assert len(splice.code) == len(request.call_bytes)
    instructions = sweep(splice.code, 0)
    assert not any(insn.is_call or insn.is_ret for insn in instructions)
    assert bytes.fromhex("4153") not in splice.code  # no synthetic PUSH R11


def test_leaf_branch_to_final_ret_lands_on_fallthrough_padding():
    root = bytes.fromhex("85ff 7406 b801000000 c3 31c0 c3")
    request = _request(root)
    splice = _build(request, (NativeFrameBody(0x1000, root),))

    instructions = sweep(splice.code, 0)
    conditional = next(insn for insn in instructions if insn.mnemonic == "je")
    early_return = next(insn for insn in instructions if insn.mnemonic == "jmp")
    xor = next(insn for insn in instructions if insn.mnemonic == "xor")
    assert conditional.branch_target == xor.address
    assert early_return.branch_target == len(splice.code)
    assert not any(insn.is_call or insn.is_ret for insn in instructions)
    assert (len(splice.code) - len(request.call_bytes)) % 8 == 0


def test_leaf_fallthrough_allows_trusted_environment_memory():
    root = bytes.fromhex("8b07 894704 c3")  # mov eax,[rdi]; mov [rdi+4],eax; ret
    request = _request(root)
    splice = _build(request, (NativeFrameBody(0x1000, root),))

    assert splice.code == root[:-1]
    assert not any(insn.is_ret for insn in sweep(splice.code, 0))


@pytest.mark.parametrize("name,source_hex", [
    ("helper_shl_cc",
     "f30f1efa440fb6c281e2e0000000753089f04585c07509c30f1f840000000000"
     "b92000000089f24429c1d3ea4489c183e201d3e0899710020000c30f1f440000"
     "4183f8207412c787100200000000000031c0c30f1f44000083e60131c089b710"
     "020000c3"),
    ("helper_shr_cc",
     "f30f1efa440fb6c281e2e0000000753089f04585c07509c30f1f840000000000"
     "418d48ff89f2d3ea4489c183e201d3e8899710020000c3660f1f840000000000"
     "4183f8207412c787100200000000000031c0c30f1f440000c1ee1f31c089b710"
     "020000c3"),
])
def test_real_hot_shift_helper_uses_multi_return_fallthrough(name, source_hex):
    source = bytes.fromhex(source_hex)
    source_insns = sweep(source, 0x1000)
    assert len(source) == 100 and sum(insn.is_ret for insn in source_insns) == 4, name

    code = _leaf_fallthrough_code(5, (NativeFrameBody(0x1000, source),))
    assert code is not None
    output = sweep(code, 0)
    starts = {insn.address for insn in output}
    assert len(code) == 125
    assert not any(insn.is_call or insn.is_ret for insn in output)
    assert sum(insn.mnemonic == "jmp" for insn in output) == 3
    assert all(insn.branch_target in starts | {len(code)}
               for insn in output if insn.branch_target is not None)
    assert bytes.fromhex("4153") not in code


@pytest.mark.parametrize("source", [
    bytes.fromhex("8b0500000000 c3"),  # RIP-relative memory
    bytes.fromhex("ffe0 c3"),          # indirect jump
    bytes.fromhex("eb05 90 c3"),       # direct branch outside the function
])
def test_leaf_fallthrough_rejects_address_dependent_or_open_control(source):
    assert _leaf_fallthrough_code(5, (NativeFrameBody(0x1000, source),)) is None


def test_stack_observing_leaf_keeps_the_return_stack_lowering():
    root = bytes.fromhex("8b442408 c3")  # mov eax,[rsp+8]; ret
    request = _request(root)
    splice = _build(request, (NativeFrameBody(0x1000, root),))

    assert splice.code.startswith(bytes.fromhex("4c8d1d"))
    assert splice.code[7:9] == bytes.fromhex("4153")
    assert any(insn.is_ret for insn in sweep(splice.code, 0))


def test_root_tail_wrapper_calls_shared_body_then_falls_through():
    # endbr64; sub rdi,0x2bc0; jmp 0x1100
    root = bytes.fromhex("f30f1efa 4881efc02b0000 e9f0000000")
    request = _request(root)
    splice = _build(request, (NativeFrameBody(0x1000, root),))

    instructions = sweep(splice.code, 0)
    assert root_wrapper_refusal(0x1000, root) is None
    assert not any(insn.is_ret or insn.mnemonic == "jmp" for insn in instructions)
    assert sum(insn.is_call for insn in instructions) == 1
    assert bytes.fromhex("4881efc02b0000") in splice.code
    assert (request.target_abs + 0x100).to_bytes(8, "little") in splice.code


def test_root_aligned_call_wrapper_drops_outer_stack_pair():
    # endbr64; sub rsp,8; sub rdi,0x2bc0; call 0x1100; add rsp,8;
    # movzx eax,ax; ret
    root = bytes.fromhex(
        "f30f1efa 4883ec08 4881efc02b0000 e8ec000000 4883c408 0fb7c0 c3")
    request = _request(root)
    splice = _build(request, (NativeFrameBody(0x1000, root),))

    assert root_wrapper_refusal(0x1000, root) is None
    assert bytes.fromhex("4883ec08") not in splice.code
    assert bytes.fromhex("4883c408") not in splice.code
    instructions = sweep(splice.code, 0)
    assert sum(insn.is_call for insn in instructions) == 1
    assert not any(insn.is_ret for insn in instructions)
    assert bytes.fromhex("0fb7c0") in splice.code


def test_root_wrapper_rejects_general_stack_frame():
    root = bytes.fromhex("55 4889e5 b801000000 5d c3")
    refusal = root_wrapper_refusal(0x1000, root)
    assert refusal is not None
    assert refusal[0] == "not_root_wrapper"


def test_leaf_fallthrough_validator_rebuilds_and_rejects_changed_bytes():
    root = bytes.fromhex("b82a000000 c3")
    request = _request(root)
    splice = _build(request, (NativeFrameBody(0x1000, root),))
    request = replace(request, splice=splice)
    verify_helper_graph_splice_body(request)

    changed = replace(request, splice=replace(splice, code=splice.code + b"\x90"))
    with pytest.raises(NativeInlineError) as exc:
        verify_helper_graph_splice_body(changed)
    assert exc.value.reason == "helper_graph_correspondence"


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
    # Stack observation deliberately selects the general graph lowering, whose
    # synthetic return slot gives this metadata test nonzero growth.
    root = bytes.fromhex("8b442408 c3")
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


def test_trusted_helper_graph_source_size_policy_is_explicit_and_complete():
    callee = SimpleNamespace(
        code=b"r" * 31,
        frame_chain=(NativeFrameBody(0x1100, b"a" * 17),
                     NativeFrameBody(0x1200, b"b" * 23)),
    )
    binding = SimpleNamespace(callee=callee)
    policy = HostInlineGraph()

    assert policy._policy_refusal(binding, {"trusted_max_source_graph_bytes": 0}) is None
    assert policy._policy_refusal(binding, {"trusted_max_source_graph_bytes": 71}) is None
    reason, detail = policy._policy_refusal(
        binding, {"trusted_max_source_graph_bytes": 70})
    assert reason == "helper_graph_size_policy"
    assert "71 bytes" in detail

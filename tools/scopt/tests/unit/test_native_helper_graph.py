import ctypes
import mmap
import platform
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
    assert not any(insn.is_ret for insn in instructions)
    assert sum(insn.is_call for insn in instructions) == 1
    assert bytes.fromhex("4881efc02b0000") in splice.code
    assert (request.target_abs + 0x100).to_bytes(8, "little") in splice.code


def test_root_aligned_call_wrapper_preserves_inner_stack_and_flags():
    # endbr64; sub rsp,8; sub rdi,0x2bc0; call 0x1100; add rsp,8;
    # movzx eax,ax; ret
    root = bytes.fromhex(
        "f30f1efa 4883ec08 4881efc02b0000 e8ec000000 4883c408 0fb7c0 c3")
    request = _request(root)
    splice = _build(request, (NativeFrameBody(0x1000, root),))

    assert root_wrapper_refusal(0x1000, root) is None
    assert bytes.fromhex("4883ec08") in splice.code
    assert bytes.fromhex("4883c408") in splice.code
    instructions = sweep(splice.code, 0)
    assert sum(insn.is_call for insn in instructions) == 1
    assert any(insn.is_ret for insn in instructions)
    assert bytes.fromhex("0fb7c0") in splice.code


def test_root_wrapper_rejects_general_stack_frame():
    root = bytes.fromhex("55 4889e5 b801000000 5d c3")
    refusal = root_wrapper_refusal(0x1000, root)
    assert refusal is not None
    assert refusal[0] == "not_root_wrapper"


@pytest.mark.skipif(platform.system() != "Linux" or platform.machine() != "x86_64",
                    reason="executes native SysV x86-64 code")
@pytest.mark.parametrize(("source", "child", "expected", "wrapper"), [
    # lea rax,[rip+disp32]; jmp child. The address must still name the source data.
    ("488d0539000000 e9f4000000", "c3", "data_address", True),
    # sub rsp,8; call child; add rsp,8; sete al; ret. ADD clears the child's ZF.
    ("4883ec08 e8f7000000 4883c408 0f94c0 c3", "31c0 c3", 0, True),
    # The ADD flags also matter when SUB's flags were killed before the call.
    ("4883ec08 39c0 e8f5000000 4883c408 0f94c0 c3", "31c0 c3", 0, True),
    # The inner callee observes an outgoing stack argument.
    ("4883ec08 e8f7000000 4883c408 c3", "488b442418 c3", 42, True),
    # A private inner target consumes the wrapper's R11.
    ("41bb2a000000 e9f5000000", "4c89d8 c3", 42, True),
    ("4883ec08 41bb2a000000 e8f1000000 4883c408 c3", "4c89d8 c3", 42, True),
    # mov [rsp-8],42; mov eax,[rip+disp32]; mov rax,[rsp-8]; ret
    ("48c74424f82a000000 8b0531000000 488b4424f8 c3", "c3", 42, False),
    # The indirect tail transfer must preserve the red-zone value and original RSP.
    ("48c74424f82a000000 ff2531000000", "488b4424f8 c3", 42, False),
])
def test_native_graph_matches_original_after_relocation(source, child, expected, wrapper):
    root = bytes.fromhex(source)
    assert (root_wrapper_refusal(0x1000, root) is None) == wrapper
    # One page contains both functions, pointer data, the baseline caller, and
    # the optimized caller. The two callers enter with the same stack alignment.
    with mmap.mmap(-1, mmap.PAGESIZE, prot=mmap.PROT_READ | mmap.PROT_WRITE) as page:
        address = ctypes.addressof(ctypes.c_char.from_buffer(page))
        page[:len(root)] = root
        page[64:72] = (address + 256).to_bytes(8, "little")
        child = bytes.fromhex(child)
        page[256:256 + len(child)] = child
        request = replace(_request(root), target_abs=address)
        splice = _build(request, (NativeFrameBody(0x1000, root),))
        prologue = bytes.fromhex("4883ec18 48c704242a000000 48c744241049000000")
        epilogue = bytes.fromhex("4883c418 c3")
        call = b"\xe8" + (-(512 + len(prologue) + 5)).to_bytes(4, "little", signed=True)
        for offset, body in ((512, call), (1024, splice.code)):
            caller = prologue + body + epilogue
            page[offset:offset + len(caller)] = caller
        libc = ctypes.CDLL(None)
        assert libc.mprotect(ctypes.c_void_p(address), ctypes.c_size_t(len(page)),
                             mmap.PROT_READ | mmap.PROT_EXEC) == 0
        target = address + 64 if expected == "data_address" else expected
        func = ctypes.CFUNCTYPE(ctypes.c_uint64)
        assert func(address + 512)() == func(address + 1024)() == target


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
    assert splice.code[thunk:thunk + 8] == bytes.fromhex("ff2502000000 49bb")
    assert int.from_bytes(splice.code[thunk + 8:thunk + 16], "little") == (
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

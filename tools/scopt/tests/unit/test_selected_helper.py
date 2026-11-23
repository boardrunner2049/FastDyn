"""Hot-site root lowering and receipt checks using ordinary SysV x86-64 code."""

import ctypes
from dataclasses import replace
import mmap
import platform
from types import SimpleNamespace

import pytest

from scopt.decode.x86 import sweep
from scopt.importer.native_callee import NativeCalleeResolver
from scopt.model.native import InlineCall, NativeFrameBody
from scopt.validate_sym.native_helper_graph import _build, verify_helper_graph_splice_body
from scopt.validate_sym.native_inline import NativeInlineError
from tests.unit.test_native_helper_graph import _request


def test_selected_root_folds_immediate_leaf_and_retains_branch_continuations():
    # xor eax,eax; test edi,edi; je ret; call child; add eax,1; ret
    root = NativeFrameBody(0x1000, bytes.fromhex("31c0 85ff 7408 e8f5000000 83c001 c3"))
    child = NativeFrameBody(0x1100, bytes.fromhex("b82a000000 c3"))
    cold = NativeFrameBody(0x1200, bytes.fromhex("b877777777 c3"))
    request = _request(root.code)
    splice = _build(request, (root, child, cold), selected_root=True)
    instructions = sweep(splice.code, 0)
    assert splice.frame_chain == (child,)
    assert not any(i.is_call or i.is_ret for i in instructions)
    assert bytes.fromhex("b82a000000 83c001") in splice.code
    branch = next(i for i in instructions if i.mnemonic == "je")
    assert branch.branch_target == next(i.end for i in instructions if i.mnemonic == "add")
    assert splice.call_lowering.nested_calls_eliminated == 1
    assert splice.call_lowering.nested_calls_retained == 0
    assert splice.call_lowering.copied_body_instances == 2
    recovered = InlineCall.from_json(replace(request, splice=splice).to_json())
    verify_helper_graph_splice_body(recovered)


@pytest.mark.parametrize("child", [
    "488b0424 c3",        # Reads return slot
    "e8fbffffff c3",      # Self-recursive call
    "e8fb000000 c3",      # An additional call depth
    "e2fe c3",            # LOOP rel8 needs rebasing
])
def test_selected_root_declines_unmodeled_children(child):
    root = NativeFrameBody(0x1000, bytes.fromhex("e8fb000000 83c001 c3"))
    graph = (root, NativeFrameBody(0x1100, bytes.fromhex(child)))
    with pytest.raises(NativeInlineError, match="root requires"):
        _build(_request(root.code), graph, selected_root=True)


def test_selected_tail_wrapper_receipt_reports_shared_call():
    root = NativeFrameBody(0x1000, bytes.fromhex("4883ef04 e9f7000000"))
    child = NativeFrameBody(0x1100, bytes.fromhex("488b0424 c3"))
    splice = _build(_request(root.code), (root, child), selected_root=True)
    instructions = sweep(splice.code, 0)
    assert sum(i.is_call for i in instructions) == 1
    assert splice.frame_chain == ()
    assert splice.call_lowering.root_transfer == "shared-wrapper"
    assert splice.call_lowering.wrapper_calls_introduced == 1
    assert splice.call_lowering.nested_calls_eliminated == 0


@pytest.mark.skipif(platform.machine() != "x86_64" or platform.system() != "Linux",
                    reason="executes native SysV x86-64 code")
def test_selected_branch_and_tail_match_native_calls():
    child = NativeFrameBody(0x1100, bytes.fromhex("8d4703 c3"))   # x + 3
    roots = [("31c0 85ff 7408 e8f5000000 83c001 c3", (0, 9)),  # x ? child(x)+1 : 0
             ("83c701 e9f8000000", (4, 9))]                    # tail child(x+1)
    for source, expected in roots:
        root = NativeFrameBody(0x1000, bytes.fromhex(source))
        splice = _build(_request(root.code), (root, child), selected_root=True)
        assert not any(i.is_call or i.is_ret for i in sweep(splice.code, 0))
        with mmap.mmap(-1, mmap.PAGESIZE, prot=mmap.PROT_READ | mmap.PROT_WRITE) as page:
            base = ctypes.addressof(ctypes.c_char.from_buffer(page))
            page[:len(root.code)] = root.code
            page[256:256 + len(child.code)] = child.code
            page[512:513 + len(splice.code)] = splice.code + b"\xc3"
            assert ctypes.CDLL(None).mprotect(ctypes.c_void_p(base), ctypes.c_size_t(len(page)),
                                              mmap.PROT_READ | mmap.PROT_EXEC) == 0
            func = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)
            for arg, result in zip((0, 5), expected, strict=True):
                assert func(base)(arg) == func(base + 512)(arg) == result


def test_selected_discovery_stops_after_direct_children(monkeypatch):
    resolver = NativeCalleeResolver(None, None, {}, "capture", immediate_helpers=True)
    image = SimpleNamespace(symbols=SimpleNamespace(
        lookup=lambda addr: ("f", 0),
        bounded_function=lambda name, addr: SimpleNamespace(size=6)))
    seen = []
    def read(image, mapping, bias, addr, size):
        seen.append(addr)
        return bytes.fromhex("e8fb000000 c3")
    monkeypatch.setattr(resolver, "_text_bytes", read)
    children = resolver._helper_graph(image, SimpleNamespace(path="qemu"), 0, 0x1000,
                                      bytes.fromhex("e8fb000000 c3"))
    assert seen == [0x1100] and [b.address for b in children] == [0x1100]


def test_exhaustive_graph_records_retained_nested_calls():
    root = NativeFrameBody(0x1000, bytes.fromhex("e8fb000000 83c001 c3"))
    child = NativeFrameBody(0x1100, bytes.fromhex("b82a000000 c3"))
    splice = _build(_request(root.code), (root, child))
    assert splice.call_lowering.root_transfer == "synthetic-return"
    assert splice.call_lowering.nested_calls_retained == 1
    assert splice.call_lowering.nested_calls_eliminated == 0

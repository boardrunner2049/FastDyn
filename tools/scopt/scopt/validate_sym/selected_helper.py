"""Emit a selected QEMU helper root, optionally folding immediate native leaves."""

from __future__ import annotations

from functools import lru_cache

from ..decode.x86 import sweep
from ..model.native import HelperCallLowering, NativeFrameBody
from .native_inline import NativeInlineError


def selected_frame_chain(call, captured):
    """Reconstruct the exact selected source subset from authenticated ELF bodies."""
    root = NativeFrameBody(call.elf_address, call.callee_code)
    _, used, _ = lower_selected_root(root, direct_children(root, tuple(captured)),
                                     call.target_abs, len(call.call_bytes))
    return used


@lru_cache(maxsize=256)
def direct_children(root: NativeFrameBody, captured: tuple[NativeFrameBody, ...]):
    # Only these bodies can enter the selected artifact. Deeper callees can
    # still execute via an admitted wrapper's call into the shared QEMU image.
    targets = {i.branch_target for i in sweep(root.code, root.address)
               if i.is_call or i.mnemonic == "jmp"}
    return tuple(c for c in captured if c.address in targets and c.address != root.address)


@lru_cache(maxsize=256)
def lower_selected_root(root: NativeFrameBody, children: tuple[NativeFrameBody, ...],
                        target_abs: int, call_size: int):
    from .native_helper_graph import (_absolute_rip, _closed_fallthrough_code,
                                      _root_wrapper_fallthrough_code, _root_wrapper_plan)
    from .native_splice import _PADDING

    calls = [i for i in sweep(root.code, root.address) if i.is_call]
    substitutions = {}
    used = []
    for child in direct_children(root, children):
        leaf = _closed_fallthrough_code(None, child, {})
        if leaf is not None:
            substitutions[child.address] = leaf
            used.append(child)
    code = _closed_fallthrough_code(call_size, root, substitutions)
    if code is not None:
        return (code, tuple(used),
                HelperCallLowering("fallthrough", 1 + len(calls), len(calls), 0))

    wrapper = _root_wrapper_plan(root.address, root.code)
    if wrapper is not None and wrapper[0] == "tail" and wrapper[2] in substitutions:
        _, prep, target, _ = wrapper
        bias = target_abs - root.address
        prelude = b"".join(_absolute_rip(i, bias + i.rip_target)
                           if i.rip_target is not None else i.raw for i in prep)
        code = prelude + substitutions[target]
        code += _PADDING[(call_size - len(code)) % 8]
        return code, tuple(used), HelperCallLowering("tail-leaf-fallthrough", 2, 0, 0)

    # Retain an external tail target when it requires a shared implementation.
    code = _root_wrapper_fallthrough_code(root.address, target_abs, call_size, (root,))
    if code is not None:
        is_tail = _root_wrapper_plan(root.address, root.code)[0] == "tail"
        return code, (), HelperCallLowering("shared-wrapper", 1, 0, len(calls), int(is_tail))
    raise NativeInlineError("selected_root_unsupported",
                            "root requires an observable return frame, open control flow, "
                            "or unsupported relocation")

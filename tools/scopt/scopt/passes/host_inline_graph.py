"""Explicitly trusted, unbounded-depth QEMU helper-graph inlining."""

from __future__ import annotations

from dataclasses import replace

from ..analysis.native_callees import NativeCallBinding
from ..model.native import InlineCall, NativeFragment, NativeFrameBody
from ..validate_sym.native_inline import NativeInlineError
from .host_inline import HostInlineSplice


class HostInlineGraph(HostInlineSplice):
    """Transplant every captured QEMU helper and its finite direct-call graph.

    There is deliberately no profitability, site, byte, instruction, wall-time,
    or expansion-depth policy here.  The graph reaches a fixed point by source
    identity, so recursive SCCs are copied once and remain recursive.
    """

    consider_cold = True
    max_sites_per_unit = None
    default_sites_per_unit = None

    def _binding_refusal(self, binding: NativeCallBinding) -> tuple[str, str] | None:
        call, callee = binding.call, binding.callee
        if callee.role != "qemu" or callee.abi != "sysv-amd64":
            return "unsupported_abi", "trusted graph policy requires the captured QEMU x86-64 image"
        return None

    def _uses_leaf_fragment(self, binding: NativeCallBinding) -> bool:
        return False

    def _prepare_request(self, unit, request: InlineCall, code_address: int,
                         binding: NativeCallBinding, params,
                         growth_room: int | None) -> InlineCall:
        from ..splice_layout import prepare_splice_layout
        from ..validate_sym.native_helper_graph import prepare_helper_graph_splice

        callee = binding.callee
        graph = (NativeFrameBody(callee.elf_address, callee.code), *callee.frame_chain)
        request = replace(request, fragment=NativeFragment(b"", (), ()),
                          tcg_typemask=callee.tcg_typemask)
        body = prepare_helper_graph_splice(unit, request, code_address, graph)
        return prepare_splice_layout(unit, replace(request, splice=body), code_address)

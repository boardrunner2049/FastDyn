"""Explicitly trusted, unbounded-depth QEMU helper-graph inlining."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

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

    def _policy_refusal(self, binding: NativeCallBinding,
                        params: Mapping[str, Any]) -> tuple[str, str] | None:
        if params.get("trusted_root_wrapper_only", False):
            from ..validate_sym.native_helper_graph import root_wrapper_refusal

            if refusal := root_wrapper_refusal(binding.callee.elf_address,
                                                binding.callee.code):
                return refusal
        limit = params.get("trusted_max_source_graph_bytes", 0)
        if not limit:
            return None
        source_bytes = self._source_graph_bytes(binding, params)
        if source_bytes > limit:
            return ("helper_graph_size_policy",
                    f"captured source graph is {source_bytes} bytes; policy limit is {limit}")
        return None

    def _source_graph_bytes(self, binding: NativeCallBinding,
                            params: Mapping[str, Any]) -> int:
        if params.get("trusted_selected_root", False):
            from ..validate_sym.selected_helper import direct_children

            root = NativeFrameBody(binding.callee.elf_address, binding.callee.code)
            children = direct_children(root, tuple(binding.callee.frame_chain))
            return len(root.code) + sum(len(body.code) for body in children)
        if params.get("trusted_root_wrapper_only", False):
            return len(binding.callee.code)
        return super()._source_graph_bytes(binding, params)

    def _prepare_request(self, unit, request: InlineCall, code_address: int,
                         binding: NativeCallBinding, params,
                         growth_room: int | None) -> InlineCall:
        from ..splice_layout import prepare_splice_layout
        from ..validate_sym.native_helper_graph import prepare_helper_graph_splice

        callee = binding.callee
        graph = (NativeFrameBody(callee.elf_address, callee.code),)
        selected_root = params.get("trusted_selected_root", False)
        if selected_root:
            from ..validate_sym.selected_helper import direct_children

            graph += direct_children(graph[0], tuple(callee.frame_chain))
        elif not params.get("trusted_root_wrapper_only", False):
            graph += tuple(callee.frame_chain)
        request = replace(request, fragment=NativeFragment(b"", (), ()),
                          tcg_typemask=callee.tcg_typemask)
        body = prepare_helper_graph_splice(unit, request, code_address, graph,
                                           selected_root=selected_root)
        if (params.get("hot_loop_only") and not params.get("include_hot_shared_wrappers")
                and body.call_lowering.root_transfer == "shared-wrapper"):
            raise NativeInlineError("shared_wrapper_retains_call",
                                    "selected hot wrapper still calls the shared QEMU body")
        return prepare_splice_layout(unit, replace(request, splice=body), code_address)

"""``chain-static@1``: GENERAL guarded slots on static off-page exits (design §3.1, §3.5, §10.1). WP21.

Same mechanism and refusals as ``chain-returns`` (see
:mod:`scopt.passes.chain_returns`) for exits of class ``static``: ``bl``,
``b``, ``b<cond>`` taken arms or page-end fall-throughs whose destination is
off the 1 KiB page (the capture's off-page StaticExit at the exit
instruction). ``g_pc`` is still compared (PCREL aliasing, §4.1). Reported as
its own arm (``b3-static``), beyond the spec's return scope.
"""

from __future__ import annotations

from typing import Any, ClassVar

from .base import PassDescriptor, PassResult
from .chain_returns import _descriptor, general_slots

__all__ = ["ChainStatic", "PASSES"]


class ChainStatic:
    descriptor: ClassVar[PassDescriptor] = _descriptor("chain-static")

    def run(self, view: Any, ctx: Any, edits: Any) -> PassResult:
        return general_slots(view, ctx, edits, "static")


PASSES = (ChainStatic,)

"""The identity pass (design §5.10): no edits, preserves every analysis.

The identity pipeline (``pipelines/identity.toml``) runs only this pass, so its
Candidate has an empty edit log and NativeLinker (WP10) reproduces the input
cache byte for byte (V9).
"""

from __future__ import annotations

from .base import Outcome, PassDescriptor, PassResult

__all__ = ["Identity", "PASSES"]


class Identity:
    # requires_capture_level=0: identity edits nothing, so it needs no capture
    # metadata (the frozen PassDescriptor default is 1; design §5.10 omits it).
    descriptor = PassDescriptor(name="identity", version=1, preserves=frozenset({"*"}),
                                requires_capture_level=0)

    def run(self, unit, ctx, edits) -> PassResult:
        return PassResult(Outcome.NO_CHANGE, "identity")


PASSES = (Identity,)

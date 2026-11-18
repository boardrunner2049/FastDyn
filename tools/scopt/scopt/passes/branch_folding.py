"""``branch-folding@1``: fold hconst-decided conditional jumps in a variant (design §3.5). WP21.

Region scope. For every reachable ``jcc`` in an ``insn`` block (never the
prefix entry check, never an LDST fast-path ``jne``) whose condition
``hconst@1`` decides under the variant's facts:

* taken -> ``jmp`` to the same target, same length (template ``bf_jcc_to_jmp``:
  ``7x rel8`` -> ``eb rel8``; ``0f 8x rel32`` -> ``e9 rel32+1`` + ``nop``);
* not taken -> nops of the same length (``bf_jcc_to_nop``).

The compare/test feeding the jump is left to DHO (disjoint effects). A
variant where no branch is decided gets ``fact_unused`` (§3.8; T-B3-41's
"BF no decided branch"). Profitability: decided branches × C' entries > 0.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, ClassVar

from ..analysis.profitability import simplification_saving
from .base import Outcome, PassDescriptor, PassResult, Patch
from ._b3util import nops, refusal_result
from .constant_propagation import region_descriptor

__all__ = ["BranchFolding", "PASSES", "fold_jcc"]


def fold_jcc(old: bytes, taken: bool) -> tuple[bytes, str] | None:
    """Same-length replacement of a ``jcc`` (rel8 or rel32) decided ``taken``."""
    if len(old) == 2 and 0x70 <= old[0] <= 0x7F:
        return (b"\xeb" + old[1:2], "bf_jcc_to_jmp") if taken else (nops(2), "bf_jcc_to_nop")
    if len(old) == 6 and old[0] == 0x0F and 0x80 <= old[1] <= 0x8F:
        if not taken:
            return nops(6), "bf_jcc_to_nop"
        rel = int.from_bytes(old[2:6], "little", signed=True) + 1
        if not -(1 << 31) <= rel < (1 << 31):
            return None
        return b"\xe9" + rel.to_bytes(4, "little", signed=True) + b"\x90", "bf_jcc_to_jmp"
    return None


class BranchFolding:
    descriptor: ClassVar[PassDescriptor] = region_descriptor(
        "branch-folding", (("hconst", 1), ("hostops", 1), ("hcfg", 1)))

    def run(self, vv: Any, ctx: Any, edits: Any) -> PassResult:
        from ..analysis.api import ScopeRef

        for a in ("hostops", "hcfg", "hconst"):
            ctx.analysis(a, ScopeRef("unit", vv.src.uid))
        h, g, lv, hc = vv.analyses()
        if not hc.ok:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "validation_inconclusive",
                              f"variant analyses not available: {hc.reasons}")
        refused: Counter[str] = Counter()
        out = []
        for i, taken in sorted(hc.decided.items()):
            o = h.ops[i]
            if o.log is not None:
                continue  # never the coverage LOG_REG spin (hconst never decides it; close-out)
            old = bytes(vv.code[o.off:o.end])
            f = fold_jcc(old, taken)
            if f is None:
                refused["no_same_length_encoding"] += 1
                continue
            new, tmpl = f
            out.append(Patch(vv.variant_id, o.off, old, new, tmpl,
                             f"{'taken' if taken else 'not taken'} under "
                             + ", ".join(f"{k}={v:#x}" for k, v in sorted(vv.facts.items()))))
        cap = int(ctx.params.get("max_patches", 64))
        out = out[:cap]
        if out:
            _s, ok = simplification_saving(len(out), vv.weight)
            if not ok:
                refused["not_profitable"] += 1
                out = []
        elif not refused:
            refused["fact_unused"] += 1  # no decided branch
        deps = tuple(f"fact:{f.fact_id}" for f in vv.factrefs) + (f"variant:{vv.variant_id}",)
        for p in out:
            edits.pure_rewrite(p, dependency_additions=deps)
        stats = {"decided_branches": len(out), "removed_work": len(out) * vv.weight}
        if out:
            return PassResult(Outcome.CANDIDATE, "", f"{len(out)} branches folded",
                              affected=(vv.src.uid,), stats=stats)
        return refusal_result(refused, nothing="no_decided_branch", stats=stats,
                              detail="no decided branch" if refused.get("fact_unused") else "")


PASSES = (BranchFolding,)

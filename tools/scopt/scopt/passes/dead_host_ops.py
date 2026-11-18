"""``dead-host-ops@1``: nop dead register-only host instructions in a variant (design §3.5). WP21.

Region scope. On the variant's *current* bytes (after CP and BF), a
straight-line host instruction is nopped (template ``dho_nop``) when

* it lies in an ``insn`` block (never the prefix, a coverage LOG_REG block, an
  LDST block, an exit stub, the pad or the pool) and is reachable under the
  facts (``hconst@1``);
* it has **no memory operand at all** (register-only ALU, ``test``/``cmp``,
  ``setcc``, ``cmovcc``, ``mov reg,reg|imm``; never an env load: only CP removes
  those) and is on the register-only allow-list (nothing that can fault);
* every register and flag it writes is dead after it (``hlive@1`` on the patched
  variant, full graph incl. slow paths).

Iterated to a fixpoint inside one run (removing a dead ``test`` can make the
``xor`` before it dead). An instruction whose register results are dead but
whose flags are live is counted as ``flags_live`` (§3.8; T-B3-41). Profitability:
nopped instructions × C' entries > 0.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import Any, ClassVar

from ..analysis.hconst import decode_one
from ..analysis.hostops import OpKind
from ..analysis.profitability import simplification_saving
from .base import Outcome, PassDescriptor, PassResult, Patch
from ._b3util import FLAG_SET, NO_MEM_DHO, has_mem_operand, nops, refusal_result
from .constant_propagation import region_descriptor

__all__ = ["DeadHostOps", "PASSES"]

_KINDS = (OpKind.FLAGS, OpKind.CONST, OpKind.COPY, OpKind.OPAQUE)


class DeadHostOps:
    descriptor: ClassVar[PassDescriptor] = region_descriptor(
        "dead-host-ops", (("hostops", 1), ("hcfg", 1), ("hconst", 1), ("hlive", 1)))

    def run(self, vv: Any, ctx: Any, edits: Any) -> PassResult:
        from ..analysis.api import ScopeRef
        from ..manager_b3 import VariantView

        for a in ("hostops", "hcfg", "hconst", "hlive"):
            ctx.analysis(a, ScopeRef("unit", vv.src.uid))
        cap = int(ctx.params.get("max_patches", 64))
        code = bytearray(vv.code)
        out: list[Patch] = []
        flags_live: set[int] = set()
        cur = vv
        for _round in range(16):
            h, g, lv, hc = cur.analyses()
            if not hc.ok or lv is None:
                if not out:
                    return PassResult(Outcome.SKIPPED_UNKNOWN, "validation_inconclusive",
                                      f"variant analyses not available: {hc.reasons}")
                break
            new_here = []
            for i, o in enumerate(h.ops):
                if o.block != "insn" or o.log is not None or o.kind not in _KINDS or \
                        i not in hc.reachable:
                    continue  # never the prefix nor a coverage LOG_REG block (close-out)
                if any(p.host_off == o.off for p in out):
                    continue
                ci = decode_one(code, o.off, o.size)
                if ci is None or ci.mnemonic not in NO_MEM_DHO or has_mem_operand(ci):
                    continue
                writes = lv.writes[i] if lv.writes else lv.defs[i]
                if not writes:
                    continue
                live = writes & lv.live_out[i]
                if live:
                    if live <= FLAG_SET and (writes - FLAG_SET).isdisjoint(lv.live_out[i]):
                        flags_live.add(o.off)
                    continue
                flags_live.discard(o.off)
                old = bytes(vv.code[o.off:o.end])
                new_here.append(Patch(vv.variant_id, o.off, old, nops(len(old)), "dho_nop",
                                      f"dead after: {ci.mnemonic} {ci.op_str}"))
            if not new_here:
                break
            for p in new_here:
                code[p.host_off:p.host_off + len(p.new_bytes)] = p.new_bytes
            out += new_here
            plan = replace(vv.plan, code=bytes(code))
            cur = VariantView(vv.variant_id, plan, vv.src, vv.layout, vv.code_address)
        refused: Counter[str] = Counter()
        out = sorted(out, key=lambda p: p.host_off)[:cap]
        if out:
            _s, ok = simplification_saving(len(out), vv.weight)
            if not ok:
                refused["not_profitable"] += 1
                out = []
        if not out and flags_live:
            refused["flags_live"] += len(flags_live)
        deps = tuple(f"fact:{f.fact_id}" for f in vv.factrefs) + (f"variant:{vv.variant_id}",)
        for p in out:
            edits.pure_rewrite(p, dependency_additions=deps)
        stats = {"nopped": len(out), "removed_work": len(out) * vv.weight}
        if out:
            return PassResult(Outcome.CANDIDATE, "", f"{len(out)} dead host ops",
                              affected=(vv.src.uid,), stats=stats)
        return refusal_result(refused, nothing="no_dead_host_op", stats=stats)


PASSES = (DeadHostOps,)

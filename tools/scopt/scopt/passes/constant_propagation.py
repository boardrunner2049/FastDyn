"""``constant-propagation@1``: fold env loads of fact fields in a variant (design §3.5). WP21.

Region scope: runs once per variant ``C'`` of the overlay. For every
reachable ``mov r32, dword [rbp+d]`` in an ``insn`` block whose field ``hconst@1``
knows under the variant's facts (a *foldable load*):

* value 0 -> ``xor r32, r32`` + nops (template ``cp_xor_zero``), only when the
  EFLAGS are dead after the load (``hlive@1``; the ``xor`` writes them);
* zero with live flags, or any nonzero value -> ``mov r32, imm32`` + nops (``cp_mov_imm``) when it fits in the
  load's bytes (5/6 bytes: only ``[rbp+disp32]`` loads, e.g. NF/ZF/CF/VF),
  else ``no_same_length_encoding`` (§14: non-zero constants of r0..r12 with a
  disp8 load).

Same length, inside the load's own bytes, so search rows and offsets are
unchanged (§3.4). The load is the only env load CP touches, and it is never
touched by BF/DHO (disjoint effects, [m-SP13]). Profitability (§10.3): folded
loads × C' entries > 0, else ``not_profitable``.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, ClassVar

from ..analysis.hconst import decode_one
from ..analysis.profitability import simplification_saving
from .base import Budgets, EditKind, Outcome, PassDescriptor, PassResult, Patch
from ._b3util import FLAG_SET, mov_imm32, nops, refusal_result, xor_zero

__all__ = ["ConstantPropagation", "PASSES", "region_descriptor"]


def region_descriptor(name: str, requires: tuple[tuple[str, int], ...]) -> PassDescriptor:
    params = {"max_patches": int}
    if name in ("constant-propagation", "branch-folding"):
        params["record_encoding_sites"] = bool
    return PassDescriptor(
        name=name, version=1, unit_scope="region", requires=requires,
        capabilities=frozenset({EditKind.PURE_REWRITE}),
        budgets=Budgets(max_wall_s=120.0),
        preserves=frozenset({"exit-facts", "profile.counts"}),
        invalidates=frozenset({"code"}),
        params_schema=params)


class ConstantPropagation:
    descriptor: ClassVar[PassDescriptor] = region_descriptor(
        "constant-propagation", (("hconst", 1), ("hostops", 1)))

    def run(self, vv: Any, ctx: Any, edits: Any) -> PassResult:
        from ..analysis.api import ScopeRef

        ctx.analysis("hostops", ScopeRef("unit", vv.src.uid))
        ctx.analysis("hconst", ScopeRef("unit", vv.src.uid))
        h, g, lv, hc = vv.analyses()
        if not hc.ok or lv is None:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "validation_inconclusive",
                              f"variant analyses not available: {hc.reasons}")
        cap = int(ctx.params.get("max_patches", 64))
        refused: Counter[str] = Counter()
        refusal_sites = {}
        out = []
        for i, (fld, val, reg) in sorted(hc.foldable.items()):
            o = h.ops[i]
            if o.block != "insn" or o.log is not None:
                continue  # never the coverage LOG_REG block's regs[15] load (close-out)
            old = bytes(vv.code[o.off:o.end])
            ci = decode_one(vv.code, o.off, o.size)
            if ci is None or ci.mnemonic != "mov":
                continue
            if val == 0 and not (lv.live_out[i] & FLAG_SET):
                enc, tmpl = xor_zero(reg), "cp_xor_zero"
            else:
                enc, tmpl = mov_imm32(reg, val), "cp_mov_imm"
            if enc is None or len(enc) > len(old):
                refused["no_same_length_encoding"] += 1
                if ctx.params.get("record_encoding_sites"):
                    pred, slot = vv.plan.pred
                    refusal_sites[f"encoding_site|{pred}|{slot}|{vv.src.uid}|{o.off}|{old.hex()}"] = 1
                continue
            new = enc + nops(len(old) - len(enc))
            out.append(Patch(vv.variant_id, o.off, old, new, tmpl,
                             f"{fld} == {val:#x} at C' entry ({reg})"))
        if len(out) > cap:
            refused["slot_budget"] += len(out) - cap
            out = out[:cap]
        if out:
            s, ok = simplification_saving(len(out), vv.weight)
            if not ok:
                refused["not_profitable"] += 1
                out = []
        deps = tuple(f"fact:{f.fact_id}" for f in vv.factrefs) + (f"variant:{vv.variant_id}",)
        for p in out:
            edits.pure_rewrite(p, dependency_additions=deps)
        stats = {"folded_loads": len(out), "removed_work": len(out) * vv.weight}
        stats.update(refusal_sites)
        if out:
            return PassResult(Outcome.CANDIDATE, "", f"{len(out)} loads folded",
                              affected=(vv.src.uid,), stats=stats)
        return refusal_result(refused, nothing="no_foldable_load", stats=stats)


PASSES = (ConstantPropagation,)

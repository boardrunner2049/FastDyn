"""Local idempotence and byte-exact CPU-state traffic in checked B3 variants."""
from __future__ import annotations

from dataclasses import replace

from capstone import x86 as X

from ..analysis.api import ScopeRef
from ..analysis.hconst import decode_one, fact_field_names
from ..analysis.native_integer import compare_outcome, pure_expression, result_facts
from ..analysis.obs_contract import D1_FIELDS
from ..analysis.hostops import OpKind, reg_family
from ._b3util import nops
from .base import Outcome, PassResult, Patch
from .constant_propagation import region_descriptor


def idempotent(ci, op):
    """Two adjacent identical encodings; all architectural outputs are live."""
    if ci is None or len(ci.operands) != 2 or any(ci.prefix):
        return False
    dst, src = ci.operands
    if dst.type != X.X86_OP_REG:
        return False
    family = reg_family(ci.reg_name(dst.reg))
    if family is None or family[0] in ("rbp", "rsp") or dst.size not in (4, 8):
        return False
    if src.type == X.X86_OP_MEM:
        return (ci.mnemonic == "mov" and op.kind == OpKind.ENV_LOAD and
                len(op.env) == 1 and op.env[0].exact and op.env[0].field in D1_FIELDS
                and src.mem.base == X.X86_REG_RBP
                and not src.mem.index and not src.mem.segment)
    if src.type not in (X.X86_OP_IMM, X.X86_OP_REG):
        return False
    if ci.mnemonic in ("cmp", "test", "mov", "and", "or"):
        return True
    if ci.mnemonic in ("movzx", "movsx", "movsxd") and src.type == X.X86_OP_REG:
        other = reg_family(ci.reg_name(src.reg))
        return other is not None and family[0] != other[0]
    return False


def _same_family(ci, reg):
    if ci.operands[0].type != X.X86_OP_REG:
        return False
    family = reg_family(ci.reg_name(ci.operands[0].reg))
    return family is not None and family[0] == reg


def _adjacent_reason(first, last, op, live_flags):
    if first is None or last is None:
        return None
    if first.bytes == last.bytes and idempotent(last, op):
        if last.mnemonic in ("test", "and", "or") and "AF" in live_flags:
            return None
        return "scalar_repeat_nop"
    if len(last.operands) != 2 or len(first.operands) != 2:
        return None
    dst, arg = last.operands
    if dst.type != X.X86_OP_REG or dst.size not in (4, 8):
        return None
    family = reg_family(last.reg_name(dst.reg))
    if family is None or family[0] in ("rsp", "rbp") or not _same_family(first, family[0]):
        return None
    if any(last.prefix) or any(first.prefix) or first.operands[0].size != dst.size:
        return None
    if (first.mnemonic == "and" and last.mnemonic == "test" and
            arg.type == X.X86_OP_REG and arg.reg == dst.reg and "AF" not in live_flags):
        return "scalar_test_nop"
    facts = result_facts(first)
    if (facts is not None and arg.type == X.X86_OP_IMM
            and not op.flags_written.intersection(live_flags)):
        if last.mnemonic == "and" and (~arg.imm & facts.mask) & ~facts.zero == 0:
            return "scalar_mask_nop"
        if last.mnemonic == "or" and (arg.imm & facts.mask) & ~facts.one == 0:
            return "scalar_mask_nop"
    return None


def _forwardable_load(region, graph, ops, index, code):
    """Reuse the same load into the same GPR through a bounded, sole-predecessor chain."""
    dest = ops[index]
    if dest.kind != OpKind.ENV_LOAD or not idempotent(decode_one(code, dest.off, dest.size), dest):
        return False
    lane = dest.env[0]
    for j in range(index - 1, max(-1, index - 13), -1):
        prev = ops[j]
        if (graph.predecessors(j + 1, atomic=False) != (j,) or
                prev.end != ops[j + 1].off or prev.insn != dest.insn or prev.block != "insn"
                or not region.allows(prev.off, prev.end)):
            break
        if prev.kind == OpKind.ENV_LOAD and code[prev.off:prev.end] == code[dest.off:dest.end]:
            return True
        if (prev.kind not in (OpKind.CONST, OpKind.COPY, OpKind.FLAGS, OpKind.NOP,
                              OpKind.ENV_LOAD, OpKind.ENV_STORE_REG, OpKind.ENV_STORE_IMM)
                or (prev.regs_written | prev.regs_partial) & (dest.regs_written | {"rbp"})
                or any("w" in e.rw and (not e.exact or
                       e.disp < lane.disp + lane.width and lane.disp < e.disp + e.width)
                       for e in prev.env)):
            break
    return False


def _available_expression(region, graph, ops, index, code, ins):
    """An earlier evaluation dominates and all input and output GPR versions still match."""
    dest = ops[index]
    if not pure_expression(ins) or dest.flags_written or dest.flags_read:
        return False
    if (not dest.regs_written or dest.regs_partial or dest.regs_read & dest.regs_written
            or dest.regs_written & {"rsp", "rbp"}):
        return False
    protected = dest.regs_read | dest.regs_written
    for j in range(index - 1, max(-1, index - 13), -1):
        old = ops[j]
        if (graph.predecessors(j + 1, atomic=False) != (j,) or old.end != ops[j + 1].off
                or old.insn != dest.insn or old.block != "insn" or not region.allows(old.off, old.end)):
            break
        if code[old.off:old.end] == code[dest.off:dest.end]:
            return True
        if ((old.regs_written | old.regs_partial) & protected or
                old.kind not in (OpKind.NOP, OpKind.COPY, OpKind.CONST, OpKind.FLAGS)):
            break
    return False


def _bounded_branch(code, ops, graph, region, i, compare, branch):
    from .branch_folding import fold_jcc

    if (i < 2 or branch is None or compare is None or compare.mnemonic != "cmp"
            or any(compare.prefix) or len(compare.operands) != 2
            or compare.operands[1].type != X.X86_OP_IMM or ops[i].kind != OpKind.JCC
            or graph.predecessors(i - 1, atomic=False) != (i - 2,)):
        return None
    producer, test = ops[i - 2], ops[i - 1]
    if (producer.end != test.off or producer.insn != test.insn or producer.block != "insn"
            or not region.allows(producer.off, test.end)):
        return None
    first = decode_one(code, producer.off, producer.size)
    facts = result_facts(first)
    # Exact register identity includes width. A cmp on the AL alias must never
    # consume the range of EAX, and a 32-bit compare interprets a signed imm8
    # only after x86 has extended it to the operand width.
    if (facts is None or compare.operands[0].type != X.X86_OP_REG
            or first.operands[0].reg != compare.operands[0].reg):
        return None
    taken = compare_outcome(facts, branch.mnemonic, compare.operands[1].imm)
    return None if taken is None else fold_jcc(bytes(branch.bytes), taken)


def scalar_patches(target, code, ops, graph, live, region, cap):
    if not graph.ok or live is None or region.status != "ok":
        return
    count = 0
    for i, op in enumerate(ops):
        if i == 0 or count >= cap:
            continue
        prev = ops[i - 1]
        if (graph.predecessors(i, atomic=False) != (i - 1,) or prev.end != op.off
                or prev.insn != op.insn or prev.block != "insn" or op.block != "insn"
                or not region.allows(prev.off, op.end)):
            continue
        first = decode_one(code, prev.off, prev.size)
        last = decode_one(code, op.off, op.size)
        reason = _adjacent_reason(first, last, op, live.live_out[i])
        if reason is None and _forwardable_load(region, graph, ops, i, code):
            reason = "scalar_forward_nop"
        if reason is None and _available_expression(region, graph, ops, i, code, last):
            reason = "scalar_expression_nop"
        raw = bytes(code[op.off:op.end])
        new = nops(len(raw)) if reason else None
        if reason is None:
            fold = _bounded_branch(code, ops, graph, region, i, first, last)
            if fold:
                new, reason = fold[0], "scalar_range_branch"
        if reason:
            yield Patch(target, op.off, raw, new, reason, reason)
            count += 1


def scalar_fixed_point(target, code, analyze, region, max_patches, max_rounds, budget):
    """Rebuild machine analyses after each same-size rewrite, without editing the source."""
    patches = []
    rounds = 0
    stop = "scalar_patch_budget" if max_patches <= max_rounds else "scalar_round_budget"
    while rounds < max_rounds and len(patches) < max_patches:
        budget()
        h, graph, live = analyze(code)
        rounds += 1
        if not graph.ok or live is None or region.status != "ok":
            stop = "scalar_analysis_unavailable"
            break
        # Choose one patch so a later proof always sees the effects of earlier edits.
        candidate = next((patch for patch in scalar_patches(
            target, code, h.ops, graph, live, region, len(h.ops))
            if all(patch.host_off + len(patch.old_bytes) <= previous.host_off or
                   previous.host_off + len(previous.old_bytes) <= patch.host_off
                   for previous in patches)), None)
        if candidate is None:
            stop = "scalar_fixed_point"
            break
        a, b = candidate.host_off, candidate.host_off + len(candidate.old_bytes)
        code = code[:a] + candidate.new_bytes + code[b:]
        patches.append(candidate)
    return patches, rounds, stop


class NativeScalarCleanup:
    descriptor = replace(region_descriptor("native-scalar-cleanup", (("native-region", 1),)),
                         params_schema={"max_patches": int, "max_rounds": int})

    def run(self, vv, ctx, edits):
        max_patches = ctx.params.get("max_patches", 64)
        max_rounds = ctx.params.get("max_rounds", max_patches)
        if min(max_patches, max_rounds) < 0:
            return PassResult(Outcome.FAILED, "invalid_params", "scalar budgets must be nonnegative")
        region = ctx.analysis("native-region", ScopeRef("unit", vv.src.uid)).value

        def analyze(code):
            current = vv if code == vv.code else replace(vv, plan=replace(vv.plan, code=code), _cache={})
            return current.analyses()[:3]

        out, rounds, stop = scalar_fixed_point(vv.variant_id, vv.code, analyze, region,
                                                max_patches, max_rounds, ctx.budget)
        for patch in out:
            edits.pure_rewrite(patch)
        reason = "no_redundant_operation" if not out and stop == "scalar_fixed_point" else stop
        return PassResult(Outcome.CANDIDATE if out else Outcome.NO_CHANGE, reason,
                          stats={"deleted": len(out), "analysis_rounds": rounds,
                                 "rewrite_rounds": len(out), f"stop:{stop}": 1})


class OrdinaryScalarCleanup:
    descriptor = replace(NativeScalarCleanup.descriptor, name="local-scalar-cleanup",
                         unit_scope="snapshot", requires=(("native-region", 1), ("hlive", 1),
                                                          ("hcfg", 1)),
                         params_schema={"max_patches": int})

    def run(self, snap, ctx, edits):
        count = 0
        for uid in snap.unit_ids():
            ctx.budget()
            scope = ScopeRef("unit", uid)
            region = ctx.analysis("native-region", scope).value
            if region.status != "ok":
                continue
            graph = ctx.analysis("hcfg", scope).value
            live = ctx.analysis("hlive", scope).value
            for p in scalar_patches(uid, region.code, region.operations, graph, live, region,
                                    ctx.params.get("max_patches", 64)):
                edits.pure_rewrite(p)
                count += 1
        return PassResult(Outcome.CANDIDATE if count else Outcome.NO_CHANGE,
                          "" if count else "no_redundant_operation", stats={"deleted": count})


class RegionStateForward:
    descriptor = region_descriptor("region-state-forward", (("native-region", 1), ("hconst", 1)))

    def run(self, vv, ctx, edits):
        region = ctx.analysis("native-region", ScopeRef("unit", vv.src.uid)).value
        ctx.analysis("hconst", ScopeRef("unit", vv.src.uid))
        h, graph, _, hc = vv.analyses()
        if not hc.ok or not graph.ok or graph.succ_full != graph.succ_atomic:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "state_path_unresolved",
                              "an alternative host path requires a separate value proof")
        count = 0
        fields = {fact_field_names(f): f for f, _ in hc.facts}
        for i, op in enumerate(h.ops):
            if (not region.allows(op.off, op.end) or count >= ctx.params.get("max_patches", 64)
                    or op.block != "insn" or op.log is not None or op.sites
                    or op.kind != OpKind.ENV_STORE_IMM or op.imm is None
                    or len(op.env) != 1 or not op.env[0].exact or op.env[0].width != 4):
                continue
            field = fields.get(op.store_field)
            if field is None or hc.env_at(i).get(field) != op.imm & 0xffffffff:
                continue
            raw = vv.code[op.off:op.end]
            ci = decode_one(raw, 0, len(raw))
            if ci is None or ci.mnemonic != "mov" or any(ci.prefix):
                continue
            edits.pure_rewrite(Patch(vv.variant_id, op.off, raw, nops(len(raw)), "region_same_store",
                                     f"verified {field} already contains {op.imm & 0xffffffff:#x}"),
                               dependency_additions=tuple(f"fact:{f.fact_id}" for f in vv.factrefs))
            count += 1
        return PassResult(Outcome.CANDIDATE if count else Outcome.NO_CHANGE,
                          "" if count else "no_redundant_store", stats={"deleted": count})


PASSES = (NativeScalarCleanup, OrdinaryScalarCleanup, RegionStateForward)

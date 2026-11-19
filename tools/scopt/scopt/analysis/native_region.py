"""Source-bound host region and barriers for length-preserving variant rewrites."""
from dataclasses import dataclass

from .api import AnalysisSpec, ScopeRef
from .hconst import decode_one
from .native_integer import modeled_scalar
from .hostops import HostOp, OpKind
from ..model.unit import Fixup, Insn, RecoveryPoint


@dataclass(frozen=True)
class NativeRegion:
    source_hash: str
    code: bytes
    contract_id: str
    origins: tuple[Insn, ...]
    recovery: tuple[RecoveryPoint, ...]
    references: tuple[Fixup, ...]
    operations: tuple[HostOp, ...]
    successors: tuple[tuple[int, ...], ...]
    exits: tuple[tuple[int, str], ...]
    frozen_offsets: frozenset[int]
    status: str

    def allows(self, start, end):
        return self.status == "ok" and not any(start <= p < end for p in self.frozen_offsets)


def _compute(ctx, scope):
    unit = ctx.unit(scope.id)
    host = ctx.analysis("hostops", scope).value
    graph = ctx.analysis("hcfg", scope).value
    obs = ctx.analysis("obs-contract", ScopeRef("snapshot", ctx.snapshot_id)).value
    barriers = {p.host_return_offset for p in unit.recovery}
    for ref in unit.reloc.fixups:
        barriers.update(range(ref.field_off, ref.field_off + ref.width))
    if unit.splice_applied:
        from ..splice_layout import splice_group_offsets

        offsets = splice_group_offsets(unit.native_fragments)
        for call in unit.native_fragments:
            start = offsets(call.splice.start_off)
            barriers.update(range(start, start + len(call.splice.code)))
    for op in host.ops:
        if (op.sites or op.log is not None or op.block != "insn" or op.kind in (
                OpKind.CALL, OpKind.GUEST_ACCESS, OpKind.FRAME_STORE) or
                op.kind == OpKind.OPAQUE and not modeled_scalar(
                    decode_one(unit.code, op.off, op.size))):
            barriers.add(op.off)
    return NativeRegion(unit.content_hash, bytes(unit.code), obs.contract_id, unit.insns,
                        unit.recovery, unit.reloc.fixups, host.ops, graph.succ_full,
                        tuple(sorted(graph.exits.items())), frozenset(barriers),
                        "ok" if host.ok and graph.ok else "unmodelled_region")


ANALYSES = (AnalysisSpec("native-region", 1, "unit",
                         (("hostops", 1), ("hcfg", 1), ("obs-contract", 1)),
                         frozenset({"code", "meta"}), _compute, result_schema=NativeRegion),)

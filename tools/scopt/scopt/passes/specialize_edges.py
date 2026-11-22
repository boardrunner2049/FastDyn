"""Globally plan ordinary edge specializations and emit serial partitions."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace

from ..analysis.api import ScopeRef
from ..analysis.global_values import EnvTransfer, FlowArc, bind_effect_input, field_bits, solve_program
from ..analysis.hconst import FACT_FIELDS, fact_field_names
from ..analysis.store import value_digest
from .base import Outcome, PassResult
from .specialize_returns import (PlannedSpecialization, SpecializeReturns,
                                 emit_specialization, growth_budget)


@dataclass(frozen=True, slots=True)
class Partition:
    index: int
    entries: tuple[PlannedSpecialization, ...]


@dataclass(frozen=True, slots=True)
class GlobalPlan:
    snapshot_id: str
    generation: int
    program_id: str
    summary_ids: tuple[tuple[str, str], ...]
    coverage_mode: str
    coverage_id: str | None
    growth_budget: int | None
    partitions: tuple[Partition, ...]

    @property
    def decision_id(self) -> str:
        """Content identity of global decisions independent of partition count."""
        decisions = tuple(sorted((row for p in self.partitions for row in p.entries),
                                 key=lambda r: (r.source, r.slot, r.dest)))
        return value_digest((self.snapshot_id, self.generation, self.program_id,
                             self.summary_ids, self.coverage_mode, self.coverage_id,
                             self.growth_budget, decisions))


def partition_plan(entries: tuple[PlannedSpecialization, ...], count: int) -> tuple[Partition, ...]:
    """Assign all edits for a predecessor to a single worker."""
    owner = {source: i % count for i, source in enumerate(sorted({e.source for e in entries}))}
    groups: list[list[PlannedSpecialization]] = [[] for _ in range(count)]
    for entry in entries:
        groups[owner[entry.source]].append(entry)
    return tuple(Partition(i, tuple(group)) for i, group in enumerate(groups))


class SpecializeEdges(SpecializeReturns):
    """Use independently certified predecessor-local facts on off-page static exits."""

    exit_class = "static"
    exit_stat = "static_exits"
    no_exit_reason = "no_static_exit"
    descriptor = replace(
        SpecializeReturns.descriptor, name="specialize-edges",
        requires=SpecializeReturns.descriptor.requires + (("program", 1), ("local-summary", 1),
                                                          ("obs-contract", 1)),
        params_schema={**SpecializeReturns.descriptor.params_schema, "partitions": int,
                       "max_preserving_hops": int, "coverage_mode": str, "coverage_evidence": str},
    )

    def _select_target(self, row, candidate, unit):
        # Profiles can identify a profitable member of the target set. They
        # cannot override the capture's static PC or the candidate's full key.
        return row.static_dest == candidate.vaddr and candidate.vaddr in unit.vaddrs

    def run(self, view, ctx, edits):
        count = ctx.params.get("partitions", 1)
        if not 1 <= count <= 64:
            return PassResult(Outcome.FAILED, "invalid_params", "partitions must be in [1, 64]")
        from ..formats.translation_coverage import CoverageError, select_coverage

        mode = ctx.params.get("coverage_mode", "auto")
        if mode not in ("partial", "complete", "auto"):
            return PassResult(Outcome.FAILED, "invalid_params", "invalid coverage mode")
        try:
            self.coverage = select_coverage(view.working_set.base, mode,
                                            ctx.params.get("coverage_evidence") or None)
        except CoverageError as error:
            return PassResult(Outcome.FAILED, "coverage_error", str(error))
        if ctx.b3 is not None:
            bind_effect_input(ctx.store, ctx.b3.layout)
        return super().run(view, ctx, edits)

    def _emit(self, kept, view, ctx, edits):
        selection = self.coverage
        scope = ScopeRef("snapshot", ctx.snapshot_id)
        program_result = ctx.analysis("program", scope)
        observation_result = ctx.analysis("obs-contract", scope)
        program = program_result.value
        summary_results = {}
        for unit in program.units:
            ctx.budget()
            summary_results[unit.uid] = ctx.analysis("local-summary", ScopeRef("unit", unit.uid))
        covered = frozenset(selection.covered_units) if selection.mode == "complete" else frozenset()
        roots = frozenset(selection.roots)
        reachable = program.reachable(roots, covered)
        # Every general entry is public in partial mode. Key checks alone do
        # not admit an environment value into any general entry.
        unit_ids = {u.uid: u for u in program.units}
        entries = []
        refusals = []
        chosen_sources = set()
        chosen_dests = set()
        for choice in kept:
            spec = PlannedSpecialization.from_choice(choice)
            if (unit_ids[spec.source].content_hash != spec.source_hash
                    or unit_ids[spec.dest].content_hash != spec.dest_hash
                    or spec.dest in chosen_sources or spec.source in chosen_dests
                    or spec.source in view.b3.unit_code or spec.dest in view.b3.unit_code):
                refusals.append((spec.source, spec.exit_id, "changed_or_composed_unit"))
                continue
            entries.append(spec)
            chosen_sources.add(spec.source)
            chosen_dests.add(spec.dest)
        # _emit runs after the existing shared budget fit. Mutate the local
        # selected list to ensure counters describe only emitted proposals.
        emitted = {(e.source, e.exit_id, e.dest) for e in entries}
        kept[:] = [r for r in kept if (r["row"].uid, r["row"].exit_id, r["cu"].uid) in emitted]
        plan = GlobalPlan(
            ctx.snapshot_id, view.generation, program_result.result_hash,
            tuple((uid, result.result_hash) for uid, result in sorted(summary_results.items()))
            + (("observation-contract", observation_result.result_hash),),
            selection.mode, selection.coverage_id, growth_budget(ctx.params, ctx.b3, ctx.b3.units()),
            partition_plan(tuple(entries), ctx.params.get("partitions", 1)),
        )
        layout = ctx.b3.layout
        private = []
        for entry in entries:
            facts = {}
            for fact in entry.facts:
                off = layout.offset(fact_field_names(fact.field))
                facts.update((off + i, b) for i, b in enumerate(fact.value.to_bytes(4, "little")))
            context_id = f"private:{entry.source}:{entry.slot}:{entry.dest}"
            private.append(FlowArc(entry.source, context_id,
                                   EnvTransfer(frozenset(), tuple(sorted(facts.items())))))
        values = solve_program(program, {uid: result.value for uid, result in summary_results.items()},
                               covered=covered, roots=roots, private=tuple(private))
        private_ids = {arc.target for arc in private}
        dep = (f"global-plan:{plan.decision_id}",)
        for partition in plan.partitions:
            for entry in partition.entries:
                ctx.budget()
                emit_specialization(entry, ctx.b3.offsets, edits, dep)
        info = asdict(plan)
        info["decision_id"] = plan.decision_id
        info["coverage_reason"] = selection.reason
        info["complete_unreachable"] = sorted(covered - reachable)
        info["unknown_exits"] = sum(e.dynamic for e in program.edges)
        info["global_value_entries"] = {uid: value for uid, value in values.entries
                                        if value and uid not in private_ids}
        info["private_entry_values"] = {uid: value for uid, value in values.entries
                                        if value and uid in private_ids}
        field_offsets = {f: layout.offset(fact_field_names(f)) for f in FACT_FIELDS
                         if layout.has_field(fact_field_names(f))}
        partial_bits = []
        for uid, byte_bits in values.known_bits:
            if not byte_bits:
                continue
            for field, off in field_offsets.items():
                bits = field_bits(byte_bits, off, 4)
                if 0 < bits.mask < 0xFFFFFFFF:
                    partial_bits.append({"entry": uid, "field": field, "known_mask": hex(bits.mask),
                                         "known_ones": hex(bits.value),
                                         "unsigned_bounds": bits.unsigned_bounds,
                                         "signed_bounds": bits.signed_bounds})
        info["advisory_entry_bits"] = partial_bits
        info["private_edge_contexts"] = len(entries)
        info["guarded_value_contexts"] = sum(any(f.source == "guard" for f in e.facts) for e in entries)
        info["guarded_origins"] = [
            {"predecessor": d["row"].uid, "exit": d["row"].exit_id, "target": d["cu"].uid,
             **d["srow"]["x_guarded_origin"]}
            for d in kept if "x_guarded_origin" in d["srow"]
        ]
        info["value_budget_exhausted"] = values.exhausted
        info["refusals"] = refusals
        info["effect_tables"] = sorted({r.value.effect_id for r in summary_results.values()
                                        if r.value.effect_id is not None})
        return json.dumps(info, sort_keys=True)


PASSES = (SpecializeEdges,)

from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopt.analysis.global_values import EnvTransfer, FlowArc, UNKNOWN, byte_footprint, solve_values
from scopt.analysis.hostops import EnvAccess, HostOp, OpKind
from scopt.analysis.program import Program, ProgramEdge, ProgramUnit
from scopt.ids import TBKey
from scopt.passes.specialize_edges import GlobalPlan, partition_plan
from scopt.passes.specialize_returns import PlannedSpecialization


def const(value):
    return EnvTransfer(frozenset(), ((0, value),))


PRESERVE = EnvTransfer(frozenset({0}))


@pytest.mark.parametrize(("cold_value", "expected"), [(8, ((0, 8),)), (5, ())])
def test_global_meet_includes_cold_predecessor_and_converges_on_loop(cold_value, expected):
    entries = solve_values(
        ["A", "cold", "B", "C"], [
            FlowArc("A", "B", const(8)), FlowArc("cold", "B", const(cold_value)),
            FlowArc("B", "C", PRESERVE), FlowArc("C", "B", PRESERVE),
        ], ["A", "cold"],
    )
    assert not entries.exhausted
    assert dict(entries.entries)["C"] == expected


def test_public_general_entry_and_opaque_reentry_discard_facts():
    arcs = [FlowArc("A", "C", const(8)), FlowArc("opaque", "C", UNKNOWN)]
    assert dict(solve_values(["A", "C", "opaque"], arcs, ["A"]).entries)["C"] == ((0, 8),)
    assert dict(solve_values(["A", "C", "opaque"], arcs, ["A", "C"]).entries)["C"] == ()
    assert dict(solve_values(["A", "C", "opaque"], arcs, ["A", "opaque"]).entries)["C"] == ()
    assert solve_values(["A", "C"], arcs, ["A"], max_steps=0).exhausted


def test_byte_overlap_and_precise_helper_footprints():
    tracked = frozenset(range(8))
    store = HostOp(0, 1, "store", OpKind.ENV_STORE_REG, "insn", 0,
                   env=(EnvAccess("regs[0]", 2, 2, "w", False),))
    assert byte_footprint(store, None, tracked) == (frozenset(), {2, 3}, {2, 3}, False)
    call = replace(store, kind=OpKind.CALL, helper_name="bounded")
    resolved = SimpleNamespace(
        effect=SimpleNamespace(hostmem_writes=False), reads=lambda b: b == 0,
        writes=lambda b: b in (2, 3), rowed=True,
    )
    effects = SimpleNamespace(resolved=lambda *_: resolved)
    assert byte_footprint(call, effects, tracked) == ({0}, {2, 3}, frozenset(), False)
    resolved.effect.hostmem_writes = True
    assert byte_footprint(call, effects, tracked) == (tracked, tracked, frozenset(), True)


def test_unknown_edge_retains_uncertain_target_contexts():
    units = tuple(ProgramUnit(uid, uid, (0, None, 1, ctx, 0), (0,), False)
                  for ctx, uid in enumerate(("root", "cold", "other_key")))
    edge = ProgramEdge("root", "x0", "goto_tb", 0, None, 0, ("cold",), False, True)
    program = Program("snap", units, (edge,))
    covered = frozenset(u.uid for u in units)
    assert program.reachable(frozenset({"root"}), covered) == {"root", "cold"}
    unknown = replace(edge, dynamic=True, candidates=())
    assert replace(program, edges=(unknown,)).reachable(frozenset({"root"}), covered) == covered


def test_partition_assignment_preserves_decisions_and_accounts_for_dependencies():
    def entry(source, slot):
        return PlannedSpecialization(source, source, "x0", "static", 42, slot,
                                     "dest", "desthash", 0x1000,
                                     TBKey(0, None, 0, 0, 0), (), "summary")

    decisions = (entry("B", 0), entry("A", 1), entry("A", 0))
    serial = GlobalPlan("snapshot", 0, "program", (("A", "summaryA"),),
                        "partial", None, 400, partition_plan(decisions, 1))
    partitioned = replace(serial, partitions=partition_plan(decisions, 3))
    assert serial.decision_id == partitioned.decision_id
    assert len(partitioned.partitions[0].entries) == 2
    assert len(partitioned.partitions[1].entries) == 1
    assert replace(serial, summary_ids=(("A", "different"),)).decision_id != serial.decision_id
    assert replace(serial, program_id="changed").decision_id != serial.decision_id


def test_program_flow_uses_checked_edge_and_only_seeds_true_roots():
    from scopt.analysis.global_values import ExitTransfer, LocalSummary, solve_program

    ids = ("A", "cold", "B", "C")
    units = tuple(ProgramUnit(uid, uid, (0, None, 1, ctx, 0), (0,), False)
                  for ctx, uid in enumerate(ids))
    edges = tuple(ProgramEdge(src, "x0", "goto_tb", 0, None, 0, (dst,), False, False)
                  for src, dst in (("A", "B"), ("cold", "B"), ("B", "C")))
    summaries = {
        uid: LocalSummary(uid, None, "ok", (ExitTransfer(0, "goto_tb:0", transfer, frozenset()),))
        for uid, transfer in (("A", const(7)), ("cold", const(7)), ("B", PRESERVE))
    }
    program = Program("snap", units, edges)
    covered = frozenset(ids)
    roots = frozenset({"A", "cold"})
    assert dict(solve_program(program, summaries, covered=covered, roots=roots).entries)["C"] == ((0, 7),)
    unknown = ProgramEdge("cold", "x1", "goto_ptr", None, 3, None, (), True, True)
    program = replace(program, edges=(*edges, unknown))
    private = (FlowArc("B", "private-C", const(7)),)
    facts = dict(solve_program(program, summaries, covered=covered, roots=roots, private=private).entries)
    assert facts["C"] == () and facts["private-C"] == ((0, 7),)
    partial = dict(solve_program(program, summaries, covered=frozenset(), roots=frozenset()).entries)
    assert partial["C"] == ()


def test_changed_file_backed_helper_effect_invalidates_cached_summary(tmp_path, monkeypatch):
    from scopt.analysis.api import AnalysisSpec, ScopeRef
    from scopt.analysis.global_values import EFFECT_INPUT, bind_effect_input
    from scopt.analysis.helper_effects import (ALL, NONE, HelperEffect, HelperEffectTable,
                                                helper_effects_text)
    from scopt.analysis.registry import AnalysisRegistry
    from scopt.analysis.store import AnalysisStore
    from tests.synth.fake_snapshot import FakeSnapshot

    layout = SimpleNamespace(exe_sha256="sha256:" + "a" * 64, envlayout_id="sha256:" + "b" * 64)
    row = HelperEffect("helper_a", 0, NONE, NONE, False, False, False, True, ("test.c",))
    empty = HelperEffectTable.build(layout.exe_sha256, [row])
    writer = HelperEffectTable.build(layout.exe_sha256, [replace(row, env_writes=ALL)])
    effect_file = tmp_path / ("a" * 64 + ".json")
    monkeypatch.setenv("SCOPT_HELPER_EFFECTS_PATH", str(tmp_path))
    spec = AnalysisSpec("test-effect", 1, "snapshot", (), frozenset({f"external:{EFFECT_INPUT}"}),
                        lambda ctx, _s: ctx.external_input(EFFECT_INPUT).value.table_id, str)
    snap = FakeSnapshot([], "sha256:" + "b" * 64)
    store = AnalysisStore(snap, registry=AnalysisRegistry([spec]))
    scope = ScopeRef("snapshot", snap.snapshot_id)
    effect_file.write_text(helper_effects_text(empty))
    bind_effect_input(store, layout)
    before = store.get("test-effect", scope)
    assert before.value == empty.table_id and store.is_current(before)
    effect_file.write_text(helper_effects_text(writer))
    bind_effect_input(store, layout)
    assert not store.is_current(before)
    after = store.get("test-effect", scope)
    assert after.value == writer.table_id and after.result_hash != before.result_hash


def test_value_guard_fact_requires_actual_zero_compare_and_keeps_fallback():
    from scopt.guard import GuardError, GuardOffsets, GuardTarget, decode_guard, emit_guard
    from scopt.validate_sym.equiv import certify_fact
    from scopt.validate_sym.link_check import declared_atoms
    from scopt.validate_sym.link_mutants import drop_atom_patches, stub_atoms
    from scopt.validate_sym.mutants import MINI_LAYOUT
    from scopt.validate_sym.paths import explore
    from scopt.validate_sym.program import GSlot, HelperRef, Program, patched

    offsets = GuardOffsets.from_layout(MINI_LAYOUT)
    target = GuardTarget(0x8002000, 0, "SPEC", r0_zero=True)
    tail = bytes.fromhex("488bfdff1513000000ffe0")
    st = emit_guard(stub_off=0, tail=tail, pool_slot_off=0x200, lookup_helper_id=0,
                    lookup_func=0x555555550000, kf=0x10, kf2=0x400, targets=(target,),
                    tc_abs=0x70000000, struct_abs=0x6FFFFF00, tb_ret_abs=0x6FFFFE00,
                    offsets=offsets)
    slot, = st.slots
    reg = {0: (slot.field_off, slot.reset_off)}
    dg = decode_guard(st.code, 0, tc_abs=0x70000000, struct_off=-256,
                      offsets=offsets, registered=reg)
    assert ("g_r0_zero", offsets.r0, 0) in dg.atoms
    declared = declared_atoms(offsets, 0x10, 0x400, target.vaddr, r0_zero=True)
    program = Program("guard", st.code.ljust(0x208, b"\0"), 0x70000000, 0x6FFFFF00, 0x70000208,
                      0, 0x200, helpers={st.f_off + 3: HelperRef(0, "lookup_tb_ptr", 0)},
                      slots={slot.jmp_off: GSlot(0, "SPEC", target.vaddr, declared)},
                      stub=(0, len(st.code)))
    assert certify_fact(program, 0, (offsets.r0, 4), layout=MINI_LAYOUT).certified
    pre = {(a.off, a.width): a.value for a in declared}
    pre[(offsets.r0, 4)] = 7
    result = explore(program, pre, layout=MINI_LAYOUT)
    assert result.complete and not any(s.end[1] == "guarded_slot" for s in result.paths)

    atom = next(a for a in stub_atoms(st.code, 0) if a.atom == "g_r0_zero")
    broken = patched(program, drop_atom_patches(st.code, atom))
    assert not certify_fact(broken, 0, (offsets.r0, 4), layout=MINI_LAYOUT).certified
    with pytest.raises(GuardError):
        decode_guard(broken.code, 0, tc_abs=0x70000000, struct_off=-256,
                     offsets=offsets, registered=reg)


def test_known_bits_preserve_common_bits_and_signed_order_across_a_meet():
    from scopt.analysis.global_values import field_bits

    arcs = [
        FlowArc("A", "B", const(0x7F)),
        FlowArc("cold", "B", const(-1)),  # byte store truncates modulo 256
        FlowArc("B", "C", PRESERVE),
    ]
    result = solve_values(["A", "cold", "B", "C"], arcs, ["A", "cold"])
    assert dict(result.entries)["C"] == ()
    bits = field_bits(dict(result.known_bits)["C"], 0, 1)
    assert (bits.mask, bits.value) == (0x7F, 0x7F)
    assert bits.unsigned_bounds == (0x7F, 0xFF)
    assert bits.signed_bounds == (-1, 127)


def test_word_projection_wraps_bytes_and_retains_negative_bounds():
    from scopt.analysis.global_values import field_bits

    defs = EnvTransfer(frozenset(), ((0, 256), (1, -1), (2, 511), (3, -128)))
    result = solve_values(["A", "C"], [FlowArc("A", "C", defs)], ["A"])
    known = dict(result.known_bits)["C"]
    bits = field_bits(known, 0, 4)
    assert bits.mask == 0xFFFFFFFF and bits.value == 0x80FFFF00
    assert bits.unsigned_bounds == (0x80FFFF00, 0x80FFFF00)
    assert bits.signed_bounds == (-0x7F000100, -0x7F000100)

    # Killing the high two bytes loses sign information without losing
    # knowledge of the low two bytes.
    partial = EnvTransfer(frozenset({0, 1})).apply_bits(known)
    bits = field_bits(partial, 0, 4)
    assert bits.mask == 0xFFFF and bits.value == 0xFF00
    assert bits.signed_bounds == (-0x7FFF0100, 0x7FFFFF00)


def _guarded_chain(*, writer_value=0, killed=None, max_hops=3):
    from scopt.analysis.callee_summary import guarded_static_summaries
    from scopt.formats.callee_summary_v1 import seal_row
    from tests.unit.test_wp20_return_edges import _row

    ids = [f"tb:{i:032x}#0" for i in range(6)]
    rows, edges = [], []
    for index, uid in enumerate(ids):
        row = _row()
        row["deps"]["unit"]["uid"] = row["return_unit"] = uid
        if index == 0:
            row["facts"][0].update(status="certified", value=hex(writer_value),
                                   cert_hash="sha256:" + "1" * 64)
        else:
            row["facts"] = []
        rows.append(seal_row(row))
        dest = index + 1 if index + 1 < len(ids) else 1
        target = SimpleNamespace(dst=ids[dest], admitted=True, vaddr=4 * dest, count=100 - index)
        edges.append(SimpleNamespace(uid=uid, exit_id="x0", admitted=True, exit_class="static",
                                     insn=0, tail_off=16, candidates=(target,), static_dest=target.vaddr))

    def summary(uid):
        preserved = range(3) if uid == killed else range(4)
        return SimpleNamespace(result_hash="sha256:" + uid[3:35] * 2, value=SimpleNamespace(
            effect_id="table", preserved_at_tails=((16, frozenset(preserved)),)))

    return guarded_static_summaries(rows, SimpleNamespace(exits=edges), summary, offset=0, max_hops=max_hops)


def test_bounded_global_candidate_depends_on_every_preserver():
    rows = _guarded_chain()
    assert not _guarded_chain(max_hops=1)[2]["facts"]
    assert [bool(row["facts"]) for row in rows] == [True, True, True, True, False, False]
    proof = rows[2]["x_guarded_origin"]
    assert proof["writer_row"] == rows[1]["row_hash"]
    assert proof["writer_guarded_origin"]["writer_row"] == rows[0]["row_hash"]
    assert proof["writer_tb_edge_count"] == 99
    assert rows[2]["facts"][0]["status"] == "guarded"
    assert not any(row["facts"] for row in _guarded_chain(writer_value=1)[1:])
    assert not any(row["facts"] for row in _guarded_chain(killed=rows[1]["return_unit"])[1:])


def test_propagated_guarded_fact_changes_only_private_code(tmp_path):
    from scopt.formats.envlayout_v1 import find_envlayout
    from scopt.guard import GuardOffsets
    from scopt.manager_b3 import VariantPlan, VariantView
    from scopt.passes._b3util import factref_of_row
    from scopt.passes.constant_propagation import ConstantPropagation
    from scopt.passes.specialize_returns import emit_specialization
    from tests.synth.builders import simple_units
    from tests.unit.test_wp10_link import make_snapshot
    from tests.unit.test_wp21_b3_passes import run_region

    snap = make_snapshot(tmp_path, simple_units(1, body_prefix=bytes.fromhex("8b4500")))
    dest, = snap.units()
    layout = find_envlayout("sha256:56d8bfb32c91c970fe0a16237b962c02791ce0a7fcc52bf6246343fbef430d90")
    row = _guarded_chain()[2]  # writer -> preserver -> final preserver exit
    fact = factref_of_row(row, row["facts"][0])
    choice = PlannedSpecialization(row["return_unit"], row["deps"]["unit"]["content_hash"],
                                   "x0", "static", 16, 0, dest.uid, dest.content_hash,
                                   dest.vaddrs[0], dest.key, (fact,), row["row_hash"])
    entries = []
    def record_entry(entry, **kw):
        entries.append(entry)
    def ignore(*args, **kw):
        pass
    edits = SimpleNamespace(attach_fact=ignore, clone=ignore, redirect_exit=ignore,
                            add_entry=record_entry)
    emit_specialization(choice, GuardOffsets.from_layout(layout), edits)
    entry, = entries
    assert any(atom.atom == "g_r0_zero" for atom in entry.guard_atoms)
    plan = VariantPlan(entry.variant_id, dest.uid, entry.pred, "x0", entry.facts,
                       entry.guard_atoms, dest.code, weight=10)
    private = VariantView(entry.variant_id, plan, dest, layout, snap.header.code_address)
    _, (patch,) = run_region(ConstantPropagation, private)
    assert patch.host_off == dest.insn0_start and patch.target == entry.variant_id
    assert patch.old_bytes == bytes.fromhex("8b4500") and patch.new_bytes == bytes.fromhex("31c090")
    public = VariantView(entry.variant_id, replace(plan, facts=()), dest, layout, snap.header.code_address)
    assert run_region(ConstantPropagation, public)[1] == []

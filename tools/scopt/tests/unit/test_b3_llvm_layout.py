from dataclasses import replace
from types import SimpleNamespace

from scopt.guard import GuardOffsets

from scopt.formats.sc_records import ScRecord
from scopt.link import B3Plan, NativeLinker, NativeLinkerV2, TB_ALIGN, round_up
from scopt.model.snapshot import WorkingSet
from scopt.passes.base import Patch
from tests.synth.fake_snapshot import fake_snapshot


def test_v2_moves_even_non_grown_records_and_honors_pins():
    snap = fake_snapshot(4, no_meta={1})
    first, pinned, third, last = snap.units()
    # Same-length semantic edit. Even the untouched moved unit needs a new
    # record, while the metadata-pinned unit keeps the original record.
    edited = Patch(third.uid, 0, third.code[:1], b"\x90", "dho_nop")
    plan = B3Plan(rewrites={third.uid: (edited,)})
    layout = WorkingSet(snap, sections={"hot": (last.uid, first.uid, third.uid)})
    links = NativeLinkerV2(snap, plan, placement=layout, derive_captures=False).link()
    assert links.ok, links.failures
    assert [lu.uid for lu in links.units] == snap.uids()
    by_uid = links.by_uid()
    assert by_uid[last.uid].moved
    assert by_uid[third.uid].moved and by_uid[third.uid].body[0] == 0x90
    assert by_uid[pinned.uid].record == pinned.record
    assert by_uid[last.uid].new_tb_offset < by_uid[third.uid].new_tb_offset
    for lu in links.units:
        rec = ScRecord.from_bytes(lu.record)
        assert rec.checksum_ok and rec.tb_offset == lu.new_tb_offset


def test_effective_geometry_controls_order_and_checks_growth_in_place():
    snap = fake_snapshot(4, no_meta={1})
    first, pinned, third, last = snap.units()
    effective = {first.uid: replace(first, end_offset=first.end_offset + 2 * TB_ALIGN)}
    layout = WorkingSet(snap, sections={"hot": (first.uid, third.uid, last.uid)})
    linker = NativeLinker(layout, derive_captures=False)
    positions = linker.plan(effective=effective)
    assert positions[pinned.uid] == pinned.tb_offset
    assert positions[first.uid] >= round_up(pinned.end_offset, TB_ALIGN)
    assert positions[third.uid] >= positions[first.uid] + effective[first.uid].end_offset - first.tb_offset
    assert linker._check_plan(positions, effective) == []

    fixed = NativeLinker(WorkingSet(snap), derive_captures=False)
    failures = fixed._check_plan(fixed.plan(), effective)
    assert [(f.uid, f.reason) for f in failures] == [(first.uid, "place_overlap")]


def guarded_snapshot():
    from scopt.formats.enums import Encoding, SiteKind
    from scopt.guard import GuardTarget
    from scopt.link import GuardedExitSpec, VariantSpec
    from scopt.model.unit import HelperBinding, PoolEntry, Site
    from tests.synth.fake_snapshot import FakeSnapshot

    base = fake_snapshot(3)
    pred_a, pred_b, target = base.units()
    lookup = 0x2000

    def with_lookup(u):
        tail = u.tc_size
        pool = tail + 11
        raw = bytes.fromhex("488bfdff15") + (2).to_bytes(4, "little") + bytes.fromhex("ffe0")
        code = u.code + raw + lookup.to_bytes(8, "little")
        rec = ScRecord.from_bytes(u.record).replace(tc_size=len(code),
                                                    end_offset=u.tc_offset + len(code) + len(u.search))
        sites = (Site(tail + 5, tail + 3, 4, Encoding.POOL_INDIRECT, SiteKind.HELPER_CALL,
                      1, lookup, 0, 2),
                 Site(tail + 9, tail + 9, 0, Encoding.NONE, SiteKind.GOTO_PTR, 0, 0, 0, 0))
        unit = replace(u, record=rec.to_bytes(fix_checksum=True), code=code, tc_size=len(code),
                       end_offset=rec.end_offset, sites=(*u.sites, *sites), pool_start=pool,
                       pool=(PoolEntry(tail + 5, pool, 0, 1, 0, lookup),),
                       bindings=replace(u.bindings, helpers=(HelperBinding(
                           1, "lookup_tb_ptr", "qemu", "helper_lookup_tb_ptr", 0, lookup),)))
        spec = GuardedExitSpec(tail, "return", (GuardTarget(target.vaddrs[0], 0, "SPEC"),),
                               target.key.flags, target.key.cs_base)
        return unit, spec

    pred_a, a_spec = with_lookup(pred_a)
    pred_b, b_spec = with_lookup(pred_b)
    snap = FakeSnapshot((pred_a, pred_b, target), base.snapshot_id, base.header)
    layout = WorkingSet(snap, sections={"hot": (pred_b.uid, pred_a.uid, target.uid)})
    a_spec = replace(a_spec, targets=(*a_spec.targets, GuardTarget(pred_b.vaddrs[0], 1, "GENERAL")))
    plan = B3Plan(grow={pred_a.uid: (a_spec,), pred_b.uid: (b_spec,)},
                  variants=tuple(VariantSpec(target.uid, u.uid, 0, key=u.uid)
                                 for u in (pred_a, pred_b)), order_private_by_predecessor=True)
    snap.run_info = SimpleNamespace(code_address=snap.header.code_address, tb_ret_off=0)
    return snap, plan, layout


def test_page_layout_uses_final_grown_extents_and_unplaced_slot_order():
    from scopt.manager_b3 import B3State, ExitPlan, SlotPlan, materialize
    from scopt.passes.public_input import public_layout_geometry

    snap, plan, _ = guarded_snapshot()
    pred_a, pred_b, target = snap.units()
    exits = {}
    for uid, specs in plan.grow.items():
        spec, = specs
        slots = tuple(SlotPlan(t.slot_n, t.vaddr, "GENERAL", target.uid, None, "test")
                      for t in spec.targets)
        exits[uid, spec.tail_off] = ExitPlan(uid, "x0", spec.tail_off, spec.exit_class,
                                            spec.key_flags, spec.key_cs_base, slots)
    state = B3State(exits=exits)
    ctx = SimpleNamespace(b3_state=state, b3=SimpleNamespace(
        snapshot=snap, units=lambda: {u.uid: u for u in snap.units()}, offsets=GuardOffsets()))
    baseline = NativeLinkerV2(snap, materialize(state, ctx.b3).plan,
                              derive_captures=False).link()
    assert baseline.ok, baseline.failures
    effective, starts, private, grown = public_layout_geometry(tuple(reversed(snap.units())), ctx)
    assert starts == {u.uid: u.new_tb_offset for u in baseline.units}
    assert {uid: u.tc_size for uid, u in effective.items()} == {
        lu.uid: ScRecord.from_bytes(lu.record).tc_size for lu in baseline.units}
    assert not private and grown == {pred_a.uid, pred_b.uid}


def test_private_suffix_follows_final_predecessor_order_and_ext_bindings(tmp_path):
    from scopt.analysis.llvm_layout import guarded_lookups

    snap, plan, layout = guarded_snapshot()
    pred_a, pred_b, target = snap.units()
    result = NativeLinkerV2(snap, plan, placement=layout, offsets=GuardOffsets(),
                            derive_captures=False).link()
    assert result.ok, result.failures
    assert [v.pred_uid for v in result.variants] == [pred_b.uid, pred_a.uid]
    # Ext entries use the new indices and the actual relocated records.
    assert len(result.ext.slots) == 3 and len(result.ext.links) == 2
    for link in result.ext.links:
        variant = result.variants[link.variant_index]
        assert result.ext.slots[link.slot_index].src_tb_offset == result.by_uid()[variant.pred_uid].new_tb_offset
        assert result.ext.variants[link.variant_index].tc_offset == variant.tc_offset

    slot = SimpleNamespace(slot_n=0, vaddr=target.vaddrs[0], c_uid=target.uid)
    second = SimpleNamespace(slot_n=1, vaddr=pred_b.vaddrs[0], c_uid=pred_b.uid)
    state = SimpleNamespace(exit_plan=lambda uid, _: SimpleNamespace(
        slots=(slot, second) if uid == pred_a.uid else (slot,)))
    routes = guarded_lookups(result.grown, state, snap.header.target_page_bits)
    for uid, source in result.grown.items():
        route = routes[uid].transfers[0]
        assert route.kind == "SPEC" and route.destination == target.uid
        assert source.unit.code[route.offset] == 0xe9
        assert source.unit.code[routes[uid].fallback:routes[uid].fallback + 2] == b"\xff\xe0"

    second_route = routes[pred_a.uid].transfers[1]
    assert (second_route.kind, second_route.destination, second_route.slot_n) == ("GENERAL", pred_b.uid, 1)
    assert result.grown[pred_a.uid].unit.code[second_route.offset] == 0xe9

    # Use the emitted artifact to check the shared cap at the exact boundary.
    from scopt.validate import validate_b3, write_b3_files

    write_b3_files(tmp_path, snap, result)
    bytes_used = sum(v.end_offset - v.tc_offset for v in result.variants)
    bytes_used += sum(g.stub_bytes for g in result.grown.values())
    for limit, status in ((bytes_used, "pass"), (bytes_used - 1, "fail")):
        validation, _ = validate_b3(snap, result, tmp_path, skip_reimport=True,
                                    plan=replace(plan, max_total_growth_bytes=limit))
        row = next(row for row in validation.rows if row["code"] == "V21")
        assert row["result"] == status and row["max_total_growth_bytes"] == limit

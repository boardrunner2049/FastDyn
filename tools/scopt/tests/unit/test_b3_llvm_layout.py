from dataclasses import replace

from scopt.formats.sc_v5 import ScRecord
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

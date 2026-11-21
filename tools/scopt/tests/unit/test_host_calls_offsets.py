"""Original (certificate) and physical call offsets must not be mixed after materialization."""
from dataclasses import replace

from scopt.analysis.host_calls import build_host_calls
from scopt.analysis.hostops import build_hostops
from scopt.formats.enums import Encoding, OpMarkKind, SiteKind
from scopt.model.unit import OpMark, Site
from scopt.splice_layout import materialize_splices

from tests.unit.test_native_splice_scm import _two_site_fixture

PAGE_BITS = 10


def _call_at(unit, request, ca, off):
    """`unit` with one more helper call at `off` (the fixture has NOPs there)."""
    end = off + 5
    disp = (request.target_abs - (ca + unit.tc_offset) - end).to_bytes(4, "little", signed=True)  # rel32 from the call end
    site = Site(off + 1, off, 4, Encoding.REL32, SiteKind.HELPER_CALL, request.helper_id,
                request.target_abs, 0, int.from_bytes(disp, "little"))
    return replace(
        unit, code=unit.code[:off] + b"\xe8" + disp + unit.code[end:],
        sites=tuple(sorted((*unit.sites, site), key=lambda s: s.insn_off)),
        opmarks=tuple(sorted((*unit.opmarks, OpMark(off, OpMarkKind.CALL, 0, request.helper_id)),
                             key=lambda m: m.host_off)))


def _rows(unit, ca):
    return build_host_calls(unit, build_hostops(unit, None, ca), ca, PAGE_BITS)


def _collision():
    """Calls at 11 and 52 are inlined; the first grows by 16, so the untouched call at 36
    lands on physical offset 52, the original offset of the second retired call."""
    _, unit, requests, ca = _two_site_fixture()
    first, second = requests
    assert (first.call_off, second.call_off, first.growth) == (11, 52, 16)
    unit = _call_at(unit, first, ca, 36)
    return unit, requests, ca


def test_surviving_call_at_a_retired_calls_old_offset_is_kept():
    unit, requests, ca = _collision()
    assert [row.host_off for row in _rows(unit, ca)] == [11, 36, 52]

    spliced = materialize_splices(unit, requests, ca)
    assert spliced.splice_applied and [site.insn_off for site in spliced.sites] == [52]
    rows = _rows(spliced, ca)
    assert [(row.host_off, row.reason) for row in rows] == [(52, None)]
    assert rows[0].target_abs == requests[0].target_abs and rows[0].helper_id == requests[0].helper_id
    # The retired calls stay on record separately, in the coordinates of their certificates.
    assert [call.call_off for call in spliced.native_fragments] == [11, 52]


def test_surviving_call_keeps_its_binding_after_the_unit_moves():
    unit, requests, ca = _collision()
    spliced = materialize_splices(unit, requests, ca)
    site, = spliced.sites
    moved_by = 0x240
    end = site.insn_off + 5
    disp = (site.value - (ca + spliced.tc_offset + moved_by) - end).to_bytes(4, "little", signed=True)
    moved = replace(spliced, tc_offset=spliced.tc_offset + moved_by,
                    code=spliced.code[:site.field_off] + disp + spliced.code[site.field_off + 4:],
                    sites=(replace(site, final_field=int.from_bytes(disp, "little")),))
    rows = _rows(moved, ca)
    assert [(row.host_off, row.reason, row.target_abs) for row in rows] == [(52, None, requests[0].target_abs)]


def test_unmaterialized_certificates_still_hide_their_calls():
    """Before materialization the retired calls are still in the code at their certificate offsets."""
    unit, requests, ca = _collision()
    pending = replace(unit, native_fragments=requests)
    assert not pending.splice_applied
    assert [row.host_off for row in _rows(pending, ca)] == [36]

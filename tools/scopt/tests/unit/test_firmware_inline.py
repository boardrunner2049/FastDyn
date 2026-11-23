"""Firmware-inline guest discovery and fixed-point region planning."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopt.analysis.firmware_calls import discover_firmware_calls
from scopt.analysis.firmware_regions import build_firmware_region
from scopt.decode.guest_symbols import GuestImage
from scopt.decode.thumb_sem import decode_at, direct_call_target
from scopt.formats.enums import Encoding, SiteKind
from scopt.formats.scm_v1 import ScmSite, ScmStaticExit
from scopt.ids import TBKey
from scopt.formats.sc_v7 import (build_directory, decode_directory, directory_size, read_sc7,
                                 sc7_snapshot_id, write_sc7)
from scopt.formats.sc_v5 import ScFormatError, ScHeader
from scopt.link import (FirmwareMemberLink, FirmwareRegionLink, relocate_unit,
                        retarget_private_descriptor_refs, round_up)
from scopt.validate import v7_capable_exes

from ..synth.builders import SynthUnit
from ..synth.fake_snapshot import fake_snapshot


def test_direct_call_target_comes_from_thumb_operand():
    insn = decode_at(bytes.fromhex("00f07ef8"), 0x1000)[0]
    assert insn.text == "bl #0x1100"
    assert direct_call_target(insn) == 0x1100
    assert direct_call_target(decode_at(bytes.fromhex("9847"), 0x1000)[0]) is None


def _snapshot():
    key = TBKey(0x1000, None, 0x110, 0x800400, 0xff020000)
    call = bytes.fromhex("00f07ef8")
    units = [
        SynthUnit(key, 0x100, b"\xe9\0\0\0\0", guest_bytes=call, vaddr=0x1000,
                  jmp_reset_offset=(1, 0xffff), jmp_insn_offset=(1, 0xffff),
                  meta={"sites": (ScmSite(1, 0, 4, Encoding.REL32, SiteKind.GOTO_TB,
                                                    0, 0, 0, 0),),
                        "static_exits": (ScmStaticExit(0, 1, 0, 0x1100),)}),
        SynthUnit(key, 0x200, b"\x90", guest_bytes=b"\x00\xbf", vaddr=0x1004),
        SynthUnit(key, 0x300, b"\xff\xe0", guest_bytes=b"\x70\x47", vaddr=0x1100,
                  meta={"sites": (ScmSite(0, 0, 0, Encoding.NONE, SiteKind.GOTO_PTR,
                                                    0, 0, 0, 0),)}),
    ]
    snap = fake_snapshot(units)
    snap._units = tuple(replace(u, origin="elf_verified") for u in snap.units())
    snap._by_uid = {u.uid: u for u in snap._units}
    image_bytes = bytearray(0x102)
    image_bytes[0:4] = call
    image_bytes[4:6] = b"\x00\xbf"
    image_bytes[0x100:0x102] = b"\x70\x47"
    image = GuestImage("synthetic", "sha256:" + "1" * 64,
                       ((0x1000, bytes(image_bytes)),), ((0x1000, 0x1102),), (), ())
    return snap, image


def test_discovers_guest_call_and_builds_private_region():
    snapshot, image = _snapshot()
    calls = discover_firmware_calls(snapshot, image)
    assert len(calls.calls) == 1
    call = calls.calls[0]
    assert call.eligible and call.target_vaddr == 0x1100
    assert call.continuation_vaddr == 0x1004
    plan = build_firmware_region(snapshot, image, calls, call)
    assert plan.complete
    assert [m.role for m in plan.members] == ["caller", "callee", "continuation"]
    assert [(edge.kind, edge.target_member) for edge in plan.edges] == [
        ("call", 1), ("return", 2)]
    assert plan.edges[1].guarded
    assert plan.members[1].inline_context == (call.callsite_id,)


def test_missing_nested_capture_keeps_outer_region_with_side_exit():
    key = TBKey(0x1000, None, 0x110, 0x800400, 0xff020000)
    root_call = bytes.fromhex("00f07ef8")       # 0x1000 -> 0x1100
    nested_call = bytes.fromhex("00f07ef8")     # 0x1100 -> 0x1200
    units = [
        SynthUnit(key, 0x100, b"\xe9\0\0\0\0", guest_bytes=root_call, vaddr=0x1000,
                  jmp_reset_offset=(1, 0xffff), jmp_insn_offset=(1, 0xffff),
                  meta={"sites": (ScmSite(1, 0, 4, Encoding.REL32, SiteKind.GOTO_TB,
                                                    0, 0, 0, 0),),
                        "static_exits": (ScmStaticExit(0, 1, 0, 0x1100),)}),
        SynthUnit(key, 0x200, b"\x90", guest_bytes=b"\x00\xbf", vaddr=0x1004),
        SynthUnit(key, 0x300, b"\xe9\0\0\0\0", guest_bytes=nested_call, vaddr=0x1100,
                  jmp_reset_offset=(1, 0xffff), jmp_insn_offset=(1, 0xffff),
                  meta={"sites": (ScmSite(1, 0, 4, Encoding.REL32, SiteKind.GOTO_TB,
                                                    0, 0, 0, 0),),
                        "static_exits": (ScmStaticExit(0, 1, 0, 0x1200),)}),
    ]
    snapshot = fake_snapshot(units)
    snapshot._units = tuple(replace(u, origin="elf_verified") for u in snapshot.units())
    snapshot._by_uid = {u.uid: u for u in snapshot._units}
    image_bytes = bytearray(0x104)
    image_bytes[0:4] = root_call
    image_bytes[4:6] = b"\x00\xbf"
    image_bytes[0x100:0x104] = nested_call
    image = GuestImage("synthetic", "sha256:" + "2" * 64,
                       ((0x1000, bytes(image_bytes)),), ((0x1000, 0x1204),), (), ())

    calls = discover_firmware_calls(snapshot, image)
    root = next(call for call in calls.calls if call.insn_vaddr == 0x1000)
    plan = build_firmware_region(snapshot, image, calls, root)
    assert plan.complete
    assert any(row["reason"] == "missing_callee_capture" for row in plan.pending)
    assert any(edge.kind == "side_exit" and
               edge.refusal_reason == "missing_callee_capture" for edge in plan.edges)


@pytest.mark.parametrize(("encoding", "width", "field", "initial"), [
    (Encoding.RIPREL32, 4, 3, bytes.fromhex("488d0500000000")),
    (Encoding.IMM64, 8, 2, bytes.fromhex("48b80000000000000000")),
])
def test_private_exit_tb_pointer_targets_real_descriptor(encoding, width, field, initial):
    site = ScmSite(field, 0, width, encoding, SiteKind.MOVI_EXIT_TB_PTR,
                   3, 0, 0, 0)
    source = SimpleNamespace(uid="private-member", record=b"\0" * 152, sites=(site,))
    code = bytearray(initial)
    tc_offset, tb_offset, code_address = 0x3000, 0x1800, 0x7fff_0000_0000

    assert retarget_private_descriptor_refs(
        code, source, tc_offset=tc_offset, tb_offset=tb_offset,
        code_address=code_address) == ((field, width),)
    target = code_address + tb_offset + 3
    if encoding == Encoding.RIPREL32:
        disp = int.from_bytes(code[field:field + width], "little", signed=True)
        assert code_address + tc_offset + field + width + disp == target
    else:
        assert int.from_bytes(code[field:field + width], "little") == target


def test_v7_export_and_reimport_directory(tmp_path):
    snapshot, _image = _snapshot()
    source = snapshot.units()[0]
    public = relocate_unit(source, source.tb_offset)
    start = round_up(max(u.end_offset for u in snapshot.units()), 64)
    tc = round_up(start + snapshot.header.tb_size, 16)
    member = FirmwareMemberLink(source.uid, (), start, tc, source.tc_size,
                                tc + source.tc_size, len(source.search),
                                b"\0" * snapshot.header.tb_size, source.code, source.search,
                                source.content_hash, source.vaddrs[0], (), source)
    region = FirmwareRegionLink("sha256:" + "2" * 64, "sha256:" + "3" * 64,
                                0, (member,), (source.uid,), start,
                                tc + source.tc_size + len(source.search))
    link = SimpleNamespace(units=(public,), firmware_regions=(region,),
                           firmware_table_offset=round_up(region.end_offset, 8))
    link.firmware_table_size = directory_size(link.firmware_regions)
    path = tmp_path / "cache.sc"
    write_sc7(path, snapshot.header, ((public.record, public.body),), link)
    parsed = read_sc7(path)
    assert parsed.n_regions == parsed.n_members == 1
    assert parsed.n_edges == 0
    assert parsed.table_offset == link.firmware_table_offset
    decoded = decode_directory(parsed)
    assert decoded["regions"][0]["flags"] == 1
    assert decoded["regions"][0]["composite"] == {
        "descriptor_offset": start, "tc_offset": tc, "tc_size": source.tc_size,
        "search_start": tc + source.tc_size,
        "search_end": region.end_offset,
    }


def test_v7_writes_out_of_line_search_and_tracks_copy_origins(tmp_path):
    snapshot, _image = _snapshot()
    source = snapshot.units()[0]
    public = relocate_unit(source, source.tb_offset)
    start = round_up(max(u.end_offset for u in snapshot.units()), 64)
    tc0 = round_up(start + 2 * snapshot.header.tb_size, 16)
    tc1 = tc0 + source.tc_size
    search0 = tc1 + source.tc_size
    search1 = search0 + len(source.search)
    member0 = FirmwareMemberLink(
        source.uid, ("site-a",), start, tc0, source.tc_size, search0, len(source.search),
        b"\0" * snapshot.header.tb_size, source.code, source.search, source.content_hash,
        source.vaddrs[0], (), source)
    member1 = FirmwareMemberLink(
        source.uid, ("site-b",), start + snapshot.header.tb_size, tc1, source.tc_size,
        search1, len(source.search), b"\0" * snapshot.header.tb_size, source.code,
        source.search, source.content_hash, source.vaddrs[0], (), source)
    end = search1 + len(source.search)
    region = FirmwareRegionLink("sha256:" + "2" * 64, "sha256:" + "3" * 64,
                                0, (member0, member1), (source.uid,), start, end)
    link = SimpleNamespace(units=(public,), firmware_regions=(region,),
                           firmware_table_offset=round_up(end, 8))
    link.firmware_table_size = directory_size(link.firmware_regions)
    path = tmp_path / "cache.sc"
    write_sc7(path, snapshot.header, ((public.record, public.body),), link)
    parsed = read_sc7(path)
    decoded = decode_directory(parsed)
    assert [row["source_tb_offset"] for row in decoded["origins"]] == \
        [source.tb_offset, source.tb_offset]
    assert len({row["context_id8"] for row in decoded["origins"]}) == 2
    assert [row["private_offset"] for row in decoded["origins"]] == [tc0, tc1]
    with path.open("rb") as stream:
        from scopt.formats.layouts import SC_CODE_OFFSET

        stream.seek(SC_CODE_OFFSET + search0)
        assert stream.read(len(source.search)) == source.search
        assert decoded["regions"][0]["composite"]["tc_size"] == 2 * source.tc_size
    assert sc7_snapshot_id(path) == sc7_snapshot_id(path)


def test_v7_edge_targets_use_region_member_base():
    snapshot, _image = _snapshot()
    source = snapshot.units()[0]
    public = relocate_unit(source, source.tb_offset)
    start = round_up(max(u.end_offset for u in snapshot.units()), 64)
    tc = round_up(start + 2 * snapshot.header.tb_size, 16)
    first = FirmwareMemberLink(
        source.uid, ("outer",), start, tc, source.tc_size, tc + 2 * source.tc_size,
        len(source.search), b"\0" * snapshot.header.tb_size, source.code, source.search,
        source.content_hash, source.vaddrs[0], (), source)
    second = FirmwareMemberLink(
        source.uid, ("inner",), start + snapshot.header.tb_size, tc + source.tc_size,
        source.tc_size, tc + 2 * source.tc_size + len(source.search), len(source.search),
        b"\0" * snapshot.header.tb_size, source.code, source.search, source.content_hash,
        source.vaddrs[0], (("x0", 1, 0),), source)
    end = second.search_offset + len(source.search)
    region = FirmwareRegionLink("sha256:" + "4" * 64, "sha256:" + "5" * 64,
                                0, (first, second), (source.uid,), start, end)
    link = SimpleNamespace(units=(public,), firmware_regions=(region,),
                           firmware_table_offset=round_up(end, 8))
    link.firmware_table_size = directory_size(link.firmware_regions)
    decoded = decode_directory(build_directory(link, {source.uid: source.tb_offset}))
    assert decoded["edges"] == [{
        "region_index": 0, "source_member": 1, "target_member": 0,
        "field_offset": 1, "kind": 1, "flags": 0, "reserved": 0,
        "guest_target": 0, "exit_id_fnv64": decoded["edges"][0]["exit_id_fnv64"]}]


def test_v7_rejects_noncomposite_member_geometry():
    snapshot, _image = _snapshot()
    source = snapshot.units()[0]
    public = relocate_unit(source, source.tb_offset)
    start = round_up(max(u.end_offset for u in snapshot.units()), 64)
    tc = round_up(start + 2 * snapshot.header.tb_size, 16)
    search = tc + 2 * source.tc_size
    first = FirmwareMemberLink(
        source.uid, ("outer",), start, tc, source.tc_size, search, len(source.search),
        b"\0" * snapshot.header.tb_size, source.code, source.search, source.content_hash,
        source.vaddrs[0], (), source)
    second = FirmwareMemberLink(
        source.uid, ("inner",), start + snapshot.header.tb_size, tc + source.tc_size,
        source.tc_size, search + len(source.search), len(source.search),
        b"\0" * snapshot.header.tb_size, source.code, source.search, source.content_hash,
        source.vaddrs[0], (), source)
    region = FirmwareRegionLink("sha256:" + "a" * 64, "sha256:" + "b" * 64,
                                0, (first, second), (source.uid,), start,
                                second.search_offset + len(source.search))

    def build(candidate):
        link = SimpleNamespace(units=(public,), firmware_regions=(candidate,),
                               firmware_table_size=directory_size((candidate,)),
                               firmware_table_offset=round_up(candidate.end_offset, 8))
        return build_directory(link, {source.uid: source.tb_offset})

    build(region)
    with pytest.raises(ScFormatError, match="entry member"):
        build(replace(region, entry_member=1))
    with pytest.raises(ScFormatError, match="code is not contiguous"):
        build(replace(region, members=(first, replace(second, tc_offset=second.tc_offset + 1))))
    with pytest.raises(ScFormatError, match="search data"):
        build(replace(region, members=(replace(first, search_offset=first.search_offset + 1),
                                       second)))


def test_v7_rejects_malformed_header_and_directory_counts(tmp_path):
    import scopt.formats.sc_v7 as V7

    snapshot, _image = _snapshot()
    source = snapshot.units()[0]
    public = relocate_unit(source, source.tb_offset)
    start = round_up(max(u.end_offset for u in snapshot.units()), 64)
    tc = round_up(start + snapshot.header.tb_size, 16)
    member = FirmwareMemberLink(source.uid, (), start, tc, source.tc_size,
                                tc + source.tc_size, len(source.search),
                                b"\0" * snapshot.header.tb_size, source.code, source.search,
                                source.content_hash, source.vaddrs[0], (), source)
    region = FirmwareRegionLink("sha256:" + "6" * 64, "sha256:" + "7" * 64,
                                0, (member,), (source.uid,), start,
                                member.search_offset + len(source.search))
    link = SimpleNamespace(units=(public,), firmware_regions=(region,),
                           firmware_table_offset=round_up(region.end_offset, 8))
    link.firmware_table_size = directory_size(link.firmware_regions)
    path = tmp_path / "cache.sc"
    write_sc7(path, snapshot.header, ((public.record, public.body),), link)

    with path.open("r+b") as stream:
        header = ScHeader.from_bytes(stream.read(80))
        stream.seek(0)
        stream.write(header.replace(reserved=2).with_checksum().to_bytes())
    with pytest.raises(ScFormatError, match="feature bits"):
        read_sc7(path)

    write_sc7(path, snapshot.header, ((public.record, public.body),), link)
    with path.open("r+b") as stream:
        stream.seek(V7.SC7_OFFSET)
        raw = stream.read(V7._HEADER.size)
        fields = list(V7._HEADER.unpack(raw))
        fields[1] = 1  # legacy per-member directory contract
        stream.seek(V7.SC7_OFFSET)
        stream.write(V7._HEADER.pack(*fields))
    with pytest.raises(ScFormatError, match="directory header"):
        read_sc7(path)

    write_sc7(path, snapshot.header, ((public.record, public.body),), link)
    with path.open("r+b") as stream:
        stream.seek(V7.SC7_OFFSET)
        raw = stream.read(V7._HEADER.size)
        fields = list(V7._HEADER.unpack(raw))
        fields[4] += 1  # n_members, without adding a member row
        stream.seek(V7.SC7_OFFSET)
        stream.write(V7._HEADER.pack(*fields))
    with pytest.raises(ScFormatError, match="count/size"):
        read_sc7(path)


def test_decode_directory_rejects_truncated_array():
    snapshot, _image = _snapshot()
    source = snapshot.units()[0]
    public = relocate_unit(source, source.tb_offset)
    start = round_up(max(u.end_offset for u in snapshot.units()), 64)
    tc = round_up(start + snapshot.header.tb_size, 16)
    member = FirmwareMemberLink(source.uid, (), start, tc, source.tc_size,
                                tc + source.tc_size, len(source.search),
                                b"\0" * snapshot.header.tb_size, source.code, source.search,
                                source.content_hash, source.vaddrs[0], (), source)
    region = FirmwareRegionLink("sha256:" + "8" * 64, "sha256:" + "9" * 64,
                                0, (member,), (source.uid,), start,
                                member.search_offset + len(source.search))
    link = SimpleNamespace(units=(public,), firmware_regions=(region,),
                           firmware_table_offset=round_up(region.end_offset, 8))
    link.firmware_table_size = directory_size(link.firmware_regions)
    directory = build_directory(link, {source.uid: source.tb_offset})
    with pytest.raises(ScFormatError, match="expected"):
        decode_directory(replace(directory, table=directory.table[:-1]))


def test_v7_loader_capability_is_explicit_and_distinct_from_v6(monkeypatch):
    monkeypatch.setenv("SCOPT_V6_EXES", "aa" * 32)
    monkeypatch.setenv("SCOPT_V7_EXES", "ee" * 32)
    monkeypatch.delenv("SCOPT_V7_COMPOSITE_EXES", raising=False)
    assert not v7_capable_exes()
    monkeypatch.setenv("SCOPT_V7_COMPOSITE_EXES",
                       f"sha256:{'bb' * 32},{'cc' * 32}")
    assert v7_capable_exes(("sha256:" + "dd" * 32,)) == frozenset({
        "bb" * 32, "cc" * 32, "dd" * 32})

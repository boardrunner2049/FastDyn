"""Firmware-inline guest discovery and fixed-point region planning."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from scopt.analysis.firmware_calls import compatible_units, discover_firmware_calls
from scopt.analysis.firmware_regions import build_firmware_region
from scopt.decode.guest_symbols import GuestImage
from scopt.decode.thumb_sem import decode_at, direct_call_target
from scopt.formats.enums import Encoding, SiteKind
from scopt.formats.scm_v1 import ScmSite, ScmStaticExit
from scopt.ids import TBKey
from scopt.formats.sc_v7 import (LOADER_V7_TRUSTED, build_directory, decode_directory,
                                 directory_size, loader_requirement, read_sc7,
                                 sc7_snapshot_id, write_sc7)
from scopt.formats.sc_v5 import ScFormatError, ScHeader
from scopt.link import (FirmwareMemberLink, FirmwareRegionLink, relocate_unit,
                        retarget_private_descriptor_refs, round_up,
                        trusted_firmware_edge_problem, trusted_firmware_entry_problem)
from scopt.passes.firmware_policy import _hot_loop_calls
from scopt.validate import trusted_v7_capable_exes, v7_capable_exes

from ..synth.builders import SynthUnit
from ..synth.fake_snapshot import fake_snapshot


def test_direct_call_target_comes_from_thumb_operand():
    insn = decode_at(bytes.fromhex("00f07ef8"), 0x1000)[0]
    assert insn.text == "bl #0x1100"
    assert direct_call_target(insn) == 0x1100
    assert direct_call_target(decode_at(bytes.fromhex("9847"), 0x1000)[0]) is None


def test_sealed_compatibility_selects_newest_exact_entry():
    key = TBKey(0x1000, None, 0x110, 0x800400, 0xff000000)
    common = {"vaddrs": (0x1000,), "key": key, "meta_status": "ok",
              "origin": "elf_verified"}
    fallback = SimpleNamespace(uid="fallback", slot=1, **common)
    hot = SimpleNamespace(uid="hot", slot=9, **common)
    interior = SimpleNamespace(uid="interior", slot=10, vaddrs=(0xffe, 0x1000),
                               key=key, meta_status="ok", origin="elf_verified")
    assert compatible_units((fallback, hot, interior), fallback, 0x1000) == ("hot",)


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


def test_guest_hot_loop_counts_actual_verified_slot_and_excludes_interrupted_transfers():
    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    src, dst = call.source_uid, call.callee_candidates[0]

    def edge(src, dst, kind, count, flags=(), status="exact"):
        return dict(src=src, dst=dst, kind=kind, count=count, flags=flags, status=status)

    rows = [edge(src, dst, "chain0", 1001), edge(dst, src, "lookup_hit", 1001),
            edge(src, dst, "chain1", 5000), edge(src, dst, "chain0", 80, ("irq",)),
            edge(src, dst, "chain0", 4000, status="lower_bound")]
    profile = SimpleNamespace(edges=lambda phase: rows)
    kept, residual, counts = _hot_loop_calls(snapshot, [call], profile, "steady", 1000)
    assert kept == [call] and not residual and counts[call.callsite_id] == 1001
    kept, residual, _ = _hot_loop_calls(snapshot, [call], profile, "steady", 1002)
    assert not kept and residual[0]["reason"] == "below_min_call_count"
    rows.pop(1)
    kept, residual, _ = _hot_loop_calls(snapshot, [call], profile, "steady", 2)
    assert not kept and residual[0]["reason"] == "outside_observed_loop"


@pytest.mark.parametrize("capture_nested", [False, True])
def test_nested_capture_policy_keeps_outer_region_with_side_exit(capture_nested):
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
    if capture_nested:
        units.extend([
            SynthUnit(key, 0x400, b"\xff\xe0", guest_bytes=b"\x70\x47", vaddr=0x1104,
                      meta={"sites": (ScmSite(0, 0, 0, Encoding.NONE, SiteKind.GOTO_PTR,
                                              0, 0, 0, 0),)}),
            SynthUnit(key, 0x500, b"\xff\xe0", guest_bytes=b"\x70\x47", vaddr=0x1200,
                      meta={"sites": (ScmSite(0, 0, 0, Encoding.NONE, SiteKind.GOTO_PTR,
                                              0, 0, 0, 0),)}),
        ])
    snapshot = fake_snapshot(units)
    snapshot._units = tuple(replace(u, origin="elf_verified") for u in snapshot.units())
    snapshot._by_uid = {u.uid: u for u in snapshot._units}
    image_bytes = bytearray(0x202 if capture_nested else 0x104)
    image_bytes[0:4] = root_call
    image_bytes[4:6] = b"\x00\xbf"
    image_bytes[0x100:0x104] = nested_call
    if capture_nested:
        image_bytes[0x104:0x106] = b"\x70\x47"
        image_bytes[0x200:0x202] = b"\x70\x47"
    image = GuestImage("synthetic", "sha256:" + "2" * 64,
                       ((0x1000, bytes(image_bytes)),), ((0x1000, 0x1204),), (), ())

    calls = discover_firmware_calls(snapshot, image)
    root = next(call for call in calls.calls if call.insn_vaddr == 0x1000)
    plan = build_firmware_region(snapshot, image, calls, root, expand_nested=not capture_nested)
    assert plan.complete
    reason = "nested_call_kept_public" if capture_nested else "missing_callee_capture"
    assert any(edge.kind == "side_exit" and edge.refusal_reason == reason for edge in plan.edges)
    if capture_nested:
        nested = next(call for call in calls.calls if call.insn_vaddr == 0x1100)
        assert nested.eligible
        assert nested.callee_candidates[0] not in plan.source_uids

        expanded = build_firmware_region(snapshot, image, calls, root)
        return_edges = {(expanded.members[e.source_member].source_uid,
                         expanded.members[e.target_member].source_uid)
                        for e in expanded.edges if e.kind == "return"}
        assert return_edges == {
            (nested.callee_candidates[0], nested.continuation_candidates[0]),
            (nested.continuation_candidates[0], root.continuation_candidates[0]),
        }

        from scopt.analysis.rtos_boundaries import ExpansionAction
        def stop_at_kernel(site, context):
            assert site.kind == "call" and context == (root.callsite_id,)
            assert site.source_pc == 0x1100 and site.target_pc == 0x1200
            return ExpansionAction.SHARED

        bounded = build_firmware_region(snapshot, image, calls, root, expansion=stop_at_kernel)
        assert bounded.complete and nested.callee_candidates[0] not in bounded.source_uids
        assert any(e.refusal_reason == "rtos_shared_public_lookup" and e.target_member is None
                   for e in bounded.edges)

        # An exact-PC fallback-only BL may be reached inside this region
        # without a matching discovery row. It must retain its own LR context.
        sparse_calls = replace(calls, calls=(root,))
        fallback = build_firmware_region(snapshot, image, sparse_calls, root)
        assert nested.callee_candidates[0] not in fallback.source_uids
        assert any(e.refusal_reason == "unverified_nested_call_exit"
                   and e.target_member is None for e in fallback.edges)
    else:
        assert any(row["reason"] == "missing_callee_capture" for row in plan.pending)


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


@pytest.mark.parametrize("exit_index", [0, 1, 3])
def test_trusted_exit_tb_pointer_preserves_index_on_shared_descriptor(exit_index):
    site = ScmSite(2, 0, 8, Encoding.IMM64, SiteKind.MOVI_EXIT_TB_PTR,
                   exit_index, 0, 0, 0)
    source = SimpleNamespace(uid="trusted-member", record=b"\0" * 152, sites=(site,))
    code = bytearray(bytes.fromhex("48b80000000000000000"))
    retarget_private_descriptor_refs(
        code, source, tc_offset=0x3000, tb_offset=0x1800,
        code_address=0x7fff_0000_0000)
    assert int.from_bytes(code[2:10], "little") == 0x7fff_0000_1800 + exit_index


def test_trusted_prefix_requires_exact_non_icount_poll_and_can_do_io_store():
    code = bytes.fromhex("8b5df885db0f8c01020304c645fc01") + b"\x90"
    unit = SimpleNamespace(code=code, insn0_start=15,
                           key=SimpleNamespace(cflags=0))
    assert trusted_firmware_entry_problem(unit) is None
    assert trusted_firmware_entry_problem(
        SimpleNamespace(code=code, insn0_start=15,
                        key=SimpleNamespace(cflags=0x2000))) == "icount_cflags"
    assert trusted_firmware_entry_problem(
        SimpleNamespace(code=code[:11] + bytes.fromhex("c645fc02") + code[15:],
                        insn0_start=15,
                        key=SimpleNamespace(cflags=0))) == "trusted_irq_prefix_shape"


def test_trusted_edge_requires_patchable_native_transfer():
    snapshot, _image = _snapshot()
    call, _continuation, ret = snapshot.units()
    assert trusted_firmware_edge_problem(call, "x0") is None
    assert trusted_firmware_edge_problem(ret, "x0") == "lookup_tail_missing"
    assert trusted_firmware_edge_problem(ret, "missing") == "exit_missing"


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
        "trusted": False, "member_recovery": True, "member_descriptors": False,
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


def test_v7_trusted_uses_one_descriptor_no_search_and_skip_poll_edges(tmp_path):
    snapshot, _image = _snapshot()
    source = snapshot.units()[0]
    public = relocate_unit(source, source.tb_offset)
    prefix = bytes.fromhex("8b5df885db0f8c00000000c645fc00")
    code0 = bytearray(prefix + b"\xe9\0\0\0\0")
    code1 = prefix + b"\x90"
    start = round_up(max(u.end_offset for u in snapshot.units()), 64)
    tc0 = round_up(start + snapshot.header.tb_size, 16)
    tc1 = tc0 + len(code0)
    end = tc1 + len(code1)
    field = len(prefix) + 1
    code0[field:field + 4] = (tc1 + 15 - (tc0 + field + 4)).to_bytes(
        4, "little", signed=True)
    descriptor = b"\0" * snapshot.header.tb_size
    first = FirmwareMemberLink(
        source.uid, ("root",), start, tc0, len(code0), end, 0, descriptor,
        bytes(code0), b"", source.content_hash, source.vaddrs[0], (("x0", field, 1),),
        source)
    second = FirmwareMemberLink(
        source.uid, ("callee",), start, tc1, len(code1), end, 0, descriptor,
        code1, b"", source.content_hash, source.vaddrs[0], (), source)
    region = FirmwareRegionLink("sha256:" + "c" * 64, "sha256:" + "d" * 64,
                                0, (first, second), (source.uid,), start, end, True, 15)
    link = SimpleNamespace(units=(public,), firmware_regions=(region,),
                           firmware_table_offset=round_up(end, 8))
    link.firmware_table_size = directory_size(link.firmware_regions)
    path = tmp_path / "trusted.sc"
    write_sc7(path, snapshot.header, ((public.record, public.body),), link)
    directory = read_sc7(path)
    decoded = decode_directory(directory)
    assert directory.directory_version == 3 and directory.trusted
    assert loader_requirement(directory) == LOADER_V7_TRUSTED
    assert decoded["regions"][0]["flags"] == 3
    assert decoded["regions"][0]["composite"] == {
        "descriptor_offset": start, "tc_offset": tc0,
        "tc_size": len(code0) + len(code1), "search_start": end,
        "search_end": end, "trusted": True, "member_recovery": False,
        "member_descriptors": False}
    assert {m["tb_offset"] for m in decoded["members"]} == {start}
    assert {m["search_offset"] for m in decoded["members"]} == {end}
    assert decoded["edges"][0]["flags"] == 1
    from scopt.commands.firmware_region import recover

    result = CliRunner().invoke(
        recover, ["--artifact", str(path), "--native-offset", hex(tc1 + 12), "--json"])
    assert result.exit_code == 0, result.output
    recovered = json.loads(result.output)
    assert (recovered["member_index"], recovered["source_offset"], recovered["trusted"]) == \
        (1, 12, True)


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
    monkeypatch.delenv("SCOPT_V7_TRUSTED_EXES", raising=False)
    assert not trusted_v7_capable_exes()
    monkeypatch.setenv("SCOPT_V7_TRUSTED_EXES", "ff" * 32)
    assert trusted_v7_capable_exes() == frozenset({"ff" * 32})


# -- trusted member descriptors (chainable residual exits) -----------------

_TRUSTED_PREFIX = bytes.fromhex("8b5df885db0f8c00000000c645fc00")


def _trusted_member_unit(snapshot, call, *, slot=20):
    """The caller rebuilt as a trusted-entry TB with one residual goto_tb slot.

    ``e9`` at ``slot - 1``, the zero rel32 at ``slot`` (4-aligned when ``slot``
    is), its reset path at ``slot + 4``: ``movabs rax, descriptor|0; ret``.
    """
    import struct

    from scopt.formats.sc_v5 import ScRecord
    from scopt.model.unit import Site

    original = snapshot.unit(call.source_uid)
    code = (_TRUSTED_PREFIX + b"\x90" * (slot - 1 - len(_TRUSTED_PREFIX)) + b"\xe9\0\0\0\0" +
            bytes.fromhex("48b8") + struct.pack("<Q", 0) + b"\xc3")
    sites = (Site(slot, slot - 1, 4, Encoding.REL32, SiteKind.GOTO_TB, 0, 0, 0, 0),
             Site(slot + 6, slot + 4, 8, Encoding.IMM64, SiteKind.MOVI_EXIT_TB_PTR, 0, 0, 0, 0))
    end = original.tc_offset + len(code) + len(original.search)
    record = ScRecord.from_bytes(original.record).replace(
        tc_size=len(code), end_offset=end,
        jmp_insn_offset=(slot, 0xFFFF), jmp_reset_offset=(slot + 4, 0xFFFF))
    return replace(original, code=code, tc_size=len(code), end_offset=end, insn0_start=15,
                   sites=sites, record=record.to_bytes(fix_checksum=True))


def _trusted_two_member_region(snapshot, call, unit):
    from scopt.model.firmware_region import FirmwareRegionMember

    members = (FirmwareRegionMember(unit.uid, unit.content_hash, unit.key, (), "caller"),
               FirmwareRegionMember(unit.uid, unit.content_hash, unit.key, ("c",), "callee"))
    return SimpleNamespace(members=members, trusted=True, edges=(), region_id="sha256:" + "e" * 64,
                           callsite=call, entry_member=0, source_uids=(unit.uid,),
                           trusted_entry_offset=15)


def _link_trusted(snapshot, region, unit, plan_fields=None, out_records=None):
    from scopt.link import B3Plan, NativeLinkerV2

    plan = B3Plan(firmware_regions=(region,), **(plan_fields or {}))
    linker = NativeLinkerV2(snapshot, plan, tb_ret_abs=snapshot.header.code_address + 24)
    (linked,), end = linker._link_firmware_regions(0x4000, {unit.uid: unit}, out_records)
    return linked, end


def _b3_grown_unit(unit, *, insn=None):
    """``unit`` as B3 growth leaves its public record: a guard stub appended
    past the pre-B3 code whose reset goto_tb registers the free slot 1
    (``e9`` + zero rel32 at ``insn``, 4-aligned, reset at ``insn + 4``).  A
    member copies the pre-B3 bytes, which do not have that slot."""
    from scopt.formats.sc_v5 import ScRecord

    if insn is None:
        insn = len(unit.code) + 1
        insn += (-(unit.tc_offset + insn)) % 4
    code = (unit.code + b"\x90" * (insn - 1 - len(unit.code)) + b"\xe9\0\0\0\0" +
            b"\xc3")
    rec = ScRecord.from_bytes(unit.record)
    end = unit.tc_offset + len(code) + len(unit.search)
    record = rec.replace(tc_size=len(code), end_offset=end,
                         jmp_insn_offset=(rec.jmp_insn_offset[0], insn),
                         jmp_reset_offset=(rec.jmp_reset_offset[0], insn + 4))
    return replace(unit, code=code, tc_size=len(code), end_offset=end,
                   record=record.to_bytes(fix_checksum=True))


def _b3_registered_record(unit):
    """The output record of :func:`_b3_grown_unit`."""
    return _b3_grown_unit(unit).record


def test_trusted_region_links_one_descriptor_per_member():
    from scopt.link import region_exits_chainable, residual_goto_tb_slots

    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    unit = _trusted_member_unit(snapshot, call)
    region = _trusted_two_member_region(snapshot, call, unit)
    assert region_exits_chainable(region)
    assert not region_exits_chainable(SimpleNamespace(trusted=False, members=region.members))
    assert not region_exits_chainable(region, {unit.uid})
    linked, end = _link_trusted(snapshot, region, unit)
    tb_size = snapshot.header.tb_size
    ca = snapshot.header.code_address
    assert linked.member_descriptors and linked.member_descriptor_refusal is None
    assert [m.skip_slots for m in linked.members] == [0, 0]
    assert (linked.chainable_residual_slots, linked.skipped_b3_slots) == (2, 0)
    first, second = linked.members
    assert (first.tb_offset, second.tb_offset) == (0x4000, 0x4000 + tb_size)
    assert first.tc_offset == round_up(0x4000 + 2 * tb_size, 16)
    assert second.tc_offset == round_up(first.tc_offset + first.tc_size, 16)
    assert second.tc_offset > first.tc_offset + first.tc_size          # padded
    assert end == second.tc_offset + second.tc_size
    # Each member's exit_tb result names its own descriptor, exit index kept.
    for member in linked.members:
        assert int.from_bytes(member.code[26:34], "little") == ca + member.tb_offset
        assert residual_goto_tb_slots(unit.record, member.code, member.tc_offset,
                                      code_address=ca) == (1, [])


@pytest.mark.parametrize("case", ["b3_registered_slot", "misaligned", "output_record"])
def test_trusted_region_falls_back_to_shared_descriptor(case):
    from scopt.formats.sc_v5 import ScRecord
    from scopt.link import b3_edited_uids

    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    unit = _trusted_member_unit(snapshot, call, slot=21 if case == "misaligned" else 20)
    region = _trusted_two_member_region(snapshot, call, unit)
    plan_fields, out_records = None, None
    if case == "b3_registered_slot":
        # B3 grew the source and registered its free slot 1 at a stub past
        # the pre-B3 code; without member skip flags QEMU would arm it.
        plan_fields = {"grow": {unit.uid: ()}, "member_slot_skip": False}
        out_records = {unit.uid: _b3_registered_record(unit)}
    elif case == "output_record":
        # QEMU arms slots from the OUTPUT record: one naming a non-goto_tb
        # field cannot get member descriptors.
        rec = ScRecord.from_bytes(unit.record).replace(
            jmp_insn_offset=(16, 0xFFFF), jmp_reset_offset=(20, 0xFFFF))
        out_records = {unit.uid: rec.to_bytes(fix_checksum=True)}
    linked, _ = _link_trusted(snapshot, region, unit, plan_fields, out_records)
    assert not linked.member_descriptors
    assert linked.member_descriptor_refusal.startswith(
        "b3_registered_slot" if case == "b3_registered_slot" else "residual_slot_shape")
    if case == "b3_registered_slot":
        assert linked.member_descriptor_refusal == "b3_registered_slot"
    assert all(m.skip_slots == 0 for m in linked.members)
    assert (linked.skipped_b3_slots, linked.chainable_residual_slots) == (0, 0)
    assert {m.tb_offset for m in linked.members} == {0x4000}
    first, second = linked.members
    assert second.tc_offset == first.tc_offset + first.tc_size        # legacy: contiguous
    for member in linked.members:
        assert int.from_bytes(member.code[26 + (case == "misaligned"):
                                          34 + (case == "misaligned")], "little") == \
            snapshot.header.code_address + 0x4000
    assert b3_edited_uids(SimpleNamespace(grow={"g": ()}, rewrites={"r": ()},
                                          variants=(SimpleNamespace(src_uid="v"),))) == \
        {"g", "r", "v"}


def test_skip_capable_region_with_a_bad_armed_slot_falls_back_without_flags(tmp_path):
    """With member skip flags on, a grown source's mask is non-zero; when an
    armed (unflagged) slot still fails its shape check, the region falls back
    to the legacy layout, which must carry no skip flags (a legacy member's
    flags must be 0, and build_directory refuses anything else)."""
    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    unit = _trusted_member_unit(snapshot, call, slot=21)               # slot 0 misaligned
    grown = _b3_grown_unit(unit)                                      # registers slot 1
    region = _trusted_two_member_region(snapshot, call, unit)
    linked, _ = _link_trusted(snapshot, region, unit,
                              {"grow": {unit.uid: ()}, "member_slot_skip": True},
                              {unit.uid: grown.record})
    assert not linked.member_descriptors
    assert linked.member_descriptor_refusal.startswith("residual_slot_shape")
    assert [m.skip_slots for m in linked.members] == [0, 0]
    assert (linked.skipped_b3_slots, linked.chainable_residual_slots) == (0, 0)
    assert {m.tb_offset for m in linked.members} == {0x4000}           # shared descriptor
    path, _ = _write_trusted_md(tmp_path, snapshot, call, grown, linked)
    decoded = decode_directory(read_sc7(path))
    assert decoded["regions"][0]["flags"] == 3
    assert [m["flags"] for m in decoded["members"]] == [0, 0]


def _write_trusted_md(tmp_path, snapshot, call, unit, linked, name="md.sc"):
    public = relocate_unit(unit, unit.tb_offset)
    link = SimpleNamespace(units=(public,), firmware_regions=(linked,),
                           firmware_table_offset=round_up(linked.end_offset, 8))
    link.firmware_table_size = directory_size(link.firmware_regions)
    path = tmp_path / name
    write_sc7(path, snapshot.header, ((public.record, public.body),), link)
    return path, link


def test_v7_trusted_member_descriptors_layout(tmp_path):
    from scopt.formats.layouts import SC_CODE_OFFSET
    from scopt.formats.sc_v7 import SC7_REGION_MEMBER_DESCRIPTORS

    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    unit = _trusted_member_unit(snapshot, call)
    linked, _ = _link_trusted(snapshot, _trusted_two_member_region(snapshot, call, unit), unit)
    path, link = _write_trusted_md(tmp_path, snapshot, call, unit, linked)
    directory = read_sc7(path)
    decoded = decode_directory(directory)
    region = decoded["regions"][0]
    assert SC7_REGION_MEMBER_DESCRIPTORS == 4
    assert directory.trusted and region["flags"] == 7
    first, second = linked.members
    assert region["composite"]["member_descriptors"] is True
    assert region["composite"]["tc_size"] == second.tc_offset + second.tc_size - first.tc_offset
    assert [m["tb_offset"] for m in decoded["members"]] == [first.tb_offset, second.tb_offset]
    gap_at = first.tc_offset + first.tc_size
    with path.open("rb") as stream:
        stream.seek(SC_CODE_OFFSET + gap_at)
        assert stream.read(second.tc_offset - gap_at) == b"\x90" * (second.tc_offset - gap_at)
        stream.seek(SC_CODE_OFFSET + second.tb_offset)
        assert stream.read(snapshot.header.tb_size) == b"\0" * snapshot.header.tb_size
    # The legacy alias layout of the same region still reads (flags 3).
    legacy, _ = _link_trusted(snapshot, _trusted_two_member_region(snapshot, call, unit), unit,
                              {"grow": {unit.uid: ()}}, {unit.uid: _b3_registered_record(unit)})
    assert legacy.member_descriptor_refusal == "b3_registered_slot"
    legacy_path, _ = _write_trusted_md(tmp_path, snapshot, call, unit, legacy, "legacy.sc")
    assert decode_directory(read_sc7(legacy_path))["regions"][0]["flags"] == 3


@pytest.mark.parametrize(("where", "data", "needle"), [
    ("rel32", b"\x01\0\0\0", "residual exit"),
    ("opcode", b"\x90", "residual exit"),
    ("padding", b"\xcc", "padding is not NOPs"),
])
def test_v7_reader_rejects_member_descriptor_mutants(tmp_path, where, data, needle):
    from scopt.formats.layouts import SC_CODE_OFFSET

    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    unit = _trusted_member_unit(snapshot, call)
    linked, _ = _link_trusted(snapshot, _trusted_two_member_region(snapshot, call, unit), unit)
    second = linked.members[1]
    if where == "padding":
        # Padding is outside every member hash; only the reader's check sees it.
        path, _ = _write_trusted_md(tmp_path, snapshot, call, unit, linked)
        with path.open("r+b") as stream:
            stream.seek(SC_CODE_OFFSET + second.tc_offset - 1)
            stream.write(data)
    else:
        # A member whose directory hash covers the mutated code: only the
        # residual-slot check (QEMU's load-time check) can refuse it.
        at = 20 if where == "rel32" else 19
        code = bytearray(second.code)
        code[at:at + len(data)] = data
        broken = replace(linked, members=(linked.members[0],
                                          replace(second, code=bytes(code))))
        public = relocate_unit(unit, unit.tb_offset)
        link = SimpleNamespace(units=(public,), firmware_regions=(broken,),
                               firmware_table_offset=round_up(broken.end_offset, 8))
        link.firmware_table_size = directory_size(link.firmware_regions)
        path = tmp_path / "mutant.sc"
        write_sc7(path, snapshot.header, ((public.record, public.body),), link, check=False)
    with pytest.raises(ScFormatError, match=needle):
        read_sc7(path)


def test_v7_reader_rejects_skip_flags_outside_a_composed_directory(tmp_path):
    """Without an sc6 extension the B3 slot mask is 0, so even a member of a
    member-descriptor region may not carry skip flags."""
    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    unit = _trusted_member_unit(snapshot, call)
    linked, _ = _link_trusted(snapshot, _trusted_two_member_region(snapshot, call, unit), unit)
    assert linked.member_descriptors
    flagged = replace(linked, members=tuple(replace(m, skip_slots=2) for m in linked.members))
    # The writer's readback refuses the file it wrote ...
    with pytest.raises(ScFormatError, match="skip flags 0x2 != B3 slot mask 0x0"):
        _write_trusted_md(tmp_path, snapshot, call, unit, flagged)
    # ... and so does any later read of it.
    with pytest.raises(ScFormatError, match="v7 member 0: skip flags 0x2 != B3 slot mask 0x0"):
        read_sc7(tmp_path / "md.sc")


def test_v7_member_descriptors_need_trusted_contiguous_descriptors(tmp_path):
    from scopt.canon import fnv1a64
    from scopt.formats.layouts import SC_CODE_OFFSET

    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    unit = _trusted_member_unit(snapshot, call)
    linked, _ = _link_trusted(snapshot, _trusted_two_member_region(snapshot, call, unit), unit)
    public = relocate_unit(unit, unit.tb_offset)

    def build(region):
        link = SimpleNamespace(units=(public,), firmware_regions=(region,),
                               firmware_table_offset=round_up(region.end_offset, 8),
                               firmware_table_size=directory_size((region,)))
        return build_directory(link, {unit.uid: unit.tb_offset})

    build(linked)
    first, second = linked.members
    with pytest.raises(ScFormatError, match="trusted-region layout"):
        build(replace(linked, trusted=False))
    with pytest.raises(ScFormatError, match="descriptors are not contiguous"):
        build(replace(linked, members=(first, replace(second, tb_offset=first.tb_offset))))
    with pytest.raises(ScFormatError, match="code is not contiguous"):
        build(replace(linked, members=(first, replace(
            second, tc_offset=first.tc_offset + first.tc_size))))
    # A safe (version 2) directory never accepts the flag.
    path, _ = _write_trusted_md(tmp_path, snapshot, call, unit, linked)
    from scopt.formats.sc_v7 import SC7_OFFSET, _HEADER

    with path.open("r+b") as stream:
        stream.seek(SC7_OFFSET)
        raw = bytearray(stream.read(_HEADER.size))
        fields = list(_HEADER.unpack(bytes(raw)))
        fields[1] = 2                                    # safe directory version
        off, size = fields[7], fields[8]
        stream.seek(SC_CODE_OFFSET + off)
        table = stream.read(size)
        prefix = _HEADER.pack(*fields)
        fields[11] = fnv1a64(table, fnv1a64(prefix[:64]))
        stream.seek(SC7_OFFSET)
        stream.write(_HEADER.pack(*fields))
    with pytest.raises(ScFormatError, match="region 0: invalid bounds"):
        read_sc7(path)


def test_residual_goto_tb_slots_mirrors_qemu_load_check():
    from scopt.formats.sc_v5 import ScRecord
    from scopt.link import residual_goto_tb_slots

    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    unit = _trusted_member_unit(snapshot, call)
    code = bytearray(unit.code)
    ca = snapshot.header.code_address
    assert residual_goto_tb_slots(unit.record, bytes(code), 0x1000, code_address=ca) == (1, [])
    # An admitted internal edge owns the slot: excluded, even when patched.
    code[20:24] = b"\x10\0\0\0"
    assert residual_goto_tb_slots(unit.record, bytes(code), 0x1000, code_address=ca,
                                  internal_fields=(20,)) == (0, [])
    count, problems = residual_goto_tb_slots(unit.record, bytes(code), 0x1000, code_address=ca)
    assert count == 0 and "not a reset goto_tb" in problems[0]
    code = bytearray(unit.code)
    code[19] = 0x90
    assert "not a reset goto_tb" in residual_goto_tb_slots(
        unit.record, bytes(code), 0x1000, code_address=ca)[1][0]
    assert "aligned" in residual_goto_tb_slots(
        unit.record, unit.code, 0x1001, code_address=ca)[1][0]
    bad_reset = ScRecord.from_bytes(unit.record).replace(jmp_reset_offset=(25, 0xFFFF))
    assert "offsets" in residual_goto_tb_slots(bad_reset, unit.code, 0x1000)[1][0]
    in_prefix = ScRecord.from_bytes(unit.record).replace(jmp_insn_offset=(12, 0xFFFF),
                                                        jmp_reset_offset=(16, 0xFFFF))
    assert "offsets" in residual_goto_tb_slots(in_prefix, unit.code, 0x1000)[1][0]


def test_validate_v4_checks_residual_exit_slots_on_output_records(tmp_path):
    from scopt.validate import validate_firmware_v7

    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    unit = _trusted_member_unit(snapshot, call)
    linked, _ = _link_trusted(snapshot, _trusted_two_member_region(snapshot, call, unit), unit)
    path, link = _write_trusted_md(tmp_path, snapshot, call, unit, linked)
    (tmp_path / "cache.sc").write_bytes(path.read_bytes())
    snapshot.run_info = SimpleNamespace(flags=0)
    snapshot.runtime = None

    def row(link):
        v, _ = validate_firmware_v7(snapshot, link, tmp_path, skip_reimport=True)
        found, = [r for r in v.rows if r["check"] == "residual_exit_slots"]
        contract, = [r for r in v.rows if r["check"] == "firmware_region_contract"]
        return found, contract

    found, contract = row(link)
    assert found["result"] == "pass" and found["detail"] == (
        "2 chainable residual goto_tb slots in 1 member-descriptor regions "
        "(0 B3 slots skipped)")
    assert contract["result"] == "pass"
    second = linked.members[1]
    code = bytearray(second.code)
    code[20:24] = b"\x01\0\0\0"
    broken = replace(linked, members=(linked.members[0], replace(second, code=bytes(code))))
    found, contract = row(SimpleNamespace(**{**vars(link), "firmware_regions": (broken,)}))
    assert found["result"] == "fail" and "region 0 member 1" in found["detail"]
    assert contract["result"] == "fail"
    # Skip flags must equal the slots the output record gained over the
    # snapshot source (none in a firmware-only artifact).
    flagged = replace(linked, members=(linked.members[0], replace(second, skip_slots=1)))
    found, contract = row(SimpleNamespace(**{**vars(link), "firmware_regions": (flagged,)}))
    assert found["result"] == "fail" and "skip flags 0x1 != added slots 0x0" in found["detail"]
    assert contract["result"] == "fail"


# -- composed regions: member skip flags for B3-registered slots ------------

def test_trusted_region_rewrite_or_variant_source_keeps_member_descriptors():
    """B3 rewrites and variant clones keep the source's public record, so the
    pre-B3 member copy matches every slot QEMU arms: member descriptors even
    without the member-skip capability (frozen-binary compatible)."""
    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    unit = _trusted_member_unit(snapshot, call)
    region = _trusted_two_member_region(snapshot, call, unit)
    for fields in ({"rewrites": {unit.uid: ()}},
                   {"variants": (SimpleNamespace(src_uid=unit.uid, pred_uid="p"),)}):
        linked, _ = _link_trusted(snapshot, region, unit, {**fields, "member_slot_skip": False})
        assert linked.member_descriptors and linked.member_descriptor_refusal is None
        assert [m.skip_slots for m in linked.members] == [0, 0]
        assert (linked.skipped_b3_slots, linked.chainable_residual_slots) == (0, 2)


def test_trusted_region_skips_b3_registered_slots():
    from scopt.formats.sc_v5 import ScRecord
    from scopt.link import added_goto_tb_slots, residual_goto_tb_slots

    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    unit = _trusted_member_unit(snapshot, call)
    region = _trusted_two_member_region(snapshot, call, unit)
    out = _b3_registered_record(unit)
    assert added_goto_tb_slots(unit.record, out) == 2                  # slot 1 added
    assert added_goto_tb_slots(out, out) == 0                          # nothing added
    assert added_goto_tb_slots(out, unit.record) == 0                  # slot removed
    assert added_goto_tb_slots(ScRecord.from_bytes(unit.record), ScRecord.from_bytes(out)) == 2
    linked, _ = _link_trusted(snapshot, region, unit,
                              {"grow": {unit.uid: ()}, "member_slot_skip": True},
                              {unit.uid: out})
    assert linked.member_descriptors and linked.member_descriptor_refusal is None
    assert [m.skip_slots for m in linked.members] == [2, 2]
    # One skip bit per member descriptor (QEMU counts per descriptor built).
    assert linked.skipped_b3_slots == 2
    ca = snapshot.header.code_address
    residual = 0
    for member in linked.members:
        # Slot 1 names bytes the pre-B3 copy does not have: armed, it is a problem.
        assert residual_goto_tb_slots(out, member.code, member.tc_offset, code_address=ca)[1]
        count, problems = residual_goto_tb_slots(out, member.code, member.tc_offset,
                                                 code_address=ca, skip_slots=member.skip_slots)
        assert (count, problems) == (1, [])
        residual += count
    assert linked.chainable_residual_slots == residual == 2
    from scopt.link import LinkResultV2

    row, = LinkResultV2(True, snapshot.header, (), (), firmware_regions=(linked,)
                        ).to_json()["firmware_regions"]
    assert (row["skipped_b3_slots"], row["chainable_residual_slots"]) == (2, 2)
    assert row["member_descriptor_refusal"] is None


def test_internal_coincident_b3_slot_is_flagged(monkeypatch):
    """The region's own return guard and B3's guard are both appended at the
    pre-B3 code length, so a B3-registered slot can name the rel32 field of
    an internal edge.  The skip bit is still set and counted: QEMU tests it
    before the internal check."""
    from scopt.formats.sc_v5 import ScRecord
    from scopt.link import NativeLinkerV2, residual_goto_tb_slots

    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    unit = _trusted_member_unit(snapshot, call)
    grown = _b3_grown_unit(unit)
    field = ScRecord.from_bytes(grown.record).jmp_insn_offset[1]
    # Member 0's region return guard (unregistered): the same appended stub,
    # its rel32 patched to member 1; the record keeps the pre-B3 slots.
    rec = ScRecord.from_bytes(unit.record).replace(tc_size=grown.tc_size,
                                                   end_offset=grown.end_offset)
    guarded = replace(unit, code=grown.code, tc_size=grown.tc_size,
                      end_offset=grown.end_offset, record=rec.to_bytes(fix_checksum=True))
    monkeypatch.setattr(NativeLinkerV2, "_firmware_member_units",
                        lambda self, region, by_uid: ([guarded, unit], {(0, "ret"): field}))
    edge = SimpleNamespace(source_member=0, target_member=1, exit_id="ret", kind="return",
                           guest_target=0x1100)
    region = SimpleNamespace(**{**vars(_trusted_two_member_region(snapshot, call, unit)),
                                "edges": (edge,)})
    linked, _ = _link_trusted(snapshot, region, unit,
                              {"grow": {unit.uid: ()}, "member_slot_skip": True},
                              {unit.uid: grown.record})
    first, second = linked.members
    assert linked.member_descriptors and linked.member_descriptor_refusal is None
    assert first.patched_edges == (("ret", field, 1),)
    assert [m.skip_slots for m in linked.members] == [2, 2]
    assert linked.skipped_b3_slots == 2 and linked.chainable_residual_slots == 2
    ca = snapshot.header.code_address
    assert residual_goto_tb_slots(grown.record, first.code, first.tc_offset, code_address=ca,
                                  internal_fields=(field,), skip_slots=2) == (1, [])
    # Without the capability the same region keeps the shared descriptor.
    legacy, _ = _link_trusted(snapshot, region, unit, {"grow": {unit.uid: ()}},
                              {unit.uid: grown.record})
    assert legacy.member_descriptor_refusal == "b3_registered_slot"


@pytest.mark.parametrize("skip", [True, False])
@pytest.mark.parametrize("kind", ["grown", "rewrite", "untouched"])
def test_priced_chainable_implies_member_descriptors(kind, skip):
    from scopt.link import region_exits_chainable

    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    unit = _trusted_member_unit(snapshot, call)
    region = _trusted_two_member_region(snapshot, call, unit)
    fields, out_records, grown = {"member_slot_skip": skip}, None, frozenset()
    if kind == "grown":
        fields["grow"] = {unit.uid: ()}
        out_records = {unit.uid: _b3_registered_record(unit)}
        grown = frozenset({unit.uid})
    elif kind == "rewrite":
        fields["rewrites"] = {unit.uid: ()}
    linked, _ = _link_trusted(snapshot, region, unit, fields, out_records)
    if region_exits_chainable(region, grown):
        assert linked.member_descriptors
    # Pricing ignores the capability; the linker uses it.
    assert region_exits_chainable(region, grown) == (kind != "grown")
    assert linked.member_descriptors == (skip or kind != "grown")


def _continuation_exit_snapshot():
    key = TBKey(0x1000, None, 0x110, 0x800400, 0xff020000)
    call = bytes.fromhex("00f07ef8")

    def goto(dest):
        return {"sites": (ScmSite(1, 0, 4, Encoding.REL32, SiteKind.GOTO_TB, 0, 0, 0, 0),),
                "static_exits": (ScmStaticExit(0, 1, 0, dest),)}

    units = [
        SynthUnit(key, 0x100, b"\xe9\0\0\0\0", guest_bytes=call, vaddr=0x1000,
                  jmp_reset_offset=(5, 0xffff), jmp_insn_offset=(1, 0xffff), meta=goto(0x1100)),
        SynthUnit(key, 0x200, b"\xe9\0\0\0\0", guest_bytes=b"\x00\xbf", vaddr=0x1004,
                  jmp_reset_offset=(5, 0xffff), jmp_insn_offset=(1, 0xffff), meta=goto(0x1006)),
        SynthUnit(key, 0x300, b"\xff\xe0", guest_bytes=b"\x70\x47", vaddr=0x1100,
                  meta={"sites": (ScmSite(0, 0, 0, Encoding.NONE, SiteKind.GOTO_PTR,
                                          0, 0, 0, 0),)}),
    ]
    snapshot = fake_snapshot(units)
    snapshot._units = tuple(replace(u, origin="elf_verified") for u in snapshot.units())
    snapshot._by_uid = {u.uid: u for u in snapshot._units}
    image_bytes = bytearray(0x102)
    image_bytes[0:4] = call
    image_bytes[4:6] = b"\x00\xbf"
    image_bytes[0x100:0x102] = b"\x70\x47"
    image = GuestImage("synthetic", "sha256:" + "1" * 64,
                       ((0x1000, bytes(image_bytes)),), ((0x1000, 0x1102),), (), ())
    return snapshot, image


def test_receipt_reports_root_member_exits_without_changing_the_region(monkeypatch):
    import scopt.passes.firmware_inline as FI
    from scopt.guard import GuardOffsets

    snapshot, image = _continuation_exit_snapshot()
    calls = discover_firmware_calls(snapshot, image)
    call, = calls.calls
    plan = build_firmware_region(snapshot, image, calls, call)
    edges_before = plan.edges
    emitted = []
    ctx = SimpleNamespace(b3=SimpleNamespace(snapshot=snapshot, layout_bound=True,
                                             offsets=GuardOffsets()),
                          params={"mode": "exhaustive"}, snapshot_id="sid", bundle=None,
                          analysis=lambda name, sid: SimpleNamespace(value=calls),
                          budget=lambda: None)
    monkeypatch.setattr(FI, "guest_image_for", lambda snap, bundle: image)
    result = FI.FirmwareInline().run(None, ctx, SimpleNamespace(
        emit_firmware_region=emitted.append))
    receipt = json.loads(result.detail)
    emitted_plan, = emitted
    # Receipt-only rows: the emitted plan, its edges and region_id are unchanged.
    assert emitted_plan.edges == edges_before
    assert emitted_plan.region_id == plan.region_id
    row, = [r for r in receipt["residual"] if r["reason"] == "continuation_exit"]
    assert row == {"callsite_id": call.callsite_id, "source_member": 2,
                   "source_role": "continuation", "exit_id": "x0", "guest_target": 0x1006,
                   "reason": "continuation_exit", "exit_kind": "goto_tb",
                   "patchable_rel32": True}
    assert result.stats["residual_boundary_exits"] == 1.0
    assert receipt["embedded"][0]["residual_exits_chainable"] is False   # safe region
    keys = [(r["source_member"], r["exit_id"]) for r in receipt["residual"]]
    assert len(keys) == len(set(keys))


def test_boundary_exits_cover_caller_and_unvisited_root_continuation():
    from scopt.passes.firmware_inline import boundary_residual_exits

    def member(role, context=()):
        return SimpleNamespace(source_uid=role, role=role, inline_context=context)

    def exit_(id_):
        return SimpleNamespace(id=id_)

    plan = SimpleNamespace(
        entry_member=0,
        members=(member("caller"), member("callee", ("c",)), member("continuation"),
                 member("continuation", ("c",))),
        edges=(SimpleNamespace(source_member=0, exit_id="x1"),
               SimpleNamespace(source_member=1, exit_id="x0")))
    exits = {"caller": (exit_("x0"), exit_("x1")), "continuation": (exit_("x0"),)}
    rows = boundary_residual_exits(plan, lambda uid: exits.get(uid, ()))
    assert [(i, x.id, reason) for i, x, reason in rows] == [
        (0, "x0", "caller_exit"), (2, "x0", "continuation_exit")]
    # A root continuation with recorded edges was expanded: no boundary rows.
    visited = SimpleNamespace(**{**vars(plan), "edges": plan.edges + (
        SimpleNamespace(source_member=2, exit_id="x9"),)})
    assert [(i, x.id) for i, x, _ in boundary_residual_exits(
        visited, lambda uid: exits.get(uid, ()))] == [(0, "x0")]


# -- profit gate and profit-gated recursion ----------------------------------

def _fake_view(snapshot, rows, *, phase="steady"):
    """A ProfileView stand-in that passes exact_loop_profile for ``snapshot``."""
    from collections import Counter

    from scopt.formats.profile_v1 import METHOD, Count

    edges = [dict(src=s, dst=d, kind=k, count=n, flags=(), status="exact", phase=1)
             for s, d, k, n in rows]
    entries = Counter()
    for e in edges:
        entries[e["dst"]] += e["count"]
    data = SimpleNamespace(snapshot_id=snapshot.snapshot_id, complete=True, method=METHOD,
                           collection=dict(overflow=False, chain_disabled=True),
                           phase_id=lambda name: {"boot": 0, phase: 1}.get(name),
                           phase_reached=lambda pid: True, profile_id="sha256:" + "a" * 64)
    return SimpleNamespace(profile=data, profile_id=data.profile_id,
                           edges=lambda ph: tuple(edges),
                           count=lambda uid, ph, field="entries": Count(entries[uid], "exact"))


def _run_pass(monkeypatch, snapshot, image, params, view):
    import scopt.passes.firmware_inline as FI
    from scopt.guard import GuardOffsets

    calls = discover_firmware_calls(snapshot, image)
    emitted = []
    ctx = SimpleNamespace(b3=SimpleNamespace(snapshot=snapshot, layout_bound=True,
                                             offsets=GuardOffsets()),
                          params=params, snapshot_id=snapshot.snapshot_id, bundle=None,
                          analysis=lambda name, sid: SimpleNamespace(value=calls),
                          budget=lambda: None, profile=lambda phase: view)
    monkeypatch.setattr(FI, "guest_image_for", lambda snap, bundle: image)
    result = FI.FirmwareInline().run(None, ctx, SimpleNamespace(
        emit_firmware_region=emitted.append))
    detail = json.loads(result.detail) if result.detail.startswith("{") else result.detail
    return result, detail, emitted, calls


def test_hot_loop_profit_gate_records_not_profitable_residual(monkeypatch):
    snapshot, image = _continuation_exit_snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    caller, callee = call.source_uid, call.callee_candidates[0]
    cont = call.continuation_candidates[0]
    view = _fake_view(snapshot, [(caller, callee, "chain0", 5000),
                                 (callee, cont, "lookup_hit", 5000),
                                 (cont, caller, "chain0", 5000)])
    params = {"mode": "hot-loop", "min_call_count": 2, "residual_goto_tb": "unchained"}
    result, receipt, emitted, _ = _run_pass(monkeypatch, snapshot, image, params, view)
    # Safe region: the guarded return saves lookup - guard (6 ns); the
    # continuation's goto_tb back to the caller costs a dispatch (40 ns).
    assert not emitted
    row, = [r for r in receipt["residual"] if r["reason"] == "not_profitable"]
    assert row["callsite_id"] == call.callsite_id and row["observed_call_edges"] == 5000
    assert row["profit"]["terms"]["residual_goto_tb_unchained"] == 5000
    assert row["profit"]["net_ns_per_call"] < 0
    assert result.stats["refused:not_profitable"] == 1.0
    assert result.stats["cost_model_enabled"] == 1.0
    assert result.reason == "no_profitable_call"
    assert receipt["policy"]["cost_model"]["enabled"]
    assert receipt["policy"]["cost_model"]["constants"]["dispatch_ns"] == 40.0
    # Chained residual exits (the default for safe regions) admit it.
    result, receipt, emitted, _ = _run_pass(
        monkeypatch, snapshot, image, {"mode": "hot-loop", "min_call_count": 2}, view)
    assert len(emitted) == 1 and receipt["embedded"][0]["profit"]["net_ns"] > 0
    # cost_model=off embeds exactly the region of the unpriced pass.
    off, receipt_off, emitted_off, calls = _run_pass(
        monkeypatch, snapshot, image, {**params, "cost_model": "off"}, view)
    assert [p.region_id for p in emitted_off] == [
        build_firmware_region(snapshot, image, calls, call, expand_nested=False).region_id]
    assert "profit" not in receipt_off["embedded"][0]
    assert off.stats["cost_model_enabled"] == 0.0


@pytest.mark.parametrize("params", [{"cost_model": "maybe"}, {"residual_goto_tb": "x"},
                                    {"entry_chain": "y"}, {"cost_dispatch_ns": -1.0},
                                    {"nested": "deep"}])
def test_invalid_cost_params_fail(monkeypatch, params):
    snapshot, image = _continuation_exit_snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    view = _fake_view(snapshot, [(call.source_uid, call.callee_candidates[0], "chain0", 10),
                                 (call.callee_candidates[0], call.source_uid, "lookup_hit", 10)])
    result, _, emitted, _ = _run_pass(monkeypatch, snapshot, image,
                                      {"mode": "hot-loop", "min_call_count": 2, **params}, view)
    assert result.outcome.value == "failed" and result.reason == "invalid_params"
    assert not emitted


def test_exhaustive_cost_model_needs_a_profile(monkeypatch):
    snapshot, image = _continuation_exit_snapshot()
    result, _, emitted, _ = _run_pass(monkeypatch, snapshot, image,
                                      {"mode": "exhaustive", "cost_model": "profile"}, None)
    assert result.reason == "cost_model_exact_profile_required" and not emitted


def _nested_snapshot():
    key = TBKey(0x1000, None, 0x110, 0x800400, 0xff020000)
    root_call = bytes.fromhex("00f07ef8")       # 0x1000: bl 0x1100
    nested_call = bytes.fromhex("00f07ef8")     # 0x1100: bl 0x1200

    def goto(dest):
        return {"sites": (ScmSite(1, 0, 4, Encoding.REL32, SiteKind.GOTO_TB, 0, 0, 0, 0),),
                "static_exits": (ScmStaticExit(0, 1, 0, dest),)}

    ret = {"sites": (ScmSite(0, 0, 0, Encoding.NONE, SiteKind.GOTO_PTR, 0, 0, 0, 0),)}
    units = [
        SynthUnit(key, 0x100, b"\xe9\0\0\0\0", guest_bytes=root_call, vaddr=0x1000,
                  jmp_reset_offset=(5, 0xffff), jmp_insn_offset=(1, 0xffff), meta=goto(0x1100)),
        SynthUnit(key, 0x200, b"\x90", guest_bytes=b"\x00\xbf", vaddr=0x1004),
        SynthUnit(key, 0x300, b"\xe9\0\0\0\0", guest_bytes=nested_call, vaddr=0x1100,
                  jmp_reset_offset=(5, 0xffff), jmp_insn_offset=(1, 0xffff), meta=goto(0x1200)),
        SynthUnit(key, 0x400, b"\xff\xe0", guest_bytes=b"\x70\x47", vaddr=0x1104, meta=ret),
        SynthUnit(key, 0x500, b"\xff\xe0", guest_bytes=b"\x70\x47", vaddr=0x1200, meta=ret),
    ]
    snapshot = fake_snapshot(units)
    snapshot._units = tuple(replace(u, origin="elf_verified") for u in snapshot.units())
    snapshot._by_uid = {u.uid: u for u in snapshot._units}
    image_bytes = bytearray(0x202)
    image_bytes[0:4] = root_call
    image_bytes[4:6] = b"\x00\xbf"
    image_bytes[0x100:0x104] = nested_call
    image_bytes[0x104:0x106] = b"\x70\x47"
    image_bytes[0x200:0x202] = b"\x70\x47"
    image = GuestImage("synthetic", "sha256:" + "3" * 64,
                       ((0x1000, bytes(image_bytes)),), ((0x1000, 0x1204),), (), ())
    return snapshot, image


def _nested_rows(snapshot, image, nested_calls):
    calls = discover_firmware_calls(snapshot, image).calls
    root = next(c for c in calls if c.insn_vaddr == 0x1000)
    nested = next(c for c in calls if c.insn_vaddr == 0x1100)
    caller, f, cont = root.source_uid, root.callee_candidates[0], root.continuation_candidates[0]
    g, f_tail = nested.callee_candidates[0], nested.continuation_candidates[0]
    rows = [(caller, f, "chain0", 5000), (f_tail, cont, "lookup_hit", 5000),
            (cont, caller, "chain0", 5000)]
    if nested_calls:
        rows += [(f, g, "chain0", 5000), (g, f_tail, "lookup_hit", 5000)]
    return root, nested, rows


def test_hot_loop_recursion_inlines_profitable_nested_calls(monkeypatch):
    snapshot, image = _nested_snapshot()
    root, nested, rows = _nested_rows(snapshot, image, True)
    view = _fake_view(snapshot, rows)
    result, receipt, emitted, _ = _run_pass(
        monkeypatch, snapshot, image, {"mode": "hot-loop", "min_call_count": 2}, view)
    plan, = emitted
    assert plan.callsite.callsite_id == root.callsite_id
    # The nested callee and its continuation are private: both returns are
    # guarded edges, and the receipt shows the nested_gain decision.
    assert nested.callee_candidates[0] in plan.source_uids
    assert max(len(m.inline_context) for m in plan.members) == 2
    assert sum(e.kind == "return" and e.target_member is not None for e in plan.edges) == 2
    row = receipt["embedded"][0]
    assert row["expansion_depth"] == 2 and row["nested_inlined_calls"] == 1
    decision, = receipt["policy"]["nested_decisions"]
    assert decision["callsite_id"] == nested.callsite_id and decision["action"] == "inline"
    assert decision["gain_ns"] > 0
    assert receipt["policy"]["expand_nested"] is True and receipt["policy"]["max_depth"] is None
    assert result.stats["actual_expansion_depth"] == 2.0
    # The inner root is entered only through the outer region now: its
    # entries are captured, so it is refused instead of emitted dead.
    row, = [r for r in receipt["residual"] if r["reason"] == "not_profitable"]
    assert row["callsite_id"] == nested.callsite_id and row["profit"]["entries"] == 0
    # nested=public keeps one-level regions: the outer root never reaches a
    # return and does not pay off; the inner root does.
    _, receipt, emitted, _ = _run_pass(
        monkeypatch, snapshot, image,
        {"mode": "hot-loop", "min_call_count": 2, "nested": "public"}, view)
    assert [p.callsite.callsite_id for p in emitted] == [nested.callsite_id]
    row, = [r for r in receipt["residual"] if r["reason"] == "not_profitable"]
    assert row["callsite_id"] == root.callsite_id
    assert receipt["policy"]["max_depth"] == 1


def test_hot_loop_recursion_keeps_cold_nested_calls_public(monkeypatch):
    from scopt.analysis.firmware_profit import (DispatchCosts, NestedProfitExpansion,
                                                ProfitIndex, ProfitModel)
    from scopt.analysis.rtos_boundaries import ExpansionAction, ExpansionSite

    snapshot, image = _nested_snapshot()
    root, nested, rows = _nested_rows(snapshot, image, False)
    view = _fake_view(snapshot, rows)
    model = ProfitModel(ProfitIndex.build(view, "steady"), DispatchCosts())
    calls = discover_firmware_calls(snapshot, image)
    expansion = NestedProfitExpansion(model, snapshot, image, calls.calls, trusted=False)
    site = ExpansionSite("call", nested.source_uid, 0x1100, 0x1200)
    assert expansion(site, (root.callsite_id,)) == "nested_not_profitable"
    assert expansion(ExpansionSite("branch", nested.source_uid, 0x1100, 0x1104), ()) \
        is ExpansionAction.INLINE
    plan = build_firmware_region(snapshot, image, calls, root, expansion=expansion)
    assert nested.callee_candidates[0] not in plan.source_uids
    assert any(e.refusal_reason == "nested_not_profitable" for e in plan.edges)
    assert expansion.report()[0]["action"] == "public"


def test_firmware_hot_loop_counts_real_exit_null_transfers(monkeypatch):
    snapshot, image = _continuation_exit_snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    caller, callee = call.source_uid, call.callee_candidates[0]

    def edge(src, dst, kind, count=1001):
        return dict(src=src, dst=dst, kind=kind, count=count, flags=(), status="exact")

    # The callee returns to the caller's loop only through exit_null (a
    # BASEPRI write or similar): the root is now in a loop.
    rows = [edge(caller, callee, "chain0"), edge(callee, caller, "exit_null")]
    profile = SimpleNamespace(edges=lambda phase: rows)
    kept, residual, _ = _hot_loop_calls(snapshot, [call], profile, "steady", 1000)
    assert kept == [call]
    # exit_requested never closes a loop.
    rows[1] = edge(callee, caller, "exit_requested")
    kept, residual, _ = _hot_loop_calls(snapshot, [call], profile, "steady", 1000)
    assert residual[0]["reason"] == "outside_observed_loop"
    # A handler-mode source (cs_base HANDLER) ends returns with exit_tb(0)
    # for a possible EXC_RETURN: not a loop transfer.
    rows[1] = edge(callee, caller, "exit_null")
    handler = replace(snapshot.unit(callee), key=replace(snapshot.unit(callee).key, cs_base=1))
    snapshot._by_uid = {**snapshot._by_uid, callee: handler}
    kept, residual, _ = _hot_loop_calls(snapshot, [call], profile, "steady", 1000)
    assert residual[0]["reason"] == "outside_observed_loop"

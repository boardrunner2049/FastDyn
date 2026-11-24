"""Firmware policy registration, captured call selection and precise task-loop roots."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopt.analysis.firmware_calls import discover_firmware_calls
from scopt.decode.guest_symbols import GuestFunc
from scopt.passes.firmware_policy import (
    FirmwarePolicy, PolicyError, PolicyInput, PolicySelection, firmware_policy, resolve_selection,
)

from .test_firmware_inline import _snapshot


def test_firmlooper_chooses_only_closed_loop_call_instruction_without_profile():
    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    # Worker at 0x1000: bl 0x1100; b 0x1000. Init before/after the cycle cannot qualify.
    image = replace(image, funcs=(GuestFunc("worker", 0x1000, 0x1006),),
                    segments=((0x1000, bytes.fromhex("00f07ef8fce7")),))
    outside = replace(call, insn_vaddr=0x1100)
    missing = replace(call, callee_candidates=(), refusal_reasons=("missing_callee_capture",))
    recursive = replace(call, callee_candidates=(call.source_uid,))
    inputs = PolicyInput(snapshot, image, (outside, missing, recursive, call),
                         {"policy_options": {"task_entries": ["worker"]}})
    policy = firmware_policy("firmlooper")
    selected = policy.select(inputs)
    roots, residual = resolve_selection(inputs, selected)
    assert roots == [call]
    assert policy.expand_nested and not policy.requires_edges
    assert [row["reason"] for row in residual] == [
        "outside_persistent_task_loop", "missing_callee_capture", "recursive_call_edge"]
    assert "task_loops" in selected.evidence


def test_entry_point_policy_nominates_existing_sites(monkeypatch):
    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    inputs = PolicyInput(snapshot, image, (call,), {})
    policy = FirmwarePolicy(lambda data: PolicySelection((data.calls[0].callsite_id,)),
                            expand_nested=False)

    def entry_points(*, group, name):
        assert group == "scopt.firmware_inline_policies" and name == "my-policy"
        return [SimpleNamespace(load=lambda: policy)]

    monkeypatch.setattr("scopt.passes.firmware_policy.metadata.entry_points", entry_points)
    loaded = firmware_policy("my-policy")
    assert resolve_selection(inputs, loaded.select(inputs)) == ([call], [])
    uid = call.callsite_id
    for bad in (PolicySelection(("fabricated",)), PolicySelection((uid, uid)),
                PolicySelection((uid,), refusals={uid: "contradiction"})):
        with pytest.raises(PolicyError, match="call-site"):
            resolve_selection(inputs, bad)
    roots, residual = resolve_selection(inputs, PolicySelection(()))
    assert not roots and residual[0]["reason"] == "excluded_by_policy"


def test_policy_descriptor_controls_profile_and_nesting():
    hot = firmware_policy("hot-loop")
    assert hot.requires_edges and not hot.expand_nested
    all_calls = firmware_policy("exhaustive")
    assert all_calls.expand_nested and not all_calls.requires_edges


def test_private_trusted_return_patches_guard_success_and_keeps_lookup_on_miss():
    import struct

    from scopt.formats.enums import Encoding, SiteKind
    from scopt.formats.region_v1 import unit_exits
    from scopt.formats.sc_v5 import ScRecord
    from scopt.guard import GuardOffsets, decode_guard
    from scopt.link import B3Plan, NativeLinkerV2, trusted_return_guard_problem
    from scopt.model.firmware_region import FirmwareRegionEdge, FirmwareRegionMember
    from scopt.model.unit import Bindings, HelperBinding, PoolEntry, Site

    snapshot, image = _snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    snapshot.bundle = {"target": {"cpu": "cortex-m3"}}
    original = snapshot.unit(call.callee_candidates[0])
    # Ordinary TCG entry, dynamic return lookup and its pointer pool.
    prefix = bytes.fromhex("8b5df885db0f8c00000000c645fc01")
    tail = bytes.fromhex("4889efff1508000000ffe0")
    code = (prefix + tail).ljust(32, b"\x90") + struct.pack("<Q", 0x555500)
    sites = (Site(20, 18, 4, Encoding.POOL_INDIRECT, SiteKind.HELPER_CALL, 0, 0x555500, 0, 8),
             Site(24, 24, 0, Encoding.NONE, SiteKind.GOTO_PTR, 0, 0, 0, 0))
    pool = (PoolEntry(20, 32, 0, 1, 0, 0x555500),)
    helper = HelperBinding(0, "lookup_tb_ptr", "qemu", "lookup_tb_ptr", 0, 0x555500)
    end = original.tc_offset + len(code) + len(original.search)
    rec = ScRecord.from_bytes(original.record).replace(tc_size=len(code), end_offset=end)
    unit = replace(original, code=code, tc_size=len(code), end_offset=end, insn0_start=15,
                   pool_start=32, sites=sites, pool=pool, bindings=Bindings((helper,)),
                   record=rec.to_bytes(fix_checksum=True))
    exit_, = unit_exits(unit)
    offsets = GuardOffsets(envlayout_id="test-layout")
    assert trusted_return_guard_problem(unit, exit_.id, unit.key, None,
                                        snapshot.header.code_address, cpu_model="cortex-m3") == "return_guard_layout_unbound"
    assert trusted_return_guard_problem(unit, exit_.id, unit.key, offsets,
                                        snapshot.header.code_address, cpu_model="cortex-m3") is None
    for cpu in (None, "cortex-m4", "cortex-m33"):
        assert trusted_return_guard_problem(unit, exit_.id, unit.key, offsets, snapshot.header.code_address,
                                            cpu_model=cpu) == "return_guard_cpu_unsupported"
    member = FirmwareRegionMember(unit.uid, unit.content_hash, unit.key, (), "callee")
    edge = FirmwareRegionEdge(0, exit_.id, "return", 0, unit.vaddrs[0], guarded=True)
    region = SimpleNamespace(members=(member,), trusted=True, edges=(edge,), region_id="fixture",
                             callsite=call, entry_member=0, source_uids=(unit.uid,),
                             trusted_entry_offset=15)
    linker = NativeLinkerV2(snapshot, B3Plan(firmware_regions=(region,)), offsets=offsets,
                            tb_ret_abs=snapshot.header.code_address + 24)
    (linked,), _ = linker._link_firmware_regions(0x4000, {unit.uid: unit})
    ret, = linked.members
    _, field, _ = ret.patched_edges[0]
    assert field >= len(code)                 # success field in newly emitted guard
    displacement, = struct.unpack_from("<i", ret.code, field)
    assert field + 4 + displacement == len(prefix)
    # Decode the guard before linking the success edge. The fallback still executes
    # the captured lookup with the same pool slot. g_pc checks the post-return PC.
    unlinked = bytearray(ret.code)
    unlinked[field:field + 4] = bytes(4)
    guard = decode_guard(bytes(unlinked), len(code), tc_abs=0,
                         struct_off=ret.tb_offset-ret.tc_offset, offsets=offsets)
    assert guard.vaddrs == (unit.vaddrs[0],)
    assert guard.slots[0].field_off == field
    assert guard.f_bytes[:5] == tail[:5] and guard.f_bytes[-2:] == tail[-2:]
    assert guard.pool_slot_off == 32


def test_sealed_cold_entry_and_translation_context_agree_with_lookup():
    from scopt.analysis.firmware_calls import compatible_units, selected_firmware_units
    from scopt.graph import ONE_SHOT_CFLAGS

    snapshot, _ = _snapshot()
    unit = snapshot.units()[0]
    cold = replace(unit, key=replace(unit.key, cflags=0))
    pc = cold.vaddrs[0]
    assert selected_firmware_units((cold,))[0] == ()
    assert selected_firmware_units((cold,), {pc})[0] == (cold,)
    alias = replace(cold, uid="alias", key=replace(cold.key, page_addr0=0x80000))
    special = replace(cold, uid="single-step", slot=999,
                      key=replace(cold.key, cflags=ONE_SHOT_CFLAGS & -ONE_SHOT_CFLAGS))
    assert compatible_units((cold, special), special, pc) == (cold.uid,)
    assert set(compatible_units((cold, alias), cold, pc)) == {cold.uid, alias.uid}
    shadow = replace(cold, uid="unverified", slot=999, origin="unknown")
    assert compatible_units((cold, shadow), cold, pc) == ()
    assert selected_firmware_units((cold, shadow), {pc})[0] == ()


def test_branch_veneer_kernel_boundary_and_irq_reachability():
    from scopt.analysis.rtos_boundaries import ExpansionAction, TaskBoundaries

    _, image = _snapshot()
    # 1000: bl 1100, 1004: b 1104 (a veneer), 1100/1104: bx lr.
    data = bytearray(0x108)
    data[:6] = bytes.fromhex("00f07ef87ee0")
    data[0x100:0x108] = bytes.fromhex("704700bf70477047")
    image = replace(image, segments=((0x1000, bytes(data)),), exec_ranges=((0x1000, 0x1108),),
                    exception_entries=frozenset({0x1000}), funcs=(
        GuestFunc("board_exception", 0x1000, 0x1004), GuestFunc("veneer", 0x1004, 0x1006),
        GuestFunc("common", 0x1100, 0x1102), GuestFunc("ulTaskGenericNotifyTake", 0x1104, 0x1106),
        GuestFunc("process", 0x1106, 0x1108)))
    bounds = TaskBoundaries(image)
    assert bounds.classify(0x1100) is ExpansionAction.PUBLIC
    assert bounds.classify(0x1004) is ExpansionAction.SHARED
    assert bounds.classify(0x1106) is ExpansionAction.INLINE
    assert bounds.classify(0xffff) is ExpansionAction.PUBLIC


def test_ambiguous_offpage_destinations_keep_guest_lookup():
    from scopt.formats.region_v1 import unit_exits
    from scopt.model.unit import StaticExit

    snapshot, _ = _snapshot()
    tail = snapshot.units()[-1]
    unit = replace(tail, static_exits=(StaticExit(255, True, 0, 0x1100),
                                       StaticExit(255, True, 0, 0x1200)))
    assert unit_exits(unit)[0].static_dest_vaddr is None
    assert unit_exits(replace(unit, static_exits=unit.static_exits[:1]))[0].static_dest_vaddr == 0x1100


def test_cli_addon_is_application_and_fromisr_api_is_not_an_irq_root():
    from scopt.analysis.rtos_boundaries import ExpansionAction, TaskBoundaries

    _, image = _snapshot()
    data = bytearray(0x108)
    data[:6] = bytes.fromhex("00f07ef87047")  # FromISR API directly calls common.
    data[0x100:] = bytes.fromhex("00bf704770477047")
    image = replace(image, segments=((0x1000, bytes(data)),), exec_ranges=((0x1000, 0x1108),),
                    funcs=(GuestFunc("xQueueSendFromISR", 0x1000, 0x1006),
                           GuestFunc("common", 0x1100, 0x1104),
                           GuestFunc("FreeRTOS_CLIProcessCommand", 0x1104, 0x1106),
                           GuestFunc("MPU_ulTaskGenericNotifyTake", 0x1106, 0x1108)))
    bounds = TaskBoundaries(image)
    assert bounds.classify(0x1000) is ExpansionAction.PUBLIC
    assert bounds.classify(0x1102) is ExpansionAction.INLINE
    assert bounds.classify(0x1104) is ExpansionAction.INLINE
    assert bounds.classify(0x1106) is ExpansionAction.SHARED
    irq = TaskBoundaries(replace(image, exception_entries=frozenset({0x1000})))
    assert irq.classify(0x1102) is ExpansionAction.PUBLIC

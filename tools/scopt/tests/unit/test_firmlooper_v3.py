"""FirmLooper v3: analysis revision 2, task_end, isb_boundary and the selection summary.

Revision 1 (modes firmlooper and firmlooper-v2) must stay byte-identical; the
golden values below were recorded with the code before revision 2 existed.
Top-level imports are limited to names that predate revision 2, so the
revision-1 fixtures and digests can be regenerated against an older tree.
"""

import hashlib
import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest
from keystone import KS_ARCH_ARM, KS_MODE_THUMB, Ks

from scopt.analysis.firmware_calls import discover_firmware_calls
from scopt.analysis.task_loops import discover_task_loops
from scopt.decode.guest_symbols import GuestFunc, GuestImage
from scopt.formats.enums import Encoding, SiteKind
from scopt.formats.scm_v1 import ScmSite, ScmStaticExit
from scopt.ids import TBKey
from scopt.passes.firmware_policy import PolicyInput, firmware_policy, resolve_selection

from ..synth.builders import SynthUnit
from ..synth.fake_snapshot import fake_snapshot
from .test_firmware_inline import _fake_view, _run_pass, _snapshot
from .test_task_loops import _FOREVER, _image


def asm(pc, source):
    return bytes(Ks(KS_ARCH_ARM, KS_MODE_THUMB).asm(source, pc)[0])


# -- word jump tables (A3) ----------------------------------------------------

# task @0x1040:
# loop: bl wait_for_input; cmp r0,#2; bhi.w dflt; adr r2,table; ldr.w pc,[r2,r0,lsl #2]
# table @0x1050: .word case0|1, case1|1, case2|1
# case0 @105c: bl wait_for_input; b loop   case1 @1062: bl wait_for_input; b loop
# case2 @1068: b done   dflt @106a: b loop   done @106c: pop {r4,pc}
_WORD_TABLE = bytearray.fromhex(
    "00f01ef8 0228 00f21080 01a2 52f820f0 5d000000 63000000 69000000"
    " 00f010f8 eee7 00f00df8 ebe7 00e0 e9e7 10bd")
_WORD_TABLE[0x10:0x1c] = b"".join((0x1000 + o).to_bytes(4, "little") for o in (0x5d, 0x63, 0x69))
_TABLE_MAPPING = ((0x1000, "t"), (0x100c, "d"), (0x1020, "t"), (0x1050, "d"), (0x105c, "t"))


def _table_image(task=bytes(_WORD_TABLE), mapping=_TABLE_MAPPING):
    return replace(_image(task), mapping=mapping)


def _patched(offset, raw, task=_WORD_TABLE):
    data = bytearray(task)
    data[offset:offset + len(raw)] = raw
    return bytes(data)


def test_word_jump_table_cases_join_the_task_cfg_in_revision_2():
    rev1 = discover_task_loops(_table_image(), include_terminal=True)
    assert rev1.tasks[0]["jump_tables"] == {}
    assert rev1.tasks[0]["status"] == "no_closed_task_loop"
    assert "analysis_revision" not in rev1.report()
    loops = discover_task_loops(_table_image(), include_terminal=True, revision=2)
    task, = loops.tasks
    assert task["jump_tables"] == {"0x104c": ["0x105c", "0x1062", "0x1068"]}
    # The switch is no longer an unknown exit, but the loop still ends in a return.
    assert task["status"] == "no_closed_task_loop"
    assert loops.report()["cfg_rules"] == {"jump_tables": ["tbb", "tbh", "ldr_pc_word"],
                                           "patched_entries": True}
    loops = discover_task_loops(_table_image(), include_terminal=True, revision=2, task_end=True)
    assert loops.call_pcs == {0x1040, 0x105c, 0x1062}
    cycle, = [c for c in loops.tasks[0]["cycles"] if c["status"] == "selected"]
    assert cycle["classification"] == "persistent_with_terminal_exit"
    assert cycle["terminal_kinds"] == ["task_return"]
    assert loops.tasks[0]["selected_call_pcs"] == ["0x1040", "0x105c", "0x1062"]
    assert loops.report()["loop_rule"] == (
        "dominating header with closed, classified-terminal or task-return exits")


@pytest.mark.parametrize("variant", ["no_data_mapping", "arm_state_word", "unguarded",
                                     "base_is_index", "branch_to_adr", "branch_to_ldr"])
def test_word_jump_table_guards(variant):
    image = {
        "no_data_mapping": lambda: _table_image(mapping=_TABLE_MAPPING[:3]),
        # Word 0 without the Thumb bit.
        "arm_state_word": lambda: _table_image(_patched(0x10, (0x105c).to_bytes(4, "little"))),
        # bhi.w dflt -> nop; nop: the index is no longer bounded.
        "unguarded": lambda: _table_image(_patched(0x6, bytes.fromhex("00bf00bf"))),
        # cmp r0; adr r0,table; ldr.w pc,[r0,r0,lsl #2]
        "base_is_index": lambda: _table_image(_patched(0xa, asm(0x104a, "adr r0, #4")
                                                       + asm(0x104c, "ldr.w pc,[r0,r0,lsl #2]"))),
        # dflt: b adr / b ldr bypasses the range check.
        "branch_to_adr": lambda: _table_image(_patched(0x2a, asm(0x106a, "b 0x104a"))),
        "branch_to_ldr": lambda: _table_image(_patched(0x2a, asm(0x106a, "b 0x104c"))),
    }[variant]()
    loops = discover_task_loops(image, include_terminal=True, revision=2, task_end=True)
    assert loops.tasks[0]["jump_tables"] == {}
    assert not loops.call_pcs


# -- task_end ------------------------------------------------------------------

# head: bl wait_for_input; cmp r0,#0; bne head; bx lr; dead: b dead
_FINITE = bytes.fromhex("00f01ef8 0028 fbd1 7047 fee7")


def test_task_return_is_terminal_only_for_recovered_roots_with_task_end():
    rev1 = discover_task_loops(_image(_FINITE), include_terminal=True)
    assert not rev1.call_pcs
    assert "blocking_exits" not in rev1.tasks[0]["cycles"][0]
    loops = discover_task_loops(_image(_FINITE), include_terminal=True, revision=2)
    assert not loops.call_pcs
    cycle = loops.tasks[0]["cycles"][0]
    assert cycle["status"] == "cycle_has_exit"
    assert cycle["blocking_exits"] == {"0x1048": ["normal_return"]}
    assert loops.tasks[0]["task_end"] is False
    loops = discover_task_loops(_image(_FINITE), include_terminal=True, revision=2, task_end=True)
    assert loops.call_pcs == {0x1040}
    assert loops.tasks[0]["task_end"] is True
    assert loops.tasks[0]["cycles"][0]["terminal_kinds"] == ["task_return"]
    # head: bl; cmp r0,#0; it eq; bxeq lr; b head -- a predicated return stays unknown.
    predicated = bytes.fromhex("00f01ef8 0028 08bf 7047 f9e7")
    loops = discover_task_loops(_image(predicated), include_terminal=True, revision=2,
                                task_end=True)
    assert not loops.call_pcs
    assert loops.tasks[0]["cycles"][0]["blocking_exits"] == {"unknown": ["return_in_cycle"]}
    # The same body reached only through an explicit entry is not a FreeRTOS task.
    explicit = discover_task_loops(_image(_FINITE, pointer=0), ["task"], include_terminal=True,
                                   revision=2, task_end=True)
    assert not explicit.call_pcs
    assert explicit.tasks[0]["task_end"] is False
    assert explicit.report()["task_end"] is True
    with pytest.raises(ValueError, match="revision 2"):
        discover_task_loops(_image(_FINITE), include_terminal=True, task_end=True)
    with pytest.raises(ValueError, match="revision"):
        discover_task_loops(_image(_FINITE), revision=3)


def test_task_return_forms():
    from capstone import arm as A

    from scopt.analysis.task_loops import _is_return
    from scopt.decode.thumb_sem import _md

    def one(source):
        return next(_md().disasm(asm(0x1000, source), 0x1000))

    for source in ("bx lr", "pop {r4, pc}", "ldr pc, [sp], #4"):
        assert _is_return(one(source)), source
    # capstone 5 reports the post-index offset as a third operand.
    assert len(one("ldr pc, [sp], #4").operands) == 3
    for source in ("bx r3", "pop {r4, r5}", "ldr pc, [sp, #4]!", "ldr pc, [sp, #4]",
                   "ldr pc, [r1], #4"):
        assert not _is_return(one(source)), source
    # Older capstone folds the offset into a two-operand post-indexed form.
    insn = one("ldr pc, [sp, #4]!")
    two_operand = SimpleNamespace(id=insn.id, cc=insn.cc, operands=insn.operands,
                                  writeback=True, post_index=True)
    assert _is_return(two_operand)
    assert not _is_return(_namespace_with(two_operand, post_index=False))
    # Predicated returns count only for the return_in_cycle diagnosis.
    _, bxeq = _md().disasm(bytes.fromhex("08bf7047"), 0x1000)
    assert bxeq.cc == A.ARM_CC_EQ
    assert not _is_return(bxeq) and _is_return(bxeq, conditional=True)


def _namespace_with(ns, **changes):
    return SimpleNamespace(**{**vars(ns), **changes})


def test_one_shot_and_halt_loops_stay_rejected_under_task_end():
    # init: bl; subs r4,#1; bne init; main: bl; b main
    init = bytes.fromhex("00f01ef8 013c fbd1 00f01af8 fce7")
    loops = discover_task_loops(_image(init), include_terminal=True, revision=2, task_end=True)
    assert loops.call_pcs == {0x1048}
    first = next(c for c in loops.tasks[0]["cycles"] if c["header"] == "0x1040")
    assert first["status"] == "cycle_has_exit"
    assert first["blocking_exits"] == {"0x1048": ["enters_other_cycle"]}
    # loop: bl; subs r4,#1; bne loop; done: b done -- no halt seeds.
    halt = bytes.fromhex("00f01ef8 013c fbd1 fee7")
    loops = discover_task_loops(_image(halt), include_terminal=True, revision=2, task_end=True)
    assert not loops.call_pcs
    first = next(c for c in loops.tasks[0]["cycles"] if c["header"] == "0x1040")
    assert first["blocking_exits"] == {"0x1048": ["enters_other_cycle"]}
    # A bounded cleanup cycle on the exit path cannot establish termination,
    # even though the return after it is a task_return terminal.
    cleanup = bytes.fromhex("00f01ef8 0028 fbd1 013c fdd1 7047")
    loops = discover_task_loops(_image(cleanup), include_terminal=True, revision=2, task_end=True)
    assert not loops.call_pcs
    first = next(c for c in loops.tasks[0]["cycles"] if c["header"] == "0x1040")
    assert first["blocking_exits"] == {"0x1048": ["enters_other_cycle"]}
    without = discover_task_loops(_image(cleanup), include_terminal=True, revision=2)
    first = next(c for c in without.tasks[0]["cycles"] if c["header"] == "0x1040")
    assert first["blocking_exits"] == {"0x1048": ["enters_other_cycle", "normal_return"]}


def test_nested_cycle_of_selected_loop_is_reported_as_nested():
    loops = discover_task_loops(_image(_FOREVER), revision=2)
    assert loops.call_pcs == {0x104a}
    outer, inner = loops.tasks[0]["cycles"]
    assert outer["status"] == "selected" and "terminal_kinds" not in outer
    assert inner["header"] == "0x1046" and inner["status"] == "nested_in_selected_loop"
    assert "blocking_exits" not in inner
    assert loops.tasks[0]["selected_call_pcs"] == ["0x104a"]
    # Revision 1 keeps its original row.
    assert discover_task_loops(_image(_FOREVER)).tasks[0]["cycles"][1]["status"] == "cycle_has_exit"


# -- boundaries (A1, A2) ---------------------------------------------------------

def _boundary_image(reset_name="Reset_Handler", *, thumb_entries=frozenset({0x1000, 0x1030}),
                    exception_entries=frozenset({0x1030}), patch_target=0x1020,
                    systick_calls=0x1040, patch_symbol=False):
    # 1000 reset: bl main(1010)     1010 main: bl helper(1020)
    # 1030 SysTick: bl tick(1040)   1020/1040: bx lr
    # 1100: b.w <patch_target> (BOLT entry patch, no symbol, inside $d)
    data = bytearray(0x104)
    data[0x00:0x04] = asm(0x1000, "bl 0x1010")
    data[0x10:0x14] = asm(0x1010, "bl 0x1020")
    data[0x20:0x22] = asm(0x1020, "bx lr")
    data[0x30:0x34] = asm(0x1030, f"bl {systick_calls:#x}")
    data[0x40:0x42] = asm(0x1040, "bx lr")
    data[0x100:0x104] = asm(0x1100, f"b.w {patch_target:#x}")
    funcs = [GuestFunc(reset_name, 0x1000, 0x1004), GuestFunc("main", 0x1010, 0x1014),
             GuestFunc("helper", 0x1020, 0x1022), GuestFunc("SysTick_Handler", 0x1030, 0x1034),
             GuestFunc("tick", 0x1040, 0x1042)]
    if patch_symbol:
        funcs.append(GuestFunc("helper.bolt.org", 0x1100, 0x1104))
    return GuestImage("boundaries", "", ((0x1000, bytes(data)),), ((0x1000, 0x1104),),
                      tuple(funcs), ((0x1000, "t"), (0x1050, "d")),
                      exception_entries=exception_entries, thumb_entries=thumb_entries,
                      text_sections=((0x1000, 0x1104),))


def test_reset_entry_is_not_an_interrupt_root_in_revision_2():
    from scopt.analysis.rtos_boundaries import ExpansionAction, TaskBoundaries

    public, inline = ExpansionAction.PUBLIC, ExpansionAction.INLINE
    # Named reset handler, and an ISR-like name on vector word 1 (thumb_entries only).
    for image in (_boundary_image(), _boundary_image("Board_Reset_Handler"),
                  _boundary_image(thumb_entries=frozenset())):
        assert TaskBoundaries(image).classify(0x1020) is public
        rev2 = TaskBoundaries(image, revision=2)
        assert rev2.classify(0x1020) is inline
        assert rev2.classify(0x1040) is public
    assert TaskBoundaries(_boundary_image(), revision=2).reset_entries == {0x1000}
    # A reset handler that is also installed as an exception vector stays visible.
    vector = _boundary_image(exception_entries=frozenset({0x1000, 0x1030}))
    assert TaskBoundaries(vector, revision=2).classify(0x1020) is public
    assert TaskBoundaries(_boundary_image(), revision=2).explain(0x1040) == {
        "action": "public", "rule": "exception_visible", "function": "tick",
        "via": ["SysTick_Handler", "tick"]}
    assert TaskBoundaries(_boundary_image(), revision=2).explain(0x1010) == {
        "action": "inline", "rule": "application", "function": "main"}
    assert TaskBoundaries(_boundary_image()).explain(0x1020)["via"] == [
        "Reset_Handler", "main", "helper"]
    with pytest.raises(ValueError, match="revision"):
        TaskBoundaries(_boundary_image(), revision=0)


def test_bolt_entry_patch_resolves_to_the_moved_function():
    from scopt.analysis.rtos_boundaries import ExpansionAction, TaskBoundaries

    image = _boundary_image(thumb_entries=frozenset())
    assert TaskBoundaries(image).classify(0x1100) is ExpansionAction.PUBLIC
    bounds = TaskBoundaries(image, revision=2)
    assert bounds.classify(0x1100) is ExpansionAction.INLINE
    assert bounds.explain(0x1100) == {"action": "inline", "rule": "application",
                                      "function": "helper", "patched_entry": "0x1100"}
    assert bounds.patched_entries_resolved == {0x1100}
    # A handler that calls the patched entry makes the moved body exception-visible.
    handler = _boundary_image("board_reset", thumb_entries=frozenset(), systick_calls=0x1100)
    bounds = TaskBoundaries(handler, revision=2)
    assert bounds.classify(0x1020) is ExpansionAction.PUBLIC
    assert bounds.explain(0x1100)["via"] == ["SysTick_Handler", "helper"]
    assert TaskBoundaries(handler).classify(0x1020) is ExpansionAction.INLINE
    # A 4-byte symbol at the patch without a $t mapping also resolves.
    symbol = _boundary_image(thumb_entries=frozenset(), patch_symbol=True)
    assert TaskBoundaries(symbol, revision=2).classify(0x1100) is ExpansionAction.INLINE
    # Outside an executable section, or into a function interior: unknown owner.
    outside = replace(image, text_sections=())
    assert TaskBoundaries(outside, revision=2).classify(0x1100) is ExpansionAction.PUBLIC
    interior = _boundary_image(thumb_entries=frozenset(), patch_target=0x1012)
    assert TaskBoundaries(interior, revision=2).explain(0x1100) == {
        "action": "public", "rule": "unknown_owner", "function": None}


def test_resolve_veneer_follows_patched_entry():
    from scopt.analysis.rtos_instances import Program

    data = bytearray(0x104)
    worker = asm(0x1000, "loop: movs r0,#0; bl 0x1100; b loop")
    data[:len(worker)] = worker
    data[0x80:0x82] = asm(0x1080, "bx lr")
    data[0x100:0x104] = asm(0x1100, "b.w 0x1080")
    image = GuestImage("patched", "", ((0x1000, bytes(data)),), ((0x1000, 0x1104),),
                       (GuestFunc("worker", 0x1000, 0x1000 + len(worker)),
                        GuestFunc("vTaskDelete", 0x1080, 0x1082)),
                       ((0x1000, "t"), (0x1100, "d")), text_sections=((0x1000, 0x1104),))
    rev1, rev2 = Program(image), Program(image, revision=2)
    assert 0x1002 not in rev1.api and 0x1100 not in rev1.terminals
    assert rev2.api[0x1002] == "vTaskDelete" and 0x1100 in rev2.terminals
    assert rev1.body(0x1101) is None and rev2.body(0x1101) == 0x1080
    # Revision 1 sees an ordinary call and a persistent loop; revision 2 sees
    # the task delete itself (compare test_veneer_to_self_delete_does_not_create_a_loop).
    assert discover_task_loops(image, ["worker"], include_terminal=True).call_pcs == {0x1002}
    assert not discover_task_loops(image, ["worker"], include_terminal=True,
                                   revision=2).instruction_pcs


def test_patched_entry_requires_an_exact_unconditional_branch():
    from scopt.analysis.rtos_cfg import patched_entry
    from scopt.decode.thumb_sem import _md

    image = _boundary_image(thumb_entries=frozenset())
    assert patched_entry(image, 0x1100, _md()) == 0x1020
    # A real function start inside $t is not a patch; neither is a conditional branch.
    assert patched_entry(image, 0x1010, _md()) is None
    data = bytearray(image.segments[0][1])
    data[0x100:0x104] = asm(0x1100, "beq.w 0x1020")
    assert patched_entry(replace(image, segments=((0x1000, bytes(data)),)), 0x1100, _md()) is None


# -- policy ----------------------------------------------------------------------

def _policy_inputs(snapshot, image, calls, options=None, **params):
    if options is not None:
        params["policy_options"] = options
    return PolicyInput(snapshot, image, tuple(calls), params)


def test_firmlooper_v3_options_and_mode_identity():
    from scopt.passes.firmware_policy import PolicyError, _v3_loops

    snapshot, _ = _snapshot()
    image = _table_image()
    v3 = firmware_policy("firmlooper-v3")
    assert v3.expand_nested and not v3.requires_edges and not v3.cost_model_default
    for bad in ({"task_end": "yes"}, {"boundaries": 1}, {"task_end": True, "include_terminal": False},
                {"unknown": True}):
        with pytest.raises(PolicyError):
            _v3_loops(_policy_inputs(snapshot, image, (), bad))
    for mode in ("firmlooper", "firmlooper-v2"):
        with pytest.raises(PolicyError, match="unknown FirmLooper options"):
            firmware_policy(mode).discover_loops(_policy_inputs(snapshot, image, (), {"task_end": True}))
    loops = _v3_loops(_policy_inputs(snapshot, image, (), {"task_end": False, "include_terminal": False}))
    assert not loops.call_pcs and loops.report()["task_end"] is False
    # A --params override replaces the whole policy_options dict (shallow merge):
    # v3's rules come from its mode, not from the options.
    inputs = _policy_inputs(snapshot, image, (), {"boundaries": False})
    selected = v3.select(replace(inputs, task_loops=v3.discover_loops(inputs)))
    report = selected.evidence["task_loops"]
    assert report["task_end"] is True and report["analysis_revision"] == 2
    assert selected.evidence["rtos_boundaries"] is False
    assert selected.evidence["boundary_rules"] == {"isb_boundary": True}
    assert _v3_loops(_policy_inputs(snapshot, image, (), {"signature_reference": "ref.elf"}))


def test_firmlooper_v3_signature_reference_is_honoured(monkeypatch):
    snapshot, image = _snapshot()
    params = {"mode": "firmlooper-v3", "policy_options": {"signature_reference": "/nonexistent.elf"}}
    result, detail, emitted, _ = _run_pass(monkeypatch, snapshot, image, params, None)
    assert result.reason == "invalid_params" and "RTOS signatures" in detail and not emitted


def _goto(dest, insn=0):
    return {"sites": (ScmSite(1, 0, 4, Encoding.REL32, SiteKind.GOTO_TB, 0, 0, insn, 0),),
            "static_exits": (ScmStaticExit(0, 1, insn, dest),)}


_RET = {"sites": (ScmSite(0, 0, 0, Encoding.NONE, SiteKind.GOTO_PTR, 0, 0, 0, 0),)}


def _units_snapshot(units, image_bytes, funcs, *, base=0x1000, exception_entries=frozenset()):
    snapshot = fake_snapshot(units)
    snapshot._units = tuple(replace(u, origin="elf_verified") for u in snapshot.units())
    snapshot._by_uid = {u.uid: u for u in snapshot._units}
    end = base + len(image_bytes)
    image = GuestImage("synthetic", "sha256:" + "5" * 64, ((base, bytes(image_bytes)),),
                       ((base, end),), tuple(funcs), (), exception_entries=exception_entries)
    return snapshot, image


def _isb_snapshot():
    """worker: bl store_and_yield; b worker.  store_and_yield: str; dsb; isb | bx lr."""
    key = TBKey(0x1000, None, 0x110, 0x800400, 0xff020000)
    callee = asm(0x1100, "str r1,[r0]; dsb sy; isb sy")
    units = [
        SynthUnit(key, 0x100, b"\xe9\0\0\0\0", guest_bytes=asm(0x1000, "bl 0x1100"), vaddr=0x1000,
                  jmp_reset_offset=(5, 0xffff), jmp_insn_offset=(1, 0xffff), meta=_goto(0x1100)),
        SynthUnit(key, 0x200, b"\xe9\0\0\0\0", guest_bytes=asm(0x1004, "b 0x1000"), vaddr=0x1004,
                  jmp_reset_offset=(5, 0xffff), jmp_insn_offset=(1, 0xffff), meta=_goto(0x1000)),
        # Three guest instructions (2, 4 and 4 bytes); the goto_tb belongs to the ISB.
        SynthUnit(key, 0x300, b"\xe9\0\0\0\0", guest_bytes=callee, vaddr=0x1100, icount=3,
                  jmp_reset_offset=(5, 0xffff), jmp_insn_offset=(1, 0xffff),
                  meta={**_goto(0x110a, insn=2),
                        "insns": (((0x100, 0, 0), 0, 1), ((0x102, 0, 0), 1, 2),
                                  ((0x106, 0, 0), 2, 5))}),
        SynthUnit(key, 0x400, b"\xff\xe0", guest_bytes=asm(0x110a, "bx lr"), vaddr=0x110a, meta=_RET),
    ]
    data = bytearray(0x10c)
    data[0:6] = asm(0x1000, "bl 0x1100; b 0x1000")
    data[0x100:0x10a] = callee
    data[0x10a:0x10c] = asm(0x110a, "bx lr")
    return _units_snapshot(units, data, (GuestFunc("worker", 0x1000, 0x1006),
                                         GuestFunc("store_and_yield", 0x1100, 0x110c)))


def test_firmlooper_v3_isb_member_exit_is_a_side_exit():
    from scopt.analysis.firmware_regions import build_firmware_region

    snapshot, image = _isb_snapshot()
    edges = {}
    for mode in ("firmlooper-v2", "firmlooper-v3"):
        policy = firmware_policy(mode)
        inputs = _policy_inputs(snapshot, image, (), {"task_entries": ["worker"]})
        loops = policy.discover_loops(inputs)
        calls = discover_firmware_calls(snapshot, image, verified_entry_pcs=loops.call_pcs)
        inputs = replace(inputs, calls=calls.calls, task_loops=loops)
        selection = policy.select(inputs)
        (root,), _ = resolve_selection(inputs, selection)
        plan = build_firmware_region(snapshot, image, calls, root, expansion=selection.expansion)
        isb_member = next(i for i, m in enumerate(plan.members) if m.source_uid == root.callee_candidates[0])
        edges[mode], = [e for e in plan.edges if e.source_member == isb_member]
    assert edges["firmlooper-v2"].kind == "branch" and edges["firmlooper-v2"].target_member is not None
    v3 = edges["firmlooper-v3"]
    assert (v3.kind, v3.target_member, v3.refusal_reason, v3.guest_target) == (
        "side_exit", None, "isb_boundary", 0x110a)


def _receipt_snapshot(*, worker="worker", callee="helper", handler=False, callee_hash=None):
    """worker: bl callee; nop; b worker.  Optional SysTick_Handler: bl callee."""
    key = TBKey(0x1000, None, 0x110, 0x800400, 0xff020000)
    units = [
        SynthUnit(key, 0x100, b"\xe9\0\0\0\0", guest_bytes=asm(0x1000, "bl 0x1100"), vaddr=0x1000,
                  jmp_reset_offset=(5, 0xffff), jmp_insn_offset=(1, 0xffff), meta=_goto(0x1100)),
        SynthUnit(key, 0x200, b"\xe9\0\0\0\0", guest_bytes=b"\x00\xbf", vaddr=0x1004,
                  jmp_reset_offset=(5, 0xffff), jmp_insn_offset=(1, 0xffff), meta=_goto(0x1006)),
        SynthUnit(key, 0x300, b"\xff\xe0", guest_bytes=b"\x70\x47", vaddr=0x1100, meta=_RET,
                  guest_hash=callee_hash),
    ]
    data = bytearray(0x204)
    data[0:8] = asm(0x1000, "bl 0x1100; nop; b 0x1000")
    data[0x100:0x102] = b"\x70\x47"
    funcs = [GuestFunc(worker, 0x1000, 0x1008), GuestFunc(callee, 0x1100, 0x1102)]
    if handler:
        data[0x200:0x204] = asm(0x1200, "bl 0x1100")
        funcs.append(GuestFunc("SysTick_Handler", 0x1200, 0x1204))
    return _units_snapshot(units, data, funcs)


_V3 = {"mode": "firmlooper-v3", "policy_options": {"task_entries": ["worker"]}}


@pytest.mark.parametrize("case", ["shared", "public", "public_caller"])
def test_firmlooper_v3_receipt_reports_boundaries_and_verdict(monkeypatch, case):
    snapshot, image = {
        "shared": lambda: _receipt_snapshot(callee="xQueueReceive"),
        "public": lambda: _receipt_snapshot(handler=True),
        "public_caller": lambda: _receipt_snapshot(worker="prvIdleTask"),
    }[case]()
    params = {**_V3, "policy_options": {"task_entries": ["prvIdleTask" if case == "public_caller"
                                                         else "worker"]}}
    result, receipt, emitted, _ = _run_pass(monkeypatch, snapshot, image, params, None)
    assert not emitted
    row, = [r for r in receipt["residual"] if r["callsite_id"] == receipt["discovered"][0]["callsite_id"]]
    expected = {
        "shared": ("rtos_shared_public_lookup",
                   {"action": "shared", "rule": "kernel_api_name", "function": "xQueueReceive"}),
        "public": ("rtos_public_boundary",
                   {"action": "public", "rule": "exception_visible", "function": "helper",
                    "via": ["SysTick_Handler", "helper"]}),
        "public_caller": ("rtos_public_caller",
                          {"action": "inline", "rule": "application", "function": "helper"}),
    }[case]
    assert (row["reason"], row["boundary"]) == expected
    if case == "public_caller":
        assert row["caller_boundary"] == {"action": "public", "rule": "scheduler_name",
                                          "function": "prvIdleTask"}
    else:
        assert "caller_boundary" not in row
    assert result.reason == "only_rtos_boundary_calls"
    summary = receipt["policy"]["selection_summary"]
    assert summary["verdict"] == "only_rtos_boundary_calls"
    task, = summary["tasks"].values()
    assert task == {"entry": "0x1000", "status": "selected", "loop_call_sites": 1, "captured": 1,
                    "embedded": 0, "refused": {expected[0]: 1}, "verdict": "only_rtos_boundary_calls"}
    assert receipt["policy"]["analysis_revision"] == 2
    assert receipt["policy"]["task_loops"]["analysis_revision"] == 2


def test_firmlooper_v3_embeds_inline_root_with_summary(monkeypatch):
    snapshot, image = _receipt_snapshot()
    result, receipt, emitted, _ = _run_pass(monkeypatch, snapshot, image, _V3, None)
    assert result.reason == "firmware_regions_emitted" and len(emitted) == 1
    row, = receipt["embedded"]
    assert row["boundary"] == {"action": "inline", "rule": "application", "function": "helper"}
    assert receipt["policy"]["selection_summary"]["verdict"] == "embedded"
    assert receipt["policy"]["boundary_rules"] == {"reset_entries": [], "patched_entries_resolved": 0,
                                                   "isb_boundary": True}
    assert not any(r["reason"] == "isb_boundary" for r in receipt["residual"])


def test_firmlooper_v3_profit_refusal_keeps_no_profitable_call(monkeypatch):
    snapshot, image = _receipt_snapshot()
    call, = discover_firmware_calls(snapshot, image).calls
    caller, callee = call.source_uid, call.callee_candidates[0]
    cont = call.continuation_candidates[0]
    view = _fake_view(snapshot, [(caller, callee, "chain0", 5000), (callee, cont, "lookup_hit", 5000),
                                 (cont, caller, "chain0", 5000)])
    params = {**_V3, "cost_model": "profile", "residual_goto_tb": "unchained"}
    result, receipt, emitted, _ = _run_pass(monkeypatch, snapshot, image, params, view)
    assert not emitted and result.reason == "no_profitable_call"
    task, = receipt["policy"]["selection_summary"]["tasks"].values()
    assert task["refused"] == {"not_profitable": 1} and task["verdict"] == "planned_not_embedded"


def test_firmlooper_v3_failed_planned_root_does_not_take_the_verdict(monkeypatch):
    import scopt.passes.firmware_inline as FI

    # The callee's guest bytes do not match the image: the plan is incomplete.
    snapshot, image = _receipt_snapshot(callee_hash=0x1234)
    result, receipt, emitted, _ = _run_pass(monkeypatch, snapshot, image, _V3, None)
    assert not emitted and result.stats["refused:guest_decode_failed"] >= 1.0
    assert result.reason == "no_eligible_call"
    task, = receipt["policy"]["selection_summary"]["tasks"].values()
    assert task["verdict"] == "planned_not_embedded"
    monkeypatch.setattr(FI, "firmlooper_summary",
                        lambda *args: {"verdict": "only_rtos_boundary_calls", "tasks": {}})
    result, _, _, _ = _run_pass(monkeypatch, snapshot, image, _V3, None)
    assert result.reason == "no_eligible_call"


def test_firmlooper_summary_verdicts():
    from scopt.passes.firmware_policy import firmlooper_summary

    def task(name, status="selected", calls=()):
        return {"name": name, "entry": "0x1", "status": status,
                "selected_call_pcs": [hex(pc) for pc in calls]}

    def call(pc, uid):
        return SimpleNamespace(insn_vaddr=pc, callsite_id=uid)

    def verdict(tasks, calls=(), residual=(), embedded=(), planned=()):
        loops = SimpleNamespace(tasks=list(tasks))
        return firmlooper_summary(loops, list(calls), list(residual), set(embedded), set(planned))

    assert verdict([])["verdict"] == "no_task_registrations"
    assert verdict([task("a", "no_closed_task_loop")])["verdict"] == "no_closed_task_loop"
    assert verdict([task("a", "no_closed_task_loop"), task("b")])["verdict"] == "no_task_loop_calls"
    assert verdict([task("a", calls=[1]), task("b")])["verdict"] == "task_loop_calls_not_captured"
    shared = {"callsite_id": "c1", "reason": "rtos_shared_public_lookup"}
    member = {"callsite_id": "c2", "reason": "observation_boundary", "source_member": 1}
    out = verdict([task("a", calls=[1]), task("a", "no_closed_task_loop")], [call(1, "c1")],
                  [shared, member])
    assert out["verdict"] == "only_rtos_boundary_calls"
    assert set(out["tasks"]) == {"a", "a@0x1"}
    assert out["tasks"]["a"]["refused"] == {"rtos_shared_public_lookup": 1}
    other = {"callsite_id": "c2", "reason": "missing_callee_capture"}
    assert verdict([task("a", calls=[1, 2])], [call(1, "c1"), call(2, "c2")],
                   [shared, other])["verdict"] == "no_eligible_call"
    out = verdict([task("a", calls=[1, 2]), task("b", calls=[3])],
                  [call(1, "c1"), call(2, "c2"), call(3, "c3")], [shared], embedded=["c2"],
                  planned=["c2", "c3"])
    assert out["verdict"] == "embedded"
    assert [t["verdict"] for t in out["tasks"].values()] == ["embedded", "planned_not_embedded"]


def test_sealed_firmlooper_v3_pipeline_loads():
    from scopt.manager import PipelineError, load_pipeline

    pipeline = load_pipeline("sealed-firmlooper-v3")
    inline, = pipeline.passes
    assert inline.name == "firmware-inline"
    assert inline.params == {"mode": "firmlooper-v3", "trust": "user", "interrupts": "disabled",
                             "icount": "ignored", "planner": "whopr", "partitions": 4, "workers": 4}
    with pytest.raises(PipelineError):
        load_pipeline("sealed-firmlooper-v3-b3")


# -- revision 1 stays byte-identical -------------------------------------------------

def _revision2_features_snapshot():
    """One image with every revision-2 feature, around the _snapshot() call units.

    1000 Reset_Handler: bl 0x1100 (patch); nop; bl main; b .
    1020 main: movw r0,#0x1041; bl xTaskCreate; bl helper; bx lr
    1040 task: the word-table loop, case0 calling the patch at 0x1100
    10d0 process, 10e0 xTaskCreate, 10f0 helper: bx lr
    1100: b.w helper (BOLT entry patch, unsymbolized, inside $d)
    """
    snapshot, _ = _snapshot()
    data = bytearray(0x104)
    reset = asm(0x1000, "bl 0x1100; nop; bl 0x1020; b 0x100a")
    data[:len(reset)] = reset
    main = asm(0x1020, "movw r0,#0x1041; bl 0x10e0; bl 0x10f0; bx lr")
    data[0x20:0x20 + len(main)] = main
    task = bytearray(_WORD_TABLE)
    task[0x0:0x4] = asm(0x1040, "bl 0x10d0")
    task[0x1c:0x20] = asm(0x105c, "bl 0x1100")
    task[0x22:0x26] = asm(0x1062, "bl 0x10d0")
    data[0x40:0x40 + len(task)] = task
    for pc in (0x10d0, 0x10e0, 0x10f0):
        data[pc - 0x1000:pc - 0x1000 + 2] = asm(pc, "bx lr")
    data[0x100:0x104] = asm(0x1100, "b.w 0x10f0")
    funcs = (GuestFunc("Reset_Handler", 0x1000, 0x1000 + len(reset)),
             GuestFunc("main", 0x1020, 0x1020 + len(main)),
             GuestFunc("task", 0x1040, 0x1040 + len(task)),
             GuestFunc("process", 0x10d0, 0x10d2), GuestFunc("xTaskCreate", 0x10e0, 0x10e2),
             GuestFunc("helper", 0x10f0, 0x10f2))
    image = GuestImage("revision2-features", "sha256:" + "1" * 64, ((0x1000, bytes(data)),),
                       ((0x1000, 0x1104),), funcs,
                       ((0x1000, "t"), (0x1050, "d"), (0x105c, "t"), (0x1100, "d")),
                       thumb_entries=frozenset({0x1000}), text_sections=((0x1000, 0x1104),))
    return snapshot, image


def _loop_calls(snapshot, image):
    call, = discover_firmware_calls(snapshot, image).calls
    return (call, *(replace(call, insn_vaddr=pc, target_vaddr=target)
                    for pc, target in ((0x1040, 0x10d0), (0x105c, 0x1100), (0x1062, 0x10d0))))


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _revision1_receipt_sha(result, receipt):
    """Digest of the receipt parts FirmLooper owns when nothing is planned.

    Other receipt fields (cost model constants, WHOPR rows, linker facts) can
    change with unrelated passes; these cannot without changing FirmLooper.
    """
    return _sha({"reason": result.reason, "discovered": receipt["discovered"],
                 "residual": receipt["residual"], "embedded": receipt["embedded"],
                 "task_loops": receipt["policy"]["task_loops"],
                 "rtos_boundaries": receipt["policy"]["rtos_boundaries"],
                 "mode": receipt["policy"]["mode"]})


def _revision1_outputs(mode):
    snapshot, image = _revision2_features_snapshot()
    calls = _loop_calls(snapshot, image)
    policy = firmware_policy(mode)
    inputs = PolicyInput(snapshot, image, calls, {"mode": mode})
    selected = policy.select(replace(inputs, task_loops=policy.discover_loops(inputs)))
    index = {call.callsite_id: hex(call.insn_vaddr) for call in calls}
    return {"roots": [index[uid] for uid in selected.roots],
            "refusals": {index[uid]: reason for uid, reason in selected.refusals.items()},
            "site_evidence": {index[uid]: row for uid, row in selected.site_evidence.items()},
            "evidence_keys": sorted(selected.evidence),
            "task_loops_sha256": _sha(selected.evidence["task_loops"])}


# Recorded at 2f4dce8, before analysis revision 2 existed.
_REVISION1_GOLDEN = {
    "firmlooper": {
        "roots": [],
        "refusals": {"0x1000": "outside_persistent_task_loop", "0x1040": "outside_persistent_task_loop",
                     "0x105c": "outside_persistent_task_loop", "0x1062": "outside_persistent_task_loop"},
        "site_evidence": {}, "evidence_keys": ["rtos_boundaries", "task_loops"],
        "task_loops_sha256": "9b05702ffd60f019724155f5374b948681a169bdd7f167731e143cb2551ecb88",
    },
    "firmlooper-v2": {
        "roots": [],
        "refusals": {"0x1000": "outside_persistent_task_loop", "0x1040": "outside_persistent_task_loop",
                     "0x105c": "outside_persistent_task_loop", "0x1062": "outside_persistent_task_loop"},
        "site_evidence": {}, "evidence_keys": ["rtos_boundaries", "task_loops"],
        "task_loops_sha256": "9b05702ffd60f019724155f5374b948681a169bdd7f167731e143cb2551ecb88",
    },
}
_REVISION1_PASS_GOLDEN = {
    "firmlooper": "44daca1e22d5d7c2be5832534e0fb5998fcc1f647d96ec7bbeb946e4d59a9391",
    "firmlooper-v2": "03b40472f8cd63be6d0af0bcdc2c627edaa56693642fb3da366005ef1a79f445",
}


@pytest.mark.parametrize("mode", ["firmlooper", "firmlooper-v2"])
def test_firmlooper_v1_v2_outputs_unchanged(monkeypatch, mode):
    outputs = _revision1_outputs(mode)
    assert outputs == _REVISION1_GOLDEN[mode]
    snapshot, image = _revision2_features_snapshot()
    result, receipt, emitted, _ = _run_pass(monkeypatch, snapshot, image, {"mode": mode}, None)
    assert result.reason == "no_eligible_call" and not emitted
    report = receipt["policy"]["task_loops"]
    assert "analysis_revision" not in report and "selection_summary" not in receipt["policy"]
    assert not any("blocking_exits" in c or "terminal_kinds" in c
                   for t in report["tasks"] for c in t["cycles"])
    assert not any("boundary" in row for row in receipt["residual"])
    assert _revision1_receipt_sha(result, receipt) == _REVISION1_PASS_GOLDEN[mode]


def test_firmlooper_v3_uses_revision_2_on_the_same_image():
    snapshot, image = _revision2_features_snapshot()
    calls = _loop_calls(snapshot, image)
    policy = firmware_policy("firmlooper-v3")
    inputs = PolicyInput(snapshot, image, calls, {"mode": "firmlooper-v3"})
    loops = policy.discover_loops(inputs)
    selected = policy.select(replace(inputs, task_loops=loops))
    index = {call.callsite_id: hex(call.insn_vaddr) for call in calls}
    assert sorted(index[uid] for uid in selected.roots) == ["0x1040", "0x105c", "0x1062"]
    patched = next(uid for uid, pc in index.items() if pc == "0x105c")
    assert selected.site_evidence[patched] == {"boundary": {
        "action": "inline", "rule": "application", "function": "helper", "patched_entry": "0x1100"}}
    assert selected.evidence["boundary_rules"]["reset_entries"] == ["0x1000"]
    assert loops.tasks[0]["jump_tables"] == {"0x104c": ["0x105c", "0x1062", "0x1068"]}


# Real FirmBOLT -O4 images: revision-1 task loops and boundary classes, digested
# at 2f4dce8. Set SCOPT_REVISION1_ELFS (os.pathsep-separated) to check other
# copies; an ELF whose sha256 is not listed here is skipped.
_REAL_ELFS = ("/data/scfix-campaign/packet/o4.elf", "/data/scfix-campaign/uart-w/bolt.elf",
              "/data/scfix-campaign/cli-w/fw/O0-fb/RTOSDemo.out")
_REAL_GOLDEN = {
    # /data/scfix-campaign/cli-w/fw/O0-fb/RTOSDemo.out
    "sha256:8c2aed59cc3d058bdf9d4f03e0434ef40cf467955027e0404071f1855a1d895d":
        "32c7d69c8e11b1c0174260ef98d2e7b49f8e13c8885a41ce920749edae94fa7c",
    # /data/scfix-campaign/packet/o4.elf
    "sha256:d638b4a0d3d1baf6d318762793ead824cc746d36a817a0205318cad9d0b65113":
        "a562a28a9a47b6d920f25161a09a39fe91b4f4c43c4ae9f83474d916e4162600",
    # /data/scfix-campaign/uart-w/bolt.elf
    "sha256:9a5dbe6d6e8fad511afb11b32319cdd4ddfd43d0e06cc90bc843052b286ad089":
        "6e06c6ec9dfc07c007046651b33b55b3ea897103efa8ee09de6c79e438a7cbe6",
}


def revision1_digest(image):
    """Digest of every revision-1 analysis output FirmLooper v1/v2 consume."""
    from capstone import arm as A

    from scopt.analysis.rtos_boundaries import TaskBoundaries
    from scopt.decode.thumb_sem import _md

    out = {}
    for terminal in (False, True):
        loops = discover_task_loops(image, include_terminal=terminal)
        out[f"report_{terminal}"] = loops.report()
        out[f"calls_{terminal}"] = sorted(loops.call_pcs)
    bounds = TaskBoundaries(image)
    targets = {f.start for f in image.funcs}
    for lo, hi in image.code_ranges():
        for insn in _md().disasm(image.read(lo, hi - lo) or b"", lo):
            if insn.id in (A.ARM_INS_B, A.ARM_INS_BL) and insn.operands \
                    and insn.operands[0].type == A.ARM_OP_IMM:
                targets.add(insn.operands[0].imm & ~1)
    out["classes"] = {hex(pc): bounds.classify(pc).value for pc in sorted(targets)}
    registered = {int(r["entry"], 16) for r in out["report_True"]["registrations"]
                  if r.get("status") == "resolved"}
    out["task_source"] = {hex(pc): bounds.classify_task_source(pc, registered).value
                          for pc in out["calls_True"]}
    return _sha(out)


def test_revision1_analysis_unchanged_on_real_images():
    from scopt.decode.guest_symbols import load_guest_image

    env = os.environ.get("SCOPT_REVISION1_ELFS")
    paths = env.split(os.pathsep) if env else _REAL_ELFS
    checked = 0
    for path in paths:
        if not os.path.isfile(path):
            continue
        image = load_guest_image(path)
        if image.sha256 not in _REAL_GOLDEN:
            continue
        assert revision1_digest(image) == _REAL_GOLDEN[image.sha256], path
        checked += 1
    if not checked:
        pytest.skip("no FirmBOLT -O4 ELF with a recorded revision-1 digest is present")

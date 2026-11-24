"""Thumb fixtures for FreeRTOS registration provenance and static loop membership."""

from dataclasses import replace

from scopt.analysis.task_loops import discover_task_loops
from scopt.decode.guest_symbols import GuestFunc, GuestImage


def _image(task: bytes, *, alternate_entry=False, pointer=0x1041, api_veneer=False):
    # 1000: b{eq} registration | nop
    # 1002: ldr r0,[pc,#8]; 1004: bl xTaskCreate (or veneer)
    # 1008: bx lr; 100c: .word task|1
    buf = bytearray(0x82)
    buf[0:4] = bytes.fromhex("00d0" if alternate_entry else "00bf") + bytes.fromhex("0248")
    buf[4:8] = bytes.fromhex("00f014f8" if api_veneer else "00f00cf8")
    buf[8:10] = bytes.fromhex("7047")
    buf[12:16] = pointer.to_bytes(4, "little")
    buf[0x20:0x22] = bytes.fromhex("7047")
    buf[0x30:0x32] = bytes.fromhex("f6e7")  # b 0x1020, unsymbolized public entry
    buf[0x38:0x3a] = bytes.fromhex("02e0")  # b 0x1040, unsymbolized task entry
    buf[0x40:0x40 + len(task)] = task
    buf[0x80:0x82] = bytes.fromhex("7047")
    funcs = (GuestFunc("register_tasks", 0x1000, 0x100a),
             GuestFunc("xTaskCreate", 0x1020, 0x1022),
             GuestFunc("task", 0x1040, 0x1040 + len(task)),
             GuestFunc("wait_for_input", 0x1080, 0x1082))
    return GuestImage("thumb-fixture", "", ((0x1000, bytes(buf)),),
                      ((0x1000, 0x1082),), funcs, ((0x1000, "t"), (0x100c, "d"), (0x1020, "t")))


# bl wait_for_input (outside cycle); loop: movs r0,#3
# inner: subs r0,#1; bne inner; bl wait_for_input; b loop
_FOREVER = bytes.fromhex("00f01ef8 0320 0138 fdd1 00f019f8 f9e7")


def test_registered_task_uses_closed_outer_cycle_with_nested_counted_loop():
    loops = discover_task_loops(_image(_FOREVER))
    assert loops.call_pcs == {0x104a}
    assert loops.instruction_pcs == {0x1044, 0x1046, 0x1048, 0x104a, 0x104e}
    assert loops.registrations == [{"registrar": "register_tasks", "call_pc": "0x1004",
                                    "api_entry": "0x1020", "status": "resolved",
                                    "literal": "0x100c", "pointer": "0x1041", "entry": "0x1040"}]


def test_kept_public_veneers_and_thumb_pointer_resolve_to_implementation():
    loops = discover_task_loops(_image(_FOREVER, pointer=0x1039, api_veneer=True))
    assert loops.call_pcs == {0x104a}
    assert loops.registrations[0]["entry"] == "0x1040"


def test_rejects_missing_thumb_state_bypassed_load_and_interior_entry():
    for opts in ({"pointer": 0x1040}, {"alternate_entry": True}, {"pointer": 0x1047}):
        loops = discover_task_loops(_image(_FOREVER, **opts))
        assert not loops.tasks
        assert loops.registrations[0]["status"] == "unresolved_task_pointer"


def test_explicit_root_uses_the_same_loop_checks():
    loops = discover_task_loops(_image(_FOREVER, pointer=0),
                                ["task", "0x1041", "unknown", "0x1047"])
    assert loops.call_pcs == {0x104a}
    assert [r["status"] for r in loops.registrations[1:]] == [
        "resolved", "resolved", "unknown_or_ambiguous_task_entry", "unknown_or_ambiguous_task_entry"]


def test_rejects_finite_loop_and_unreachable_spin():
    # head: bl wait_for_input; cmp r0,#0; bne head; bx lr; dead: b dead
    loops = discover_task_loops(_image(bytes.fromhex("00f01ef8 0028 fbd1 7047 fee7")))
    assert not loops.call_pcs and not loops.instruction_pcs
    assert [c["status"] for c in loops.tasks[0]["cycles"]] == ["cycle_has_exit"]


def test_rejects_cycle_with_predicated_indirect_return_or_task_deletion():
    # head: bl wait_for_input; cmp r0,#0; it eq; bxeq lr; b head
    loops = discover_task_loops(_image(bytes.fromhex("00f01ef8 0028 08bf 7047 f9e7")))
    assert not loops.instruction_pcs
    assert loops.tasks[0]["cycles"][0]["outgoing"] == ["unknown_or_termination"]
    image = _image(_FOREVER)
    image = replace(image, funcs=image.funcs[:-1] + (GuestFunc("vTaskDelete", 0x1080, 0x1082),))
    assert not discover_task_loops(image).call_pcs


def test_irreducible_two_entry_sink_is_reported_and_skipped():
    # cmp r0,#0; beq right; left: bl wait_for_input; right: b left
    loops = discover_task_loops(_image(bytes.fromhex("0028 01d0 00f01cf8 fce7")))
    assert not loops.call_pcs
    assert loops.tasks[0]["cycles"][0]["status"] == "multiple_entry_cycle"


def test_function_symbol_on_public_entry_veneer_keeps_task_loop_visible():
    image = _image(_FOREVER, pointer=0x1039)
    alias = GuestFunc("task_public_entry", 0x1038, 0x103a)
    image = replace(image, funcs=tuple(sorted((*image.funcs, alias), key=lambda f: f.start)))
    loops = discover_task_loops(image, ["task_public_entry"])
    assert len(loops.tasks) == 1
    assert loops.call_pcs == {0x104a}
    assert loops.registrations[0]["pointer"] == "0x1039"
    assert loops.registrations[0]["entry"] == loops.registrations[1]["entry"] == "0x1040"


def test_dead_registration_does_not_add_a_root():
    image = _image(_FOREVER)
    addr, raw = image.segments[0]
    image = replace(image, segments=((addr, bytes.fromhex("7047") + raw[2:]),))
    loops = discover_task_loops(image)
    assert not loops.tasks and not loops.call_pcs
    assert loops.registrations[0]["status"] == "unreachable_or_unresolved_registration"

"""Small Thumb counterexamples for value recovery, task exits and switch bounds."""
from dataclasses import replace

import pytest
from keystone import Ks, KS_ARCH_ARM, KS_MODE_THUMB

from scopt.analysis.task_loops import discover_task_loops
from scopt.decode.guest_symbols import GuestFunc, GuestImage


def asm(pc, source):
    return bytes(Ks(KS_ARCH_ARM, KS_MODE_THUMB).asm(source, pc)[0])


def fixture(defs, words=(), mapping=((0x1000, "t"),), readonly=()):
    data = bytearray(0x220)
    funcs = []
    for pc, name, instructions in sorted(defs):
        raw = asm(pc, instructions) if isinstance(instructions, str) else instructions
        data[pc-0x1000:pc-0x1000+len(raw)] = raw
        funcs.append(GuestFunc(name, pc, pc+len(raw)))
    for addr, word in words:
        data[addr-0x1000:addr-0x1000+4] = word.to_bytes(4, 'little')
    return GuestImage('fixture', '', ((0x1000, bytes(data)),), ((0x1000, 0x1200),),
                      tuple(funcs), mapping, readonly_ranges=readonly)


def setup(init):
    return fixture([(0x1000, 'init', init), (0x1080, 'xTaskCreate', 'bx lr'),
                    (0x1100, 'worker', 'bl 0x1180; b 0x1100'),
                    (0x1180, 'process', 'bx lr')])


@pytest.mark.parametrize("prep", [
    "movw r3,#0x1101; movt r3,#0; mov r0,r3",
    "movw r3,#0x1101; sub sp,#8; str r3,[sp,#4]; ldr r0,[sp,#4]; add sp,#8",
    "cmp r1,#0; beq other; movw r0,#0x1101; b join; other: movw r0,#0x1101; join: nop",
    "adr.w r1,data; ldr r0,[r1]; b after; .align 2; data: .word 0x1101; after: nop",
])
def test_recovers_known_arguments_across_scalar_stack_and_joins(prep):
    image = replace(setup(prep + "; bl 0x1080; bx lr"), readonly_ranges=((0x1000, 0x1100),))
    assert discover_task_loops(image).call_pcs == {0x1100}


@pytest.mark.parametrize("prep", [
    "cmp r1,#0; beq other; movw r0,#0x1101; b join; other: movw r0,#0x1181; join: nop",
    "movw r2,#0x1101; str r2,[sp]; strb r3,[sp,#1]; ldr r0,[sp]",
    "movw r0,#0x1101; blx r3",
    "movw r4,#0x1200; ldr r0,[r4]",
])
def test_conflicting_unknown_or_partially_overwritten_argument_is_refused(prep):
    img = setup(prep + "; bl 0x1080; bx lr")
    assert not discover_task_loops(img).tasks


def test_multiple_wrapper_contexts_stay_distinct():
    img = fixture([(0x1000, 'init', 'movw r1,#0x1101; bl 0x1040; movw r1,#0x1121; bl 0x1040; bx lr'),
                   (0x1040, 'spawn', 'mov r0,r1; b.w 0x1080'),
                   (0x1080, 'xTaskCreate', 'bx lr'),
                   (0x1100, 'a', 'bl 0x1180; b 0x1100'),
                   (0x1120, 'b', 'bl 0x1180; b 0x1120'),
                   (0x1180, 'process', 'bx lr')])
    loops = discover_task_loops(img)
    assert loops.call_pcs == {0x1100, 0x1120}
    resolved = [r for r in loops.registrations if r['status'] == 'resolved']
    assert [r['entry'] for r in sorted(resolved, key=lambda r:r['wrapper_calls'])] == ["0x1100", "0x1120"]


def test_creation_handle_flow_and_mutable_storage_are_distinct():
    img = fixture([(0x1000, 'start', 'movs r2,#2; bl 0x1080; movw r4,#0x1200; str r0,[r4]; bl 0x10a0; bx lr'),
                   (0x1040, 'worker', 'movw r4,#0x1200; ldr r0,[r4]; bl 0x10a0; bx lr'),
                   (0x1080, 'xQueueGenericCreate', 'bx lr'), (0x10a0, 'xQueueReceive', 'bx lr')])
    report = discover_task_loops(img).report()
    assert [s['kind'] for s in report['objects']] == ['semaphore']
    assert report['handle_stores'] == [{'pc':'0x100a', 'storage':'0x1200', 'creation_pc':'0x1002'}]
    local, later = report['handle_uses']
    assert local['creation_pc'] == '0x1002' and local['status'] == 'local_creation_flow'
    assert later['status'] == 'global_storage' and later['storage'] == '0x1200'


def test_unresolved_initialization_callback_returns_to_persistent_loop():
    img = fixture([(0x1000,'worker','blx r3; loop: bl 0x1080; b loop'),
                   (0x1080,'process','bx lr')])
    assert discover_task_loops(img, ['worker']).call_pcs == {0x1002}


def test_veneer_to_self_delete_does_not_create_a_loop():
    img = fixture([(0x1000,'worker','loop: movs r0,#0; bl 0x1040; b loop'),
                   (0x1040,'veneer','b.w 0x1080'), (0x1080,'vTaskDelete','bx lr')])
    assert not discover_task_loops(img,['worker'],include_terminal=True).instruction_pcs


def test_terminal_cleanup_class_requires_all_paths_and_excludes_init_and_cleanup():
    text = 'bl 0x1040; loop: bl 0x1040; cmp r0,#0; bne loop; bl 0x1040; movs r0,#0; bl 0x1080; bx lr'
    img = fixture([(0x1000,'worker',text), (0x1040,'service','bx lr'), (0x1080,'vTaskDelete','bx lr')])
    assert not discover_task_loops(img, ['worker']).call_pcs
    loops = discover_task_loops(img, ['worker'], include_terminal=True)
    assert loops.call_pcs == {0x1004}
    cycle, = loops.tasks[0]['cycles']
    assert cycle['classification'] == 'persistent_with_terminal_exit'
    assert cycle['terminal_pcs'] == ['0x100c','0x1010','0x1012']
    # vTaskDelete(other) can return normally.
    img = fixture([(0x1000,'worker',text.replace('movs r0,#0','movs r0,#1')),
                   (0x1040,'service','bx lr'), (0x1080,'vTaskDelete','bx lr')])
    assert not discover_task_loops(img, ['worker'], include_terminal=True).call_pcs


@pytest.mark.parametrize("table,width", [('tbb [pc,r0]',1), ('tbh [pc,r0,lsl #1]',2)])
def test_dominated_switch_includes_all_cases_and_default(table,width):
    prefix = asm(0x1000, f'cmp r0,#1; bhi 0x1020; {table}')
    raw = bytearray(0x26)
    raw[:8] = prefix
    raw[8:8+2*width] = (4).to_bytes(width,'little')+(8).to_bytes(width,'little')
    for pc in (0x1010,0x1018,0x1020):
        raw[pc-0x1000:pc-0x1000+6] = asm(pc, 'bl 0x1080; b 0x1000')
    img = fixture([(0x1000,'worker',bytes(raw)), (0x1080,'process','bx lr')],
                  mapping=((0x1000,'t'),(0x1008,'d'),(0x1010,'t')))
    loops = discover_task_loops(img, ['worker'])
    assert loops.call_pcs == {0x1010,0x1018,0x1020}
    assert loops.tasks[0]['jump_tables'] == {'0x1004':['0x1010','0x1018']}
    # An unguarded jump-table entry invalidates the inferred index bound.
    raw[0x20:0x24] = asm(0x1020, 'b.w 0x1004')
    invalid = fixture([(0x1000,'worker',bytes(raw))], mapping=img.mapping)
    assert not discover_task_loops(invalid,['worker']).call_pcs


@pytest.mark.parametrize("displacement, resolved", [(4, True), (0, False)])
def test_callback_store_through_stack_argument_kills_only_overlapping_slots(displacement, resolved):
    img = fixture([
        (0x1000, 'init', 'sub sp,#8; movw r4,#0x1101; str r4,[sp]; '
         f'add r0,sp,#{displacement}; bl 0x1040; ldr r0,[sp]; bl 0x1080; bx lr'),
        (0x1040, 'callback', 'movs r1,#0; str r1,[r0]; bx lr'),
        (0x1080, 'xTaskCreate', 'bx lr'), (0x1100, 'worker', 'b 0x1100')])
    assert bool(discover_task_loops(img).tasks) == resolved

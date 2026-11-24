from scopt.analysis.rtos_storage import storage_sites
from .test_rtos_instances import fixture


def queue(body):
    image = fixture([(0x1000, 'startup', body), (0x1100, 'xQueueGenericCreate', 'bx lr')])
    return storage_sites(image, 'startup', {0x2000: {'app_queue'}})['sites'][0]


CREATE = 'movs r0,#4; movs r1,#12; movs r2,#0; bl 0x1100; movw r4,#0x2000; str r0,[r4]; '


def test_acyclic_registration_with_independent_loop():
    site = queue('cmp r3,#0; beq skip; ' + CREATE + 'skip: b skip')
    assert site['reasons'] == []
    assert site['handle'] == 'app_queue'
    assert (site['r0'], site['r1']) == (4, 12)


def test_creation_inside_loop_refused_even_with_static_shape():
    assert 'creation_inside_cycle' in queue('again: ' + CREATE + 'cmp r1,#0; bne again; bx lr')['reasons']


def test_unknown_jump_or_size_cannot_nominate_a_direct_slot():
    assert 'incomplete_control_flow' in queue(CREATE + 'bx r5')['reasons']
    assert 'incomplete_control_flow' in queue(CREATE + 'blx r5; bx lr')['reasons']
    assert 'unknown_queue_dimensions' in queue(CREATE.replace('movs r1,#12;', '') + 'bx lr')['reasons']


def test_message_buffer_keeps_source_callback_choice():
    image = fixture([(0x1000, 'init', 'movs r0,#47; movs r2,#1; bl 0x1100; '
                                     'movw r4,#0x2080; str r0,[r4]; bx lr'),
                     (0x1100, 'xStreamBufferGenericCreate', 'bx lr')])
    site, = storage_sites(image, 'init', {0x2080: {'messages'}})['sites']
    assert site['candidate_constructors'] == ['xMessageBufferCreate', 'xMessageBufferCreateWithCallback']
    assert site['handle'] == 'messages'
    assert not site['reasons']

def test_two_sites_assigning_the_same_handle_need_separate_review():
    image = fixture([(0x1000, 'init', CREATE + CREATE + 'bx lr'),
                     (0x1100, 'xQueueGenericCreate', 'bx lr')])
    rows = storage_sites(image, 'init', {0x2000: {'app_queue'}})['sites']
    assert len(rows) == 2
    assert all('multiple_creation_sites_for_handle' in row['reasons'] for row in rows)

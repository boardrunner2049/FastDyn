"""Positive and conflicting evidence for opt-in Thumb function recovery."""

from dataclasses import replace

import pytest
from keystone import Ks, KS_ARCH_ARM, KS_MODE_THUMB

from scopt.analysis.rtos_signatures import recover
from scopt.decode.guest_symbols import GuestFunc, GuestImage


def raw(pc, source):
    return bytes(Ks(KS_ARCH_ARM, KS_MODE_THUMB).asm(source, pc)[0])


def image(callee, *, target=False, wrong=False, duplicate=False):
    base = 0x1000
    buf = bytearray(0x180)
    body = raw(
        callee, "movs r1,#43; adds r2,#2; eors r1,r2; muls r3,r1; mov r2,r1; bx lr"
    )
    buf[callee - base : callee - base + len(body)] = body
    call = 0x1080 if wrong else callee
    caller = raw(
        base,
        f"push {{lr}}; movs r2,#14; muls r2,r0; adds r2,#3; eors r2,r0; "
        f"subs r1,#6; bl {call}; pop {{pc}}",
    )
    buf[: len(caller)] = caller
    symbols = [
        GuestFunc("dispatch", base, base + len(caller)),
        GuestFunc("xTimerIsTimerActive", callee, callee + len(body)),
    ]
    if wrong:
        buf[0x80:0x82] = raw(call, "bx lr")
    if duplicate:
        buf[0xC0 : 0xC0 + len(body)] = body
    return GuestImage(
        "fixture",
        "",
        ((base, bytes(buf)),),
        ((base, base + len(buf)),),
        () if target else tuple(symbols),
        () if target else ((0x1000, "t"),),
        text_sections=((base, base + len(buf)),),
        thumb_entries=frozenset({base}),
    )


def test_companion_recovers_exact_map_without_modifying_target():
    ref = image(0x1040)
    tgt = image(0x1040, target=True)
    enriched, report = recover(tgt, ref)
    assert enriched.mapping == ref.mapping
    assert tgt.stripped and not tgt._starts
    assert [f.start for f in enriched.funcs] == [f.start for f in ref.funcs]
    assert {r["extent"] for r in report["matched"]} == {"companion_symbol"}


def test_changed_direct_bl_needs_independently_matched_callee():
    ref = image(0x1040)
    enriched, report = recover(image(0x1060, target=True), ref)
    assert {r["name"]: r["method"] for r in report["matched"]} == {
        "xTimerIsTimerActive": "exact_thumb_span",
        "dispatch": "linked_thumb_calls",
    }
    assert enriched.func_at(0x1060).name == "xTimerIsTimerActive"
    assert enriched.code_ranges() == [(f.start, f.end) for f in enriched.funcs]
    # Same apparent caller with a different direct callee. Even if its true
    # timer body is present elsewhere, its destination disagrees.
    tgt = image(0x1060, target=True, wrong=True)
    _, rejected = recover(replace(tgt, thumb_entries=frozenset({0x1000, 0x1060})), ref)
    assert {r["name"]: r["reason"] for r in rejected["refused"]} == {
        "dispatch": "unconfirmed_call_destination"
    }


def test_duplicate_signature_and_unsupported_target_extent():
    ref = image(0x1040)
    tgt = image(0x1060, target=True, duplicate=True)
    _, report = recover(tgt, ref)
    assert report["refusal_counts"]["ambiguous_target"] == 1
    alias = replace(ref.funcs[-1], name="other_api")
    _, report = recover(tgt, replace(ref, funcs=(*ref.funcs, alias), _starts=[]))
    assert report["refusal_counts"]["ambiguous_reference"] == 2
    tgt = image(0x1060, target=True)
    known = GuestFunc("real_target", 0x1060, 0x1070)
    enriched, report = recover(replace(tgt, funcs=(known,), _starts=[]), ref)
    assert report["refusal_counts"]["conflicting_span"] == 1
    assert enriched.func_at(known.start) == known


def test_missing_entry_and_code_data_conflicts_are_refused():
    ref, tgt = image(0x1040), image(0x1060, target=True)
    _, no_entry = recover(replace(tgt, thumb_entries=frozenset()), ref)
    assert no_entry["refusal_counts"] == {"no_independent_entry": 2}
    _, data = recover(replace(tgt, mapping=((0x1000, "t"), (0x1064, "d"))), ref)
    assert data["refusal_counts"]["conflicting_mapping"] == 1
    with pytest.raises(ValueError, match="little-endian"):
        recover(replace(tgt, little_endian=False), ref)


def test_identical_displacement_at_moved_address_calls_wrong_function():
    ref = image(0x1040)
    # Relocate the entire caller. Its unchanged BL now targets 0x2040;
    # the matching timer has moved independently to 0x2060.
    raw_body = bytearray(ref.segments[0][1])
    raw_body[0x60:0x6C] = raw_body[0x40:0x4C]
    raw_body[0x40:0x4C] = b"\x00" * 12
    tgt = GuestImage(
        "changed",
        "",
        ((0x2000, bytes(raw_body)),),
        ((0x2000, 0x2180),),
        (),
        (),
        text_sections=((0x2000, 0x2180),),
        thumb_entries=frozenset({0x2000, 0x2060}),
    )
    _, receipt = recover(tgt, ref)
    assert [f["name"] for f in receipt["matched"]] == ["xTimerIsTimerActive"]
    assert receipt["refusal_counts"] == {"unconfirmed_call_destination": 1}

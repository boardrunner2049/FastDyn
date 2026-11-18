"""Strict callsite retirement requires checked provenance, not a changed opcode."""

from types import SimpleNamespace

import pytest

from scopt.commands.host_calls import _coverage


def call(off, *, symbol="helper_test", encoding="pool_indirect", bound=True, weight=25,
         annotation="capture_site"):
    return {
        "unit": "tb:test", "host_off": off, "block": "guest_instruction",
        "annotation": annotation, "decoded_call_bytes": "ff15a0000000",
        "call_class": "pure", "image_bound": bound,
        "reason": None if bound else "call_binding_unknown",
        "tb_entries_proxy": {"value": weight, "status": "exact"},
        "call": {"guest_pcs": (8,), "encoding": encoding, "target_abs": 0x1000,
                 "symbol": symbol, "helper_id": 7},
    }


def retired(off, *, growth=72, certificate="cert"):
    row = call(off, annotation="native_fragment")
    row.update(certificate=certificate, decoded_call_bytes=None, block="native_splice",
               fragment=SimpleNamespace(splice=True, growth=growth, continuation_off=off + 6,
                                        call_bytes=bytes.fromhex("ff15a0000000"),
                                        helper_id=7, target_abs=0x1000, guest_pcs=(8,),
                                        symbol="helper_test"))
    return row


def test_coverage_verifies_archived_replacement_and_remaps_surviving_offsets():
    first, second = ("tb:test", 10), ("tb:test", 60)
    baseline = ({first: call(10, weight=100), second: call(60, symbol="other")}, {}, [])
    current = ({("tb:test", 132): call(132, symbol="other")}, {first: retired(10)}, [])
    report = _coverage(baseline, current, artifact_checked=True, native_checked=True)
    assert report["required_annotated_sites"] == 2
    assert report["retired_verified_sites"] == 1
    assert report["sites"][1]["current"]["host_off"] == 132
    assert report["tb_entry_weighted_proxy"]["retired_fraction_of_known_weight"] == 0.8
    assert not report["strict_coverage_complete"]


def test_coverage_never_counts_devirtualized_calls():
    key = "tb:test", 10
    baseline = ({key: call(10)}, {}, [])
    current = ({key: call(10, encoding="rel32")}, {}, [])
    report = _coverage(baseline, current, artifact_checked=True, native_checked=True)
    assert report["retired_verified_sites"] == 0
    assert report["sites"][0]["status"] == "surviving_devirtualized_call"


@pytest.mark.parametrize(("bound", "artifact_checked", "native_checked"), [
    (False, True, True),
    (True, False, False),
    (True, True, False),
])
def test_coverage_does_not_credit_unresolved_calls_or_unchecked_fragments(
        bound, artifact_checked, native_checked):
    key = "tb:test", 10
    baseline = ({key: call(10, bound=bound)}, {}, [])
    current = ({}, {key: retired(10)}, [])
    report = _coverage(baseline, current, artifact_checked=artifact_checked,
                       native_checked=native_checked)
    assert report["retired_verified_sites"] == 0
    assert report["blockers"]["unverified_native_fragment"] == 1


def test_coverage_counts_existing_archived_sites_and_skips_decoded_only_denominator():
    key, ghost = ("tb:test", 10), ("tb:test", 25)
    before = ({ghost: call(25, annotation="decoded_only")}, {key: retired(10)}, [])
    after = ({}, {key: retired(10)}, [])
    report = _coverage(before, after, artifact_checked=True)
    assert report["required_annotated_sites"] == 1
    assert report["retired_verified_sites"] == 1
    assert report["blockers"]["decoded_without_capture"] == 1


def test_coverage_lists_nested_calls_without_credit_or_unexplained_disappearance():
    key, nested = ("tb:test", 10), ("tb:test", 40)
    baseline = ({key: call(10)}, {}, [])
    nested_row = call(40, annotation="verified_nested_call")
    nested_row["call_class"] = "framed_nested_call"
    current = ({nested: nested_row}, {key: retired(10)}, [])
    report = _coverage(baseline, current, artifact_checked=True, native_checked=True)
    assert report["required_annotated_sites"] == 1
    assert report["retired_verified_sites"] == 1
    assert report["strict_coverage_complete"]
    assert not report["current_call_free"]
    assert report["certified_nested_frame_calls"] == [nested_row]
    assert report["unmatched_current_calls"] == []

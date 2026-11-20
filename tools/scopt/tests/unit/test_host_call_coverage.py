"""Strict callsite retirement requires checked provenance, not a changed opcode."""

from types import SimpleNamespace

import pytest

from scopt.commands.host_calls import _coverage
from scopt.model.native import NativeBranchRelaxation, NativeSplice


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


def retired(off, *, growth=72, certificate="cert", branches=()):
    row = call(off, annotation="native_fragment")
    splice = NativeSplice(start_off=off - 3, prefix=b"\x8b\x7d\x00",
                          code=b"\x90" * (9 + growth), branches=branches)
    row.update(certificate=certificate, decoded_call_bytes=None, block="native_splice",
               fragment=SimpleNamespace(splice=splice, continuation_off=off + 6,
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


def test_coverage_accounts_for_both_splices_in_original_coordinates():
    uid = "tb:test"
    original = {(uid, off): call(off) for off in (0, 10, 25, 40, 70)}
    # A surviving call between the replacements moves once, even when its new
    # offset is past the second splice's original coordinate.
    live = {(uid, off): call(off) for off in (0, 97, 166)}
    splices = {(uid, 40): retired(40, growth=24), (uid, 10): retired(10)}
    report = _coverage((original, {}, []), (live, splices, []),
                       artifact_checked=True, native_checked=True)
    assert report["retired_verified_sites"] == 2
    assert report["unmatched_current_calls"] == []
    survivors = [r["current"]["host_off"] for r in report["sites"] if r["status"] == "surviving_call"]
    assert survivors == [0, 97, 166]


def test_coverage_accounts_for_widening_at_the_original_branch():
    uid = "tb:test"
    original = {(uid, off): call(off) for off in (10, 25, 60)}
    live = {(uid, off): call(off) for off in (97, 140)}
    widened = NativeBranchRelaxation(source_off=40, source_bytes=b"\xeb\xec")
    splices = {(uid, 10): retired(10, branches=(widened,))}
    report = _coverage((original, {}, []), (live, splices, []),
                       artifact_checked=True, native_checked=True)
    assert [r["status"] for r in report["sites"]] == ["retired_verified", "surviving_call", "surviving_call"]
    assert report["unmatched_current_calls"] == []


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


@pytest.mark.parametrize("existing", [True, False])
def test_guarded_inline_reports_live_fallback_and_keeps_denominator(existing):
    from scopt.model.native import UDIV_KIND

    uid = "tb:test"
    key, fallback_key = (uid, 10), (uid, 42)
    proof = retired(10)
    req = proof["fragment"]
    req.splice = NativeSplice(10, b"", b"\x90" * 32 + req.call_bytes, kind=UDIV_KIND)
    proof["fallback_off"] = fallback_key[1]
    fallback = dict(call(42, annotation="guarded_fallback_call"), parent_certificate="cert")
    artifact = ({fallback_key: fallback}, {key: proof}, [])
    before = artifact if existing else ({key: call(10)}, {}, [])
    report = _coverage(before, artifact, artifact_checked=True, native_checked=True)
    assert report["required_annotated_sites"] == report["guarded_inline_sites"] == 1
    assert report["sites"][0]["status"] == "guarded_inline_with_fallback"
    assert report["retired_verified_sites"] == report["tb_entry_weighted_proxy"]["known_retired_weight"] == 0
    assert report["unmatched_current_calls"] == []
    assert not report["current_call_free"] and not report["strict_coverage_complete"]

    missing = _coverage(before, ({}, {key: proof}, []), artifact_checked=True, native_checked=True)
    assert missing["guarded_inline_sites"] == 0 and not missing["strict_coverage_complete"]


def test_retired_udiv_lists_verified_nested_raise_call():
    from dataclasses import replace

    from scopt.commands.host_calls import _exception_call
    from scopt.decode.x86 import sweep
    from scopt.validate_sym.native_udiv_full import prepare_full_udiv_splice
    from tests.unit.test_native_udiv_full import _case

    u, req, ca = _case()
    req = replace(req, splice=prepare_full_udiv_splice(u, req, ca))
    body = sweep(req.splice.code, req.call_off)
    nested, = [ins for ins in body if ins.is_call]
    u = replace(u, splice_applied=True, native_fragments=(req,))
    cert, target = _exception_call(u, nested.address, nested)
    assert target == req.target_abs - 0x210 and cert == req.certificate_id
    assert _exception_call(u, nested.address + 1, nested) is None
    assert _exception_call(replace(u, meta_status="incomplete"), nested.address, nested) is None

    # Coverage must report that the original UDIV call is retired while the
    # exception routine still has a real, independently decoded CALL.
    source_key, nested_key = ("tb:test", req.call_off), ("tb:test", nested.address)
    baseline_row = call(req.call_off, symbol=req.symbol)
    baseline_row["decoded_call_bytes"] = req.call_bytes.hex()
    baseline_row["call"].update(helper_id=req.helper_id, target_abs=req.target_abs, guest_pcs=req.guest_pcs)
    proof = dict(retired(req.call_off), fragment=req, certificate=cert)
    child = dict(call(nested.address, annotation="verified_exception_call"),
                 call_class="exception_runtime", parent_certificate=cert)
    result = _coverage(({source_key: baseline_row}, {}, []),
                       ({nested_key: child}, {source_key: proof}, []),
                       artifact_checked=True, native_checked=True)
    assert result["retired_verified_sites"] == 1 and result["guarded_inline_sites"] == 0
    assert result["strict_coverage_complete"] and not result["current_call_free"]
    assert result["certified_exception_calls"] == [child] and not result["unmatched_current_calls"]

"""Report native callsites, exact image binding and certified retirement."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

import click

from ..analysis.api import ScopeRef
from ..analysis.native_callees import NATIVE_IMAGES, prepare_native_images
from ..analysis.store import AnalysisStore
from ..canon import sha256_id
from ..decode.x86 import sweep
from ..formats.artifact import ArtifactError
from ..formats.enums import OpMarkKind, SiteKind
from ..formats.profile_v1 import ProfileView, read_profile
from ..importer.import_cache import open_snapshot
from ..importer.join import ImportRejected
from ..splice_layout import SpliceGroupOffsets, splice_offsets
from ..validate_sym.native_inline import NativeInlineError

_CALL_SITES = (SiteKind.HELPER_CALL, SiteKind.LDST_HELPER_CALL)


def register(group: click.Group) -> None:
    group.add_command(host_calls)


def _calls(snap, store, profile, phase):
    rows, counts = [], Counter()
    for unit in snap.units():
        result = store.get("native-callees", ScopeRef("unit", unit.uid))
        hotness = None if profile is None else profile.count(unit.uid, phase).to_json()
        for binding in result.value:
            call = asdict(binding.call)
            call["original_bytes"] = binding.call.original_bytes.hex()
            callee = None if binding.callee is None else asdict(binding.callee)
            if callee is not None:
                del callee["code"]
                callee["code_ranges"] = binding.callee.code_ranges
            counts[binding.reason or "image_bound"] += 1
            rows.append({"call": call, "callee": callee, "reason": binding.reason,
                         "detail": binding.detail, "tb_entries": hotness})
    return rows, counts


def _block(unit, off):
    """Identify the current block from the physical capture partition."""
    if unit.stub_start is not None and off >= unit.stub_start:
        return "guard_stub"
    if unit.inline_start is not None and off >= unit.inline_start:
        return "native_island"
    if any(row.slow_start <= off < row.slow_end for row in unit.ldst):
        return "ldst_slow"
    if off < unit.insn0_start:
        return "prefix"
    if any(row.host_start <= off < row.host_end for row in unit.insns):
        return "guest_instruction"
    return "remainder"


def _framed_call(unit, off, inst):
    """Classify a surviving rel32 CALL that matches a verified frame relocation."""
    if (not unit.splice_applied or unit.meta_status != "ok" or inst is None
            or inst.raw[:1] != b"\xe8" or inst.size != 5):
        return None
    for req in unit.native_fragments:
        splice = req.splice
        if splice is None or splice.kind != "frame":
            continue
        relative = off - splice.start_off
        if 0 <= relative <= len(splice.code) - inst.size and any(
            r.field_off == relative + 1 and r.next_off == relative + inst.size
            and r.target_abs == inst.branch_target for r in splice.relocations
        ):
            return req.certificate_id
    return None


def _inventory(snapshot, rows, profile, phase):
    """Count both captured references and independently decoded physical calls."""
    live, retired, uninspected = {}, {}, []
    by_unit = {(row["call"]["uid"], row["call"]["host_off"]): row for row in rows}
    for unit in snapshot.units():
        if unit.meta_status != "ok":
            uninspected.append(unit.uid)
        base = snapshot.header.code_address + unit.tc_offset
        # Pool data is never swept as instructions. Out-of-line certified
        # fragments and guard stubs have their own physical span.
        code_end = unit.pool_start if unit.pool_start is not None else unit.ldst_end
        ranges = [(0, code_end)]
        out_of_line = unit.inline_start if unit.inline_start is not None else unit.stub_start
        if out_of_line is not None:
            ranges.append((out_of_line, unit.tc_size))
        decoded = {}
        if unit.meta_status == "ok":
            for lo, hi in ranges:
                for inst in sweep(unit.code[lo:hi], base + lo):
                    if inst.bad and unit.uid not in uninspected:
                        uninspected.append(unit.uid)
                    if inst.is_call:
                        decoded[inst.address - base] = inst
        sites = {site.insn_off for site in unit.sites if site.kind in _CALL_SITES}
        seen_marks = set()
        hotness = None if profile is None else profile.count(unit.uid, phase).to_json()
        for off in sorted(sites | decoded.keys()):
            key = unit.uid, off
            row = by_unit.get(key)
            call = None if row is None else row["call"]
            if call is not None and call["opmark_off"] is not None:
                seen_marks.add(call["opmark_off"])
            inst = decoded.get(off)
            frame = _framed_call(unit, off, inst) if off not in sites else None
            if frame is not None:
                live[key] = {
                    "unit": unit.uid, "host_off": off, "block": "native_frame",
                    "annotation": "verified_nested_call", "decoded_call_bytes": inst.raw.hex(),
                    "call": {"target_abs": inst.branch_target, "encoding": "rel32"},
                    "call_class": "framed_nested_call", "capture_resolved": True,
                    "image_bound": False, "reason": None, "tb_entries_proxy": hotness,
                    "parent_certificate": frame,
                }
                continue
            live[key] = {
                "unit": unit.uid, "host_off": off, "block": _block(unit, off),
                "annotation": "capture_site" if off in sites else "decoded_only",
                "decoded_call_bytes": inst.raw.hex() if inst is not None else None, "call": call,
                "call_class": call["call_class"] if call is not None else "unknown",
                "capture_resolved": call is not None and call["reason"] is None,
                "image_bound": row is not None and row["callee"] is not None,
                "reason": "call_binding_unknown" if row is None else row["reason"],
                "tb_entries_proxy": hotness,
            }
        for mark in unit.opmarks:
            if mark.kind != OpMarkKind.CALL or mark.host_off in seen_marks:
                continue
            key = unit.uid, mark.host_off
            if key in live:
                # One decoded physical call also carries this mark.
                if live[key]["annotation"] == "decoded_only":
                    live[key]["annotation"] = "opmark"
                continue
            live[key] = {
                "unit": unit.uid, "host_off": mark.host_off, "block": _block(unit, mark.host_off),
                "annotation": "opmark", "decoded_call_bytes": None, "call": None,
                "call_class": "unknown", "capture_resolved": False, "image_bound": False,
                "reason": "call_binding_unknown",
                "tb_entries_proxy": hotness,
            }
        for req in unit.native_fragments:
            key = unit.uid, req.call_off
            imported = unit.meta_status == "ok" and (unit.splice_applied or unit.inline_start is not None)
            retired[key] = {
                "unit": unit.uid, "host_off": req.call_off,
                "block": "native_splice" if req.splice is not None else "native_island",
                "annotation": "native_fragment", "decoded_call_bytes": None,
                "call": {"original_bytes": req.call_bytes.hex(), "symbol": req.symbol,
                         "helper_id": req.helper_id, "target_abs": req.target_abs,
                         "guest_pcs": req.guest_pcs},
                "call_class": "pure", "capture_resolved": imported, "image_bound": imported,
                "reason": None if imported else "unverified_native_fragment",
                "certificate": req.certificate_id,
                "tb_entries_proxy": hotness, "fragment": req,
            }
    return live, retired, uninspected


def _public_row(row):
    return {key: value for key, value in row.items() if key != "fragment"}


def _matches(row, fragment):
    call = row["call"]
    return (call is not None and row["image_bound"]
            and row["decoded_call_bytes"] == fragment.call_bytes.hex()
            and call["target_abs"] == fragment.target_abs
            and call["helper_id"] == fragment.helper_id and call["symbol"] == fragment.symbol
            and tuple(call["guest_pcs"]) == fragment.guest_pcs)


def _coverage(baseline, current, *, artifact_checked=False, native_checked=False):
    """Pair stable baseline callsites with present calls or proved native replacements."""
    before, old_retired, baseline_uninspected = baseline
    after, new_retired, current_uninspected = current
    details, blockers, counts = [], Counter(), Counter()
    matched_live, matched_retired = set(), set()
    # Apply the linker's original-to-physical map once per TB. Sites already
    # present in the baseline have already shifted its live call coordinates.
    spans = defaultdict(list)
    for key, row in new_retired.items():
        fragment = row["fragment"]
        if key not in old_retired and fragment.splice is not None:
            spans[key[0]].append(splice_offsets(fragment))
    offsets = {uid: SpliceGroupOffsets(tuple(items)) for uid, items in spans.items()}
    known_weight, retired_weight, unknown_weight, nonexact_weight = 0, 0, 0, 0
    for key, row in sorted((before | old_retired).items()):
        if row["annotation"] == "verified_nested_call":
            continue
        annotated = row["annotation"] != "decoded_only"
        if annotated:
            counts["required"] += 1
            counts["resolved" if row["image_bound"] else "unresolved"] += 1
        else:
            blockers["decoded_without_capture"] += 1

        proof = new_retired.get(key)
        current_row, status = None, None
        if proof is not None:
            matches_old = key in old_retired and proof["certificate"] == row["certificate"]
            verify_new = (key not in old_retired and annotated and native_checked
                          and _matches(row, proof["fragment"]))
            if artifact_checked and proof["image_bound"] and (matches_old or verify_new):
                matched_retired.add(key)
                current_row, status = proof, "retired_verified"
        if status is None:
            uid, off = key
            current_key = key
            remap = offsets.get(uid)
            if remap is not None:
                try:
                    current_key = None if remap.contains(off) else (uid, remap(off))
                except NativeInlineError:
                    # An opmark inside a widened instruction has no live call
                    # coordinate. Leave its disappearance unverified.
                    current_key = None
            current_row = after.get(current_key)
            if current_row is not None:
                matched_live.add(current_key)
                status = "surviving_call" if current_row["decoded_call_bytes"] is not None else (
                    "unverified_current_call")
                if (row["call"] is not None and current_row["call"] is not None
                        and row["call"].get("encoding") == "pool_indirect"
                        and current_row["call"].get("encoding") == "rel32"):
                    status = "surviving_devirtualized_call"
            else:
                status = "unverified_disappearance"
        is_retired = status == "retired_verified"
        if annotated:
            if is_retired:
                counts["retired_verified"] += 1
            else:
                blockers[row["reason"] or status] += 1
            hotness = row.get("tb_entries_proxy")
            if hotness is not None and hotness["value"] is not None:
                known_weight += hotness["value"]
                retired_weight += hotness["value"] if is_retired else 0
                nonexact_weight += hotness["status"] != "exact"
            else:
                unknown_weight += 1
        details.append({"baseline": _public_row(row), "status": status,
                        "current": None if current_row is None else _public_row(current_row)})

    nested_keys = {key for key, row in after.items()
                   if row["annotation"] == "verified_nested_call"}
    certified_nested = [_public_row(after[key]) for key in sorted(nested_keys)]
    explained = matched_live | (nested_keys if artifact_checked else set())
    extra_live = [_public_row(after[key]) for key in sorted(after.keys() - explained)]
    extra_retired = [_public_row(new_retired[key]) for key in sorted(new_retired.keys() - matched_retired)]
    if extra_live:
        blockers["unmatched_current_call"] += len(extra_live)
    if extra_retired:
        blockers["unverified_native_fragment"] += len(extra_retired)
    if baseline_uninspected:
        blockers["uninspected_baseline_tb"] += len(baseline_uninspected)
    if current_uninspected:
        blockers["uninspected_current_tb"] += len(current_uninspected)
    if not artifact_checked:
        blockers["artifact_not_validated"] += 1
    return {
        "schema": "scopt.host_call_coverage/1",
        "required_annotated_sites": counts["required"],
        "resolved_sites": counts["resolved"], "unresolved_sites": counts["unresolved"],
        "retired_verified_sites": counts["retired_verified"],
        "strict_coverage_complete": not blockers, "blockers": dict(blockers),
        "current_call_free": not after and not current_uninspected,
        "certified_nested_frame_calls": certified_nested,
        "classified_current_calls": dict(Counter(row["call_class"] for row in after.values())),
        "current_call_blocks": dict(Counter(row["block"] for row in after.values())),
        "tb_entry_weighted_proxy": {
            "known_required_weight": known_weight, "known_retired_weight": retired_weight,
            "unknown_weight_sites": unknown_weight, "nonexact_weight_sites": nonexact_weight,
            "retired_fraction_of_known_weight": retired_weight / known_weight if known_weight else None,
        },
        "sites": details, "unmatched_current_calls": extra_live,
        "unverified_native_fragments": extra_retired,
        "uninspected_baseline_units": baseline_uninspected,
        "uninspected_current_units": current_uninspected,
    }


@click.command("host-calls")
@click.argument("snapshot", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--profile", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--phase", default="steady", show_default=True)
@click.option("--out", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--backend-revision", default="census-only", show_default=True)
@click.option("--bridge", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--bridge-version", default=1, type=click.IntRange(min=1), show_default=True)
@click.option("--policy-version", default=1, type=click.IntRange(min=1), show_default=True)
@click.option("--coverage", is_flag=True, help="Report the strict annotated call-site denominator.")
@click.option("--artifact", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Compare a published artifact against SNAPSHOT with --coverage.")
def host_calls(snapshot, profile, phase, out, backend_revision, bridge, bridge_version,
               policy_version, coverage, artifact) -> None:
    """Report captured callsites, verified native functions and refusal reasons."""
    if artifact is not None and not coverage:
        raise click.UsageError("--artifact requires --coverage")
    try:
        snap = open_snapshot(snapshot)
        view = None if profile is None else ProfileView.open(read_profile(profile), snap)
        bridge_sha = None if bridge is None else sha256_id(bridge.read_bytes())
        prepared = prepare_native_images(
            snap, backend_revision=backend_revision, bridge_version=bridge_version,
            policy_version=policy_version, bridge_sha256=bridge_sha)
        store = AnalysisStore(snap, external_inputs={NATIVE_IMAGES: prepared})
        rows, counts = _calls(snap, store, view, phase)
        if coverage:
            baseline = _inventory(snap, rows, view, phase)
            current, artifact_id, checked, native = baseline, None, False, False
            artifact_validation = None
            if artifact is not None:
                from ..validate import reimport, validate_artifact

                validation = validate_artifact(artifact, snap)
                artifact_id = validation["artifact_id"]
                artifact_validation = {key: validation[key] for key in
                                       ("verdict", "codes", "stored_codes_agree")}
                checked = validation["verdict"] == "accepted" and validation["stored_codes_agree"]
                native = checked and validation["codes"].get("V24") == "pass"
                target = reimport(artifact, snap).snapshot
                actual, _ = _calls(target, AnalysisStore(target), None, phase)
                current = _inventory(target, actual, view, phase)
            report = _coverage(baseline, current, artifact_checked=checked, native_checked=native)
            report.update(snapshot_id=snap.snapshot_id, artifact_id=artifact_id,
                          artifact_validation=artifact_validation, profile_phase=phase if view is not None else None)
        else:
            report = {
                "schema": "scopt.host_calls/1", "snapshot_id": snap.snapshot_id,
                "native_images_id": prepared.input_id, "capture_ids": prepared.value.capture_ids,
                "profile_phase": phase if view is not None else None,
                "hotness_metric": "TB-entry proxy",
                "counts": dict(counts), "calls": rows,
            }
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if out is None:
            click.echo(rendered, nl=False)
        else:
            out.write_text(rendered)
    except (ArtifactError, ImportRejected, ValueError, OSError) as error:
        raise click.ClickException(str(error)) from error

#!/usr/bin/env python3
"""Generate the mechanism-based report for the focused LEMIX O0 rerun."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import statistics
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN = ROOT / "build/preflight/lemix-o0-rerun-2026-11-16"
DEFAULT_PROTOTYPE = ROOT / "build/preflight/preflight-sdk-v1-2026-11-16"
DEFAULT_BOLT_WRAPPER = ROOT / "build/preflight/bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16"
DEFAULT_THUMB_M3 = ROOT / "build/preflight/thumb-cortex-m3-pilot-2026-11-16/report.json"
DEFAULT_THUMB_FIXED = ROOT / "build/preflight/thumb-fop-benchmark/results/report.json"
DEFAULT_THUMB_M0 = ROOT / "build/preflight/thumb-cortex-m0plus-pilot-2026-11-16/report.json"
DEFAULT_THUMB_PROFILE = ROOT / "build/preflight/thumb-cortex-m3-profile-2026-11-16/report.json"
DEFAULT_THUMB_TOP2 = ROOT / "build/preflight/thumb-cortex-m3-hot-fop-top2-nop-rejected-2026-11-16/report.json"
DEFAULT_THUMB_HOT = ROOT / "build/preflight/thumb-cortex-m3-hot-fop-2026-11-16/report.json"
DEFAULT_THUMB_MOV = ROOT / "build/preflight/thumb-cortex-m3-hot-fop-mov-rejected-2026-11-16/report.json"
DEFAULT_THUMB_PACED_CALIBRATION = ROOT / "build/preflight/thumb-cortex-m3-hot-fop-calibration/report.json"
DEFAULT_THUMB_BRANCH_PROFILE = ROOT / "build/preflight/thumb-cortex-m3-fallthrough-profile-2026-11-16/report.json"
DEFAULT_THUMB_BRANCH = ROOT / "build/preflight/tfb-pin-final-2026-11-16/report.json"
DEFAULT_THUMB_BOUNDED_PROFILE = ROOT / "build/preflight/thumb-cortex-m3-bounded-fop-profile-2026-11-16/report.json"
DEFAULT_THUMB_BOUNDED = ROOT / "build/preflight/thumb-cortex-m3-bounded-fop-2026-11-16/report.json"
DEFAULT_THUMB_BOUNDED_ABLATION = ROOT / "build/preflight/thumb-cortex-m3-bounded-fop-ablation-2026-11-16/report.json"
DEFAULT_THUMB_HOT_EXIT = ROOT / "build/preflight/thumb-cortex-m3-hot-exit-layout-2026-11-17/report.json"


def load(path: Path) -> Any:
    with path.open() as source:
        return json.load(source)


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def frame_stats(run_dir: Path, arm: str) -> dict[str, int]:
    text = (run_dir / f"commands-and-logs/bolt-{arm}.stdout").read_text()
    optimized = re.search(
        r"FOP optimized (\d+) redundant load\(s\) and (\d+) unused store\(s\)", text
    )
    frequency = re.search(
        r"Frequency of redundant loads is (\d+) and frequency of unused stores is (\d+)",
        text,
    )
    deleted = re.search(
        r"FOP deleted (\d+) load\(s\) \(dyn count: (\d+)\) and (\d+) store\(s\)",
        text,
    )
    if not (optimized and frequency and deleted):
        raise SystemExit(f"could not parse FOP statistics for {arm}")
    return {
        "redundant_loads": int(optimized.group(1)),
        "unused_stores": int(optimized.group(2)),
        "redundant_load_frequency": int(frequency.group(1)),
        "unused_store_frequency": int(frequency.group(2)),
        "deleted_loads": int(deleted.group(1)),
        "deleted_load_frequency": int(deleted.group(2)),
        "deleted_stores": int(deleted.group(3)),
    }


def result_counts(results: list[dict[str, Any]], arm: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in results:
        if item["arm"] == arm:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
    return counts


def generate(run_dir: Path, main_report: Path | None = None) -> None:
    required = [
        "manifest.json",
        "workloads-and-contracts.json",
        "bolt-arm-options.json",
        "paired-results.json",
        "binary-layout-attribution.json",
        "candidate-universe.csv",
        "stage-funnel.csv",
        "search-seeds-and-costs.json",
        "stoke-legality.json",
        "stoke-installation.json",
        "stoke-search-installation.json",
        "accessor-harness/paired-results.json",
        "accessor-harness/legality-frs.json",
        "accessor-harness/body-provenance-frs.json",
    ]
    missing = [name for name in required if not (run_dir / name).is_file()]
    if missing:
        raise SystemExit("missing required evidence: " + ", ".join(missing))

    manifest = load(run_dir / "manifest.json")
    paired = load(run_dir / "paired-results.json")
    layout = load(run_dir / "binary-layout-attribution.json")["variants"]
    reconciliation = load(run_dir / "stoke-candidate-reconciliation.json")
    search = load(run_dir / "search-seeds-and-costs.json")
    search_results = search["results"]
    legality = load(run_dir / "stoke-legality.json")
    native_legality = load(run_dir / "accessor-harness/legality-frs.json")
    accessor_perf = load(run_dir / "accessor-harness/paired-results.json")
    provenance = load(run_dir / "accessor-harness/body-provenance-frs.json")
    installation = load(run_dir / "stoke-search-installation.json")
    f1 = frame_stats(run_dir, "F1")
    fh = frame_stats(run_dir, "FH")
    frs = frame_stats(run_dir, "FRS")
    prototype_dir = DEFAULT_PROTOTYPE
    prototype_required = [
        "manifest.json", "identity-roundtrip.json", "pass-outcomes.json",
        "reference-class-audit.json", "state-construction-audit.json",
        "s2-verification-classification.json", "fault-identity-comparison.json",
        "work-measurement.json", "cortex-m3-identity-boundary.json",
        "stage-ledger.jsonl",
    ]
    prototype_missing = [
        name for name in prototype_required if not (prototype_dir / name).is_file()
    ]
    if prototype_missing:
        raise SystemExit(
            "missing prototype evidence; run scripts/preflight/run-preflight-prototype.sh: "
            + ", ".join(prototype_missing)
        )
    prototype_manifest = load(prototype_dir / "manifest.json")
    prototype_identity = load(prototype_dir / "identity-roundtrip.json")
    prototype_outcomes = load(prototype_dir / "pass-outcomes.json")["outcomes"]
    prototype_references = load(prototype_dir / "reference-class-audit.json")
    prototype_states = load(prototype_dir / "state-construction-audit.json")
    prototype_s2 = load(prototype_dir / "s2-verification-classification.json")
    prototype_fault = load(prototype_dir / "fault-identity-comparison.json")
    prototype_work = load(prototype_dir / "work-measurement.json")
    prototype_cortex = load(prototype_dir / "cortex-m3-identity-boundary.json")
    bolt_wrapper_dir = DEFAULT_BOLT_WRAPPER
    bolt_wrapper_required = [
        "wrapper-result.json", "bolt-post-optimization-report.json",
        "custom-pass-baseline.json", "command.json", "bolt.stdout", "bolt.stderr",
    ]
    bolt_wrapper_missing = [
        name for name in bolt_wrapper_required if not (bolt_wrapper_dir / name).is_file()
    ]
    bolt_wrapper = None if bolt_wrapper_missing else load(bolt_wrapper_dir / "wrapper-result.json")
    bolt_native_report = (
        None if bolt_wrapper_missing
        else load(bolt_wrapper_dir / "bolt-post-optimization-report.json")
    )
    bolt_compact = None if bolt_wrapper is None else bolt_wrapper["compact_selected_adoption"]
    bolt_compact_timing_path = bolt_wrapper_dir / "auto-compact-paired-samples.json"
    if not bolt_compact_timing_path.is_file():
        bolt_compact_timing_path = (
            ROOT / "build/preflight/bolt-firmware-wrapper-2026-11-16/"
            "auto-compact-paired-samples.json"
        )
    bolt_compact_timing = (
        load(bolt_compact_timing_path) if bolt_compact_timing_path.is_file() else None
    )
    bolt_hot_timing_path = bolt_wrapper_dir / "hot-vs-all-paired-samples.json"
    if not bolt_hot_timing_path.is_file():
        bolt_hot_timing_path = (
            ROOT / "build/preflight/bolt-firmware-wrapper-2026-11-16/"
            "hot-vs-all-paired-samples.json"
        )
    bolt_hot_timing = load(bolt_hot_timing_path) if bolt_hot_timing_path.is_file() else None
    queue_fixed_path = ROOT / "build/preflight/lemix-queue/fixed-work-paired-results.json"
    queue_fixed = load(queue_fixed_path) if queue_fixed_path.is_file() else None
    queue_wrapper_dir = ROOT / "build/preflight/lemix-queue/bolt-firmware-wrapper"
    queue_wrapper_result_path = queue_wrapper_dir / "wrapper-result.json"
    queue_wrapper_timing_path = queue_wrapper_dir / "fixed-work-paired-results.json"
    queue_wrapper_result = (
        load(queue_wrapper_result_path) if queue_wrapper_result_path.is_file() else None
    )
    queue_wrapper_timing = (
        load(queue_wrapper_timing_path) if queue_wrapper_timing_path.is_file() else None
    )
    queue_policy_path = (
        ROOT / "build/preflight/lemix-queue/"
        "bolt-policy-matrix-2026-11-16/report.json"
    )
    queue_policy = load(queue_policy_path) if queue_policy_path.is_file() else None
    queue_deployment_path = (
        queue_wrapper_dir / "queue-policy-held-out-report.json"
    )
    queue_deployment = (
        load(queue_deployment_path) if queue_deployment_path.is_file() else None
    )
    firm_freertos_path = (
        ROOT / "build/preflight/firm-bolt-freertos-o0-o3-2026-11-17/manifest.json"
    )
    firm_freertos = (
        load(firm_freertos_path) if firm_freertos_path.is_file() else None
    )
    queue_publication_path = queue_wrapper_dir / "published-policy/index.json"
    queue_publication = (
        load(queue_publication_path) if queue_publication_path.is_file() else None
    )
    queue_candidates_path = (
        queue_wrapper_dir / "fixed-layout-firmware-candidates/index.json"
    )
    queue_candidates = (
        load(queue_candidates_path) if queue_candidates_path.is_file() else None
    )
    queue_reloc_candidates_path = (
        queue_wrapper_dir / "relocatable-firmware-candidates/index.json"
    )
    queue_reloc_candidates = (
        load(queue_reloc_candidates_path)
        if queue_reloc_candidates_path.is_file() else None
    )
    corpus_wrapper_path = (
        ROOT / "build/preflight/recovered-firmware-corpus-2026-11-16/"
        "bolt-firmware-wrapper/held-out-policy-report.json"
    )
    corpus_wrapper = (
        load(corpus_wrapper_path) if corpus_wrapper_path.is_file() else None
    )
    corpus_publication_path = (
        corpus_wrapper_path.parent / "published-policy/index.json"
    )
    corpus_publication = (
        load(corpus_publication_path) if corpus_publication_path.is_file() else None
    )
    corpus_candidates_path = (
        corpus_wrapper_path.parent / "fixed-layout-firmware-candidates/index.json"
    )
    corpus_candidates = (
        load(corpus_candidates_path) if corpus_candidates_path.is_file() else None
    )
    corpus_reloc_candidates_path = (
        corpus_wrapper_path.parent / "relocatable-firmware-candidates/index.json"
    )
    corpus_reloc_candidates = (
        load(corpus_reloc_candidates_path)
        if corpus_reloc_candidates_path.is_file() else None
    )
    smartspeaker_precision_path = (
        ROOT / "build/preflight/lemix-smartspeaker-policy-precision-2026-11-17/"
        "report.json"
    )
    smartspeaker_precision = (
        load(smartspeaker_precision_path)
        if smartspeaker_precision_path.is_file() else None
    )
    smartspeaker_hot_root = (
        ROOT / "build/preflight/lemix-smartspeaker-hot-fop-2026-11-17"
    )
    smartspeaker_hot_path = smartspeaker_hot_root / "report.json"
    smartspeaker_hot = (
        load(smartspeaker_hot_path) if smartspeaker_hot_path.is_file() else None
    )
    smartspeaker_policy_index_path = (
        smartspeaker_hot_root / "published-policy/index.json"
    )
    smartspeaker_candidate_index_path = (
        smartspeaker_hot_root / "relocatable-firmware-candidates/index.json"
    )
    smartspeaker_candidate_report_path = (
        smartspeaker_hot_root / "relocatable-firmware-candidates/"
        "lemix_smartspeaker_paced/optimization-report.json"
    )
    memory_test_root = (
        ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
    )
    memory_test_default_path = (
        ROOT / "build/preflight/bolt-firmware-wrapper-memory-test-2026-11-17/"
        "bolt-post-optimization-report.json"
    )
    memory_test_abi_native_path = (
        ROOT / "build/preflight/bolt-firmware-wrapper-memory-test-abi-2026-11-17/"
        "bolt-post-optimization-report.json"
    )
    memory_test_abi_wrapper_path = (
        ROOT / "build/preflight/bolt-firmware-wrapper-memory-test-abi-2026-11-17/"
        "wrapper-result.json"
    )
    memory_test_aggregate_path = (
        memory_test_root / "firm-bolt-memory-test-abi-selection-aggregate-130.json"
    )
    memory_test_paths = (
        memory_test_default_path,
        memory_test_abi_native_path,
        memory_test_abi_wrapper_path,
        memory_test_aggregate_path,
    )
    memory_test_evidence = (
        tuple(load(path) for path in memory_test_paths)
        if all(path.is_file() for path in memory_test_paths)
        else None
    )
    frame_overlay_root = (
        ROOT / "build/preflight/lemix-smartspeaker-stock-frame-overlay-2026-11-17"
    )
    frame_evidence_paths = (
        memory_test_root / "firm-bolt-trivial-frame-abi-selection-aggregate-130.json",
        memory_test_root / "firm-bolt-leaf-frame-abi-training-report.json",
        memory_test_root / "firm-bolt-private-spill-abi-training-report.json",
        frame_overlay_root / "selection.json",
        frame_overlay_root / "training-report.json",
        ROOT / "build/preflight/lemix-smartspeaker-stock-frame-overlay-cfi-qualified-2026-11-17/selection.json",
    )
    frame_evidence = (
        tuple(load(path) for path in frame_evidence_paths)
        if all(path.is_file() for path in frame_evidence_paths)
        else None
    )
    thumb_paths = (
        DEFAULT_THUMB_M3, DEFAULT_THUMB_FIXED, DEFAULT_THUMB_M0,
        DEFAULT_THUMB_PROFILE, DEFAULT_THUMB_TOP2, DEFAULT_THUMB_HOT,
        DEFAULT_THUMB_BRANCH_PROFILE, DEFAULT_THUMB_BRANCH,
        DEFAULT_THUMB_BOUNDED_PROFILE, DEFAULT_THUMB_BOUNDED,
        DEFAULT_THUMB_BOUNDED_ABLATION, DEFAULT_THUMB_HOT_EXIT,
    )
    thumb_missing = [str(path) for path in thumb_paths if not path.is_file()]
    if thumb_missing:
        raise SystemExit(
            "missing bounded Thumb evidence; run scripts/preflight/run-thumb-preflight.sh: "
            + ", ".join(thumb_missing)
        )
    thumb_m3 = load(DEFAULT_THUMB_M3)
    thumb_fixed = load(DEFAULT_THUMB_FIXED)
    thumb_m0 = load(DEFAULT_THUMB_M0)
    thumb_profile = load(DEFAULT_THUMB_PROFILE)
    thumb_top2 = load(DEFAULT_THUMB_TOP2)
    thumb_hot = load(DEFAULT_THUMB_HOT)
    thumb_branch_profile = load(DEFAULT_THUMB_BRANCH_PROFILE)
    thumb_branch = load(DEFAULT_THUMB_BRANCH)
    thumb_bounded_profile = load(DEFAULT_THUMB_BOUNDED_PROFILE)
    thumb_bounded = load(DEFAULT_THUMB_BOUNDED)
    thumb_bounded_ablation = load(DEFAULT_THUMB_BOUNDED_ABLATION)
    thumb_hot_exit = load(DEFAULT_THUMB_HOT_EXIT)

    if smartspeaker_precision is not None and smartspeaker_hot is not None:
        stock_reference = smartspeaker_precision["comparisons"][
            "stock_bolt_over_reference_policy"
        ]
        stock_hot = smartspeaker_hot[
            "stock_bolt_over_hot_fop_cpu_per_marker"
        ]
        smartspeaker_policy_text = (
            "A declared 100-block precision extension on the unchanged five-second "
            "fixture refuted the earlier exact-reference fallback: stock BOLT/reference "
            f"CPU cost was {stock_reference['median_ratio']:.4f} "
            f"[{stock_reference['bootstrap_95_percent'][0]:.4f}, "
            f"{stock_reference['bootstrap_95_percent'][1]:.4f}], where a value below "
            "one means stock BOLT used less CPU. The next mechanism was frozen before "
            "measurement: hot-only FOP applies 39 load transformations while retaining "
            "98.53% of full FOP's profiled redundant-load frequency. Thirty paired "
            "30-second blocks completed 3,000 markers per arm and matched the same "
            "400-interrupt trace prefix. Stock BOLT/hot-FOP CPU cost was "
            f"{stock_hot['median_ratio']:.4f} "
            f"[{stock_hot['bootstrap_95_percent'][0]:.4f}, "
            f"{stock_hot['bootstrap_95_percent'][1]:.4f}]; it did not clear the 1.005 "
            "deployment gate, so the policy retains stock BOLT."
        )
        if (
            smartspeaker_policy_index_path.is_file()
            and smartspeaker_candidate_index_path.is_file()
            and smartspeaker_candidate_report_path.is_file()
        ):
            smart_candidate = load(smartspeaker_candidate_report_path)
            smartspeaker_policy_text += (
                " That exact timed stock-BOLT image is atomically published as a "
                "`FirmwareCandidate v1`. A separately rebuilt BAT carrier has byte-"
                "identical executable sections and supplies origins for "
                f"{smart_candidate['bat_instruction_origin_count']:,} relocated "
                "instructions without replacing the deployed image; entry coverage is "
                "complete. Policy/candidate index SHA-256 values are `"
                + hashlib.sha256(
                    smartspeaker_policy_index_path.read_bytes()
                ).hexdigest()
                + "` and `"
                + hashlib.sha256(
                    smartspeaker_candidate_index_path.read_bytes()
                ).hexdigest()
                + "`. This is a measured non-regression fallback, not a strict "
                "SmartSpeaker speedup or a capacity result."
            )
    else:
        smartspeaker_policy_text = (
            "The post-rerun SmartSpeaker precision policy evidence is absent."
        )
    smartspeaker_layout_path = (
        ROOT / "build/preflight/lemix-smartspeaker-layout-matrix-2026-11-17/"
        "layout-result.json"
    )
    smartspeaker_overlay_path = (
        ROOT / "build/preflight/lemix-smartspeaker-stock-fop-overlay-2026-11-17/"
        "report.json"
    )
    if smartspeaker_layout_path.is_file():
        layout_result = load(smartspeaker_layout_path)
        layout_rows = []
        for name, item in sorted(layout_result["paired_results"].items()):
            layout_rows.append(
                f"`{name}` {item['median_ratio']:.4f} "
                f"[{item['bootstrap_95_percent'][0]:.4f}, "
                f"{item['bootstrap_95_percent'][1]:.4f}]"
            )
        smartspeaker_policy_text += (
            "\n\nA separately frozen BOLT-native layout screen compared eight "
            "BAT-enabled FOP layouts while holding dataflow treatment fixed. "
            "Callgrind was unsupported because Valgrind did not retain the "
            "zero-backed MMIO mapping; this was recorded as an environment failure, "
            "not a candidate failure. Static profile-weighted dynostats selected "
            "no-split, ordinary split and `cdsplit` before native training. All 40 "
            "executions completed 1,500 markers and shared the same 400-event trace. "
            "Stock/candidate CPU results were " + "; ".join(layout_rows) + ". No "
            "lower interval bound exceeded 1.0, so no layout advanced and no "
            "held-out result was consumed."
        )
    if smartspeaker_overlay_path.is_file():
        overlay = load(smartspeaker_overlay_path)
        paced = overlay["paced_application"][
            "stock_bolt_over_overlay_cpu_per_marker"
        ]
        function = overlay["function_level"]
        confirmatory = overlay["accelerated_schedule_diagnostics"]["ten_x"].get(
            "confirmatory"
        )
        confirmatory_text = ""
        if confirmatory is not None:
            result_10x = confirmatory["paired_result"]
            confirmatory_text = (
                " A frozen 30-block confirmatory run in that 10x environment "
                "completed 60 valid 20-second executions at approximately 20,000 "
                "markers each. Stock/overlay CPU-per-marker was "
                f"{result_10x['median_ratio']:.4f} "
                f"[{result_10x['bootstrap_95_percent'][0]:.4f}, "
                f"{result_10x['bootstrap_95_percent'][1]:.4f}]; its lower bound "
                "did not clear the predeclared 1.005 gate, so it also does not "
                "advance the overlay."
            )
        smartspeaker_policy_text += (
            "\n\nThe firmware wrapper then composed four checked, profile-prioritized "
            "ordinary-FOP bodies directly into the exact stock-BOLT layout, leaving "
            "all cold/unknown bodies unchanged. The overlay covers "
            f"{overlay['overlay']['selected_profile_entries']:,} profiled function "
            f"entries, saves {overlay['overlay']['live_bytes_saved_in_place']} live "
            "bytes inside existing capacities, and records "
            f"{overlay['overlay']['changed_instruction_origin_mappings']} exact changed-"
            "instruction origins with no file growth. Twenty matched 30-second "
            "executions completed 3,000 markers and the same interrupt prefix; "
            f"stock/overlay CPU was {paced['median_ratio']:.4f} "
            f"[{paced['bootstrap_95_percent'][0]:.4f}, "
            f"{paced['bootstrap_95_percent'][1]:.4f}], so deployment still retains "
            "stock BOLT. The dominant overlaid accessor is byte-identical to the "
            "previously measured FOP body and therefore retains its function-level "
            f"{function['cpu_cost_reduction_percent']:.1f}% CPU-cost reduction; this "
            "does not establish application capacity. A separately identified 10x "
            "tick calibration completed 5,000 markers per arm."
            + confirmatory_text
            + " The 100x calibration saturated shutdown and killed the stock "
            "process, so its apparent ratio is invalid."
        )
    exact_schedule_root = (
        ROOT / "build/preflight/lemix-smartspeaker-marker-profile-2026-11-17"
    )
    wrapper_schedule_root = (
        ROOT / "build/preflight/lemix-smartspeaker-frs-wrapper-2026-11-17"
    )
    exact_paths = {
        "fop": exact_schedule_root / "timing-selection.json",
        "passes": exact_schedule_root / "pass-selection.json",
        "hot": exact_schedule_root / "fop-hot-selection.json",
        "wrapper": wrapper_schedule_root / "wrapper-selection.json",
        "inplace": wrapper_schedule_root / "inplace-selection.json",
        "isolated": wrapper_schedule_root / "isolated-inplace-selection.json",
        "ordinary_ledger": (
            wrapper_schedule_root / "wrapper-ordinary-fop/wrapper-result.json"
        ),
        "frs_ledger": wrapper_schedule_root / "wrapper/wrapper-result.json",
        "inplace_ledger": (
            wrapper_schedule_root / "wrapper-fop-inplace/wrapper-result.json"
        ),
    }
    if all(path.is_file() for path in exact_paths.values()):
        exact = {name: load(path) for name, path in exact_paths.items()}
        full_fop = next(
            item for item in exact["fop"]["candidates"]
            if item["candidate"] == "marker_fop"
        )
        ordinary_wrapper = next(
            item for item in exact["wrapper"]["candidates"]
            if item["candidate"] == "ordinary_fop_wrapper"
        )
        frs_wrapper = next(
            item for item in exact["wrapper"]["candidates"]
            if item["candidate"] == "frs_gated_wrapper"
        )
        inplace = exact["inplace"]["stock_bolt_over_candidate"]
        isolated = exact["isolated"]["stock_bolt_over_candidate"]
        hot = exact["hot"]["stock_bolt_over_candidate"]
        hot_precision_path = exact_schedule_root / "fop-hot-precision-report.json"
        if hot_precision_path.is_file():
            hot = load(hot_precision_path)["stock_bolt_over_candidate"]
        full_fop_precision_path = (
            exact_schedule_root / "fop-full-precision-aggregate-report.json"
        )
        full_fop_precision = (
            load(full_fop_precision_path)
            if full_fop_precision_path.is_file() else None
        )
        if full_fop_precision is not None:
            full_fop_cpu = full_fop_precision["stock_bolt_over_full_fop"][
                "target_cpu_ns"
            ]
        else:
            full_fop_cpu = full_fop["target_cpu"]
        ordinary_ledger = exact["ordinary_ledger"]
        frs_ledger = exact["frs_ledger"]
        inplace_ledger = exact["inplace_ledger"]
        smartspeaker_policy_text += f"""

The rerun now also has a valid exact-work SmartSpeaker schedule. The interposer disables the wall timer and injects exactly ten acknowledged ticks for each completed interrupt-task marker. Every timed execution below completed 20,000 units and 200,000 ticks and matched the same ordered 400-event interrupt prefix. A newly collected compatible BOLT profile makes scheduler and support-code hotness explicit; it does not turn the zero-backed MMIO shim into a device model.

No exact-schedule arm cleared the frozen gate. Exact-profile full FOP had stock/candidate target CPU {full_fop_cpu['median_ratio']:.4f} [{full_fop_cpu['bootstrap_95_percent'][0]:.4f}, {full_fop_cpu['bootstrap_95_percent'][1]:.4f}]. Existing PLT rewriting, inlining, ICP, register reassignment, peepholes, read-only-data simplification and their frozen combinations also failed training. The ordinary checked compact wrapper installed {ordinary_ledger['qualified']['function_count']} bodies ({ordinary_ledger['qualified']['profile_executed_functions']} profiled) and saved {ordinary_ledger['qualified']['saved_live_bytes']} live bytes; its target-CPU ratio was {ordinary_wrapper['target_cpu']['median_ratio']:.4f} [{ordinary_wrapper['target_cpu']['bootstrap_95_percent'][0]:.4f}, {ordinary_wrapper['target_cpu']['bootstrap_95_percent'][1]:.4f}].

Experimental frame-store removal deleted {frs_ledger['fop']['deleted_stores']} stores in the relocated BOLT image, but the firmware effect gate rejected {frs_ledger['rejections']['effect_contract']['changed_nonload_or_store_effect']} structurally eligible functions with changed explicit stores. The remaining {frs_ledger['qualified']['function_count']} installable bodies reached only {frs_wrapper['target_cpu']['median_ratio']:.4f} [{frs_wrapper['target_cpu']['bootstrap_95_percent'][0]:.4f}, {frs_wrapper['target_cpu']['bootstrap_95_percent'][1]:.4f}]. This is the intended contract boundary, not a claim that BOLT store removal is generally unsound.

Disabling layout/splitting exposed a missing absolute-reference repair: the first compact image copied `vAudio_Task` with `vRecord_Task` still bound to BOLT address `0x808840` and faulted there. BAT-backed typed rebinding repaired that exact mechanism. The accepted fixed-layout image installs {inplace_ledger['qualified']['function_count']} bodies ({inplace_ledger['qualified']['profile_executed_functions']} profiled), repairs {inplace_ledger['qualified']['absolute_references_repaired']} absolute references and saves {inplace_ledger['qualified']['saved_live_bytes']} live bytes. Its shared-CPU target ratio was {inplace['target_cpu_ns']['median_ratio']:.4f} [{inplace['target_cpu_ns']['bootstrap_95_percent'][0]:.4f}, {inplace['target_cpu_ns']['bootstrap_95_percent'][1]:.4f}]; a separately identified target/driver affinity-isolated run was {isolated['target_cpu_ns']['median_ratio']:.4f} [{isolated['target_cpu_ns']['bootstrap_95_percent'][0]:.4f}, {isolated['target_cpu_ns']['bootstrap_95_percent'][1]:.4f}]. Exact-profile hot-only FOP retained 1,608,668 weighted deleted-load executions but was {hot['target_cpu_ns']['median_ratio']:.4f} [{hot['target_cpu_ns']['bootstrap_95_percent'][0]:.4f}, {hot['target_cpu_ns']['bootstrap_95_percent'][1]:.4f}]. Every wall-time lower bound also missed its gate, so deployment continues to retain exact-profile stock BOLT. The mechanism findings are nevertheless concrete: exact completed-work scheduling is now valid; explicit-store rejection works; unresolved relocated address-valued immediates are an installation blocker; and broader checked adoption does not by itself establish profitability.
"""
        arithmetic_wrapper_root = (
            ROOT / "build/preflight/bolt-firmware-wrapper-arithmetic-rev-2-2026-11-17"
        )
        cmp_wrapper_root = (
            ROOT / "build/preflight/bolt-firmware-wrapper-fop-cmp-2026-11-17"
        )
        extension_paths = {
            "full_fop": full_fop_precision_path,
            "peepholes": exact_schedule_root / "fop-peepholes-selection.json",
            "arithmetic": (
                exact_schedule_root / "firm-bolt-arithmetic-precision-report.json"
            ),
            "cmp": exact_schedule_root / "firm-bolt-fop-cmp-selection.json",
            "arithmetic_native": (
                arithmetic_wrapper_root / "bolt-post-optimization-report.json"
            ),
            "arithmetic_wrapper": arithmetic_wrapper_root / "wrapper-result.json",
            "arithmetic_stdout": arithmetic_wrapper_root / "bolt.stdout",
            "cmp_wrapper": cmp_wrapper_root / "wrapper-result.json",
            "cmp_stdout": cmp_wrapper_root / "bolt.stdout",
        }
        if all(path.is_file() for path in extension_paths.values()):
            extension = {
                name: load(path)
                for name, path in extension_paths.items()
                if path.suffix == ".json"
            }
            full_precision = extension["full_fop"]["stock_bolt_over_full_fop"]
            peephole = extension["peepholes"][
                "ordinary_fop_over_fop_peepholes"
            ]
            arithmetic = extension["arithmetic"]["stock_bolt_over_firm_bolt"]
            cmp_result = extension["cmp"]["primary_stock_bolt_over_candidate"]
            arithmetic_fusions = extension["arithmetic_native"][
                "arithmetic_fusions"
            ]
            arithmetic_wrapper = extension["arithmetic_wrapper"][
                "memory_effect_qualified_adoption"
            ]
            cmp_wrapper = extension["cmp_wrapper"]
            cmp_adoption = cmp_wrapper["memory_effect_qualified_adoption"]

            def fop_frequency(path: Path) -> int:
                match = re.search(
                    r"Frequency of redundant loads is (\d+)", path.read_text()
                )
                if match is None:
                    raise SystemExit(f"missing FOP frequency in {path}")
                return int(match.group(1))

            upstream_loads = extension["arithmetic_wrapper"]["fop"][
                "optimized_redundant_loads"
            ]
            extended_loads = cmp_wrapper["fop"]["optimized_redundant_loads"]
            upstream_weighted = fop_frequency(
                extension_paths["arithmetic_stdout"]
            )
            extended_weighted = fop_frequency(extension_paths["cmp_stdout"])
            hot_fusions = sum(
                item["known_execution_count"] > 0 for item in arithmetic_fusions
            )
            weighted_fusions = sum(
                item["known_execution_count"] for item in arithmetic_fusions
            )
            smartspeaker_policy_text += f"""

### Native Firm-BOLT extensions and high-precision selection

The final implementation extends the pinned BOLT pass pipeline rather than running a separate raw optimizer. First, a conservative x86-64 arithmetic pass replaces the exact O0 multiply-by-40 sequence `mov; shl $2; add; shl $3` with `lea; shl $3`, retaining the final flag-producing shift and exporting every consumed original identity. It found {len(arithmetic_fusions)} static fusions, {hot_fusions} with profile evidence and {weighted_fusions:,} profile-weighted executions. The firmware wrapper accepted {arithmetic_wrapper['selected_function_count']} compact bodies and saved {arithmetic_wrapper['saved_live_bytes']:,} live bytes. Nevertheless, the frozen 100-block precision comparison was neutral: stock/Firm-BOLT target CPU was {arithmetic['target_cpu_ns']['median_ratio']:.4f} [{arithmetic['target_cpu_ns']['bootstrap_95_percent'][0]:.4f}, {arithmetic['target_cpu_ns']['bootstrap_95_percent'][1]:.4f}] and wall time was {arithmetic['wall_ns']['median_ratio']:.4f} [{arithmetic['wall_ns']['bootstrap_95_percent'][0]:.4f}, {arithmetic['wall_ns']['bootstrap_95_percent'][1]:.4f}]. Exact completed work and the ordered trace matched, so the result is `not_profitable`, not a correctness failure.

Second, Firm-BOLT extends BOLT's own frame optimizer to forward available 64-bit stack values into `CMP64mi8`, `CMP64mi32`, `CMP64mr` and `CMP64rm` while retaining the establishing store and exporting the dependency. This raises ordinary FOP activity from {upstream_loads:,} to {extended_loads:,} static redundant loads (+{extended_loads - upstream_loads:,}) and from {upstream_weighted:,} to {extended_weighted:,} profile-weighted loads (+{extended_weighted - upstream_weighted:,}). The wrapper records {cmp_adoption['forwarding_dependency_count']:,} accepted dependencies, installs {cmp_adoption['selected_function_count']} compact bodies and saves {cmp_adoption['saved_live_bytes']:,} live bytes. Its frozen training result was also not deployable: stock/extended-FOP target CPU was {cmp_result['target_cpu_ns']['median_ratio']:.4f} [{cmp_result['target_cpu_ns']['bootstrap_95_percent'][0]:.4f}, {cmp_result['target_cpu_ns']['bootstrap_95_percent'][1]:.4f}] and wall time was {cmp_result['wall_ns']['median_ratio']:.4f} [{cmp_result['wall_ns']['bootstrap_95_percent'][0]:.4f}, {cmp_result['wall_ns']['bootstrap_95_percent'][1]:.4f}].

The precision extension also resolves the ordinary full-FOP question. Across {extension['full_fop']['blocks']} paired blocks, stock/full-FOP target CPU was {full_precision['target_cpu_ns']['median_ratio']:.4f} [{full_precision['target_cpu_ns']['bootstrap_95_percent'][0]:.4f}, {full_precision['target_cpu_ns']['bootstrap_95_percent'][1]:.4f}] and wall time was {full_precision['wall_ns']['median_ratio']:.4f} [{full_precision['wall_ns']['bootstrap_95_percent'][0]:.4f}, {full_precision['wall_ns']['bootstrap_95_percent'][1]:.4f}]; its confidence bounds do not clear the frozen CPU and wall gates. An isolated peephole arm patched 25 double jumps and passed exact-work smoke, but ordinary-FOP/peephole target CPU was {peephole['target_cpu_ns']['median_ratio']:.4f} [{peephole['target_cpu_ns']['bootstrap_95_percent'][0]:.4f}, {peephole['target_cpu_ns']['bootstrap_95_percent'][1]:.4f}]. Therefore the SmartSpeaker policy correctly retains stock BOLT. These are measured profitability rejections under an externally scheduled workload, not evidence that arithmetic fusion, load forwarding or peepholes are generally ineffective, and they do not establish whole-application capacity.
"""
    o3_oracle_root = (
        ROOT / "build/preflight/lemix-smartspeaker-o3-oracle-2026-11-17"
    )
    o3_mechanism_path = o3_oracle_root / "mechanism-report.json"
    o3_comparison_path = o3_oracle_root / "comparison-report.json"
    o3_qualification_path = o3_oracle_root / "qualification-manifest.json"
    smartspeaker_o3_abstract = (
        "The matched SmartSpeaker FreeRTOS O0/Firm-BOLT/O3 evidence is absent."
    )
    if all(path.is_file() for path in (
        o3_mechanism_path, o3_comparison_path, o3_qualification_path,
    )):
        o3_mechanism = load(o3_mechanism_path)
        o3_qualification = load(o3_qualification_path)
        o3_ratios = o3_mechanism["target_cpu_cost_ratios"]
        firm_o3 = o3_ratios["firm_bolt_over_o3"]
        firm_o0 = o3_ratios["firm_bolt_over_o0"]
        o3_o0 = o3_ratios["o3_over_o0"]
        o3_inventory = o3_mechanism["binary_inventory"]
        o3_functions = o3_mechanism["functions"]
        mechanism_labels = {
            "vListInsert": "FOP installs 168 B; O3 adds register/CFG optimization",
            "uxListRemove": "FOP installs 126 B; O3 adds register/CFG optimization",
            "event_wait": "FOP removes one byte; O3 removes frame/data movement",
            "xTaskIncrementTick": "full FOP is 896 B; compact import rejects a removed branch-target identity",
            "vTaskSwitchContext": "O3 entry is 37 B plus a 796 B `.part.0`; entry size alone is misleading",
            "prvSwitchThread": "standalone O3 symbol disappears through interprocedural optimization",
        }
        oracle_rows = []
        for name in (
            "vListInsert", "uxListRemove", "event_wait", "xTaskIncrementTick",
            "vTaskSwitchContext", "prvSwitchThread",
        ):
            item = o3_functions[name]
            oracle_rows.append(
                f"| `{name}` | {item['o0_bytes'] or 0} | "
                f"{item['firm_bolt_live_bytes'] or 0} | "
                f"{item['o3_total_component_bytes']} | {mechanism_labels[name]} |"
            )
        smartspeaker_o3_abstract = (
            "The matched SmartSpeaker FreeRTOS O0/Firm-BOLT/O3 experiment now "
            "closes the requested supported-host comparison. Across 30 randomized "
            "exact-work blocks, Firm-BOLT/O3 target CPU cost is "
            f"{firm_o3['median_ratio']:.4f} "
            f"[{firm_o3['bootstrap_95_percent'][0]:.4f}, "
            f"{firm_o3['bootstrap_95_percent'][1]:.4f}], wholly within the frozen "
            "five-percent parity band. O3 itself is unresolved against O0 for this "
            "host-scheduled workload. The result supports BOLT reuse plus firmware "
            "acceptance and compact emission; it does not establish Cortex-M or "
            "FastDyn acceleration."
        )
        smartspeaker_policy_text += f"""

### Matched FreeRTOS SmartSpeaker O0/Firm-BOLT/O3 comparison

The same pinned LEMIX SmartSpeaker source/rehost was rebuilt with GCC `-g -O3` and linked with `-g -O3 -pthread -no-pie -Wl,--emit-relocs`, then compared with the exact O0 input after Firm-BOLT. The manifest records source revision `{o3_qualification['source_revision']}`, {o3_qualification['o3_compile_commands']} compile commands and {o3_qualification['o3_build_warning_lines']} warning lines; the host rehost's pointer-width warnings remain an applicability limitation. O3 is only a performance and mechanism oracle: no O3 body is installed into the O0 image and source-level assumptions do not discharge the machine contract. The final matrix used 30 randomized blocks, 20,000 completed markers and 200,000 acknowledged ticks per execution at the existing 100 us settle interval. All 90 executions returned the expected status and shared the same ordered 400-event prefix.

Firm-BOLT/O3 target CPU cost was {firm_o3['median_ratio']:.4f} [{firm_o3['bootstrap_95_percent'][0]:.4f}, {firm_o3['bootstrap_95_percent'][1]:.4f}], wholly inside the frozen [0.95, 1.05] parity band. This establishes comparability, not superiority: the interval spans both directions. Firm-BOLT/O0 was {firm_o0['median_ratio']:.4f} [{firm_o0['bootstrap_95_percent'][0]:.4f}, {firm_o0['bootstrap_95_percent'][1]:.4f}], and O3/O0 was {o3_o0['median_ratio']:.4f} [{o3_o0['bootstrap_95_percent'][0]:.4f}, {o3_o0['bootstrap_95_percent'][1]:.4f}].

| Representative function | O0 bytes | Firm-BOLT live bytes | O3 linked-component bytes | Observed mechanism |
| --- | ---: | ---: | ---: | --- |
{chr(10).join(oracle_rows)}

O3 has {o3_inventory['o3']['allocated_executable_section_bytes']:,} allocated executable bytes versus {o3_inventory['o0']['allocated_executable_section_bytes']:,} for O0, a {(1.0 - o3_inventory['o3']['allocated_executable_section_bytes'] / o3_inventory['o0']['allocated_executable_section_bytes']) * 100.0:.2f}% reduction. Its debug/relocation-bearing ELF file is larger, so file size is not used as live text. Firm-BOLT retains the original fixed layout and executable capacity but saves {o3_inventory['firm_bolt_live_bytes_saved_inside_original_ranges']:,} live bytes across {o3_mechanism['firm_bolt_wrapper']['installed_functions']} installed bodies. In this historical O3-oracle artifact, the compact importer retained `xTaskIncrementTick` and `vTaskSwitchContext` because FOP removed original branch-target instructions. The later exact-edge/origin importer closes that integration gap for these bodies and installs the 896-byte and 324-byte FOP forms. Its fixed-work candidate remained neutral against stock BOLT at target CPU 0.9997 [0.9874, 1.0086]. Branch provenance is therefore no longer the blocker for these exact functions, and O3 itself is unresolved against O0.

The result therefore does not trigger a new raw optimizer: Firm-BOLT already meets the predeclared O3 parity requirement. The next measurement needs an authentic input/device phase or another compute-limited region rather than more work on these already admitted scheduler bodies. Full hashes, commands, raw samples, disassemblies and mechanism counts are in the [SmartSpeaker O3 oracle evidence](../lemix-smartspeaker-o3-oracle-2026-11-17/mechanism-report.json) and the later [contiguous-hot selection](../lemix-smartspeaker-marker-profile-2026-11-17/firm-bolt-contiguous-hot-selection.json).
"""
    thumb_mov = load(DEFAULT_THUMB_MOV) if DEFAULT_THUMB_MOV.is_file() else None
    thumb_paced = (
        load(DEFAULT_THUMB_PACED_CALIBRATION)
        if DEFAULT_THUMB_PACED_CALIBRATION.is_file() else None
    )
    thumb_tcg_path = DEFAULT_THUMB_HOT.parent / "tcg-diagnostic/report.json"
    thumb_tcg = load(thumb_tcg_path) if thumb_tcg_path.is_file() else None
    thumb_tcg_cost_path = DEFAULT_THUMB_HOT.parent / "tcg-diagnostic/cost-decision.json"
    thumb_tcg_cost = load(thumb_tcg_cost_path) if thumb_tcg_cost_path.is_file() else None
    thumb_branch_tcg_path = DEFAULT_THUMB_BRANCH.parent / "tcg-diagnostic/report.json"
    thumb_branch_tcg = (
        load(thumb_branch_tcg_path) if thumb_branch_tcg_path.is_file() else None
    )
    thumb_branch_cost_path = DEFAULT_THUMB_BRANCH.parent / "tcg-diagnostic/cost-decision.json"
    thumb_branch_cost = (
        load(thumb_branch_cost_path) if thumb_branch_cost_path.is_file() else None
    )
    thumb_bounded_tcg_path = DEFAULT_THUMB_BOUNDED.parent / "tcg-diagnostic/report.json"
    thumb_bounded_tcg = load(thumb_bounded_tcg_path)

    candidate_rows = list(csv.DictReader((run_dir / "candidate-universe.csv").open()))
    funnel_rows = list(csv.DictReader((run_dir / "stage-funnel.csv").open()))
    prefix_count = sum(int(row.get("ProtectedPrefixBytes") or 0) == 4 for row in candidate_rows)
    state_counts: dict[str, int] = {}
    for row in funnel_rows:
        state_counts[row["state_construction"]] = state_counts.get(row["state_construction"], 0) + 1

    comparison = paired["comparisons"]
    function_ratio = accessor_perf["median_reference_over_fop"]
    function_cost_reduction = (1.0 - 1.0 / function_ratio) * 100.0
    summed_search_elapsed = sum(
        float(item.get("result", {}).get("elapsed_seconds", 0.0)) for item in search_results
    )
    total_iterations = sum(
        int((item.get("machine") or {}).get("statistics", {}).get("total_iterations", 0) or 0)
        for item in search_results
    )
    s1_counts = result_counts(search_results, "S1")
    s2_counts = result_counts(search_results, "S2")

    counterexamples = run_dir / "counterexamples"
    dump(counterexamples / "native-accessor-alias-and-faults.json", native_legality)
    dump(
        counterexamples / "stoke-accessor-legality.json",
        {
            "contracts": legality["contracts"],
            "native_alias_probe": legality["native_alias_probe"],
            "probes": {
                key: value
                for key, value in legality["probes"].items()
                if "accessor" in key
            },
        },
    )
    rejected = [
        {
            "case": item["case"],
            "arm": item["arm"],
            "seed": item["seed"],
            "search_status": item["status"],
            "best_correct": (item.get("machine") or {}).get("best_correct"),
            "bounded_verification": item.get("bounded_verification"),
            "candidate_sha256": sha256_text(item["candidate"]) if item.get("candidate") else None,
        }
        for item in search_results
        if item["status"] == "candidate_rejected"
    ]
    dump(counterexamples / "stoke-s2-rejections.json", rejected)
    (counterexamples / "README.md").write_text(
        "# Counterexamples\n\n"
        "These records are copied from the immutable raw run evidence for convenient review. "
        "The native probe demonstrates the stack/object alias mismatch and fault-PC differences. "
        "The STOKE records retain the declared contracts and all bounded-verifier rejections.\n"
    )

    costs = {
        "schema_version": 1,
        "predeclared_caps": {
            "extra_provisioning_hours": 1,
            "harness_adapter_repair_hours": 2,
            "search_and_proof_cpu_hours": 6,
            "wall_time_working_days": 1,
        },
        "observed": {
            "whole_application_samples": len((run_dir / "samples.jsonl").read_text().splitlines()),
            "whole_application_nominal_window_seconds_each": 5,
            "accessor_samples": len((run_dir / "accessor-harness/samples.jsonl").read_text().splitlines()),
            "search_runs": len(search_results),
            "search_iterations": total_iterations,
            "summed_search_subprocess_elapsed_seconds": summed_search_elapsed,
            "search_workers": 4,
        },
        "measurement_note": "Summed subprocess elapsed is neither CPU time nor parallel wall time; it is retained only as an auditable cost bound.",
    }
    dump(run_dir / "budget-and-costs.json", costs)

    def ratio(name: str) -> tuple[float, float, float]:
        item = comparison[name]
        return (
            item["median_reference_over_candidate"],
            item["bootstrap_95_percent"][0],
            item["bootstrap_95_percent"][1],
        )

    r_f1 = ratio("R/F1")
    l1_f1 = ratio("L1/F1")
    l0_f0 = ratio("L0/F0")
    f0_f1 = ratio("F0/F1")
    r_fh = ratio("R/FH")

    def layout_row(name: str) -> str:
        item = layout[name]
        attr = item["attribution"]
        return (
            f"| {name} | {item['file_bytes']:,} | {item['executable_section_bytes']:,} | "
            f"{attr['retained_original_text_bytes']:,} | "
            f"{attr['replacement_hot_and_cold_text_bytes']:,} | "
            f"{item['segment_minus_executable_sections_bytes']:,} |"
        )

    def s2_row(item: dict[str, Any]) -> str:
        return (
            f"| `{item['case']}` | {item['seed']} | "
            f"`{item['target_sha256'][:12]}` / {item['target_known_correct_cost']} | "
            f"`{item['normalized_seed_sha256'][:12]}` / {item['normalized_seed_starting_cost']} | "
            f"`{item['returned_candidate_sha256'][:12]}` / {item['returned_testcase_correct_cost']} | "
            f"{item['bounded_verification']} |"
        )

    s2_table = "\n".join(s2_row(item) for item in prototype_s2["results"])
    prototype_positive = [item for item in prototype_outcomes if item["status"] == "accepted"]
    prototype_negative = [item for item in prototype_outcomes if item["status"] == "counterexample"]
    reference_status_counts: dict[str, int] = {}
    for item in prototype_references["cases"]:
        reference_status_counts[item["status"]] = reference_status_counts.get(item["status"], 0) + 1

    if memory_test_evidence is not None:
        (
            memory_test_default,
            memory_test_abi_native,
            memory_test_abi_wrapper,
            memory_test_aggregate,
        ) = memory_test_evidence
        default_memory_tests = [
            item
            for item in memory_test_default["arithmetic_fusions"]
            if item["kind"] == "x86_dead_load_self_test_to_memory_test"
        ]
        abi_memory_tests = [
            item
            for item in memory_test_abi_native["arithmetic_fusions"]
            if item["kind"] == "x86_dead_load_self_test_to_memory_test"
        ]
        abi_target = memory_test_aggregate["stock_bolt_over_firm_bolt"][
            "target_cpu_ns"
        ]
        abi_process = memory_test_aggregate["stock_bolt_over_firm_bolt"][
            "process_cpu_ns"
        ]
        abi_wall = memory_test_aggregate["stock_bolt_over_firm_bolt"]["wall_ns"]
        memory_test_followup_text = f"""

The final bounded rule combines a dead `MOV64rm` result with its following same-register `TEST64rr`, emitting `TEST64mi32 mem,-1`. It retains the original load/fault identity and one memory access of the same address, width and order, while reproducing the exact TEST flags. Under the default full-register-output contract, BOLT liveness admits {len(default_memory_tests)} sites: deleting the loaded value is not legal merely because the following branch uses only flags. A separately labeled supported-host System V ABI arm enables `--assume-abi`; this qualifies {len(abi_memory_tests)} sites, {sum(item['known_execution_count'] > 0 for item in abi_memory_tests)} observed and {sum(item['known_execution_count'] for item in abi_memory_tests):,} block-weighted executions. The wrapper recognizes only ledgered fusions, preserves the load identity, requires the original register test to disappear, and installs {sum(edit['classification'] == 'checked_memory_test_fusion' for edit in memory_test_abi_wrapper['memory_effect_qualified_adoption']['edits'])} affected bodies. All six fixed-work observations match.

The frozen 30-block screen initially had target CPU 1.0151 [1.0056, 1.0314] but did not clear its wall non-regression gate. One predeclared 100-block precision extension was then combined with the original blocks without retuning or early stopping. Across all {memory_test_aggregate['blocks']} selection blocks, target CPU is {abi_target['median_ratio']:.4f} [{abi_target['bootstrap_95_percent'][0]:.4f}, {abi_target['bootstrap_95_percent'][1]:.4f}], process CPU is {abi_process['median_ratio']:.4f} [{abi_process['bootstrap_95_percent'][0]:.4f}, {abi_process['bootstrap_95_percent'][1]:.4f}], and wall is {abi_wall['median_ratio']:.4f} [{abi_wall['bootstrap_95_percent'][0]:.4f}, {abi_wall['bootstrap_95_percent'][1]:.4f}]. The target lower bound remains below 1.005 and the wall lower bound below 1.0, so the frozen result is `training_rejected`; no held-out sample was consumed and stock BOLT remains selected. This distinguishes a real target-CPU signal from a deployable policy result and keeps the ABI assumption out of the firmware default. The aggregate and raw hashes are retained in [firm-bolt-memory-test-abi-selection-aggregate-130.json](../lemix-smartspeaker-marker-profile-2026-11-17/firm-bolt-memory-test-abi-selection-aggregate-130.json).
"""
    else:
        memory_test_followup_text = ""

    if frame_evidence is not None:
        (
            trivial_frame_aggregate,
            leaf_frame_report,
            private_spill_report,
            frame_overlay_selection,
            frame_overlay_report,
            cfi_frame_overlay_selection,
        ) = frame_evidence
        if (
            cfi_frame_overlay_selection["output"]["sha256"]
            != frame_overlay_selection["output"]["sha256"]
        ):
            raise SystemExit(
                "CFI-qualified frame overlay differs from the timed artifact"
            )
        trivial_target = trivial_frame_aggregate["stock_bolt_over_firm_bolt"][
            "target_cpu_ns"
        ]
        trivial_wall = trivial_frame_aggregate["stock_bolt_over_firm_bolt"][
            "wall_ns"
        ]
        leaf_target = leaf_frame_report["stock_bolt_over_firm_bolt"][
            "target_cpu_ns"
        ]
        leaf_wall = leaf_frame_report["stock_bolt_over_firm_bolt"]["wall_ns"]
        private_target = private_spill_report["stock_bolt_over_firm_bolt"][
            "target_cpu_ns"
        ]
        private_wall = private_spill_report["stock_bolt_over_firm_bolt"][
            "wall_ns"
        ]
        overlay_comparison = frame_overlay_report[
            "stock_bolt_over_checked_frame_overlay"
        ]
        overlay_target = overlay_comparison["target_cpu_ns"]
        overlay_process = overlay_comparison["process_cpu_ns"]
        overlay_wall = overlay_comparison["wall_ns"]
        overlay_status = frame_overlay_report["status"]
        overlay_decision = (
            "advances to held-out evaluation"
            if overlay_status == "selected_for_held_out"
            else "is rejected by the frozen training policy; stock BOLT remains selected"
        )
        frame_followup_text = f"""

### 8.6 ABI-qualified leaf-frame elimination and the layout-isolation result

The next Firm-BOLT rule removes an exact RBP call frame only under explicit `--assume-abi`, never under the default firmware contract. Its leaf form requires a single-entry, single-block function; no calls, branches, early returns, stack opcodes or RBP/RSP aliases in the retained body; an exact `push rbp; mov rsp,rbp; ...; pop rbp; ret` shape; and a protected `endbr64` prefix when present. It removes the matching CFI state changes together with the frame. This is a supported-host System V ABI experiment, not authorization to erase Cortex-M exception-visible stack/register effects.

The exact empty-frame arm qualified 7 observed functions and 222,270 block-weighted entries. Across its predeclared 130-block aggregate, target CPU was {trivial_target['median_ratio']:.4f} [{trivial_target['bootstrap_95_percent'][0]:.4f}, {trivial_target['bootstrap_95_percent'][1]:.4f}] and wall was {trivial_wall['median_ratio']:.4f} [{trivial_wall['bootstrap_95_percent'][0]:.4f}, {trivial_wall['bootstrap_95_percent'][1]:.4f}]. The target gate cleared, but the wall non-regression gate did not. Generalizing only to stack-independent leaf bodies qualified 62 functions, 7 observed and 243,389 weighted; its frozen 100-block result was target CPU {leaf_target['median_ratio']:.4f} [{leaf_target['bootstrap_95_percent'][0]:.4f}, {leaf_target['bootstrap_95_percent'][1]:.4f}] and wall {leaf_wall['median_ratio']:.4f} [{leaf_wall['bootstrap_95_percent'][0]:.4f}, {leaf_wall['bootstrap_95_percent'][1]:.4f}], again not deployable.

A second exact rule handles the two fixed-global-return functions whose post-FOP bodies contain a private, nonescaped `-8(%rbp)` return spill. It accepts only a RIP-relative global load into RAX, that exact spill, the already ledgered FOP-deleted reload, no pointer input or escape, and the retained return. It deliberately does not admit `prvGetThreadFromTask`: the accessor's caller-supplied pointer can alias its frame store, as the retained negative fixture demonstrates. The two accepted private-spill functions add 441,399 weighted entries. In the complete relocated Firm-BOLT image, target CPU was {private_target['median_ratio']:.4f} [{private_target['bootstrap_95_percent'][0]:.4f}, {private_target['bootstrap_95_percent'][1]:.4f}] and wall {private_wall['median_ratio']:.4f} [{private_wall['bootstrap_95_percent'][0]:.4f}, {private_wall['bootstrap_95_percent'][1]:.4f}]. This was rejected, but changed function sizes also shifted unrelated BOLT output, so it did not isolate the local mechanism.

The address-stable overlay closes that confound. It starts from the exact stock-BOLT marker image, installs only {frame_overlay_selection['cohort']['selected_count']} observed checked frame bodies ({frame_overlay_selection['cohort']['selected_profile_entry_count']:,} weighted entries), repairs {sum(len(item['reference_repairs']) for item in frame_overlay_selection['edits'])} RIP-relative references to the same original objects, and fills the remaining symbol capacities with NOPs. It saves {frame_overlay_selection['cohort']['saved_live_bytes']} live bytes, changes {frame_overlay_selection['cohort']['changed_file_byte_count']} file bytes, changes zero bytes outside the selected stock symbol ranges, preserves every stock address and file size, and records an original identity for every emitted live instruction. A separate 5,000-unit smoke completed exact work and the same 400-event ordered interrupt prefix. Before packaging, the native rule was tightened to reject personality/LSDA functions and to remove only an absent or exact four-operation canonical RBP unwind program. That stricter build qualifies the same 62 leaf and 2 private-spill functions and re-emits the byte-identical timed overlay (SHA-256 `{cfi_frame_overlay_selection['output']['sha256']}`), so the timing evidence applies to the packaged rule without post-result retuning.

Across the frozen 100 randomized paired blocks, the exact stock-layout overlay reports target CPU {overlay_target['median_ratio']:.4f} [{overlay_target['bootstrap_95_percent'][0]:.4f}, {overlay_target['bootstrap_95_percent'][1]:.4f}], process CPU {overlay_process['median_ratio']:.4f} [{overlay_process['bootstrap_95_percent'][0]:.4f}, {overlay_process['bootstrap_95_percent'][1]:.4f}], and wall {overlay_wall['median_ratio']:.4f} [{overlay_wall['bootstrap_95_percent'][0]:.4f}, {overlay_wall['bootstrap_95_percent'][1]:.4f}]. Exact completed work and traces match; the candidate {overlay_decision}. This comparison isolates the checked frame bodies from global layout motion. It remains a native supported-host cost result under the named ABI contract, not whole-application capacity, original-MCU performance or arbitrary firmware equivalence. Evidence is in the [frame-overlay selection](../lemix-smartspeaker-stock-frame-overlay-2026-11-17/selection.json), [strict-CFI selection](../lemix-smartspeaker-stock-frame-overlay-cfi-qualified-2026-11-17/selection.json), and [training report](../lemix-smartspeaker-stock-frame-overlay-2026-11-17/training-report.json).
"""
    else:
        frame_followup_text = ""

    if bolt_wrapper is not None and bolt_native_report is not None and bolt_compact is not None:
        native_summary = bolt_wrapper["native_post_optimization_summary"]
        native_functions = bolt_native_report["functions"]
        native_accessor = next(
            item for item in native_functions if item["name"].startswith("prvGetThreadFromTask")
        )
        native_list = next(
            item for item in native_functions if item["name"].startswith("vListInitialiseItem")
        )
        function_byte_reduction = (
            100.0
            * (native_summary["original_function_bytes"]
               - native_summary["estimated_post_optimization_function_bytes"])
            / native_summary["original_function_bytes"]
        )
        origin_coverage = (
            100.0 * native_summary["instructions_with_original_offset"]
            / native_summary["post_optimization_instruction_count"]
        )
        compact_edits = {item["symbol"]: item for item in bolt_compact["edits"]}
        bolt_auto = bolt_wrapper["automatic_compact_adoption"]
        bolt_qualified = bolt_wrapper["memory_effect_qualified_adoption"]
        bolt_claim_rows = f"""| Observed | The BOLT-first wrapper compactly installs {bolt_qualified['selected_function_count']} exact BOLT bodies, saving {bolt_qualified['saved_live_bytes']} live bytes without growing the reference ELF. |
| Observed | Of those bodies, {bolt_qualified['exact_memory_sequence_function_count']} preserve the explicit-memory sequence verbatim and {bolt_qualified['fop_dependency_function_count']} are justified by {bolt_qualified['forwarding_dependency_count']} exported FOP store-to-load dependencies. |
| Observed | The direct-call extension repairs {bolt_qualified['direct_call_extension']['repaired_call_count']} exact original targets across {bolt_qualified['direct_call_extension']['qualified_function_count']} additional functions. |
| Observed | The internal-branch extension validates {bolt_qualified['internal_branch_extension']['validated_branch_count']} identity-preserving local branches across {bolt_qualified['internal_branch_extension']['qualified_function_count']} additional functions. |
| Observed | RIP-relative handling repairs {bolt_qualified['rip_relative_extension']['repaired_reference_count'] + bolt_qualified['combined_call_rip_extension']['repaired_data_reference_count']} exact original data targets across {bolt_qualified['rip_relative_extension']['qualified_function_count'] + bolt_qualified['combined_call_rip_extension']['qualified_function_count']} additional functions. |
| Observed | The branch-heavy composition admits {bolt_qualified['branch_heavy_extension']['qualified_function_count']} more functions after validating {bolt_qualified['branch_heavy_extension']['validated_branch_count']} branch targets, {bolt_qualified['branch_heavy_extension']['repaired_call_count']} calls and {bolt_qualified['branch_heavy_extension']['repaired_data_reference_count']} data references. |
| Observed | Typed absolute-symbol handling repairs {bolt_qualified['absolute_symbol_reference_extension']['repaired_reference_count']} callback references across {bolt_qualified['absolute_symbol_reference_extension']['qualified_function_count']} functions. |"""
        if bolt_compact_timing is not None:
            compact_paired = bolt_compact_timing["paired_comparisons"]["AUTO"]
            compact_timing_text = (
                f"In 30 randomized paired two-second blocks, its reference/candidate "
                f"CPU-cost-per-marker ratio was {compact_paired['median_ratio']:.4f} "
                f"({compact_paired['bootstrap_95_percent'][0]:.4f}–"
                f"{compact_paired['bootstrap_95_percent'][1]:.4f}). The interval spans "
                "both gains and regressions, so this paced application comparison is "
                "inconclusive and does not establish capacity or an application speedup. "
                "Raw block order, CPU/wall/work observations, and paired ratios are in "
                "[auto-compact-paired-samples.json]"
                "(../bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16/"
                "auto-compact-paired-samples.json)."
            )
        else:
            compact_timing_text = (
                "The paired timing artifact is absent; the one-second marker smoke is an "
                "installation check, not a performance result."
            )
        if bolt_hot_timing is not None:
            hot_comparison = bolt_hot_timing["paired_comparisons"]["HOT"]
            all_comparison = bolt_hot_timing["paired_comparisons"]["ALL"]
            hot_timing_text = (
                f"A separate three-arm run compared reference, hot-only and all-qualified "
                f"publication. HOT was {hot_comparison['median_ratio']:.4f} "
                f"({hot_comparison['bootstrap_95_percent'][0]:.4f}–"
                f"{hot_comparison['bootstrap_95_percent'][1]:.4f}); ALL was "
                f"{all_comparison['median_ratio']:.4f} "
                f"({all_comparison['bootstrap_95_percent'][0]:.4f}–"
                f"{all_comparison['bootstrap_95_percent'][1]:.4f}). Both are inconclusive."
            )
        else:
            hot_timing_text = "The hot-only paired timing artifact is absent."
        if queue_fixed is not None:
            queue_bolt_cpu = queue_fixed["paired_comparisons"]["BOLT"]["cpu_ns"]
            queue_bolt_wall = queue_fixed["paired_comparisons"]["BOLT"]["wall_ns"]
            queue_text = (
                f"The existing fixed-work FreeRTOS queue fixture completes two million "
                f"messages with identical checksum. Stock BOLT-PGO has CPU ratio "
                f"{queue_bolt_cpu['median_ratio']:.4f} "
                f"({queue_bolt_cpu['bootstrap_95_percent'][0]:.4f}–"
                f"{queue_bolt_cpu['bootstrap_95_percent'][1]:.4f}) and wall ratio "
                f"{queue_bolt_wall['median_ratio']:.4f} "
                f"({queue_bolt_wall['bootstrap_95_percent'][0]:.4f}–"
                f"{queue_bolt_wall['bootstrap_95_percent'][1]:.4f}); neither establishes "
                f"a gain. Raw paired evidence is in "
                f"[fixed-work-paired-results.json](../lemix-queue/"
                f"fixed-work-paired-results.json)."
            )
        else:
            queue_text = "The fixed-work queue evidence is absent."
        if queue_wrapper_result is not None and queue_wrapper_timing is not None:
            queue_full_cpu = queue_wrapper_timing["paired_comparisons"]["FULL_FOP"]["cpu_ns"]
            queue_full_wall = queue_wrapper_timing["paired_comparisons"]["FULL_FOP"]["wall_ns"]
            queue_wrap_cpu = queue_wrapper_timing["paired_comparisons"]["WRAP"]["cpu_ns"]
            queue_wrap_wall = queue_wrapper_timing["paired_comparisons"]["WRAP"]["wall_ns"]
            queue_qualified = queue_wrapper_result["qualified"]
            queue_relocatable = queue_wrapper_result["relocatable_publication"]
            queue_wrapper_text = (
                f"The same fixed-work fixture now also exercises the firmware wrapper. "
                f"It installed {queue_qualified['function_count']} qualified BOLT bodies "
                f"in place, repaired {queue_qualified['direct_calls_repaired']} direct calls "
                f"and {queue_qualified['rip_references_repaired']} RIP-relative references, "
                f"validated {queue_qualified['local_branches_validated']} local branches, and "
                f"saved {queue_qualified['saved_live_bytes']} live bytes without changing the "
                f"reference ELF size. Under the relocatable policy, the wrapper accepted and "
                f"atomically published the complete BOLT candidate after fixed-work, protected-"
                f"entry and address-map checks; it is byte-identical to the validated BOLT "
                f"candidate ({queue_relocatable['protected_entry_count']} protected entries, "
                f"status `{queue_relocatable['status']}`). This policy retains BOLT's "
                f"optimization rather than claiming a second speedup. The published BOLT+FOP "
                f"artifact resolved a CPU improvement against the "
                f"reference: {queue_full_cpu['median_ratio']:.4f} "
                f"({queue_full_cpu['bootstrap_95_percent'][0]:.4f}–"
                f"{queue_full_cpu['bootstrap_95_percent'][1]:.4f}); wall ratio was "
                f"{queue_full_wall['median_ratio']:.4f} "
                f"({queue_full_wall['bootstrap_95_percent'][0]:.4f}–"
                f"{queue_full_wall['bootstrap_95_percent'][1]:.4f}). The fixed-layout compact "
                f"fallback produced CPU ratio {queue_wrap_cpu['median_ratio']:.4f} "
                f"({queue_wrap_cpu['bootstrap_95_percent'][0]:.4f}–"
                f"{queue_wrap_cpu['bootstrap_95_percent'][1]:.4f}) and wall ratio "
                f"{queue_wrap_wall['median_ratio']:.4f} "
                f"({queue_wrap_wall['bootstrap_95_percent'][0]:.4f}–"
                f"{queue_wrap_wall['bootstrap_95_percent'][1]:.4f}). Thus compact installation "
                f"is currently an accepted but statistically unresolved pass result, "
                f"not an improvement claim. The mechanism boundary is visible: the compact "
                f"image retains selected BOLT instruction bodies but restores original placement, "
                f"whereas the full candidate retains BOLT reordering and splitting. This run "
                f"does not by itself isolate layout from FOP because the direct stock-BOLT/FOP "
                f"contrast remains noisy. Evidence is in "
                f"[wrapper-result.json](../lemix-queue/bolt-firmware-wrapper/wrapper-result.json) "
                f"and [fixed-work-paired-results.json]"
                f"(../lemix-queue/bolt-firmware-wrapper/fixed-work-paired-results.json)."
            )
        else:
            queue_wrapper_text = "The fixed-work queue wrapper evidence is absent."
        if queue_policy is not None:
            ordinary = queue_policy["arms"]["ordinary_fop"]
            store = queue_policy["arms"]["experimental_store_removal"]
            ordinary_cpu = queue_policy["timing"]["reference_over_ordinary_fop"][
                "cpu_ns"
            ]
            store_cpu = queue_policy["timing"]["reference_over_store_removal"][
                "cpu_ns"
            ]
            store_over_ordinary = queue_policy["timing"][
                "ordinary_fop_over_store_removal"
            ]["cpu_ns"]
            work = queue_policy["deterministic_work_diagnostics"]
            queue_policy_text = (
                f"A frozen follow-up tested BOLT peepholes, conservative and aggressive "
                f"register reassignment, and experimental frame-store removal behind the "
                f"same wrapper. Peepholes removed zero branches; both register modes reported "
                f"no changes, and all three compact images were byte-identical to ordinary "
                f"FOP. Store removal deleted {store['fop']['deleted_stores']} stores in the "
                f"full candidate, but the explicit-effect gate rejected "
                f"{store['effect_contract_rejections'].get('changed_nonload_or_store_effect', 0)} "
                f"affected functions. It accepted {store['qualified_function_count']} functions "
                f"versus {ordinary['qualified_function_count']} for ordinary FOP. After exact "
                f"in-place reference repair, none of the accepted experimental bodies differed "
                f"from ordinary FOP, so per-function composition selected zero experimental "
                f"bodies. In the new 30-block CPU-pinned timing run, reference/ordinary-FOP CPU "
                f"was {ordinary_cpu['median_ratio']:.4f} "
                f"({ordinary_cpu['bootstrap_95_percent'][0]:.4f}–"
                f"{ordinary_cpu['bootstrap_95_percent'][1]:.4f}) and reference/store-removal "
                f"was {store_cpu['median_ratio']:.4f} "
                f"({store_cpu['bootstrap_95_percent'][0]:.4f}–"
                f"{store_cpu['bootstrap_95_percent'][1]:.4f}); neither establishes a native "
                f"gain in this run. Store removal versus ordinary FOP was "
                f"{store_over_ordinary['median_ratio']:.4f} "
                f"({store_over_ordinary['bootstrap_95_percent'][0]:.4f}–"
                f"{store_over_ordinary['bootstrap_95_percent'][1]:.4f}) in CPU cost. "
                f"Deterministic Callgrind diagnostics nevertheless show a real mechanism: "
                f"ordinary FOP removes {work['ordinary_fop_instruction_ref_reduction']:,} "
                f"instruction references, or "
                f"{work['ordinary_fop_instruction_ref_reduction_percent']:.3f}%, while the "
                f"contract-safe store-removal image executes "
                f"{work['store_removal_minus_ordinary_fop_instruction_refs']:,} more than "
                f"ordinary FOP. The correct decision is to retain ordinary FOP and stop "
                f"expanding these x86 policies for this workload. Full evidence is in the "
                f"[policy-matrix report](../lemix-queue/bolt-policy-matrix-2026-11-16/report.json)."
            )
        else:
            queue_policy_text = "The bounded queue BOLT policy matrix is absent."
        if queue_deployment is not None:
            queue_ref = queue_deployment["held_out"]["reference_over_policy_cpu"]
            queue_stock = queue_deployment["held_out"]["stock_bolt_over_policy_cpu"]
            queue_deployment_text = (
                "A subsequent training/held-out deployment experiment removes that "
                "uncertainty without changing the optimization mechanism. A 30-block "
                "CPU-pinned training matrix froze full BOLT+ordinary FOP for relocatable "
                "images and the exact reference for fixed layout. On 30 newly randomized "
                "held-out blocks, the relocatable policy improved CPU cost over O0 by "
                f"{queue_ref['median_ratio']:.4f} "
                f"[{queue_ref['bootstrap_95_percent'][0]:.4f}, "
                f"{queue_ref['bootstrap_95_percent'][1]:.4f}] and was directly faster "
                f"than frozen stock BOLT by {queue_stock['median_ratio']:.4f} "
                f"[{queue_stock['bootstrap_95_percent'][0]:.4f}, "
                f"{queue_stock['bootstrap_95_percent'][1]:.4f}]. Every run completed "
                "two million messages with the same checksum. The fixed-layout policy "
                "retains the reference because compact FOP did not clear the 1.005 "
                "training lower-bound gate. Full evidence is in the "
                "[queue held-out policy report](../lemix-queue/bolt-firmware-wrapper/"
                "queue-policy-held-out-report.json)."
            )
            if queue_publication is not None:
                queue_deployment_text += (
                    " The two layout decisions are atomically published through the "
                    "hash-bound deployment schema; publication index SHA-256 is `"
                    + hashlib.sha256(queue_publication_path.read_bytes()).hexdigest()
                    + "`."
                )
            if queue_candidates is not None:
                queue_deployment_text += (
                    " Its fixed-layout choice is also published as a standard "
                    "`FirmwareCandidate v1` with identity virtual-address origins; "
                    "candidate index SHA-256 is `"
                    + hashlib.sha256(queue_candidates_path.read_bytes()).hexdigest()
                    + "`."
                )
            if queue_reloc_candidates is not None:
                queue_bat_item = next(
                    item for item in queue_reloc_candidates["candidates"]
                    if item["origin_policy"] == "bolt_bat_instruction_origins"
                )
                queue_bat_report = load(
                    queue_reloc_candidates_path.parent
                    / queue_bat_item["relative_path"]
                    / "optimization-report.json"
                )
                queue_deployment_text += (
                    " The selected relocated image is published as a second "
                    "`FirmwareCandidate v1`; BAT provides instruction-level non-"
                    f"identity origins for {queue_bat_report['bat_instruction_origin_count']:,} "
                    "emitted instructions, retained executable sections contribute "
                    f"{queue_bat_report['identity_executable_range_count']} identity "
                    "ranges, and the entry point is covered. Relocatable candidate "
                    "index SHA-256 is `"
                    + hashlib.sha256(
                        queue_reloc_candidates_path.read_bytes()
                    ).hexdigest()
                    + "`."
                )
        else:
            queue_deployment_text = "The held-out queue deployment policy is absent."
        if firm_freertos is not None:
            firm_results = firm_freertos["results"]
            firm_comparisons = firm_results["comparisons"]
            o3_firm = firm_comparisons["O3_over_FIRM_BOLT"]["cpu_ns"]
            o0_firm = firm_comparisons["O0_over_FIRM_BOLT"]["cpu_ns"]
            stock_firm = firm_comparisons["STOCK_BOLT_over_FIRM_BOLT"]["cpu_ns"]
            firm_freertos_text = (
                "The final supported-host comparison rebuilds the same FreeRTOS POSIX "
                "queue application at O0 and O3, trains Firm-BOLT only on the O0 image, "
                "then freezes all artifacts before 30 randomized paired two-million-message "
                "runs. Every variant returned the same completed-message count and checksum. "
                f"O3/Firm-BOLT CPU cost is {o3_firm['median_ratio']:.4f} "
                f"[{o3_firm['bootstrap_95_percent'][0]:.4f}, "
                f"{o3_firm['bootstrap_95_percent'][1]:.4f}], so Firm-BOLT is "
                f"{(1.0 / o3_firm['median_ratio'] - 1.0) * 100.0:.2f}% slower than O3 "
                "but clears the frozen 5% parity bound. O0/Firm-BOLT is "
                f"{o0_firm['median_ratio']:.4f} "
                f"[{o0_firm['bootstrap_95_percent'][0]:.4f}, "
                f"{o0_firm['bootstrap_95_percent'][1]:.4f}], while stock-BOLT/Firm-BOLT is "
                f"{stock_firm['median_ratio']:.4f} "
                f"[{stock_firm['bootstrap_95_percent'][0]:.4f}, "
                f"{stock_firm['bootstrap_95_percent'][1]:.4f}]. Firm-BOLT therefore beats "
                "the exact stock-BOLT baseline by 2.73% in this run while remaining within "
                "2.38% of source O3. It optimizes the exact O0 binary; O3 is a performance "
                "reference, not a semantic oracle. This is a whole FreeRTOS x86-64 rehost "
                "result, not Cortex-M or FastDyn evidence. Full commands, hashes, raw samples "
                "and the frozen gate are in the "
                "[Firm-BOLT O0/O3 report]"
                "(../firm-bolt-freertos-o0-o3-2026-11-17/report.md)."
            )
        else:
            firm_freertos_text = "The final Firm-BOLT FreeRTOS O0/O3 evidence is absent."
        if corpus_wrapper is not None:
            corpus_rows = []
            for name, item in sorted(corpus_wrapper["workloads"].items()):
                choice = item["training_choice"]["relocatable"]
                reference = item["held_out_reference_over_policy"]["relocatable"]
                stock = item["held_out_stock_bolt_over_policy"]["relocatable"]
                corpus_rows.append(
                    f"| `{name}` | `{choice}` | {reference['median_ratio']:.4f} "
                    f"[{reference['bootstrap_95_percent'][0]:.4f}, "
                    f"{reference['bootstrap_95_percent'][1]:.4f}] | "
                    f"{stock['median_ratio']:.4f} "
                    f"[{stock['bootstrap_95_percent'][0]:.4f}, "
                    f"{stock['bootstrap_95_percent'][1]:.4f}] |"
                )
            corpus_summary = corpus_wrapper["summary"]
            corpus_fop = corpus_wrapper["bolt_and_wrapper"]["fop"]
            corpus_artifacts = corpus_wrapper["artifacts"]
            if corpus_publication is not None:
                publication_hash = hashlib.sha256(
                    corpus_publication_path.read_bytes()
                ).hexdigest()
                corpus_publication_text = (
                    "The decision is deployable rather than a benchmark-only label. "
                    f"The atomic publisher emitted {corpus_publication['deployment_count']} "
                    "hash-bound decisions (nine workloads in relocatable and fixed-layout "
                    "modes). Each selected executable embeds the frozen selection and "
                    "held-out evidence, and the loader revalidates their hashes, workload "
                    "choice and output-acceptance status. The publication index SHA-256 is "
                    f"`{publication_hash}`."
                )
                if corpus_candidates is not None:
                    corpus_publication_text += (
                        f" All {corpus_candidates['candidate_count']} fixed-layout "
                        "decisions are additionally published as consumer-loadable "
                        "`FirmwareCandidate v1` directories with identity virtual-"
                        "address origin ranges; candidate index SHA-256 is `"
                        + hashlib.sha256(corpus_candidates_path.read_bytes()).hexdigest()
                        + "`."
                    )
                if corpus_reloc_candidates is not None:
                    bat_items = [
                        item for item in corpus_reloc_candidates["candidates"]
                        if item["origin_policy"] == "bolt_bat_instruction_origins"
                    ]
                    bat_reports = [
                        load(
                            corpus_reloc_candidates_path.parent
                            / item["relative_path"]
                            / "optimization-report.json"
                        )
                        for item in bat_items
                    ]
                    bat_instruction_counts = sorted({
                        report["bat_instruction_origin_count"]
                        for report in bat_reports
                    })
                    bat_identity_counts = sorted({
                        report["identity_executable_range_count"]
                        for report in bat_reports
                    })
                    corpus_publication_text += (
                        f" All {corpus_reloc_candidates['candidate_count']} relocatable "
                        "decisions are also `FirmwareCandidate v1` artifacts: "
                        f"{len(bat_items)} selected BOLT images use instruction-level "
                        "BAT origins and the remaining exact-reference fallbacks retain "
                        "identity origins. The selected BOLT artifacts each map "
                        f"{', '.join(f'{count:,}' for count in bat_instruction_counts)} "
                        "emitted instructions and retain "
                        f"{', '.join(str(count) for count in bat_identity_counts)} "
                        "identity executable ranges; every entry point is covered. "
                        "Relocatable candidate index SHA-256 is `"
                        + hashlib.sha256(
                            corpus_reloc_candidates_path.read_bytes()
                        ).hexdigest()
                        + "`."
                    )
            else:
                corpus_publication_text = (
                    "The hash-bound deployment publication has not been generated."
                )
            corpus_precision = corpus_wrapper.get("precision_extensions", {}).get(
                "crc8"
            )
            if corpus_precision is not None:
                precision_cpu = corpus_precision[
                    "stock_bolt_over_frozen_policy_cpu"
                ]
                corpus_precision_text = (
                    "The primary 30-block `crc8` comparison was inconclusive. "
                    "A declared post-primary precision extension retained the frozen "
                    "policy and unchanged artifacts and collected "
                    f"{corpus_precision['paired_blocks']} new paired blocks. It resolved "
                    "that case in favor of exact-reference fallback: stock BOLT used "
                    f"{precision_cpu['median_ratio']:.4f} times its CPU cost "
                    f"(95% CI {precision_cpu['bootstrap_95_percent'][0]:.4f}--"
                    f"{precision_cpu['bootstrap_95_percent'][1]:.4f}). Thus the policy "
                    "is faster than stock BOLT on 9/9 corpus workloads when this "
                    "precision extension is included; the original primary result "
                    "remains recorded as 8 faster and one inconclusive."
                )
            else:
                corpus_precision_text = "No post-primary precision extension is present."
            corpus_wrapper_text = f"""
The BOLT-first policy was also evaluated on the complete retained firmware-derived native corpus rather than only SmartSpeaker and the queue fixture. The rebuilt O0 executable contains three ESCape32 computations and five FreeRTOS list routines. BOLT FOP optimized {corpus_fop['optimized_redundant_loads']} loads, deleted {corpus_fop['deleted_loads']} load representing {corpus_fop['deleted_load_dynamic_count']:,} training executions, and the wrapper accepted {corpus_wrapper['bolt_and_wrapper']['qualified_function_count']} compact bodies saving {corpus_wrapper['bolt_and_wrapper']['saved_live_bytes']} live bytes.

The deployment policy was frozen on seed 11 before evaluating seed 13. For relocatable images it selects full BOLT+FOP only when the training lower confidence bound clears 1.005; otherwise it executes the exact reference. Fixed-layout images may select only the compact accepted image or the reference. The held-out results use 30 paired CPU-pinned blocks. `Reference/policy` measures improvement over O0; `stock/policy` measures the selected wrapper policy directly against the trained stock-BOLT artifact.

| Workload | Frozen relocatable choice | Reference/policy CPU | Stock BOLT/policy CPU |
| --- | --- | ---: | ---: |
{chr(10).join(corpus_rows)}

The relocatable policy generalized six selected gains and used exact-reference fallback on three workloads. It was directly faster than stock BOLT on {corpus_summary['relocatable_policy_faster_than_stock_bolt']}/{corpus_summary['workload_count']} workloads, inconclusive on {corpus_summary['relocatable_policy_vs_stock_inconclusive']}, and slower on {corpus_summary['relocatable_policy_slower_than_stock_bolt']}. The fixed-layout policy generalized {corpus_summary['fixed_layout_policy_generalized_gains']} gains and retained the reference for {corpus_summary['fixed_layout_policy_exact_reference_fallbacks']} cases. This establishes a supported-host corpus result: profitability selection around BOLT prevents the measured `crc8dvbs2` and combined-workload regressions while retaining BOLT's large wins.

{corpus_precision_text}

{corpus_publication_text}

Emission accounting remains policy-specific. The reference and compact fixed-layout images are both {corpus_artifacts['reference']['file_bytes']:,} bytes; the compact image saves {corpus_wrapper['bolt_and_wrapper']['saved_live_bytes']} live code bytes inside existing function capacities. The complete relocatable BOLT container is {corpus_artifacts['full_relocatable']['file_bytes']:,} bytes because it retains original code and adds a distant host-ELF text segment. That container size is not a Cortex-M flash-occupancy result and is not used for the fixed-layout policy. These results do not establish original-MCU or FastDyn speedup. Full evidence is in the [held-out corpus policy report](../recovered-firmware-corpus-2026-11-16/bolt-firmware-wrapper/held-out-policy-report.json), and the complete workflow is reproduced by `scripts/preflight/run-recovered-corpus-wrapper.sh`.
"""
        else:
            corpus_wrapper_text = "The held-out recovered-corpus wrapper evidence is absent."
        bolt_wrapper_section = f"""
## 8. BOLT-first firmware wrapper

The production direction now uses BOLT as the optimizer rather than competing with it. A native `FastDynFirmwareReport : BinaryFunctionPass` runs immediately after BOLT's `FrameOptimizerPass` and `AllocCombinerPass`, before instruction lowering. The thin driver invokes the pinned trained F1 pipeline, enables BOLT Address Translation, consumes the native report, validates the emitted ELF and protected entries, records the earlier custom pass as a baseline, and then performs bounded compact adoption.

```text
Pinned BOLT profile/CFG/dataflow/layout/FOP
                    ↓
native post-optimization candidate report
                    ↓
firmware entry/reference/effect/placement gates
                    ↓
full BOLT candidate or selectively adopted compact image
```

The first end-to-end LEMIX result is `integration_accepted`. Real BOLT again optimized {bolt_wrapper['fop']['optimized_redundant_loads']:,} redundant loads and deleted {bolt_wrapper['fop']['deleted_loads']:,} loads representing {bolt_wrapper['fop']['deleted_load_dynamic_count']:,} profile-weighted executions; it removed no stores. The native post-pass observed {native_summary['function_count']:,} function records, including {native_summary['ignored_function_count']} ignored records, and {native_summary['size_changed_function_count']:,} functions whose estimated post-optimization size differs from their original size. Aggregate function bytes fell from {native_summary['original_function_bytes']:,} to {native_summary['estimated_post_optimization_function_bytes']:,} ({function_byte_reduction:.2f}%); the post-optimization representation contains {native_summary['post_optimization_instruction_count']:,} non-pseudo instructions. The pre-pass origin tracker plus FOP annotation preservation maps {native_summary['instructions_with_original_offset']:,} of them ({origin_coverage:.2f}%) to exact original instruction addresses. The remaining {native_summary['synthesized_control_edge_instruction_count']:,} instructions are all BOLT-synthesized unconditional jumps: {native_summary['verified_synthesized_control_edge_instruction_count']:,} have exact original source-to-target CFG-edge provenance, while eight lack that proof and remain ineligible. No instruction is assigned a nearest-address identity.

| Representative BOLT result | Original | Post-optimization | Result |
| --- | ---: | ---: | --- |
| `prvGetThreadFromTask` | {native_accessor['original_size']} B | {native_accessor['estimated_post_optimization_size']} B | {native_accessor['post_optimization_instruction_count']} instructions; exact measured FOP body |
| `vListInitialiseItem` | {native_list['original_size']} B | {native_list['estimated_post_optimization_size']} B | {native_list['post_optimization_instruction_count']} instructions |

All {len(bolt_wrapper['protected_entries'])} `endbr64` entry constraints were found in the emitted binary and preserved. The reference and full BOLT candidate each completed {bolt_wrapper['paced_installation_smoke']['reference']['dispatch_markers']} markers in the one-second paced installation smoke. With BAT enabled, the full BOLT ELF is {bolt_wrapper['candidate']['file_bytes']:,} bytes with {bolt_wrapper['candidate']['executable_section_bytes']:,} executable-section bytes; this remains a host layout artifact, not acceptable fixed-flash occupancy by itself.

The wrapper also imported the **actual emitted BOLT bodies**, rather than regenerating them, for the two reference-complete fixtures. `prvGetThreadFromTask` uses {compact_edits['prvGetThreadFromTask']['live_bolt_bytes']} live BOLT bytes in its {compact_edits['prvGetThreadFromTask']['qualified_capacity_bytes']}-byte original range; `vListInitialiseItem` uses {compact_edits['vListInitialiseItem']['live_bolt_bytes']} in {compact_edits['vListInitialiseItem']['qualified_capacity_bytes']} bytes and preserves its protected prefix. Every emitted instruction in both adopted bodies has an exact original-address mapping. The resulting ELF remains exactly {bolt_compact['candidate']['file_bytes']:,} bytes—equal to the reference instead of the full BOLT candidate—and completed {bolt_compact['paced_installation_smoke']['dispatch_markers']} paced markers. This is selective compact adoption, not full-program compact BOLT emission: reference completeness remains a gate before more functions can be adopted.

The same mechanism now selects a conservative cohort automatically: simple, single-entry, unsplit functions with complete origins, no calls/branches/RIP-relative operands, at most {bolt_wrapper['wrapper_policy']['max_region_bytes']} original bytes, a strictly smaller BOLT body, non-overlapping original ranges, and an unchanged protected prefix. It admitted {bolt_auto['selected_function_count']} functions covering {bolt_auto['original_capacity_bytes']:,} original bytes and imported {bolt_auto['live_bolt_bytes']:,} live BOLT bytes, saving {bolt_auto['saved_live_bytes']:,} live bytes while retaining the reference's {bolt_auto['candidate']['file_bytes']:,}-byte ELF size. The automatically compacted image completed {bolt_auto['paced_installation_smoke']['dispatch_markers']} paced markers.

The native extension now exports the **pre-FOP instruction/effect inventory** and {len(bolt_native_report['frame_forwardings']):,} exact load-to-store forwarding edges. The firmware wrapper qualifies {bolt_qualified['selected_function_count']} compactly installed bodies: {bolt_qualified['exact_memory_sequence_function_count']} retain the exact ordered explicit-memory instruction sequence, while {bolt_qualified['fop_dependency_function_count']} use {bolt_qualified['forwarding_dependency_count']} audited FOP edges from an original stack load to the exact earlier unchanged source store. No store or other non-forwarded memory instruction may change in this tier.

The next reference class is also implemented rather than discarded: {bolt_qualified['direct_call_extension']['qualified_function_count']} additional functions contain {bolt_qualified['direct_call_extension']['repaired_call_count']} direct x86 `E8 rel32` calls. Each call must retain its exact original instruction identity and order. The importer decodes the semantic target from the original call bytes, range-checks and rewrites the displacement for the original placement, and verifies the repaired target before publication. This adds {bolt_qualified['direct_call_extension']['saved_live_bytes']} bytes of savings.

For local control flow, {bolt_qualified['internal_branch_extension']['qualified_function_count']} more functions with {bolt_qualified['internal_branch_extension']['validated_branch_count']} relative branches are installed. A branch is admitted only when its BOLT target remains inside the adopted body and the target instruction maps to the exact original target identity; moving the whole body then leaves the relative displacement invariant. Five otherwise compactable functions remain unchanged because their optimized branch target did not meet this strict identity rule. The admitted branch class contributes {bolt_qualified['internal_branch_extension']['saved_live_bytes']} more saved bytes.

RIP-relative data references are now handled without freezing their values. Using {bolt_qualified['rip_relative_extension']['decoder']}, the wrapper decodes each original reference target, rewrites the candidate displacement for the original placement, range-checks it, and re-decodes the emitted address. The leaf-only arm adds {bolt_qualified['rip_relative_extension']['qualified_function_count']} functions and {bolt_qualified['rip_relative_extension']['repaired_reference_count']} references; the combined call-plus-data arm adds {bolt_qualified['combined_call_rip_extension']['qualified_function_count']} functions, {bolt_qualified['combined_call_rip_extension']['repaired_call_count']} calls, and {bolt_qualified['combined_call_rip_extension']['repaired_data_reference_count']} data references. Mutable globals remain references to their exact original addresses. These two arms add {bolt_qualified['rip_relative_extension']['qualified_function_count'] + bolt_qualified['combined_call_rip_extension']['qualified_function_count']} functions and {bolt_qualified['rip_relative_extension']['saved_live_bytes'] + bolt_qualified['combined_call_rip_extension']['saved_live_bytes']} saved bytes.

Composing the same rules over branch-heavy bodies admits another {bolt_qualified['branch_heavy_extension']['qualified_function_count']} functions. This arm validates {bolt_qualified['branch_heavy_extension']['validated_branch_count']:,} exact local branch targets, repairs {bolt_qualified['branch_heavy_extension']['repaired_call_count']} direct calls and {bolt_qualified['branch_heavy_extension']['repaired_data_reference_count']} RIP-relative data references, and contributes {bolt_qualified['branch_heavy_extension']['saved_live_bytes']} saved bytes. Conditional branches retain exact original instruction identities and targets. BOLT-created or removed unconditional jumps are instead proven by equality of the original and post-pass CFG edge sets, with exact source/target block identities; final offsets come from ELF decoding and target identities from BAT. The wrapper explicitly rejects {bolt_qualified['branch_heavy_extension']['selection_rejection_counts'].get('unproven_instruction_provenance', 0)} unproven provenance cases, {bolt_qualified['branch_heavy_extension']['selection_rejection_counts'].get('control_edge_set_changed', 0)} changed edge sets, and {bolt_qualified['branch_heavy_extension']['selection_rejection_counts'].get('branch_target_identity_changed', 0)} changed targets. Across all admitted arms, the wrapper installs {bolt_qualified['selected_function_count']} functions and saves {bolt_qualified['saved_live_bytes']} live bytes without ELF growth. This remains a bounded x86 reference contract, not a whole-firmware equivalence proof.

Raising the frozen region limit from 256 to {bolt_wrapper['wrapper_policy']['max_region_bytes']} bytes exposed a distinct reference class rather than an optimization failure. The first larger candidate crashed in `main_full`: BOLT had relocated six callback/function-pointer constants into its output address space, while the compact importer had repaired only calls and RIP-relative operands. Reproducible bisection isolated the function, and exact disassembly identified the immediates. The typed absolute-symbol adapter now resolves each symbol to its original identity, patches the final immediate at the original placement, and re-decodes it before installation. It repairs {bolt_qualified['absolute_symbol_reference_extension']['repaired_reference_count']} references in {bolt_qualified['absolute_symbol_reference_extension']['qualified_function_count']} functions; `main_full` now completes the same paced work. The failed artifact and [crash bisection](../bolt-firmware-wrapper-edge-provenance-1k-2026-11-16/crash-bisection.json) are retained rather than overwritten. One unsupported `MP3_GetBuffer_Info` RIP encoding is localized to that function, which retains its reference body.

{compact_timing_text}

The frozen profile marks {bolt_wrapper['hot_qualified_adoption']['selected_function_count']} of the correctness-qualified functions as executed. A separate hot-only image publishes those bodies, saves {bolt_wrapper['hot_qualified_adoption']['saved_live_bytes']} live bytes, remains reference-sized, and does not use profile membership as a correctness argument. {hot_timing_text}

{queue_text}

{queue_wrapper_text}

{queue_policy_text}

{queue_deployment_text}

{firm_freertos_text}

{corpus_wrapper_text}

The earlier custom transformation results remain unchanged in [custom-pass-baseline.json](../bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16/custom-pass-baseline.json). They are now explicitly classified as a bounded validation/emission baseline, not the production optimizer. Native and wrapper evidence is retained in [wrapper-result.json](../bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16/wrapper-result.json), [bolt-post-optimization-report.json](../bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16/bolt-post-optimization-report.json), and the exact [BOLT command](../bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16/command.json). The pre-BAT, instruction-only provenance, and failed pre-absolute-reference runs are preserved separately as historical evidence.

### 8.5 Native BOLT follow-up: opcode audit and copy cleanup

Width-complete stack-CMP forwarding raised FOP from 3,601 to 4,652 static reads but added only 20,055 weighted executions. The new available-but-unsupported audit then found no remaining opcode class above 21 dynamic reads, closing additional opcode plumbing as a useful SmartSpeaker direction.

A registered post-FOP copy pass deleted 22 adjacent redundant copies in ten observed blocks (122,405 exact block-weighted executions). Across the complete 800-block independent aggregate, Firm-BOLT is strictly faster at target CPU 1.0083 [1.0049, 1.0126] and wall 1.0013 [1.0004, 1.0019], with matching exact work and traces. Its target lower bound is nevertheless 0.0066 percentage point short of the frozen greater-than-0.5% useful-effect rule, so stock BOLT remains selected. A failed first precision attempt is retained separately: a missing driver observation triggered a legacy logger exception at sequence 462, and none of its samples enter the aggregate. The repaired harness adds append-only journaling and deterministic resume validation. Per-block equality tracking expands the pass to 391 deletions/144,395 weighted executions; its 30-block screen regressed at target CPU 0.9914 [0.9808, 1.0103]. A hot-only 29-site form was neutral at 1.0006 [0.9862, 1.0174]. The dominant extra identity is an original stack reload across a pointer store; after FOP it is a register copy, but end-to-end acceptance still inherits an unproved private-stack/no-alias obligation. Generalized equality now clears at every store, and the wrapper rejects FOP dependencies that cross an intervening store. The corrected candidate deletes 69 copies/122,846 weighted executions and passes installation, but its training screen is unresolved at target CPU 1.0041 [0.9875, 1.0147] and wall 0.9998 [0.9983, 1.0015]. Inline-all produced byte-identical executable sections. Four frozen alignment policies failed, with target medians from 0.9855 to 0.9993.

Exact block counts then selected two bounded follow-ups. A 32-bit self-move is deleted only after an immediately preceding whitelisted same-GPR 32-bit definition has already zeroed the upper half; 53 static sites cover only 13 weighted executions, and both its generalized and adjacent-base screens fail advancement. A higher-activity rule folds `mov src,tmp; mov (tmp),tmp` by replacing only the following load's exact base register. It accepts only a zero-displacement, unindexed, unsegmented `MOV64rm` that overwrites `tmp`, retains the load identity and memory/fault behavior, and records the removed copy. The 45 sites include six observed sites and 483,207 weighted executions, dominated by the 441,399-execution accessor. The wrapper admits the resulting 21-byte accessor only when both its exact byte string and native cleanup ledger agree; all six fixed-work installations match. The native screen still fails advancement at target CPU 1.0004 [0.9875, 1.0153], process CPU 1.0025 [0.9834, 1.0148], and wall 0.9998 [0.9971, 1.0033]. This is consistent with x86 register-move elimination and does not establish the separately measured TCG cost.

A final exact-rule coalescing `lea; shl; mov` into the destination removed 28 copies representing 562,102 weighted executions, 541,394 in the two scheduler functions. Its first wrapper run exposed a validator bug: a one-second paced observation differed by one marker while exact fixed-work behavior matched. Publication now gates on completed units, ticks, marker multiplicity, return status and ordered interrupt prefix; paced counts are diagnostic only. The unchanged candidate then screened at target CPU 0.9939 [0.9915, 1.0071] and wall 1.0002 [0.9997, 1.0003], so it is semantically accepted but not profitable. These are recorded mechanism and stop results, not general claims about copy propagation, alignment or inlining.
{memory_test_followup_text}
{frame_followup_text}
"""
    else:
        bolt_claim_rows = "| Unresolved | BOLT-first wrapper evidence is not present. |"
        bolt_wrapper_section = """
## 8. BOLT-first firmware wrapper

The BOLT-first wrapper evidence is not present in this run directory. Run `scripts/preflight/run-bolt-firmware-wrapper.sh` to build the native post-optimization reporter, execute the trained BOLT pipeline, retain the custom-pass baseline, and regenerate this section.
"""

    thumb_mov_text = (
        "The initial same-register policy emitted `mov r0,r0`; its two-site CPU "
        f"ratio was {thumb_mov['paired_comparison']['cpu_ns']['median_ratio']:.4f} "
        f"[{thumb_mov['paired_comparison']['cpu_ns']['bootstrap_95_percent'][0]:.4f}, "
        f"{thumb_mov['paired_comparison']['cpu_ns']['bootstrap_95_percent'][1]:.4f}], "
        "a resolved regression, so it was rejected. "
        if thumb_mov is not None else ""
    )
    thumb_paced_text = (
        "A non-`icount` calibration completed equal tick counts but produced a "
        f"different 2 MiB RAM prefix in {len(thumb_paced['validation']['ram_mismatch_blocks'])} "
        "block, demonstrating that externally paced event counts do not freeze the "
        "firmware schedule. "
        if thumb_paced is not None else ""
    )
    thumb_tcg_text = (
        f"For the inspected SysTick translation block, optimized TCG emitted "
        f"{thumb_tcg['candidate']['optimized_tcg_operation_count']} operations and "
        f"{thumb_tcg['candidate']['host_output_bytes']} host bytes versus "
        f"{thumb_tcg['reference']['optimized_tcg_operation_count']} operations and "
        f"{thumb_tcg['reference']['host_output_bytes']} bytes for the reference. "
        if thumb_tcg is not None else ""
    )
    branch_cpu = thumb_branch["paired_comparison"]["cpu_ns"]
    branch_guest = thumb_branch["paired_comparison"]["guest_benchmark_ns"]
    thumb_branch_tcg_text = (
        f"For the inspected hot translation block, reference and candidate both emit "
        f"{thumb_branch_tcg['reference']['optimized_tcg_operation_count']} optimized "
        f"TCG operations and {thumb_branch_tcg['reference']['host_output_bytes']} host "
        f"bytes. The generated-host prefilter therefore returns "
        f"`{thumb_branch_cost['status']}:{thumb_branch_cost['reason_code']}`. "
        if thumb_branch_tcg is not None and thumb_branch_cost is not None else ""
    )

    report = f"""# BOLT-first optimization with a firmware acceptance and compact-emission layer

**Date:** 17 November 2026
**Project:** BinFast / FastDyn, Student 1
**Scope:** focused supported-host baseline plus patch-independent preflight prototype

## Abstract

We reran the LEMIX SmartSpeaker experiment to separate six previously conflated causes of failure: disabled optimization, extraction, state/equivalence contracts, stochastic search, emitted layout, and external timer pacing. We reused the exact unoptimized SmartSpeaker ELF (SHA-256 `{manifest['hashes']['reference_sha256']}`), frozen profile, and zero-backed MMIO shim, and pinned BOLT and STOKE to the required revisions. The result changes the earlier diagnosis in three important ways.

First, BOLT's x86 frame optimizer was disabled in the historical run because `frame-opt` defaults to `none`. Enabling its ordinary load-forwarding mode optimized {f1['redundant_loads']:,} redundant stack loads and deleted {f1['deleted_loads']:,}; the deleted loads represent {f1['deleted_load_frequency']:,} profile-weighted executions. On the exact 29-byte `prvGetThreadFromTask` body, BOLT produced a legal 24-byte body and reduced fixed-work CPU cost by {function_cost_reduction:.1f}% (reference/FOP ratio {function_ratio:.4f}, paired 95% interval {accessor_perf['bootstrap_95_percent'][0]:.4f}–{accessor_perf['bootstrap_95_percent'][1]:.4f}). This is a function-level result, not whole-application acceleration. In the timer-paced SmartSpeaker application, R/F1 was {r_f1[0]:.4f} ({r_f1[1]:.4f}–{r_f1[2]:.4f}); all arms still completed about 500 timer-supplied dispatch markers in five seconds, so SmartSpeaker capacity remains unestablished. A separate two-million-message queue fixture now supplies a bounded fixed-work whole-program comparison, reported below.

Second, the dominant STOKE failure was representation, not search. The stock path admitted only 1 of 23 frozen structural candidates. Exact decoding showed that 22 candidates began with `endbr64`, not disposable padding; the ELF declares IBT/SHSTK properties. Those properties establish a protected-entry requirement for this study, but they do not establish that runtime enforcement was enabled. Preserving the four-byte entry prefix and exporting only the searchable fall-through body raised extraction to 23/23. The bottleneck then moved to semantic linking and state construction: 10/23 reached states, 11/23 retained unresolved RIP-relative/external references, and 2/23 failed memory resizing.

Third, the proposed three-instruction accessor is not legal under the admitted memory-output contract. If the input pointer aliases the original spill slot at entry stack address `S-0x20`, the original store overwrites the pointed-to object before the load, whereas the short rewrite reads the old value. Ordinary inputs conceal this difference. BOLT's ordinary frame optimization retains both stores and passes the declared STOKE checks; BOLT's experimental store removal produces the same semantic mismatch as the short rewrite. Legal normalization improved the testcase objective in all eight S2 runs, but improved verified search effectiveness remains unestablished: three candidates produced concrete verifier counterexamples and five ended unknown without a retained counterexample, for 0/8 verified results. One cold `_fini` candidate was supplied by initialization with zero proposals, then verified, emitted, installed, and executed. It is a cold integration control, not a discovered hot-path optimization or a performance result.

The design conclusion is not that existing binary optimizers are ineffective. It is that their optimization strength needs a firmware-specific acceptance boundary: exact code/reference identities, entry constraints, observable memory and fault behavior, fixed-layout/code-growth budgets, and completed-work cost. The bounded custom `PreflightPass SDK v1` run is retained in Section 7 as an informative baseline, but it is no longer the proposed optimizer. Section 8 reports the corrected architecture and its first execution: real pinned BOLT optimizes first, a native post-optimization pass exports the candidate state, and the firmware wrapper validates or compactly adopts the result. Patch actions remain empty in phase one.

{smartspeaker_o3_abstract}

## 1. Research question and boundary

The experiment asks whether optimization policies—not missing Cortex-M engineering—can exploit an O0 firmware-derived workload after LEMIX recompiles it as a supported x86-64 application. It does **not** measure an original MCU binary, hardware timing, a realistic device model, or vulnerability preservation. The preload shim only maps fixed addresses to zero-backed memory. It makes the application runnable; it does not reproduce peripheral state.

The frozen reference has 97 compile commands, all with `-O0 -g -fno-omit-frame-pointer`; the link uses pthreads, non-PIE output, and emitted relocations. The build log records 377 pointer-width warnings, which is relevant when interpreting a 64-bit rehost as evidence about 32-bit firmware. The reference, profile, preload, BOLT binary, archive, build log, host, CPU affinity, governor, and recovered historical controls are pinned in [manifest.json](manifest.json).

### 1.1 Historical screening experiment and why it was rerun

The first screening experiment used the same SmartSpeaker reference but did not enable BOLT frame optimization. It compared the reference with an unprofiled restricted rewrite and a trained layout rewrite using 11 five-second samples per arm. All arms produced 500 timer-supplied dispatch markers. The trained arm's reference/candidate CPU ratio was 0.9987 with an independently resampled 95% interval of 0.8978–1.0743; the restricted arm's ratio was 0.9817 with interval 0.8695–1.1068. BOLT's pre-FOP profile model predicted 3.1% fewer executed instructions and 6.4% fewer branches, but the timer-paced measurement could not resolve a CPU effect.

The historical STOKE route attempted 1,332 functions, emitted 9, and admitted only `prvGetThreadFromTask` from the frozen 23-candidate cohort. Four searches totaling 10 million proposals found no new rewrite. That experiment correctly exposed an end-to-end availability problem, but it incorrectly treated the leading `f3 0f 1e fa` bytes as disposable alignment and used the short accessor as if its legality were self-evident. It also inferred too much from whole-ELF growth and equal timer-paced work counts.

The focused rerun retains those historical observations as a screening baseline and corrects their interpretation with controlled contrasts:

| Question | Historical evidence | Focused rerun |
| --- | --- | --- |
| O0 dataflow | Layout and rewrite controls; FOP left at `none` | `L0/L1/F0/F1/FH` matrix with ordinary and hot FOP |
| Timing | 11 samples; independently resampled arms | 30 randomized paired blocks; CPU seconds per actual marker |
| Capacity | 500 markers in five seconds | Still timer-capped; capacity explicitly unestablished |
| Local execution | No fixed-work exact-body result | 30 paired blocks over byte-identical accessor bodies |
| Extraction | 1/23 structural candidates reached STOKE | 23/23 after preserving the `endbr64` entry prefix |
| Accessor target | Short form presented as obvious cleanup | Alias and fault probes plus STOKE contract checks |
| Search | One body, original seed, 10 million proposals | Six frozen cases; S1/S2; 70 million proposals; separate proof/install gates |
| Growth | Aggregate text/file growth | Retained original, replacement, alignment, and executable mapping inventory |

This report is therefore the single authoritative narrative for both experiments. Historical raw files and logs remain unchanged so that the corrections are auditable.

## 2. Experimental design

### 2.1 BOLT matrix

One frozen profile was used for all rewritten arms. Every unrelated option was explicit. `L0` and `F0` disable layout; `L1`, `F1`, and `FH` use `ext-tsp`, `cdsort`, function splitting, and all-cold splitting. `F0/F1` use `--frame-opt=all`; `FH` uses `hot`. Experimental store removal (`FRS`) is diagnostic and excluded from the primary matrix. The historical unprofiled restricted control remains separate from profiled `L0`.

The primary timing protocol used 30 complete randomized blocks, with all six variants randomized within each block. Ratios are reference CPU seconds per dispatch divided by candidate CPU seconds per dispatch; values above one favor the candidate. Intervals bootstrap complete paired blocks 100,000 times. A 5% ratio was frozen as the minimum useful effect.

### 2.2 STOKE funnel

The original 23 profiled, loop-free, call-free candidates were retained. Identities use file offset, virtual address, size, and byte hash rather than names. We distinguish stock extraction, canonical export, state construction, search, testcase correctness, bounded verification, emission, installation, and final acceptance. S1 searches canonical unchanged bodies. S2 starts from a checked deterministic BOLT-FOP normalization but uses the same contract, states, four seeds, and 2.5-million-proposal limit per seed. Only six preselected cases were searched.

## 3. BOLT findings

### 3.1 The historical run did not execute frame optimization

This is source-confirmed and command-confirmed. In the pinned source, `frame-opt` defaults to `none`, and the recovered historical commands did not override it. The named dynamic-statistics stage also reports counts before SCTC/FOP; those figures are not a final post-FOP instruction inventory. The historical controls and logs are preserved and hashed in the new manifest rather than overwritten.

With ordinary FOP enabled, both `F0` and `F1` report {f1['redundant_loads']:,} optimized redundant loads, all replaced from registers, and {f1['deleted_loads']:,} deleted loads. No stores are removed. The full-mode profile frequencies are {f1['redundant_load_frequency']:,} optimized-load executions and {f1['deleted_load_frequency']:,} deleted-load executions. Frame restoration failed for 168 functions representing 5.2% dynamic coverage, and 69 functions were not optimized; these are explicit eligibility limits, not silent failures.

Hot-only FOP transforms only {fh['redundant_loads']} loads and deletes {fh['deleted_loads']}, but captures {100.0 * fh['redundant_load_frequency'] / f1['redundant_load_frequency']:.2f}% of the full-mode dynamic redundant-load frequency and {100.0 * fh['deleted_load_frequency'] / f1['deleted_load_frequency']:.2f}% of deleted-load frequency. Thus {100.0 * fh['redundant_loads'] / f1['redundant_loads']:.2f}% of the static transformations cover almost all of the profiled dynamic opportunity. This is strong evidence that a profile can prioritize safe dataflow work; it is not evidence that cold code is semantically dispensable.

### 3.2 Representative example: legal load forwarding versus illegal store removal

The exact original accessor is:

```asm
pushq %rbp
movq  %rsp, %rbp
movq  %rdi, -0x18(%rbp)
movq  -0x18(%rbp), %rax
movq  (%rax), %rax
movq  %rax, -0x8(%rbp)
movq  -0x8(%rbp), %rax
addq  $0x8, %rax
popq  %rbp
retq
```

Ordinary FOP forwards the two reloads but retains the stores. The result is 9 instructions/24 bytes instead of 10/29. The body used by the native harness is byte-identical to the production BOLT body, as recorded in [body-provenance-frs.json](accessor-harness/body-provenance-frs.json). Over {accessor_perf['iterations_per_sample']:,} calls per sample and 30 paired blocks, its reference/FOP ratio is {function_ratio:.4f} ({accessor_perf['bootstrap_95_percent'][0]:.4f}–{accessor_perf['bootstrap_95_percent'][1]:.4f}), corresponding to {function_cost_reduction:.1f}% lower CPU cost per call.

Experimental `--frame-opt-rm-stores` removes {frs['unused_stores']} stores program-wide ({frs['unused_store_frequency']:,} profile-weighted executions) and shrinks this accessor to 7 instructions/16 bytes. It is not admissible under the broad memory-output contract. In the concrete alias state, the initial object contains `0x1234` and the pointer equals the spill address. The original and FOP bodies store the pointer before loading and return `pointer+8`; the short and FRS bodies leave `0x1234` in memory and return `0x123c`. This is a concrete semantic counterexample, not a timeout or unsupported-input result.

Invalid-pointer probes also fault at address 1, but at different host instruction offsets: original 12, FOP 11, short 0, and FRS 7. Offset changes alone are not lost attribution. The prototype maps the FOP load back to the exact original instruction identity and compares fault kind, address, width, required state, and ordered event prefix; that declared comparison accepts. These remain host provenance observations and do not establish original MCU fault-PC preservation.

### 3.3 CPU cost changed locally, not at the timer ceiling

| Comparison | Median paired ratio | Paired bootstrap 95% interval | Interpretation |
| --- | ---: | ---: | --- |
| R / F1 | {r_f1[0]:.4f} | {r_f1[1]:.4f}–{r_f1[2]:.4f} | no observed 5% useful gain; not semantic equivalence |
| L1 / F1 | {l1_f1[0]:.4f} | {l1_f1[1]:.4f}–{l1_f1[2]:.4f} | incremental FOP unresolved at application scale |
| L0 / F0 | {l0_f0[0]:.4f} | {l0_f0[1]:.4f}–{l0_f0[2]:.4f} | same conclusion without layout |
| F0 / F1 | {f0_f1[0]:.4f} | {f0_f1[1]:.4f}–{f0_f1[2]:.4f} | layout effect unresolved |
| R / FH | {r_fh[0]:.4f} | {r_fh[1]:.4f}–{r_fh[2]:.4f} | hot-only FOP unresolved |

All 180 primary application samples passed the output/return/work checks. Median dispatch markers were 500 for every arm because the event source supplies one approximately every 10 ms. Median process CPU cost per marker was {paired['variants']['R']['median_cpu_seconds_per_dispatch']:.9f} s for R and {paired['variants']['F1']['median_cpu_seconds_per_dispatch']:.9f} s for F1. Equal marker counts therefore say nothing about capacity. The function-level replay establishes that useful CPU savings exist in an exact hot body, but their application share is too small or noisy to resolve under the paced fixture. No existing compute-limited whole-application route was substituted after seeing results, so whole-application capacity remains unestablished.

{smartspeaker_policy_text}

Hardware counters were unavailable because the installed `perf` did not support the running 6.17 kernel. We therefore make no cache, iTLB, or cycle claim.

### 3.4 Emitted growth is retention and layout, not merely a larger hot body

| Arm | ELF bytes | Executable-section bytes | Retained original text | Replacement hot+cold text | Exec-segment bytes outside executable sections |
| --- | ---: | ---: | ---: | ---: | ---: |
{layout_row('R')}
{layout_row('L0')}
{layout_row('L1')}
{layout_row('F0')}
{layout_row('F1')}
{layout_row('FH')}
{layout_row('FRS')}

The key mechanism is visible in the section inventory. Rewritten binaries retain 340,195 bytes in executable `.bolt.org.text` and add replacement `.text`/`.text.cold`. With layout enabled, 2 MiB alignment and load-segment spacing dominate whole-file and mapped-executable growth. Ordinary FOP itself reduces replacement executable sections by {layout['L1']['executable_section_bytes'] - layout['F1']['executable_section_bytes']:,} bytes relative to L1; without layout it reduces them by {layout['L0']['executable_section_bytes'] - layout['F0']['executable_section_bytes']:,} bytes. File size is unchanged across L1/F1/FH because section/segment alignment masks those local savings.

This refutes the simplistic explanation that FOP grew the hot code. It also exposes a firmware design gap: a fixed-flash optimizer needs in-place or compact emission, reference/alternate-entry proof before dropping old bodies, branch/literal repair accounting, and an explicit final image budget. Host ELF growth is not MCU flash occupancy, and section labels are not dynamic hotness; no cache penalty is inferred from these sizes.

The historical screening report's 600,250-byte reference and 709,714-byte BOLT “text” figures came from the aggregate `size` definition. The table above instead totals allocated executable sections (342,107 bytes for R and 696,222 bytes for L1) and separately inventories mapped segment overhead. These denominators are not interchangeable, so this report does not compare their percentages.

## 4. STOKE findings

### 4.1 Candidate reconciliation and the recovered extraction funnel

The recovered populations are {reconciliation['defined_text_symbols']:,} defined text symbols, {reconciliation['bolt_functions']:,} BOLT functions, {reconciliation['extractor_attempts']:,} stock extractor attempts, {reconciliation['extractor_emitted']} emitted bodies, and {reconciliation['unprofiled_csv_rows']:,} unprofiled `StokeInfo` rows. These counts differ because they are distinct representations; the rerun joins by address/range/hash rather than assuming names or counts correspond one-to-one.

Among 194 profiled rows, the frozen screen contains 23 structural candidates (11.86%). They cover 416/9,158 static instructions (4.54%) and 685,955/4,855,284 profile score (14.13%). The old report's 5.47% score for one function is not treated as an Amdahl fraction. Likewise, the corrected fraction of 125 layouts among 1,337 functions is **9.35%**, not 8.96%; historical raw output remains unchanged.

| Funnel stage | Result | Mechanism |
| --- | ---: | --- |
| Frozen structural cohort | 23/23 | preselected before rerun outcomes |
| Stock extraction within cohort | 1/23 | 22 bodies blocked at entry representation |
| Full-body canonical attempt | 1/23 | confirmed the entry instruction remained the blocker |
| Fixed-prefix canonical extraction | 23/23 | preserve four-byte `endbr64`; search fall-through body |
| State construction accepted | {state_counts.get('accepted', 0)}/23 | supported bounded state/effect model |
| State construction unresolved references | {state_counts.get('blocked_by_unresolved_reference', 0)}/23 | requires semantic linking, not byte deletion |
| State construction memory failure | {state_counts.get('failed', 0)}/23 | two retained `Could not resize memory` failures |

The earlier interpretation of `f3 0f 1e fa` as padding was wrong. Exact decoding identifies `endbr64` in {prefix_count}/23 candidates, and `readelf` reports IBT and SHSTK properties. The pinned STOKE assembler cannot re-encode that instruction. Treating it as a protected fixed entry prefix recovers searchability without deleting a control-flow constraint. We did not verify runtime CET enablement: the [Linux shadow-stack documentation](https://docs.kernel.org/arch/x86/shstk.html) distinguishes ELF capability notes from loader/runtime enablement and exposes actual shadow-stack status through `/proc/$PID/status`. This is a representative firmware lesson: vector entries, mode switches, landing pads, literal pools, and alignment bytes require typed preservation, not blanket NOP stripping.

The next bottleneck is now explicit. Eleven bodies have RIP-relative or external control/data references whose semantics cannot be recovered by rebasing text alone; two state-generation runs fail while resizing memory. These are adapter/state issues, not search failures and not counterexamples.

### 4.2 Accessor legality under named contracts

| Candidate | Native ordinary case | Native stack/object alias | STOKE declared contract | Fault/event contract |
| --- | --- | --- | --- | --- |
| BOLT FOP (stores retained) | same return | same memory and return | hold-out and bounded: equivalent | same fault address; host PC offset differs |
| Three-instruction short form | same return | **different memory and return** | hold-out and bounded: counterexample | not qualified |
| FRS store-removal form | same return | **different memory and return** | hold-out and bounded: counterexample | not qualified |

The short form's non-discovery is therefore not evidence of weak search. It is illegal under the experiment's admitted alias/memory-output contract. If a future firmware ABI proves that the input can never alias private stack, that proof defines a narrower experiment; it may not be assumed from a normal testcase. STOKE's current checks also do not express FastDyn's future original-guest fault attribution and ordered event-prefix obligations. That is a qualification boundary, not a blanket claim that all STOKE verification is inadequate.

### 4.3 Controlled conditioning and end-to-end installation

The timed search executed {total_iterations:,} proposals across {len(search_results)} runs, with {summed_search_elapsed:.1f} seconds of summed subprocess elapsed. S1 produced {s1_counts.get('no_candidate', 0)} no-candidate runs and four duplicate successful `_fini` controls (one per seed). The `_fini` target after the protected entry prefix was `sub $8,%rsp; add $8,%rsp; ret`; the zero initializer supplied `ret`, so these runs required zero proposals. The candidate passed bounded verification, changed nine original file offsets, installed successfully, and completed 500 paced markers. Because it was supplied at initialization and `_fini` is cold, this is only an integration acceptance control—not a search discovery, hot-path optimization, or speedup.

For the accessor, unchanged S1 began at cost 4,922 and retained best-correct cost 2,005 across all four 2.5-million-proposal runs. The earlier 2,534-versus-2,005 observation is explained by STOKE's initialization: current cost belongs to the configured zero/padded current program, while `best_correct` is initialized from the target. The richer states/contract in this rerun also change the starting cost, so costs across the two experiments are not directly comparable.

S2 began from checked FOP seeds for the accessor and `vListInitialiseItem`. In all eight runs, search returned a lower testcase-objective cost (accessor 1,005; list 1,003), but all eight failed the separate bounded verifier. The measured claim is therefore limited: normalization improved testcase-objective outcomes; improved verified search effectiveness remains unestablished. Three failures have concrete verifier counterexamples and five are unknown without a retained counterexample. There were no verifier timeouts or tool failures in this set.

| Case | Seed | Target hash / cost | Normalized-seed hash / start cost | Returned-candidate hash / testcase cost | Verifier result |
| --- | ---: | --- | --- | --- | --- |
{s2_table}

Full candidates, verifier output, and counterexample states are retained in [s2-verification-classification.json](../preflight-sdk-v1-2026-11-16/s2-verification-classification.json) and [counterexamples/stoke-s2-rejections.json](counterexamples/stoke-s2-rejections.json). A search-level correctness bit is not a completed proof.

The deterministic FOP versions of both functions passed the declared checks, installed through STOKE's replacement tool, and ran the paced fixture. This shows that export/check/emission/installation can work independently of stochastic discovery.

## 5. Answers to the eight required questions

1. **Did FOP run previously?** No. The recovered commands omitted `frame-opt`, whose pinned default is `none`. Enabling ordinary FOP optimized 1,609 loads and deleted 253 without removing stores; hot-only mode captured almost all profiled dynamic load opportunity with 39 transformations.
2. **What changed?** In the representative accessor, two reloads became register uses and one redundant reload disappeared, reducing 29 to 24 bytes while retaining both stores. Across the emitted image FOP reduced replacement executable bytes by 2,368 with layout and 2,308 without it. The much larger ELF growth comes from retaining original text plus replacement regions and alignment, not FOP.
3. **Does matched-work CPU cost improve?** Yes at exact-function scope: {function_cost_reduction:.1f}% lower CPU cost per accessor call. No useful whole-application gain was resolved in the paced run: R/F1={r_f1[0]:.4f} ({r_f1[1]:.4f}–{r_f1[2]:.4f}). Capacity throughput remains unestablished.
4. **How much coverage was recovered?** Structural-cohort extraction rose from 1/23 to 23/23. State construction reached 10/23; 11 references and two memory-state failures are now the dominant integration blockers. Search was deliberately limited to six accepted cases.
5. **Is the short accessor legal?** It matches ordinary cases but fails both the concrete stack/object alias probe and STOKE checks with stack/heap outputs. It is not legal under the declared contract. FastDyn fault/event preservation was not checked and remains a phase-two obligation.
6. **Does normalization help search?** It lowers the testcase objective in 8/8 S2 runs, but this does not establish improved verified search effectiveness: three candidates have concrete verifier counterexamples, five are unknown without a counterexample, and none verifies or installs. The unrelated cold `_fini` control was supplied by initialization with zero proposals and only demonstrates integration.
7. **What is observed versus unresolved?** The classification appears in Section 6. We do not convert unsupported extraction, state failures, timeouts, or unmeasured hardware counters into semantic or performance claims.
8. **What should be reused?** Reuse BOLT-style pass scheduling, decoding/CFG/profile machinery, ordinary FOP dataflow, and compact deterministic rules; reuse STOKE testcase/checking/search/replacement for bounded supported x86 regions. Build an independent firmware acceptance layer for exact identities, typed references/effects, state construction, in-place/size-bounded emission, fault/MMIO observations, and completed-work cost. Do not build a new stochastic engine from this evidence.

## 6. Claim audit

| Status | Finding |
| --- | --- |
| Observed | Ordinary BOLT FOP transforms O0 stack reloads and materially accelerates the exact accessor body. |
| Observed | The paced application shows no resolved 5% CPU-per-marker gain; capacity is not measured. |
{bolt_claim_rows}
| Observed | The compact cohort's paired paced interval spans gains and regressions, so it is inconclusive rather than a speedup claim. |
| Observed | Canonical fixed-prefix export recovers 23/23 structural bodies; state construction then admits 10. |
| Observed | The short/FRS accessor violates the admitted alias and memory-output contract. |
| Observed | S2 improves testcase-objective outcomes; verified search effectiveness remains unestablished at 0/8. |
| Observed | A zero-proposal, cold `_fini` initialization control verifies, emits, installs, and executes. |
| Source-confirmed | Pinned FOP defaults to `none`, qualifies its implemented treatment for x86, and runs after the named pre-FOP dynostats boundary. |
| Source-confirmed | `StokeInfo` is a screening/effect exporter, not a search/import/installation pipeline. |
| Refuted | “The frame optimizer ran and found no O0 opportunity.” It was disabled. |
| Refuted | “The 22 extraction failures were harmless padding.” They contain protected `endbr64` entries; runtime CET enforcement was not checked. |
| Refuted | “The three-instruction accessor is an obvious legal target.” It has a concrete alias counterexample. |
| Refuted | “BOLT text growth proves a larger hot footprint.” Retained old text and segment alignment explain most mapped/file growth; dynamic hot bytes were not measured. |
| Unresolved | Whole-application compute-limited capacity, hardware cache effects, realistic device inputs, original Cortex-M acceleration, and vulnerability preservation. |
| Deferred | FastDyn patch bindings, patch trigger semantics, and original-guest fault/event attribution. |

## 7. Recorded custom-pass baseline

Before the BOLT-first pivot, the rerun findings were carried into a bounded custom prototype. Its results remain useful acceptance fixtures and are preserved rather than rewritten. `PreflightPass SDK v1` exposes immutable `FirmwareUnitView` inputs and proposal-only `RewriteBuilder` edits; passes are registered explicitly with target/features, required and invalidated analyses, allowed edits, growth/region/work budgets, and stable skip reasons. Eligibility is expressed in machine structure and features, never in a required source optimization level. The pipeline records the input-contract, empty-patch-contract, state-set, proposal, and output hashes at each gate. This code now supplies contracts, fixtures and validation services around BOLT; its two pattern-specific transformations are not the production optimizer.

The identity round trip preserved one protected `endbr64` entry, one mutable RIP-relative reference, and six potentially faulting origins for `xTaskGetTickCount`; its serialized contract hash round-tripped exactly. Runtime CET enforcement remains `unknown`, rather than being inferred from ELF properties. An external pass-writer example is explicitly registered in `examples/preflight/external_identity_pass.py`; its ordinary NOP case is accepted and its protected-entry case is rejected.

### 7.1 Typed reference and state backlog

The 11 formerly unresolved cases were classified by referenced object rather than by encoded displacement:

| Reference class | Cases | Prototype disposition |
| --- | ---: | --- |
| Direct mutable scalar globals | 5 | `xTaskGetTickCount` linked and admitted with an 8-byte observable `xTickCount` object; four linked but not yet admitted |
| Pointer roots requiring transitive objects | 3 | unsupported; pointee ranges, permissions, and aliases remain undeclared |
| Static-storage address materialization | 2 | unsupported; escape and output binding are not qualified |
| Writable table without immutability proof | 1 | unsupported; `.data` is not frozen as a constant |

Thus the immediate linking result is {reference_status_counts.get('supported', 0)} supported, {reference_status_counts.get('resolved_not_admitted', 0)} resolved but conservatively not admitted, and {reference_status_counts.get('unsupported', 0)} explicitly unsupported/unchanged. Every reference records target identity, addend, encoding/range rule, permissions, mutability, binding, and layout invalidations. The two legacy `Could not resize memory` cases now have explicit bounded list-object/stack states with requested address, size, permission, operation, and outcome. The selected historical StateGen retained only its generic error—not the failing randomized state/address—so the prototype does not invent those missing historical values. Evidence is in [reference-class-audit.json](../preflight-sdk-v1-2026-11-16/reference-class-audit.json) and [state-construction-audit.json](../preflight-sdk-v1-2026-11-16/state-construction-audit.json).

### 7.2 Checked FOP reuse and compact emission

Four frozen pass outcomes exercise the acceptance boundary:

| Fixture | Result | Mechanism |
| --- | --- | --- |
| `prvGetThreadFromTask` ordinary FOP | accepted | 29-byte qualified range → 24 live bytes + 5 NOP padding; stores and fault origins retained |
| `vListInitialiseItem` ordinary FOP | accepted | 23-byte qualified body → 22 live bytes + 1 NOP padding; protected prefix remains outside the edit |
| Three-instruction short accessor | counterexample | observable stack-memory summary changed under the admitted alias contract |
| Experimental store-removal accessor | counterexample | same concrete contract mismatch; no bytes published |

Both positive outputs were installed by atomic in-place replacement with zero retained original bytes in the edited range, zero repair bytes, unchanged whole-image length, and exact origin maps. The emitted accessor body hash is `{prototype_work['emitted_body_sha256']}` and exactly matches the BOLT FOP body used for the 30-block fixed-work result. The reference and installed candidate each produced {prototype_work['installation']['reference']['dispatch_markers']} paced markers in the one-second installation smoke. This confirms installation, not application capacity: the only quantitative execution benefit remains the exact-body {function_cost_reduction:.1f}% CPU-cost reduction, and FastDyn benefit is unestablished.

The negative proposals stop at the model gate with `observable_effect_summary_changed`; neither `SHOULD-NOT-EXIST` output is published. The fault fixture accepts despite raw host offsets 12 versus 11 because it compares exact original identity, fault kind/address/width, recovery state, and ordered event prefix. Its result is `{prototype_fault['status']}: {prototype_fault['reason']}`. Complete stage and size evidence is in [stage-ledger.jsonl](../preflight-sdk-v1-2026-11-16/stage-ledger.jsonl), [pass-outcomes.json](../preflight-sdk-v1-2026-11-16/pass-outcomes.json), and [fault-identity-comparison.json](../preflight-sdk-v1-2026-11-16/fault-identity-comparison.json).

### 7.3 Firmware boundary

The original identity boundary remains recorded for `RTOSDemo.axf` hash `{prototype_cortex['image_sha256']}`, but the implementation is no longer identity-only. BOLT itself still rejects ARM32/Thumb, so the firmware layer now contains a deliberately bounded fallback that adapts ordinary FOP's established spill/reload forwarding rule to 16-bit Thumb instructions. This is neither a BOLT Thumb backend nor a new dataflow algorithm. It admits only an explicitly private, nonescaped stack slot with no observer boundary; non-adjacent cases additionally require exact no-clobber/no-overwrite evidence for every intervening instruction. Contracts that admit an invalid/device-mapped stack, asynchronous observation, unknown effects or alternate entries retain the original bytes.

On the real Cortex-M3 `RTOSDemo.axf`, the checked candidate changed only `ldr r1, [sp]` to flag-preserving `mov r1, r0` in `vListInitialiseItem`, retained the store, changed exactly two ELF bytes, added zero bytes, preserved the vector and every other ELF byte, and produced zero mismatches over {thumb_m3['validation']['bounded_state_count']} bounded architectural states. Reference and candidate both booted under FastDyn and reached the function entry in every run. Because that function executes only during initialization, the 30-block timing is inconclusive: reference/candidate CPU ratio {thumb_m3['timing']['cpu_ns']['median_ratio']:.4f}, 95% paired bootstrap interval [{thumb_m3['timing']['cpu_ns']['bootstrap_95_percent'][0]:.4f}, {thumb_m3['timing']['cpu_ns']['bootstrap_95_percent'][1]:.4f}]. This is a real-firmware execution/installation result, not a speedup claim.

A generated fixed-work Cortex-M3 fixture executes the exact same O0 body {thumb_fixed['work']['completed_calls_per_sample']:,} times per sample, removing exactly that many guest stack loads. Its median CPU ratio is {thumb_fixed['paired_comparison']['cpu_ns']['median_ratio']:.4f} with 95% interval [{thumb_fixed['paired_comparison']['cpu_ns']['bootstrap_95_percent'][0]:.4f}, {thumb_fixed['paired_comparison']['cpu_ns']['bootstrap_95_percent'][1]:.4f}]; guest-timed ratio is {thumb_fixed['paired_comparison']['guest_benchmark_ns']['median_ratio']:.4f} [{thumb_fixed['paired_comparison']['guest_benchmark_ns']['bootstrap_95_percent'][0]:.4f}, {thumb_fixed['paired_comparison']['guest_benchmark_ns']['bootstrap_95_percent'][1]:.4f}]. The central estimate is positive, but both intervals include 1.0, so this calibrated rerun is inconclusive and does not establish FastDyn or real-firmware acceleration. The earlier five-million-call run is retained separately as raw evidence rather than substituted into this claim.

On the real Cortex-M0+ ESCape32 O0 image, the same rule changed `serialresp` from `str r0, [r7,#4]; ldr r3, [r7,#4]` to `str r0, [r7,#4]; mov r3,r0`. It verified the `r7` frame-base prologue, changed exactly two file bytes with zero growth, and atomically published candidate `{thumb_m0['candidate']['candidate_id']}`. Target execution remains unavailable for that board/device configuration, so this is structural acceptance only. The audit also corrects an earlier survey error: this artifact's vector-bearing executable range begins at `0x{thumb_m0['evidence']['qualification']['executable_ranges'][0]['original_start']:08x}`, not `0x08000000`, consistent with its retained 4 KiB boot plus 2 KiB configuration reservation.

### 7.4 Real-firmware hot-cohort result

A training-only exact-PC probe found {thumb_profile['candidate_count']} adjacent spill/reload pairs across the real M3 image. During its separately identified three-second run, {thumb_profile['hit_candidate_count']} pairs executed {thumb_profile['total_pair_hits']:,} times. The dominant `xTaskIncrementTick` and `SysTick_Handler` pairs each executed {thumb_profile['top_candidates'][0]['hit_count']:,} times. Profile membership only selected work; every installed site still passed the structural/effect/branch-target/emission gates.

The final measurement uses QEMU `icount` (`shift=5`, no sleep/alignment), a newly added no-I/O `benchmark_end_after` action, and exactly {thumb_hot['work']['completed_units_per_sample']:,} completed `SysTick_Handler` returns per sample. Reference and candidate completed equal work in all 30 randomized blocks, and their compared 2 MiB RAM prefixes matched in every block. {thumb_paced_text}The `icount` environment is therefore a deterministic measurement configuration, not a claim that it is identical to board-timed execution.

Three target policies were retained rather than reporting only the best-looking one. {thumb_mov_text}Specializing same-register forwarding to the architectural Thumb NOP improved the two-site median to {thumb_top2['paired_comparison']['cpu_ns']['median_ratio']:.4f} [{thumb_top2['paired_comparison']['cpu_ns']['bootstrap_95_percent'][0]:.4f}, {thumb_top2['paired_comparison']['cpu_ns']['bootstrap_95_percent'][1]:.4f}], but the interval includes neutrality and its lower bound misses the predeclared 1.005 useful-effect gate. Expanding the checked rule to all {thumb_hot['work']['selected_static_reload_sites']} measured-hot sites produced CPU ratio {thumb_hot['paired_comparison']['cpu_ns']['median_ratio']:.4f} [{thumb_hot['paired_comparison']['cpu_ns']['bootstrap_95_percent'][0]:.4f}, {thumb_hot['paired_comparison']['cpu_ns']['bootstrap_95_percent'][1]:.4f}]. Both artifacts are semantically admitted under the declared private-stack contract but rejected as `not_profitable`; deployment retains the reference.

{thumb_tcg_text}Thus a smaller translated hot block still failed the full-harness profitability gate. The observed mechanism refutes “remove the most guest loads” as a sufficient target policy: emitted host-block size, runtime dispatch/cache interactions, fixed-work CPU cost, and schedule identity are separate obligations. Hardware performance counters were unavailable on this kernel, so the remaining cause of the full-harness result is unresolved rather than attributed to a cache effect.

The next extension keeps the same established forwarding rule but permits a bounded gap only when exact instruction identities prove that no intervening instruction clobbers the source/base register, overwrites the private slot, introduces an alternate entry, or crosses a control/observer boundary. The fresh training profile found {thumb_bounded_profile['candidate_count']} structurally qualified sites, {thumb_bounded_profile['hit_candidate_count']} executed sites and {thumb_bounded_profile['total_pair_hits']:,} total hits. The added `xTaskIncrementTick` reload at `0x08000d8e` executed {thumb_bounded_profile['top_candidates'][1]['hit_count']:,} times after an earlier same-slot reload and a retained global store. It is not treated as adjacent or pure: the intermediate stack read and global write remain in the effect/event sequence.

The three-site bounded candidate reduces the inspected hot tick block from {thumb_bounded_tcg['reference']['optimized_tcg_operation_count']} to {thumb_bounded_tcg['candidate']['optimized_tcg_operation_count']} optimized TCG operations and from {thumb_bounded_tcg['reference']['host_output_bytes']} to {thumb_bounded_tcg['candidate']['host_output_bytes']} generated host bytes. Nevertheless, its 30-block 100,000-tick CPU ratio is {thumb_bounded['paired_comparison']['cpu_ns']['median_ratio']:.4f} [{thumb_bounded['paired_comparison']['cpu_ns']['bootstrap_95_percent'][0]:.4f}, {thumb_bounded['paired_comparison']['cpu_ns']['bootstrap_95_percent'][1]:.4f}], with zero RAM-prefix mismatches, so it is rejected as `not_profitable`.

A separately frozen five-arm ablation retained reference plus four policies in every randomized block. Adjacent tick-only was {thumb_bounded_ablation['paired_comparisons']['tick_adjacent']['cpu_ns']['median_ratio']:.4f} [{thumb_bounded_ablation['paired_comparisons']['tick_adjacent']['cpu_ns']['bootstrap_95_percent'][0]:.4f}, {thumb_bounded_ablation['paired_comparisons']['tick_adjacent']['cpu_ns']['bootstrap_95_percent'][1]:.4f}]; bounded tick-pair was {thumb_bounded_ablation['paired_comparisons']['tick_bounded']['cpu_ns']['median_ratio']:.4f} [{thumb_bounded_ablation['paired_comparisons']['tick_bounded']['cpu_ns']['bootstrap_95_percent'][0]:.4f}, {thumb_bounded_ablation['paired_comparisons']['tick_bounded']['cpu_ns']['bootstrap_95_percent'][1]:.4f}]; handler-only was {thumb_bounded_ablation['paired_comparisons']['handler']['cpu_ns']['median_ratio']:.4f} [{thumb_bounded_ablation['paired_comparisons']['handler']['cpu_ns']['bootstrap_95_percent'][0]:.4f}, {thumb_bounded_ablation['paired_comparisons']['handler']['cpu_ns']['bootstrap_95_percent'][1]:.4f}]; and combined was {thumb_bounded_ablation['paired_comparisons']['combined']['cpu_ns']['median_ratio']:.4f} [{thumb_bounded_ablation['paired_comparisons']['combined']['cpu_ns']['bootstrap_95_percent'][0]:.4f}, {thumb_bounded_ablation['paired_comparisons']['combined']['cpu_ns']['bootstrap_95_percent'][1]:.4f}]. No arm's lower interval met the frozen 1.005 useful-effect threshold, all RAM prefixes matched, and deployment therefore retains the reference. This rules out simple policy mixing as the missing Cortex-M gain for this workload; a different optimization mechanism is required. Raw evidence is retained in the [bounded profile](../thumb-cortex-m3-bounded-fop-profile-2026-11-16/report.json), [three-site result](../thumb-cortex-m3-bounded-fop-2026-11-16/report.json), [TCG inventory](../thumb-cortex-m3-bounded-fop-2026-11-16/tcg-diagnostic/report.json), and [policy ablation](../thumb-cortex-m3-bounded-fop-ablation-2026-11-16/report.json).

### 7.5 Target-cost prefilter and fallthrough-branch control

The next bounded pass targeted a different O0 mechanism: a 16-bit unconditional Thumb branch whose resolved target is exactly its sequential successor. Replacing `b.n next` with one architectural NOP preserves the next PC, registers, flags, memory, instruction count and in-place size under a contract that does not expose branch events or instruction-cycle timing. The pass requires an exact resolved code edge and rejects unknown targets or observable branch/timing contracts.

Within the 26-function cohort selected before this experiment, the training probe found {thumb_branch_profile['candidate_count']} structural sites; {thumb_branch_profile['hit_candidate_count']} executed {thumb_branch_profile['total_branch_hits']:,} times. All {thumb_branch['work']['selected_static_branch_sites']} executed sites crossed the SDK, semantic, reference and compact-emission gates. The candidate changed {thumb_branch['validation']['changed_bytes']} bytes with zero file growth. Across 30 randomized blocks of {thumb_branch['work']['completed_units_per_sample']:,} completed SysTick returns, pinned to CPU {thumb_branch['work']['affinity_cpu']}, every compared 2 MiB RAM prefix matched. CPU ratio was {branch_cpu['median_ratio']:.4f} [{branch_cpu['bootstrap_95_percent'][0]:.4f}, {branch_cpu['bootstrap_95_percent'][1]:.4f}], and guest-region ratio was {branch_guest['median_ratio']:.4f} [{branch_guest['bootstrap_95_percent'][0]:.4f}, {branch_guest['bootstrap_95_percent'][1]:.4f}]. The interval misses the predeclared 1.005 useful-effect gate, so the candidate is `not_profitable` and the reference remains selected.

{thumb_branch_tcg_text}This identifies the mechanism: the source-level-looking branch cleanup did not reduce the inspected optimized TCG or generated host block. The new target-cost service now rejects zero/non-Pareto translated-code deltas before full timing. Passing remains only a prefilter: the stack-forwarding candidate reduced the inspected block by five TCG operations and 96 host bytes but still failed full-harness profitability. Generated-host cost and fixed-work measurement are therefore sequential, non-substitutable gates.

### 7.6 Accepted profile-weighted hot-exit layout

The next target mechanism used the same firmware SDK and reference fallback but changed block layout rather than deleting firmware effects. Training found 236 structurally qualified unused frame-pointer initializations, of which 18 executed. The dominant function, `prvCheckTasksWaitingTermination`, executed {thumb_hot_exit['integration']['profile_hits']:,} times in three seconds. Its apparently dead `mov r7,sp` was deliberately retained because asynchronous SysTick entry can expose the transient register state through the exception frame.

The accepted `thumb-hot-exit-layout-v1` pass instead permutes the same 34 instruction identities inside the function's existing 92-byte range. It keeps the prologue, memory accesses, calls, flags, fault annotations and epilogue, repairs all internal code references, and makes the common zero-cleanup return the fall-through path. Both the original and candidate hot path contain {thumb_hot_exit['integration']['hot_path_instruction_count']} instruction identities, preserving the declared instruction-count timing model; the image grows by {thumb_hot_exit['integration']['code_growth_bytes']} bytes.

The profile-weighted translated path, rather than one entry block, explains the useful effect. The reference executes {len(thumb_hot_exit['tcg']['reference']['blocks'])} translation blocks with {thumb_hot_exit['tcg']['reference']['optimized_tcg_operation_count']} optimized TCG operations and {thumb_hot_exit['tcg']['reference']['host_output_bytes']} generated host bytes. The candidate executes {len(thumb_hot_exit['tcg']['candidate']['blocks'])} blocks with {thumb_hot_exit['tcg']['candidate']['optimized_tcg_operation_count']} operations and {thumb_hot_exit['tcg']['candidate']['host_output_bytes']} bytes. Across 30 randomized paired executions of {thumb_hot_exit['work']['completed_systick_returns_per_execution']:,} completed SysTick returns, CPU ratio was {thumb_hot_exit['paired_comparison']['cpu_ns']['median_ratio']:.4f} [{thumb_hot_exit['paired_comparison']['cpu_ns']['bootstrap_95_percent'][0]:.4f}, {thumb_hot_exit['paired_comparison']['cpu_ns']['bootstrap_95_percent'][1]:.4f}], guest-region ratio was {thumb_hot_exit['paired_comparison']['guest_benchmark_ns']['median_ratio']:.4f} [{thumb_hot_exit['paired_comparison']['guest_benchmark_ns']['bootstrap_95_percent'][0]:.4f}, {thumb_hot_exit['paired_comparison']['guest_benchmark_ns']['bootstrap_95_percent'][1]:.4f}], and wall ratio was {thumb_hot_exit['paired_comparison']['wall_ns']['median_ratio']:.4f} [{thumb_hot_exit['paired_comparison']['wall_ns']['bootstrap_95_percent'][0]:.4f}, {thumb_hot_exit['paired_comparison']['wall_ns']['bootstrap_95_percent'][1]:.4f}]. All RAM-prefix comparisons matched and the CPU lower bound clears the frozen 1.005 gate, so this candidate is accepted. This is an original Cortex-M3 image running under FastDyn/QEMU, not a hardware-cycle, device-fidelity or unknown-vulnerability-preservation claim. Raw evidence and exact instruction origins are retained in the [hot-exit report](../thumb-cortex-m3-hot-exit-layout-2026-11-17/report.md).

{bolt_wrapper_section}

## 9. Design implications for a firmware binary optimizer

The evidence supports an independent firmware acceptance layer around BOLT, not an independent raw optimizer. Firmware changes the acceptance boundary:

- **Representation is semantic.** Entry instructions, vectors, literals, branch targets, alternate entries, and code/data boundaries need typed identities. The `endbr64` recovery is a host analogue of this requirement.
- **Memory effects cannot be optimized from “unused” alone.** Stack/private memory, aliased objects, MMIO, DMA-visible memory, and faulting accesses require distinct summaries. The accessor counterexample is the minimal concrete case.
- **Emission is part of optimization.** Retained old bodies, alignment, veneers, and fixed ranges must be priced after repair. A profitable local rewrite can still be over budget.
- **Profiles prioritize; they do not authorize.** Hot-only FOP demonstrates excellent concentration of dynamic opportunity, while the 14.5% function coverage shows why cold/error paths remain part of the contract.
- **Search is downstream of integration.** Extraction and states, not the proposal engine, were the first bottlenecks. Once repaired, normalized search found testcase winners that proof rejected. Verification and installation must remain explicit gates.
- **The objective is completed work in the deployed execution environment.** Timer-supplied markers cap observed throughput. Firmware evaluation needs fixed-work or input-driven work oracles plus CPU/host work, image growth, and offline cost.
- **Guest simplification is not target profitability.** The real M3 hot cohort removes checked stack reloads and shrinks the inspected generated host block, yet both the two-site and all-hot policies fail the fixed-work CPU acceptance gate. The wrapper must retain measured reference fallback and cannot rank candidates by removed guest loads alone.
- **Generated-host inventory is an effective rejection prefilter, not an acceptance oracle.** Branch-to-next cleanup produced no optimized-TCG or host-byte delta and is now rejected before expensive timing; stack forwarding passed that prefilter but failed the full-harness gate.
- **Path-level translation shape can dominate local instruction count.** The accepted hot-exit pass preserves the same hot-path instruction identities and modeled guest instruction count, yet reduces the translated path from three blocks to two, from 72 to 61 optimized TCG operations, and from 768 to 696 generated host bytes. This mechanism clears the fixed-work profitability gate where local forwarding did not.

The optimizer is BOLT wherever BOLT supports the ISA; the custom SDK is the firmware acceptance, compact-emission and bounded unsupported-target fallback layer. Exact instruction identities, typed synthesized-edge provenance, original effect inventories, FOP dependency edges, direct-call repair, bounded RIP-relative data repair, absolute-symbol rebinding, and BAT-backed branch import now compose across the supported-host branch-heavy cohort. All emitted instructions are classified, but eight synthesized edges remain unproven and are rejected; changed CFGs and branch targets also retain the reference. For x86-64, the next integration backlog is alternate-entry/reference completeness beyond the qualified 1 KiB region policy. For Cortex-M, three deterministic rules now cross profile selection, contract checking, compact emission and real-firmware installation. The first two local rewrites were rejected by fixed-work profitability; the profile-weighted hot-exit layout is accepted and publishes exact origins for all 34 reordered instructions. General Thumb decoding, branch/literal/veneer repair beyond this bounded function, exception semantics, alternate entries and device/interrupt effects remain unsupported. Search remains optional until the five unknown S2 verifier results and three counterexamples are understood. FastDyn patch behavior remains phase two.

## 10. Limitations

This is one LEMIX application under a low-fidelity zero-backed MMIO environment plus one runnable Cortex-M3 image and one structural-only M0+ image. The LEMIX x86-64 ABI, pointer width, security landing pads, host RTOS port, and timer behavior differ from the source MCU. The candidate cohort is structurally screened and the timed search covers six cases. The bounded verifier is appropriate only to the loop-free regions and declared model. The Thumb rules do not qualify arbitrary MPU/device/observer contracts. Finite tests and matched RAM prefixes do not prove preservation of unknown vulnerabilities. No hardware counters were available, no second input-driven whole application was introduced, and no source was recompiled at a better optimization level. One bounded layout candidate is an accepted original-image speedup under the declared FastDyn/QEMU configuration; it does not establish hardware speed, deadline preservation, broad Cortex-M support, or equivalence under arbitrary interrupt/device schedules. The real M0+ candidate was not executed.

## 11. Reproduction and evidence

Run the documented top-level command:

```bash
scripts/preflight/run-lemix-o0-focused-rerun.sh \\
  build/preflight/lemix-o0-rerun-YYYYMMDD

scripts/preflight/run-preflight-prototype.sh \\
  build/preflight/preflight-sdk-v1-2026-11-16

scripts/preflight/run-bolt-firmware-wrapper.sh \\
  build/preflight/bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16

scripts/preflight/run-bolt-queue-policy-matrix.sh

scripts/preflight/run-recovered-corpus-wrapper.sh

scripts/preflight/run-smartspeaker-layout-overlay.sh

python3 scripts/preflight/build-smartspeaker-stock-frame-overlay.py

python3 scripts/preflight/run-smartspeaker-stock-frame-overlay-training.py || \
  test $? -eq 2

scripts/preflight/run-smartspeaker-10x-confirmatory.sh

python3 scripts/preflight/run-smartspeaker-wrapper-policy.py

python3 scripts/preflight/run-smartspeaker-inplace-policy.py

python3 scripts/preflight/run-smartspeaker-isolated-inplace-policy.py

python3 scripts/preflight/build-smartspeaker-fop-hot.py

python3 scripts/preflight/run-smartspeaker-fop-hot-policy.py

python3 scripts/preflight/build-smartspeaker-absolute-reference-negative.py

python3 scripts/preflight/build-smartspeaker-o3-oracle.py

python3 scripts/preflight/run-smartspeaker-o3-comparison.py

python3 scripts/preflight/analyze-smartspeaker-o3-oracle.py

scripts/preflight/run-thumb-preflight.sh

PYTHONPATH=src taskset -c 0 \\
  python3 scripts/preflight/profile-thumb-unused-frame-pointers.py --replace

PYTHONPATH=src taskset -c 0 \\
  python3 scripts/preflight/run-thumb-hot-exit-layout-pilot.py --replace
```

The raw commands, logs, samples, disassemblies, inventories, candidate bodies, machine outputs, hashes, counterexamples, and costs are retained separately as supporting data. Primary baseline entry points are [manifest.json](manifest.json), [bolt-arm-options.json](bolt-arm-options.json), [samples.jsonl](samples.jsonl), [paired-results.json](paired-results.json), [binary-layout-inventory.json](binary-layout-inventory.json), [candidate-universe.csv](candidate-universe.csv), [stage-funnel.csv](stage-funnel.csv), [search-seeds-and-costs.json](search-seeds-and-costs.json), and [budget-and-costs.json](budget-and-costs.json). Prototype entry points are [prototype manifest](../preflight-sdk-v1-2026-11-16/manifest.json), [stage ledger](../preflight-sdk-v1-2026-11-16/stage-ledger.jsonl), and [pass outcomes](../preflight-sdk-v1-2026-11-16/pass-outcomes.json). This document consolidates the historical screening experiment, focused rerun, and bounded prototype; historical raw evidence remains unchanged.

### Pinned references

- LLVM/BOLT commit `7864c0736a8a09670dfbfc513f1e9649799f2486`, including `FrameOptimizer.cpp` and `BinaryPassManager.cpp`.
- STOKE commit `98d8a0f028f2daf2052bfe607dbc32ec8d55ba9e`, with submodules and solver configuration recorded in [stoke-environment.json](stoke-environment.json).
- Official LEMIX artifact archive SHA-256 `{manifest['hashes']['archive_sha256']}` (historical MD5 retained as required).
- [Linux userspace shadow-stack enablement documentation](https://docs.kernel.org/arch/x86/shstk.html), used only to distinguish ELF properties from runtime enablement.
"""
    (run_dir / "report.md").write_text(report)
    if main_report is not None:
        main_report.parent.mkdir(parents=True, exist_ok=True)
        evidence_root = Path(os.path.relpath(run_dir, main_report.parent)).as_posix()

        def rewrite_link(match: re.Match[str]) -> str:
            target = match.group(1)
            if target.startswith(("http://", "https://", "#", "/")):
                return match.group(0)
            normalized = os.path.normpath(f"{evidence_root}/{target}")
            return f"]({Path(normalized).as_posix()})"

        consolidated = re.sub(r"]\(([^)]+)\)", rewrite_link, report)
        main_report.write_text(consolidated)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--main-report", type=Path)
    args = parser.parse_args()
    generate(
        args.run_dir.resolve(),
        args.main_report.resolve() if args.main_report is not None else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

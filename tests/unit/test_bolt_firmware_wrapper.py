"""Acceptance assertions for the BOLT-first firmware wrapper evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from elftools.elf.elffile import ELFFile


def _result() -> dict:
    root = Path(__file__).resolve().parents[2]
    path = (
        root
        / "build/preflight/bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16"
        / "wrapper-result.json"
    )
    if not path.exists():
        pytest.skip("BOLT-first wrapper evidence has not been generated")
    return json.loads(path.read_text())


def _recovered_corpus_policy_result() -> dict:
    root = Path(__file__).resolve().parents[2]
    path = (
        root
        / "build/preflight/recovered-firmware-corpus-2026-11-16"
        / "bolt-firmware-wrapper"
        / "held-out-policy-report.json"
    )
    if not path.exists():
        pytest.skip("recovered firmware corpus policy evidence has not been generated")
    return json.loads(path.read_text())


def test_real_bolt_runs_before_firmware_wrapper_and_preserves_entries() -> None:
    result = _result()
    assert result["status"] == "integration_accepted"
    assert result["architecture"] == "real BOLT optimization followed by firmware wrapper"
    assert result["fop"]["optimized_redundant_loads"] == 1609
    assert result["fop"]["deleted_loads"] == 253
    assert len(result["protected_entries"]) == 22
    assert {item["status"] for item in result["protected_entries"]} == {"preserved"}


def test_compact_adoption_uses_bolt_bodies_with_complete_origins() -> None:
    result = _result()
    compact = result["compact_selected_adoption"]
    assert compact["classification"] == "exact BOLT bodies imported after optimization"
    assert compact["per_instruction_origin_complete"] is True
    assert compact["candidate"]["file_bytes"] == result["reference"]["file_bytes"]
    assert compact["paced_installation_smoke"]["dispatch_markers"] == 100
    assert {item["symbol"] for item in compact["edits"]} == {
        "prvGetThreadFromTask", "vListInitialiseItem"
    }
    assert all(item["origin_mappings"] for item in compact["edits"])


def test_custom_pass_results_are_retained_only_as_baseline() -> None:
    result = _result()
    baseline = result["custom_pass_baseline"]
    assert baseline["classification"] == (
        "custom_bounded_prototype_baseline_not_production_optimizer"
    )
    assert [item["status"] for item in baseline["outcomes"]] == [
        "accepted", "accepted", "counterexample", "counterexample"
    ]


def test_automatic_compact_cohort_is_bolt_sourced_and_size_neutral() -> None:
    result = _result()
    automatic = result["automatic_compact_adoption"]
    assert automatic["selected_function_count"] == 115
    assert automatic["saved_live_bytes"] == 609
    assert automatic["candidate"]["file_bytes"] == result["reference"]["file_bytes"]
    assert automatic["paced_installation_smoke"]["dispatch_markers"] == 100
    assert all(item["per_instruction_origin_complete"] for item in automatic["edits"])


def test_memory_effect_qualification_exports_exact_fop_dependencies() -> None:
    result = _result()
    qualified = result["memory_effect_qualified_adoption"]
    assert qualified["selected_function_count"] == 855
    assert qualified["exact_memory_sequence_function_count"] == 473
    assert qualified["fop_dependency_function_count"] == 382
    assert qualified["forwarding_dependency_count"] == 1219
    assert qualified["rejection_counts"] == {
        "branch_heavy_data_repair:unsupported_rip_relative_encoding": 1
    }
    assert qualified["saved_live_bytes"] == 2368
    assert qualified["candidate"]["file_bytes"] == result["reference"]["file_bytes"]
    assert qualified["paced_installation_smoke"]["dispatch_markers"] == 100
    for edit in qualified["edits"]:
        if edit["classification"] == "bolt_fop_store_to_load_dependency":
            assert edit["forwarding_dependencies"]


def test_direct_call_extension_repairs_every_original_target() -> None:
    result = _result()
    qualified = result["memory_effect_qualified_adoption"]
    direct = qualified["direct_call_extension"]
    assert direct["structurally_selected_function_count"] == 65
    assert direct["qualified_function_count"] == 65
    assert direct["repaired_call_count"] == 159
    assert direct["saved_live_bytes"] == 127
    edits = [
        item for item in qualified["edits"]
        if item["reference_repair_class"] == "x86_direct_call_rel32"
    ]
    assert len(edits) == 684
    assert sum(len(item["reference_repairs"]) for item in edits) == 1982
    assert all(
        repair["target_original_address"] > 0
        for item in edits
        for repair in item["reference_repairs"]
    )
    root = Path(__file__).resolve().parents[2]
    image_path = (
        root
        / "build/preflight/bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16"
        / "SmartSpeaker.bolt-qualified-compact"
    )
    with image_path.open("rb") as stream:
        elf = ELFFile(stream)
        for edit in edits:
            for repair in edit["reference_repairs"]:
                address = repair["emitted_call_address"]
                segment = next(
                    item
                    for item in elf.iter_segments()
                    if item["p_type"] == "PT_LOAD"
                    and int(item["p_vaddr"]) <= address
                    and address + 5
                    <= int(item["p_vaddr"]) + int(item["p_filesz"])
                )
                offset = int(segment["p_offset"]) + address - int(segment["p_vaddr"])
                stream.seek(offset)
                call = stream.read(5)
                assert call[0] == 0xE8
                displacement = int.from_bytes(call[1:], "little", signed=True)
                assert address + 5 + displacement == repair["target_original_address"]


def test_branch_extensions_keep_targets_inside_adopted_body() -> None:
    result = _result()
    qualified = result["memory_effect_qualified_adoption"]
    branch = qualified["internal_branch_extension"]
    assert branch["qualified_function_count"] == 3
    assert branch["validated_branch_count"] == 3
    assert branch["saved_live_bytes"] == 16
    heavy = qualified["branch_heavy_extension"]
    assert heavy["qualified_function_count"] == 646
    assert heavy["validated_branch_count"] == 4956
    assert heavy["repaired_call_count"] == 1770
    assert heavy["repaired_data_reference_count"] == 1321
    assert heavy["saved_live_bytes"] == 1539
    assert heavy["selection_rejection_counts"]["branch_target_identity_changed"] == 14
    assert heavy["selection_rejection_counts"]["control_edge_set_changed"] == 45
    assert heavy["selection_rejection_counts"]["unproven_instruction_provenance"] == 8
    edits = [item for item in qualified["edits"] if item["branch_identity_records"]]
    assert len(edits) == 649
    assert sum(len(item["branch_identity_records"]) for item in edits) == 4959
    assert {"uxListRemove", "prvBytesInBuffer", "mem_set"}.issubset(
        {item["symbol"] for item in edits}
    )
    for edit in edits:
        start = edit["original_address"]
        end = start + edit["live_bolt_bytes"]
        for record in edit["branch_identity_records"]:
            assert start <= record["emitted_branch_address"] < end
            assert start <= record["emitted_target_address"] < end
            assert record["target_original_address"] > 0


def test_bat_is_used_only_for_final_target_identity() -> None:
    result = _result()
    assert len(result["bolt_binary_sha256"]) == 64
    assert len(result["bat_dump_binary_sha256"]) == 64
    assert result["identity_sources"] == {
        "source_instruction_identity_and_order": "native post-optimization report",
        "final_instruction_offset_and_size": "Capstone decode of final BOLT ELF",
        "final_branch_target_identity": "BOLT BAT translation",
    }
    records = [
        record
        for edit in result["memory_effect_qualified_adoption"]["edits"]
        for record in edit["branch_identity_records"]
        if record.get("identity_source")
        == "native-source-final-offset-BAT-target"
    ]
    assert len(records) == 4956


def test_synthesized_jumps_require_exact_original_cfg_edges() -> None:
    result = _result()
    summary = result["native_post_optimization_summary"]
    assert summary["synthesized_control_edge_instruction_count"] == 2148
    assert summary["verified_synthesized_control_edge_instruction_count"] == 2140
    assert summary["unmapped_instruction_count"] == 0
    edge_records = [
        record
        for edit in result["memory_effect_qualified_adoption"]["edits"]
        for record in edit["branch_identity_records"]
        if record.get("provenance_kind") == "control_edge"
    ]
    assert len(edge_records) == 599
    assert all(
        record["source_block_original_address"] > 0
        and record["target_original_address"] > 0
        for record in edge_records
    )
    root = Path(__file__).resolve().parents[2]
    native = json.loads(
        (
            root
            / "build/preflight/bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16"
            / "bolt-post-optimization-report.json"
        ).read_text()
    )
    original_edges = {
        (
            edge["source_block_original_address"],
            edge["target_block_original_address"],
        )
        for function in native["functions"]
        for edge in function["original_control_edges"]
    }
    assert {
        (
            record["source_block_original_address"],
            record["target_original_address"],
        )
        for record in edge_records
    }.issubset(original_edges)


def test_rip_relative_extensions_preserve_original_data_targets() -> None:
    result = _result()
    qualified = result["memory_effect_qualified_adoption"]
    rip = qualified["rip_relative_extension"]
    mixed = qualified["combined_call_rip_extension"]
    assert rip["qualified_function_count"] == 9
    assert rip["repaired_reference_count"] == 14
    assert rip["saved_live_bytes"] == 39
    assert mixed["qualified_function_count"] == 17
    assert mixed["repaired_call_count"] == 53
    assert mixed["repaired_data_reference_count"] == 38
    assert mixed["saved_live_bytes"] == 38
    edits = [item for item in qualified["edits"] if item["data_reference_repairs"]]
    assert len(edits) == 568
    assert sum(len(item["data_reference_repairs"]) for item in edits) == 1373
    assert all(
        repair["target_original_address"] > 0
        for item in edits
        for repair in item["data_reference_repairs"]
    )


def test_absolute_symbol_references_are_rebound_to_original_identities() -> None:
    result = _result()
    qualified = result["memory_effect_qualified_adoption"]
    absolute = qualified["absolute_symbol_reference_extension"]
    assert absolute["structurally_selected_function_count"] == 2
    assert absolute["qualified_function_count"] == 2
    assert absolute["repaired_reference_count"] == 6
    edits = [
        item for item in qualified["edits"]
        if item.get("absolute_reference_repairs")
    ]
    assert {item["symbol"] for item in edits} == {"main_full", "vNet_LsnStart"}
    repairs = [
        repair
        for item in edits
        for repair in item["absolute_reference_repairs"]
    ]
    assert len(repairs) == 6
    root = Path(__file__).resolve().parents[2]
    image_path = (
        root
        / "build/preflight/bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16"
        / "SmartSpeaker.bolt-qualified-compact"
    )
    with image_path.open("rb") as stream:
        elf = ELFFile(stream)
        for repair in repairs:
            immediate_address = (
                repair["emitted_instruction_address"] + repair["immediate_offset"]
            )
            immediate_size = repair["immediate_size"]
            segment = next(
                item
                for item in elf.iter_segments()
                if item["p_type"] == "PT_LOAD"
                and int(item["p_vaddr"]) <= immediate_address
                and immediate_address + immediate_size
                <= int(item["p_vaddr"]) + int(item["p_filesz"])
            )
            offset = (
                int(segment["p_offset"])
                + immediate_address
                - int(segment["p_vaddr"])
            )
            stream.seek(offset)
            installed_target = int.from_bytes(
                stream.read(immediate_size), "little", signed=False
            )
            assert installed_target == repair["target_original_address"]


def test_paced_compact_timing_is_recorded_as_inconclusive() -> None:
    root = Path(__file__).resolve().parents[2]
    path = (
        root
        / "build/preflight/bolt-firmware-wrapper-edge-provenance-1k-absref-2026-11-16"
        / "auto-compact-paired-samples.json"
    )
    if not path.exists():
        pytest.skip("paired compact timing evidence has not been generated")
    timing = json.loads(path.read_text())
    comparison = timing["paired_comparisons"]["AUTO"]
    assert timing["method"]["paired_blocks"] is True
    assert timing["method"]["samples_per_variant"] == 30
    assert all(item["ok"] for variant in timing["variants"].values() for item in variant["samples"])
    assert comparison["bootstrap_95_percent"][0] < 1.0
    assert comparison["bootstrap_95_percent"][1] > 1.0


def test_hot_qualified_publication_is_separate_from_correctness() -> None:
    result = _result()
    hot = result["hot_qualified_adoption"]
    assert hot["selected_function_count"] == 59
    assert hot["saved_live_bytes"] == 160
    assert hot["candidate"]["file_bytes"] == result["reference"]["file_bytes"]
    assert hot["paced_installation_smoke"]["dispatch_markers"] == 100
    assert hot["profile_authorizes_correctness"] is False


def test_recovered_corpus_policy_beats_frozen_stock_bolt_without_overclaiming() -> None:
    result = _recovered_corpus_policy_result()
    assert result["status"] == "held_out_policy_evaluation_complete"
    assert result["summary"] == {
        "all_outputs_match": True,
        "fixed_layout_policy_exact_reference_fallbacks": 6,
        "fixed_layout_policy_generalized_gains": 3,
        "relocatable_policy_exact_reference_fallbacks": 3,
        "relocatable_policy_faster_than_stock_bolt": 8,
        "relocatable_policy_generalized_gains": 6,
        "relocatable_policy_slower_than_stock_bolt": 0,
        "relocatable_policy_vs_stock_inconclusive": 1,
        "workload_count": 9,
    }
    assert result["claims"] == {
        "better_than_frozen_stock_bolt_policy_on_supported_host_corpus": True,
        "fastdyn_speedup": False,
        "original_cortex_m_speedup": False,
        "whole_firmware_equivalence": False,
    }
    assert {
        workload_id
        for workload_id, workload in result["workloads"].items()
        if workload["training_choice"]["relocatable"] == "reference"
    } == {"combined_all", "crc8", "crc8dvbs2"}
    assert all(
        workload["all_outputs_match"]
        for workload in result["workloads"].values()
    )
    precision = result["precision_extensions"]["crc8"]
    assert precision["classification"] == (
        "post_primary_precision_extension_no_retuning"
    )
    assert precision["paired_blocks"] == 100
    assert precision["wrapper_policy_faster"] is True
    assert precision["all_outputs_match"] is True
    assert precision["artifacts_unchanged"] is True
    assert result["summary_including_declared_precision_extensions"] == {
        "primary_results_preserved": True,
        "relocatable_policy_faster_than_stock_bolt": 9,
        "relocatable_policy_slower_than_stock_bolt": 0,
        "relocatable_policy_vs_stock_inconclusive": 0,
        "workload_count": 9,
    }


def test_recovered_corpus_held_out_artifacts_follow_frozen_choices() -> None:
    result = _recovered_corpus_policy_result()
    root = Path(__file__).resolve().parents[2]
    timing_root = (
        root
        / "build/preflight/recovered-firmware-corpus-2026-11-16"
        / "bolt-firmware-wrapper/held-out-timing"
    )
    expected_hash = {
        "reference": result["artifacts"]["reference"]["sha256"],
        "full_fop": result["artifacts"]["full_relocatable"]["sha256"],
        "compact_fop": result["artifacts"]["compact_fixed_layout"]["sha256"],
    }
    for workload_id, workload in result["workloads"].items():
        timing = json.loads((timing_root / f"{workload_id}.json").read_text())
        assert timing["method"]["paired_blocks"] is True
        assert timing["method"]["samples_per_variant"] == 30
        assert timing["method"]["interleaved_randomization_seed"] == 20261117
        assert timing["method"]["program_arguments"][-1] == "13"
        assert timing["variants"]["relocatable_policy"]["sha256"] == expected_hash[
            workload["training_choice"]["relocatable"]
        ]
        assert timing["variants"]["fixed_layout_policy"]["sha256"] == expected_hash[
            workload["training_choice"]["fixed_layout"]
        ]
        assert all(
            variant["all_outputs_match"]
            for variant in timing["variants"].values()
        )


def test_queue_policy_beats_reference_and_frozen_stock_bolt() -> None:
    root = Path(__file__).resolve().parents[2]
    path = (
        root
        / "build/preflight/lemix-queue/bolt-firmware-wrapper"
        / "queue-policy-held-out-report.json"
    )
    if not path.is_file():
        pytest.skip("held-out queue policy evidence has not been generated")
    result = json.loads(path.read_text())
    assert result["status"] == "held_out_policy_evaluation_complete"
    assert result["selection"]["frozen_before_held_out_measurement"] is True
    assert result["selection"]["choice"] == {
        "fixed_layout": "reference",
        "relocatable": "full_fop",
    }
    assert result["held_out"]["paired_blocks"] == 30
    assert result["held_out"]["all_outputs_match"] is True
    assert result["claims"] == {
        "fastdyn_speedup": False,
        "faster_than_frozen_stock_bolt": True,
        "faster_than_reference": True,
        "original_cortex_m_speedup": False,
    }
    assert result["held_out"]["reference_over_policy_cpu"][
        "bootstrap_95_percent"
    ][0] > 1.0
    assert result["held_out"]["stock_bolt_over_policy_cpu"][
        "bootstrap_95_percent"
    ][0] > 1.0
    publication = json.loads((path.parent / "published-policy/index.json").read_text())
    assert publication["status"] == "atomically_published"
    assert publication["deployment_count"] == 2
    assert {
        (item["layout_mode"], item["choice"])
        for item in publication["deployments"]
    } == {("relocatable", "full_fop"), ("fixed_layout", "reference")}


def test_firm_bolt_extensions_are_registered_in_native_bolt() -> None:
    root = Path(__file__).resolve().parents[2]
    manager = (
        root
        / "third_party/preflight/llvm-project/bolt/lib/Rewrite/"
        "BinaryPassManager.cpp"
    ).read_text()
    target = (
        root
        / "third_party/preflight/llvm-project/bolt/lib/Target/X86/"
        "X86MCPlusBuilder.cpp"
    ).read_text()
    wrapper = (root / "scripts/preflight/run-bolt-firmware-wrapper.py").read_text()
    patch = (root / "patches/bolt-fastdyn-preflight.patch").read_text()

    assert "FastDynArithmeticStrengthReduction" in manager
    assert "FastDynCopyCleanup" in manager
    assert "FastDynMemoryTestFolding" in manager
    assert "FastDynTrivialFrameElimination" in manager
    assert "FastDynExactLeafCallInlining" in manager
    assert "--arithmetic-strength-reduction" in wrapper
    assert "--copy-cleanup" in wrapper
    assert "--copy-cleanup-hot-only" in wrapper
    assert "--copy-cleanup-adjacent-only" in wrapper
    assert "--memory-test-folding" in wrapper
    assert "--trivial-frame-elimination" in wrapper
    assert "--exact-leaf-call-inlining" in wrapper
    assert "--trivial-frame-elimination requires" in wrapper
    assert "--bolt-extra-arg" in wrapper
    assert "observe_fixed_work" in wrapper
    assert '"fixed_work_acceptance"' in wrapper
    assert '"paced_marker_counts_are_diagnostic_only": True' in wrapper
    for opcode in (
        "CMP8mi", "CMP16mi", "CMP32mi", "CMP64mi8",
        "CMP64mi32", "CMP64mr", "CMP64rm",
    ):
        assert opcode in target
    pass_source = (
        root / "tools/preflight/bolt/FastDynPatchContracts.cpp"
    ).read_text()
    assert "EqualRegisters" in pass_source
    assert 'Root["copy_cleanups"]' in pass_source
    assert "x86_mul40_dead_tmp_copy_coalesce" in pass_source
    assert "BC.MIB->mayStore(*Inst)" in pass_source
    assert "x86_mov32_self_after_exact_32bit_def" in pass_source
    assert "x86_dead_load_self_test_to_memory_test" in pass_source
    assert "x86_trivial_frame_elimination" in pass_source
    assert "x86_private_return_spill_frame_elimination" in pass_source
    assert "x86_exact_leaf_call_inline" in pass_source
    assert "FastDyn trivial-frame elimination requires --assume-abi" in pass_source
    assert "BF.getPersonalityFunction() != nullptr" in pass_source
    assert "CFIInstructions.size() != 4" in pass_source
    assert "getLiveOut(*Test)" in pass_source
    assert 'Instruction["block_known_execution_count"]' in pass_source
    assert "fop_dependency_crosses_intervening_store" in wrapper
    assert "BC.MIB->mayStore(*Inst)" in patch
    assert "x86_mov32_self_after_exact_32bit_def" in patch
    assert "replaceRegSelfTestWithMem" in patch
    assert "FastDynMemoryTestFolding" in patch
    assert "FastDynTrivialFrameElimination" in patch
    assert "FastDynExactLeafCallInlining" in patch
    assert "CFIInstructions.size() != 4" in patch
    assert "new file mode 100644" in patch

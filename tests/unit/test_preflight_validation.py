from __future__ import annotations

import json
from pathlib import Path

import pytest

from fastdyn.preflight.validation import compare_observation_traces


def _header(bundle_id: str) -> dict[str, object]:
    return {
        "type": "trace_header",
        "schema_version": 1,
        "bundle_id": bundle_id,
        "case_id": "malformed-packet-7",
        "contract_id": "fastdyn-guest-observations-v1",
        "input_hash": "input",
        "schedule_hash": "schedule",
        "device_model_hash": "device",
        "instrumentation_hash": "instrumentation",
        "patch_plan_hash": "patch-plan",
    }


def _write_trace(path: Path, bundle_id: str, events: list[dict[str, object]]) -> None:
    documents = [_header(bundle_id)]
    documents.extend({"sequence": index, **event} for index, event in enumerate(events))
    path.write_text(
        "".join(json.dumps(document, sort_keys=True) + "\n" for document in documents),
        encoding="utf-8",
    )


def _termination() -> dict[str, object]:
    return {"type": "termination", "behavior": "guest_fault"}


def test_accepts_equal_original_observations_with_different_emitted_pcs(tmp_path: Path) -> None:
    reference = tmp_path / "reference.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    event = {
        "type": "patch_trigger",
        "binding_id": "patch-1",
        "original_identity": "image:0x1000",
        "origin_pc": 0x1000,
        "trigger_phase": "instruction_entry",
        "ordinal": 1,
        "observed_state": {"r0": 7, "apsr": 0},
    }
    _write_trace(reference, "reference-bundle", [{**event, "current_pc": 0x1000}, _termination()])
    _write_trace(candidate, "candidate-bundle", [{**event, "current_pc": 0x8000}, _termination()])

    result = compare_observation_traces(reference, candidate)

    assert result.accepted


def test_rejects_modified_pre_hook_state(tmp_path: Path) -> None:
    reference = tmp_path / "reference.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    event = {
        "type": "patch_trigger",
        "binding_id": "patch-1",
        "original_identity": "image:0x1000",
        "origin_pc": 0x1000,
        "trigger_phase": "instruction_entry",
        "ordinal": 1,
        "observed_state": {"r0": 7},
    }
    _write_trace(reference, "reference", [event, _termination()])
    _write_trace(
        candidate,
        "candidate",
        [{**event, "observed_state": {"r0": 8}}, _termination()],
    )

    result = compare_observation_traces(reference, candidate)

    assert result.status == "counterexample"
    assert result.event_index == 0


def test_rejects_eliminated_unused_faulting_load_and_event_prefix(tmp_path: Path) -> None:
    reference = tmp_path / "reference.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    access = {
        "type": "guest_access",
        "origin_pc": 0x1200,
        "address": 0xDEADBEEF,
        "width": 4,
        "access_type": "read",
    }
    fault = {
        "type": "fault",
        "origin_pc": 0x1200,
        "fault_kind": "data_abort",
        "access_address": 0xDEADBEEF,
        "access_width": 4,
        "access_type": "read",
        "recovery_state": {"r0": 3, "sp": 0x20001000},
    }
    _write_trace(reference, "reference", [access, fault, _termination()])
    _write_trace(candidate, "candidate", [_termination()])

    result = compare_observation_traces(reference, candidate)

    assert result.status == "counterexample"
    assert result.event_index == 0


def test_unknown_event_is_unsupported_not_a_semantic_failure(tmp_path: Path) -> None:
    reference = tmp_path / "reference.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    _write_trace(reference, "reference", [_termination()])
    _write_trace(
        candidate,
        "candidate",
        [{"type": "future_observer", "payload": 1}, _termination()],
    )

    result = compare_observation_traces(reference, candidate)

    assert result.status == "unsupported"


def test_different_schedule_is_unknown(tmp_path: Path) -> None:
    reference = tmp_path / "reference.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    _write_trace(reference, "reference", [_termination()])
    _write_trace(candidate, "candidate", [_termination()])
    documents = candidate.read_text(encoding="utf-8").splitlines()
    header = json.loads(documents[0])
    header["schedule_hash"] = "different"
    candidate.write_text(
        json.dumps(header) + "\n" + "\n".join(documents[1:]) + "\n",
        encoding="utf-8",
    )

    result = compare_observation_traces(reference, candidate)

    assert result.status == "unknown"


def test_rejects_duplicated_patch_trigger(tmp_path: Path) -> None:
    reference = tmp_path / "reference.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    trigger = {
        "type": "patch_trigger",
        "binding_id": "patch-1",
        "original_identity": "image:0x1000",
        "origin_pc": 0x1000,
        "trigger_phase": "instruction_entry",
        "ordinal": 1,
        "observed_state": {"r0": 7},
    }
    _write_trace(reference, "reference", [trigger, _termination()])
    _write_trace(
        candidate,
        "candidate",
        [trigger, {**trigger, "ordinal": 2}, _termination()],
    )

    result = compare_observation_traces(reference, candidate)

    assert result.status == "counterexample"
    assert result.event_index == 1


@pytest.mark.parametrize(
    ("event", "changed"),
    [
        (
            {"type": "device", "effect_id": "uart-write", "observation": {"value": 1}},
            {"type": "device", "effect_id": "uart-write", "observation": {"value": 2}},
        ),
        (
            {"type": "callback", "effect_id": "dma", "observation": {"count": 1}},
            {"type": "callback", "effect_id": "dma", "observation": {"count": 0}},
        ),
        (
            {"type": "detector", "detector_id": "asan", "observation": "hit"},
            {"type": "detector", "detector_id": "asan", "observation": "miss"},
        ),
        (
            {"type": "coverage", "origin_pc": 0x1234, "observation": "edge"},
            {"type": "coverage", "origin_pc": 0x1236, "observation": "edge"},
        ),
        (
            {"type": "interrupt_opportunity", "origin_pc": 0x2000, "interrupt_id": 5, "disposition": "taken"},
            {"type": "interrupt_opportunity", "origin_pc": 0x2000, "interrupt_id": 5, "disposition": "deferred"},
        ),
        (
            {"type": "guest_time", "ticks": 10},
            {"type": "guest_time", "ticks": 11},
        ),
        (
            {"type": "termination", "behavior": "guest_fault"},
            {"type": "termination", "behavior": "normal"},
        ),
    ],
)
def test_rejects_each_declared_observation_class(tmp_path: Path, event, changed) -> None:
    reference = tmp_path / "reference.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    reference_events = [event] if event["type"] == "termination" else [event, _termination()]
    candidate_events = [changed] if changed["type"] == "termination" else [changed, _termination()]
    _write_trace(reference, "reference", reference_events)
    _write_trace(candidate, "candidate", candidate_events)

    result = compare_observation_traces(reference, candidate)

    assert result.status == "counterexample"

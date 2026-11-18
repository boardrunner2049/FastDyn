"""Strict comparison of reference and candidate FastDyn guest-observation traces."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


TRACE_SCHEMA_VERSION = 1

_HEADER_IDENTITY_FIELDS = (
    "case_id",
    "contract_id",
    "input_hash",
    "schedule_hash",
    "device_model_hash",
    "instrumentation_hash",
    "patch_plan_hash",
)

_REQUIRED_EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "guest_access": ("origin_pc", "address", "width", "access_type"),
    "fault": (
        "origin_pc",
        "fault_kind",
        "access_address",
        "access_width",
        "access_type",
        "recovery_state",
    ),
    "detector": ("detector_id", "observation"),
    "device": ("effect_id", "observation"),
    "callback": ("effect_id", "observation"),
    "patch_trigger": (
        "binding_id",
        "original_identity",
        "origin_pc",
        "trigger_phase",
        "ordinal",
        "observed_state",
    ),
    "coverage": ("origin_pc", "observation"),
    "interrupt_opportunity": (
        "origin_pc",
        "interrupt_id",
        "disposition",
    ),
    "guest_time": ("ticks",),
    "termination": ("behavior",),
}

_NON_SEMANTIC_FIELDS = frozenset(
    {
        "bundle_id",
        "current_pc",
        "emitted_pc",
        "host_timestamp_ns",
    }
)


class TraceFormatError(ValueError):
    """A trace cannot be interpreted under the selected observation contract."""


class UnsupportedTraceEvent(TraceFormatError):
    """A trace contains an event outside the versioned supported vocabulary."""


@dataclass(frozen=True)
class ObservationTrace:
    """One parsed execution trace, still bound to its producing bundle."""

    path: Path
    header: Mapping[str, Any]
    events: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class TraceComparison:
    """An acceptance decision with a stable status and diagnostic location."""

    status: str
    reason: str
    event_index: int | None = None
    reference_event: Mapping[str, Any] | None = None
    candidate_event: Mapping[str, Any] | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "event_index": self.event_index,
            "reference_event": self.reference_event,
            "candidate_event": self.candidate_event,
        }


def _require_fields(value: Mapping[str, Any], fields: Sequence[str], context: str) -> None:
    missing = [field for field in fields if field not in value]
    if missing:
        raise TraceFormatError(f"{context} is missing required fields: {', '.join(missing)}")


def load_observation_trace(path: Path) -> ObservationTrace:
    """Load JSON Lines without silently accepting malformed or unknown events."""

    path = path.resolve()
    documents: list[Any] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    documents.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise TraceFormatError(
                        f"{path}:{line_number}: invalid JSON: {exc.msg}"
                    ) from exc
    except OSError as exc:
        raise TraceFormatError(f"cannot read trace {path}: {exc}") from exc
    if not documents or not isinstance(documents[0], dict):
        raise TraceFormatError("trace requires an object header")

    header = documents[0]
    _require_fields(
        header,
        ("type", "schema_version", "bundle_id", *_HEADER_IDENTITY_FIELDS),
        "trace header",
    )
    if header["type"] != "trace_header" or header["schema_version"] != TRACE_SCHEMA_VERSION:
        raise TraceFormatError("unsupported trace header or schema version")

    events: list[Mapping[str, Any]] = []
    for index, document in enumerate(documents[1:]):
        if not isinstance(document, dict):
            raise TraceFormatError(f"event {index} must be an object")
        _require_fields(document, ("type", "sequence"), f"event {index}")
        if document["sequence"] != index:
            raise TraceFormatError(f"event {index} has a non-contiguous sequence number")
        event_type = document["type"]
        if event_type not in _REQUIRED_EVENT_FIELDS:
            raise UnsupportedTraceEvent(f"event {index} has unsupported type {event_type!r}")
        _require_fields(document, _REQUIRED_EVENT_FIELDS[event_type], f"event {index}")
        events.append(document)
    if not events or events[-1]["type"] != "termination":
        raise TraceFormatError("trace must end with a termination event")
    if any(event["type"] == "termination" for event in events[:-1]):
        raise TraceFormatError("termination must occur exactly once at the end of the trace")
    return ObservationTrace(path, header, tuple(events))


def _semantic_event(event: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key not in _NON_SEMANTIC_FIELDS}


def _compare_loaded(reference: ObservationTrace, candidate: ObservationTrace) -> TraceComparison:
    for field in _HEADER_IDENTITY_FIELDS:
        if reference.header[field] != candidate.header[field]:
            return TraceComparison(
                "unknown",
                f"execution identity mismatch in trace header field {field}",
            )

    common = min(len(reference.events), len(candidate.events))
    for index in range(common):
        reference_event = _semantic_event(reference.events[index])
        candidate_event = _semantic_event(candidate.events[index])
        if reference_event != candidate_event:
            return TraceComparison(
                "counterexample",
                "declared guest observation differs",
                index,
                reference_event,
                candidate_event,
            )
    if len(reference.events) != len(candidate.events):
        index = common
        return TraceComparison(
            "counterexample",
            "ordered event prefix length differs",
            index,
            _semantic_event(reference.events[index]) if index < len(reference.events) else None,
            _semantic_event(candidate.events[index]) if index < len(candidate.events) else None,
        )
    return TraceComparison("accepted", "all declared guest observations agree")


def compare_observation_traces(reference_path: Path, candidate_path: Path) -> TraceComparison:
    """Return an explicit accepted/counterexample/unknown/unsupported decision."""

    try:
        reference = load_observation_trace(reference_path)
        candidate = load_observation_trace(candidate_path)
    except UnsupportedTraceEvent as exc:
        return TraceComparison("unsupported", str(exc))
    except TraceFormatError as exc:
        return TraceComparison("unknown", str(exc))
    return _compare_loaded(reference, candidate)

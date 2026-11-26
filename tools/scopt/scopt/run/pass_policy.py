"""Request passes explicitly in replay and ablation drivers, and check what ran.

Seven registered passes run only on request (``DEFAULT_DISABLED_PASSES``). Naming a
pipeline that contains one does not request it unless the pipeline entry sets
``enabled = true``, so a driver that means to measure such a stage passes ``--enable``
for it (redundant, and harmless, for an opted-in entry) and then confirms, from the
receipts, that the stage was not skipped by policy.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..manager import DEFAULT_DISABLED_PASSES, R
from ..passes.base import B3Reason, Outcome, Reason

POLICY_DISABLED = "policy_disabled"   # --disable / --only, or a default-disabled pass not requested
UNSUPPORTED = "unsupported"           # the stage could not run in this configuration
MISSING = "missing"                   # a required input, analysis, or executable is unavailable
FAILED = "failed"                     # the pass or its proposal failed
INCOMPLETE = "incomplete"             # budget, indeterminate outcome, or incomplete receipt
NO_CHANGE = "no_change"               # it ran and found nothing to do
EDITED = "edited"                     # it ran and produced edits

_POLICY_REASONS = frozenset({R.DISABLED_BY_DEFAULT, R.ABLATION_DISABLED})
_MISSING_REASONS = frozenset({Reason.MISSING_ANALYSIS, Reason.MISSING_PROFILE,
                              "missing_steady_profile", "missing_hotness", "native_bridge_unavailable"})


class PolicyMismatch(RuntimeError):
    """A stage the driver meant to exercise was skipped by policy or could not run."""


def policy_arguments(names: Sequence[str], enabled: Iterable[str]) -> list[str]:
    """``scopt optimize`` options that run exactly ``enabled`` out of the pipeline's ``names``."""
    wanted = set(enabled)
    unknown = sorted(wanted - set(names))
    if unknown:
        raise ValueError(f"not in the pipeline: {unknown}")
    args: list[str] = []
    for name in names:
        if name not in wanted:
            args += ["--disable", name]
        elif name in DEFAULT_DISABLED_PASSES:
            args += ["--enable", name]
    return args


def stage_status(receipt: Mapping[str, Any]) -> str:
    reason = receipt.get("reason")
    outcome = receipt.get("outcome")
    if outcome == Outcome.FAILED.value:
        return FAILED
    if reason in _POLICY_REASONS:
        return POLICY_DISABLED
    if reason in _MISSING_REASONS:
        return MISSING
    count = receipt.get("n_edits")
    if type(count) is not int or count < 0:
        return INCOMPLETE
    if outcome == Outcome.CANDIDATE.value:
        return EDITED if count else NO_CHANGE
    if count:
        return INCOMPLETE
    if outcome == Outcome.NO_CHANGE.value:
        return NO_CHANGE
    if outcome == Outcome.SKIPPED_UNSUPPORTED.value:
        # B3 uses a skipped outcome for the leading site refusal after a scan.
        # Older receipts also expose the common target_not_profiled case by name.
        if reason == B3Reason.TARGET_NOT_PROFILED or receipt.get("stats", {}).get("no_applicable_sites"):
            return NO_CHANGE
        return UNSUPPORTED
    return INCOMPLETE


def effective_configuration(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One row per pipeline stage of an ``optimize --json`` result: what actually happened."""
    return [{"pass": r["pass"], "status": stage_status(r), "outcome": r.get("outcome"),
             "reason": r.get("reason") or "", "n_edits": r.get("n_edits")}
            for r in result.get("passes", ())]


def check_requested(result: Mapping[str, Any], requested: Iterable[str],
                    *, stages: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """The effective configuration; raises when a requested stage did not get to run.

    A stage that ran and changed nothing is a legitimate outcome and is not an error.
    """
    rows = effective_configuration(result)
    names = [row["pass"] for row in rows]
    wanted = set(requested)
    problems = [f"{name}: not in the result" for name in sorted(wanted - set(names))]
    if stages is not None:
        # A disabled native prefix is omitted entirely by the optimizer.
        # Every requested stage must appear, including repeated stages in order.
        expected = [name for name in stages if name in wanted or name in names]
        if names != expected:
            problems.append(f"expected pipeline stages {expected!r}, received {names!r}")
    for row in rows:
        name, status = row["pass"], row["status"]
        if name in wanted and status not in (NO_CHANGE, EDITED):
            problems.append(f"{name}: {status} ({row['reason']})")
        elif stages is not None and name not in wanted and status != POLICY_DISABLED:
            problems.append(f"{name}: expected policy_disabled, received {status}")
    if problems:
        raise PolicyMismatch("requested stages did not run: " + "; ".join(problems))
    return rows

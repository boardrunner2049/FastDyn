"""Request passes explicitly in replay and ablation drivers, and check what ran.

Seven registered passes run only on request (``DEFAULT_DISABLED_PASSES``). Naming a
pipeline that contains one does not request it, so a driver that means to measure
such a stage has to pass ``--enable`` for it and then confirm, from the receipts,
that the stage was not skipped by policy.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..manager import DEFAULT_DISABLED_PASSES, R

POLICY_DISABLED = "policy_disabled"   # --disable / --only, or a default-disabled pass not requested
UNSUPPORTED = "unsupported"           # the stage could not run in this configuration
NO_CHANGE = "no_change"               # it ran and found nothing to do
EDITED = "edited"                     # it ran and produced edits

_POLICY_REASONS = frozenset({R.DISABLED_BY_DEFAULT, R.ABLATION_DISABLED})
_UNSUPPORTED_REASONS = frozenset({R.PASS_NOT_REGISTERED, R.PASS_VERSION_UNAVAILABLE, R.INVALID_PARAMS,
                                  R.DOMAIN, R.REGION_SCOPE})


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
    reason = receipt.get("reason") or ""
    if reason in _POLICY_REASONS:
        return POLICY_DISABLED
    if reason in _UNSUPPORTED_REASONS:
        return UNSUPPORTED
    return EDITED if receipt.get("n_edits") else NO_CHANGE


def effective_configuration(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One row per pipeline stage of an ``optimize --json`` result: what actually happened."""
    return [{"pass": r["pass"], "status": stage_status(r), "reason": r.get("reason") or "",
             "n_edits": r.get("n_edits", 0)} for r in result.get("passes", ())]


def check_requested(result: Mapping[str, Any], requested: Iterable[str]) -> list[dict[str, Any]]:
    """The effective configuration; raises when a requested stage did not get to run.

    A stage that ran and changed nothing is a legitimate outcome and is not an error.
    """
    rows = effective_configuration(result)
    seen = {row["pass"]: row for row in rows}
    wanted = list(requested)
    problems = [f"{name}: not in the result" for name in wanted if name not in seen]
    problems += [f"{name}: {seen[name]['status']} ({seen[name]['reason']})" for name in wanted
                 if name in seen and seen[name]["status"] in (POLICY_DISABLED, UNSUPPORTED)]
    if problems:
        raise PolicyMismatch("requested stages did not run: " + "; ".join(problems))
    return rows

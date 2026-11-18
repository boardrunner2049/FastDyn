"""Target-generated-code cost gates for firmware preflight candidates."""

from __future__ import annotations

from typing import Any, Mapping

from .sdk import Decision


def evaluate_generated_host_inventory(
    report: Mapping[str, Any],
    *,
    minimum_operation_reduction: int = 1,
    minimum_host_byte_reduction: int = 1,
) -> Decision:
    """Apply a conservative TCG/host-block prefilter.

    This gate can reject a candidate that produces no smaller translated block;
    passing it never establishes runtime profitability.  Full fixed-work timing
    remains mandatory because generated block size does not model dispatch,
    cache, device, or whole-harness costs.
    """

    try:
        reference = report["reference"]
        candidate = report["candidate"]
        reference_operations = int(reference["optimized_tcg_operation_count"])
        candidate_operations = int(candidate["optimized_tcg_operation_count"])
        reference_bytes = int(reference["host_output_bytes"])
        candidate_bytes = int(candidate["host_output_bytes"])
    except (KeyError, TypeError, ValueError):
        return Decision("unknown", "generated_host_cost_inventory_incomplete")
    if min(
        reference_operations, candidate_operations, reference_bytes, candidate_bytes
    ) < 0:
        return Decision("unknown", "generated_host_cost_inventory_invalid")
    operation_reduction = reference_operations - candidate_operations
    byte_reduction = reference_bytes - candidate_bytes
    evidence = {
        "reference_optimized_tcg_operations": reference_operations,
        "candidate_optimized_tcg_operations": candidate_operations,
        "optimized_tcg_operation_reduction": operation_reduction,
        "reference_host_output_bytes": reference_bytes,
        "candidate_host_output_bytes": candidate_bytes,
        "host_output_byte_reduction": byte_reduction,
        "minimum_operation_reduction": minimum_operation_reduction,
        "minimum_host_byte_reduction": minimum_host_byte_reduction,
        "full_harness_still_required": True,
    }
    pareto_nonworse = operation_reduction >= 0 and byte_reduction >= 0
    materially_smaller = (
        operation_reduction >= minimum_operation_reduction
        or byte_reduction >= minimum_host_byte_reduction
    )
    if not (pareto_nonworse and materially_smaller):
        return Decision(
            "not_profitable", "generated_host_cost_not_pareto_improved", evidence
        )
    return Decision(
        "accepted", "generated_host_cost_reduced_requires_full_harness", evidence
    )

from fastdyn.preflight import evaluate_generated_host_inventory


def _inventory(reference_ops: int, candidate_ops: int, reference_bytes: int, candidate_bytes: int):
    return {
        "reference": {
            "optimized_tcg_operation_count": reference_ops,
            "host_output_bytes": reference_bytes,
        },
        "candidate": {
            "optimized_tcg_operation_count": candidate_ops,
            "host_output_bytes": candidate_bytes,
        },
    }


def test_generated_host_cost_rejects_an_identical_translation_inventory():
    decision = evaluate_generated_host_inventory(_inventory(15, 15, 100, 100))

    assert decision.status == "not_profitable"
    assert decision.reason_code == "generated_host_cost_not_pareto_improved"
    assert decision.evidence["full_harness_still_required"] is True


def test_generated_host_cost_admits_a_pareto_reduction_only_as_a_prefilter():
    decision = evaluate_generated_host_inventory(_inventory(24, 19, 320, 224))

    assert decision.status == "accepted"
    assert decision.reason_code == "generated_host_cost_reduced_requires_full_harness"
    assert decision.evidence["optimized_tcg_operation_reduction"] == 5
    assert decision.evidence["host_output_byte_reduction"] == 96
    assert decision.evidence["full_harness_still_required"] is True


def test_generated_host_cost_rejects_a_non_pareto_tradeoff():
    decision = evaluate_generated_host_inventory(_inventory(24, 18, 320, 336))

    assert decision.status == "not_profitable"


def test_generated_host_cost_keeps_incomplete_inventory_unknown():
    decision = evaluate_generated_host_inventory({"reference": {}, "candidate": {}})

    assert decision.status == "unknown"
    assert decision.reason_code == "generated_host_cost_inventory_incomplete"

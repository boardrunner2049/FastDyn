from scripts.benchmark_passes import comparison_effect


def test_byte_identical_comparison_keeps_measured_ratios_without_attribution():
    samples = [
        {"input": 0, "ns": {"before": 12, "after": 10}},
        {"input": 1, "ns": {"before": 24, "after": 20}},
    ]
    effect = comparison_effect(samples, {"before": "same", "after": "same"}, "before", "after")
    assert effect["byte_identical_sc"]
    assert effect["median"] == 1.2
    assert effect["median_by_input"] == {0: 1.2, 1: 1.2}
    assert effect["paired_95pct_bootstrap"] == [1.2, 1.2]
    assert effect["pairs"] == 2
    assert effect["attribution"] == {
        "status": "not_attributable",
        "reason": "byte_identical_sc",
        "speed_ratio": None,
        "paired_95pct_bootstrap": None,
    }


def test_different_cache_comparison_exposes_observed_ratio_and_interval():
    samples = [{"input": 0, "ns": {"before": 10, "after": 12}}]
    effect = comparison_effect(samples, {"before": "old", "after": "new"}, "before", "after")
    assert not effect["byte_identical_sc"]
    assert effect["attribution"] == {
        "status": "eligible",
        "reason": "different_sc_bytes",
        "speed_ratio": effect["median"],
        "paired_95pct_bootstrap": effect["paired_95pct_bootstrap"],
    }

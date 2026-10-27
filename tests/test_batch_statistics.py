"""Independent statistical expectations, including missing and failed runs."""
from math import sqrt
import pytest
from experiments.analysis_stats import describe, wilson95
from experiments.batch import aggregate


def test_descriptive_statistics_and_linear_percentile():
    result = describe([20, None, 0, 10])
    assert result == dict(samples=3, mean=10, stddev=10, median=10, p95=19, min=0, max=20)


@pytest.mark.parametrize("values,n,stddev", [([], 0, None), ([None], 0, None),
                                           ([7], 1, None), ([7, 7], 2, 0)])
def test_small_samples_are_not_zero_variance(values, n, stddev):
    result = describe(values)
    assert result["samples"] == n and result["stddev"] == stddev
    if n == 0:
        assert all(v is None for k, v in result.items() if k != "samples")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "10"])
def test_invalid_numeric_measurement_rejected(value):
    with pytest.raises(ValueError):
        describe([0, value])


def test_wilson_known_interval_and_extremes():
    assert wilson95(5, 10) == pytest.approx((0.2365930905, 0.7634069095))
    assert wilson95(0, 10) == pytest.approx((0, 0.2775327999))
    assert wilson95(10, 10) == pytest.approx((0.7224672001, 1))
    assert wilson95(0, 0) == (None, None)
    assert wilson95(1, 1)[0] < 0.3  # one success is not certainty


@pytest.mark.parametrize("counts", [(-1, 2), (3, 2), (0, -1), (True, 1)])
def test_invalid_rate_counts_rejected(counts):
    with pytest.raises(ValueError):
        wilson95(*counts)


def rows():
    base = dict(task_id="toy", scenario_id="clean", condition="clean", synthetic=True,
                comparison_group_id="toy", workflow={"model": "fixture"})
    values = [(True, 10, 0), (False, 20, None), (None, None, 6)]
    return [dict(config={**base, "repeat_index": i}, error="timeout" if i == 1 else None,
                 summary=dict(final_agentdojo_utility=success, final_strict_success=success,
                              total_latency_ms=latency, total_cost={"input_tokens": tokens}))
            for i, (success, latency, tokens) in enumerate(values)]


def test_batch_variation_has_per_metric_denominators_and_preserves_failures(tmp_path):
    report = aggregate(rows(), tmp_path)[0]
    assert report["total"] == 3 and report["execution_failures"] == 1
    assert report["completion_rate_samples"] == 2
    assert report["completion_rate"] == 0.5
    assert report["completion_rate_ci95_lower"] == pytest.approx(wilson95(1, 2)[0])
    assert report["latency_samples"] == report["latency_ms_samples"] == 2
    assert report["stddev_latency_ms"] == pytest.approx(sqrt(50))
    assert report["median_latency_ms"] == 15 and report["p95_latency_ms"] == 19.5
    assert report["input_tokens_samples"] == 2 and report["mean_input_tokens"] == 3
    assert report["stddev_input_tokens"] == pytest.approx(sqrt(18))
    assert report["propagation_rate_ci95_lower"] is None
    assert report["recovery_success_rate_samples"] == 0


def test_duplicate_repeats_flagged_not_used_for_confidence_claim(tmp_path):
    data = rows()
    data[1]["config"]["repeat_index"] = 0
    report = aggregate(data, tmp_path)[0]
    assert report["total"] == 3 and report["duplicate_repeat_rows"] == 1
    assert report["completion_rate"] == 0.5  # observations are retained
    assert report["completion_rate_ci95_lower"] is None
    assert report["rate_ci_status"] == "duplicate_repeat_indices"

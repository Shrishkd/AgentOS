from benchmark import distribution


def test_nearest_rank_uses_full_population_without_mutating_samples():
    values = list(range(100, 0, -1))
    assert distribution(values) == {"p50": 50, "p95": 95, "p99": 99}
    assert values[0] == 100


def test_small_cohort_reports_observed_tail_without_extrapolation():
    assert distribution([1] * 9 + [200]) == {"p50": 1, "p95": 200}

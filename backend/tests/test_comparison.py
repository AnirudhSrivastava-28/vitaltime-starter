from copy import deepcopy

from app.comparison.simulation import ComparisonConfig, run_comparison


def test_comparison_is_deterministic_and_paired():
    config = ComparisonConfig(trials=2, events_per_day=20, seed=41)

    first = run_comparison(config)
    second = run_comparison(config)

    assert first == second
    assert first["checks"]["paired_event_streams"] is True
    assert set(first["policies"]) == {"vitaltime", "call_light_a", "call_light_b"}


def test_comparison_does_not_mutate_config_or_live_state():
    assumptions = {"notice_delay_mean_minutes": 1.25}
    original = deepcopy(assumptions)

    result = run_comparison(
        ComparisonConfig(trials=1, events_per_day=10, seed=9, assumptions=assumptions)
    )

    assert assumptions == original
    assert result["checks"]["state_isolated_from_live_store"] is True
    assert result["checks"]["non_negative_durations"] is True


def test_comparison_reports_metrics_and_assumptions():
    result = run_comparison(ComparisonConfig(trials=1, events_per_day=30, seed=12))

    assert result["config"]["call_light_assumptions"]["recognition_delay_minutes"] == 1.0
    assert "fatigue" in result["disclosure"]
    for policy in result["policies"].values():
        assert "overall" in policy
        assert "load_distribution" in policy
        assert "response_time_minutes" in policy["overall"]


def test_degenerate_call_light_is_the_fairness_control():
    result = run_comparison(
        ComparisonConfig(
            trials=2,
            events_per_day=20,
            seed=22,
            degenerate_call_light=True,
        )
    )

    assert result["policies"]["call_light_a"] == result["policies"]["vitaltime"]
    assert result["policies"]["call_light_b"] == result["policies"]["vitaltime"]

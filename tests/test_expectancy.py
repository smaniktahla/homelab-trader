"""
Pure unit tests for shared/expectancy.py -- no DB, mirrors
tests/test_lifecycle_performance.py's style (plain tuples/dicts, direct
float == comparisons).
"""

import expectancy as exp


def test_holding_period_bucket_boundaries():
    assert exp.holding_period_bucket(0.5) == "<1d"
    assert exp.holding_period_bucket(1.0) == "1-5d"
    assert exp.holding_period_bucket(4.99) == "1-5d"
    assert exp.holding_period_bucket(5.0) == "5-20d"
    assert exp.holding_period_bucket(19.99) == "5-20d"
    assert exp.holding_period_bucket(20.0) == "20d+"
    assert exp.holding_period_bucket(500.0) == "20d+"
    assert exp.holding_period_bucket(None) is None


def test_sample_quality_tiers():
    rows_insufficient = [("A", 10.0, None)] * 4
    rows_preliminary = [("A", 10.0, None)] * 5 + [("A", -5.0, None)] * 14
    rows_established = [("A", 10.0, None)] * 20

    assert exp.bucket_stats(rows_insufficient)["A"]["sample_quality"] == "insufficient"
    assert exp.bucket_stats(rows_preliminary)["A"]["sample_quality"] == "preliminary"
    assert exp.bucket_stats(rows_established)["A"]["sample_quality"] == "established"


def test_win_loss_and_profit_factor():
    rows = [
        ("A", 100.0, None),
        ("A", 100.0, None),
        ("A", -50.0, None),
    ]
    s = exp.bucket_stats(rows)["A"]
    assert s["n"] == 3
    assert s["win_rate"] == round(100 * 2 / 3, 1)
    assert s["avg_win"] == 100.0
    assert s["avg_loss"] == -50.0
    assert s["profit_factor"] == round(200.0 / 50.0, 2)
    assert s["expectancy_dollars"] == round((100 + 100 - 50) / 3, 2)


def test_profit_factor_none_when_no_losses():
    rows = [("A", 100.0, None), ("A", 50.0, None)]
    s = exp.bucket_stats(rows)["A"]
    assert s["profit_factor"] is None
    assert s["avg_loss"] is None


def test_avg_win_none_when_no_wins():
    rows = [("A", -10.0, None), ("A", -20.0, None)]
    s = exp.bucket_stats(rows)["A"]
    assert s["avg_win"] is None
    assert s["profit_factor"] == 0.0   # zero gross profit over real gross loss -- a real, computable 0.0, not undefined
    assert s["expectancy_dollars"] == -15.0


def test_r_expectancy_only_over_non_null_subset():
    rows = [
        ("A", 100.0, 2.0),
        ("A", -50.0, None),   # no linked stop price -- realized_r unknown
        ("A", 200.0, 1.5),
    ]
    s = exp.bucket_stats(rows)["A"]
    assert s["n"] == 3
    assert s["n_with_r"] == 2
    assert s["expectancy_r"] == round((2.0 + 1.5) / 2, 3)
    # dollar expectancy still uses the FULL sample, unaffected by missing R
    assert s["expectancy_dollars"] == round((100 - 50 + 200) / 3, 2)


def test_expectancy_r_none_when_no_lifecycle_has_r():
    rows = [("A", 100.0, None), ("A", -50.0, None)]
    s = exp.bucket_stats(rows)["A"]
    assert s["n_with_r"] == 0
    assert s["expectancy_r"] is None


def test_multiple_buckets_are_independent():
    rows = [
        ("mean_reversion", 100.0, None),
        ("congress_shreve_hern", -300.0, None),
    ]
    stats = exp.bucket_stats(rows)
    assert stats["mean_reversion"]["n"] == 1
    assert stats["congress_shreve_hern"]["n"] == 1
    assert stats["mean_reversion"]["expectancy_dollars"] == 100.0
    assert stats["congress_shreve_hern"]["expectancy_dollars"] == -300.0

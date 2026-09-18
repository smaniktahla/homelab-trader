"""
PR 17-18, Hypothesis-Driven Trading Architecture epic. Confirms
strategy_registry.py's overlay functions match direct calls to the
underlying indicator primitives -- the same as-of-bar-t-safe
per-bar-loop pattern api/main.py's existing GET /api/prices endpoint
already uses in production for Bollinger bands, or (for SuperTrend, PR 18)
a direct call to its own single-pass band recursion.
"""

from datetime import datetime, timedelta, timezone

from backtest_engine import Bar
from market_structure import ema
from signals import compute_bollinger
from strategy_registry import (
    STRATEGIES,
    _bollinger_overlays,
    _daily_8ema_pullback_overlays,
    _ema_crossover_overlays,
    _supertrend_overlays,
)
from supertrend_strategy import _supertrend_bands

SYMBOL = "TEST"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bars(closes):
    return [
        Bar(symbol=SYMBOL, ts=START + timedelta(days=i), open=c, high=c, low=c, close=c, volume=1000)
        for i, c in enumerate(closes)
    ]


def test_registry_has_all_pr16_18_strategies():
    assert set(STRATEGIES.keys()) == {
        "bollinger_breakout_continuation",
        "ema_crossover_trend",
        "supertrend",
        "daily_8ema_momentum_retest",
    }
    for spec in STRATEGIES.values():
        assert callable(spec["make_strategy"])
        assert callable(spec["compute_overlays"])
        assert isinstance(spec["display_name"], str) and spec["display_name"]


def test_bollinger_overlays_match_direct_compute_bollinger_calls():
    closes = [100.0 + i for i in range(30)]
    bars = _bars(closes)
    overlays = _bollinger_overlays(bars, period=20, num_std=2.0)
    names = {o["name"] for o in overlays}
    assert names == {"bb_upper", "bb_middle", "bb_lower"}

    by_name = {o["name"]: o["values"] for o in overlays}
    for i in range(len(closes)):
        expected_upper, expected_middle, expected_lower, _ = compute_bollinger(closes[: i + 1], 20, 2.0)
        assert by_name["bb_upper"][i]["value"] == expected_upper
        assert by_name["bb_middle"][i]["value"] == expected_middle
        assert by_name["bb_lower"][i]["value"] == expected_lower
        assert by_name["bb_upper"][i]["ts"] == bars[i].ts.isoformat()


def test_bollinger_overlays_handle_too_short_series_without_raising():
    bars = _bars([100.0, 101.0, 102.0])  # far short of period=20
    overlays = _bollinger_overlays(bars, period=20, num_std=2.0)
    for o in overlays:
        assert all(v["value"] is None for v in o["values"])


def test_ema_overlays_match_direct_ema_calls():
    closes = [100.0 + i for i in range(30)]
    bars = _bars(closes)
    overlays = _ema_crossover_overlays(bars, fast_period=3, slow_period=5)
    names = {o["name"] for o in overlays}
    assert names == {"ema_3", "ema_5"}

    by_name = {o["name"]: o["values"] for o in overlays}
    for i in range(len(closes)):
        expected_fast = ema(closes[: i + 1], 3)
        expected_slow = ema(closes[: i + 1], 5)
        assert by_name["ema_3"][i]["value"] == expected_fast
        assert by_name["ema_5"][i]["value"] == expected_slow


def test_ema_overlays_handle_too_short_series_without_raising():
    bars = _bars([100.0, 101.0])  # far short of slow_period=5
    overlays = _ema_crossover_overlays(bars, fast_period=3, slow_period=5)
    for o in overlays:
        assert all(v["value"] is None for v in o["values"])


def test_supertrend_overlays_match_direct_supertrend_bands_call():
    closes = [100.0] * 5 + [120, 140, 160, 180, 200, 190, 170, 150, 130, 110, 90]
    bars = _bars(closes)
    overlays = _supertrend_overlays(bars, period=2, multiplier=1.0)
    assert {o["name"] for o in overlays} == {"supertrend"}

    trend, final_upper, final_lower = _supertrend_bands(closes, closes, closes, 2, 1.0)
    expected = [
        final_lower[i] if trend[i] == 1 else final_upper[i] if trend[i] == -1 else None
        for i in range(len(closes))
    ]
    values = overlays[0]["values"]
    assert [v["value"] for v in values] == expected
    assert values[0]["ts"] == bars[0].ts.isoformat()


def test_supertrend_overlays_handle_too_short_series_without_raising():
    bars = _bars([100.0, 101.0, 102.0])  # far short of period=10 default
    overlays = _supertrend_overlays(bars)
    assert all(v["value"] is None for v in overlays[0]["values"])


def test_daily_8ema_pullback_overlays_match_direct_ema_calls():
    closes = [100.0 + i for i in range(30)]
    bars = _bars(closes)
    overlays = _daily_8ema_pullback_overlays(bars, ema_period=8)
    assert {o["name"] for o in overlays} == {"ema_8"}

    by_name = {o["name"]: o["values"] for o in overlays}
    for i in range(len(closes)):
        expected = ema(closes[: i + 1], 8)
        assert by_name["ema_8"][i]["value"] == expected


def test_daily_8ema_pullback_overlays_handle_too_short_series_without_raising():
    bars = _bars([100.0, 101.0])  # far short of ema_period=8 default
    overlays = _daily_8ema_pullback_overlays(bars)
    assert all(v["value"] is None for v in overlays[0]["values"])

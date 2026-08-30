"""
PR 17, Hypothesis-Driven Trading Architecture epic. Confirms
strategy_registry.py's overlay functions match direct calls to the
underlying indicator primitives per bar -- the same as-of-bar-t-safe
per-bar-loop pattern api/main.py's existing GET /api/prices endpoint
already uses in production for Bollinger bands.
"""

from datetime import datetime, timedelta, timezone

from backtest_engine import Bar
from market_structure import ema
from signals import compute_bollinger
from strategy_registry import STRATEGIES, _bollinger_overlays, _ema_crossover_overlays

SYMBOL = "TEST"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bars(closes):
    return [
        Bar(symbol=SYMBOL, ts=START + timedelta(days=i), open=c, high=c, low=c, close=c, volume=1000)
        for i, c in enumerate(closes)
    ]


def test_registry_has_both_pr16_strategies():
    assert set(STRATEGIES.keys()) == {"bollinger_breakout_continuation", "ema_crossover_trend"}
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

"""
PR 18, Hypothesis-Driven Trading Architecture epic. Runs full
run_backtest() cycles (shared/backtest_engine.py, PR 15) over a fixture
sequence whose exact SuperTrend band/trend values were independently
computed by hand (period=2, multiplier=1, so the arithmetic is tractable)
before writing these assertions -- see shared/supertrend_strategy.py's
module docstring for why this indicator, unlike Bollinger/EMA, has to
recompute a recursive band series rather than a pure windowed function.
"""

from datetime import datetime, timedelta, timezone

from backtest_engine import Bar, run_backtest
from supertrend_strategy import (
    DEFAULT_MULTIPLIER,
    DEFAULT_PERIOD,
    _supertrend_series,
    make_supertrend_strategy,
)

SYMBOL = "TEST"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
PERIOD, MULTIPLIER = 2, 1.0

# Flat run to let ATR settle at 0, then a strong uptrend, then a reversal --
# every value below (final bands, trend flips) was hand-derived from the
# module's own recursion formula before being used as an assertion.
_CLOSES = [100.0] * 5 + [120, 140, 160, 180, 200] + [190, 170, 150, 130, 110, 90]
_BUY_INDEX = 5
_SELL_INDEX = 11


def _bars(closes):
    # high == low == close: true range collapses to |close[i] - close[i-1]|,
    # which is what the hand derivation in the module docstring assumes.
    return [
        Bar(symbol=SYMBOL, ts=START + timedelta(days=i), open=c, high=c, low=c, close=c, volume=1000)
        for i, c in enumerate(closes)
    ]


def test_hand_derived_trend_series_matches_fixture():
    # Sanity-check the fixture's own claim before trusting downstream
    # assertions built on it.
    closes = _CLOSES
    trend = _supertrend_series(closes, closes, closes, PERIOD, MULTIPLIER)
    assert trend[_BUY_INDEX - 1] is None
    assert trend[_BUY_INDEX] == 1
    assert trend[_SELL_INDEX - 1] == 1
    assert trend[_SELL_INDEX] == -1


def test_defaults_require_enough_bars_before_any_decision():
    strategy = make_supertrend_strategy()  # period=10, multiplier=3.0
    bars = _bars([100.0] * (DEFAULT_PERIOD + 1))  # one short of period+2
    assert strategy(bars) is None


def test_uptrend_flip_fires_exactly_once_at_the_transition_bar():
    strategy = make_supertrend_strategy(period=PERIOD, multiplier=MULTIPLIER)
    bars = _bars(_CLOSES[: _BUY_INDEX + 1])  # truncate right after the flip
    result = run_backtest(bars, strategy, execution_timing="next_bar_open")
    buy_signals = [s for s in result.signals if s.side == "buy"]
    assert len(buy_signals) == 1
    assert buy_signals[0].bar_ts == bars[_BUY_INDEX].ts
    assert buy_signals[0].actionable is True


def test_persistent_uptrend_does_not_refire_buy_signal():
    strategy = make_supertrend_strategy(period=PERIOD, multiplier=MULTIPLIER)
    bars = _bars(_CLOSES[:_SELL_INDEX])  # everything through just before the downflip
    result = run_backtest(bars, strategy, execution_timing="next_bar_open")
    buy_signals = [s for s in result.signals if s.side == "buy"]
    # Exactly one buy across the entire sustained uptrend, including the
    # dip at index 10 that never breaches the trailing lower band.
    assert len(buy_signals) == 1
    assert buy_signals[0].bar_ts == bars[_BUY_INDEX].ts


def test_downtrend_flip_fires_exactly_once_at_the_transition_bar():
    strategy = make_supertrend_strategy(period=PERIOD, multiplier=MULTIPLIER)
    bars = _bars(_CLOSES)
    result = run_backtest(bars, strategy, execution_timing="next_bar_open")
    sell_signals = [s for s in result.signals if s.side == "sell"]
    assert len(sell_signals) == 1
    assert sell_signals[0].bar_ts == bars[_SELL_INDEX].ts


def test_full_round_trip_produces_exactly_two_signals_and_one_closed_trade():
    strategy = make_supertrend_strategy(period=PERIOD, multiplier=MULTIPLIER)
    bars = _bars(_CLOSES)
    result = run_backtest(bars, strategy, execution_timing="next_bar_open")

    assert len(result.signals) == 2  # one buy, one sell -- no refires anywhere
    assert [s.side for s in result.signals] == ["buy", "sell"]

    closed = [t for t in result.trades if t.status == "closed"]
    assert len(closed) == 1
    trade = closed[0]
    assert trade.entry_execution_ts == bars[_BUY_INDEX + 1].ts
    assert trade.exit_execution_ts == bars[_SELL_INDEX + 1].ts
    assert trade.entry_price == 140.0  # bars[6].open (idx5 buy -> fill at idx6)
    assert trade.exit_price == 150.0   # bars[12].open (idx11 sell -> fill at idx12)
    assert trade.gross_pnl == trade.net_pnl == 10.0

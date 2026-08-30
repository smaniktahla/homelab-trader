"""
PR 16, Hypothesis-Driven Trading Architecture epic. Runs full
run_backtest() cycles (shared/backtest_engine.py, PR 15) over a fixture
sequence whose exact EMA crossover behavior was independently computed
(via shared/market_structure.py::ema() directly, not hardcoded guesses)
before writing these assertions -- see the module docstring in
shared/ema_crossover_strategy.py for why the <=/>= vs. >/< asymmetry
matters.

The fixture below (fast_period=3, slow_period=5) covers, in one sequence,
every boundary case the original spec calls out explicitly: fast==slow at
the transition, fast crosses above, fast remains above for many bars
(must NOT refire), and fast crosses below.
"""

from datetime import datetime, timedelta, timezone

from backtest_engine import Bar, run_backtest
from ema_crossover_strategy import DEFAULT_FAST_PERIOD, DEFAULT_SLOW_PERIOD, make_ema_crossover_strategy
from market_structure import ema

SYMBOL = "TEST"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
FAST, SLOW = 3, 5

# Verified independently via market_structure.ema() before writing
# assertions: fast crosses above at index 8 (from an EXACT fast==slow==100
# equality at index 7), stays above through index 19 (no refire), crosses
# below at index 20, stays below through the end (no refire).
_CLOSES = (
    [100.0] * 8
    + [110, 120, 130, 140, 150, 160, 170, 180, 190, 200]
    + [190, 170, 150, 130, 110, 90, 70, 50, 30, 10]
)
_BUY_INDEX = 8
_SELL_INDEX = 20


def _bars(closes):
    return [
        Bar(symbol=SYMBOL, ts=START + timedelta(days=i), open=c, high=c, low=c, close=c, volume=1000)
        for i, c in enumerate(closes)
    ]


def test_equality_boundary_confirmed_in_fixture():
    # Sanity-check the fixture's own claim before trusting downstream
    # assertions built on it.
    fast_prev = ema(_CLOSES[:_BUY_INDEX], FAST)
    slow_prev = ema(_CLOSES[:_BUY_INDEX], SLOW)
    assert fast_prev == slow_prev == 100.0


def test_defaults_require_enough_bars_before_any_decision():
    strategy = make_ema_crossover_strategy()  # fast=20, slow=21
    bars = _bars([100.0] * DEFAULT_SLOW_PERIOD)  # exactly slow_period bars, one short of slow_period+1
    assert strategy(bars) is None


def test_cross_above_fires_exactly_once_at_the_transition_bar():
    strategy = make_ema_crossover_strategy(fast_period=FAST, slow_period=SLOW)
    bars = _bars(_CLOSES[: _BUY_INDEX + 1])  # truncate right after the cross
    result = run_backtest(bars, strategy, execution_timing="next_bar_open")
    buy_signals = [s for s in result.signals if s.side == "buy"]
    assert len(buy_signals) == 1
    assert buy_signals[0].bar_ts == bars[_BUY_INDEX].ts
    assert buy_signals[0].actionable is True


def test_persistent_uptrend_does_not_refire_buy_signal():
    strategy = make_ema_crossover_strategy(fast_period=FAST, slow_period=SLOW)
    bars = _bars(_CLOSES[:_SELL_INDEX])  # everything up through just before the downcross
    result = run_backtest(bars, strategy, execution_timing="next_bar_open")
    buy_signals = [s for s in result.signals if s.side == "buy"]
    # Exactly one buy across the ENTIRE sustained uptrend (indices 8-19),
    # not one per bar the fast EMA remains above the slow EMA.
    assert len(buy_signals) == 1
    assert buy_signals[0].bar_ts == bars[_BUY_INDEX].ts


def test_cross_below_fires_exactly_once_at_the_transition_bar():
    strategy = make_ema_crossover_strategy(fast_period=FAST, slow_period=SLOW)
    bars = _bars(_CLOSES)
    result = run_backtest(bars, strategy, execution_timing="next_bar_open")
    sell_signals = [s for s in result.signals if s.side == "sell"]
    assert len(sell_signals) == 1
    assert sell_signals[0].bar_ts == bars[_SELL_INDEX].ts


def test_full_round_trip_produces_exactly_two_signals_and_one_closed_trade():
    strategy = make_ema_crossover_strategy(fast_period=FAST, slow_period=SLOW)
    bars = _bars(_CLOSES)
    result = run_backtest(bars, strategy, execution_timing="next_bar_open")

    assert len(result.signals) == 2  # one buy, one sell -- no refires anywhere
    assert [s.side for s in result.signals] == ["buy", "sell"]

    closed = [t for t in result.trades if t.status == "closed"]
    assert len(closed) == 1
    trade = closed[0]
    assert trade.entry_execution_ts == bars[_BUY_INDEX + 1].ts
    assert trade.exit_execution_ts == bars[_SELL_INDEX + 1].ts
    assert trade.entry_price == 120.0  # bars[9].open (idx8 buy -> fill at idx9)
    assert trade.exit_price == 130.0   # bars[21].open (idx20 sell -> fill at idx21)
    assert trade.gross_pnl == trade.net_pnl == 10.0

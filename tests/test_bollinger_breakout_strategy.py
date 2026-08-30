"""
PR 16, Hypothesis-Driven Trading Architecture epic. Runs full
run_backtest() cycles (shared/backtest_engine.py, PR 15) over constructed
fixture bar sequences -- not just isolated indicator math -- to prove the
Bollinger Breakout Continuation strategy behaves correctly end to end,
including the same lookahead-safety guarantee PR 15 already proved
generically.
"""

from datetime import datetime, timedelta, timezone

from backtest_engine import Bar, run_backtest
from bollinger_breakout_strategy import DEFAULT_NUM_STD, DEFAULT_PERIOD, make_bollinger_breakout_strategy
from signals import compute_bollinger

SYMBOL = "TEST"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bars(closes, opens=None):
    opens = opens or closes
    return [
        Bar(symbol=SYMBOL, ts=START + timedelta(days=i), open=o, high=max(o, c), low=min(o, c), close=c, volume=1000)
        for i, (o, c) in enumerate(zip(opens, closes))
    ]


def _flat_series(n, price=100.0):
    """A tight, unmoving series -- can never breach a 2-std band."""
    return [price] * n


def test_defaults_match_compute_bollinger_parameter_meaning():
    # Sanity: the strategy's defaults are the same lookback/std this test
    # module uses to independently verify breakout math, not a duplicated
    # or drifted formula.
    closes = _flat_series(DEFAULT_PERIOD) + [200.0]
    upper, sma, lower, std = compute_bollinger(closes, DEFAULT_PERIOD, DEFAULT_NUM_STD)
    strategy = make_bollinger_breakout_strategy()
    bars = _bars(closes)
    decision = strategy(bars)
    assert (closes[-1] > upper) == (decision == "buy")


def test_no_signal_while_price_stays_inside_bands():
    closes = _flat_series(30, price=100.0)
    strategy = make_bollinger_breakout_strategy()
    bars = _bars(closes)
    result = run_backtest(bars, strategy, execution_timing="next_bar_open")
    assert result.signals == []
    assert result.fills == []
    assert result.trades == []


def test_known_breakout_fires_exact_signal_and_fill_bar():
    # 20 flat bars to seed the band tightly around 100, then a clear
    # breakout on the day-21 bar (index 20), matching the spec's own
    # "day 3 breakout" example in shape (known-in-advance transition day).
    # opens are deliberately distinct from closes so the "fill uses the
    # NEXT bar's open, not the breakout bar's own close" assertion below
    # is a real check, not one where both prices coincidentally match. The
    # trailing bar's close (105) is deliberately back inside the bands so
    # it doesn't ALSO register as a (non-actionable) breakout signal --
    # this test is about the single transition bar, not repeat-signal
    # behavior (covered elsewhere).
    closes = _flat_series(DEFAULT_PERIOD) + [130.0, 105.0]
    opens = _flat_series(DEFAULT_PERIOD) + [128.0, 150.0]
    bars = _bars(closes, opens=opens)
    strategy = make_bollinger_breakout_strategy()
    result = run_backtest(bars, strategy, execution_timing="next_bar_open")

    breakout_bar = bars[DEFAULT_PERIOD]  # index 20, the first bar with close=130
    assert len(result.signals) == 1
    assert result.signals[0].bar_ts == breakout_bar.ts
    assert result.signals[0].side == "buy"
    assert result.signals[0].actionable is True

    # Fill must be on the NEXT bar's open (150), never the breakout bar's
    # own close (130).
    assert len(result.fills) == 1
    fill = result.fills[0]
    next_bar = bars[DEFAULT_PERIOD + 1]
    assert fill.execution_ts == next_bar.ts
    assert fill.price == next_bar.open == 150.0
    assert fill.price != breakout_bar.close


def test_full_round_trip_entry_and_lower_band_exit():
    # Flat seed, breakout up (entry), then a sharp drop below the lower
    # band (exit) -- one closed Trade. One extra trailing bar after the
    # drop so the exit signal (which fires on the drop bar) has a next
    # bar to fill on -- a signal on the very last bar is recorded but
    # never filled (PR 15's own tested behavior), so this fixture must
    # not end on the triggering bar itself.
    closes = _flat_series(DEFAULT_PERIOD) + [130.0, 130.0, 130.0, 130.0, 30.0, 30.0]
    bars = _bars(closes)
    strategy = make_bollinger_breakout_strategy()
    result = run_backtest(bars, strategy, execution_timing="next_bar_open")

    closed = [t for t in result.trades if t.status == "closed"]
    assert len(closed) == 1
    trade = closed[0]
    assert trade.entry_price == bars[DEFAULT_PERIOD + 1].open
    assert trade.exit_price is not None

    exit_signals = [s for s in result.signals if s.side == "sell"]
    assert len(exit_signals) >= 1
    assert exit_signals[0].actionable is True


def test_custom_period_and_std_are_honored():
    # A short period/tight std should breakout far sooner than defaults.
    closes = _flat_series(5) + [110.0]
    strategy = make_bollinger_breakout_strategy(period=5, num_std=1.0)
    bars = _bars(closes)
    result = run_backtest(bars, strategy, execution_timing="next_bar_open")
    assert len(result.signals) == 1
    assert result.signals[0].side == "buy"

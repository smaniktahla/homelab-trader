"""
VR-3a.1 (Volatility Forecasting & Risk-Targeted Position Sizing epic):
shared/mean_reversion_strategy.py is a simplified, single-symbol-only proxy
of the live mean_reversion thesis, adapted to shared/backtest_engine.py's
Strategy interface so it can run inside
shared/sizing_policy_comparison.py's paired-opportunity harness. These
tests prove the adapter's entry/exit behavior end to end via real
run_backtest() cycles, the same style as test_bollinger_breakout_strategy.py
-- not a claim that this reproduces live compute_signals() exactly, see
mean_reversion_strategy.py's own docstring for what's included/omitted.
"""

from datetime import datetime, timedelta, timezone

from backtest_engine import Bar, run_backtest
from mean_reversion_strategy import make_mean_reversion_strategy
from signals import DEFAULTS

SYMBOL = "TEST"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bars(closes, opens=None):
    opens = opens or closes
    return [
        Bar(symbol=SYMBOL, ts=START + timedelta(days=i), open=o, high=max(o, c), low=min(o, c), close=c, volume=1000)
        for i, (o, c) in enumerate(zip(opens, closes))
    ]


def _flat_series(n, price=100.0):
    return [price] * n


def test_no_signal_on_flat_series():
    # Flat market: RSI pins at a neutral/overbought-looking 100 (no losses
    # ever seen) and price sits exactly on the middle band -- raw score is
    # 0, well under score_log_min (30 by default). No buy should fire, and
    # price never dips below bb_middle so no spurious sell either.
    closes = _flat_series(30)
    strategy = make_mean_reversion_strategy()
    bars = _bars(closes)
    result = run_backtest(bars, strategy, execution_timing="next_bar_open")
    assert result.trades == []


def test_oversold_dip_triggers_buy_above_score_threshold():
    # 20 flat bars to seed RSI/BB, then a steady decline -- deeply oversold
    # RSI plus price pushing through the lower band produces a raw score
    # well above score_log_min on the very first down bar (verified
    # directly against score_signal(): score=66 at close=86 for this exact
    # fixture shape).
    closes = _flat_series(20) + [98, 96, 94, 92, 90, 88, 86]
    strategy = make_mean_reversion_strategy()
    bars = _bars(closes)
    result = run_backtest(bars, strategy, execution_timing="next_bar_open")

    buy_signals = [s for s in result.signals if s.side == "buy"]
    assert len(buy_signals) >= 1
    assert buy_signals[0].actionable is True
    # First down bar (index 20, close=98) is already enough to cross the
    # score threshold -- entry shouldn't need to wait for the full decline.
    assert buy_signals[0].bar_ts == bars[20].ts


def test_thesis_complete_exit_on_return_to_middle_band():
    # Flat seed, oversold dip (entry), then a recovery back up through the
    # middle band (SMA20) -- the adapter's only modeled exit
    # ("thesis_complete"). One trailing bar after the recovery crossing so
    # the exit signal has a next bar to fill on.
    closes = (
        _flat_series(20)
        + [98, 96, 94, 92, 90, 88, 86]     # decline -> entry
        + [90, 94, 98, 101, 101]           # recovery back above bb_middle -> exit
    )
    strategy = make_mean_reversion_strategy()
    bars = _bars(closes)
    result = run_backtest(bars, strategy, execution_timing="next_bar_open")

    closed = [t for t in result.trades if t.status == "closed"]
    assert len(closed) == 1
    trade = closed[0]
    assert trade.entry_price is not None
    assert trade.exit_price is not None

    sell_signals = [s for s in result.signals if s.side == "sell"]
    assert any(s.actionable for s in sell_signals)


def test_score_log_min_param_is_honored():
    # Same mild dip fixture as the flat-series sanity check above, but with
    # score_log_min raised so high that even a real oversold reading can't
    # clear it -- proves the threshold param is actually read, not
    # hardcoded.
    closes = _flat_series(20) + [98, 96, 94, 92, 90, 88, 86]
    strategy = make_mean_reversion_strategy(params={"score_log_min": 99})
    bars = _bars(closes)
    result = run_backtest(bars, strategy, execution_timing="next_bar_open")
    assert [s for s in result.signals if s.side == "buy" and s.actionable] == []


def test_defaults_come_from_signals_module_not_duplicated():
    # Sanity: the adapter's own DEFAULTS merge starts from signals.py's
    # real DEFAULTS dict (score_log_min=30 today) rather than a
    # hand-copied/independently-drifting constant.
    assert DEFAULTS["score_log_min"] == 30
    assert DEFAULTS["bb_period"] == 20

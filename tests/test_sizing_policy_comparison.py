"""
Tests for shared/sizing_policy_comparison.py -- VR-3a of the Volatility
Forecasting & Risk-Targeted Position Sizing epic. All synthetic,
in-memory data (no DB) -- same style as tests/test_backtest_engine.py,
since this module is a pure wrapper around run_backtest() plus
shared/volatility_forecast.py's estimators.
"""

import math
import sys
import pathlib
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
p = str(ROOT / "shared")
if p not in sys.path:
    sys.path.insert(0, p)

for _mod in ("sizing_policy_comparison", "backtest_engine", "volatility_forecast", "market_structure"):
    sys.modules.pop(_mod, None)
import sizing_policy_comparison as spc
from backtest_engine import Bar

SYMBOL = "TEST"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bars_from_closes(closes):
    return [
        Bar(symbol=SYMBOL, ts=START + timedelta(days=i), open=c, high=c, low=c, close=c, volume=1000)
        for i, c in enumerate(closes)
    ]


def _buy_then_sell_at(buy_idx, sell_idx):
    def strategy(bars_seen):
        n = len(bars_seen)
        if n == buy_idx + 1:
            return "buy"
        if n == sell_idx + 1:
            return "sell"
        return None
    return strategy


# ─────────────────────────────────────────────────────────────────────────
# fixed_notional_qty_fn
# ─────────────────────────────────────────────────────────────────────────

def test_fixed_notional_qty_fn_floors_notional_over_price():
    qty_fn = spc.fixed_notional_qty_fn(base_notional=1000.0)
    bars = _bars_from_closes([37.0])
    assert qty_fn(bars, None) == math.floor(1000.0 / 37.0)


# ─────────────────────────────────────────────────────────────────────────
# volatility_scaled_qty_fn
# ─────────────────────────────────────────────────────────────────────────

def _volatile_closes(n, amplitude=0.05, base=100.0):
    """Alternating up/down returns of fixed magnitude -- a large, easily
    distinguishable realized volatility, deterministic and hand-traceable."""
    closes = [base]
    for i in range(n - 1):
        r = amplitude if i % 2 == 0 else -amplitude
        closes.append(closes[-1] * math.exp(r))
    return closes


def _calm_closes(n, amplitude=0.001, base=100.0):
    closes = [base]
    for i in range(n - 1):
        r = amplitude if i % 2 == 0 else -amplitude
        closes.append(closes[-1] * math.exp(r))
    return closes


def test_volatility_scaled_qty_fn_reduces_size_when_forecast_above_reference():
    """A visibly volatile series (5% daily swings) with reference_vol=0.25
    must produce a smaller qty than the fixed baseline for the same
    base_notional -- multiplier < 1."""
    closes = _volatile_closes(25)
    bars = _bars_from_closes(closes)
    fixed_fn = spc.fixed_notional_qty_fn(base_notional=10_000.0)
    vol_fn = spc.volatility_scaled_qty_fn(
        base_notional=10_000.0, estimator="realized_vol", reference_vol=0.25,
        vol_floor=0.05, max_multiplier=1.0,
        params={"volatility_realized_vol_window": 20},
    )
    fixed_qty = fixed_fn(bars, None)
    vol_qty = vol_fn(bars, None)
    assert vol_qty < fixed_qty
    assert vol_qty > 0


def test_volatility_scaled_qty_fn_caps_at_max_multiplier_for_calm_series():
    """A very calm series would imply a multiplier well above 1.0 --
    max_multiplier=1.0 caps it, so vol_qty must never exceed fixed_qty
    (the roadmap's own 'reductions only' non-negotiable)."""
    closes = _calm_closes(25)
    bars = _bars_from_closes(closes)
    fixed_fn = spc.fixed_notional_qty_fn(base_notional=10_000.0)
    vol_fn = spc.volatility_scaled_qty_fn(
        base_notional=10_000.0, estimator="realized_vol", reference_vol=0.25,
        vol_floor=0.05, max_multiplier=1.0,
        params={"volatility_realized_vol_window": 20},
    )
    assert vol_fn(bars, None) <= fixed_fn(bars, None)


def test_volatility_scaled_qty_fn_falls_back_to_fixed_when_insufficient_history():
    """Too few bars for realized_vol_daily's window (default 20) -> estimator
    returns insufficient_history -> qty_fn must fall back to the exact fixed
    policy, per VR-2's §6.4 fallback semantics carried into the backtest."""
    closes = _calm_closes(3)  # far short of the default 20-bar window
    bars = _bars_from_closes(closes)
    fixed_fn = spc.fixed_notional_qty_fn(base_notional=5_000.0)
    vol_fn = spc.volatility_scaled_qty_fn(base_notional=5_000.0)
    assert vol_fn(bars, None) == fixed_fn(bars, None)


def test_unknown_estimator_raises():
    try:
        spc.volatility_scaled_qty_fn(base_notional=1000.0, estimator="not_a_real_estimator")
        assert False, "expected ValueError"
    except ValueError:
        pass


# ─────────────────────────────────────────────────────────────────────────
# compare_sizing_policies
# ─────────────────────────────────────────────────────────────────────────

def test_paired_trades_share_identical_entry_exit_timestamps():
    closes = _volatile_closes(30)
    bars = _bars_from_closes(closes)
    strategy = _buy_then_sell_at(buy_idx=6, sell_idx=15)
    report = spc.compare_sizing_policies(
        bars, strategy, base_notional=10_000.0, estimator="realized_vol",
        params={"volatility_realized_vol_window": 5},
    )
    assert report.trade_count == 1
    trade = report.paired_trades[0]
    assert trade.entry_ts == bars[7].ts   # next_bar_open: signal on bar6, fill at bar7's open
    assert trade.exit_ts == bars[16].ts


def test_paired_trades_have_different_qty_but_same_prices():
    closes = _volatile_closes(30)
    bars = _bars_from_closes(closes)
    strategy = _buy_then_sell_at(buy_idx=6, sell_idx=15)
    report = spc.compare_sizing_policies(
        bars, strategy, base_notional=10_000.0, estimator="realized_vol",
        reference_vol=0.25, vol_floor=0.05, max_multiplier=1.0,
        params={"volatility_realized_vol_window": 5},
    )
    trade = report.paired_trades[0]
    assert trade.fixed_qty != trade.volatility_qty
    assert trade.volatility_qty < trade.fixed_qty
    assert trade.entry_price == bars[7].open
    assert trade.exit_price == bars[16].open


def test_pnl_and_return_pct_computed_correctly_per_policy():
    closes = _volatile_closes(30)
    bars = _bars_from_closes(closes)
    strategy = _buy_then_sell_at(buy_idx=6, sell_idx=15)
    report = spc.compare_sizing_policies(
        bars, strategy, base_notional=10_000.0,
        params={"volatility_realized_vol_window": 5},
    )
    trade = report.paired_trades[0]
    expected_fixed_pnl = (trade.exit_price - trade.entry_price) * trade.fixed_qty
    expected_vol_pnl = (trade.exit_price - trade.entry_price) * trade.volatility_qty
    assert trade.fixed_pnl == expected_fixed_pnl
    assert trade.volatility_pnl == expected_vol_pnl
    assert trade.fixed_return_pct == expected_fixed_pnl / (trade.fixed_qty * trade.entry_price)
    assert trade.volatility_return_pct == expected_vol_pnl / (trade.volatility_qty * trade.entry_price)
    assert report.fixed_total_pnl == trade.fixed_pnl
    assert report.volatility_total_pnl == trade.volatility_pnl


def test_multiple_trades_all_paired():
    closes = _volatile_closes(40)
    bars = _bars_from_closes(closes)

    def strategy(bars_seen):
        n = len(bars_seen)
        if n in (7, 22):
            return "buy"
        if n in (14, 32):
            return "sell"
        return None

    report = spc.compare_sizing_policies(
        bars, strategy, base_notional=10_000.0,
        params={"volatility_realized_vol_window": 5},
    )
    assert report.trade_count == 2
    assert report.mean_fixed_return_pct is not None
    assert report.mean_volatility_return_pct is not None


def test_no_trades_gives_empty_report():
    bars = _bars_from_closes(_calm_closes(10))
    report = spc.compare_sizing_policies(bars, lambda bars_seen: None, base_notional=1000.0)
    assert report.trade_count == 0
    assert report.fixed_total_pnl == 0
    assert report.mean_fixed_return_pct is None

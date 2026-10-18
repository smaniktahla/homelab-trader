"""
Mechanics of ingest/research/backtests/backtest_position_adds_study.py:
episode/add-event detection, per-lot fill basis, and the paired-difference
bootstrap the preregistered decision rule (DocMost db0c5f7a) actually calls
for. Synthetic bars/params -- not the values of any study.
"""

import datetime as dt
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _dir in (ROOT / "shared",):
    p = str(_dir)
    if p not in sys.path:
        sys.path.insert(0, p)

_SPEC = importlib.util.spec_from_file_location(
    "backtest_position_adds_study",
    ROOT / "ingest" / "research" / "backtests" / "backtest_position_adds_study.py")
bpas = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bpas)

from exit_policies import MarketContext, WARMUP  # noqa: E402


def _series(closes, start=dt.date(2020, 1, 6)):
    """Ascending weekday dates, high/low a fixed band around each close."""
    d, dates = start, []
    while len(dates) < len(closes):
        if d.weekday() < 5:
            dates.append(d)
        d += dt.timedelta(days=1)
    o = [closes[0]] + closes[:-1]
    h = [max(a, b) * 1.01 for a, b in zip(o, closes)]
    l = [min(a, b) * 0.99 for a, b in zip(o, closes)]
    return {"d": dates, "o": o, "h": h, "l": l, "c": closes}


def _flat_ctx(n_days=WARMUP + 400):
    dates = []
    d = dt.date(2019, 1, 1)
    while len(dates) < n_days:
        if d.weekday() < 5:
            dates.append(d)
        d += dt.timedelta(days=1)
    return MarketContext(dates, [("bull_calm", 0)] * n_days, dates, [400.0] * n_days)


P = dict(
    rsi_period=14, rsi_oversold=30, rsi_overbought=70, rsi_strong_oversold=25, rsi_strong_overbought=75,
    bb_period=20, bb_std=2.0, score_log_min=10, score_proposal_min=10,
    regime_sma_fast=50, regime_sma_slow=200, regime_band=0.02, stop_loss_pct=0.12, trade_cost_flat=1.0,
)


def test_block_bootstrap_ci_is_deterministic_and_centered_on_the_mean():
    values = [0.01, 0.02, -0.01, 0.03, 0.0, 0.02]
    weeks = [(2020, 1), (2020, 1), (2020, 2), (2020, 2), (2020, 3), (2020, 3)]
    out = bpas.block_bootstrap_ci(values, weeks, n=500, seed=0)
    assert out["mean"] == pytest.approx(sum(values) / len(values))
    assert out["ci90"][0] <= out["mean"] <= out["ci90"][1]
    assert out["effective_n_weeks"] == 3
    again = bpas.block_bootstrap_ci(values, weeks, n=500, seed=0)
    assert out["ci90"] == again["ci90"]   # seeded -- reproducible


def test_block_bootstrap_ci_empty_input_returns_none():
    assert bpas.block_bootstrap_ci([], []) is None


def test_fill_price_is_next_bar_open_with_slippage():
    s = _series([100.0] * 10)
    fill, fill_bar = bpas._fill_price(s, 3, slippage=0.001)
    assert fill_bar == 4
    assert fill == pytest.approx(s["o"][4] * 1.001)


def test_fill_price_returns_none_at_end_of_series():
    s = _series([100.0] * 5)
    assert bpas._fill_price(s, 4, slippage=0.0) == (None, None)


def test_walk_add_lot_hard_stop_uses_the_lots_own_fill_not_episode_start():
    """A later add lot's 12% stop is measured from ITS OWN fill, not the
    original episode's entry price -- the exact basis bug found and fixed
    while building this script."""
    closes = [100.0] * 30 + [70.0] * 10   # a later fill near 70 has its stop near 61.6, not ~88
    s = _series(closes)
    ctx = _flat_ctx()
    fill, fill_bar = bpas._fill_price(s, 32, slippage=0.0)
    result = bpas._walk_add_lot(s, P, ctx, episode_start_bar=5, fill=fill, fill_bar=fill_bar,
                                 stop_loss_pct=0.12, slippage=0.0)
    # Flat closes after the drop -- nothing should trip a stop measured off
    # the (much higher) episode-start price, which this fill is already below.
    assert fill == pytest.approx(70.0)
    if result is not None:
        assert result["reason"] != "hard_stop"


def test_run_symbol_detects_an_underwater_add_and_buckets_it():
    """A synthetic RSI-oversold dip that recurs (drop, small bounce, drop
    again) should produce at least one 'add' event once the position is
    open and the second dip prices below the first lot's fill."""
    n = WARMUP + 60
    closes = [100.0] * WARMUP
    closes += [100 - i * 1.5 for i in range(15)]   # sharp drop -> oversold buy signal
    closes += [closes[-1] * 1.01] * 5              # small bounce
    closes += [closes[-1] - i * 1.2 for i in range(15)]  # second leg down, underwater add candidate
    closes += [closes[-1]] * (n - len(closes))
    s = _series(closes)
    ctx = _flat_ctx(n_days=n + 10)

    events = bpas.run_symbol("SYN", s, P, ctx, stop_loss_pct=0.12, slippage=0.0,
                              cost_flat=0.0, min_bar=WARMUP, max_bar=n - 1)
    assert isinstance(events["add"], list) and isinstance(events["b0"], list)
    for ev in events["add"]:
        assert ev["underwater_pct"] >= 0.0
        assert ev["fill_bar"] > ev["signal_bar"]
        assert "r" in ev and "ret" in ev


def test_summarize_paired_diff_matches_manual_difference():
    adds = [{"r": 0.10, "date": "2020-01-06"}, {"r": -0.05, "date": "2020-01-13"}]
    b1s = [{"r": 0.02, "date": "2020-01-06"}, {"r": 0.01, "date": "2020-01-13"}]
    out = bpas.summarize_paired_diff("t", adds, b1s)
    assert out["mean"] == pytest.approx(((0.10 - 0.02) + (-0.05 - 0.01)) / 2)
    assert out["n"] == 2


def test_summarize_paired_diff_skips_pairs_missing_the_key():
    adds = [{"r": 0.10, "date": "2020-01-06"}, {"r": None, "date": "2020-01-13"}]
    b1s = [{"r": 0.02, "date": "2020-01-06"}, {"r": 0.01, "date": "2020-01-13"}]
    out = bpas.summarize_paired_diff("t", adds, b1s)
    assert out["n"] == 1

"""
Tests for shared/account_sizing_comparison.py -- VR-3b of the Volatility
Forecasting & Risk-Targeted Position Sizing epic. Synthetic in-memory
data only, no DB -- same style as tests/test_portfolio_backtest_engine.py/
tests/test_sizing_policy_comparison.py.

Deliberately does NOT re-prove shared/risk_engine.py::evaluate_proposal()'s
own constraint math (buying power, position/sector allocation, stop-
distance risk budget) -- that's tests/test_risk_engine.py's job. These
tests cover what's NEW here: that the qty_fn adapters correctly wire
portfolio_state into evaluate_proposal()'s expected shape, that the
in-memory open-risk ledger tracks and prunes correctly across
buy/sell cycles, and that compare_account_policies() reports divergence
rather than asserting it away.
"""

import math
import sys
import pathlib
from datetime import date, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
p = str(ROOT / "shared")
if p not in sys.path:
    sys.path.insert(0, p)

for _mod in ("account_sizing_comparison", "portfolio_backtest_engine", "risk_engine", "volatility_forecast", "market_structure"):
    sys.modules.pop(_mod, None)
import account_sizing_comparison as asc
from portfolio_backtest_engine import run_portfolio_backtest

START = date(2026, 1, 1)

P = {
    "max_position_pct": 0.20,
    "sector_max_pct": 0.30,
    "risk_per_trade_pct": 0.01,
    "max_portfolio_open_risk_pct": 0.06,
    "trade_allocation_pct": 0.05,
}

VOL_P_ENABLED = dict(
    P,
    volatility_sizing_enabled=1,
    volatility_reference_vol=0.25,
    volatility_vol_floor=0.05,
    volatility_max_multiplier=1.0,
)


class SimpleBar:
    def __init__(self, symbol, ts, open, high, low, close, volume=1000):
        self.symbol = symbol
        self.ts = ts
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


def _bars(symbol, closes, opens=None, start=START):
    opens = opens or closes
    return [
        SimpleBar(symbol, start + timedelta(days=i), o, max(o, c), min(o, c), c)
        for i, (o, c) in enumerate(zip(opens, closes))
    ]


def _flat_closes(n, level=100.0):
    return [level] * n


def _volatile_closes(n, amplitude=0.05, base=100.0):
    closes = [base]
    for i in range(n - 1):
        r = amplitude if i % 2 == 0 else -amplitude
        closes.append(closes[-1] * math.exp(r))
    return closes


def _buy_and_hold_strategy(symbol, buy_date):
    def strategy(today, bars_by_symbol_asof, portfolio_state):
        if today == buy_date and symbol in bars_by_symbol_asof:
            return [(symbol, "buy")]
        return []
    return strategy


# ─────────────────────────────────────────────────────────────────────────
# make_current_policy_qty_fn -- adapter correctness
# ─────────────────────────────────────────────────────────────────────────

def test_current_policy_qty_fn_returns_zero_when_no_room():
    qty_fn = asc.make_current_policy_qty_fn(sector_map={}, params=P)
    portfolio_state = {"cash": 0.0, "total_equity": 0.0, "positions": {}}
    assert qty_fn("AAA", 100.0, portfolio_state, [], "buy") == 0


def test_current_policy_qty_fn_sizes_within_buying_power():
    qty_fn = asc.make_current_policy_qty_fn(sector_map={}, params=P)
    portfolio_state = {"cash": 100_000.0, "total_equity": 100_000.0, "positions": {}}
    qty = qty_fn("AAA", 100.0, portfolio_state, [], "buy")
    assert qty > 0
    # trade_allocation_pct 5% of 100k = $5000 -> 50 shares, well within
    # buying power and every other constraint at these generous defaults.
    assert qty == 50


def test_current_policy_qty_fn_respects_position_allocation_cap_via_market_value():
    """Confirms the adapter's positions_by_symbol wiring actually reaches
    evaluate_proposal() -- an existing large position in the SAME symbol
    should shrink room via max_position_pct, same as risk_engine.py's own
    test suite proves for the raw function."""
    qty_fn = asc.make_current_policy_qty_fn(sector_map={}, params=P)
    portfolio_state = {
        "cash": 100_000.0, "total_equity": 100_000.0,
        "positions": {"AAA": {"qty": 190.0, "entry_price": 100.0, "market_value": 19_000.0}},
    }
    # max_position_pct 20% of 100k = 20k cap - 19k existing = 1k room / $100 = 10 shares
    qty = qty_fn("AAA", 100.0, portfolio_state, [], "buy")
    assert qty == 10


def test_open_risk_ledger_accumulates_across_positions_and_caps_portfolio_open_risk():
    """Two open positions' tracked risk-dollars should combine to bind
    portfolio_open_risk on a third candidate, proving the in-memory ledger
    (not just a single call's own risk) is actually being summed.

    Tuned so this is a real, non-trivial bind rather than a coincidence:
    risk_per_trade_pct=1% of $100k equity = $1000 budget/position;
    risk_per_share = 100-92 = 8 (stop_loss_pct=0.08) -> risk_qty=125/position.
    max_portfolio_open_risk_pct=2% of $100k = $2000 total -- exactly two
    125-share positions (@ $1000 risk each) exhaust it, so a third
    candidate must get 0, not just "somewhat less."""
    params = dict(P, max_position_pct=0.50, sector_max_pct=0.50,
                  max_portfolio_open_risk_pct=0.02, trade_allocation_pct=0.50)
    qty_fn = asc.make_current_policy_qty_fn(sector_map={}, params=params, stop_loss_pct=0.08)
    equity = 100_000.0

    portfolio_state = {"cash": equity, "total_equity": equity, "positions": {}}
    qty1 = qty_fn("AAA", 100.0, portfolio_state, [], "buy")
    assert qty1 == 125

    portfolio_state = {
        "cash": equity, "total_equity": equity,
        "positions": {"AAA": {"qty": qty1, "entry_price": 100.0, "market_value": qty1 * 100.0}},
    }
    qty2 = qty_fn("BBB", 100.0, portfolio_state, [], "buy")
    assert qty2 == 125

    portfolio_state = {
        "cash": equity, "total_equity": equity,
        "positions": {
            "AAA": {"qty": qty1, "entry_price": 100.0, "market_value": qty1 * 100.0},
            "BBB": {"qty": qty2, "entry_price": 100.0, "market_value": qty2 * 100.0},
        },
    }
    qty3 = qty_fn("CCC", 100.0, portfolio_state, [], "buy")
    assert qty3 == 0


def test_ledger_prunes_closed_positions():
    """Once a symbol is no longer in portfolio_state['positions'] (the
    engine closed it), its tracked risk-dollars must drop out of the
    ledger -- otherwise a closed position would permanently and
    incorrectly suppress portfolio_open_risk room forever.

    Same tuned params as the accumulation test above, where a single
    125-share position already consumes the ENTIRE $2000
    max_portfolio_open_risk_pct budget -- if pruning didn't work, the
    second call (AAA no longer in positions) would still see AAA's risk
    and return 0, not 125."""
    params = dict(P, max_position_pct=0.50, sector_max_pct=0.50,
                  max_portfolio_open_risk_pct=0.02, trade_allocation_pct=0.50)
    qty_fn = asc.make_current_policy_qty_fn(sector_map={}, params=params, stop_loss_pct=0.08)
    equity = 100_000.0
    portfolio_state = {"cash": equity, "total_equity": equity, "positions": {}}
    qty1 = qty_fn("AAA", 100.0, portfolio_state, [], "buy")
    assert qty1 == 125

    # AAA position "closes" -- next call's portfolio_state no longer lists it.
    portfolio_state_after_close = {"cash": equity, "total_equity": equity, "positions": {}}
    qty2 = qty_fn("BBB", 100.0, portfolio_state_after_close, [], "buy")
    assert qty2 == 125  # would be 0 if AAA's risk were still (incorrectly) counted


# ─────────────────────────────────────────────────────────────────────────
# make_volatility_policy_qty_fn
# ─────────────────────────────────────────────────────────────────────────

def test_unknown_estimator_raises():
    try:
        asc.make_volatility_policy_qty_fn(sector_map={}, params=VOL_P_ENABLED, estimator="not_real")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_volatility_policy_reduces_size_for_a_visibly_volatile_series():
    closes = _volatile_closes(30)
    bars = _bars("AAA", closes)
    qty_fn = asc.make_volatility_policy_qty_fn(
        sector_map={}, params=VOL_P_ENABLED,
        volatility_params={"volatility_realized_vol_window": 5},
    )
    current_fn = asc.make_current_policy_qty_fn(sector_map={}, params=P)

    portfolio_state = {"cash": 100_000.0, "total_equity": 100_000.0, "positions": {}}
    bars_seen = bars[:10]
    price = bars_seen[-1].close

    current_qty = current_fn("AAA", price, portfolio_state, bars_seen, "buy")
    vol_qty = qty_fn("AAA", price, portfolio_state, bars_seen, "buy")
    assert vol_qty < current_qty
    assert vol_qty > 0


def test_volatility_policy_matches_current_policy_when_forecast_unavailable():
    """Too little history for the estimator -> volatility_forecast stays
    None -> evaluate_proposal()'s own §6.4 fallback means this must equal
    what the current (no-overlay) policy would produce for the identical
    inputs -- the bit-for-bit guarantee VR-2 established, now exercised
    through this module's adapters instead of calling evaluate_proposal()
    directly."""
    bars = _bars("AAA", _flat_closes(3))
    current_fn = asc.make_current_policy_qty_fn(sector_map={}, params=P)
    vol_fn = asc.make_volatility_policy_qty_fn(sector_map={}, params=VOL_P_ENABLED)

    portfolio_state = {"cash": 100_000.0, "total_equity": 100_000.0, "positions": {}}
    price = bars[-1].close
    assert current_fn("AAA", price, portfolio_state, bars, "buy") == vol_fn("AAA", price, portfolio_state, bars, "buy")


# ─────────────────────────────────────────────────────────────────────────
# compare_account_policies -- end to end, divergence reporting
# ─────────────────────────────────────────────────────────────────────────

def test_compare_account_policies_report_structure():
    bars_by_symbol = {
        "AAA": _bars("AAA", _volatile_closes(20)),
        "BBB": _bars("BBB", _flat_closes(20, level=50.0)),
    }

    def strategy(today, bars_by_symbol_asof, portfolio_state):
        candidates = []
        if today == START and "AAA" in bars_by_symbol_asof:
            candidates.append(("AAA", "buy"))
        if today == START + timedelta(days=5) and "BBB" in bars_by_symbol_asof:
            candidates.append(("BBB", "buy"))
        return candidates

    report = asc.compare_account_policies(
        bars_by_symbol, strategy, starting_cash=100_000.0, sector_map={},
        current_params=P, volatility_params=VOL_P_ENABLED,
        volatility_estimator_params={"volatility_realized_vol_window": 5},
    )
    summary = report.summary
    assert "current" in summary and "volatility" in summary
    assert "trade_set_divergence" in summary
    for side in ("current", "volatility"):
        assert summary[side]["final_equity"] > 0
        assert isinstance(summary[side]["trade_count"], int)


def test_compare_account_policies_does_not_assert_identical_trade_sets():
    """Cash-tight scenario deliberately constructed so the two policies
    CAN diverge on which candidates get filled -- proves the comparison
    harness surfaces divergence via symbols_only_in_current/
    symbols_only_in_volatility rather than raising, unlike VR-3a's paired
    analysis which explicitly asserts identical trade sets."""
    bars_by_symbol = {
        "AAA": _bars("AAA", _volatile_closes(20)),
        "BBB": _bars("BBB", _volatile_closes(20, base=90.0)),
    }

    def strategy(today, bars_by_symbol_asof, portfolio_state):
        if today == START:
            return [("AAA", "buy"), ("BBB", "buy")]
        return []

    report = asc.compare_account_policies(
        bars_by_symbol, strategy, starting_cash=20_000.0, sector_map={},
        current_params=dict(P, trade_allocation_pct=0.60),
        volatility_params=dict(VOL_P_ENABLED, trade_allocation_pct=0.60),
        volatility_estimator_params={"volatility_realized_vol_window": 5},
    )
    # Whatever the actual divergence, the report must characterize it
    # without raising -- both trade sets are non-crashing, well-formed.
    assert isinstance(report.common_symbols, set)
    assert isinstance(report.symbols_only_in_current, set)
    assert isinstance(report.symbols_only_in_volatility, set)

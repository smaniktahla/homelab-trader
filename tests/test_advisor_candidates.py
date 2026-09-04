"""
Tests for api/main.py::_build_advisor_candidates() -- the risk-engine-aware
sizing fix for GET /api/advisor's candidate list.

Bug: suggested_shares used to be a bare notional/price calc with zero
awareness of shared/risk_engine.py's constraints -- a user could be shown
"buy 17 shares" and have it rejected outright the moment they acted on it
via /api/trade (portfolio_open_risk was the constraint that actually bit
in production, once many positions were already open). This module's
function now routes every candidate through risk_engine.evaluate_proposal(),
the same authoritative function the live trade path uses.

Extracted as a pure function precisely so it's testable without hitting
Alpaca or the DB -- get_advisor() itself queries a `user_profile` table
this repo's schema.sql/migrations never define, making the endpoint
un-testable end-to-end against the disposable test Postgres (a
pre-existing, unrelated gap, not something fixed here). Same
env-var-then-import pattern as tests/test_api_risk_engine.py's api_client
fixture, since api/main.py requires DATABASE_URL at import time.
"""

import os
import sys
import pathlib

import pytest


@pytest.fixture
def api_main(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ.get(
        "TEST_DATABASE_URL", "postgresql://invest_test:not_a_real_credential@localhost:15432/invest_test"))
    monkeypatch.setenv("INVEST_USER", "test_invest_user")
    monkeypatch.setenv("INVEST_PASS", "test_invest_pass_not_real")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://fake-alpaca.test")

    api_dir = str(pathlib.Path(__file__).resolve().parent.parent / "api")
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    for mod in ("main", "risk_engine", "signals", "circuit_breaker"):
        sys.modules.pop(mod, None)
    import main as api_main_module
    return api_main_module


P = {
    "max_position_pct": 0.20,
    "sector_max_pct": 0.30,
    "risk_per_trade_pct": 0.01,
    "max_portfolio_open_risk_pct": 0.06,
    "circuit_breaker_drawdown_pct": 0.15,
}


def _candidate(symbol="AAPL", price=100.0, rsi=28.0, buy_score=80):
    return {"symbol": symbol, "price": price, "rsi": rsi, "buy_score": buy_score}


def test_suggested_shares_reflects_risk_engine_not_naive_notional(api_main):
    """The exact production bug: per_trade_notional/price alone would
    suggest 100 shares ($10,000 / $100); with 6% max_portfolio_open_risk_pct
    already almost entirely consumed (open_risk_dollars=5900 of a 100k
    portfolio's $6000 cap), the risk engine should clamp this down hard --
    suggested_shares must NOT equal the naive 100."""
    candidates = api_main._build_advisor_candidates(
        top_buys=[_candidate(price=100.0)],
        held_symbols=set(),
        per_trade_notional=10_000.0,
        stop_loss_pct=0.08,
        cash=100_000.0, portfolio_value=100_000.0,
        positions_by_symbol={}, sector_map={},
        open_risk_dollars=5900.0, params=P, drawdown_mult=1.0,
    )
    assert len(candidates) == 1
    c = candidates[0]
    naive_suggestion = int(10_000.0 / 100.0)
    assert c["suggested_shares"] != naive_suggestion
    assert c["suggested_shares"] < naive_suggestion
    assert c["risk_outcome"] in ("reduced", "rejected")
    assert c["risk_binding_constraint"] == "portfolio_open_risk"


def test_suggested_shares_matches_naive_calc_when_risk_engine_has_ample_room(api_main):
    """Sanity check the other direction: with no open positions and ample
    room on every constraint, the risk-engine-approved qty should equal
    (or very nearly equal) what a human would expect from the naive calc
    -- the fix must not make recommendations gratuitously more
    conservative than necessary."""
    candidates = api_main._build_advisor_candidates(
        top_buys=[_candidate(price=100.0)],
        held_symbols=set(),
        per_trade_notional=1_000.0,
        stop_loss_pct=0.08,
        cash=1_000_000.0, portfolio_value=1_000_000.0,
        positions_by_symbol={}, sector_map={},
        open_risk_dollars=0.0, params=P, drawdown_mult=1.0,
    )
    c = candidates[0]
    assert c["suggested_shares"] == int(1_000.0 / 100.0)
    assert c["risk_outcome"] == "approved"
    assert c["risk_binding_constraint"] is None


def test_zero_or_missing_price_candidate_yields_no_suggestion_not_a_crash(api_main):
    candidates = api_main._build_advisor_candidates(
        top_buys=[_candidate(price=0.0)],
        held_symbols=set(),
        per_trade_notional=1000.0,
        stop_loss_pct=0.08,
        cash=100_000.0, portfolio_value=100_000.0,
        positions_by_symbol={}, sector_map={},
        open_risk_dollars=0.0, params=P, drawdown_mult=1.0,
    )
    c = candidates[0]
    assert c["suggested_shares"] is None
    assert c["suggested_notional"] is None
    assert c["risk_outcome"] is None


def test_is_held_flag_reflects_held_symbols(api_main):
    candidates = api_main._build_advisor_candidates(
        top_buys=[_candidate(symbol="MSFT", price=50.0)],
        held_symbols={"MSFT"},
        per_trade_notional=1000.0,
        stop_loss_pct=0.08,
        cash=100_000.0, portfolio_value=100_000.0,
        positions_by_symbol={"MSFT": {"market_value": 5000.0}}, sector_map={},
        open_risk_dollars=0.0, params=P, drawdown_mult=1.0,
    )
    assert candidates[0]["is_held"] is True


def test_multiple_candidates_each_sized_independently(api_main):
    candidates = api_main._build_advisor_candidates(
        top_buys=[_candidate(symbol="AAA", price=50.0), _candidate(symbol="BBB", price=200.0)],
        held_symbols=set(),
        per_trade_notional=1000.0,
        stop_loss_pct=0.08,
        cash=100_000.0, portfolio_value=100_000.0,
        positions_by_symbol={}, sector_map={},
        open_risk_dollars=0.0, params=P, drawdown_mult=1.0,
    )
    assert len(candidates) == 2
    assert candidates[0]["symbol"] == "AAA"
    assert candidates[0]["suggested_shares"] == int(1000.0 / 50.0)
    assert candidates[1]["symbol"] == "BBB"
    assert candidates[1]["suggested_shares"] == int(1000.0 / 200.0)

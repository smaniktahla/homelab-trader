"""
Tests for api/main.py::_risk_drift_for_symbols() -- PR D of the
exit/protection-state series (docs/position-exit-state-investigation.md).

Needs the real DB (joins actual trades/trade_proposals rows), so this is
an integration test against tests/conftest.py's `conn` fixture, not a pure
unit test -- same reasoning as tests/test_api_protective_stops.py, which
this borrows its RealDictCursor pattern from.
"""

import os
import sys
import pathlib
from datetime import datetime, timezone

import psycopg2.extras
import pytest

API_DIR = str(pathlib.Path(__file__).resolve().parent.parent / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)


@pytest.fixture(autouse=True)
def _api_main_env(monkeypatch):
    """api/main.py reads several env vars at import time (DB_DSN,
    ALPACA_*, INVEST_USER/PASS) -- same monkeypatch set as
    tests/test_api_trade_thesis_snapshot.py's api_client fixture, minus
    the TestClient/Alpaca-mocking machinery this file doesn't need since
    it calls _risk_drift_for_symbols() directly against a real cursor,
    never through an HTTP request."""
    monkeypatch.setenv("DATABASE_URL", os.environ.get(
        "TEST_DATABASE_URL", "postgresql://invest_test:not_a_real_credential@localhost:15432/invest_test"))
    monkeypatch.setenv("INVEST_USER", "test_invest_user")
    monkeypatch.setenv("INVEST_PASS", "test_invest_pass_not_real")
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://fake-alpaca.test")


def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug='mean_reversion'")
        return cur.fetchone()[0]


def _seed_proposal(conn, symbol, planned_entry, planned_stop, thesis_id):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals
                (symbol, side, qty, rationale, signal_score, thesis_id,
                 planned_entry_price, planned_initial_stop_price, decision, decided_at)
            VALUES (%s, 'buy', 10, 'test seed', 80, %s, %s, %s, 'approved', NOW())
            RETURNING id
        """, (symbol, thesis_id, planned_entry, planned_stop))
        proposal_id = cur.fetchone()[0]
    conn.commit()
    return proposal_id


def _seed_buy_trade(conn, symbol, proposal_id, actual_entry, actual_stop, thesis_id):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trades
                (symbol, side, qty, price, notional, order_id, traded_at,
                 status, proposal_id, cost, thesis_id, initial_stop_price)
            VALUES (%s, 'buy', 10, %s, %s, %s, %s, 'filled', %s, 0, %s, %s)
        """, (symbol, actual_entry, actual_entry * 10, f"order-{symbol}",
              datetime.now(timezone.utc), proposal_id, thesis_id, actual_stop))
    conn.commit()


def _risk_drift(conn, symbols):
    for mod in ("main", "rule_adherence", "signals", "risk_engine", "trading_permission", "circuit_breaker"):
        sys.modules.pop(mod, None)
    import main as api_main
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        return api_main._risk_drift_for_symbols(cur, symbols)


def test_wider_than_planned_drift_is_positive(conn):
    """Mirrors the real APP case that motivated this PR: proposal planned
    a 12% stop off $334.24, but the actual fill was $350.21 (price moved
    up before the order filled) -- the frozen dollar stop is unchanged,
    so realized risk is now wider than planned. drift_pct must be
    positive."""
    thesis_id = _mean_reversion_thesis_id(conn)
    proposal_id = _seed_proposal(conn, "APP", 334.24, 294.14, thesis_id)
    _seed_buy_trade(conn, "APP", proposal_id, 350.21, 294.14, thesis_id)

    result = _risk_drift(conn, ["APP"])

    assert "APP" in result
    d = result["APP"]
    assert d["planned_risk_pct"] == pytest.approx(12.0, abs=0.05)
    assert d["realized_risk_pct"] == pytest.approx(16.0, abs=0.05)
    assert d["drift_pct"] > 0
    assert d["planned_entry_price"] == pytest.approx(334.24)
    assert d["actual_entry_price"] == pytest.approx(350.21)


def test_tighter_than_planned_drift_is_negative(conn):
    """The same mechanism cuts the other way: if price gaps DOWN before
    fill, the frozen dollar stop ends up closer to the actual (lower)
    entry than planned -- realized risk is tighter, drift_pct negative."""
    thesis_id = _mean_reversion_thesis_id(conn)
    proposal_id = _seed_proposal(conn, "TTD", 14.17, 12.47, thesis_id)  # planned 12%
    _seed_buy_trade(conn, "TTD", proposal_id, 13.51, 12.47, thesis_id)  # filled lower

    result = _risk_drift(conn, ["TTD"])

    d = result["TTD"]
    assert d["planned_risk_pct"] == pytest.approx(12.0, abs=0.05)
    assert d["realized_risk_pct"] < d["planned_risk_pct"]
    assert d["drift_pct"] < 0


def test_fill_exactly_at_plan_has_zero_drift(conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    proposal_id = _seed_proposal(conn, "SRE", 84.95, 74.76, thesis_id)
    _seed_buy_trade(conn, "SRE", proposal_id, 84.95, 74.76, thesis_id)

    result = _risk_drift(conn, ["SRE"])

    assert result["SRE"]["drift_pct"] == pytest.approx(0.0, abs=0.01)


def test_manual_buy_with_no_proposal_is_omitted_not_zero(conn):
    """A trade with no proposal_id (manual buy) has nothing to diff
    against -- must be absent from the result, never a fabricated 0%
    drift that would misleadingly claim 'no drift' when there was no
    plan at all."""
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trades
                (symbol, side, qty, price, notional, order_id, traded_at,
                 status, proposal_id, cost, thesis_id, initial_stop_price)
            VALUES ('MANUAL', 'buy', 10, 50.0, 500.0, 'order-manual', %s,
                    'filled', NULL, 0, %s, 44.0)
        """, (datetime.now(timezone.utc), thesis_id))
    conn.commit()

    result = _risk_drift(conn, ["MANUAL"])

    assert "MANUAL" not in result


def test_symbol_with_no_trades_at_all_is_omitted(conn):
    result = _risk_drift(conn, ["NEVER_TRADED"])
    assert "NEVER_TRADED" not in result


def test_empty_symbols_returns_empty_dict(conn):
    result = _risk_drift(conn, [])
    assert result == {}


def test_uses_most_recent_buy_when_symbol_bought_more_than_once(conn):
    """Pyramided/re-entered position: must compare against the LATEST
    buy's plan, not an earlier one -- matches every other "most recent
    per symbol" convention already in api/main.py (DISTINCT ON ...
    ORDER BY ... DESC)."""
    thesis_id = _mean_reversion_thesis_id(conn)
    old_proposal = _seed_proposal(conn, "CMS", 60.0, 52.8, thesis_id)  # planned 12%, stale
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trades
                (symbol, side, qty, price, notional, order_id, traded_at,
                 status, proposal_id, cost, thesis_id, initial_stop_price)
            VALUES ('CMS', 'buy', 5, 60.5, 302.5, 'order-cms-old', %s,
                    'filled', %s, 0, %s, 52.8)
        """, (datetime(2026, 1, 1, tzinfo=timezone.utc), old_proposal, thesis_id))
    conn.commit()

    new_proposal = _seed_proposal(conn, "CMS", 70.66, 62.18, thesis_id)  # planned 12%, current
    _seed_buy_trade(conn, "CMS", new_proposal, 70.34, 62.18, thesis_id)

    result = _risk_drift(conn, ["CMS"])

    assert result["CMS"]["planned_entry_price"] == pytest.approx(70.66, abs=0.01)

"""
Tests for Trade History's date-range filtering and buy/sell date pairing
(GET /api/trades' new start/end params and lifecycle_opened_at/
lifecycle_closed_at/lifecycle_status fields).

Same convention as test_api_symbol_performance.py: every test inserts raw
trades, then runs ingest/build_position_lifecycles.py's real
build_position_lifecycles(conn) to materialize position_lifecycles/
position_trades -- the API only ever reads those materialized tables, so
tests exercise the real trades -> build_position_lifecycles -> API
pipeline rather than hand-seeding derived state.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg2.extras
import pytest
import requests_mock


@pytest.fixture
def api_client(_schema_ready, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ.get(
        "TEST_DATABASE_URL", "postgresql://invest_test:not_a_real_credential@localhost:15432/invest_test"))
    monkeypatch.setenv("INVEST_USER", "test_invest_user")
    monkeypatch.setenv("INVEST_PASS", "test_invest_pass_not_real")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://fake-alpaca.test")

    import pathlib
    api_dir = str(pathlib.Path(__file__).resolve().parent.parent / "api")
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)

    sys.modules.pop("main", None)
    import main as api_main
    from fastapi.testclient import TestClient
    return TestClient(api_main.app)


AUTH = ("test_invest_user", "test_invest_pass_not_real")


def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug='mean_reversion'")
        return cur.fetchone()[0]


def _insert_trade(conn, symbol, side, qty, price, cost, traded_at, status="filled"):
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trades (symbol, side, qty, price, notional, traded_at, cost, status, thesis_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (symbol, side, qty, price, qty * price, traded_at, cost, status, thesis_id))
        trade_id = cur.fetchone()[0]
    conn.commit()
    return trade_id


def _materialize_lifecycles(conn):
    import pathlib
    ingest_dir = str(pathlib.Path(__file__).resolve().parent.parent / "ingest")
    if ingest_dir not in sys.path:
        sys.path.insert(0, ingest_dir)
    sys.modules.pop("build_position_lifecycles", None)
    from build_position_lifecycles import build_position_lifecycles
    build_position_lifecycles(conn)


def test_lifecycle_dates_by_trade_id_closed_and_open(api_client, conn):
    import main as api_main

    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    buy1 = _insert_trade(conn, "AAPL", "buy", 10, 100.0, 0.0, base)
    sell1 = _insert_trade(conn, "AAPL", "sell", 10, 110.0, 0.0, base + timedelta(days=5))  # fully closes
    buy2 = _insert_trade(conn, "MSFT", "buy", 10, 200.0, 0.0, base + timedelta(days=1))
    sell2 = _insert_trade(conn, "MSFT", "sell", 4, 210.0, 0.0, base + timedelta(days=2))   # partial exit, still open
    _materialize_lifecycles(conn)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        result = api_main._lifecycle_dates_by_trade_id(cur)

    assert result[buy1]["status"] == "closed"
    assert result[buy1]["closed_at"] is not None
    assert result[sell1]["status"] == "closed"
    assert result[sell1]["opened_at"] == result[buy1]["opened_at"]
    assert result[sell1]["closed_at"] == result[buy1]["closed_at"]

    assert result[buy2]["status"] == "open"
    assert result[buy2]["closed_at"] is None
    assert result[sell2]["status"] == "open"
    assert result[sell2]["closed_at"] is None
    assert result[sell2]["opened_at"] == result[buy2]["opened_at"]


def test_lifecycle_dates_by_trade_id_untouched_trade_absent(api_client, conn):
    """A trade with no position_trades row at all (shouldn't normally
    happen since match_lifecycles() walks every filled trade, but the
    helper must fail open rather than KeyError if it ever does)."""
    import main as api_main

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        result = api_main._lifecycle_dates_by_trade_id(cur)
    assert result == {}


def test_get_trades_no_date_params_unchanged_shape(api_client, conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _insert_trade(conn, "AAPL", "buy", 10, 100.0, 0.0, base)
    _insert_trade(conn, "AAPL", "sell", 10, 110.0, 0.0, base + timedelta(days=5))
    _materialize_lifecycles(conn)

    r = api_client.get("/api/trades", auth=AUTH)
    assert r.status_code == 200
    trades = r.json()
    assert len(trades) == 2
    for t in trades:
        assert "lifecycle_opened_at" in t
        assert "lifecycle_closed_at" in t
        assert "lifecycle_status" in t
        assert "realized_pnl" in t

    sell = next(t for t in trades if t["side"] == "sell")
    assert sell["lifecycle_status"] == "closed"
    assert sell["lifecycle_opened_at"] is not None
    assert sell["lifecycle_closed_at"] is not None


def test_get_trades_start_end_filters_to_period_exclusive_upper_bound(api_client, conn):
    week1_start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    week2_start = datetime(2026, 6, 8, tzinfo=timezone.utc)
    _insert_trade(conn, "AAPL", "buy", 10, 100.0, 0.0, week1_start + timedelta(days=1))
    _insert_trade(conn, "AAPL", "sell", 10, 110.0, 0.0, week1_start + timedelta(days=2))
    _insert_trade(conn, "MSFT", "buy", 5, 200.0, 0.0, week2_start)  # exactly at the boundary -- excluded
    _insert_trade(conn, "MSFT", "sell", 5, 210.0, 0.0, week2_start + timedelta(days=1))
    _materialize_lifecycles(conn)

    r = api_client.get("/api/trades", params={
        "start": week1_start.isoformat(), "end": week2_start.isoformat(),
    }, auth=AUTH)
    assert r.status_code == 200
    trades = r.json()
    assert len(trades) == 2
    assert all(t["symbol"] == "AAPL" for t in trades)


def test_get_trades_partial_exit_of_open_lifecycle_shows_still_open(api_client, conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _insert_trade(conn, "MSFT", "buy", 10, 200.0, 0.0, base)
    _insert_trade(conn, "MSFT", "sell", 4, 210.0, 0.0, base + timedelta(days=1))  # partial -- 6 shares remain open
    _materialize_lifecycles(conn)

    r = api_client.get("/api/trades", auth=AUTH)
    assert r.status_code == 200
    sell = next(t for t in r.json() if t["side"] == "sell")
    assert sell["lifecycle_status"] == "open"
    assert sell["lifecycle_closed_at"] is None
    assert sell["lifecycle_opened_at"] is not None

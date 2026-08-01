"""
End-to-end test of GET /api/symbol-performance/{symbol} against a real
Postgres connection, with Alpaca's live-position lookup mocked. Exists
specifically to catch integration-level issues pure round_trips.py unit
tests can't (RealDictCursor row shapes, the HTTPException(404)-as-"flat"
path, actual SQL execution) — the same reasoning as
test_api_symbol_features.py in the signal-component work.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

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
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (symbol, side, qty, price, qty * price, traded_at, cost, status, thesis_id))
    conn.commit()


def test_symbol_performance_no_trades(api_client, conn):
    with requests_mock.Mocker() as m:
        m.get("https://fake-alpaca.test/v2/positions/AAPL", status_code=404, json={"message": "not found"})
        r = api_client.get("/api/symbol-performance/AAPL", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["completed_round_trips"] == 0
    assert body["methodology"] == "average_cost_reconstruction"
    assert body["open_position"] is None


def test_symbol_performance_completed_and_open(api_client, conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _insert_trade(conn, "AAPL", "buy", 10, 100.0, 1.0, base)
    _insert_trade(conn, "AAPL", "sell", 10, 90.0, 1.0, base + timedelta(days=3))     # completed loser
    _insert_trade(conn, "AAPL", "buy", 5, 95.0, 1.0, base + timedelta(days=10))       # still open

    with requests_mock.Mocker() as m:
        m.get("https://fake-alpaca.test/v2/positions/AAPL", json={
            "symbol": "AAPL", "qty": "5", "avg_entry_price": "95.0",
            "current_price": "150.0", "unrealized_pl": "275.0", "market_value": "750.0",
        })
        r = api_client.get("/api/symbol-performance/AAPL", auth=AUTH)
    assert r.status_code == 200
    body = r.json()

    assert body["completed_round_trips"] == 1
    assert body["wins"] == 0
    assert body["losses"] == 1
    assert body["win_rate_pct"] == 0.0
    assert body["realized_pnl"] == -102.0   # -100 pnl - 2 in costs across the closed trip
    assert body["unrealized_pnl"] == 275.0
    assert body["total_pnl"] == pytest.approx(173.0)
    assert body["open_position"]["qty"] == 5.0
    assert body["open_position"]["unrealized_pnl"] == 275.0


def test_symbol_round_trips_endpoint_distinguishes_open_from_closed(api_client, conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _insert_trade(conn, "MSFT", "buy", 10, 200.0, 0.0, base)
    _insert_trade(conn, "MSFT", "sell", 10, 220.0, 0.0, base + timedelta(days=4))
    _insert_trade(conn, "MSFT", "buy", 3, 210.0, 0.0, base + timedelta(days=20))

    with requests_mock.Mocker() as m:
        m.get("https://fake-alpaca.test/v2/positions/MSFT", json={
            "symbol": "MSFT", "qty": "3", "avg_entry_price": "210.0",
            "current_price": "215.0", "unrealized_pl": "15.0", "market_value": "645.0",
        })
        r = api_client.get("/api/symbol-performance/MSFT/round-trips", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert len(body["round_trips"]) == 1
    assert body["round_trips"][0]["status"] == "closed"
    assert body["round_trips"][0]["net_pnl"] == 200.0
    assert body["open_position"]["qty"] == 3.0

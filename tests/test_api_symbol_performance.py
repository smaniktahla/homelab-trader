"""
End-to-end test of GET /api/symbol-performance/{symbol} against a real
Postgres connection, with Alpaca's live-position lookup mocked. Exists
specifically to catch integration-level issues pure lifecycle_performance.py
unit tests can't (RealDictCursor row shapes, the HTTPException(404)-as-"flat"
path, actual SQL execution) — same reasoning as test_api_symbol_features.py.

Platform Improvements PR A.1: this endpoint now reads from the materialized
position_lifecycles/position_trades/position_lifecycle_symbol_status
tables instead of reconstructing from shared/round_trips.py (removed) at
request time. Every test here inserts trades, then runs
ingest/build_position_lifecycles.py's build_position_lifecycles(conn) to
actually materialize those tables — exercising the real trades ->
build_position_lifecycles -> API pipeline end to end, not just the API
layer in isolation.
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


def _materialize_lifecycles(conn):
    """Runs the real ingest/build_position_lifecycles.py builder against
    whatever's currently in `trades`, same as a real ingest cycle would --
    the API only ever reads the tables this populates, never the ledger
    directly."""
    import pathlib
    ingest_dir = str(pathlib.Path(__file__).resolve().parent.parent / "ingest")
    if ingest_dir not in sys.path:
        sys.path.insert(0, ingest_dir)
    sys.modules.pop("build_position_lifecycles", None)
    from build_position_lifecycles import build_position_lifecycles
    build_position_lifecycles(conn)


def test_symbol_performance_no_trades(api_client, conn):
    _materialize_lifecycles(conn)
    with requests_mock.Mocker() as m:
        m.get("https://fake-alpaca.test/v2/positions/AAPL", status_code=404, json={"message": "not found"})
        r = api_client.get("/api/symbol-performance/AAPL", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["completed_round_trips"] == 0
    assert body["methodology"] == "position_lifecycle_fifo"
    assert body["open_position"] is None


def test_symbol_performance_completed_and_open(api_client, conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _insert_trade(conn, "AAPL", "buy", 10, 100.0, 1.0, base)
    _insert_trade(conn, "AAPL", "sell", 10, 90.0, 1.0, base + timedelta(days=3))     # completed loser
    _insert_trade(conn, "AAPL", "buy", 5, 95.0, 1.0, base + timedelta(days=10))       # still open
    _materialize_lifecycles(conn)

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
    assert body["realized_pnl"] == -102.0   # -100 pnl - 2 in costs across the closed lifecycle
    assert body["unrealized_pnl"] == 275.0
    assert body["total_pnl"] == pytest.approx(173.0)
    assert body["open_position"]["qty"] == 5.0
    assert body["open_position"]["unrealized_pnl"] == 275.0
    assert body["reconciliation"]["status"] == "match"
    assert body["methodology_status"] == "complete"
    assert body["capital_deployed_methodology"] == "sum_of_entry_notionals"


def test_symbol_performance_fifo_differs_from_average_cost_on_pyramided_position(api_client, conn):
    """The whole point of PR A.1: a pyramided (added-to) position produces
    different numbers under true FIFO lot matching than round_trips.py's
    now-removed average-cost blending would have. Two entries at different
    prices, then a partial sell that FIFO resolves against the older,
    cheaper lot first."""
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _insert_trade(conn, "TSLA", "buy", 10, 100.0, 0.0, base)                       # older, cheaper lot
    _insert_trade(conn, "TSLA", "buy", 10, 200.0, 0.0, base + timedelta(days=1))   # newer, pricier lot
    _insert_trade(conn, "TSLA", "sell", 10, 250.0, 0.0, base + timedelta(days=5))  # FIFO consumes the $100 lot fully
    _materialize_lifecycles(conn)

    with requests_mock.Mocker() as m:
        m.get("https://fake-alpaca.test/v2/positions/TSLA", json={
            "symbol": "TSLA", "qty": "10", "avg_entry_price": "150.0",
            "current_price": "250.0", "unrealized_pl": "1000.0", "market_value": "2500.0",
        })
        r = api_client.get("/api/symbol-performance/TSLA", auth=AUTH)
    body = r.json()

    # Still one open lifecycle (started by the first buy, never fully
    # closed) -- FIFO's realized-to-date P&L on the sell is
    # 10 * (250 - 100) = 1500 against the OLDEST lot's cost, not the $150
    # blended average an average-cost reconstruction would have used
    # (which would've computed 10 * (250 - 150) = 1000).
    assert body["completed_round_trips"] == 0
    assert body["open_position"]["partial_realized_pnl"] == 1500.0
    assert body["methodology"] == "position_lifecycle_fifo"


def test_symbol_performance_flags_qty_mismatch_end_to_end(api_client, conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _insert_trade(conn, "AAPL", "buy", 5, 100.0, 0.0, base)
    _materialize_lifecycles(conn)

    with requests_mock.Mocker() as m:
        # Broker reports more shares than the local ledger knows about --
        # e.g. a pre-ledger holding that was added to locally later.
        m.get("https://fake-alpaca.test/v2/positions/AAPL", json={
            "symbol": "AAPL", "qty": "20", "avg_entry_price": "100.0",
            "current_price": "100.0", "unrealized_pl": "0.0", "market_value": "2000.0",
        })
        r = api_client.get("/api/symbol-performance/AAPL", auth=AUTH)
    body = r.json()
    assert body["reconciliation"]["status"] == "qty_mismatch"
    assert body["reconciliation"]["ledger_status"] == "open"
    assert body["reconciliation"]["broker_status"] == "open"


def test_symbol_performance_flags_partial_methodology_on_oversell(api_client, conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _insert_trade(conn, "AAPL", "buy", 5, 100.0, 0.0, base)
    _insert_trade(conn, "AAPL", "sell", 25, 110.0, 0.0, base + timedelta(days=3))
    _materialize_lifecycles(conn)

    with requests_mock.Mocker() as m:
        m.get("https://fake-alpaca.test/v2/positions/AAPL", status_code=404, json={"message": "not found"})
        r = api_client.get("/api/symbol-performance/AAPL", auth=AUTH)
    body = r.json()
    assert body["methodology_status"] == "partial"
    assert body["unmatched_sell_qty"] == 20.0


def test_symbol_round_trips_endpoint_distinguishes_open_from_closed(api_client, conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _insert_trade(conn, "MSFT", "buy", 10, 200.0, 0.0, base)
    _insert_trade(conn, "MSFT", "sell", 10, 220.0, 0.0, base + timedelta(days=4))
    _insert_trade(conn, "MSFT", "buy", 3, 210.0, 0.0, base + timedelta(days=20))
    _materialize_lifecycles(conn)

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

"""
Tests for ingest.py's reconcile_broker_stop_fills() -- "Execution:
protective stop orders". When a broker-side OTO stop-loss child leg fires
autonomously, it was never submitted through this app's own trade/approval
endpoints, so no trades row exists for it unless this function creates
one -- without it, position_lifecycles (built from the trades ledger)
would silently diverge from the real Alpaca account.
"""

import os
import sys
import pathlib

import psycopg2.extras
import pytest
import requests_mock


def _import_ingest(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ.get(
        "TEST_DATABASE_URL", "postgresql://invest_test:not_a_real_credential@localhost:15432/invest_test"))
    monkeypatch.setenv("ALPACA_BASE_URL", "https://fake-alpaca.test")
    ingest_dir = str(pathlib.Path(__file__).resolve().parent.parent / "ingest")
    if ingest_dir not in sys.path:
        sys.path.insert(0, ingest_dir)
    sys.modules.pop("ingest", None)
    import ingest
    return ingest


def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug='mean_reversion'")
        return cur.fetchone()[0]


def _stop_fill_order(order_id="stop-fill-1", symbol="AAPL", qty="10", price="88.0"):
    return {
        "id": order_id, "symbol": symbol, "status": "filled", "type": "stop",
        "side": "sell", "filled_qty": qty, "filled_avg_price": price,
        "filled_at": "2026-06-01T14:30:00Z",
    }


def test_records_a_new_broker_stop_fill(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    with requests_mock.Mocker() as m:
        m.get("https://fake-alpaca.test/v2/orders", json=[_stop_fill_order()])
        ingest.reconcile_broker_stop_fills(conn)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM trades WHERE order_id='stop-fill-1'")
        trade = cur.fetchone()
    assert trade is not None
    assert trade["side"] == "sell"
    assert float(trade["qty"]) == 10.0
    assert float(trade["price"]) == 88.0
    assert trade["source"] == "broker_stop"
    assert trade["symbol"] == "AAPL"
    assert trade["proposal_id"] is None
    assert trade["thesis_id"] == _mean_reversion_thesis_id(conn)


def test_idempotent_on_rerun(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    with requests_mock.Mocker() as m:
        m.get("https://fake-alpaca.test/v2/orders", json=[_stop_fill_order()])
        ingest.reconcile_broker_stop_fills(conn)
        ingest.reconcile_broker_stop_fills(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trades WHERE order_id='stop-fill-1'")
        assert cur.fetchone()[0] == 1


def test_ignores_non_stop_orders(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    market_order = {
        "id": "market-order-1", "symbol": "AAPL", "status": "filled", "type": "market",
        "side": "sell", "filled_qty": "10", "filled_avg_price": "90.0", "filled_at": "2026-06-01T14:30:00Z",
    }
    with requests_mock.Mocker() as m:
        m.get("https://fake-alpaca.test/v2/orders", json=[market_order])
        ingest.reconcile_broker_stop_fills(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trades")
        assert cur.fetchone()[0] == 0


def test_ignores_unfilled_stop_orders(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    pending_stop = _stop_fill_order()
    pending_stop["status"] = "canceled"
    with requests_mock.Mocker() as m:
        m.get("https://fake-alpaca.test/v2/orders", json=[pending_stop])
        ingest.reconcile_broker_stop_fills(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trades")
        assert cur.fetchone()[0] == 0


def test_alpaca_fetch_failure_does_not_raise(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    with requests_mock.Mocker() as m:
        m.get("https://fake-alpaca.test/v2/orders", status_code=500)
        ingest.reconcile_broker_stop_fills(conn)  # must not raise

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trades")
        assert cur.fetchone()[0] == 0


def test_multiple_new_fills_all_recorded(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    with requests_mock.Mocker() as m:
        m.get("https://fake-alpaca.test/v2/orders", json=[
            _stop_fill_order("stop-fill-a", "AAPL", "10", "88.0"),
            _stop_fill_order("stop-fill-b", "MSFT", "5", "170.0"),
        ])
        ingest.reconcile_broker_stop_fills(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trades")
        assert cur.fetchone()[0] == 2

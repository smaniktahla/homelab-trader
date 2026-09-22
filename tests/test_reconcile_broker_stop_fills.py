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
from datetime import datetime, timedelta, timezone

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


# ---- _stop_fill_lookback_start ----

def _open_lifecycle(conn, symbol, opened_at):
    """Minimal open position_lifecycles row -- only opened_at/status matter
    to _stop_fill_lookback_start(), so nothing else here needs to be
    realistic."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO position_lifecycles (symbol, status, opened_at, qty, entry_notional,
                                              exit_notional, total_cost, gross_pnl, net_pnl)
            VALUES (%s,'open',%s,1,0,0,0,0,0)
        """, (symbol, opened_at))
    conn.commit()


def test_lookback_defaults_to_30d_floor_with_no_open_lifecycles(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    start = ingest._stop_fill_lookback_start(conn)
    expected = datetime.now(timezone.utc) - timedelta(days=30)
    assert abs((start - expected).total_seconds()) < 5


def test_lookback_stays_at_floor_for_a_recently_opened_lifecycle(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    _open_lifecycle(conn, "AAPL", datetime.now(timezone.utc) - timedelta(days=5))
    start = ingest._stop_fill_lookback_start(conn)
    expected = datetime.now(timezone.utc) - timedelta(days=30)
    assert abs((start - expected).total_seconds()) < 5


def test_lookback_extends_past_floor_for_an_old_open_lifecycle(conn, monkeypatch):
    """The 2026-09-20 bug: a lifecycle open longer than the fixed 30-day
    floor (EIX, opened 2026-08-08) needs the window to reach back to its
    own entry, one day of margin included, not just the floor."""
    ingest = _import_ingest(monkeypatch)
    opened_at = datetime.now(timezone.utc) - timedelta(days=45)
    _open_lifecycle(conn, "EIX", opened_at)
    start = ingest._stop_fill_lookback_start(conn)
    assert abs((start - (opened_at - timedelta(days=1))).total_seconds()) < 5


def test_lookback_uses_the_oldest_of_several_open_lifecycles(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    oldest = datetime.now(timezone.utc) - timedelta(days=60)
    _open_lifecycle(conn, "OLD", oldest)
    _open_lifecycle(conn, "NEW", datetime.now(timezone.utc) - timedelta(days=2))
    start = ingest._stop_fill_lookback_start(conn)
    assert abs((start - (oldest - timedelta(days=1))).total_seconds()) < 5


def test_lookback_ignores_closed_lifecycles(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO position_lifecycles (symbol, status, opened_at, closed_at, qty,
                                              entry_notional, exit_notional, total_cost, gross_pnl, net_pnl)
            VALUES ('OLD','closed',%s,%s,1,0,0,0,0,0)
        """, (datetime.now(timezone.utc) - timedelta(days=90), datetime.now(timezone.utc) - timedelta(days=80)))
    conn.commit()
    start = ingest._stop_fill_lookback_start(conn)
    expected = datetime.now(timezone.utc) - timedelta(days=30)
    assert abs((start - expected).total_seconds()) < 5


def test_reconciliation_finds_a_stop_fill_for_a_lifecycle_older_than_30_days(conn, monkeypatch):
    """End-to-end reproduction of the 2026-09-20 gap: a stop fill dated
    within a lifecycle-derived window but outside a fixed 30-day one is
    now found and recorded."""
    ingest = _import_ingest(monkeypatch)
    _open_lifecycle(conn, "EIX", datetime.now(timezone.utc) - timedelta(days=45))
    with requests_mock.Mocker() as m:
        m.get("https://fake-alpaca.test/v2/orders", json=[_stop_fill_order("stop-eix", "EIX", "70", "56.13")])
        ingest.reconcile_broker_stop_fills(conn)
    after = datetime.fromisoformat(m.request_history[0].qs["after"][0].upper().replace("Z", "+00:00"))
    assert (datetime.now(timezone.utc) - after).days >= 44   # well past the old fixed 30d cutoff
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trades WHERE order_id='stop-eix'")
        assert cur.fetchone()[0] == 1


def test_lookback_query_failure_falls_back_to_floor(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    conn.close()   # forces the SELECT inside _stop_fill_lookback_start to raise
    start = ingest._stop_fill_lookback_start(conn)
    expected = datetime.now(timezone.utc) - timedelta(days=30)
    assert abs((start - expected).total_seconds()) < 5

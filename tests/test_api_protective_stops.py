"""
End-to-end tests for "Execution: protective stop orders" -- buy orders now
attach a real broker-side OTO stop-loss child leg (POST /api/trade,
PATCH /api/proposals/{id}), and any unrelated sell cancels a resting
stop-leg order for that symbol first (a resting stop holds shares
"unavailable" at Alpaca, which would otherwise make a legitimate exit
look like it has no shares to sell).
"""

import os
import re
import sys
from datetime import datetime, timezone

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

    for mod in ("main", "rule_adherence", "signals", "risk_engine", "trading_permission", "circuit_breaker"):
        sys.modules.pop(mod, None)
    import main as api_main
    monkeypatch.setattr(api_main.time, "sleep", lambda s: None)
    from fastapi.testclient import TestClient
    return TestClient(api_main.app)


AUTH = ("test_invest_user", "test_invest_pass_not_real")


def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug='mean_reversion'")
        return cur.fetchone()[0]


def _seed_price(conn, symbol, close, ts=None):
    ts = ts or datetime(2026, 6, 1, tzinfo=timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO price_history (symbol, ts, close) VALUES (%s, %s, %s)
            ON CONFLICT (symbol, ts) DO NOTHING
        """, (symbol, ts, close))
    conn.commit()


def _mock_common_alpaca(m, cash=50000.0, portfolio_value=100000.0, positions=None, open_orders=None):
    positions = positions or []
    m.get("https://fake-alpaca.test/v2/account", json={"cash": str(cash), "portfolio_value": str(portfolio_value)})
    m.get("https://fake-alpaca.test/v2/positions", json=positions)
    for pos in positions:
        m.get(f"https://fake-alpaca.test/v2/positions/{pos['symbol']}", json=pos)
    m.get(re.compile(r"https://fake-alpaca\.test/v2/orders/.*"), json={
        "status": "filled", "filled_avg_price": "1.0", "filled_qty": "1",
    })
    # GET /v2/orders?status=open&symbols=... (cancel_resting_stop_orders)
    m.get(re.compile(r"https://fake-alpaca\.test/v2/orders\?.*status=open.*"), json=open_orders or [])
    m.delete(re.compile(r"https://fake-alpaca\.test/v2/orders/.*"), status_code=204)


def _order_post_requests(m):
    return [r for r in m.request_history if r.method == "POST" and r.path == "/v2/orders"]


def _delete_requests(m):
    return [r for r in m.request_history if r.method == "DELETE"]


# ─────────────────────────────────────────────────────────────────────────
# POST /api/trade -- BUY attaches OTO stop_loss
# ─────────────────────────────────────────────────────────────────────────

def test_manual_buy_with_proposal_attaches_oto_stop_from_planned_stop(api_client, conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, thesis_id, planned_entry_price, planned_initial_stop_price)
            VALUES ('AAPL', 'buy', 5, %s, 100.0, 88.0) RETURNING id
        """, (thesis_id,))
        proposal_id = cur.fetchone()[0]
    conn.commit()
    _seed_price(conn, "AAPL", 100.0)

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m, cash=50000.0, portfolio_value=100000.0)
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-1", "status": "filled", "filled_avg_price": "100.0", "filled_qty": "5",
        })
        r = api_client.post("/api/trade", json={
            "symbol": "AAPL", "side": "buy", "qty": 5, "proposal_id": proposal_id,
        }, auth=AUTH)
        assert r.status_code == 200
        orders = _order_post_requests(m)
        body = orders[0].json()
        assert body["order_class"] == "oto"
        assert body["stop_loss"]["stop_price"] == "88.0"


def test_manual_buy_without_proposal_uses_stop_loss_pct_fallback(api_client, conn):
    """No proposal_id at all -- the OTO stop still gets attached, derived
    from the live stop_loss_pct ratio against the reference price (the
    default is 0.08 in this codebase's DEFAULTS, unseeded in the test
    fixture schema) -- previously a manual buy with no proposal had NO
    protective stop at all."""
    _seed_price(conn, "MSFT", 200.0)

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m, cash=50000.0, portfolio_value=100000.0)
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-2", "status": "filled", "filled_avg_price": "200.0", "filled_qty": "5",
        })
        r = api_client.post("/api/trade", json={"symbol": "MSFT", "side": "buy", "qty": 5}, auth=AUTH)
        assert r.status_code == 200
        orders = _order_post_requests(m)
        body = orders[0].json()
        assert body["order_class"] == "oto"
        assert body["stop_loss"]["stop_price"] == str(round(200.0 * (1 - 0.08), 2))


# ─────────────────────────────────────────────────────────────────────────
# POST /api/trade -- SELL cancels resting stop orders first
# ─────────────────────────────────────────────────────────────────────────

def test_manual_sell_cancels_resting_stop_order_before_selling(api_client, conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trades (symbol, side, qty, price, notional, traded_at, cost, status, thesis_id)
            VALUES ('AAPL', 'buy', 10, 100.0, 1000.0, %s, 0, 'filled', %s)
        """, (base, thesis_id))
    conn.commit()

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m, positions=[{
            "symbol": "AAPL", "qty": "10", "avg_entry_price": "100.0",
            "current_price": "150.0", "market_value": "1500.0", "unrealized_plpc": "0.5",
        }], open_orders=[{"id": "stop-order-1", "type": "stop", "symbol": "AAPL"}])
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-3", "status": "filled", "filled_avg_price": "150.0", "filled_qty": "10",
        })
        r = api_client.post("/api/trade", json={"symbol": "AAPL", "side": "sell", "qty": 10}, auth=AUTH)
        assert r.status_code == 200

        deletes = _delete_requests(m)
        assert len(deletes) == 1
        assert deletes[0].path == "/v2/orders/stop-order-1"
        # cancellation happened BEFORE the sell was submitted
        sell_post_index = next(i for i, req in enumerate(m.request_history) if req.method == "POST" and req.path == "/v2/orders")
        delete_index = next(i for i, req in enumerate(m.request_history) if req.method == "DELETE")
        assert delete_index < sell_post_index


def test_manual_sell_with_no_resting_stop_orders_skips_delete(api_client, conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trades (symbol, side, qty, price, notional, traded_at, cost, status, thesis_id)
            VALUES ('AAPL', 'buy', 10, 100.0, 1000.0, %s, 0, 'filled', %s)
        """, (base, thesis_id))
    conn.commit()

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m, positions=[{
            "symbol": "AAPL", "qty": "10", "avg_entry_price": "100.0",
            "current_price": "150.0", "market_value": "1500.0", "unrealized_plpc": "0.5",
        }], open_orders=[])
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-4", "status": "filled", "filled_avg_price": "150.0", "filled_qty": "10",
        })
        r = api_client.post("/api/trade", json={"symbol": "AAPL", "side": "sell", "qty": 10}, auth=AUTH)
        assert r.status_code == 200
        assert _delete_requests(m) == []


def test_manual_buy_order_has_no_delete_calls(api_client, conn):
    """A buy never needs to cancel anything -- cancel_resting_stop_orders
    is sell-only."""
    _seed_price(conn, "AAPL", 100.0)
    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m, cash=50000.0, portfolio_value=100000.0)
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-5", "status": "filled", "filled_avg_price": "100.0", "filled_qty": "5",
        })
        r = api_client.post("/api/trade", json={"symbol": "AAPL", "side": "buy", "qty": 5}, auth=AUTH)
        assert r.status_code == 200
        assert _delete_requests(m) == []


# ─────────────────────────────────────────────────────────────────────────
# PATCH /api/proposals/{id}
# ─────────────────────────────────────────────────────────────────────────

def test_proposal_approval_buy_attaches_oto_stop(api_client, conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, thesis_id, planned_entry_price, planned_initial_stop_price)
            VALUES ('MSFT', 'buy', 5, %s, 200.0, 176.0) RETURNING id
        """, (thesis_id,))
        proposal_id = cur.fetchone()[0]
    conn.commit()

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m, cash=50000.0, portfolio_value=100000.0)
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-6", "status": "filled", "filled_avg_price": "200.0", "filled_qty": "5",
        })
        r = api_client.patch(f"/api/proposals/{proposal_id}", json={"decision": "approved"}, auth=AUTH)
        assert r.status_code == 200
        orders = _order_post_requests(m)
        body = orders[0].json()
        assert body["order_class"] == "oto"
        assert body["stop_loss"]["stop_price"] == "176.0"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT initial_stop_price FROM trades WHERE proposal_id=%s", (proposal_id,))
        trade = cur.fetchone()
    assert float(trade["initial_stop_price"]) == 176.0


def test_proposal_approval_sell_cancels_resting_stop_before_availability_check(api_client, conn):
    """The resting stop order holds all 10 shares "unavailable" -- if
    cancellation didn't run BEFORE the qty_available check, this exit
    would incorrectly 400 with "No available long position"."""
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, thesis_id, exit_reason)
            VALUES ('AAPL', 'sell', 10, %s, 'thesis_complete') RETURNING id
        """, (thesis_id,))
        proposal_id = cur.fetchone()[0]
    conn.commit()

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m, positions=[{
            "symbol": "AAPL", "qty": "10", "qty_available": "10", "avg_entry_price": "100.0",
            "current_price": "150.0", "market_value": "1500.0", "unrealized_plpc": "0.5",
        }], open_orders=[{"id": "stop-order-2", "type": "stop", "symbol": "AAPL"}])
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-7", "status": "filled", "filled_avg_price": "150.0", "filled_qty": "10",
        })
        r = api_client.patch(f"/api/proposals/{proposal_id}", json={"decision": "approved"}, auth=AUTH)
        assert r.status_code == 200
        deletes = _delete_requests(m)
        assert len(deletes) == 1
        assert deletes[0].path == "/v2/orders/stop-order-2"

"""
End-to-end tests of the risk engine's binding integration into POST
/api/trade and PATCH /api/proposals/{id} -- see
docs/risk-engine-architecture-reconciliation.md section C.1. Unlike
rule_adherence (advisory, tested in test_api_rule_adherence.py), the risk
engine is authoritative: it clamps the qty actually sent to Alpaca, and
can reject a trade outright (400) before any order is submitted.
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

    for mod in ("main", "rule_adherence", "signals", "risk_engine"):
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


def _mock_common_alpaca(m, cash=50000.0, portfolio_value=100000.0, positions=None, order_status="filled"):
    m.get("https://fake-alpaca.test/v2/account", json={"cash": str(cash), "portfolio_value": str(portfolio_value)})
    m.get("https://fake-alpaca.test/v2/positions", json=positions or [])
    m.get(re.compile(r"https://fake-alpaca\.test/v2/orders/.*"), json={
        "status": order_status, "filled_avg_price": "1.0", "filled_qty": "1",
    })


def _order_post_requests(m):
    return [r for r in m.request_history if r.method == "POST" and r.path == "/v2/orders"]


def _risk_decision_row(conn, symbol):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM risk_decisions WHERE symbol=%s ORDER BY id DESC LIMIT 1
        """, (symbol,))
        return cur.fetchone()


# ─────────────────────────────────────────────────────────────────────────
# POST /api/trade
# ─────────────────────────────────────────────────────────────────────────

def test_manual_buy_clamps_qty_sent_to_alpaca(api_client, conn):
    """Requesting 900 shares at $100 with a 20%-of-100k position cap must
    submit a CLAMPED qty to Alpaca (200, not 900) -- this is the behavior
    change from advisory-only rule_adherence to a binding risk engine."""
    _seed_price(conn, "AAPL", 100.0)
    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m, cash=50000.0, portfolio_value=100000.0)
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-1", "status": "filled", "filled_avg_price": "100.0", "filled_qty": "200",
        })
        r = api_client.post("/api/trade", json={"symbol": "AAPL", "side": "buy", "qty": 900}, auth=AUTH)
        assert r.status_code == 200
        orders = _order_post_requests(m)
        assert len(orders) == 1
        assert orders[0].json()["qty"] == "200"

    body = r.json()
    assert body["risk_decision"]["requested_qty"] == 900
    assert body["risk_decision"]["approved_quantity"] == 200
    assert body["risk_decision"]["outcome"] == "reduced"
    assert body["risk_decision"]["binding_constraint"] == "position_allocation"


def test_manual_buy_rejected_by_risk_engine_places_no_order(api_client, conn):
    """Zero buying power -> the risk engine rejects outright, and no order
    is ever submitted to Alpaca at all."""
    _seed_price(conn, "AAPL", 100.0)
    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m, cash=0.0, portfolio_value=100000.0)
        m.post("https://fake-alpaca.test/v2/orders", json={"id": "should-not-be-called"})
        r = api_client.post("/api/trade", json={"symbol": "AAPL", "side": "buy", "qty": 10}, auth=AUTH)
        assert r.status_code == 400
        assert len(_order_post_requests(m)) == 0

    row = _risk_decision_row(conn, "AAPL")
    assert row is not None
    assert row["outcome"] == "rejected"
    assert row["approved_quantity"] == 0
    assert row["context"] == "manual_trade"


def test_manual_buy_without_price_history_400s_before_any_order(api_client, conn):
    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m)
        m.post("https://fake-alpaca.test/v2/orders", json={"id": "should-not-be-called"})
        r = api_client.post("/api/trade", json={"symbol": "ZZZZ", "side": "buy", "qty": 10}, auth=AUTH)
        assert r.status_code == 400
        assert len(_order_post_requests(m)) == 0


def test_manual_sell_is_not_clamped_by_risk_engine(api_client, conn):
    """Sells were never in scope for the risk engine (position sizing is a
    buy-side concept) -- a sell for the full held qty must pass through
    unclamped, same as before this PR."""
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trades (symbol, side, qty, price, notional, traded_at, cost, status, thesis_id)
            VALUES ('AAPL', 'buy', 500, 100.0, 50000.0, %s, 0, 'filled', %s)
        """, (base, thesis_id))
    conn.commit()

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m, positions=[{
            "symbol": "AAPL", "qty": "500", "avg_entry_price": "100.0",
            "current_price": "150.0", "market_value": "75000.0", "unrealized_plpc": "0.5",
        }])
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-2", "status": "filled", "filled_avg_price": "150.0", "filled_qty": "500",
        })
        r = api_client.post("/api/trade", json={"symbol": "AAPL", "side": "sell", "qty": 500}, auth=AUTH)
        assert r.status_code == 200
        orders = _order_post_requests(m)
        assert orders[0].json()["qty"] == "500.0"  # TradeRequest.qty is a float field
    assert "risk_decision" not in r.json()


# ─────────────────────────────────────────────────────────────────────────
# PATCH /api/proposals/{id}
# ─────────────────────────────────────────────────────────────────────────

def test_proposal_approval_clamps_qty_sent_to_alpaca(api_client, conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, thesis_id, planned_entry_price)
            VALUES ('MSFT', 'buy', 900, %s, 100.0) RETURNING id
        """, (thesis_id,))
        proposal_id = cur.fetchone()[0]
    conn.commit()

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m, cash=50000.0, portfolio_value=100000.0)
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-3", "status": "filled", "filled_avg_price": "100.0", "filled_qty": "200",
        })
        # Overrides _mock_common_alpaca's generic order-status stub (which
        # always returns filled_qty="1") so the synchronous _reconcile_fill
        # background task doesn't clobber the trade row back down to 1 share
        # -- this test cares specifically about the persisted qty.
        m.get("https://fake-alpaca.test/v2/orders/order-3", json={
            "status": "filled", "filled_avg_price": "100.0", "filled_qty": "200",
        })
        r = api_client.patch(f"/api/proposals/{proposal_id}", json={"decision": "approved"}, auth=AUTH)
        assert r.status_code == 200
        orders = _order_post_requests(m)
        assert orders[0].json()["qty"] == "200"

    body = r.json()
    assert body["risk_decision"]["approved_quantity"] == 200
    assert body["risk_decision"]["outcome"] == "reduced"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT qty FROM trades WHERE proposal_id=%s", (proposal_id,))
        trade = cur.fetchone()
    assert float(trade["qty"]) == 200.0


def test_proposal_approval_with_planned_stop_price_does_not_crash(api_client, conn):
    """Regression test: a real compute_signals()-generated buy proposal
    always has planned_initial_stop_price set (unlike every other test in
    this file, which only sets planned_entry_price) -- psycopg2 returns
    that NUMERIC column as decimal.Decimal, and the risk engine's
    price - planned_initial_stop_price crashed with "unsupported operand
    type(s) for -: 'float' and 'decimal.Decimal'" when planned_stop_price
    wasn't converted to float before being passed through. Caught live in
    production (screenshot from the dashboard) -- no existing test had a
    non-null planned_initial_stop_price on the approval path at all."""
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, thesis_id, planned_entry_price, planned_initial_stop_price)
            VALUES ('DVA', 'buy', 25, %s, 185.43, 163.28) RETURNING id
        """, (thesis_id,))
        proposal_id = cur.fetchone()[0]
    conn.commit()

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m, cash=50000.0, portfolio_value=100000.0)
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-dva", "status": "filled", "filled_avg_price": "185.43", "filled_qty": "25",
        })
        r = api_client.patch(f"/api/proposals/{proposal_id}", json={"decision": "approved"}, auth=AUTH)

    assert r.status_code == 200
    assert r.json()["risk_decision"]["outcome"] in ("approved", "reduced")


def test_manual_buy_with_planned_stop_price_does_not_crash(api_client, conn):
    """Same regression as above, via POST /api/trade's proposal_id path."""
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, thesis_id, planned_entry_price, planned_initial_stop_price)
            VALUES ('DVA', 'buy', 25, %s, 185.43, 163.28) RETURNING id
        """, (thesis_id,))
        proposal_id = cur.fetchone()[0]
    conn.commit()
    _seed_price(conn, "DVA", 185.43)

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m, cash=50000.0, portfolio_value=100000.0)
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-dva2", "status": "filled", "filled_avg_price": "185.43", "filled_qty": "25",
        })
        r = api_client.post("/api/trade", json={
            "symbol": "DVA", "side": "buy", "qty": 25, "proposal_id": proposal_id,
        }, auth=AUTH)

    assert r.status_code == 200
    assert r.json()["risk_decision"]["outcome"] in ("approved", "reduced")


def test_proposal_approval_human_override_qty_is_still_clamped(api_client, conn):
    """A human explicitly overriding qty at approval time (body.qty) is the
    exact bypass path section C.1 of the reconciliation doc identifies --
    this must be clamped the same as the proposal's own default qty."""
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, thesis_id, planned_entry_price)
            VALUES ('MSFT', 'buy', 5, %s, 100.0) RETURNING id
        """, (thesis_id,))
        proposal_id = cur.fetchone()[0]
    conn.commit()

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m, cash=50000.0, portfolio_value=100000.0)
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-4", "status": "filled", "filled_avg_price": "100.0", "filled_qty": "200",
        })
        r = api_client.patch(f"/api/proposals/{proposal_id}", json={"decision": "approved", "qty": 5000}, auth=AUTH)
        assert r.status_code == 200
        orders = _order_post_requests(m)
        assert orders[0].json()["qty"] == "200"  # NOT 5000


def test_proposal_rejected_by_risk_engine_returns_400_proposal_stays_undecided(api_client, conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, thesis_id, planned_entry_price)
            VALUES ('MSFT', 'buy', 10, %s, 100.0) RETURNING id
        """, (thesis_id,))
        proposal_id = cur.fetchone()[0]
    conn.commit()

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m, cash=0.0, portfolio_value=100000.0)
        m.post("https://fake-alpaca.test/v2/orders", json={"id": "should-not-be-called"})
        r = api_client.patch(f"/api/proposals/{proposal_id}", json={"decision": "approved"}, auth=AUTH)
        assert r.status_code == 400
        assert len(_order_post_requests(m)) == 0

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT decision FROM trade_proposals WHERE id=%s", (proposal_id,))
        row = cur.fetchone()
    assert row["decision"] is None  # never got marked approved/rejected

    row = _risk_decision_row(conn, "MSFT")
    assert row["outcome"] == "rejected"
    assert row["context"] == "proposal_approval"

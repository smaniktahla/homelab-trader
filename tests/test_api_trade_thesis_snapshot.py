"""
PR 9 of the Hypothesis-Driven Trading Architecture epic -- "Thesis
snapshot on proposal/position." trade_proposals.trade_thesis_id and
trades.trade_thesis_id already existed (PR 1); this closes the gap that
was left open -- api/main.py now actually copies trade_thesis_id down
from trade_proposals to trades at fill time, through both trade-creation
endpoints (POST /api/trade with a proposal_id, and PATCH /api/proposals/
{id} approval), same pattern as the existing thesis_id/initial_stop_price
copy-down.

Same api_client fixture as tests/test_api_protective_stops.py.
"""

import os
import sys
from datetime import datetime, timezone

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


def _seed_trade_thesis(conn, thesis_id, symbol="AAPL"):
    from trade_thesis import TradeThesis, record_trade_thesis
    thesis = TradeThesis(
        thesis_id=thesis_id, symbol=symbol,
        hypothesis_type="mean_reversion_oversold", hypothesis_text="test seed",
        entry_conditions={"feature": "technical.rsi_14", "op": "lt", "value": 30},
        invalidation_spec={"feature": "technical.close", "op": "lt", "value": 1.0},
        success_spec={"feature": "technical.bb_pct_b", "op": "gte", "value": 0.5},
        evidence_context={
            "as_of": "2026-06-01",
            "providers": {"technical": {"source": "symbol_features", "feature_version": "v1"}},
        },
        provenance={"entry_conditions": "explicit"},
        as_of=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    row_id = record_trade_thesis(conn, thesis)
    assert row_id is not None
    return row_id


def _mock_common_alpaca(m, cash=50000.0, portfolio_value=100000.0):
    m.get("https://fake-alpaca.test/v2/account", json={"cash": str(cash), "portfolio_value": str(portfolio_value)})
    m.get("https://fake-alpaca.test/v2/positions", json=[])


def _trade_thesis_id_of_last_trade(conn, symbol):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT trade_thesis_id FROM trades WHERE symbol=%s ORDER BY id DESC LIMIT 1
        """, (symbol,))
        return cur.fetchone()[0]


def test_execute_trade_copies_trade_thesis_id_from_linked_proposal(api_client, conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    trade_thesis_id = _seed_trade_thesis(conn, thesis_id)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, thesis_id, trade_thesis_id, planned_entry_price, planned_initial_stop_price)
            VALUES ('AAPL', 'buy', 5, %s, %s, 100.0, 88.0) RETURNING id
        """, (thesis_id, trade_thesis_id))
        proposal_id = cur.fetchone()[0]
    conn.commit()
    _seed_price(conn, "AAPL", 100.0)

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m)
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-1", "status": "filled", "filled_avg_price": "100.0", "filled_qty": "5",
        })
        r = api_client.post("/api/trade", json={
            "symbol": "AAPL", "side": "buy", "qty": 5, "proposal_id": proposal_id,
        }, auth=AUTH)
        assert r.status_code == 200

    assert _trade_thesis_id_of_last_trade(conn, "AAPL") == trade_thesis_id


def test_execute_trade_without_proposal_leaves_trade_thesis_id_null(api_client, conn):
    _seed_price(conn, "MSFT", 200.0)

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m)
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-2", "status": "filled", "filled_avg_price": "200.0", "filled_qty": "5",
        })
        r = api_client.post("/api/trade", json={"symbol": "MSFT", "side": "buy", "qty": 5}, auth=AUTH)
        assert r.status_code == 200

    assert _trade_thesis_id_of_last_trade(conn, "MSFT") is None


def test_execute_trade_proposal_with_no_trade_thesis_id_leaves_trade_null(api_client, conn):
    # Linked proposal exists but was never instantiated (trade_thesis_id
    # IS NULL on it) -- must not fabricate one.
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, thesis_id, planned_entry_price, planned_initial_stop_price)
            VALUES ('NVDA', 'buy', 5, %s, 100.0, 88.0) RETURNING id
        """, (thesis_id,))
        proposal_id = cur.fetchone()[0]
    conn.commit()
    _seed_price(conn, "NVDA", 100.0)

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m)
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-3", "status": "filled", "filled_avg_price": "100.0", "filled_qty": "5",
        })
        r = api_client.post("/api/trade", json={
            "symbol": "NVDA", "side": "buy", "qty": 5, "proposal_id": proposal_id,
        }, auth=AUTH)
        assert r.status_code == 200

    assert _trade_thesis_id_of_last_trade(conn, "NVDA") is None


def test_decide_proposal_approval_copies_trade_thesis_id(api_client, conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    trade_thesis_id = _seed_trade_thesis(conn, thesis_id, symbol="TSLA")
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, thesis_id, trade_thesis_id, planned_entry_price, planned_initial_stop_price)
            VALUES ('TSLA', 'buy', 3, %s, %s, 200.0, 176.0) RETURNING id
        """, (thesis_id, trade_thesis_id))
        proposal_id = cur.fetchone()[0]
    conn.commit()
    _seed_price(conn, "TSLA", 200.0)

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m)
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-4", "status": "filled", "filled_avg_price": "200.0", "filled_qty": "3",
        })
        r = api_client.patch(f"/api/proposals/{proposal_id}", json={"decision": "approved"}, auth=AUTH)
        assert r.status_code == 200

    assert _trade_thesis_id_of_last_trade(conn, "TSLA") == trade_thesis_id

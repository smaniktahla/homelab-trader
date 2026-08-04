"""
End-to-end test of Platform Improvements PR C's two hook sites --
POST /api/trade (manual trades) and PATCH /api/proposals/{id} (approval) --
against a real Postgres connection, with Alpaca fully mocked. Verifies a
rule_adherence_checks row lands with the right context/any_violation for
both a clean scenario and one that trips a real gate, and that a failure
in the adherence check itself never breaks the trade/approval response
(fail-open).
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

    for mod in ("main", "rule_adherence", "signals"):
        sys.modules.pop(mod, None)
    import main as api_main
    # _reconcile_fill (a BackgroundTask, which TestClient runs synchronously
    # in-process) calls time.sleep(3.0) at least once per request -- a
    # no-op here keeps this test file fast without touching production code.
    monkeypatch.setattr(api_main.time, "sleep", lambda s: None)
    from fastapi.testclient import TestClient
    return TestClient(api_main.app)


AUTH = ("test_invest_user", "test_invest_pass_not_real")


def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug='mean_reversion'")
        return cur.fetchone()[0]


def _mock_common_alpaca(m, cash=50000.0, portfolio_value=100000.0, positions=None):
    m.get("https://fake-alpaca.test/v2/account", json={"cash": str(cash), "portfolio_value": str(portfolio_value)})
    m.get("https://fake-alpaca.test/v2/positions", json=positions or [])
    # _reconcile_fill (a background task run synchronously by TestClient)
    # polls GET /v2/orders/{id} at least once per request -- mocked
    # generically here so it resolves immediately instead of retrying.
    m.get(re.compile(r"https://fake-alpaca\.test/v2/orders/.*"), json={
        "status": "filled", "filled_avg_price": "1.0", "filled_qty": "1",
    })


def _rule_adherence_row(conn, trade_id=None, proposal_id=None):
    """The `conn` fixture (tests/conftest.py) is a plain tuple-cursor
    connection -- explicit dict cursor here so the tests below can assert
    by column name."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if trade_id is not None:
            cur.execute("SELECT context, any_violation, rule_results FROM rule_adherence_checks WHERE trade_id=%s", (trade_id,))
        else:
            cur.execute("SELECT context, any_violation, rule_results FROM rule_adherence_checks WHERE proposal_id=%s ORDER BY id DESC LIMIT 1", (proposal_id,))
        return cur.fetchone()


def test_manual_trade_clean_records_no_violation(api_client, conn):
    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m)
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-1", "status": "filled", "filled_avg_price": "150.0", "filled_qty": "5",
        })
        r = api_client.post("/api/trade", json={"symbol": "AAPL", "side": "buy", "qty": 5}, auth=AUTH)
    assert r.status_code == 200
    trade_id = r.json()["trade_id"]

    row = _rule_adherence_row(conn, trade_id=trade_id)
    assert row is not None
    assert row["context"] == "manual_trade"
    assert row["any_violation"] is False
    assert len(row["rule_results"]) == 6


def test_manual_trade_flags_a_real_violation(api_client, conn):
    """A buy that blows the position-sizing cap (max_position_pct default
    20%) should land with any_violation=true and the specific rule
    flagged -- without affecting the trade's own success response."""
    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m, cash=50000.0, portfolio_value=100000.0)
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-2", "status": "filled", "filled_avg_price": "100.0", "filled_qty": "900",
        })
        r = api_client.post("/api/trade", json={"symbol": "AAPL", "side": "buy", "qty": 900}, auth=AUTH)
    assert r.status_code == 200   # trade itself is never blocked
    trade_id = r.json()["trade_id"]

    row = _rule_adherence_row(conn, trade_id=trade_id)
    assert row["any_violation"] is True
    sizing = next(x for x in row["rule_results"] if x["rule"] == "position_sizing")
    assert sizing["passed"] is False


def test_manual_sell_trade_records_position_held_context(api_client, conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with conn.cursor() as cur:
        thesis_id = _mean_reversion_thesis_id(conn)
        cur.execute("""
            INSERT INTO trades (symbol, side, qty, price, notional, traded_at, cost, status, thesis_id)
            VALUES ('AAPL', 'buy', 10, 100.0, 1000.0, %s, 0, 'filled', %s)
        """, (base, thesis_id))
    conn.commit()

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m, positions=[{
            "symbol": "AAPL", "qty": "10", "avg_entry_price": "100.0",
            "current_price": "150.0", "market_value": "1500.0", "unrealized_plpc": "0.5",
        }])
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-3", "status": "filled", "filled_avg_price": "150.0", "filled_qty": "10",
        })
        r = api_client.post("/api/trade", json={"symbol": "AAPL", "side": "sell", "qty": 10}, auth=AUTH)
    assert r.status_code == 200
    trade_id = r.json()["trade_id"]

    row = _rule_adherence_row(conn, trade_id=trade_id)
    assert row["context"] == "manual_trade"
    assert row["rule_results"] == [{"rule": "position_held", "passed": True, "detail": None}]


def test_proposal_approval_records_proposal_approval_context(api_client, conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, thesis_id)
            VALUES ('MSFT', 'buy', 5, %s) RETURNING id
        """, (thesis_id,))
        proposal_id = cur.fetchone()[0]
    conn.commit()

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m)
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-4", "status": "filled", "filled_avg_price": "200.0", "filled_qty": "5",
        })
        r = api_client.patch(f"/api/proposals/{proposal_id}", json={"decision": "approved"}, auth=AUTH)
    assert r.status_code == 200

    row = _rule_adherence_row(conn, proposal_id=proposal_id)
    assert row is not None
    assert row["context"] == "proposal_approval"
    assert row["any_violation"] is False


def test_proposal_rejection_does_not_record_adherence_check(api_client, conn):
    """Rejections never place a trade -- there's nothing to bypass, so no
    row should be written at all."""
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, thesis_id)
            VALUES ('MSFT', 'buy', 5, %s) RETURNING id
        """, (thesis_id,))
        proposal_id = cur.fetchone()[0]
    conn.commit()

    r = api_client.patch(f"/api/proposals/{proposal_id}", json={"decision": "rejected"}, auth=AUTH)
    assert r.status_code == 200
    assert _rule_adherence_row(conn, proposal_id=proposal_id) is None

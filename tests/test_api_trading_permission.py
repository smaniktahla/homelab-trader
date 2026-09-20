"""
Trading-permission manual override API (2026-09-19). GET /api/trading-
permission, POST /api/trading-permission/override,
POST /api/trading-permission/override/{id}/revoke. Uses api/main.py's own
plain psycopg2.connect(DB_DSN) (not db()'s RealDictCursor), same
reasoning as the hypothesis-types/candidates/strategy-lifecycle endpoints'
own test files. GET /api/trading-permission calls fetch_alpaca_portfolio()
so every test mocks the same Alpaca endpoints test_api_proposals_ranking.py
already established the pattern for.
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


def _mock_common_alpaca(m, cash=100000.0, portfolio_value=100000.0):
    m.get("https://fake-alpaca.test/v2/account",
          json={"cash": str(cash), "buying_power": str(cash), "portfolio_value": str(portfolio_value)})
    m.get("https://fake-alpaca.test/v2/positions", json=[])
    m.get("https://fake-alpaca.test/v2/orders", json=[])


def _seed_loss_streak(conn, n=4):
    # Recent, not a fixed old date: loss_streak_window_days (default 7) ages out old losses.
    base = datetime.now(timezone.utc) - timedelta(days=n + 1)
    with conn.cursor() as cur:
        for i in range(n):
            cur.execute("""
                INSERT INTO position_lifecycles (symbol, status, opened_at, closed_at, qty, net_pnl)
                VALUES (%s, 'closed', %s, %s, 10, -10.0)
            """, (f"SYM{i}", base + timedelta(days=i - 1), base + timedelta(days=i)))
    conn.commit()


def test_get_trading_permission_allowed_when_no_conditions_met(api_client, conn):
    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m)
        r = api_client.get("/api/trading-permission", auth=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    # breadth trigger is on by default (loaded from signal_params), so its detail rides along
    assert body == {"new_entries_allowed": True, "scope": "account", "reasons": [], "override": None,
                    "breadth": {"in_streak": 0, "held": 0, "symbols": []}}


def test_get_trading_permission_blocked_by_loss_streak(api_client, conn):
    _seed_loss_streak(conn)
    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m)
        r = api_client.get("/api/trading-permission", auth=AUTH)
    body = r.json()
    assert body["new_entries_allowed"] is False
    assert body["reasons"] == ["loss_streak_limit"]


def test_create_override_requires_confirm_true(api_client, conn):
    r = api_client.post("/api/trading-permission/override", auth=AUTH, json={
        "reason": "reviewed the losses, resuming manually", "actor": "salil", "confirm": False,
    })
    assert r.status_code == 422


def test_create_override_requires_non_empty_reason(api_client, conn):
    r = api_client.post("/api/trading-permission/override", auth=AUTH, json={
        "reason": "", "actor": "salil", "confirm": True,
    })
    assert r.status_code == 422


def test_create_override_unblocks_entries(api_client, conn):
    _seed_loss_streak(conn)
    r = api_client.post("/api/trading-permission/override", auth=AUTH, json={
        "reason": "reviewed the losses, resuming manually", "actor": "salil", "confirm": True,
    })
    assert r.status_code == 200, r.text
    override_id = r.json()["id"]

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m)
        get_r = api_client.get("/api/trading-permission", auth=AUTH)
    body = get_r.json()
    assert body["new_entries_allowed"] is True
    assert body["reasons"] == ["loss_streak_limit"]  # still reported
    assert body["override"]["id"] == override_id
    assert body["override"]["created_by"] == "salil"


def test_revoke_override_reblocks_entries(api_client, conn):
    _seed_loss_streak(conn)
    create_r = api_client.post("/api/trading-permission/override", auth=AUTH, json={
        "reason": "resuming manually", "actor": "salil", "confirm": True,
    })
    override_id = create_r.json()["id"]

    revoke_r = api_client.post(f"/api/trading-permission/override/{override_id}/revoke", auth=AUTH, json={
        "actor": "salil",
    })
    assert revoke_r.status_code == 200, revoke_r.text

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m)
        get_r = api_client.get("/api/trading-permission", auth=AUTH)
    assert get_r.json()["new_entries_allowed"] is False
    assert get_r.json()["override"] is None


def test_revoke_unknown_override_422(api_client, conn):
    r = api_client.post("/api/trading-permission/override/999999/revoke", auth=AUTH, json={"actor": "salil"})
    assert r.status_code == 422


def test_trading_permission_endpoints_require_auth(api_client, conn):
    assert api_client.get("/api/trading-permission").status_code == 401
    assert api_client.post("/api/trading-permission/override",
                            json={"reason": "x", "actor": "y", "confirm": True}).status_code == 401
    assert api_client.post("/api/trading-permission/override/1/revoke", json={"actor": "y"}).status_code == 401


# ─────────────────────────────────────────────────────────────────────────
# Loss-streak attribution enforcement, POST /api/trade (2026-09-19)
# ─────────────────────────────────────────────────────────────────────────

def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug='mean_reversion'")
        return cur.fetchone()[0]


def _seed_position(conn, symbol, qty=10):
    """A minimal open position (via a prior buy trade) so a manual sell
    against it has real cost basis and something to actually close."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trades (symbol, side, qty, price, traded_at, source, thesis_id, counts_toward_loss_streak)
            VALUES (%s, 'buy', %s, 100.0, NOW() - interval '5 days', 'model_approved', %s, TRUE)
        """, (symbol, qty, _mean_reversion_thesis_id(conn)))
    conn.commit()


def test_manual_sell_without_counts_toward_loss_streak_422(api_client, conn):
    _seed_position(conn, "ATTR1")
    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m)
        r = api_client.post("/api/trade", auth=AUTH, json={
            "symbol": "ATTR1", "side": "sell", "qty": 10, "source": "manual",
        })
    assert r.status_code == 422


def test_manual_sell_with_counts_toward_loss_streak_succeeds(api_client, conn):
    _seed_position(conn, "ATTR2")
    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m)
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-attr2", "status": "filled", "filled_avg_price": "95.0", "filled_qty": "10",
        })
        r = api_client.post("/api/trade", auth=AUTH, json={
            "symbol": "ATTR2", "side": "sell", "qty": 10, "source": "manual",
            "counts_toward_loss_streak": False,
        })
    assert r.status_code == 200, r.text

    with conn.cursor() as cur:
        cur.execute("SELECT counts_toward_loss_streak FROM trades WHERE symbol='ATTR2' AND side='sell'")
        assert cur.fetchone()[0] is False


def test_non_manual_sell_does_not_require_counts_toward_loss_streak(api_client, conn):
    _seed_position(conn, "ATTR3")
    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m)
        m.post("https://fake-alpaca.test/v2/orders", json={
            "id": "order-attr3", "status": "filled", "filled_avg_price": "95.0", "filled_qty": "10",
        })
        r = api_client.post("/api/trade", auth=AUTH, json={
            "symbol": "ATTR3", "side": "sell", "qty": 10, "source": "advisor_stop_loss",
        })
    assert r.status_code == 200, r.text

    with conn.cursor() as cur:
        cur.execute("SELECT counts_toward_loss_streak FROM trades WHERE symbol='ATTR3' AND side='sell'")
        assert cur.fetchone()[0] is True  # always forced True for a non-manual source

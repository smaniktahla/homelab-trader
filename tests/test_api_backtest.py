"""
PR 17, Hypothesis-Driven Trading Architecture epic. Backtest visualization
endpoints -- GET /api/backtest-strategies, GET /api/backtest/{strategy_key}/
{symbol}, GET /backtest/{strategy_key}/{symbol} (HTML page). Uses
api/main.py's own plain psycopg2.connect(DB_DSN) (not db()'s
RealDictCursor) -- see the endpoints' comment in api/main.py for why.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest


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
SYMBOL = "BKTEST"


def _seed_price_history(conn, symbol, n=40, start_price=100.0):
    # Anchored to "now" (not a fixed historical date) since the endpoint
    # under test queries load_bars() relative to datetime.now(timezone.utc).
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=n)
    with conn.cursor() as cur:
        price = start_price
        for i in range(n):
            ts = start + timedelta(days=i)
            price += 1.0 if i % 3 else -0.5
            cur.execute(
                "INSERT INTO price_history (symbol, ts, open, high, low, close, volume) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (symbol, ts) DO NOTHING",
                (symbol, ts, price - 0.5, price + 1, price - 1, price, 1000),
            )
    conn.commit()


def test_list_backtest_strategies(api_client, conn):
    r = api_client.get("/api/backtest-strategies", auth=AUTH)
    assert r.status_code == 200
    keys = {s["key"] for s in r.json()}
    assert keys == {"bollinger_breakout_continuation", "ema_crossover_trend"}


def test_run_backtest_returns_bars_overlays_signals_fills(api_client, conn):
    _seed_price_history(conn, SYMBOL)
    r = api_client.get(f"/api/backtest/bollinger_breakout_continuation/{SYMBOL}?days=60", auth=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == SYMBOL
    assert body["strategy"] == "bollinger_breakout_continuation"
    assert body["execution_timing"] == "next_bar_open"
    assert len(body["bars"]) > 0
    assert {o["name"] for o in body["overlays"]} == {"bb_upper", "bb_middle", "bb_lower"}
    assert isinstance(body["signals"], list)
    assert isinstance(body["fills"], list)
    # signals and fills are structurally distinct lists, not aliases of
    # each other -- even when both are empty for this fixture, the keys
    # must be present and independently typed.
    assert "trade_count" in body and "win_rate" in body and "total_pnl" in body


def test_run_backtest_with_ema_crossover_strategy(api_client, conn):
    _seed_price_history(conn, SYMBOL, n=50)
    r = api_client.get(f"/api/backtest/ema_crossover_trend/{SYMBOL}?days=60", auth=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {o["name"] for o in body["overlays"]} == {"ema_20", "ema_21"}


def test_run_backtest_404_for_unknown_strategy(api_client, conn):
    _seed_price_history(conn, SYMBOL)
    r = api_client.get(f"/api/backtest/not_a_real_strategy/{SYMBOL}", auth=AUTH)
    assert r.status_code == 404


def test_run_backtest_404_for_symbol_with_no_price_history(api_client, conn):
    r = api_client.get("/api/backtest/bollinger_breakout_continuation/NODATA", auth=AUTH)
    assert r.status_code == 404


def test_run_backtest_requires_auth(api_client, conn):
    r = api_client.get(f"/api/backtest/bollinger_breakout_continuation/{SYMBOL}")
    assert r.status_code == 401


def test_list_backtest_strategies_requires_auth(api_client, conn):
    r = api_client.get("/api/backtest-strategies")
    assert r.status_code == 401

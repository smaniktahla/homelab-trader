"""
GET /api/signals had zero test coverage before this fix. Covers the new
optional `symbol` filter (added so a single quiet symbol's older signals
aren't pushed out of the unfiltered global `limit` window -- see the
endpoint's own docstring in api/main.py) alongside the pre-existing
unfiltered behavior, to make sure that's unchanged.
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


def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug='mean_reversion'")
        return cur.fetchone()[0]


def _insert_signal(conn, symbol, signal_type, score, generated_at):
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO signals (symbol, signal_type, score, rationale, generated_at, thesis_id)
            VALUES (%s, %s, %s, 'test', %s, %s)
        """, (symbol, signal_type, score, generated_at, thesis_id))
    conn.commit()


def test_symbol_filter_returns_only_that_symbols_signals(api_client, conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # A noisy symbol with many recent signals that would otherwise crowd a
    # quiet symbol's older signal out of an unfiltered `limit` window.
    for i in range(20):
        _insert_signal(conn, "NOISY", "rsi_mr_buy", 40, base + timedelta(days=100 + i))
    _insert_signal(conn, "GLW", "rsi_mr_buy", 33, base)

    r = api_client.get("/api/signals?symbol=GLW&limit=10", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["symbol"] == "GLW"


def test_symbol_filter_is_case_insensitive(api_client, conn):
    _insert_signal(conn, "GLW", "rsi_mr_buy", 33, datetime(2026, 6, 1, tzinfo=timezone.utc))
    r = api_client.get("/api/signals?symbol=glw", auth=AUTH)
    assert len(r.json()) == 1


def test_no_symbol_filter_returns_across_all_symbols(api_client, conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _insert_signal(conn, "GLW", "rsi_mr_buy", 33, base)
    _insert_signal(conn, "AAPL", "rsi_mr_sell", 70, base + timedelta(days=1))

    r = api_client.get("/api/signals?limit=50", auth=AUTH)
    symbols = {row["symbol"] for row in r.json()}
    assert {"GLW", "AAPL"}.issubset(symbols)

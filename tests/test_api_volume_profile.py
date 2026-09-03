"""
PR D, Volume & Volume Profile epic. GET /api/volume-profile/{symbol} --
exposes shared/volume_profile.py's POC/VAH/VAL calculation. Mirrors
test_api_backtest.py's api_client fixture pattern.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

from backtest_engine import Bar
from volume_profile import compute_volume_profile


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
SYMBOL = "VPAPITEST"


def _seed_hourly_bars(conn, symbol, n=30):
    now = datetime.now(timezone.utc)
    rows = []
    price = 100.0
    with conn.cursor() as cur:
        for i in range(n):
            ts = now - timedelta(hours=n - i)
            price += 1.0 if i % 3 else -0.5
            o, h, l, c, v = price - 0.5, price + 1, price - 1, price, 1000 + i * 10
            cur.execute(
                "INSERT INTO price_history_hourly (symbol, ts, open, high, low, close, volume, source) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'alpaca_iex') ON CONFLICT (symbol, ts) DO NOTHING",
                (symbol, ts, o, h, l, c, v),
            )
            rows.append((ts, o, h, l, c, v))
    conn.commit()
    return rows


def test_get_volume_profile_matches_direct_compute(api_client, conn):
    rows = _seed_hourly_bars(conn, SYMBOL, n=30)
    r = api_client.get(f"/api/volume-profile/{SYMBOL}?days=2&bucket_count=10", auth=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["buckets"]) == 10
    assert body["bucket_count"] == 10
    assert body["feed"] == "alpaca_iex"
    assert body["source_table"] == "price_history_hourly"
    assert body["poc"] is not None

    # Independently recompute from the raw seeded rows within the same
    # window and confirm the API's values match.
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=2)
    windowed = [row for row in rows if row[0] >= window_start]
    bars = [
        Bar(symbol=SYMBOL, ts=ts, open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v))
        for ts, o, h, l, c, v in windowed
    ]
    expected = compute_volume_profile(bars, ["alpaca_iex"] * len(bars), bucket_count=10)
    assert body["poc"] == pytest.approx(expected.poc)
    assert body["vah"] == pytest.approx(expected.vah)
    assert body["val"] == pytest.approx(expected.val)
    assert body["total_volume"] == pytest.approx(expected.total_volume)


def test_get_volume_profile_404_for_no_data(api_client, conn):
    r = api_client.get("/api/volume-profile/NODATAVP?days=14", auth=AUTH)
    assert r.status_code == 404


def test_get_volume_profile_default_bucket_count(api_client, conn):
    _seed_hourly_bars(conn, SYMBOL, n=30)
    r = api_client.get(f"/api/volume-profile/{SYMBOL}?days=2", auth=AUTH)
    assert r.status_code == 200
    assert r.json()["bucket_count"] == 30  # UI-layer default, not volume_profile.py's own 50


def test_get_volume_profile_requires_auth(api_client, conn):
    r = api_client.get(f"/api/volume-profile/{SYMBOL}")
    assert r.status_code == 401

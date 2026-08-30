"""
PR B, Volume & Volume Profile epic. GET /api/prices/{symbol} extended with
include_volume_metrics -- exposes shared/volume_metrics.py's calculations
via the API so nothing recomputes them client-side (the spec's explicit
"do not create separate front-end and back-end implementations of the
same metric" rule). No prior dedicated test file existed for this
endpoint at all (confirmed via search) -- this covers both the pre-existing
include_bb behavior (regression) and the new volume-metrics behavior.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

from volume_metrics import relative_volume, volume_percentile, volume_zscore


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
SYMBOL = "PXTEST"


def _seed_price_history(conn, symbol, n=40, start_price=100.0, with_gap_volume_at=None):
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=n)
    with conn.cursor() as cur:
        price = start_price
        for i in range(n):
            ts = start + timedelta(days=i)
            price += 1.0 if i % 3 else -0.5
            volume = 100000 + i * 500
            if i == with_gap_volume_at:
                volume = None
            cur.execute(
                "INSERT INTO price_history (symbol, ts, open, high, low, close, volume, source) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'yahoo') ON CONFLICT (symbol, ts) DO NOTHING",
                (symbol, ts, price - 0.5, price + 1, price - 1, price, volume),
            )
    conn.commit()


def test_prices_without_flag_has_no_volume_metric_fields(api_client, conn):
    _seed_price_history(conn, SYMBOL)
    r = api_client.get(f"/api/prices/{SYMBOL}?days=30", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert len(body) > 0
    for field in ("dollar_volume", "rvol", "volume_zscore", "volume_percentile"):
        assert field not in body[0]


def test_prices_with_volume_metrics_matches_direct_calls(api_client, conn):
    _seed_price_history(conn, SYMBOL, n=40)
    r = api_client.get(f"/api/prices/{SYMBOL}?days=30&include_volume_metrics=true&volume_period=20", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert len(body) > 0
    for row in body:
        assert "dollar_volume" in row
        assert "rvol" in row
        assert "volume_zscore" in row
        assert "volume_percentile" in row

    # dollar_volume is a trivial per-row check.
    for row in body:
        if row["close"] is not None and row["volume"] is not None:
            assert row["dollar_volume"] == pytest.approx(row["close"] * row["volume"])

    # rvol/zscore/percentile: recompute independently via the same
    # shared/volume_metrics.py functions the endpoint calls, using the raw
    # DB rows (not the API's own output, to avoid a circular check) as the
    # ground-truth volume series, and confirm the last row's API values
    # match what those functions produce over that same window.
    with conn.cursor() as cur:
        cur.execute("SELECT volume FROM price_history WHERE symbol=%s ORDER BY ts ASC", (SYMBOL,))
        all_volumes = [float(v[0]) for v in cur.fetchall()]
    expected_rvol = relative_volume(all_volumes, 20)
    expected_zscore = volume_zscore(all_volumes, 20)
    expected_pctile = volume_percentile(all_volumes, 20)
    last = body[-1]
    assert last["rvol"] == pytest.approx(expected_rvol)
    assert last["volume_zscore"] == pytest.approx(expected_zscore)
    assert last["volume_percentile"] == pytest.approx(expected_pctile)


def test_prices_volume_metrics_none_when_volume_gap_in_window(api_client, conn):
    _seed_price_history(conn, SYMBOL, n=40, with_gap_volume_at=25)
    r = api_client.get(f"/api/prices/{SYMBOL}?days=30&include_volume_metrics=true&volume_period=5", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    # Rows whose trailing 5-window includes the None-volume bar must report
    # None metrics, not a 500 and not a silently wrong number.
    none_rows = [row for row in body if row["rvol"] is None]
    assert len(none_rows) > 0


def test_prices_with_bb_and_volume_metrics_together(api_client, conn):
    _seed_price_history(conn, SYMBOL, n=40)
    r = api_client.get(
        f"/api/prices/{SYMBOL}?days=14&include_bb=true&include_volume_metrics=true", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 14
    assert "bb_upper" in body[-1]
    assert "rvol" in body[-1]


def test_prices_requires_auth(api_client, conn):
    r = api_client.get(f"/api/prices/{SYMBOL}")
    assert r.status_code == 401

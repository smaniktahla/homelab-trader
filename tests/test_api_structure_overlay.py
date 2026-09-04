"""
Price Structure epic PR F. GET /api/structure-overlay/{symbol} -- exposes
confirmed swings, active zones, structural events, and Fair Value Gaps
(with derived lifecycle status) for the stock-detail chart overlay.
Mirrors test_api_volume_profile.py's api_client fixture pattern.
"""

import os
import sys
from datetime import date, timedelta

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
SYMBOL = "STRUCTOVERLAY"
D0 = date.today() - timedelta(days=15)  # recent enough to stay within every test's default days=30 cutoff


def _insert_swing(conn, symbol, timeframe, swing_type, event_time, confirmation_time, price):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO structural_swings (symbol, timeframe, swing_type, event_time, confirmation_time, price)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
        """, (symbol, timeframe, swing_type, event_time, confirmation_time, price))
    conn.commit()


def _insert_zone(conn, symbol, timeframe, zone_type, upper, lower):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO structural_zones (symbol, timeframe, zone_type, center_price, upper, lower, touch_count, last_touched, active)
            VALUES (%s,%s,%s,%s,%s,%s,1,%s,TRUE)
        """, (symbol, timeframe, zone_type, (upper + lower) / 2, upper, lower, D0))
    conn.commit()


def _insert_event(conn, symbol, timeframe, event_type, reference_type, reference_id, event_time, confirmation_time):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO structural_events
                (symbol, timeframe, event_type, reference_type, reference_id, event_time, confirmation_time, metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'{}')
        """, (symbol, timeframe, event_type, reference_type, reference_id, event_time, confirmation_time))
    conn.commit()


def _insert_gap(conn, symbol, timeframe, gap_type, upper, lower, event_time):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO fair_value_gaps (symbol, timeframe, gap_type, zone_upper, zone_lower, event_time, confirmation_time)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (symbol, timeframe, gap_type, upper, lower, event_time, event_time))
        gap_id = cur.fetchone()[0]
    conn.commit()
    return gap_id


def test_structure_overlay_returns_swings_zones_events_and_gaps(api_client, conn):
    _insert_swing(conn, SYMBOL, "daily", "high", D0 + timedelta(days=5), D0 + timedelta(days=8), 120.0)
    _insert_swing(conn, SYMBOL, "daily", "low", D0 + timedelta(days=2), D0 + timedelta(days=5), 95.0)
    _insert_zone(conn, SYMBOL, "daily", "resistance", 121.0, 119.0)
    _insert_event(conn, SYMBOL, "daily", "breakout", "zone", 1, D0 + timedelta(days=10), D0 + timedelta(days=11))
    gap_id = _insert_gap(conn, SYMBOL, "daily", "bullish", 105.0, 100.0, D0 + timedelta(days=3))
    _insert_event(conn, SYMBOL, "daily", "fvg_partially_entered", "fvg", gap_id,
                   D0 + timedelta(days=6), D0 + timedelta(days=6))

    r = api_client.get(f"/api/structure-overlay/{SYMBOL}?days=30", auth=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["symbol"] == SYMBOL
    assert len(body["swings"]) == 2
    assert {s["type"] for s in body["swings"]} == {"high", "low"}
    assert len(body["zones"]) == 1
    assert body["zones"][0]["type"] == "resistance"
    assert len(body["events"]) == 1
    assert body["events"][0]["type"] == "breakout"
    assert len(body["fair_value_gaps"]) == 1
    assert body["fair_value_gaps"][0]["status"] == "fvg_partially_entered"


def test_structure_overlay_fvg_status_defaults_to_untouched_with_no_lifecycle_events(api_client, conn):
    _insert_gap(conn, SYMBOL, "daily", "bearish", 105.0, 100.0, D0 + timedelta(days=3))

    r = api_client.get(f"/api/structure-overlay/{SYMBOL}?days=30", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["fair_value_gaps"][0]["status"] == "untouched"


def test_structure_overlay_excludes_fvg_lifecycle_events_from_generic_events_list(api_client, conn):
    """fvg_partially_entered etc. must show up via fair_value_gaps[].status,
    never duplicated into the generic events[] list (reference_type='fvg'
    rows are explicitly filtered out of that query)."""
    gap_id = _insert_gap(conn, SYMBOL, "daily", "bullish", 105.0, 100.0, D0 + timedelta(days=3))
    _insert_event(conn, SYMBOL, "daily", "fvg_fully_filled", "fvg", gap_id,
                   D0 + timedelta(days=6), D0 + timedelta(days=6))

    r = api_client.get(f"/api/structure-overlay/{SYMBOL}?days=30", auth=AUTH)
    body = r.json()
    assert body["events"] == []
    assert body["fair_value_gaps"][0]["status"] == "fvg_fully_filled"


def test_structure_overlay_respects_days_cutoff(api_client, conn):
    old = date.today() - timedelta(days=60)  # well outside the days=30 window used below
    _insert_swing(conn, SYMBOL, "daily", "high", old, old + timedelta(days=3), 120.0)
    recent = date.today() - timedelta(days=2)
    _insert_swing(conn, SYMBOL, "daily", "low", recent, recent + timedelta(days=3), 95.0)

    r = api_client.get(f"/api/structure-overlay/{SYMBOL}?days=30", auth=AUTH)
    body = r.json()
    assert len(body["swings"]) == 1
    assert body["swings"][0]["type"] == "low"


def test_structure_overlay_rejects_invalid_timeframe(api_client, conn):
    r = api_client.get(f"/api/structure-overlay/{SYMBOL}?timeframe=hourly", auth=AUTH)
    assert r.status_code == 400


def test_structure_overlay_empty_for_symbol_with_no_data(api_client, conn):
    r = api_client.get("/api/structure-overlay/NODATASTRUCT", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["swings"] == body["zones"] == body["events"] == body["fair_value_gaps"] == []


def test_structure_overlay_requires_auth(api_client, conn):
    r = api_client.get(f"/api/structure-overlay/{SYMBOL}")
    assert r.status_code == 401

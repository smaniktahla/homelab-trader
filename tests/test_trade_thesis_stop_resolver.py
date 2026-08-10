import json
from datetime import date

import pytest

from trade_thesis_stop_resolver import (
    STOP_RESOLVER_DEFAULTS,
    load_stop_resolver_params,
    resolve_initial_stop_price,
)

SYMBOL = "AAPL"


def _insert_market_structure(conn, symbol, trading_date, nearest_support=None):
    component_values = {"nearest_support": nearest_support} if nearest_support is not None else {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market_structure_history (trading_date, symbol, trend, component_values)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (trading_date, symbol) DO UPDATE SET component_values = EXCLUDED.component_values
            """,
            (trading_date, symbol, "bullish", json.dumps(component_values)),
        )
    conn.commit()


def test_defaults():
    assert STOP_RESOLVER_DEFAULTS["structure_aware_stop_enabled"] == 0
    assert STOP_RESOLVER_DEFAULTS["max_structure_stop_multiple"] == 2.5


def test_load_params_defaults_with_empty_signal_params(conn):
    params = load_stop_resolver_params(conn)
    assert params["structure_aware_stop_enabled"] == 0
    assert params["max_structure_stop_multiple"] == 2.5


def test_load_params_reads_overrides(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO signal_params (key, value, description) VALUES (%s, %s, '') "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            ("structure_aware_stop_enabled", 1),
        )
    conn.commit()
    params = load_stop_resolver_params(conn)
    assert params["structure_aware_stop_enabled"] == 1


# --- resolve_initial_stop_price ------------------------------------------------

def test_falls_back_to_percentage_when_no_structure_data(conn):
    result = resolve_initial_stop_price(conn, SYMBOL, price=100.0, percentage_stop_price=92.0)
    assert result["stop_price"] == 92.0
    assert result["source"] == "percentage_fallback"
    assert result["structure_support_price"] is None


def test_falls_back_when_no_support_zone_recorded(conn):
    _insert_market_structure(conn, SYMBOL, date(2026, 1, 1), nearest_support=None)
    result = resolve_initial_stop_price(conn, SYMBOL, price=100.0, percentage_stop_price=92.0)
    assert result["source"] == "percentage_fallback"


def test_uses_structure_support_when_sane(conn):
    # Support at 95, price 100 -- distance 5, vs percentage-stop distance 8
    # (92.0). Well within the default 2.5x sanity cap.
    _insert_market_structure(conn, SYMBOL, date(2026, 1, 1), nearest_support={"price": 95.0, "touch_count": 3})
    result = resolve_initial_stop_price(conn, SYMBOL, price=100.0, percentage_stop_price=92.0)
    assert result["stop_price"] == 95.0
    assert result["source"] == "structure_support"
    assert result["structure_support_price"] == 95.0


def test_falls_back_when_support_at_or_above_price(conn):
    _insert_market_structure(conn, SYMBOL, date(2026, 1, 1), nearest_support={"price": 100.0, "touch_count": 3})
    result = resolve_initial_stop_price(conn, SYMBOL, price=100.0, percentage_stop_price=92.0)
    assert result["source"] == "percentage_fallback"
    assert result["stop_price"] == 92.0


def test_falls_back_when_support_exceeds_sanity_cap(conn):
    # percentage distance = 8 (100 -> 92); default cap is 2.5x = 20;
    # support at 70 is 30 below price -- exceeds the cap.
    _insert_market_structure(conn, SYMBOL, date(2026, 1, 1), nearest_support={"price": 70.0, "touch_count": 3})
    result = resolve_initial_stop_price(conn, SYMBOL, price=100.0, percentage_stop_price=92.0)
    assert result["source"] == "percentage_fallback"
    assert result["stop_price"] == 92.0
    assert result["structure_support_price"] == 70.0


def test_custom_sanity_cap_multiple_is_respected(conn):
    # Same 30-below-price support as above, but a looser 5x cap (=40)
    # allows it through this time.
    _insert_market_structure(conn, SYMBOL, date(2026, 1, 1), nearest_support={"price": 70.0, "touch_count": 3})
    params = {"max_structure_stop_multiple": 5.0}
    result = resolve_initial_stop_price(conn, SYMBOL, price=100.0, percentage_stop_price=92.0, params=params)
    assert result["source"] == "structure_support"
    assert result["stop_price"] == 70.0

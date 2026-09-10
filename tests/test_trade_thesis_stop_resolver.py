import json
from datetime import date

import pytest

from trade_thesis_stop_resolver import (
    STOP_RESOLVER_DEFAULTS,
    load_stop_resolver_params,
    resolve_initial_stop_price,
)

SYMBOL = "AAPL"


def _insert_market_structure(conn, symbol, trading_date, nearest_support=None, atr=None):
    component_values = {"nearest_support": nearest_support} if nearest_support is not None else {}
    if atr is not None:
        # Matches the REAL persisted shape from combine_timeframe_structures()
        # -- ATR lives nested at component_values["daily"]["volatility"]["atr"],
        # unlike nearest_support which combine_timeframe_structures promotes
        # to the top level. See trade_thesis_stop_resolver._daily_atr().
        component_values["daily"] = {"volatility": {"atr": atr}}
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


# --- ATR-stop mode (Volatility Sizing epic follow-on branch) ------------------

def test_atr_stop_disabled_by_default_is_byte_for_byte_unchanged(conn):
    """atr_stop_enabled defaults to 0 -- even with a valid ATR reading on
    file, behavior (and the returned dict, apart from the new always-
    present atr_value=None key) must be identical to before this mode
    existed."""
    _insert_market_structure(conn, SYMBOL, date(2026, 1, 1), atr=2.0)
    result = resolve_initial_stop_price(conn, SYMBOL, price=100.0, percentage_stop_price=92.0)
    assert result["source"] == "percentage_fallback"
    assert result["stop_price"] == 92.0
    assert result["atr_value"] is None


def test_atr_stop_used_when_enabled_and_within_bounds(conn):
    # ATR=2.0, multiple=2.0 -> raw distance=4.0 -> 4% of price=100, well
    # within the default [2%, 25%] floor/cap.
    _insert_market_structure(conn, SYMBOL, date(2026, 1, 1), atr=2.0)
    params = {"atr_stop_enabled": 1, "atr_stop_multiple": 2.0, "atr_stop_min_pct": 0.02, "atr_stop_max_pct": 0.25}
    result = resolve_initial_stop_price(conn, SYMBOL, price=100.0, percentage_stop_price=92.0, params=params)
    assert result["source"] == "atr_stop"
    assert result["stop_price"] == 96.0
    assert result["atr_value"] == 2.0
    assert result["structure_support_price"] is None


def test_atr_stop_clamped_to_floor_when_raw_distance_too_tight():
    # ATR=0.1, multiple=2.0 -> raw distance=0.2 (0.2% of price) -- far
    # below the 2% floor -- must clamp UP to the floor, not use the raw,
    # near-zero distance. _daily_atr is monkeypatched to isolate the
    # clamp math itself from the persistence read (a real conn is not
    # touched by _atr_stop_price beyond that one call).
    import trade_thesis_stop_resolver as tsr
    orig = tsr._daily_atr
    tsr._daily_atr = lambda conn, symbol: 0.1
    try:
        stop_price, atr_value = tsr._atr_stop_price(
            object(), SYMBOL, price=100.0,
            params={"atr_stop_multiple": 2.0, "atr_stop_min_pct": 0.02, "atr_stop_max_pct": 0.25},
        )
    finally:
        tsr._daily_atr = orig
    assert atr_value == 0.1
    assert stop_price == 98.0  # 100 - (100 * 0.02) -- floor applied, not the raw 0.2 distance


def test_atr_stop_clamped_to_cap_when_raw_distance_too_wide():
    # ATR=20, multiple=2.0 -> raw distance=40 (40% of price) -- far above
    # the 25% cap -- must clamp DOWN to the cap.
    import trade_thesis_stop_resolver as tsr
    orig = tsr._daily_atr
    tsr._daily_atr = lambda conn, symbol: 20.0
    try:
        stop_price, atr_value = tsr._atr_stop_price(
            object(), SYMBOL, price=100.0,
            params={"atr_stop_multiple": 2.0, "atr_stop_min_pct": 0.02, "atr_stop_max_pct": 0.25},
        )
    finally:
        tsr._daily_atr = orig
    assert atr_value == 20.0
    assert stop_price == 75.0  # 100 - (100 * 0.25) -- cap applied, not the raw 40 distance


def test_atr_stop_falls_back_to_structure_when_no_atr_reading(conn):
    """atr_stop_enabled on, but no ATR persisted yet (insufficient
    history) -- falls through to the structure tier (if that's also
    enabled and sane), never blocks the proposal."""
    _insert_market_structure(conn, SYMBOL, date(2026, 1, 1), nearest_support={"price": 95.0, "touch_count": 3})
    params = {"atr_stop_enabled": 1, "structure_aware_stop_enabled": 1}
    result = resolve_initial_stop_price(conn, SYMBOL, price=100.0, percentage_stop_price=92.0, params=params)
    assert result["source"] == "structure_support"
    assert result["stop_price"] == 95.0


def test_atr_stop_falls_back_to_percentage_when_no_atr_and_no_structure(conn):
    params = {"atr_stop_enabled": 1}
    result = resolve_initial_stop_price(conn, SYMBOL, price=100.0, percentage_stop_price=92.0, params=params)
    assert result["source"] == "percentage_fallback"
    assert result["stop_price"] == 92.0


def test_atr_stop_takes_precedence_over_structure_when_both_enabled_and_valid(conn):
    _insert_market_structure(conn, SYMBOL, date(2026, 1, 1), nearest_support={"price": 95.0, "touch_count": 3}, atr=2.0)
    params = {"atr_stop_enabled": 1, "structure_aware_stop_enabled": 1, "atr_stop_multiple": 2.0,
              "atr_stop_min_pct": 0.02, "atr_stop_max_pct": 0.25}
    result = resolve_initial_stop_price(conn, SYMBOL, price=100.0, percentage_stop_price=92.0, params=params)
    assert result["source"] == "atr_stop"
    assert result["stop_price"] == 96.0


def test_atr_stop_default_multiple_and_bounds_registered():
    assert STOP_RESOLVER_DEFAULTS["atr_stop_enabled"] == 0
    assert STOP_RESOLVER_DEFAULTS["atr_stop_multiple"] == 2.0
    assert STOP_RESOLVER_DEFAULTS["atr_stop_min_pct"] == 0.02
    assert STOP_RESOLVER_DEFAULTS["atr_stop_max_pct"] == 0.25

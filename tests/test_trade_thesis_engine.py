from datetime import date, datetime, timedelta, timezone

import pytest

from trade_thesis_engine import (
    TRADE_THESIS_ENGINE_DEFAULTS,
    instantiate_buy_trade_thesis,
    load_trade_thesis_engine_params,
)

SYMBOL = "AAPL"
START = date(2026, 1, 1)

# Enough history for RSI(14)/BB(20) to have real values, and shaped so the
# final close is genuinely oversold (RSI low, %B near/below the lower band)
# -- same "sharp drop into oversold" shape test_market_structure_integration.py
# uses for its own BUY fixture, trimmed to the columns this module needs.
_CLOSES = [180 + 0.05 * ((-1) ** i) for i in range(20)] + [175, 170, 165, 160, 150]


def _insert_price_history(conn, symbol, closes, start=START):
    with conn.cursor() as cur:
        for i, close in enumerate(closes):
            d = start + timedelta(days=i)
            cur.execute(
                """
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, ts) DO NOTHING
                """,
                (symbol, d, close, close, close, close, 1_000_000),
            )
    conn.commit()


def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug = 'mean_reversion'")
        return cur.fetchone()[0]


def _as_of():
    return datetime.combine(START + timedelta(days=len(_CLOSES) - 1), datetime.min.time(), tzinfo=timezone.utc)


def test_defaults_have_instantiation_disabled():
    assert TRADE_THESIS_ENGINE_DEFAULTS["trade_thesis_instantiation_enabled"] == 0


def test_load_params_defaults_to_disabled_with_empty_signal_params(conn):
    params = load_trade_thesis_engine_params(conn)
    assert params["trade_thesis_instantiation_enabled"] == 0


def test_load_params_reads_enabled_flag(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO signal_params (key, value, description) VALUES (%s, %s, '') "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            ("trade_thesis_instantiation_enabled", 1),
        )
    conn.commit()
    params = load_trade_thesis_engine_params(conn)
    assert params["trade_thesis_instantiation_enabled"] == 1


def test_instantiate_buy_trade_thesis_succeeds_with_real_history(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    thesis_id = _mean_reversion_thesis_id(conn)
    as_of = _as_of()

    row_id = instantiate_buy_trade_thesis(
        conn, thesis_id, SYMBOL, as_of, rsi_oversold_threshold=30, planned_initial_stop_price=140.0
    )

    assert row_id is not None
    with conn.cursor() as cur:
        cur.execute("""
            SELECT thesis_id, symbol, hypothesis_type, status, invalidation_spec
            FROM trade_theses WHERE id = %s
        """, (row_id,))
        row = cur.fetchone()
    assert row[0] == thesis_id
    assert row[1] == SYMBOL
    assert row[2] == "mean_reversion_oversold"
    assert row[3] == "proposed"
    assert row[4] == {"feature": "technical.close", "op": "lt", "value": 140.0}


def test_instantiate_buy_trade_thesis_returns_none_without_price_history(conn):
    # No _insert_price_history call -- feature evaluation will fail
    # (None for every feature), so semantic validation must reject it.
    thesis_id = _mean_reversion_thesis_id(conn)
    as_of = _as_of()

    row_id = instantiate_buy_trade_thesis(
        conn, thesis_id, SYMBOL, as_of, rsi_oversold_threshold=30, planned_initial_stop_price=140.0
    )

    assert row_id is None
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM trade_theses")
        assert cur.fetchone()[0] == 0


def test_instantiate_buy_trade_thesis_returns_none_on_persistence_failure(conn):
    # A thesis_id with no matching theses row: construction + validation
    # both succeed (they don't check theses FK integrity), but the DB
    # insert itself must fail its FK constraint, and the engine must
    # swallow that (fail-open) rather than raise.
    _insert_price_history(conn, SYMBOL, _CLOSES)
    as_of = _as_of()

    row_id = instantiate_buy_trade_thesis(
        conn, 999999, SYMBOL, as_of, rsi_oversold_threshold=30, planned_initial_stop_price=140.0
    )

    assert row_id is None
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM trade_theses")
        assert cur.fetchone()[0] == 0


def test_instantiate_buy_trade_thesis_leaves_connection_usable_after_failure(conn):
    as_of = _as_of()
    # First call fails (no price history) -- must not poison the connection.
    assert instantiate_buy_trade_thesis(
        conn, _mean_reversion_thesis_id(conn), SYMBOL, as_of, 30, 140.0
    ) is None

    _insert_price_history(conn, SYMBOL, _CLOSES)
    row_id = instantiate_buy_trade_thesis(
        conn, _mean_reversion_thesis_id(conn), SYMBOL, as_of, 30, 140.0
    )
    assert row_id is not None

import dataclasses
from datetime import date, timedelta

import pytest

from feature_registry import (
    FEATURES,
    PROVIDERS,
    evaluate_feature,
    get_feature,
    get_provider,
    is_legal_feature,
    is_legal_provider,
)
from signals import compute_bollinger, compute_rsi
from market_structure import _cluster_zones, _atr_series, SR_ATR_MULT

SYMBOL = "AAPL"
START = date(2026, 1, 1)


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


def _insert_market_regime_history(conn, rows):
    """rows: list of (trading_date, overall)."""
    with conn.cursor() as cur:
        for trading_date, overall in rows:
            cur.execute(
                """
                INSERT INTO market_regime_history (trading_date, overall)
                VALUES (%s, %s)
                ON CONFLICT (trading_date) DO UPDATE SET overall = EXCLUDED.overall
                """,
                (trading_date, overall),
            )
    conn.commit()


# A mildly noisy but deterministic 30-day close series, long enough for both
# RSI(14) and BB(20) to have real (non-None) values partway through.
_CLOSES = [
    100, 101, 99, 102, 103, 101, 104, 105, 103, 106,
    107, 105, 108, 109, 107, 110, 111, 109, 112, 113,
    111, 114, 115, 113, 116, 200, 5, 300, 1, 400,  # last 4 are lookahead poison
]


# --- provider/feature registry lookups --------------------------------------

def test_all_providers_have_required_fields():
    for provider_id, spec in PROVIDERS.items():
        assert spec.provider_id == provider_id
        assert spec.source_table
        assert spec.description


def test_all_features_reference_a_legal_provider():
    for feature_id, spec in FEATURES.items():
        assert spec.feature_id == feature_id
        assert spec.provider_id in PROVIDERS
        assert spec.output_type in ("numeric", "text")


def test_is_legal_provider():
    assert is_legal_provider("technical") is True
    assert is_legal_provider("market_structure") is True
    assert is_legal_provider("not_a_real_provider") is False


def test_is_legal_feature():
    assert is_legal_feature("technical.rsi_14") is True
    assert is_legal_feature("technical.not_a_real_feature") is False


def test_get_provider_and_get_feature_return_none_when_unregistered():
    assert get_provider("bogus") is None
    assert get_feature("bogus.bogus") is None


def test_get_provider_and_get_feature_return_matching_spec():
    assert get_provider("technical").source_table == "symbol_features"
    assert get_feature("technical.rsi_14").provider_id == "technical"


# --- as-of-safe evaluation, technical features -------------------------------

def test_eval_close_matches_price_history(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    as_of = START + timedelta(days=24)  # index 24 -> close 116
    result = evaluate_feature(conn, "technical.close", SYMBOL, as_of)
    assert result == pytest.approx(116)


def test_eval_close_ignores_future_rows(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    as_of = START + timedelta(days=24)
    result = evaluate_feature(conn, "technical.close", SYMBOL, as_of)
    # Index 25+ are the lookahead-poison values (200/5/300/1/400) -- must
    # never leak into a result as-of index 24.
    assert result not in (200, 5, 300, 1, 400)


def test_eval_rsi_14_matches_direct_compute_rsi_on_truncated_window(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    as_of_index = 24
    as_of = START + timedelta(days=as_of_index)
    expected = compute_rsi(_CLOSES[: as_of_index + 1], 14)

    result = evaluate_feature(conn, "technical.rsi_14", SYMBOL, as_of)
    assert result == pytest.approx(expected)


def test_eval_bb_pct_b_matches_direct_compute_bollinger_on_truncated_window(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    as_of_index = 24
    as_of = START + timedelta(days=as_of_index)
    window = _CLOSES[: as_of_index + 1]
    upper, _, lower, _ = compute_bollinger(window, 20, 2.0)
    expected = (window[-1] - lower) / (upper - lower)

    result = evaluate_feature(conn, "technical.bb_pct_b", SYMBOL, as_of)
    assert result == pytest.approx(expected)


def test_eval_rsi_14_none_when_insufficient_history(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES[:5])  # far short of RSI(14)'s minimum
    as_of = START + timedelta(days=4)
    assert evaluate_feature(conn, "technical.rsi_14", SYMBOL, as_of) is None


def test_eval_close_none_when_as_of_predates_any_history(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    as_of = START - timedelta(days=10)
    assert evaluate_feature(conn, "technical.close", SYMBOL, as_of) is None


def test_eval_accepts_datetime_as_of(conn):
    from datetime import datetime, timezone
    _insert_price_history(conn, SYMBOL, _CLOSES)
    as_of_date = START + timedelta(days=24)
    as_of_dt = datetime.combine(as_of_date, datetime.min.time(), tzinfo=timezone.utc)
    result = evaluate_feature(conn, "technical.close", SYMBOL, as_of_dt)
    assert result == pytest.approx(116)


# --- as-of-safe evaluation, market_regime feature ----------------------------

def test_eval_market_regime_overall_uses_last_date_leq_as_of(conn):
    _insert_market_regime_history(conn, [
        (date(2026, 1, 1), "bullish"),
        (date(2026, 1, 5), "neutral"),
        (date(2026, 1, 10), "bearish"),
    ])
    assert evaluate_feature(conn, "market_regime.overall", SYMBOL, date(2026, 1, 7)) == "neutral"


def test_eval_market_regime_overall_ignores_future_rows(conn):
    _insert_market_regime_history(conn, [
        (date(2026, 1, 1), "bullish"),
        (date(2026, 1, 10), "bearish"),  # future relative to as_of below
    ])
    assert evaluate_feature(conn, "market_regime.overall", SYMBOL, date(2026, 1, 3)) == "bullish"


def test_eval_market_regime_overall_none_before_any_history(conn):
    _insert_market_regime_history(conn, [(date(2026, 1, 10), "bearish")])
    assert evaluate_feature(conn, "market_regime.overall", SYMBOL, date(2026, 1, 1)) is None


# --- fail-open contract -------------------------------------------------------

def test_evaluate_feature_returns_none_for_unregistered_feature(conn):
    assert evaluate_feature(conn, "not.registered", SYMBOL, date(2026, 1, 1)) is None


def test_evaluate_feature_fails_open_on_eval_fn_exception(conn, monkeypatch):
    # FeatureSpec is frozen -- swap the whole registry entry for a copy
    # with a broken eval_fn (monkeypatch.setitem restores the original
    # entry after the test, same as it would for a plain dict value).
    def _boom(conn, symbol, as_of):
        raise RuntimeError("simulated eval failure")

    broken_spec = dataclasses.replace(FEATURES["technical.close"], eval_fn=_boom)
    monkeypatch.setitem(FEATURES, "technical.close", broken_spec)
    assert evaluate_feature(conn, "technical.close", SYMBOL, date(2026, 1, 1)) is None


# --- Price Structure epic PR C: market_structure.trend_state -----------------

def _insert_market_structure_history(conn, symbol, rows):
    """rows: list of (trading_date, trend)."""
    with conn.cursor() as cur:
        for trading_date, trend in rows:
            cur.execute(
                """
                INSERT INTO market_structure_history (trading_date, symbol, trend)
                VALUES (%s, %s, %s)
                ON CONFLICT (trading_date, symbol) DO UPDATE SET trend = EXCLUDED.trend
                """,
                (trading_date, symbol, trend),
            )
    conn.commit()


def test_eval_market_structure_trend_state_uses_last_date_leq_as_of(conn):
    _insert_market_structure_history(conn, SYMBOL, [
        (date(2026, 1, 1), "higher_highs_higher_lows"),
        (date(2026, 1, 10), "mixed"),
    ])
    assert evaluate_feature(conn, "market_structure.trend_state", SYMBOL, date(2026, 1, 5)) == "higher_highs_higher_lows"
    assert evaluate_feature(conn, "market_structure.trend_state", SYMBOL, date(2026, 1, 10)) == "mixed"


def test_eval_market_structure_trend_state_none_before_any_history(conn):
    _insert_market_structure_history(conn, SYMBOL, [(date(2026, 1, 10), "mixed")])
    assert evaluate_feature(conn, "market_structure.trend_state", SYMBOL, date(2026, 1, 1)) is None


# --- Price Structure epic PR C: structural_zones.nearest_*_distance_atr ------

def _insert_daily_swing(conn, symbol, timeframe, swing_type, event_time, confirmation_time, price):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO structural_swings
                (symbol, timeframe, swing_type, event_time, confirmation_time, price)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, timeframe, swing_type, event_time) DO NOTHING
            """,
            (symbol, timeframe, swing_type, event_time, confirmation_time, price),
        )
    conn.commit()


# Steady, mildly noisy 20-day close series centered around 105, long enough
# for _atr_series (needs ATR_PERIOD+1 = 15 bars) to produce a real value.
_STRUCTURE_CLOSES = [105, 106, 104, 105, 106, 104, 105, 106, 104, 105,
                      106, 104, 105, 106, 104, 105, 106, 104, 105, 106]


def test_eval_nearest_support_distance_atr_matches_direct_cluster_computation(conn):
    _insert_price_history(conn, SYMBOL, _STRUCTURE_CLOSES, start=date(2026, 1, 1))
    as_of = date(2026, 1, 1) + timedelta(days=19)  # last close, index 19 = 106

    # two confirmed lows near 100 (support, below current price 106)
    _insert_daily_swing(conn, SYMBOL, "daily", "low", date(2026, 1, 3), date(2026, 1, 6), 100.0)
    _insert_daily_swing(conn, SYMBOL, "daily", "low", date(2026, 1, 10), date(2026, 1, 13), 100.2)

    result = evaluate_feature(conn, "structural_zones.nearest_support_distance_atr", SYMBOL, as_of)
    assert result is not None

    ohlc = [(date(2026, 1, 1) + timedelta(days=i), c, c, c, c) for i, c in enumerate(_STRUCTURE_CLOSES)]
    atr = _atr_series(ohlc)[-1]
    tolerance = atr * SR_ATR_MULT
    zones = _cluster_zones([{"date": date(2026, 1, 3), "price": 100.0}, {"date": date(2026, 1, 10), "price": 100.2}], tolerance)
    expected = abs(106 - zones[0]["price"]) / atr
    assert result == pytest.approx(expected)


def test_eval_nearest_support_distance_atr_excludes_swing_not_yet_confirmed(conn):
    """The core lookahead proof for this feature: a swing whose
    confirmation_time is AFTER as_of must never move the result, even
    though its event_time (and its dramatic proximity to price) would
    make it very tempting to include if confirmation_time were ignored."""
    _insert_price_history(conn, SYMBOL, _STRUCTURE_CLOSES, start=date(2026, 1, 1))
    as_of = date(2026, 1, 1) + timedelta(days=19)

    _insert_daily_swing(conn, SYMBOL, "daily", "low", date(2026, 1, 3), date(2026, 1, 6), 100.0)
    baseline = evaluate_feature(conn, "structural_zones.nearest_support_distance_atr", SYMBOL, as_of)

    # a much closer low, but not confirmed until AFTER as_of -- must not
    # change the result at all.
    _insert_daily_swing(conn, SYMBOL, "daily", "low", date(2026, 1, 18), date(2026, 1, 25), 105.9)
    result = evaluate_feature(conn, "structural_zones.nearest_support_distance_atr", SYMBOL, as_of)

    assert result == pytest.approx(baseline)


def test_eval_nearest_resistance_distance_atr_none_when_no_resistance_swings(conn):
    _insert_price_history(conn, SYMBOL, _STRUCTURE_CLOSES, start=date(2026, 1, 1))
    as_of = date(2026, 1, 1) + timedelta(days=19)
    assert evaluate_feature(conn, "structural_zones.nearest_resistance_distance_atr", SYMBOL, as_of) is None


# --- Price Structure epic PR C: structural_events.* --------------------------

def _insert_structural_event(conn, symbol, event_type, event_time, confirmation_time, reference_id=1):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO structural_events
                (symbol, timeframe, event_type, reference_type, reference_id, event_time, confirmation_time, metadata)
            VALUES (%s,'daily',%s,'zone',%s,%s,%s,'{}')
            ON CONFLICT (symbol, timeframe, event_type, reference_type, reference_id, event_time) DO NOTHING
            """,
            (symbol, event_type, reference_id, event_time, confirmation_time),
        )
    conn.commit()


def test_eval_recent_event_type_uses_last_confirmation_time_leq_as_of(conn):
    _insert_structural_event(conn, SYMBOL, "breakout", date(2026, 1, 1), date(2026, 1, 2), reference_id=1)
    _insert_structural_event(conn, SYMBOL, "acceptance", date(2026, 1, 5), date(2026, 1, 5), reference_id=1)
    _insert_structural_event(conn, SYMBOL, "failed_breakout", date(2026, 1, 20), date(2026, 1, 20), reference_id=1)

    assert evaluate_feature(conn, "structural_events.recent_event_type", SYMBOL, date(2026, 1, 10)) == "acceptance"


def test_eval_bars_since_last_breakout_counts_trading_days(conn):
    _insert_price_history(conn, SYMBOL, _STRUCTURE_CLOSES, start=date(2026, 1, 1))
    _insert_structural_event(conn, SYMBOL, "breakout", date(2026, 1, 3), date(2026, 1, 6), reference_id=1)
    as_of = date(2026, 1, 1) + timedelta(days=19)  # index 19

    result = evaluate_feature(conn, "structural_events.bars_since_last_breakout", SYMBOL, as_of)
    # confirmation_time 2026-01-06 is index 5 in price_history (Jan 1 = index 0); as_of is index 19.
    assert result == 14


def test_eval_bars_since_last_breakout_none_when_no_breakout_yet(conn):
    _insert_price_history(conn, SYMBOL, _STRUCTURE_CLOSES, start=date(2026, 1, 1))
    as_of = date(2026, 1, 1) + timedelta(days=19)
    assert evaluate_feature(conn, "structural_events.bars_since_last_breakout", SYMBOL, as_of) is None


def test_structural_providers_and_features_are_legal():
    assert is_legal_provider("structural_zones") is True
    assert is_legal_provider("structural_events") is True
    assert is_legal_feature("structural_zones.nearest_support_distance_atr") is True
    assert is_legal_feature("structural_events.recent_event_type") is True

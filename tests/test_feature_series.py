"""
shared/feature_series.py must return, for every bar t, exactly what
feature_registry.evaluate_feature() returns for as_of = dates[t] -- the
backtest bridge is only as trustworthy as that parity.
"""

import random
from datetime import date, timedelta

import pytest

from feature_registry import evaluate_feature
from feature_series import (
    SUPPORTED_FEATURES, UnsupportedFeature, bb_pct_b_series, build_feature_series, check_supported, rsi_series,
)
from regime_common import load_daily_series
from signals import compute_rsi

SYMBOL = "PARITY"
START = date(2025, 3, 3)


def _walk(n, seed=7):
    rng = random.Random(seed)
    closes, c = [], 100.0
    for _ in range(n):
        c = round(max(1.0, c * (1 + rng.gauss(0, 0.02))), 4)
        closes.append(c)
    return closes


def test_rsi_series_matches_compute_rsi_on_every_prefix():
    closes = _walk(120)
    series = rsi_series(closes)
    for t in range(len(closes)):
        assert series[t] == compute_rsi(closes[: t + 1], 14)


def test_rsi_series_handles_flat_prices_like_compute_rsi():
    closes = [50.0] * 30
    series = rsi_series(closes)
    for t in range(len(closes)):
        assert series[t] == compute_rsi(closes[: t + 1], 14)


def test_bb_pct_b_none_before_window_and_on_zero_width_bands():
    assert bb_pct_b_series([10.0] * 25) == [None] * 25
    series = bb_pct_b_series(_walk(25))
    assert series[:19] == [None] * 19 and all(v is not None for v in series[19:])


def test_unsupported_features_raise_instead_of_evaluating():
    for f in ("structural_zones.nearest_support_distance_atr", "market_structure.trend_state"):
        with pytest.raises(UnsupportedFeature):
            check_supported({"technical.rsi_14", f})


def _seed(conn, closes):
    with conn.cursor() as cur:
        for i, c in enumerate(closes):
            d = START + timedelta(days=i)
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, 1000)
            """, (SYMBOL, d, c, c, c, c))
        for i in range(0, len(closes), 3):  # sparse regime rows: as-of lookup must carry forward
            cur.execute("INSERT INTO market_regime_history (trading_date, overall) VALUES (%s, %s)",
                        (START + timedelta(days=i), ["neutral", "bull_calm", "bear_fear"][i % 3]))
        events = [
            ("breakout", 5, 7), ("acceptance", 9, 9), ("rejection", 9, 9),   # same-day tie: id order decides
            ("fvg_midpoint_reached", 20, 22), ("breakdown", 30, 33), ("failed_breakout", 41, 41),
        ]
        for ref, (etype, ev, conf) in enumerate(events, start=1):
            cur.execute("""
                INSERT INTO structural_events
                    (symbol, timeframe, event_type, reference_type, reference_id, event_time, confirmation_time, metadata)
                VALUES (%s, 'daily', %s, 'zone', %s, %s, %s, '{}')
            """, (SYMBOL, etype, ref, START + timedelta(days=ev), START + timedelta(days=conf)))
    conn.commit()


def test_series_match_evaluate_feature_on_every_bar(conn):
    _seed(conn, _walk(60))
    dates, closes = load_daily_series(conn, SYMBOL)
    with conn.cursor() as cur:
        cur.execute("SELECT trading_date, overall FROM market_regime_history ORDER BY trading_date")
        regime = cur.fetchall()
        cur.execute("""
            SELECT confirmation_time, id, event_type FROM structural_events
            WHERE symbol=%s AND timeframe='daily'
        """, (SYMBOL,))
        events = cur.fetchall()
    series = build_feature_series(
        SUPPORTED_FEATURES, dates, closes,
        regime=([r[0] for r in regime], [r[1] for r in regime]), events=events)
    for f in SUPPORTED_FEATURES:
        for t, d in enumerate(dates):
            assert series[f][t] == evaluate_feature(conn, f, SYMBOL, d), (f, d)

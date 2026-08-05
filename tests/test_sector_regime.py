"""
Tests for shared/sector_regime.py.

classify_sector_regime is pure (no I/O) and gets a table covering the
main scenarios from the PR spec: strong bullish, bullish-with-positive-RS,
a rising sector that underperforms the market, and missing/insufficient
data. compute_sector_regime/store_sector_regime_day are tested against a
real Postgres connection, same reasoning as test_market_regime_history.py.
"""

import sys
import pathlib
from datetime import date, timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
p = str(ROOT / "shared")
if p not in sys.path:
    sys.path.insert(0, p)

for _mod in ("sector_regime", "sector_mapping", "regime_common"):
    sys.modules.pop(_mod, None)
import sector_regime as sr


def _rising_series(n, start, daily_pct):
    closes = [start]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + daily_pct))
    return closes


def _flat_series(n, level):
    return [level] * n


# ─────────────────────────────────────────────────────────────────────────
# classify_sector_regime — pure
# ─────────────────────────────────────────────────────────────────────────

def test_strong_bullish_sector():
    """Sector ETF rising steadily, market flat: absolute trend and relative
    strength both agree bullish -> strong_bullish."""
    sector_closes = _rising_series(300, 50.0, 0.003)
    market_closes = _flat_series(300, 400.0)
    ctx = sr.classify_sector_regime(sector_closes, market_closes)
    assert ctx["classification"] in ("strong_bullish", "bullish")
    assert ctx["absolute_trend_score"] > 0
    assert ctx["relative_strength_score"] > 0
    assert ctx["confidence"] > 0.8


def test_bullish_sector_positive_relative_strength():
    sector_closes = _rising_series(300, 50.0, 0.0015)
    market_closes = _rising_series(300, 400.0, 0.0002)
    ctx = sr.classify_sector_regime(sector_closes, market_closes)
    assert ctx["classification"] in ("bullish", "strong_bullish")
    assert ctx["relative_strength_score"] > 0


def test_rising_sector_underperforming_market():
    """Sector is up in absolute terms but market is up more -- relative
    strength should be negative even though absolute trend is bullish."""
    sector_closes = _rising_series(300, 50.0, 0.0005)
    market_closes = _rising_series(300, 400.0, 0.003)
    ctx = sr.classify_sector_regime(sector_closes, market_closes)
    assert ctx["absolute_trend_score"] > 0
    assert ctx["relative_strength_score"] < 0


def test_insufficient_history_yields_none_inputs_not_false():
    """Fewer than 50 bars: every component should be None (unknown), never
    silently coerced to a bearish/false value."""
    sector_closes = _flat_series(10, 50.0)
    market_closes = _flat_series(10, 400.0)
    ctx = sr.classify_sector_regime(sector_closes, market_closes)
    assert ctx["classification"] == "insufficient_data"
    assert ctx["confidence"] == 0.0
    assert all(v is None for v in ctx["component_values"].values())


# ─────────────────────────────────────────────────────────────────────────
# compute_sector_regime — unmapped sector / insufficient data / as-of date
# ─────────────────────────────────────────────────────────────────────────

def test_compute_sector_regime_unknown_sector_mapping(conn):
    ctx = sr.compute_sector_regime(conn, "Not A Real Sector")
    assert ctx["classification"] == "unknown"
    assert ctx["reason"] == "no_etf_mapping"


def test_compute_sector_regime_insufficient_price_history(conn):
    _insert_prices(conn, "XLF", date(2026, 1, 1), _flat_series(5, 40.0))
    _insert_prices(conn, "SPY", date(2026, 1, 1), _flat_series(5, 400.0))
    ctx = sr.compute_sector_regime(conn, "Financials", as_of_date=date(2026, 1, 5))
    assert ctx["classification"] == "insufficient_data"


def test_compute_sector_regime_as_of_date_ignores_future_prices(conn):
    """A future price spike must not affect an as-of classification -- the
    result must match what classifying the pre-target-date data alone
    would produce, not something dragged toward the (later) spike."""
    dates_start = date(2024, 1, 1)
    n = 300
    target_idx = 200

    sector_closes = _flat_series(n, 40.0)
    market_closes = _flat_series(n, 400.0)
    expected = sr.classify_sector_regime(sector_closes[:target_idx + 1], market_closes[:target_idx + 1])

    for i in range(target_idx + 1, n):
        sector_closes[i] = 4000.0
        market_closes[i] = 4000.0

    _insert_prices(conn, "XLF", dates_start, sector_closes)
    _insert_prices(conn, "SPY", dates_start, market_closes)

    target_date = dates_start + timedelta(days=target_idx)
    ctx = sr.compute_sector_regime(conn, "Financials", as_of_date=target_date)
    assert ctx["classification"] == expected["classification"]
    assert ctx["total_score"] == expected["total_score"]
    assert ctx["classification"] not in ("bullish", "strong_bullish")


def _insert_prices(conn, symbol, start_date, closes):
    with conn.cursor() as cur:
        for i, c in enumerate(closes):
            d = start_date + timedelta(days=i)
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, (symbol, d, c, c, c, c, 1000))
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────
# store_sector_regime_day / load_latest_sector_regime — DB round trip
# ─────────────────────────────────────────────────────────────────────────

_CTX = {
    "sector": "Financials", "symbol": "XLF", "benchmark_symbol": "SPY",
    "classification": "bullish", "total_score": 4, "absolute_trend_score": 3,
    "relative_strength_score": 1, "breadth_score": None, "confidence": 0.8,
    "component_values": {"close_above_sma50": True},
}


def test_store_and_load_round_trip(conn):
    sr.store_sector_regime_day(conn, date(2026, 1, 2), "Financials", _CTX)
    row = sr.load_latest_sector_regime(conn, "Financials")
    assert row["classification"] == "bullish"
    assert row["sector_symbol"] == "XLF"
    assert row["trading_date"] == date(2026, 1, 2)


def test_store_upserts_same_date(conn):
    sr.store_sector_regime_day(conn, date(2026, 1, 2), "Financials", _CTX)
    bear_ctx = dict(_CTX, classification="bearish", total_score=-4)
    sr.store_sector_regime_day(conn, date(2026, 1, 2), "Financials", bear_ctx)

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sector_regime_history")
        assert cur.fetchone()[0] == 1

    row = sr.load_latest_sector_regime(conn, "Financials")
    assert row["classification"] == "bearish"


def test_load_latest_returns_none_when_no_data(conn):
    assert sr.load_latest_sector_regime(conn, "Financials") is None

"""
Tests for shared/security_regime.py.

Same two-tier shape as test_sector_regime.py: classify_security_regime is
pure; compute_security_regime/store_security_regime_day are tested against
a real Postgres connection. Covers the PR spec's "bullish stock
underperforming its sector" and "missing sector mapping" scenarios
specifically.
"""

import sys
import pathlib
from datetime import date, timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
p = str(ROOT / "shared")
if p not in sys.path:
    sys.path.insert(0, p)

for _mod in ("security_regime", "sector_mapping", "regime_common"):
    sys.modules.pop(_mod, None)
import security_regime as secr


def _rising_series(n, start, daily_pct):
    closes = [start]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + daily_pct))
    return closes


def _flat_series(n, level):
    return [level] * n


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
# classify_security_regime — pure
# ─────────────────────────────────────────────────────────────────────────

def test_bullish_stock_underperforming_sector():
    """Stock is trending up (bullish absolute trend) but its sector is
    rising faster -- vs_sector should be negative/underperforming even
    though the stock's own trend is bullish."""
    stock_closes = _rising_series(300, 100.0, 0.001)
    sector_closes = _rising_series(300, 40.0, 0.004)
    market_closes = _flat_series(300, 400.0)
    ctx = secr.classify_security_regime(stock_closes, sector_closes, market_closes)
    assert ctx["absolute_trend_score"] > 0
    assert ctx["vs_sector_score"] < 0
    assert ctx["vs_sector_classification"] == "underperforming_sector"


def test_missing_sector_mapping_still_computes_rest():
    """No sector series (unmapped sector) -- vs_sector inputs stay unknown,
    but absolute trend and vs_market still compute normally."""
    stock_closes = _rising_series(300, 100.0, 0.002)
    market_closes = _flat_series(300, 400.0)
    ctx = secr.classify_security_regime(stock_closes, [], market_closes)
    assert ctx["vs_sector_classification"] == "unknown"
    assert ctx["vs_sector_score"] == 0  # no evidence -> zero, not guessed
    assert ctx["absolute_trend_score"] > 0
    assert ctx["vs_market_score"] > 0
    assert ctx["classification"] != "insufficient_data"


def test_insufficient_history_all_none():
    stock_closes = _flat_series(10, 100.0)
    market_closes = _flat_series(10, 400.0)
    ctx = secr.classify_security_regime(stock_closes, [], market_closes)
    assert ctx["classification"] == "insufficient_data"
    assert ctx["confidence"] == 0.0


# ─────────────────────────────────────────────────────────────────────────
# compute_security_regime — DB-backed
# ─────────────────────────────────────────────────────────────────────────

def test_compute_security_regime_missing_sector_mapping(conn):
    stock_closes = _rising_series(300, 100.0, 0.002)
    market_closes = _flat_series(300, 400.0)
    _insert_prices(conn, "ACME", date(2024, 1, 1), stock_closes)
    _insert_prices(conn, "SPY", date(2024, 1, 1), market_closes)

    ctx = secr.compute_security_regime(conn, "ACME", sector="Not A Real Sector",
                                        as_of_date=date(2024, 12, 1))
    assert ctx["classification"] != "insufficient_data"
    assert ctx["vs_sector_classification"] == "unknown"
    assert ctx["sector_symbol"] is None


def test_compute_security_regime_insufficient_lookback(conn):
    _insert_prices(conn, "NEWCO", date(2026, 1, 1), _flat_series(5, 20.0))
    _insert_prices(conn, "SPY", date(2026, 1, 1), _flat_series(5, 400.0))
    ctx = secr.compute_security_regime(conn, "NEWCO", as_of_date=date(2026, 1, 5))
    assert ctx["classification"] == "insufficient_data"


def test_compute_security_regime_as_of_date_ignores_future_prices(conn):
    dates_start = date(2024, 1, 1)
    n = 300
    target_idx = 200

    stock_closes = _rising_series(n, 100.0, 0.001)
    sector_closes = _flat_series(n, 40.0)
    market_closes = _flat_series(n, 400.0)
    expected = secr.classify_security_regime(
        stock_closes[:target_idx + 1], sector_closes[:target_idx + 1], market_closes[:target_idx + 1])

    for i in range(target_idx + 1, n):
        stock_closes[i] = 1.0  # spike downward hard, should be ignored

    _insert_prices(conn, "ACME", dates_start, stock_closes)
    _insert_prices(conn, "XLF", dates_start, sector_closes)
    _insert_prices(conn, "SPY", dates_start, market_closes)

    target_date = dates_start + timedelta(days=target_idx)
    ctx = secr.compute_security_regime(conn, "ACME", sector="Financials", as_of_date=target_date)
    assert ctx["classification"] == expected["classification"]
    assert ctx["total_score"] == expected["total_score"]


# ─────────────────────────────────────────────────────────────────────────
# store_security_regime_day / load_latest_security_regime — round trip
# ─────────────────────────────────────────────────────────────────────────

_CTX = {
    "symbol": "ACME", "sector": "Financials", "benchmark_symbol": "SPY",
    "classification": "bullish", "total_score": 5, "absolute_trend_score": 3,
    "vs_sector_score": 1, "vs_market_score": 1, "confidence": 0.75,
    "component_values": {"abs_close_above_sma20": True},
}


def test_store_and_load_round_trip(conn):
    secr.store_security_regime_day(conn, date(2026, 1, 2), "ACME", _CTX)
    row = secr.load_latest_security_regime(conn, "ACME")
    assert row["classification"] == "bullish"
    assert row["sector"] == "Financials"
    assert row["trading_date"] == date(2026, 1, 2)


def test_store_upserts_same_date(conn):
    secr.store_security_regime_day(conn, date(2026, 1, 2), "ACME", _CTX)
    bear_ctx = dict(_CTX, classification="bearish", total_score=-5)
    secr.store_security_regime_day(conn, date(2026, 1, 2), "ACME", bear_ctx)

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM security_regime_history")
        assert cur.fetchone()[0] == 1

    row = secr.load_latest_security_regime(conn, "ACME")
    assert row["classification"] == "bearish"


def test_load_latest_returns_none_when_no_data(conn):
    assert secr.load_latest_security_regime(conn, "ACME") is None

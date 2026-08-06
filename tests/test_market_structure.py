"""
Tests for shared/market_structure.py -- no prior test coverage existed
(the module is new this PR). Everything here is a pure function over
in-memory OHLC lists; no DB fixture is needed, same reasoning as
tests/test_sector_regime.py's pure-function section.

Strategy: unit-test the small deterministic primitives (detect_swings,
_trend_direction, _detect_bos_choch) against hand-built, fully-verified-
by-hand series first, then a couple of end-to-end
classify_timeframe_structure()/combine_timeframe_structures() integration
tests against a synthetic zigzag series for the clear uptrend/downtrend
cases (a "mixed" full-pipeline case is deliberately not attempted end-to-
end -- whether floating-point sine-wave swing points land exactly equal/
higher/lower is too fragile to assert on; _trend_direction's own unit
test below covers "mixed" precisely instead).
"""

import sys
import pathlib
import math
from datetime import date, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
p = str(ROOT / "shared")
if p not in sys.path:
    sys.path.insert(0, p)

for _mod in ("market_structure", "regime_common"):
    sys.modules.pop(_mod, None)
import market_structure as ms

# tests/conftest.py's `conn` fixture (session-scoped schema + per-test
# truncate) is picked up automatically -- pytest discovers fixtures from
# conftest.py for every test file in the same directory tree.


def _zigzag_ohlc(n, start, trend_pct_per_bar, cycle_len=20, amplitude_frac=0.04,
                  start_date=date(2020, 1, 1)):
    """Deterministic oscillating-trend series: a clear net drift
    (trend_pct_per_bar) modulated by a sine wave (cycle_len bars per
    cycle, amplitude_frac of price) so swing highs/lows are unambiguous
    and well-separated -- good for exercising the full classify pipeline
    without hand-building hundreds of bars."""
    ohlc = []
    level = start
    d = start_date
    for i in range(n):
        level *= (1 + trend_pct_per_bar)
        c = level + level * amplitude_frac * math.sin(2 * math.pi * i / cycle_len)
        ohlc.append((d, c, c * 1.001, c * 0.999, c))
        d = d + timedelta(days=1)
    return ohlc


def _fake_tf(trend_direction, price=100.0, choch=False, choch_direction=None,
             bos=False, bos_direction=None, trend_strength="strong",
             vol_regime="normal", ema_alignment=True,
             nearest_support=None, nearest_resistance=None):
    """Hand-built dict matching classify_timeframe_structure()'s exact
    return shape, for testing combine_timeframe_structures() in isolation
    without depending on real swing detection to produce a specific
    alignment scenario."""
    return {
        "price": price,
        "trend_direction": trend_direction,
        "trend_strength": trend_strength,
        "swing_highs": [], "swing_lows": [],
        "bos": bos, "bos_direction": bos_direction,
        "choch": choch, "choch_direction": choch_direction,
        "nearest_support": nearest_support, "nearest_resistance": nearest_resistance,
        "volatility": {"atr": 1.0, "atr_percentile": 50.0, "regime": vol_regime},
        "component_values": {"ema_alignment": ema_alignment},
    }


# ─────────────────────────────────────────────────────────────────────────
# detect_swings -- hand-verified small series
# ─────────────────────────────────────────────────────────────────────────

def test_detect_swings_on_hand_built_series():
    """closes rise to a peak at index 3, fall to a trough at index 6, rise
    to a higher peak at index 10 -- verified by hand which indices are the
    true local max/min within a k=3 symmetric window."""
    closes = [5, 6, 7, 8, 7, 6, 5, 6, 7, 8, 9, 8, 7, 6, 5]
    d = date(2020, 1, 1)
    ohlc = [(d + timedelta(days=i), c, c, c, c) for i, c in enumerate(closes)]

    swing_highs, swing_lows = ms.detect_swings(ohlc, k=3)

    assert [s["price"] for s in swing_highs] == [8, 9]
    assert [s["price"] for s in swing_lows] == [5]


def test_detect_swings_too_short_returns_empty():
    closes = [1, 2, 3, 2, 1]
    d = date(2020, 1, 1)
    ohlc = [(d + timedelta(days=i), c, c, c, c) for i, c in enumerate(closes)]
    swing_highs, swing_lows = ms.detect_swings(ohlc, k=3)
    assert swing_highs == [] and swing_lows == []


# ─────────────────────────────────────────────────────────────────────────
# _trend_direction -- pure, hand-built swing lists
# ─────────────────────────────────────────────────────────────────────────

def test_trend_direction_higher_highs_higher_lows():
    highs = [{"date": date(2020, 1, 1), "price": 10}, {"date": date(2020, 1, 5), "price": 12}]
    lows = [{"date": date(2020, 1, 3), "price": 8}, {"date": date(2020, 1, 7), "price": 9}]
    assert ms._trend_direction(highs, lows) == "higher_highs_higher_lows"


def test_trend_direction_lower_highs_lower_lows():
    highs = [{"date": date(2020, 1, 1), "price": 12}, {"date": date(2020, 1, 5), "price": 10}]
    lows = [{"date": date(2020, 1, 3), "price": 9}, {"date": date(2020, 1, 7), "price": 7}]
    assert ms._trend_direction(highs, lows) == "lower_highs_lower_lows"


def test_trend_direction_mixed_when_highs_and_lows_disagree():
    highs = [{"date": date(2020, 1, 1), "price": 10}, {"date": date(2020, 1, 5), "price": 12}]
    lows = [{"date": date(2020, 1, 3), "price": 9}, {"date": date(2020, 1, 7), "price": 7}]
    assert ms._trend_direction(highs, lows) == "mixed"


def test_trend_direction_insufficient_data_with_fewer_than_two_points():
    highs = [{"date": date(2020, 1, 1), "price": 10}]
    lows = [{"date": date(2020, 1, 3), "price": 8}, {"date": date(2020, 1, 7), "price": 9}]
    assert ms._trend_direction(highs, lows) == "insufficient_data"


# ─────────────────────────────────────────────────────────────────────────
# _detect_bos_choch -- pure
# ─────────────────────────────────────────────────────────────────────────

def test_bos_bullish_when_uptrend_breaks_above_last_swing_high():
    highs = [{"date": date(2020, 1, 1), "price": 10}, {"date": date(2020, 1, 5), "price": 12}]
    lows = [{"date": date(2020, 1, 3), "price": 8}, {"date": date(2020, 1, 7), "price": 9}]
    result = ms._detect_bos_choch("higher_highs_higher_lows", highs, lows, last_close=13)
    assert result == {"bos": True, "bos_direction": "bullish", "choch": False, "choch_direction": None}


def test_choch_bearish_when_uptrend_breaks_below_last_swing_low():
    highs = [{"date": date(2020, 1, 1), "price": 10}, {"date": date(2020, 1, 5), "price": 12}]
    lows = [{"date": date(2020, 1, 3), "price": 8}, {"date": date(2020, 1, 7), "price": 9}]
    result = ms._detect_bos_choch("higher_highs_higher_lows", highs, lows, last_close=8.5)
    assert result == {"bos": False, "bos_direction": None, "choch": True, "choch_direction": "bearish"}


def test_bos_bearish_when_downtrend_breaks_below_last_swing_low():
    highs = [{"date": date(2020, 1, 1), "price": 12}, {"date": date(2020, 1, 5), "price": 10}]
    lows = [{"date": date(2020, 1, 3), "price": 9}, {"date": date(2020, 1, 7), "price": 7}]
    result = ms._detect_bos_choch("lower_highs_lower_lows", highs, lows, last_close=6)
    assert result == {"bos": True, "bos_direction": "bearish", "choch": False, "choch_direction": None}


def test_choch_bullish_when_downtrend_breaks_above_last_swing_high():
    highs = [{"date": date(2020, 1, 1), "price": 12}, {"date": date(2020, 1, 5), "price": 10}]
    lows = [{"date": date(2020, 1, 3), "price": 9}, {"date": date(2020, 1, 7), "price": 7}]
    result = ms._detect_bos_choch("lower_highs_lower_lows", highs, lows, last_close=10.5)
    assert result == {"bos": False, "bos_direction": None, "choch": True, "choch_direction": "bullish"}


def test_no_bos_choch_when_structure_is_mixed():
    highs = [{"date": date(2020, 1, 1), "price": 10}, {"date": date(2020, 1, 5), "price": 12}]
    lows = [{"date": date(2020, 1, 3), "price": 9}, {"date": date(2020, 1, 7), "price": 7}]
    result = ms._detect_bos_choch("mixed", highs, lows, last_close=100)
    assert result == {"bos": False, "bos_direction": None, "choch": False, "choch_direction": None}


# ─────────────────────────────────────────────────────────────────────────
# classify_timeframe_structure -- end to end, synthetic zigzag series
# ─────────────────────────────────────────────────────────────────────────

def test_classify_uptrend_series_is_higher_highs_higher_lows():
    ohlc = _zigzag_ohlc(n=260, start=100.0, trend_pct_per_bar=0.0015, cycle_len=20)
    result = ms.classify_timeframe_structure(ohlc)
    assert result["trend_direction"] == "higher_highs_higher_lows"
    assert len(result["swing_highs"]) >= 2
    assert len(result["swing_lows"]) >= 2
    assert result["trend_strength"] in ("weak", "moderate", "strong")
    assert result["volatility"]["regime"] in ("compression", "normal", "expansion")


def test_classify_downtrend_series_is_lower_highs_lower_lows():
    ohlc = _zigzag_ohlc(n=260, start=100.0, trend_pct_per_bar=-0.0015, cycle_len=20)
    result = ms.classify_timeframe_structure(ohlc)
    assert result["trend_direction"] == "lower_highs_lower_lows"
    assert len(result["swing_highs"]) >= 2
    assert len(result["swing_lows"]) >= 2


def test_classify_insufficient_history_returns_insufficient_data_not_a_guess():
    ohlc = _zigzag_ohlc(n=5, start=100.0, trend_pct_per_bar=0.001)
    result = ms.classify_timeframe_structure(ohlc)
    assert result["trend_direction"] == "insufficient_data"
    assert result["trend_strength"] == "insufficient_data"
    assert result["swing_highs"] == [] and result["swing_lows"] == []
    assert result["volatility"]["regime"] == "insufficient_data"
    assert result["price"] is None


# ─────────────────────────────────────────────────────────────────────────
# combine_timeframe_structures -- pure, hand-built per-timeframe dicts
# ─────────────────────────────────────────────────────────────────────────

def test_combine_all_timeframes_aligned_bullish_is_high_confidence_low_risk():
    monthly = _fake_tf("higher_highs_higher_lows")
    weekly = _fake_tf("higher_highs_higher_lows")
    daily = _fake_tf("higher_highs_higher_lows", trend_strength="strong")
    result = ms.combine_timeframe_structures(monthly, weekly, daily)
    assert result["trend"] == "bullish"
    assert result["confidence"] >= 70
    assert result["risk"] == "low"
    assert result["bos"] is False and result["choch"] is False
    assert "Monthly" in result["summary"]


def test_combine_disagreeing_monthly_weekly_is_mixed():
    monthly = _fake_tf("higher_highs_higher_lows")
    weekly = _fake_tf("lower_highs_lower_lows")
    daily = _fake_tf("higher_highs_higher_lows")
    result = ms.combine_timeframe_structures(monthly, weekly, daily)
    assert result["trend"] == "mixed"


def test_combine_choch_on_daily_forces_high_risk():
    monthly = _fake_tf("higher_highs_higher_lows")
    weekly = _fake_tf("higher_highs_higher_lows")
    daily = _fake_tf("higher_highs_higher_lows", choch=True, choch_direction="bearish")
    result = ms.combine_timeframe_structures(monthly, weekly, daily)
    assert result["choch"] is True
    assert result["risk"] == "high"
    assert "CHoCH" in result["summary"]


def test_combine_insufficient_monthly_data_yields_insufficient_trend():
    monthly = _fake_tf("insufficient_data", trend_strength="insufficient_data")
    weekly = _fake_tf("higher_highs_higher_lows")
    daily = _fake_tf("higher_highs_higher_lows")
    result = ms.combine_timeframe_structures(monthly, weekly, daily)
    assert result["trend"] == "insufficient_data"


# ─────────────────────────────────────────────────────────────────────────
# resample_weekly / resample_monthly -- pure
# ─────────────────────────────────────────────────────────────────────────

def test_resample_weekly_groups_by_iso_week():
    # Mon(2024-01-01) .. Sun(2024-01-07) is ISO week 1; Mon(2024-01-08) starts week 2.
    d = date(2024, 1, 1)
    ohlc = [(d + timedelta(days=i), 10 + i, 10 + i + 1, 10 + i - 1, 10 + i) for i in range(10)]
    weekly = ms.resample_weekly(ohlc)
    assert len(weekly) == 2
    week1 = weekly[0]
    assert week1[0] == date(2024, 1, 7)   # dated to the group's last bar
    assert week1[1] == ohlc[0][1]          # open = first bar's open
    assert week1[4] == ohlc[6][4]          # close = last bar's close
    assert week1[2] == max(b[2] for b in ohlc[:7])
    assert week1[3] == min(b[3] for b in ohlc[:7])


def test_resample_monthly_groups_by_calendar_month():
    d = date(2024, 1, 25)
    ohlc = [(d + timedelta(days=i), 10 + i, 10 + i + 1, 10 + i - 1, 10 + i) for i in range(10)]  # crosses into Feb
    monthly = ms.resample_monthly(ohlc)
    jan_bars = [b for b in ohlc if b[0].month == 1]
    assert len(monthly) == 2
    assert monthly[0][0] == jan_bars[-1][0]
    assert monthly[0][4] == jan_bars[-1][4]


# ─────────────────────────────────────────────────────────────────────────
# compute_market_structure / store_market_structure_day /
# load_latest_market_structure -- I/O, real disposable Postgres
# ─────────────────────────────────────────────────────────────────────────

def _insert_daily_ohlc(conn, symbol, start_date, closes):
    with conn.cursor() as cur:
        for i, c in enumerate(closes):
            d = start_date + timedelta(days=i)
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, (symbol, d, c, c * 1.001, c * 0.999, c, 1000))
    conn.commit()


def test_compute_market_structure_insufficient_history(conn):
    _insert_daily_ohlc(conn, "XYZ", date(2026, 1, 1), [100.0, 101.0, 100.0])
    ctx = ms.compute_market_structure(conn, "XYZ", as_of_date=date(2026, 1, 3))
    assert ctx["trend"] == "insufficient_data"
    assert ctx["symbol"] == "XYZ"


def test_compute_market_structure_uptrend_end_to_end(conn):
    closes = _zigzag_ohlc(n=400, start=100.0, trend_pct_per_bar=0.0015, cycle_len=20,
                           start_date=date(2024, 1, 1))
    with conn.cursor() as cur:
        for d, o, h, l, c in closes:
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, ("XYZ", d, o, h, l, c, 1000))
    conn.commit()

    ctx = ms.compute_market_structure(conn, "XYZ", as_of_date=closes[-1][0])
    assert ctx["daily"]["trend_direction"] == "higher_highs_higher_lows"
    # monthly/weekly may still be too thin at 400 daily bars (~19 monthly
    # bars) to produce 2 swing highs + 2 swing lows -- any of these three
    # outcomes is a legitimate, non-guessed result.
    assert ctx["trend"] in ("bullish", "mixed", "insufficient_data")


def test_compute_market_structure_as_of_date_ignores_future_prices(conn):
    """Same no-lookahead regression shape as
    test_sector_regime.test_compute_sector_regime_as_of_date_ignores_future_prices:
    a future price spike must not change an as-of classification."""
    n = 400
    target_idx = 300
    ohlc = _zigzag_ohlc(n=n, start=100.0, trend_pct_per_bar=0.0015, cycle_len=20,
                         start_date=date(2024, 1, 1))
    expected = ms.classify_timeframe_structure(ohlc[:target_idx + 1])

    spiked = list(ohlc)
    for i in range(target_idx + 1, n):
        d, o, h, l, c = spiked[i]
        spiked[i] = (d, 9999.0, 9999.0, 9999.0, 9999.0)

    with conn.cursor() as cur:
        for d, o, h, l, c in spiked:
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, ("XYZ", d, o, h, l, c, 1000))
    conn.commit()

    target_date = ohlc[target_idx][0]
    ctx = ms.compute_market_structure(conn, "XYZ", as_of_date=target_date)
    assert ctx["daily"]["trend_direction"] == expected["trend_direction"]
    assert ctx["daily"]["price"] == expected["price"]


_STORE_CTX = {
    "trend": "bullish", "confidence": 82, "trend_strength": "strong", "volatility": "normal",
    "bos": True, "choch": False, "risk": "low", "summary": "Monthly bullish; Weekly bullish",
    "monthly": {"trend_direction": "higher_highs_higher_lows", "swing_highs": [
        {"date": date(2024, 1, 1), "price": 10.0}]},
    "weekly": {"trend_direction": "higher_highs_higher_lows"},
    "daily": {"trend_direction": "higher_highs_higher_lows", "nearest_support": {
        "price": 9.0, "touch_count": 2, "last_touched": date(2024, 6, 1)}},
}


def test_store_and_load_round_trip(conn):
    ms.store_market_structure_day(conn, date(2026, 1, 2), "XYZ", _STORE_CTX)
    row = ms.load_latest_market_structure(conn, "XYZ")
    assert row["trend"] == "bullish"
    assert row["bos"] is True
    assert row["trading_date"] == date(2026, 1, 2)
    # component_values round-trips through Json() with dates -> ISO strings
    assert row["component_values"]["monthly"]["swing_highs"][0]["date"] == "2024-01-01"


def test_store_upserts_same_date(conn):
    ms.store_market_structure_day(conn, date(2026, 1, 2), "XYZ", _STORE_CTX)
    bear_ctx = dict(_STORE_CTX, trend="bearish", confidence=20)
    ms.store_market_structure_day(conn, date(2026, 1, 2), "XYZ", bear_ctx)

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM market_structure_history")
        assert cur.fetchone()[0] == 1

    row = ms.load_latest_market_structure(conn, "XYZ")
    assert row["trend"] == "bearish"


def test_load_latest_returns_none_when_no_data(conn):
    assert ms.load_latest_market_structure(conn, "NOPE") is None


# ─────────────────────────────────────────────────────────────────────────
# snapshot_market_structure_for_symbol / update_market_structure
# ─────────────────────────────────────────────────────────────────────────

def test_snapshot_returns_insufficient_data_shape_when_nothing_persisted(conn):
    snap = ms.snapshot_market_structure_for_symbol(conn, "NEVER_COMPUTED")
    assert snap["trend"] == "insufficient_data"
    assert snap["confidence"] == 0
    assert snap["symbol"] == "NEVER_COMPUTED"


def test_snapshot_reads_back_whats_persisted(conn):
    ms.store_market_structure_day(conn, date(2026, 1, 2), "XYZ", _STORE_CTX)
    snap = ms.snapshot_market_structure_for_symbol(conn, "XYZ")
    assert snap["trend"] == "bullish"
    assert snap["confidence"] == 82.0
    assert snap["bos"] is True


def test_update_market_structure_persists_rows_from_real_price_history(conn):
    ohlc = _zigzag_ohlc(n=300, start=50.0, trend_pct_per_bar=0.001, cycle_len=20,
                         start_date=date(2024, 1, 1))
    with conn.cursor() as cur:
        for d, o, h, l, c in ohlc:
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, ("XYZ", d, o, h, l, c, 1000))
    conn.commit()

    ms.update_market_structure(conn, ["XYZ", "NO_PRICE_DATA_AT_ALL"])

    row = ms.load_latest_market_structure(conn, "XYZ")
    assert row is not None
    assert row["trend"] in ("bullish", "mixed", "insufficient_data")
    # a symbol with zero price_history rows degrades to an insufficient_data
    # row (compute_market_structure never raises for missing data) rather
    # than raising and blocking the rest of the batch.
    empty_row = ms.load_latest_market_structure(conn, "NO_PRICE_DATA_AT_ALL")
    assert empty_row is not None
    assert empty_row["trend"] == "insufficient_data"

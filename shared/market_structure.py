"""
Market Structure Engine. Phase 1 (this file's top section): pure,
deterministic top-down trend/structure classification for Monthly/
Weekly/Daily timeframes -- no I/O, no DB. Phase 2 (bottom section,
mirrors shared/sector_regime.py's single-file "pure classify + I/O
compute/store/load" pattern exactly): compute_market_structure() resamples
real price_history into the three timeframes and persists one row per
symbol per day to market_structure_history, same as sector_regime_history/
security_regime_history. Strategy/scoring integration is still a
follow-up PR -- this snapshot is computed and queryable but not yet
load-bearing for any score or gate, same staged-rollout discipline as
shared/sector_regime.py/shared/security_regime.py/shared/hierarchy_regime.py,
all merged inert before anything downstream depended on them.

4H/1H timeframes are deliberately NOT supported by this module.
price_history_hourly only has a few weeks of history for a subset of
symbols today (see ingest/ingest.py's ingest_hourly_prices docstring) --
nowhere near enough bars to detect meaningful swing structure. Adding
those timeframes is a follow-up once ingest/backfill_intraday_alpaca.py
has actually been run and enough history has accumulated.

Every classification here follows the same "None means couldn't be
evaluated, never coerced to a guess" convention shared/regime_common.py
established -- a short OHLC window degrades individual fields to None/
"insufficient_data" rather than producing a false-confidence result.

An OHLC bar is a plain tuple `(date, open, high, low, close)`, oldest to
newest -- same shape convention as shared/signals.py's (high, low, close)
loader, extended with date+open since swing/BOS/CHoCH detection needs the
bar's actual calendar position and support/resistance zones are priced
off highs/lows, not just closes.
"""

import logging
import sys
import pathlib
from datetime import date as date_cls

from psycopg2.extras import Json, RealDictCursor

_here = pathlib.Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from regime_common import score_inputs, load_daily_ohlc, asof_index  # noqa: E402

log = logging.getLogger(__name__)

CALCULATION_VERSION = 1

EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
ADX_PERIOD = 14
REGRESSION_LOOKBACK = 20
ATR_PERIOD = 14
ATR_PERCENTILE_LOOKBACK = 100
SWING_K = 3          # bars on each side a pivot must beat to count as a swing point
SR_ATR_MULT = 0.5    # support/resistance cluster tolerance, in units of ATR
REGRESSION_SLOPE_STEEP_PCT = 0.03  # %-of-price-per-bar threshold for a "steep" trend


# ─────────────────────────────────────────────────────────────────────────
# Primitives -- none of these existed anywhere in the repo before this PR
# (confirmed: zero hits for swing/support-resistance/adx/average-directional)
# ─────────────────────────────────────────────────────────────────────────

def ema(closes, period):
    """Latest EMA value only (mirrors regime_common.sma's "just the current
    value" shape, not a full series) -- seeded with a plain SMA of the
    first `period` closes, standard convention."""
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(closes[:period]) / period
    for c in closes[period:]:
        e = c * k + e * (1 - k)
    return e


def regression_slope(closes, lookback):
    """Least-squares slope of the trailing `lookback` closes, normalized to
    %-of-mean-price per bar so it's comparable across symbols/timeframes
    regardless of absolute price level."""
    if len(closes) < lookback:
        return None
    window = closes[-lookback:]
    n = len(window)
    mean_x = (n - 1) / 2.0
    mean_y = sum(window) / n
    if not mean_y:
        return None
    num = sum((i - mean_x) * (window[i] - mean_y) for i in range(n))
    den = sum((i - mean_x) ** 2 for i in range(n))
    if den == 0:
        return None
    slope = num / den
    return (slope / mean_y) * 100.0


def adx(ohlc, period=ADX_PERIOD):
    """Wilder's ADX from a list of (date, open, high, low, close)
    oldest->newest. Returns the latest ADX value, or None if there isn't
    enough history for a stable smoothed value (needs ~2*period+1 bars)."""
    if len(ohlc) < period * 2 + 1:
        return None
    plus_dm, minus_dm, tr = [], [], []
    for i in range(1, len(ohlc)):
        _, _, h, l, c = ohlc[i]
        _, _, ph, pl, pc = ohlc[i - 1]
        up_move = h - ph
        down_move = pl - l
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))

    def _wilder_smooth(series, period):
        smoothed = [sum(series[:period])]
        for v in series[period:]:
            smoothed.append(smoothed[-1] - (smoothed[-1] / period) + v)
        return smoothed

    tr_s = _wilder_smooth(tr, period)
    plus_s = _wilder_smooth(plus_dm, period)
    minus_s = _wilder_smooth(minus_dm, period)

    dxs = []
    for i in range(len(tr_s)):
        if not tr_s[i]:
            continue
        plus_di = 100 * plus_s[i] / tr_s[i]
        minus_di = 100 * minus_s[i] / tr_s[i]
        denom = plus_di + minus_di
        if denom:
            dxs.append(100 * abs(plus_di - minus_di) / denom)
    if len(dxs) < period:
        return None
    adx_val = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period
    return adx_val


def _atr_series(ohlc, period=ATR_PERIOD):
    """Full Wilder ATR series (not just the latest value, unlike
    signals.compute_atr) -- needed for ATR-percentile/compression-expansion
    classification, which requires the trailing distribution, not one
    number. Oldest->newest, aligned to ohlc[period:]."""
    if len(ohlc) < period + 1:
        return []
    trs = []
    prev_close = ohlc[0][4]
    for _, _, h, l, c in ohlc[1:]:
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
        prev_close = c
    if len(trs) < period:
        return []
    series = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        series.append((series[-1] * (period - 1) + tr) / period)
    return series


def _percentile_rank(value, series):
    if not series:
        return None
    below = sum(1 for x in series if x <= value)
    return round(100.0 * below / len(series), 1)


def detect_swings(ohlc, k=SWING_K):
    """Fractal swing-point detection: bar i is a swing high if its high is
    the max high in the symmetric window [i-k, i+k] (swing low symmetric on
    lows). Deterministic, no external dependency. Returns
    (swing_highs, swing_lows), each a chronological list of
    {"date", "price"} dicts."""
    n = len(ohlc)
    swing_highs, swing_lows = [], []
    for i in range(k, n - k):
        window = ohlc[i - k:i + k + 1]
        highs = [b[2] for b in window]
        lows = [b[3] for b in window]
        if ohlc[i][2] == max(highs):
            swing_highs.append({"date": ohlc[i][0], "price": ohlc[i][2]})
        if ohlc[i][3] == min(lows):
            swing_lows.append({"date": ohlc[i][0], "price": ohlc[i][3]})
    return swing_highs, swing_lows


def _cluster_zones(points, tolerance):
    """Greedy 1-D clustering of swing points into support/resistance zones:
    sort by price, merge a point into the running cluster if it's within
    `tolerance` of the cluster's last member. Returns
    [{"price", "touch_count", "last_touched"}, ...]."""
    if not points:
        return []
    pts = sorted(points, key=lambda p: p["price"])
    zones, current = [], [pts[0]]
    for p in pts[1:]:
        if p["price"] - current[-1]["price"] <= tolerance:
            current.append(p)
        else:
            zones.append(current)
            current = [p]
    zones.append(current)
    result = []
    for z in zones:
        prices = [p["price"] for p in z]
        dates = [p["date"] for p in z]
        result.append({
            "price": sum(prices) / len(prices),
            "touch_count": len(z),
            "last_touched": max(dates),
        })
    return result


def _nearest_support_resistance(swing_highs, swing_lows, price, atr_latest):
    tolerance = (atr_latest or 0) * SR_ATR_MULT
    if tolerance <= 0:
        tolerance = price * 0.005  # ATR unavailable (short window) -- fall back to 0.5% of price
    zones = _cluster_zones(swing_highs + swing_lows, tolerance)
    below = [z for z in zones if z["price"] < price]
    above = [z for z in zones if z["price"] > price]
    nearest_support = max(below, key=lambda z: z["price"]) if below else None
    nearest_resistance = min(above, key=lambda z: z["price"]) if above else None
    return nearest_support, nearest_resistance


# ─────────────────────────────────────────────────────────────────────────
# Structure classification
# ─────────────────────────────────────────────────────────────────────────

def _trend_direction(swing_highs, swing_lows):
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "insufficient_data"
    highs_rising = swing_highs[-1]["price"] > swing_highs[-2]["price"]
    highs_falling = swing_highs[-1]["price"] < swing_highs[-2]["price"]
    lows_rising = swing_lows[-1]["price"] > swing_lows[-2]["price"]
    lows_falling = swing_lows[-1]["price"] < swing_lows[-2]["price"]
    if highs_rising and lows_rising:
        return "higher_highs_higher_lows"
    if highs_falling and lows_falling:
        return "lower_highs_lower_lows"
    return "mixed"


def _detect_bos_choch(trend_direction, swing_highs, swing_lows, last_close):
    """BOS (break of structure) = price extends the established trend past
    its most recent swing point -- continuation. CHoCH (change of
    character) = price breaks the most recent swing point AGAINST the
    established trend -- an early warning, not a confirmed reversal. Only
    defined when a trend is actually established (HH/HL or LH/LL); mixed
    or insufficient-data structure can't be "broken.\""""
    none_result = {"bos": False, "bos_direction": None, "choch": False, "choch_direction": None}
    if trend_direction not in ("higher_highs_higher_lows", "lower_highs_lower_lows"):
        return none_result
    last_high = swing_highs[-1]["price"] if swing_highs else None
    last_low = swing_lows[-1]["price"] if swing_lows else None
    if trend_direction == "higher_highs_higher_lows":
        if last_high is not None and last_close > last_high:
            return {"bos": True, "bos_direction": "bullish", "choch": False, "choch_direction": None}
        if last_low is not None and last_close < last_low:
            return {"bos": False, "bos_direction": None, "choch": True, "choch_direction": "bearish"}
    else:
        if last_low is not None and last_close < last_low:
            return {"bos": True, "bos_direction": "bearish", "choch": False, "choch_direction": None}
        if last_high is not None and last_close > last_high:
            return {"bos": False, "bos_direction": None, "choch": True, "choch_direction": "bullish"}
    return none_result


def _trend_strength(price, trend_direction, ema20, ema50, ema200, adx_val, slope_pct):
    """weak/moderate/strong, scored the same score_inputs()/fraction-of-
    max-score way shared/regime_common.classify_from_score buckets regime
    confidence -- direction-agnostic magnitude of how well EMA stack, ADX,
    slope, and distance-from-EMA200 all agree with whatever direction
    _trend_direction already established."""
    if trend_direction not in ("higher_highs_higher_lows", "lower_highs_lower_lows"):
        return "insufficient_data", {}
    bullish = trend_direction == "higher_highs_higher_lows"
    inputs = {}
    if ema20 is not None and ema50 is not None and ema200 is not None:
        inputs["ema_alignment"] = (
            (price > ema20 > ema50 > ema200) if bullish else (price < ema20 < ema50 < ema200)
        )
    else:
        inputs["ema_alignment"] = None
    inputs["adx_strong"] = (adx_val > 25) if adx_val is not None else None
    if slope_pct is not None:
        inputs["regression_slope_aligned"] = (
            slope_pct > REGRESSION_SLOPE_STEEP_PCT if bullish else slope_pct < -REGRESSION_SLOPE_STEEP_PCT
        )
    else:
        inputs["regression_slope_aligned"] = None
    if ema200:
        inputs["distance_from_ema200_significant"] = abs(price - ema200) / ema200 > 0.05
    else:
        inputs["distance_from_ema200_significant"] = None

    score, evidence, total = score_inputs(inputs)
    if evidence == 0:
        return "insufficient_data", inputs
    frac = score / total
    if frac >= 0.5:
        label = "strong"
    elif frac >= 0:
        label = "moderate"
    else:
        label = "weak"
    return label, inputs


def _volatility_context(ohlc):
    series = _atr_series(ohlc)
    if not series:
        return {"atr": None, "atr_percentile": None, "regime": "insufficient_data"}
    latest = series[-1]
    pct = _percentile_rank(latest, series[-ATR_PERCENTILE_LOOKBACK:])
    if pct is None:
        regime = "insufficient_data"
    elif pct < 25:
        regime = "compression"
    elif pct > 75:
        regime = "expansion"
    else:
        regime = "normal"
    return {"atr": round(latest, 4), "atr_percentile": pct, "regime": regime}


def classify_timeframe_structure(ohlc, k=SWING_K):
    """Pure function: given one timeframe's OHLC bars (oldest->newest,
    already whatever lookback the caller wants), classify swing structure,
    trend direction/strength, BOS/CHoCH, volatility, and nearest support/
    resistance. No DB, no network -- see module docstring."""
    if len(ohlc) < 2 * k + 1:
        return {
            "price": None,
            "trend_direction": "insufficient_data", "trend_strength": "insufficient_data",
            "swing_highs": [], "swing_lows": [],
            "bos": False, "bos_direction": None, "choch": False, "choch_direction": None,
            "nearest_support": None, "nearest_resistance": None,
            "volatility": {"atr": None, "atr_percentile": None, "regime": "insufficient_data"},
            "component_values": {},
        }

    closes = [b[4] for b in ohlc]
    price = closes[-1]
    swing_highs, swing_lows = detect_swings(ohlc, k)
    trend_direction = _trend_direction(swing_highs, swing_lows)
    ema20, ema50, ema200 = ema(closes, EMA_FAST), ema(closes, EMA_MID), ema(closes, EMA_SLOW)
    adx_val = adx(ohlc)
    slope_pct = regression_slope(closes, REGRESSION_LOOKBACK)
    trend_strength, strength_inputs = _trend_strength(
        price, trend_direction, ema20, ema50, ema200, adx_val, slope_pct)
    bc = _detect_bos_choch(trend_direction, swing_highs, swing_lows, price)
    volatility = _volatility_context(ohlc)
    nearest_support, nearest_resistance = _nearest_support_resistance(
        swing_highs, swing_lows, price, volatility["atr"])

    return {
        "price": price,
        "trend_direction": trend_direction,
        "trend_strength": trend_strength,
        "swing_highs": swing_highs[-5:],
        "swing_lows": swing_lows[-5:],
        "bos": bc["bos"], "bos_direction": bc["bos_direction"],
        "choch": bc["choch"], "choch_direction": bc["choch_direction"],
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "volatility": volatility,
        "component_values": {
            "ema20": ema20, "ema50": ema50, "ema200": ema200,
            "adx": adx_val, "regression_slope_pct": slope_pct,
            **strength_inputs,
        },
    }


def _support_ok(tf):
    ns = tf.get("nearest_support")
    if ns is None or tf.get("price") is None:
        return None
    return tf["price"] > ns["price"]


def _resistance_ok(tf):
    nr = tf.get("nearest_resistance")
    if nr is None or not tf.get("price"):
        return None
    return (nr["price"] - tf["price"]) / tf["price"] > 0.02


def combine_timeframe_structures(monthly, weekly, daily):
    """Top-down combiner: each argument is the dict classify_timeframe_
    structure() returns for that timeframe. Produces the top-level
    MarketStructure-shaped dict the spec calls for -- trend/confidence/
    per-timeframe breakdown/trend_strength/volatility/bos/choch/nearest
    support & resistance/risk/summary. 4H/1H are deliberately not accepted
    here -- see module docstring."""

    def _direction_bool(tf, direction):
        return tf["trend_direction"] == direction if tf["trend_direction"] != "insufficient_data" else None

    monthly_bull, monthly_bear = _direction_bool(monthly, "higher_highs_higher_lows"), _direction_bool(monthly, "lower_highs_lower_lows")
    weekly_bull, weekly_bear = _direction_bool(weekly, "higher_highs_higher_lows"), _direction_bool(weekly, "lower_highs_lower_lows")
    daily_bull, daily_bear = _direction_bool(daily, "higher_highs_higher_lows"), _direction_bool(daily, "lower_highs_lower_lows")

    if monthly_bull and weekly_bull:
        overall_trend = "bullish"
    elif monthly_bear and weekly_bear:
        overall_trend = "bearish"
    elif monthly["trend_direction"] == "insufficient_data" or weekly["trend_direction"] == "insufficient_data":
        overall_trend = "insufficient_data"
    else:
        overall_trend = "mixed"

    if overall_trend == "bullish":
        daily_aligned = daily_bull
    elif overall_trend == "bearish":
        daily_aligned = daily_bear
    else:
        daily_aligned = None

    monthly_aligned = monthly_bull if overall_trend == "bullish" else (monthly_bear if overall_trend == "bearish" else None)
    weekly_aligned = weekly_bull if overall_trend == "bullish" else (weekly_bear if overall_trend == "bearish" else None)

    weighted = [
        ("monthly_trend_aligned", monthly_aligned, 3),
        ("weekly_trend_aligned", weekly_aligned, 2),
        ("daily_trend_aligned", daily_aligned, 1),
        ("ema_alignment", daily["component_values"].get("ema_alignment"), 1),
        ("no_structural_break",
         (not daily["choch"]) if daily["trend_direction"] != "insufficient_data" else None, 1),
        ("trend_strength_ok",
         (daily["trend_strength"] in ("strong", "moderate"))
         if daily["trend_strength"] != "insufficient_data" else None, 1),
        ("distance_from_support_ok", _support_ok(daily), 1),
        ("distance_from_resistance_ok", _resistance_ok(daily), 1),
        ("volatility_favorable",
         (daily["volatility"]["regime"] != "expansion")
         if daily["volatility"]["regime"] != "insufficient_data" else None, 1),
    ]
    score = sum(w if v else -w for _, v, w in weighted if v is not None)
    evidence_weight = sum(w for _, v, w in weighted if v is not None)
    # 0-100 by design (the spec explicitly wants this range) -- a deliberate
    # divergence from regime_common's 0.0-1.0 confidence convention.
    confidence = round(100 * (score + evidence_weight) / (2 * evidence_weight)) if evidence_weight else 0

    bos = daily["bos"] or weekly["bos"]
    choch = daily["choch"] or weekly["choch"]

    if choch or daily["volatility"]["regime"] == "expansion":
        risk = "high"
    elif overall_trend in ("bullish", "bearish") and confidence >= 60:
        risk = "low"
    else:
        risk = "medium"

    parts = []
    if overall_trend != "insufficient_data":
        parts.append(f"Monthly {monthly['trend_direction']}")
        parts.append(f"Weekly {weekly['trend_direction']}")
        parts.append(f"Daily {daily['trend_direction']} ({daily['trend_strength']} strength)")
    if daily["component_values"].get("ema_alignment"):
        parts.append("EMA alignment confirms trend")
    if choch:
        parts.append(f"CHoCH warning ({daily.get('choch_direction') or weekly.get('choch_direction')})")
    if bos:
        parts.append(f"BOS confirmed ({daily.get('bos_direction') or weekly.get('bos_direction')})")
    if daily.get("nearest_resistance") and daily.get("price"):
        dist = (daily["nearest_resistance"]["price"] - daily["price"]) / daily["price"] * 100
        parts.append(f"Resistance {dist:.1f}% away")
    summary = "; ".join(parts) if parts else "Insufficient data for structure analysis"

    return {
        "trend": overall_trend,
        "confidence": confidence,
        "monthly": monthly, "weekly": weekly, "daily": daily,
        "trend_strength": daily["trend_strength"],
        "volatility": daily["volatility"]["regime"],
        "bos": bos, "choch": choch,
        "nearest_support": daily["nearest_support"],
        "nearest_resistance": daily["nearest_resistance"],
        "risk": risk,
        "summary": summary,
    }


# ─────────────────────────────────────────────────────────────────────────
# Phase 2: resampling + I/O (load price_history, persist to
# market_structure_history, snapshot for trade_proposals). Mirrors
# shared/sector_regime.py's single-file "pure classify, then compute/
# store/load" layout.
# ─────────────────────────────────────────────────────────────────────────

MIN_DAILY_BARS = 2 * SWING_K + 1


def _resample(ohlc, key_fn):
    """Group consecutive bars sharing key_fn(date) into one bar: open of
    the first, close of the last, high/low across the group, dated to the
    group's last bar. Pure function, no I/O -- used for both weekly and
    monthly so there's exactly one grouping/aggregation implementation."""
    groups, order = {}, []
    for bar in ohlc:
        k = key_fn(bar[0])
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(bar)
    resampled = []
    for k in order:
        bars = groups[k]
        resampled.append((
            bars[-1][0],
            bars[0][1],
            max(b[2] for b in bars),
            min(b[3] for b in bars),
            bars[-1][4],
        ))
    return resampled


def resample_weekly(ohlc):
    return _resample(ohlc, lambda d: d.isocalendar()[:2])


def resample_monthly(ohlc):
    return _resample(ohlc, lambda d: (d.year, d.month))


def _json_safe(obj):
    """Recursively convert date objects (swing-point dates, S/R zone
    last_touched dates) to ISO strings so the combined structure dict can
    go straight into a JSONB column via Json() -- psycopg2's default JSON
    encoder can't serialize datetime.date on its own."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, date_cls):
        return obj.isoformat()
    return obj


def compute_market_structure(conn, symbol, as_of_date=None):
    """I/O orchestrator: load symbol's daily price_history, as-of slice
    (no lookahead), resample to weekly/monthly, classify all three, combine.
    Never raises -- insufficient history degrades to the "insufficient_data"
    shape classify_timeframe_structure/combine_timeframe_structures already
    produce, same convention as compute_sector_regime/compute_security_regime."""
    target_date = as_of_date or date_cls.today()
    try:
        daily_all = load_daily_ohlc(conn, symbol)
    except Exception:
        daily_all = []

    dates = [b[0] for b in daily_all]
    idx = asof_index(dates, target_date)
    daily_ohlc = daily_all[:idx + 1] if idx is not None else []

    weekly_ohlc = resample_weekly(daily_ohlc)
    monthly_ohlc = resample_monthly(daily_ohlc)

    daily = classify_timeframe_structure(daily_ohlc)
    weekly = classify_timeframe_structure(weekly_ohlc)
    monthly = classify_timeframe_structure(monthly_ohlc)

    combined = combine_timeframe_structures(monthly, weekly, daily)
    combined["symbol"] = symbol
    return combined


def store_market_structure_day(conn, trading_date, symbol, ctx):
    """Upsert one day's market structure classification. component_values
    holds the full combined dict (per-timeframe breakdown + summary) --
    same "flat queryable columns + full JSONB detail" split as
    sector_regime_history/security_regime_history."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO market_structure_history
                (trading_date, symbol, trend, confidence, trend_strength, volatility,
                 bos, choch, risk, component_values, calculation_version, computed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (trading_date, symbol) DO UPDATE SET
                trend=EXCLUDED.trend,
                confidence=EXCLUDED.confidence,
                trend_strength=EXCLUDED.trend_strength,
                volatility=EXCLUDED.volatility,
                bos=EXCLUDED.bos,
                choch=EXCLUDED.choch,
                risk=EXCLUDED.risk,
                component_values=EXCLUDED.component_values,
                calculation_version=EXCLUDED.calculation_version,
                computed_at=NOW()
        """, (
            trading_date, symbol, ctx.get("trend"), ctx.get("confidence"),
            ctx.get("trend_strength"), ctx.get("volatility"), ctx.get("bos"), ctx.get("choch"),
            ctx.get("risk"), Json(_json_safe(ctx)), CALCULATION_VERSION,
        ))
    conn.commit()


def load_latest_market_structure(conn, symbol):
    """Latest persisted market structure row for symbol, or None."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM market_structure_history
                WHERE symbol=%s ORDER BY trading_date DESC LIMIT 1
            """, (symbol,))
            row = cur.fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _f(v):
    """NUMERIC columns come back as Decimal via psycopg2 -- cast to plain
    float (None stays None) before it goes anywhere JSON-serialized. Same
    helper shared/hierarchy_regime.py uses for the same reason."""
    return float(v) if v is not None else None


def snapshot_market_structure_for_symbol(conn, symbol):
    """Assembles the per-proposal market structure snapshot from whatever's
    latest-persisted (not recomputed live) -- update_market_structure is
    expected to have already run this cycle, same "compute once, snapshot
    read at proposal time" split as hierarchy_regime.snapshot_hierarchy_for_symbol.
    Missing data degrades to an explicit insufficient_data/unknown shape
    rather than blocking snapshot assembly."""
    row = load_latest_market_structure(conn, symbol)
    if not row:
        return {
            "symbol": symbol, "trend": "insufficient_data", "confidence": 0,
            "trend_strength": "insufficient_data", "volatility": "insufficient_data",
            "bos": False, "choch": False, "risk": "unknown",
            "summary": "No market structure data yet",
        }
    component_values = row.get("component_values") or {}
    return {
        "symbol": symbol,
        "trend": row.get("trend"),
        "confidence": _f(row.get("confidence")),
        "trend_strength": row.get("trend_strength"),
        "volatility": row.get("volatility"),
        "bos": row.get("bos"),
        "choch": row.get("choch"),
        "risk": row.get("risk"),
        "summary": component_values.get("summary"),
    }


def update_market_structure(conn, symbols):
    """Compute + persist today's market structure for every symbol. Each
    symbol is isolated in its own try/except -- one bad symbol must never
    stop the rest, mirroring hierarchy_regime.update_hierarchy_regime's
    per-item isolation."""
    today = date_cls.today()
    for symbol in symbols:
        try:
            ctx = compute_market_structure(conn, symbol)
            store_market_structure_day(conn, today, symbol, ctx)
        except Exception as e:
            log.warning(f"Market structure update failed for {symbol}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass

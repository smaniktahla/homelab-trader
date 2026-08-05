"""
Sector-level regime: absolute trend of a sector's ETF + its relative
strength against the market benchmark (SPY by default).

Mirrors market_regime.py's split between a pure classification function
(classify_sector_regime, no I/O -- unit-testable in isolation) and an
orchestrator that loads data and calls it (compute_sector_regime). Also
supports an explicit as_of_date so historical/backtest callers get the same
no-lookahead guarantee market_regime_history.regime_for_date established.

Persisted to sector_regime_history (one row per trading_date+sector) via
store_sector_regime_day, read back via load_latest_sector_regime.
"""

from datetime import date as date_cls

from psycopg2.extras import Json, RealDictCursor

from regime_common import (
    load_daily_series, asof_index, sma, slope_positive, pct_return,
    ratio_series, score_inputs, classify_from_score,
)
from sector_mapping import get_sector_etf

CALCULATION_VERSION = 1

SMA_FAST = 50
SMA_SLOW = 200
RS_SMA = 50
MIN_ABS_TREND_BARS = SMA_FAST  # below this, absolute-trend inputs go "unknown" rather than guess


def classify_sector_regime(sector_closes, market_closes):
    """Pure function: given as-of-sliced sector ETF and market benchmark
    close series (already lookahead-safe), compute the absolute-trend and
    relative-strength subscores and combine them.

    Returns a dict: classification, total_score, absolute_trend_score,
    relative_strength_score, breadth_score (None -- no constituent data
    source yet), confidence, component_values.
    """
    abs_inputs = {}
    if len(sector_closes) >= SMA_FAST:
        price = sector_closes[-1]
        sma50 = sma(sector_closes, SMA_FAST)
        sma200 = sma(sector_closes, SMA_SLOW)
        abs_inputs["close_above_sma50"] = price > sma50 if sma50 else None
        abs_inputs["close_above_sma200"] = price > sma200 if sma200 else None
        abs_inputs["sma50_above_sma200"] = (sma50 > sma200) if (sma50 and sma200) else None
        abs_inputs["sma50_slope_positive"] = slope_positive(sector_closes, SMA_FAST)
        r20 = pct_return(sector_closes, 20)
        r60 = pct_return(sector_closes, 60)
        abs_inputs["return_20d_positive"] = (r20 > 0) if r20 is not None else None
        abs_inputs["return_60d_positive"] = (r60 > 0) if r60 is not None else None
    else:
        r20 = r60 = None
        abs_inputs = {
            "close_above_sma50": None, "close_above_sma200": None,
            "sma50_above_sma200": None, "sma50_slope_positive": None,
            "return_20d_positive": None, "return_60d_positive": None,
        }

    rel_inputs = {}
    sector_r20 = r20
    sector_r60 = r60
    market_r20 = pct_return(market_closes, 20) if market_closes else None
    market_r60 = pct_return(market_closes, 60) if market_closes else None

    rs = ratio_series(sector_closes, market_closes) if (sector_closes and market_closes) else []
    if len(rs) >= RS_SMA:
        rs_sma = sma(rs, RS_SMA)
        rel_inputs["rs_above_sma"] = (rs[-1] > rs_sma) if rs_sma else None
        rel_inputs["rs_slope_positive"] = slope_positive(rs, RS_SMA)
    else:
        rel_inputs["rs_above_sma"] = None
        rel_inputs["rs_slope_positive"] = None
    rel_inputs["return_20d_beats_market"] = (
        (sector_r20 - market_r20) > 0 if (sector_r20 is not None and market_r20 is not None) else None
    )
    rel_inputs["return_60d_beats_market"] = (
        (sector_r60 - market_r60) > 0 if (sector_r60 is not None and market_r60 is not None) else None
    )

    abs_score, abs_evidence, abs_total = score_inputs(abs_inputs)
    rel_score, rel_evidence, rel_total = score_inputs(rel_inputs)
    total_score = abs_score + rel_score
    total_evidence = abs_evidence + rel_evidence
    total_max = abs_total + rel_total

    classification = classify_from_score(total_score, total_max, total_evidence)
    confidence = round(total_evidence / total_max, 2) if total_max else 0.0

    return {
        "classification": classification,
        "total_score": total_score,
        "absolute_trend_score": abs_score,
        "relative_strength_score": rel_score,
        "breadth_score": None,
        "confidence": confidence,
        "component_values": {**abs_inputs, **rel_inputs},
    }


def compute_sector_regime(conn, sector, market_benchmark="SPY", as_of_date=None):
    """Resolve sector -> ETF, load both series, as-of slice, classify.
    Never raises: unmapped sector or insufficient data comes back as an
    explicit classification string, not a guessed neutral score."""
    etf = get_sector_etf(sector)
    if not etf:
        return {
            "sector": sector, "symbol": None, "benchmark_symbol": market_benchmark,
            "classification": "unknown", "reason": "no_etf_mapping",
            "total_score": None, "absolute_trend_score": None,
            "relative_strength_score": None, "breadth_score": None,
            "confidence": 0.0, "component_values": {},
        }

    target_date = as_of_date or date_cls.today()

    try:
        sector_dates, sector_closes_all = load_daily_series(conn, etf)
        market_dates, market_closes_all = load_daily_series(conn, market_benchmark)
    except Exception:
        sector_dates, sector_closes_all = [], []
        market_dates, market_closes_all = [], []

    s_i = asof_index(sector_dates, target_date)
    m_i = asof_index(market_dates, target_date)
    sector_closes = sector_closes_all[:s_i + 1] if s_i is not None else []
    market_closes = market_closes_all[:m_i + 1] if m_i is not None else []

    if len(sector_closes) < MIN_ABS_TREND_BARS or len(market_closes) < MIN_ABS_TREND_BARS:
        result = {
            "sector": sector, "symbol": etf, "benchmark_symbol": market_benchmark,
            "classification": "insufficient_data", "reason": "insufficient_price_history",
            "total_score": None, "absolute_trend_score": None,
            "relative_strength_score": None, "breadth_score": None,
            "confidence": 0.0, "component_values": {},
        }
        return result

    ctx = classify_sector_regime(sector_closes, market_closes)
    ctx["sector"] = sector
    ctx["symbol"] = etf
    ctx["benchmark_symbol"] = market_benchmark
    return ctx


def store_sector_regime_day(conn, trading_date, sector, ctx):
    """Upsert one day's sector regime classification."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sector_regime_history
                (trading_date, sector, benchmark_symbol, sector_symbol, classification,
                 total_score, absolute_trend_score, relative_strength_score, breadth_score,
                 confidence, component_values, calculation_version, computed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (trading_date, sector) DO UPDATE SET
                benchmark_symbol=EXCLUDED.benchmark_symbol,
                sector_symbol=EXCLUDED.sector_symbol,
                classification=EXCLUDED.classification,
                total_score=EXCLUDED.total_score,
                absolute_trend_score=EXCLUDED.absolute_trend_score,
                relative_strength_score=EXCLUDED.relative_strength_score,
                breadth_score=EXCLUDED.breadth_score,
                confidence=EXCLUDED.confidence,
                component_values=EXCLUDED.component_values,
                calculation_version=EXCLUDED.calculation_version,
                computed_at=NOW()
        """, (
            trading_date, sector, ctx.get("benchmark_symbol"), ctx.get("symbol"),
            ctx.get("classification"), ctx.get("total_score"), ctx.get("absolute_trend_score"),
            ctx.get("relative_strength_score"), ctx.get("breadth_score"), ctx.get("confidence"),
            Json(ctx.get("component_values") or {}), CALCULATION_VERSION,
        ))
    conn.commit()


def load_latest_sector_regime(conn, sector):
    """Latest persisted sector regime row, or None."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM sector_regime_history
                WHERE sector=%s ORDER BY trading_date DESC LIMIT 1
            """, (sector,))
            row = cur.fetchone()
        return dict(row) if row else None
    except Exception:
        return None

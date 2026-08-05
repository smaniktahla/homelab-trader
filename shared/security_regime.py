"""
Stock-level regime: absolute trend of the stock itself + its relative
strength against both its sector ETF and the market benchmark (SPY).

Same shape as sector_regime.py: a pure classify_security_regime (unit
testable, no I/O) plus compute_security_regime (loads data, as-of slices,
calls the pure function). Missing sector mapping degrades gracefully --
vs-sector inputs come back None/"unknown" rather than blocking the rest of
the computation (absolute trend + vs-market can still be computed).

Persisted to security_regime_history (one row per trading_date+symbol).
"""

from datetime import date as date_cls

from psycopg2.extras import Json, RealDictCursor

from regime_common import (
    load_daily_series, asof_index, sma, slope_positive, pct_return,
    ratio_series, score_inputs, classify_from_score,
)
from sector_mapping import get_sector_etf

CALCULATION_VERSION = 1

SMA_SHORT = 20
SMA_FAST = 50
SMA_SLOW = 200
RS_SMA = 50
MIN_ABS_TREND_BARS = SMA_FAST


def _relative_strength_inputs(own_closes, other_closes):
    """ratio_above_sma / ratio_slope_positive for own_closes/other_closes,
    or all-None if other_closes is empty (unmapped sector, missing data)."""
    if not other_closes:
        return {"ratio_above_sma": None, "ratio_slope_positive": None}
    rs = ratio_series(own_closes, other_closes)
    if len(rs) < RS_SMA:
        return {"ratio_above_sma": None, "ratio_slope_positive": None}
    rs_sma = sma(rs, RS_SMA)
    return {
        "ratio_above_sma": (rs[-1] > rs_sma) if rs_sma else None,
        "ratio_slope_positive": slope_positive(rs, RS_SMA),
    }


def classify_security_regime(stock_closes, sector_closes, market_closes):
    """Pure function over already as-of-sliced series. sector_closes may be
    an empty list (no sector mapping) -- vs-sector inputs then stay None."""
    if len(stock_closes) >= SMA_FAST:
        price = stock_closes[-1]
        sma20 = sma(stock_closes, SMA_SHORT)
        sma50 = sma(stock_closes, SMA_FAST)
        sma200 = sma(stock_closes, SMA_SLOW)
        abs_inputs = {
            "close_above_sma20": (price > sma20) if sma20 else None,
            "close_above_sma50": (price > sma50) if sma50 else None,
            "close_above_sma200": (price > sma200) if sma200 else None,
            "sma20_above_sma50": (sma20 > sma50) if (sma20 and sma50) else None,
            "sma50_above_sma200": (sma50 > sma200) if (sma50 and sma200) else None,
            "sma50_slope_positive": slope_positive(stock_closes, SMA_FAST),
        }
        stock_r20 = pct_return(stock_closes, 20)
        stock_r60 = pct_return(stock_closes, 60)
        abs_inputs["return_20d_positive"] = (stock_r20 > 0) if stock_r20 is not None else None
        abs_inputs["return_60d_positive"] = (stock_r60 > 0) if stock_r60 is not None else None
    else:
        stock_r20 = stock_r60 = None
        abs_inputs = {
            "close_above_sma20": None, "close_above_sma50": None, "close_above_sma200": None,
            "sma20_above_sma50": None, "sma50_above_sma200": None, "sma50_slope_positive": None,
            "return_20d_positive": None, "return_60d_positive": None,
        }

    sector_r20 = pct_return(sector_closes, 20) if sector_closes else None
    market_r20 = pct_return(market_closes, 20) if market_closes else None

    vs_sector_inputs = _relative_strength_inputs(stock_closes, sector_closes)
    vs_sector_inputs["return_beats_sector"] = (
        (stock_r20 - sector_r20) > 0 if (stock_r20 is not None and sector_r20 is not None) else None
    )

    vs_market_inputs = _relative_strength_inputs(stock_closes, market_closes)
    vs_market_inputs["return_beats_market"] = (
        (stock_r20 - market_r20) > 0 if (stock_r20 is not None and market_r20 is not None) else None
    )

    abs_score, abs_evidence, abs_total = score_inputs(abs_inputs)
    vs_sector_score, vs_sector_evidence, vs_sector_total = score_inputs(vs_sector_inputs)
    vs_market_score, vs_market_evidence, vs_market_total = score_inputs(vs_market_inputs)

    total_score = abs_score + vs_sector_score + vs_market_score
    total_evidence = abs_evidence + vs_sector_evidence + vs_market_evidence
    total_max = abs_total + vs_sector_total + vs_market_total

    classification = classify_from_score(total_score, total_max, total_evidence)
    confidence = round(total_evidence / total_max, 2) if total_max else 0.0

    if vs_sector_evidence == 0:
        vs_sector_classification = "unknown"
    elif vs_sector_score > 0:
        vs_sector_classification = "outperforming_sector"
    elif vs_sector_score < 0:
        vs_sector_classification = "underperforming_sector"
    else:
        vs_sector_classification = "in_line_with_sector"

    return {
        "classification": classification,
        "vs_sector_classification": vs_sector_classification,
        "total_score": total_score,
        "absolute_trend_score": abs_score,
        "vs_sector_score": vs_sector_score,
        "vs_market_score": vs_market_score,
        "confidence": confidence,
        "component_values": {
            **{f"abs_{k}": v for k, v in abs_inputs.items()},
            **{f"vs_sector_{k}": v for k, v in vs_sector_inputs.items()},
            **{f"vs_market_{k}": v for k, v in vs_market_inputs.items()},
        },
    }


def compute_security_regime(conn, symbol, sector=None, market_benchmark="SPY", as_of_date=None):
    """Loads the stock's own series always; sector ETF series only if
    `sector` resolves to a mapped ETF. Never raises."""
    target_date = as_of_date or date_cls.today()
    etf = get_sector_etf(sector) if sector else None

    try:
        stock_dates, stock_closes_all = load_daily_series(conn, symbol)
        market_dates, market_closes_all = load_daily_series(conn, market_benchmark)
        sector_dates, sector_closes_all = load_daily_series(conn, etf) if etf else ([], [])
    except Exception:
        stock_dates, stock_closes_all = [], []
        market_dates, market_closes_all = [], []
        sector_dates, sector_closes_all = [], []

    st_i = asof_index(stock_dates, target_date)
    mk_i = asof_index(market_dates, target_date)
    se_i = asof_index(sector_dates, target_date) if sector_dates else None

    stock_closes = stock_closes_all[:st_i + 1] if st_i is not None else []
    market_closes = market_closes_all[:mk_i + 1] if mk_i is not None else []
    sector_closes = sector_closes_all[:se_i + 1] if se_i is not None else []

    if len(stock_closes) < MIN_ABS_TREND_BARS or len(market_closes) < MIN_ABS_TREND_BARS:
        return {
            "symbol": symbol, "sector": sector, "sector_symbol": etf,
            "benchmark_symbol": market_benchmark,
            "classification": "insufficient_data", "vs_sector_classification": "unknown",
            "reason": "insufficient_price_history",
            "total_score": None, "absolute_trend_score": None,
            "vs_sector_score": None, "vs_market_score": None,
            "confidence": 0.0, "component_values": {},
        }

    ctx = classify_security_regime(stock_closes, sector_closes, market_closes)
    ctx["symbol"] = symbol
    ctx["sector"] = sector
    ctx["sector_symbol"] = etf
    ctx["benchmark_symbol"] = market_benchmark
    return ctx


def store_security_regime_day(conn, trading_date, symbol, ctx):
    """Upsert one day's stock regime classification."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO security_regime_history
                (trading_date, symbol, benchmark_symbol, sector, classification,
                 total_score, absolute_trend_score, vs_sector_score, vs_market_score,
                 confidence, component_values, calculation_version, computed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (trading_date, symbol) DO UPDATE SET
                benchmark_symbol=EXCLUDED.benchmark_symbol,
                sector=EXCLUDED.sector,
                classification=EXCLUDED.classification,
                total_score=EXCLUDED.total_score,
                absolute_trend_score=EXCLUDED.absolute_trend_score,
                vs_sector_score=EXCLUDED.vs_sector_score,
                vs_market_score=EXCLUDED.vs_market_score,
                confidence=EXCLUDED.confidence,
                component_values=EXCLUDED.component_values,
                calculation_version=EXCLUDED.calculation_version,
                computed_at=NOW()
        """, (
            trading_date, symbol, ctx.get("benchmark_symbol"), ctx.get("sector"),
            ctx.get("classification"), ctx.get("total_score"), ctx.get("absolute_trend_score"),
            ctx.get("vs_sector_score"), ctx.get("vs_market_score"), ctx.get("confidence"),
            Json(ctx.get("component_values") or {}), CALCULATION_VERSION,
        ))
    conn.commit()


def load_latest_security_regime(conn, symbol):
    """Latest persisted stock regime row, or None."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM security_regime_history
                WHERE symbol=%s ORDER BY trading_date DESC LIMIT 1
            """, (symbol,))
            row = cur.fetchone()
        return dict(row) if row else None
    except Exception:
        return None

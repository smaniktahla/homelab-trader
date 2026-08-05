"""
Orchestrates the market -> sector -> stock regime hierarchy each ingest
cycle, and assembles the per-proposal snapshot shape signals.py attaches to
trade_proposals.

update_hierarchy_regime is the single call ingest.py's run_once makes (one
new fail-open try/except block, sibling to the existing market-regime
block) -- it computes and persists sector + stock regime for whatever's
currently relevant, so signals.py never has to do its own live regime
fetch per-symbol during proposal generation (one computation per cycle,
not one per proposal).
"""

import logging
from datetime import date as date_cls

from sector_regime import compute_sector_regime, store_sector_regime_day, load_latest_sector_regime
from security_regime import compute_security_regime, store_security_regime_day, load_latest_security_regime
from regime_scoring import bucket_market_trend, bucket_sector_trend

log = logging.getLogger(__name__)


def update_hierarchy_regime(conn, symbols):
    """Compute + persist today's sector regime (for every distinct sector
    represented in `symbols`) and stock regime (for every symbol). Each
    item is isolated in its own try/except -- one bad symbol/sector must
    never stop the rest, mirroring compute_signals' per-symbol isolation."""
    from signals import load_sector_map  # local import: avoids a signals<->hierarchy_regime import cycle

    sector_map = load_sector_map(conn, symbols)
    today = date_cls.today()

    distinct_sectors = sorted(set(sector_map.values()))
    for sector in distinct_sectors:
        try:
            ctx = compute_sector_regime(conn, sector)
            store_sector_regime_day(conn, today, sector, ctx)
        except Exception as e:
            log.warning(f"Sector regime update failed for {sector}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass

    for symbol in symbols:
        try:
            ctx = compute_security_regime(conn, symbol, sector=sector_map.get(symbol))
            store_security_regime_day(conn, today, symbol, ctx)
        except Exception as e:
            log.warning(f"Security regime update failed for {symbol}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass


def _f(v):
    """NUMERIC columns come back as Decimal via psycopg2 -- Json()'s
    default encoder can't serialize those, so every score/confidence field
    that might have come from a DB row is cast to plain float (None stays
    None) before it goes into a snapshot dict destined for a JSONB column."""
    return float(v) if v is not None else None


def _vs_sector_classification_from_score(vs_sector_score):
    if vs_sector_score is None:
        return "unknown"
    if vs_sector_score > 0:
        return "outperforming_sector"
    if vs_sector_score < 0:
        return "underperforming_sector"
    return "in_line_with_sector"


def _alignment_bucket(overall, sector_classification, stock_classification):
    m = bucket_market_trend(overall) or "neutral"
    s = bucket_sector_trend(sector_classification) or "neutral"
    t = bucket_sector_trend(stock_classification) or "neutral"  # same bull/bear/neutral collapse applies
    if m == s == t == "bull":
        return "market_sector_stock_bullish"
    if m == s == t == "bear":
        return "market_sector_stock_bearish"
    return f"market_{m}_sector_{s}_stock_{t}"


def snapshot_hierarchy_for_symbol(conn, symbol, sector, market_overall):
    """Assembles the market/sector/stock regime snapshot for a proposal,
    from whatever's latest-persisted (not recomputed live) -- update_hierarchy_regime
    is expected to have already run this cycle. Missing/unmapped sector or
    missing history degrades every level to an explicit unknown/insufficient_data
    state rather than blocking snapshot assembly."""
    market_regime = {"symbol": "SPY", "classification": market_overall or "unknown"}

    sector_row = load_latest_sector_regime(conn, sector) if sector else None
    if sector_row:
        sector_regime = {
            "sector": sector_row.get("sector"),
            "symbol": sector_row.get("sector_symbol"),
            "classification": sector_row.get("classification"),
            "score": _f(sector_row.get("total_score")),
            "absolute_trend_score": _f(sector_row.get("absolute_trend_score")),
            "relative_strength_score": _f(sector_row.get("relative_strength_score")),
            "confidence": _f(sector_row.get("confidence")),
        }
    else:
        sector_regime = {
            "sector": sector, "symbol": None, "classification": "unknown" if sector else "insufficient_data",
            "score": None, "absolute_trend_score": None, "relative_strength_score": None, "confidence": 0.0,
        }

    stock_row = load_latest_security_regime(conn, symbol)
    if stock_row:
        vs_sector_classification = _vs_sector_classification_from_score(stock_row.get("vs_sector_score"))
        stock_regime = {
            "symbol": stock_row.get("symbol"),
            "classification": stock_row.get("classification"),
            "vs_sector_classification": vs_sector_classification,
            "score": _f(stock_row.get("total_score")),
            "absolute_trend_score": _f(stock_row.get("absolute_trend_score")),
            "vs_sector_score": _f(stock_row.get("vs_sector_score")),
            "vs_market_score": _f(stock_row.get("vs_market_score")),
            "confidence": _f(stock_row.get("confidence")),
        }
    else:
        vs_sector_classification = "unknown"
        stock_regime = {
            "symbol": symbol, "classification": "insufficient_data",
            "vs_sector_classification": "unknown",
            "score": None, "absolute_trend_score": None, "vs_sector_score": None,
            "vs_market_score": None, "confidence": 0.0,
        }

    hierarchy_alignment = _alignment_bucket(
        market_overall, sector_regime["classification"], stock_regime["classification"])

    return {
        "market_regime": market_regime,
        "sector_regime": sector_regime,
        "stock_regime": stock_regime,
        "hierarchy_alignment": hierarchy_alignment,
    }

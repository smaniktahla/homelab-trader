"""
Configurable proposal-score adjustment from hierarchy alignment
(market x sector x stock-vs-sector). Flat scalar keys in signal_params,
same table/pattern signals.py's DEFAULTS/load_params already uses --
round-trips through the existing GET/PATCH /api/signal-params endpoints
with zero API changes.

Conservative by design: regime_scoring_enabled defaults to 0 (off), and any
unknown/insufficient_data input yields a zero adjustment rather than
guessing a direction. This never rejects a proposal -- it only shifts the
score that's later compared against score_proposal_min, and only when
explicitly enabled.
"""

import logging

log = logging.getLogger(__name__)

REGIME_SCORING_DEFAULTS = {
    "regime_scoring_enabled": 0,
    "regime_mkt_bull_sector_bull": 15,
    "regime_mkt_bull_sector_neutral": 5,
    "regime_mkt_bull_sector_bear": -10,
    "regime_mkt_bear_sector_bull": 0,
    "regime_mkt_bear_sector_neutral": -10,
    "regime_mkt_bear_sector_bear": -20,
    "regime_stock_outperform_sector": 5,
    "regime_stock_underperform_sector": -5,
}

# market_regime.py's `overall` buckets collapse to bull/bear/neutral for this
# lookup; sector_regime.py's classification buckets collapse the same way.
_MARKET_BULL = {"bull_calm", "bull_volatile"}
_MARKET_BEAR = {"bear_calm", "bear_volatile", "bear_fear", "fear"}
_SECTOR_BULL = {"strong_bullish", "bullish"}
_SECTOR_BEAR = {"strong_bearish", "bearish"}


def load_regime_scoring_params(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM signal_params")
            rows = cur.fetchall()
        params = dict(REGIME_SCORING_DEFAULTS)
        for row in rows:
            k = row[0] if isinstance(row, (list, tuple)) else row["key"]
            if k not in REGIME_SCORING_DEFAULTS:
                continue
            v = row[1] if isinstance(row, (list, tuple)) else row["value"]
            params[k] = float(v)
        return params
    except Exception as e:
        log.warning(f"Could not load regime scoring params, using defaults: {e}")
        return dict(REGIME_SCORING_DEFAULTS)


def bucket_market_trend(overall):
    """market_regime.py's `overall` bucket -> bull/bear/None (neutral/unknown)."""
    if overall in _MARKET_BULL:
        return "bull"
    if overall in _MARKET_BEAR:
        return "bear"
    return None  # neutral/unknown -- no market x sector row applies


def bucket_sector_trend(classification):
    if classification in _SECTOR_BULL:
        return "bull"
    if classification in _SECTOR_BEAR:
        return "bear"
    if classification in ("neutral",):
        return "neutral"
    return None  # unknown/insufficient_data


def compute_regime_adjustment(market_overall, sector_classification, stock_vs_sector_classification, params):
    """Returns {market_regime_adjustment, sector_regime_adjustment,
    relative_strength_adjustment, total_regime_adjustment}. market_regime_adjustment
    stays 0 always -- the market x sector table already prices in the market
    side jointly with the sector side (matching the spec's single combined
    market_bullish_sector_bullish-style config keys); it's kept as its own
    field so callers/persistence have the documented breakdown shape even
    though this implementation folds it into sector_regime_adjustment."""
    zero = {
        "market_regime_adjustment": 0,
        "sector_regime_adjustment": 0,
        "relative_strength_adjustment": 0,
        "total_regime_adjustment": 0,
    }
    if not params.get("regime_scoring_enabled"):
        return zero

    market_bucket = bucket_market_trend(market_overall)
    sector_bucket = bucket_sector_trend(sector_classification)

    sector_adj = 0
    if market_bucket and sector_bucket:
        key = f"regime_mkt_{market_bucket}_sector_{sector_bucket}"
        if key in params:
            sector_adj = int(params[key])

    rs_adj = 0
    if stock_vs_sector_classification == "outperforming_sector":
        rs_adj = int(params["regime_stock_outperform_sector"])
    elif stock_vs_sector_classification == "underperforming_sector":
        rs_adj = int(params["regime_stock_underperform_sector"])

    total = sector_adj + rs_adj
    return {
        "market_regime_adjustment": 0,
        "sector_regime_adjustment": sector_adj,
        "relative_strength_adjustment": rs_adj,
        "total_regime_adjustment": total,
    }

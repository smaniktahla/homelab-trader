"""
Configurable proposal-score adjustment from the Market Structure Engine's
top-down trend classification (shared/market_structure.py). Same flat-
scalar-keys-in-signal_params pattern as shared/regime_scoring.py -- round-
trips through the existing GET/PATCH /api/signal-params endpoints with
zero API changes.

Conservative by design, same precedent as regime_scoring.py:
structure_scoring_enabled defaults to 0 (off), and insufficient_data
yields a zero adjustment rather than guessing a direction. Applied
uniformly regardless of buy/sell side, same simplicity as
regime_scoring.compute_regime_adjustment -- no side-specific logic here,
deferred to a real tuning pass once this has actually been enabled and
observed against live data.
"""

import logging

log = logging.getLogger(__name__)

STRUCTURE_SCORING_DEFAULTS = {
    "structure_scoring_enabled": 0,
    "structure_trend_bullish": 10,
    "structure_trend_bearish": -10,
    "structure_choch_penalty": -10,
    "structure_bos_bonus": 5,
}


def load_structure_scoring_params(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM signal_params")
            rows = cur.fetchall()
        params = dict(STRUCTURE_SCORING_DEFAULTS)
        for row in rows:
            k = row[0] if isinstance(row, (list, tuple)) else row["key"]
            if k not in STRUCTURE_SCORING_DEFAULTS:
                continue
            v = row[1] if isinstance(row, (list, tuple)) else row["value"]
            params[k] = float(v)
        return params
    except Exception as e:
        log.warning(f"Could not load structure scoring params, using defaults: {e}")
        return dict(STRUCTURE_SCORING_DEFAULTS)


def compute_structure_adjustment(market_structure_snapshot, params):
    """Returns {structure_trend_adjustment, structure_event_adjustment,
    total_structure_adjustment}. trend="mixed"/"insufficient_data" always
    yields a zero trend adjustment -- only a clear bullish/bearish top-down
    read moves the score. BOS/CHoCH are independent event adjustments (a
    symbol can be simultaneously trend-bullish and CHoCH-warned)."""
    zero = {
        "structure_trend_adjustment": 0,
        "structure_event_adjustment": 0,
        "total_structure_adjustment": 0,
    }
    if not params.get("structure_scoring_enabled"):
        return zero

    trend = market_structure_snapshot.get("trend")
    trend_adj = 0
    if trend == "bullish":
        trend_adj = int(params["structure_trend_bullish"])
    elif trend == "bearish":
        trend_adj = int(params["structure_trend_bearish"])

    event_adj = 0
    if market_structure_snapshot.get("choch"):
        event_adj += int(params["structure_choch_penalty"])
    if market_structure_snapshot.get("bos"):
        event_adj += int(params["structure_bos_bonus"])

    total = trend_adj + event_adj
    return {
        "structure_trend_adjustment": trend_adj,
        "structure_event_adjustment": event_adj,
        "total_structure_adjustment": total,
    }

"""
Generic as-of-date-safe price series helpers shared by the sector- and
security-level regime modules (shared/sector_regime.py, shared/security_regime.py).

This is the canonical copy of the load/as-of pattern market_regime_history.py
established for the market level (load_daily_series/asof_index) plus the SMA
math market_regime.py established (_sma/_classify_trend) -- market_regime.py
and market_regime_history.py are intentionally left untouched (they're
already tested and load-bearing for live gating) rather than refactored to
import from here, to avoid destabilizing working code for this PR. New
regime code (sector/stock) imports from here instead so there's exactly one
place, going forward, where this math is duplicated a third time.
"""

import bisect

import psycopg2.extensions


def load_daily_series(conn, symbol):
    """(dates, closes) ascending, python date objects. Explicit tuple cursor
    regardless of the caller connection's default -- this module is called
    from both ingest.py (tuple cursors) and api/main.py (dict cursors)."""
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute("""
            SELECT DATE(ts), close FROM price_history
            WHERE symbol=%s ORDER BY ts ASC
        """, (symbol,))
        rows = cur.fetchall()
    return [r[0] for r in rows], [float(r[1]) for r in rows]


def load_daily_ohlc(conn, symbol):
    """Full (date, open, high, low, close) bars ascending -- sibling to
    load_daily_series, for callers that need the whole bar (e.g. swing/
    structure detection in market_structure.py) rather than just closes."""
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute("""
            SELECT DATE(ts), open, high, low, close FROM price_history
            WHERE symbol=%s ORDER BY ts ASC
        """, (symbol,))
        rows = cur.fetchall()
    return [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows]


def asof_index(dates, target_date):
    """Index of the last date <= target_date, or None if no such date."""
    idx = bisect.bisect_right(dates, target_date) - 1
    return idx if idx >= 0 else None


def sma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def slope_positive(closes, period):
    """True if SMA(period) today > SMA(period) as of `period` bars ago.
    None if there isn't enough history to compare both points."""
    if len(closes) < period * 2:
        return None
    current = sma(closes, period)
    prior = sma(closes[:-period], period)
    if current is None or prior is None:
        return None
    return current > prior


def pct_return(closes, lookback):
    """% return over the trailing `lookback` bars, or None if too short."""
    if len(closes) < lookback + 1:
        return None
    start = closes[-(lookback + 1)]
    end = closes[-1]
    if not start:
        return None
    return (end - start) / start * 100.0


def score_inputs(inputs):
    """inputs: dict[name -> bool | None]. None means "couldn't be evaluated"
    (insufficient history) and is excluded from both the score and the
    evidence count, rather than counted as bearish/false -- this is what
    keeps missing data from silently becoming a neutral/negative score.
    Returns (score, evidence_count, total_count) where score is
    +1 per True, -1 per False, summed."""
    score = 0
    evidence_count = 0
    for v in inputs.values():
        if v is None:
            continue
        evidence_count += 1
        score += 1 if v else -1
    return score, evidence_count, len(inputs)


def classify_from_score(score, max_score, evidence_count):
    """Bucket a -max_score..+max_score score into the standard 5-way
    classification, or 'insufficient_data' when nothing could be evaluated.
    Thresholds are fractions of max_score so this scales with however many
    inputs a given level (sector vs stock) actually has."""
    if evidence_count == 0:
        return "insufficient_data"
    if max_score <= 0:
        return "unknown"
    frac = score / max_score
    if frac >= 0.6:
        return "strong_bullish"
    if frac >= 0.2:
        return "bullish"
    if frac > -0.2:
        return "neutral"
    if frac > -0.6:
        return "bearish"
    return "strong_bearish"


def ratio_series(closes_a, closes_b):
    """Elementwise a/b over the common trailing length of both series
    (aligned from the end -- callers are expected to have already as-of
    sliced both to the same target date, but lengths can still differ if
    one symbol has a shorter listing history). NaN/inf-safe: skips any
    pair where b is zero/falsy rather than producing inf."""
    n = min(len(closes_a), len(closes_b))
    a = closes_a[-n:]
    b = closes_b[-n:]
    return [a[i] / b[i] for i in range(n) if b[i]]

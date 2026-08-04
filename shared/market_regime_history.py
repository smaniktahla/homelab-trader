"""
Persisted per-day market regime history + regime-similarity lookup.

market_context (market_regime.py) is a single overwritten row -- today's
regime only. This module adds the durable one-row-per-trading-day
counterpart (market_regime_history table) so a "find days that looked
like today" backtest-date-finder can query history instead of
recomputing it live. The actual classification cascade
(market_regime.classify_overall) is reused here, not reimplemented, so
the live and historical paths can never drift from each other -- this
module and backtest_portfolio_montecarlo.py's historical_market_context
both funnel through it.

DB-access functions each set their own cursor_factory explicitly rather
than relying on the caller connection's default -- ingest.py's
connections are plain tuple cursors, api/main.py's are dict cursors, and
this module may be called from either context (see the PR C bug this
same lesson came from).
"""

import bisect
from datetime import date as date_cls

import psycopg2
from psycopg2.extras import RealDictCursor

from market_regime import classify_overall, _classify_trend, _classify_vix, SMA_FAST


def load_daily_series(conn, symbol):
    """(dates, closes) ascending, python date objects -- lighter than the
    OHLC loader backtest scripts use for individual stocks, since regime
    classification only needs closing price."""
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute("""
            SELECT DATE(ts), close FROM price_history
            WHERE symbol=%s ORDER BY ts ASC
        """, (symbol,))
        rows = cur.fetchall()
    return [r[0] for r in rows], [float(r[1]) for r in rows]


def asof_index(dates, target_date):
    """Index of the last date <= target_date, or None if no such date."""
    idx = bisect.bisect_right(dates, target_date) - 1
    return idx if idx >= 0 else None


def regime_for_date(spy_dates, spy_closes, qqq_dates, qqq_closes, vix_dates, vix_closes, target_date):
    """Classify the market regime as of target_date, using only price
    history up to and including that date (no lookahead). Returns the
    same field shape store_regime_day() expects -- a subset of
    compute_market_regime()'s live result dict (no SMA/rationale detail,
    market_regime_history doesn't persist those)."""
    spy_i = asof_index(spy_dates, target_date)
    qqq_i = asof_index(qqq_dates, target_date)
    vix_i = asof_index(vix_dates, target_date)

    spy_closes_upto = spy_closes[:spy_i + 1] if spy_i is not None else []
    qqq_closes_upto = qqq_closes[:qqq_i + 1] if qqq_i is not None else []
    vix_level = vix_closes[vix_i] if vix_i is not None else None

    spy_trend, _, _, _ = _classify_trend(spy_closes_upto) if len(spy_closes_upto) >= SMA_FAST else ("unknown", None, None, None)
    qqq_trend, _, _, _ = _classify_trend(qqq_closes_upto) if len(qqq_closes_upto) >= SMA_FAST else ("unknown", None, None, None)
    vix_regime, _ = _classify_vix(vix_level)

    overall, score_modifier, alloc_modifier, _rationale = classify_overall(spy_trend, qqq_trend, vix_regime)

    return {
        "spy_trend": spy_trend,
        "qqq_trend": qqq_trend,
        "vix": round(vix_level, 2) if vix_level is not None else None,
        "vix_regime": vix_regime,
        "overall": overall,
        "score_modifier": score_modifier,
        "alloc_modifier": alloc_modifier,
    }


def store_regime_day(conn, trading_date, ctx):
    """Upsert one day's regime classification. ctx must have the same keys
    regime_for_date()/compute_market_regime() produce: spy_trend, qqq_trend,
    vix, vix_regime, overall, score_modifier, alloc_modifier."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO market_regime_history
                (trading_date, spy_trend, qqq_trend, vix, vix_regime, overall,
                 score_modifier, alloc_modifier, computed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (trading_date) DO UPDATE SET
                spy_trend=EXCLUDED.spy_trend, qqq_trend=EXCLUDED.qqq_trend,
                vix=EXCLUDED.vix, vix_regime=EXCLUDED.vix_regime,
                overall=EXCLUDED.overall, score_modifier=EXCLUDED.score_modifier,
                alloc_modifier=EXCLUDED.alloc_modifier, computed_at=NOW()
        """, (
            trading_date, ctx["spy_trend"], ctx["qqq_trend"], ctx["vix"],
            ctx["vix_regime"], ctx["overall"], ctx["score_modifier"], ctx["alloc_modifier"],
        ))
    conn.commit()


def record_today(conn, ctx):
    """Upsert today's row -- called alongside save_market_context() in the
    live ingest cycle (see ingest.py). ctx is compute_market_regime()'s own
    result dict, a superset of what store_regime_day() needs."""
    store_regime_day(conn, date_cls.today(), ctx)


def find_similar_regime_days(conn, overall, start_date=None, end_date=None, limit=None):
    """Plain query against market_regime_history for days whose overall
    regime bucket matches -- no live recomputation. This is the data-layer
    half of a "find a similar market regime" backtest-date-finder; any
    API/dashboard surface is a separate, later effort."""
    query = ("SELECT trading_date, spy_trend, qqq_trend, vix, vix_regime, overall, "
             "score_modifier, alloc_modifier FROM market_regime_history WHERE overall=%s")
    params = [overall]
    if start_date is not None:
        query += " AND trading_date >= %s"
        params.append(start_date)
    if end_date is not None:
        query += " AND trading_date <= %s"
        params.append(end_date)
    query += " ORDER BY trading_date"
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, params)
        return [dict(r) for r in cur.fetchall()]

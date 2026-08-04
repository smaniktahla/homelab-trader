#!/usr/bin/env python3
"""One-off backfill: per-trading-day market regime history, walking every
SPY trading day in price_history and classifying it via
market_regime_history.regime_for_date() (the same classify_overall()
cascade compute_market_regime() uses live, fed historical price arrays
instead of a live fetch).

SPY is the walk driver (its price_history is the longest continuous daily
series available, from 2016-07-22). QQQ only has price_history from
2020-07-27 -- days before that classify qqq_trend='unknown', which
classify_overall() already handles safely (same fail-open behavior the
live path uses for any single missing input), just less differentiated.
^VIX has full daily history back to 1990 as of the 2026-08-04
backfill_vix.py fix, so it's never the binding constraint here.

Idempotent (ON CONFLICT DO UPDATE via store_regime_day) -- safe to re-run.

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 backfill_market_regime_history.py
"""

import os
import sys
import logging

import psycopg2

sys.path.insert(0, "/app")
from market_regime_history import load_daily_series, regime_for_date, store_regime_day

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_DSN = os.environ["DATABASE_URL"]


def get_db():
    return psycopg2.connect(DB_DSN)


def backfill(conn):
    spy_dates, spy_closes = load_daily_series(conn, "SPY")
    qqq_dates, qqq_closes = load_daily_series(conn, "QQQ")
    vix_dates, vix_closes = load_daily_series(conn, "^VIX")

    if not spy_dates:
        log.error("No SPY price_history -- run backfill_alpaca.py first")
        sys.exit(1)

    for trading_date in spy_dates:
        ctx = regime_for_date(spy_dates, spy_closes, qqq_dates, qqq_closes,
                               vix_dates, vix_closes, trading_date)
        store_regime_day(conn, trading_date, ctx)

    return len(spy_dates), spy_dates[0], spy_dates[-1]


def main():
    conn = get_db()
    n, first, last = backfill(conn)
    log.info(f"Done. {n} trading days classified, {first} to {last}.")
    conn.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""One-off backfill: full available daily VIX history from Yahoo Finance.
Alpaca's equities/ETF backfill (backfill_alpaca.py) has no coverage for
^VIX since it isn't a tradable us_equity asset, so the overall bull/bear+VIX
market-regime gate in market_regime.py can't be replayed historically
without this. Stored in price_history under symbol '^VIX' alongside the
equity/ETF history, same table, same shape.

Fetched in bounded (CHUNK_YEARS-wide) windows via explicit period1/period2
timestamps, not a single range=max call -- confirmed empirically (2026-08-04)
that Yahoo's chart API silently downsamples interval=1d to ~monthly
granularity when range=max is requested for a multi-decade symbol like
^VIX (36 years of history came back as ~439 rows, spaced ~30 days apart,
despite interval=1d), while a bounded range (e.g. range=2y) returns
genuine consecutive daily bars. This produced a real bug: any regime
classification keyed to a specific historical trading day's VIX level was
silently using a reading up to ~30 days stale for most of history.

Idempotent (ON CONFLICT DO NOTHING) — safe to re-run. Re-running this
fixed version over an already-backfilled (monthly-only) table is exactly
how the gap gets filled in: existing monthly rows conflict-skip on their
own date, every other trading day in between is newly inserted.

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 backfill_vix.py
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2
import requests

NY_TZ = ZoneInfo("America/New_York")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_DSN = os.environ["DATABASE_URL"]
YF_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; invest-agent/1.0)"}
SYMBOL = "^VIX"

CHUNK_YEARS = 2               # bounded window per request -- confirmed daily-granularity-safe
VIX_INCEPTION = datetime(1990, 1, 1, tzinfo=timezone.utc)   # real VIX index history start
CHUNK_REQUEST_DELAY_S = 0.3   # polite pause between chunk requests, not rate-limit-critical


def get_db():
    return psycopg2.connect(DB_DSN)


def _fetch_chunk(period1, period2):
    """One bounded-window request. Returns list of (date_str, open, high,
    low, close) tuples for whatever Yahoo actually has in this window --
    empty for windows before real ^VIX history starts."""
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{SYMBOL}"
    r = requests.get(url, params={
        "interval": "1d",
        "period1": int(period1.timestamp()),
        "period2": int(period2.timestamp()),
    }, headers=YF_HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    result = data["chart"]["result"][0]
    timestamps = result.get("timestamp") or []
    ohlcv = result["indicators"]["quote"][0]

    rows = []
    for i, ts in enumerate(timestamps):
        o, h, l, c = ohlcv["open"][i], ohlcv["high"][i], ohlcv["low"][i], ohlcv["close"][i]
        if o is None or c is None:
            continue
        date_str = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(NY_TZ).strftime("%Y-%m-%d")
        rows.append((date_str, o, h, l, c))
    return rows


def fetch_vix_full_history(now=None):
    """Full available ^VIX daily history from Yahoo, stitched from bounded
    chunks (see module docstring for why range=max can't be used directly).
    Returns a list of (date_str, open, high, low, close) tuples, oldest
    first, deduplicated by date across chunk boundaries.

    now: injectable for deterministic testing (how many chunks get fetched
    depends on wall-clock time otherwise); defaults to the real current
    time for actual runs."""
    now = now or datetime.now(timezone.utc)
    by_date = {}
    chunk_start = VIX_INCEPTION
    while chunk_start < now:
        chunk_end = min(chunk_start + timedelta(days=365 * CHUNK_YEARS), now)
        for date_str, o, h, l, c in _fetch_chunk(chunk_start, chunk_end):
            by_date[date_str] = (date_str, o, h, l, c)
        chunk_start = chunk_end
        if chunk_start < now:
            time.sleep(CHUNK_REQUEST_DELAY_S)
    return [by_date[d] for d in sorted(by_date)]


def _market_open_utc(date_str):
    """Same DST-aware market-open convention used elsewhere in price_history
    (see backfill_alpaca.py's _normalize_ts) so ^VIX rows line up on the
    same daily timestamp as SPY/QQQ/equity rows for a given trading day."""
    y, m, d = (int(p) for p in date_str.split("-"))
    market_open_ny = datetime(y, m, d, 9, 30, tzinfo=NY_TZ)
    return market_open_ny.astimezone(timezone.utc)


def store_rows(conn, rows):
    inserted = 0
    with conn.cursor() as cur:
        for date_str, o, h, l, c in rows:
            ts = _market_open_utc(date_str)
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, NULL)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, (SYMBOL, ts, o, h, l, c))
            inserted += cur.rowcount
    conn.commit()
    return inserted


def main():
    conn = get_db()
    log.info(f"Fetching full {SYMBOL} history from Yahoo Finance in {CHUNK_YEARS}-year chunks...")
    try:
        rows = fetch_vix_full_history()
    except Exception as e:
        log.error(f"{SYMBOL} fetch failed: {e}")
        sys.exit(1)

    if not rows:
        log.error(f"{SYMBOL}: no data returned")
        sys.exit(1)

    n = store_rows(conn, rows)
    log.info(f"Done. {SYMBOL}: {len(rows)} bars fetched ({rows[0][0]} to {rows[-1][0]}), {n} new rows inserted.")
    conn.close()


if __name__ == "__main__":
    main()

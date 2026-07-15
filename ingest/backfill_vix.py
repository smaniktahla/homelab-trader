#!/usr/bin/env python3
"""One-off backfill: full available daily VIX history from Yahoo Finance
(range=max returns Yahoo's entire archive in one call, not just a live
window). Alpaca's equities/ETF backfill (backfill_alpaca.py) has no
coverage for ^VIX since it isn't a tradable us_equity asset, so the
overall bull/bear+VIX market-regime gate in market_regime.py can't be
replayed historically without this. Stored in price_history under symbol
'^VIX' alongside the equity/ETF history, same table, same shape.

Idempotent (ON CONFLICT DO NOTHING) — safe to re-run.

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 backfill_vix.py
"""

import os
import sys
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg2
import requests

NY_TZ = ZoneInfo("America/New_York")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_DSN = os.environ["DATABASE_URL"]
YF_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; invest-agent/1.0)"}
SYMBOL = "^VIX"


def get_db():
    return psycopg2.connect(DB_DSN)


def fetch_vix_full_history():
    """Full available ^VIX daily history from Yahoo. Returns list of
    (date_str, open, high, low, close) tuples, oldest first."""
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{SYMBOL}"
    r = requests.get(url, params={"interval": "1d", "range": "max"},
                      headers=YF_HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    result = data["chart"]["result"][0]
    timestamps = result["timestamp"]
    ohlcv = result["indicators"]["quote"][0]

    rows = []
    for i, ts in enumerate(timestamps):
        o, h, l, c = ohlcv["open"][i], ohlcv["high"][i], ohlcv["low"][i], ohlcv["close"][i]
        if o is None or c is None:
            continue
        date_str = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(NY_TZ).strftime("%Y-%m-%d")
        rows.append((date_str, o, h, l, c))
    return rows


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
    log.info(f"Fetching full {SYMBOL} history from Yahoo Finance...")
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

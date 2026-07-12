#!/usr/bin/env python3
"""One-off backfill: pull full available daily-bar history (Alpaca's free
IEX-tier data goes back to ~Aug 2020 in practice, though requesting further
back than that just returns whatever exists) for every scannable universe
symbol, supplementing the 1y-max Yahoo Finance window ingest_prices() uses
day to day. Idempotent (ON CONFLICT DO NOTHING) — safe to re-run.

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 backfill_alpaca.py
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg2
import requests

NY_TZ = ZoneInfo("America/New_York")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_DSN = os.environ["DATABASE_URL"]
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET", "")
ALPACA_DATA_BASE = "https://data.alpaca.markets"
ALPACA_HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

START = "2016-01-01"   # aggressive floor; Alpaca just returns whatever it actually has
BATCH_SIZE = 8          # symbols per request — keeps typical (~1500-bar) responses
                         # under the 10000-row cap without relying on pagination,
                         # though pagination below handles it anyway if exceeded
SLEEP_BETWEEN_BATCHES = 0.4  # stay well under the free-tier rate limit


def get_db():
    return psycopg2.connect(DB_DSN)


def get_universe_symbols(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM universe WHERE scannable=TRUE ORDER BY symbol")
        return [r[0] for r in cur.fetchall()]


def fetch_bars_batch(symbols):
    """Daily bars for a batch of symbols, following pagination if the batch
    exceeds the row cap. split-adjusted (not dividend-adjusted) so historical
    prices don't show an artificial cliff at stock splits when charted
    alongside the Yahoo-sourced recent window, which is split-adjusted too."""
    params = {
        "symbols": ",".join(symbols),
        "timeframe": "1Day",
        "start": START,
        "limit": 10000,
        "feed": "iex",
        "adjustment": "split",
    }
    all_bars = {}
    page_token = None
    while True:
        if page_token:
            params["page_token"] = page_token
        r = requests.get(f"{ALPACA_DATA_BASE}/v2/stocks/bars",
                          headers=ALPACA_HEADERS, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for sym, bars in (data.get("bars") or {}).items():
            all_bars.setdefault(sym, []).extend(bars)
        page_token = data.get("next_page_token")
        if not page_token:
            break
    return all_bars


def _normalize_ts(alpaca_ts):
    """Alpaca stamps daily bars at session-start in UTC (which itself shifts
    with DST — 04:00:00Z in EDT, 05:00:00Z in EST). The Yahoo-sourced rows
    already in price_history are stamped at actual market-open (9:30am
    America/New_York, converted to UTC), which is 13:30:00Z in EDT months
    and 14:30:00Z in EST months — NOT a fixed time year-round. A naive fixed
    stamp collides with Yahoo's rows for roughly 8 months a year and silently
    mismatches for the other 4 (EST) months, producing duplicate same-day
    rows instead of merging. Compute the same DST-aware market-open time
    Yahoo's epoch timestamps naturally represent, so the two sources always
    agree on a given trading day's timestamp."""
    date_str = alpaca_ts[:10]
    y, m, d = (int(p) for p in date_str.split("-"))
    market_open_ny = datetime(y, m, d, 9, 30, tzinfo=NY_TZ)
    return market_open_ny.astimezone(timezone.utc).isoformat()

def store_bars(conn, symbol, bars):
    inserted = 0
    with conn.cursor() as cur:
        for b in bars:
            if b.get("o") is None or b.get("c") is None:
                continue
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, (symbol, _normalize_ts(b["t"]), b["o"], b["h"], b["l"], b["c"], b.get("v")))
            inserted += cur.rowcount
    conn.commit()
    return inserted


def main():
    if not ALPACA_KEY or not ALPACA_SECRET:
        log.error("ALPACA_API_KEY / ALPACA_API_SECRET not set")
        sys.exit(1)

    conn = get_db()
    symbols = get_universe_symbols(conn)
    log.info(f"Backfilling {len(symbols)} universe symbols from Alpaca (start={START})")

    total_inserted = 0
    total_symbols_with_data = 0
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        try:
            bars_by_symbol = fetch_bars_batch(batch)
        except Exception as e:
            log.warning(f"Batch {batch} failed: {e}")
            time.sleep(SLEEP_BETWEEN_BATCHES)
            continue
        for sym in batch:
            bars = bars_by_symbol.get(sym, [])
            if not bars:
                log.info(f"{sym}: no data returned")
                continue
            n = store_bars(conn, sym, bars)
            total_inserted += n
            total_symbols_with_data += 1
            log.info(f"{sym}: {len(bars)} bars fetched, {n} new rows")
        time.sleep(SLEEP_BETWEEN_BATCHES)

    log.info(
        f"Done. {total_inserted} new price_history rows inserted across "
        f"{total_symbols_with_data}/{len(symbols)} symbols with data."
    )
    conn.close()


if __name__ == "__main__":
    main()

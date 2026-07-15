#!/usr/bin/env python3
"""One-off backfill: hourly bar history (Alpaca's free IEX-tier data goes
back to ~Aug 2020 in practice, same underlying feed as the daily backfill
just aggregated at a different resolution) for every scannable universe
symbol, into price_history_hourly.

Mirrors backfill_alpaca.py's batching pattern (same rate-limit concern,
same fix): BATCH_SIZE symbols per request, paginated, with a sleep between
batches to stay under the free-tier rate limit. Simpler than the daily
script in one respect — the DST-aware timestamp normalization there exists
only to align two *different* daily sources (Yahoo vs. Alpaca) onto the
same convention; hourly has a single source (Alpaca), so bars are stored
at their raw returned UTC timestamp, no normalization needed.

See docs/thesis-horizons-and-intraday-data.md for why this is a separate
table from price_history rather than a rename or a rollup.

Idempotent (ON CONFLICT DO NOTHING) — safe to re-run.

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 backfill_intraday_alpaca.py
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone

import psycopg2
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_DSN = os.environ["DATABASE_URL"]
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET", "")
ALPACA_DATA_BASE = "https://data.alpaca.markets"
ALPACA_HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

START = "2020-08-01"    # aggressive floor; Alpaca just returns whatever it actually has
TIMEFRAME = "1Hour"
BATCH_SIZE = 8           # symbols per request — same as backfill_alpaca.py, keeps
                         # responses well under the 10000-row page cap
SLEEP_BETWEEN_BATCHES = 0.4  # stay well under the free-tier rate limit


def get_db():
    return psycopg2.connect(DB_DSN)


def get_universe_symbols(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM universe WHERE scannable=TRUE ORDER BY symbol")
        return [r[0] for r in cur.fetchall()]


def fetch_bars_batch(symbols):
    """Hourly bars for a batch of symbols, following pagination if the batch
    exceeds the row cap. split-adjusted (not dividend-adjusted), same
    convention as backfill_alpaca.py's daily bars."""
    params = {
        "symbols": ",".join(symbols),
        "timeframe": TIMEFRAME,
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


def store_bars(conn, symbol, bars):
    inserted = 0
    with conn.cursor() as cur:
        for b in bars:
            if b.get("o") is None or b.get("c") is None:
                continue
            ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(timezone.utc)
            cur.execute("""
                INSERT INTO price_history_hourly (symbol, ts, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, (symbol, ts, b["o"], b["h"], b["l"], b["c"], b.get("v")))
            inserted += cur.rowcount
    conn.commit()
    return inserted


def main():
    if not ALPACA_KEY or not ALPACA_SECRET:
        log.error("ALPACA_API_KEY / ALPACA_API_SECRET not set")
        sys.exit(1)

    conn = get_db()
    symbols = get_universe_symbols(conn)
    log.info(f"Backfilling {len(symbols)} universe symbols' hourly bars from Alpaca (start={START})")

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
            log.info(f"{sym}: {len(bars)} hourly bars fetched, {n} new rows")
        time.sleep(SLEEP_BETWEEN_BATCHES)

    log.info(
        f"Done. {total_inserted} new price_history_hourly rows inserted across "
        f"{total_symbols_with_data}/{len(symbols)} symbols with data."
    )
    conn.close()


if __name__ == "__main__":
    main()

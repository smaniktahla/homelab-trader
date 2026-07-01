#!/usr/bin/env python3
"""Scheduled ingest: price history, news, order reconciliation, and universe scan."""

import os, time, logging
import psycopg2
import requests
from datetime import datetime, timezone

from signals import compute_signals
from scanner import seed_universe, scan_universe, promote_demote

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_DSN = os.environ["DATABASE_URL"]
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET", "")
ALPACA_BASE = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
INTERVAL_SECONDS = int(os.environ.get("INGEST_INTERVAL", "3600"))
UNIVERSE_SCAN_INTERVAL = int(os.environ.get("UNIVERSE_SCAN_INTERVAL", "14400"))  # 4 hours
YF_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; invest-agent/1.0)"}

TERMINAL_STATUSES = {"filled", "canceled", "expired", "replaced"}


def get_db():
    return psycopg2.connect(DB_DSN)


def get_watchlist(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM watchlist")
        return [r[0] for r in cur.fetchall()]


def fetch_prices_yf(symbol, yf_range="5d"):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = requests.get(url, params={"interval": "1d", "range": yf_range},
                     headers=YF_HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    result = data["chart"]["result"][0]
    timestamps = result["timestamp"]
    ohlcv = result["indicators"]["quote"][0]
    rows = []
    for i, ts in enumerate(timestamps):
        rows.append({
            "ts": datetime.fromtimestamp(ts, tz=timezone.utc),
            "open": ohlcv["open"][i],
            "high": ohlcv["high"][i],
            "low": ohlcv["low"][i],
            "close": ohlcv["close"][i],
            "volume": ohlcv["volume"][i],
        })
    return rows


def ingest_prices(conn, symbols):
    with conn.cursor() as cur:
        for sym in symbols:
            try:
                # Fetch 3 months on first ingest for a symbol, 5 days for updates
                cur.execute("SELECT COUNT(*) FROM price_history WHERE symbol=%s", (sym,))
                count = cur.fetchone()[0]
                yf_range = "3mo" if count == 0 else "5d"
                rows = fetch_prices_yf(sym, yf_range)
                for row in rows:
                    if None in (row["open"], row["close"]):
                        continue
                    cur.execute("""
                        INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (symbol, ts) DO NOTHING
                    """, (sym, row["ts"], row["open"], row["high"],
                          row["low"], row["close"], row["volume"]))
                log.info(f"Prices for {sym}: {len(rows)} rows ({yf_range})")
            except Exception as e:
                log.warning(f"Price ingest failed for {sym}: {e}")
    conn.commit()


def ingest_news(conn, symbols):
    if not FINNHUB_KEY:
        return
    now = int(time.time())
    day_ago = now - 86400
    with conn.cursor() as cur:
        for sym in symbols:
            try:
                r = requests.get(
                    "https://finnhub.io/api/v1/company-news",
                    params={"symbol": sym,
                            "from": datetime.fromtimestamp(day_ago, tz=timezone.utc).strftime("%Y-%m-%d"),
                            "to": datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d"),
                            "token": FINNHUB_KEY},
                    timeout=10)
                r.raise_for_status()
                items = r.json()
                time.sleep(1.1)  # Finnhub free tier: 60 req/min
                for item in items:
                    cur.execute("""
                        INSERT INTO news (symbol, headline, source, url, published_at, summary)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (url) DO NOTHING
                    """, (sym, item.get("headline"), item.get("source"),
                          item.get("url"),
                          datetime.fromtimestamp(item.get("datetime", now), tz=timezone.utc),
                          item.get("summary")))
                log.info(f"News for {sym}: {len(items)} items")
            except Exception as e:
                log.warning(f"News ingest failed for {sym}: {e}")
    conn.commit()


def reconcile_orders(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, order_id FROM trades
            WHERE status NOT IN %s AND order_id IS NOT NULL
        """, (tuple(TERMINAL_STATUSES),))
        pending = cur.fetchall()

    if not pending:
        return

    log.info(f"Reconciling {len(pending)} pending order(s)")
    with conn.cursor() as cur:
        for trade_id, order_id in pending:
            try:
                r = requests.get(f"{ALPACA_BASE}/v2/orders/{order_id}",
                                  headers=ALPACA_HEADERS, timeout=10)
                r.raise_for_status()
                order = r.json()
                status = order["status"]
                filled_qty = float(order.get("filled_qty") or 0)
                filled_price = float(order.get("filled_avg_price") or 0)
                notional = filled_qty * filled_price if filled_qty and filled_price else None

                cur.execute("""
                    UPDATE trades
                    SET status=%s, qty=COALESCE(NULLIF(%s,0), qty), price=COALESCE(NULLIF(%s,0), price), notional=COALESCE(%s, notional)
                    WHERE id=%s
                """, (status, filled_qty or None, filled_price or None, notional, trade_id))
                log.info(f"Order {order_id}: {status}, qty={filled_qty}, price={filled_price}")
            except Exception as e:
                log.warning(f"Reconcile failed for order {order_id}: {e}")
    conn.commit()


def get_positions():
    try:
        r = requests.get(f"{ALPACA_BASE}/v2/positions", headers=ALPACA_HEADERS, timeout=10)
        r.raise_for_status()
        return {p["symbol"]: p for p in r.json()}
    except Exception as e:
        log.warning(f"Could not fetch positions: {e}")
        return {}


def run_once(conn, last_universe_scan):
    symbols = get_watchlist(conn)
    log.info(f"Watchlist ({len(symbols)} symbols): {symbols}")
    ingest_prices(conn, symbols)
    ingest_news(conn, symbols)
    compute_signals(conn, symbols)
    reconcile_orders(conn)

    # Universe scan runs on its own slower interval
    now = time.time()
    if now - last_universe_scan >= UNIVERSE_SCAN_INTERVAL:
        log.info("Starting universe scan...")
        positions = get_positions()
        watchlist_before = set(get_watchlist(conn))
        candidates = scan_universe(conn)
        promote_demote(conn, positions, candidates)
        newly_promoted = set(get_watchlist(conn)) - watchlist_before
        if newly_promoted:
            log.info(f"Running signals on {len(newly_promoted)} newly promoted: {sorted(newly_promoted)}")
            compute_signals(conn, list(newly_promoted))
        return now
    return last_universe_scan


if __name__ == "__main__":
    log.info("Ingest service starting")
    conn = get_db()
    with open("/app/schema.sql") as f:
        with conn.cursor() as cur:
            cur.execute(f.read())
    conn.commit()
    conn.close()
    log.info("Schema ready")

    # Startup tasks
    conn = get_db()
    try:
        seed_universe(conn)
        reconcile_orders(conn)
    except Exception as e:
        log.warning(f"Startup tasks failed: {e}")
    finally:
        conn.close()

    # Trigger universe scan immediately on first run
    last_universe_scan = 0

    while True:
        conn = get_db()
        try:
            last_universe_scan = run_once(conn, last_universe_scan)
        except Exception as e:
            log.error(f"Ingest cycle failed: {e}")
        finally:
            conn.close()
        log.info(f"Sleeping {INTERVAL_SECONDS}s")
        time.sleep(INTERVAL_SECONDS)

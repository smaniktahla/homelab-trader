#!/usr/bin/env python3
"""Scheduled ingest: price history, news, and order reconciliation."""

import os, time, logging
import psycopg2
import requests
from datetime import datetime, timezone

from signals import compute_signals

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_DSN = os.environ["DATABASE_URL"]
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET", "")
ALPACA_BASE = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
INTERVAL_SECONDS = int(os.environ.get("INGEST_INTERVAL", "3600"))
YF_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; invest-agent/1.0)"}

TERMINAL_STATUSES = {"filled", "canceled", "expired", "replaced"}


def get_db():
    return psycopg2.connect(DB_DSN)


def get_watchlist(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM watchlist")
        return [r[0] for r in cur.fetchall()]


def fetch_prices_yf(symbol):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = requests.get(url, params={"interval": "1d", "range": "5d"},
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
                rows = fetch_prices_yf(sym)
                for row in rows:
                    if None in (row["open"], row["close"]):
                        continue
                    cur.execute("""
                        INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (symbol, ts) DO NOTHING
                    """, (sym, row["ts"], row["open"], row["high"],
                          row["low"], row["close"], row["volume"]))
                log.info(f"Prices for {sym}: {len(rows)} rows")
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
    """Check pending/accepted trades against Alpaca and update fill data."""
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


def run_once():
    conn = get_db()
    try:
        symbols = get_watchlist(conn)
        log.info(f"Watchlist: {symbols}")
        ingest_prices(conn, symbols)
        ingest_news(conn, symbols)
        compute_signals(conn, symbols)
        reconcile_orders(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    log.info("Ingest service starting")
    conn = get_db()
    with open("/app/schema.sql") as f:
        with conn.cursor() as cur:
            cur.execute(f.read())
    conn.commit()
    conn.close()
    log.info("Schema ready")

    # Reconcile immediately on startup to catch anything that filled while we were down
    try:
        conn = get_db()
        reconcile_orders(conn)
        conn.close()
    except Exception as e:
        log.warning(f"Startup reconcile failed: {e}")

    while True:
        try:
            run_once()
        except Exception as e:
            log.error(f"Ingest cycle failed: {e}")
        log.info(f"Sleeping {INTERVAL_SECONDS}s")
        time.sleep(INTERVAL_SECONDS)

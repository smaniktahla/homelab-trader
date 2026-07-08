"""
Earnings Blackout: PRD v1.1 #2. Fetch known earnings dates from Finnhub's
free calendar endpoint and skip new BUY proposals within a configurable
window of a symbol's earnings date — pre-earnings uncertainty and
post-earnings gap risk are both outside what RSI/Bollinger mean reversion
is meant to trade.
"""

import logging
import os
from datetime import date, timedelta

import requests

log = logging.getLogger(__name__)

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")

LOOKBACK_DAYS = 10   # keep recently-reported symbols in the table briefly after
LOOKAHEAD_DAYS = 45  # how far out to fetch upcoming earnings


def sync_earnings_calendar(conn):
    """Fetch upcoming (and recently past) earnings dates from Finnhub and
    upsert into earnings_events. No-op if FINNHUB_API_KEY isn't set — the
    blackout check simply finds nothing and never blocks in that case."""
    if not FINNHUB_KEY:
        return
    today = date.today()
    frm = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    to = (today + timedelta(days=LOOKAHEAD_DAYS)).isoformat()
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={"from": frm, "to": to, "token": FINNHUB_KEY},
            timeout=15,
        )
        r.raise_for_status()
        events = r.json().get("earningsCalendar", [])
    except Exception as e:
        log.warning(f"Earnings calendar sync failed: {e}")
        return

    if not events:
        return

    with conn.cursor() as cur:
        for e in events:
            sym = e.get("symbol")
            edate = e.get("date")
            if not sym or not edate:
                continue
            cur.execute("""
                INSERT INTO earnings_events (symbol, earnings_date)
                VALUES (%s, %s)
                ON CONFLICT (symbol, earnings_date) DO NOTHING
            """, (sym, edate))
    conn.commit()
    log.info(f"Earnings calendar: synced {len(events)} event(s) for {frm}..{to}")


def earnings_blackout_reason(conn, symbol, blackout_days):
    """Return a block reason string if `symbol` has a known earnings date
    within blackout_days (either side) of today, else None."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT earnings_date FROM earnings_events
            WHERE symbol=%s AND earnings_date BETWEEN %s AND %s
            ORDER BY earnings_date ASC LIMIT 1
        """, (symbol,
              date.today() - timedelta(days=int(blackout_days)),
              date.today() + timedelta(days=int(blackout_days))))
        row = cur.fetchone()
    if row:
        return f"earnings_blackout:{row[0]}"
    return None

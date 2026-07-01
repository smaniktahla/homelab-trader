"""
Universe scanner: fast RSI scan across all tradeable US equities using Alpaca
batch bars. Runs every 4 hours. Promotes top candidates to watchlist.
"""

import logging
import os
import time
from datetime import datetime, timezone, timedelta

import requests

log = logging.getLogger(__name__)

ALPACA_BASE = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_DATA = "https://data.alpaca.markets"
ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET", ""),
}

BATCH_SIZE = 100        # symbols per Alpaca bars request
PROMOTE_THRESHOLD = 45  # RSI score (buy or sell) to auto-promote to watchlist
DEMOTE_WEAK_HOURS = 12  # hours a watchlist entry can stay weak before demotion


def _rsi(closes, period=14):
    if len(closes) < period + 2:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + ag / al))


def _buy_score(rsi, price, closes):
    """Quick buy score from RSI only (no BB — speed over precision for universe scan)."""
    if rsi is None:
        return 0
    if rsi < 20:
        return 80
    if rsi < 25:
        return 65
    if rsi < 30:
        return 50
    if rsi < 35:
        return 35
    return 0


def _sell_score(rsi, price, closes):
    if rsi is None:
        return 0
    if rsi > 80:
        return 80
    if rsi > 75:
        return 65
    if rsi > 70:
        return 50
    if rsi > 65:
        return 35
    return 0


def seed_universe(conn):
    """Populate universe table from Alpaca assets if nearly empty."""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM universe")
        row = cur.fetchone()
        count = row[0] if isinstance(row, (list, tuple)) else row["count"]
    if count > 100:
        log.info(f"Universe already seeded ({count} symbols)")
        return

    log.info("Seeding universe from Alpaca assets API...")
    r = requests.get(
        f"{ALPACA_BASE}/v2/assets",
        params={"status": "active", "asset_class": "us_equity"},
        headers=ALPACA_HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    assets = r.json()

    valid_exchanges = {"NYSE", "NASDAQ", "ARCA", "BATS", "NYSE ARCA", "NYSE MKT"}
    filtered = [
        a for a in assets
        if a.get("tradable")
        and a.get("fractionable")
        and a.get("easy_to_borrow")
        and a.get("exchange") in valid_exchanges
        and "." not in a["symbol"]
        and "/" not in a["symbol"]
        and len(a["symbol"]) <= 5
    ]

    log.info(f"Alpaca assets: {len(assets)} total → {len(filtered)} universe symbols after filtering")

    with conn.cursor() as cur:
        for a in filtered:
            cur.execute("""
                INSERT INTO universe (symbol, name, exchange)
                VALUES (%s, %s, %s)
                ON CONFLICT (symbol) DO NOTHING
            """, (a["symbol"], a.get("name", ""), a.get("exchange", "")))
    conn.commit()
    log.info(f"Universe seeded: {len(filtered)} symbols")


def _fetch_bars_batch(symbols):
    """
    Fetch last 45 days of daily closes for a batch of symbols via Alpaca.
    Returns dict: symbol -> list of closes (oldest first).
    """
    start = (datetime.now(timezone.utc) - timedelta(days=65)).strftime("%Y-%m-%d")
    try:
        r = requests.get(
            f"{ALPACA_DATA}/v2/stocks/bars",
            params={
                "symbols": ",".join(symbols),
                "timeframe": "1Day",
                "start": start,
                "limit": 50,
                "feed": "iex",
                "adjustment": "raw",
            },
            headers=ALPACA_HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json().get("bars", {})
        result = {}
        for sym, bars in data.items():
            if bars:
                result[sym] = [float(b["c"]) for b in bars]
        return result
    except Exception as e:
        log.warning(f"Bars batch failed for {symbols[:3]}...: {e}")
        return {}


def scan_universe(conn):
    """
    Fast RSI scan across entire universe using Alpaca batch bars.
    Upserts results into universe_scan. Returns list of top candidates.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM universe ORDER BY symbol")
        rows = cur.fetchall()
    symbols = [r[0] if isinstance(r, (list, tuple)) else r["symbol"] for r in rows]

    if not symbols:
        log.warning("Universe is empty — run seed_universe first")
        return []

    log.info(f"Scanning universe: {len(symbols)} symbols in batches of {BATCH_SIZE}")
    total_scanned = 0
    candidates = []

    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        bars = _fetch_bars_batch(batch)

        with conn.cursor() as cur:
            for sym in batch:
                closes = bars.get(sym, [])
                if len(closes) < 16:
                    continue
                price = closes[-1]
                rsi = _rsi(closes)
                buy_sc = _buy_score(rsi, price, closes)
                sell_sc = _sell_score(rsi, price, closes)
                regime = "unknown"  # regime needs 200 days; skip for speed

                cur.execute("""
                    INSERT INTO universe_scan (symbol, price, rsi, buy_score, sell_score, regime, scanned_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (symbol) DO UPDATE SET
                        price=EXCLUDED.price, rsi=EXCLUDED.rsi,
                        buy_score=EXCLUDED.buy_score, sell_score=EXCLUDED.sell_score,
                        regime=EXCLUDED.regime, scanned_at=EXCLUDED.scanned_at
                """, (sym, price, rsi, buy_sc, sell_sc, regime))

                if buy_sc >= PROMOTE_THRESHOLD or sell_sc >= PROMOTE_THRESHOLD:
                    candidates.append({
                        "symbol": sym,
                        "price": price,
                        "rsi": rsi,
                        "buy_score": buy_sc,
                        "sell_score": sell_sc,
                    })
                total_scanned += 1
        conn.commit()

        # Small pause between batches to be polite to Alpaca
        if i + BATCH_SIZE < len(symbols):
            time.sleep(0.3)

    candidates.sort(key=lambda x: max(x["buy_score"], x["sell_score"]), reverse=True)
    log.info(f"Universe scan complete: {total_scanned} symbols scanned, {len(candidates)} candidates")
    return candidates


def promote_demote(conn, positions, candidates):
    """
    Promote strong candidates to watchlist (pinned=False).
    Demote non-pinned entries that have been weak for too long.
    Always keep symbols with open positions on the watchlist.
    """
    now = datetime.now(timezone.utc)
    position_symbols = set(positions.keys()) if positions else set()

    # Promote candidates
    for c in candidates:
        sym = c["symbol"]
        score = max(c["buy_score"], c["sell_score"])
        with conn.cursor() as cur:
            cur.execute("SELECT symbol, pinned FROM watchlist WHERE symbol=%s", (sym,))
            existing = cur.fetchone()
            if not existing:
                cur.execute("""
                    INSERT INTO watchlist (symbol, name, pinned, promoted_at, signal_score)
                    VALUES (%s, %s, FALSE, NOW(), %s)
                    ON CONFLICT (symbol) DO UPDATE SET
                        signal_score=EXCLUDED.signal_score,
                        weak_since=NULL
                """, (sym, sym, score))
                log.info(f"Promoted to watchlist: {sym} (score={score})")
            else:
                cur.execute("""
                    UPDATE watchlist SET signal_score=%s, weak_since=NULL WHERE symbol=%s
                """, (score, sym))
        conn.commit()

    # Mark non-candidates as weak
    candidate_symbols = {c["symbol"] for c in candidates}
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE watchlist
            SET weak_since = COALESCE(weak_since, NOW())
            WHERE pinned = FALSE
            AND symbol NOT IN %s
            AND symbol NOT IN %s
        """, (
            tuple(candidate_symbols) if candidate_symbols else ("__none__",),
            tuple(position_symbols) if position_symbols else ("__none__",),
        ))
    conn.commit()

    # Demote entries that have been weak long enough
    cutoff = now - timedelta(hours=DEMOTE_WEAK_HOURS)
    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM watchlist
            WHERE pinned = FALSE
            AND weak_since < %s
            AND symbol NOT IN %s
            RETURNING symbol
        """, (
            cutoff,
            tuple(position_symbols) if position_symbols else ("__none__",),
        ))
        demoted = [r[0] if isinstance(r, (list, tuple)) else r["symbol"] for r in cur.fetchall()]
    conn.commit()
    if demoted:
        log.info(f"Demoted from watchlist: {demoted}")

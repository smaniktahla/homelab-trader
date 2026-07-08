"""
Universe scanner: RSI scan across S&P 500 + major ETFs via Yahoo Finance.
Runs every 4 hours. Promotes top candidates to watchlist.
S&P 500 constituent list fetched from Wikipedia on startup; updated when stale.
"""

import logging
import os
import time
from datetime import datetime, timezone, timedelta

import requests
from html.parser import HTMLParser

log = logging.getLogger(__name__)

ALPACA_BASE = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET", ""),
}
YF_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; invest-scanner/1.0)"}

PROMOTE_THRESHOLD = 45  # score to auto-promote to watchlist
DEMOTE_WEAK_HOURS = 12  # hours a watchlist entry can stay weak before demotion
SCAN_DELAY = 0.25       # seconds between Yahoo Finance requests

# Major ETFs always included in the scan universe
CORE_ETFS = [
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY",
    "XLP", "XLU", "XLB", "XLRE", "GLD", "SLV", "TLT", "HYG", "EEM", "VIX",
]


class _SP500Parser(HTMLParser):
    """Parse S&P 500 table (Symbol + GICS Sector) from Wikipedia."""
    def __init__(self):
        super().__init__()
        self._in_table = False
        self._in_cell = False
        self._col = 0
        self._cur_symbol = None
        self.rows = []  # [(symbol, sector), ...]

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table" and "constituents" in attrs.get("id", ""):
            self._in_table = True
        if self._in_table and tag == "tr":
            self._col = 0
            self._cur_symbol = None
        if self._in_table and tag == "td":
            self._in_cell = True

    def handle_endtag(self, tag):
        if tag == "table":
            self._in_table = False
        if tag == "td":
            self._in_cell = False
            self._col += 1

    def handle_data(self, data):
        if not self._in_cell:
            return
        text = data.strip()
        if not text:
            return
        if self._col == 0:
            sym = text.replace(".", "-")  # BRK.B → BRK-B for Yahoo
            if 1 <= len(sym) <= 5:
                self._cur_symbol = sym
        elif self._col == 2 and self._cur_symbol:
            self.rows.append((self._cur_symbol, text))


def _fetch_sp500_data():
    """Fetch current S&P 500 constituents + GICS sector from Wikipedia.
    Returns [(symbol, sector), ...], deduped, preserving order."""
    try:
        r = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0 invest-scanner/1.0"},
            timeout=15,
        )
        r.raise_for_status()
        parser = _SP500Parser()
        parser.feed(r.text)
        rows = list(dict(parser.rows).items())  # dedupe by symbol, preserve order
        log.info(f"Fetched {len(rows)} S&P 500 symbols (with sector) from Wikipedia")
        return rows
    except Exception as e:
        log.warning(f"Could not fetch S&P 500 from Wikipedia: {e}")
        return []


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
    """
    Populate universe table.
    - All Alpaca US equities (full metadata, tradable flag)
    - Mark S&P 500 + core ETFs as scannable=True (these get RSI scanned)
    - Tag S&P 500 constituents with their GICS sector (for the sector cap)
    """
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM universe WHERE scannable = TRUE")
        row = cur.fetchone()
        scannable = row[0] if isinstance(row, (list, tuple)) else row["count"]

    if scannable <= 400:
        # Fetch all Alpaca assets for metadata
        log.info("Seeding universe from Alpaca assets API...")
        try:
            r = requests.get(
                f"{ALPACA_BASE}/v2/assets",
                params={"status": "active", "asset_class": "us_equity"},
                headers=ALPACA_HEADERS,
                timeout=30,
            )
            r.raise_for_status()
            assets = r.json()
            valid_exchanges = {"NYSE", "NASDAQ", "ARCA", "BATS", "NYSE ARCA", "NYSE MKT"}
            with conn.cursor() as cur:
                for a in assets:
                    if (a.get("tradable") and a.get("exchange") in valid_exchanges
                            and "." not in a["symbol"] and "/" not in a["symbol"]
                            and len(a["symbol"]) <= 5):
                        cur.execute("""
                            INSERT INTO universe (symbol, name, exchange)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (symbol) DO NOTHING
                        """, (a["symbol"], a.get("name", ""), a.get("exchange", "")))
            conn.commit()
            log.info(f"Universe: {len(assets)} Alpaca assets loaded")
        except Exception as e:
            log.warning(f"Alpaca asset seed failed: {e}")
    else:
        log.info(f"Universe already seeded ({scannable} scannable symbols)")

    # Sector backfill runs independently of the scannable check above, so
    # upgrading an already-seeded deployment to sector-aware caps doesn't
    # require a full universe reset.
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM universe WHERE sector IS NOT NULL")
        sector_count = cur.fetchone()[0]

    if sector_count >= 100:
        log.info(f"Sector data already populated ({sector_count} symbols)")
        return

    sp500 = _fetch_sp500_data()
    sector_map = dict(sp500)
    scannable_syms = list(dict.fromkeys([s for s, _ in sp500] + CORE_ETFS))
    with conn.cursor() as cur:
        for sym in scannable_syms:
            cur.execute("""
                INSERT INTO universe (symbol, name, exchange, scannable, sector)
                VALUES (%s, '', '', TRUE, %s)
                ON CONFLICT (symbol) DO UPDATE SET
                    scannable = TRUE,
                    sector = COALESCE(EXCLUDED.sector, universe.sector)
            """, (sym, sector_map.get(sym)))
    conn.commit()
    log.info(f"Marked {len(scannable_syms)} symbols as scannable (S&P 500 + ETFs), "
             f"{len(sector_map)} with GICS sector")


def _fetch_closes_yf(symbol, range_="1mo"):
    """Fetch daily closes from Yahoo Finance. Returns list oldest→newest, or []."""
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
    try:
        r = requests.get(url, params={"interval": "1d", "range": range_},
                         headers=YF_HEADERS, timeout=10)
        r.raise_for_status()
        result = r.json().get("chart", {}).get("result", [])
        if not result:
            return []
        closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
        return [float(c) for c in closes if c is not None]
    except Exception:
        return []


def scan_universe(conn):
    """
    Fast RSI scan across scannable symbols (S&P 500 + ETFs) via Yahoo Finance.
    Per-symbol with SCAN_DELAY between requests. Upserts into universe_scan.
    Returns list of top candidates.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM universe WHERE scannable = TRUE ORDER BY symbol")
        rows = cur.fetchall()
    symbols = [r[0] if isinstance(r, (list, tuple)) else r["symbol"] for r in rows]

    if not symbols:
        log.warning("No scannable symbols — run seed_universe first")
        return []

    log.info(f"Scanning {len(symbols)} scannable symbols via Yahoo Finance...")
    total_scanned = 0
    candidates = []

    for sym in symbols:
        closes = _fetch_closes_yf(sym, range_="1mo")
        if len(closes) < 16:
            time.sleep(SCAN_DELAY)
            continue

        price = closes[-1]
        rsi = _rsi(closes)
        buy_sc = _buy_score(rsi, price, closes)
        sell_sc = _sell_score(rsi, price, closes)

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO universe_scan (symbol, price, rsi, buy_score, sell_score, regime, scanned_at)
                VALUES (%s, %s, %s, %s, %s, 'unknown', NOW())
                ON CONFLICT (symbol) DO UPDATE SET
                    price=EXCLUDED.price, rsi=EXCLUDED.rsi,
                    buy_score=EXCLUDED.buy_score, sell_score=EXCLUDED.sell_score,
                    regime=EXCLUDED.regime, scanned_at=EXCLUDED.scanned_at
            """, (sym, price, rsi, buy_sc, sell_sc))
        conn.commit()

        if buy_sc >= PROMOTE_THRESHOLD or sell_sc >= PROMOTE_THRESHOLD:
            candidates.append({"symbol": sym, "price": price, "rsi": rsi,
                                "buy_score": buy_sc, "sell_score": sell_sc})
        total_scanned += 1
        time.sleep(SCAN_DELAY)

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

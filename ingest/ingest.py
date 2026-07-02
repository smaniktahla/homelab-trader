#!/usr/bin/env python3
"""Scheduled ingest: price history, news, order reconciliation, and universe scan."""

import os, time, logging, smtplib
import psycopg2
import requests
from datetime import datetime, timezone, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

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

ATQ_URL    = os.environ.get("ATQ_URL", "http://10.10.10.226:8700")

# SMTP/notification settings are loaded from app_settings table at send time
_SMTP_ENV_USER = os.environ.get("SMTP_USER", "")
_SMTP_ENV_PASS = os.environ.get("SMTP_PASS", "")


def get_app_settings(conn):
    """Load settings from app_settings table, fall back to env vars."""
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM app_settings")
        rows = cur.fetchall()
    s = {r[0]: r[1] for r in rows} if rows else {}
    return {
        "smtp_user":        s.get("smtp_user") or _SMTP_ENV_USER,
        "smtp_pass":        s.get("smtp_pass") or _SMTP_ENV_PASS,
        "digest_to":        s.get("digest_to") or s.get("smtp_user") or _SMTP_ENV_USER,
        "atq_url":          s.get("atq_url") or ATQ_URL,
        "notify_email":     s.get("notify_email", "true") == "true",
        "notify_whatsapp":  s.get("notify_whatsapp", "true") == "true",
        "digest_hour_utc":  int(s.get("digest_hour_utc", "21")),
        "digest_minute_utc": int(s.get("digest_minute_utc", "30")),
    }


def get_last_digest_date(conn):
    """Read persisted last digest date from DB."""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key='last_digest_date'")
        row = cur.fetchone()
    if row and row[0]:
        try:
            return date.fromisoformat(row[0])
        except ValueError:
            pass
    return None


def set_last_digest_date(conn, d: date):
    """Persist last digest date to DB so restarts don't skip digests."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value) VALUES ('last_digest_date', %s) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            (d.isoformat(),)
        )
    conn.commit()


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
                yf_range = "1y" if count == 0 else "5d"
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


def send_digest(conn):
    """Send daily portfolio digest email + WhatsApp ping after market close."""
    try:
        # Fetch account + positions from Alpaca
        acct = requests.get(f"{ALPACA_BASE}/v2/account", headers=ALPACA_HEADERS, timeout=10).json()
        pos_list = requests.get(f"{ALPACA_BASE}/v2/positions", headers=ALPACA_HEADERS, timeout=10).json()

        portfolio_value = float(acct.get("portfolio_value", 0))
        cash = float(acct.get("cash", 0))
        last_equity = float(acct.get("last_equity", portfolio_value))
        day_pl = portfolio_value - last_equity
        day_pct = (day_pl / last_equity * 100) if last_equity else 0

        # SPY benchmark
        spy_pct = None
        try:
            r = requests.get("https://query2.finance.yahoo.com/v8/finance/chart/SPY",
                             params={"interval": "1d", "range": "2d"},
                             headers=YF_HEADERS, timeout=10)
            result = r.json()["chart"]["result"][0]
            closes = result["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            if len(closes) >= 2:
                spy_pct = (closes[-1] - closes[-2]) / closes[-2] * 100
        except Exception:
            pass

        # Pending proposals
        with conn.cursor() as cur:
            cur.execute("SELECT symbol, side, qty, signal_score FROM trade_proposals WHERE decision IS NULL ORDER BY signal_score DESC")
            proposals = cur.fetchall()

        # Build HTML email
        sign = lambda v: ("+" if v >= 0 else "") + f"{v:.2f}"
        color = lambda v: "#2ecc71" if v >= 0 else "#e74c3c"

        pos_rows = ""
        total_pl = 0
        for p in pos_list:
            upl = float(p.get("unrealized_pl", 0))
            total_pl += upl
            plpct = float(p.get("unrealized_plpc", 0)) * 100
            pos_rows += f"""
            <tr>
              <td style="padding:6px 12px;font-weight:600">{p['symbol']}</td>
              <td style="padding:6px 12px">{float(p['qty']):.0f} shares</td>
              <td style="padding:6px 12px">${float(p['current_price']):.2f}</td>
              <td style="padding:6px 12px;color:{color(upl)}">{sign(upl)}</td>
              <td style="padding:6px 12px;color:{color(plpct)}">{sign(plpct)}%</td>
            </tr>"""

        prop_rows = ""
        for p in proposals:
            prop_rows += f"<tr><td style='padding:4px 12px'>{p[0] if isinstance(p, tuple) else p['symbol']}</td><td style='padding:4px 12px'>{p[1] if isinstance(p, tuple) else p['side']}</td><td style='padding:4px 12px'>{p[3] if isinstance(p, tuple) else p['signal_score']}</td></tr>"

        spy_line = f" &nbsp;·&nbsp; SPY {sign(spy_pct)}%" if spy_pct is not None else ""
        proposals_section = ""
        if proposals:
            proposals_section = f"""
            <h3 style="color:#aaa;font-size:13px;margin:24px 0 8px">PENDING PROPOSALS ({len(proposals)})</h3>
            <table style="border-collapse:collapse;width:100%">
              <tr style="color:#888;font-size:11px"><td style="padding:4px 12px">SYMBOL</td><td style="padding:4px 12px">SIDE</td><td style="padding:4px 12px">SCORE</td></tr>
              {prop_rows}
            </table>"""

        today = date.today().strftime("%B %d, %Y")
        html = f"""
        <div style="font-family:sans-serif;background:#0d0f1a;color:#e8eaf6;padding:24px;max-width:600px">
          <h2 style="margin:0 0 4px">Portfolio Digest &mdash; {today}</h2>
          <p style="color:#888;margin:0 0 20px;font-size:13px">Paper trading · Alpaca</p>

          <div style="display:flex;gap:16px;margin-bottom:24px">
            <div style="background:#1a1d27;border-radius:8px;padding:14px 20px;flex:1">
              <div style="color:#888;font-size:11px;margin-bottom:4px">PORTFOLIO VALUE</div>
              <div style="font-size:22px;font-weight:700">${portfolio_value:,.2f}</div>
            </div>
            <div style="background:#1a1d27;border-radius:8px;padding:14px 20px;flex:1">
              <div style="color:#888;font-size:11px;margin-bottom:4px">TODAY{spy_line}</div>
              <div style="font-size:22px;font-weight:700;color:{color(day_pl)}">{sign(day_pl)} ({sign(day_pct)}%)</div>
            </div>
            <div style="background:#1a1d27;border-radius:8px;padding:14px 20px;flex:1">
              <div style="color:#888;font-size:11px;margin-bottom:4px">UNREALIZED P&L</div>
              <div style="font-size:22px;font-weight:700;color:{color(total_pl)}">{sign(total_pl)}</div>
            </div>
          </div>

          <h3 style="color:#aaa;font-size:13px;margin:0 0 8px">POSITIONS</h3>
          <table style="border-collapse:collapse;width:100%;background:#1a1d27;border-radius:8px">
            <tr style="color:#888;font-size:11px">
              <td style="padding:6px 12px">SYMBOL</td><td style="padding:6px 12px">SHARES</td>
              <td style="padding:6px 12px">PRICE</td><td style="padding:6px 12px">P&L</td>
              <td style="padding:6px 12px">RETURN</td>
            </tr>
            {pos_rows}
          </table>

          {proposals_section}

          <p style="color:#555;font-size:11px;margin-top:24px">
            <a href="http://10.10.10.13:8100" style="color:#4f8ef7">Open Dashboard</a>
          </p>
        </div>"""

        cfg = get_app_settings(conn)

        # Send email
        if cfg["notify_email"] and cfg["smtp_user"] and cfg["smtp_pass"]:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"📈 Portfolio Digest {today} | {sign(day_pl)} ({sign(day_pct)}%)"
            msg["From"] = cfg["smtp_user"]
            msg["To"] = cfg["digest_to"]
            msg.attach(MIMEText(html, "html"))
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
                s.login(cfg["smtp_user"], cfg["smtp_pass"])
                s.sendmail(cfg["smtp_user"], cfg["digest_to"], msg.as_string())
            log.info(f"Digest email sent to {cfg['digest_to']}")

        # WhatsApp ping via ATQ
        if cfg["notify_whatsapp"]:
            spy_note = f" (SPY {sign(spy_pct)}%)" if spy_pct is not None else ""
            whatsapp_msg = (
                f"📈 *Portfolio Digest {today}*\n"
                f"Value: ${portfolio_value:,.0f} | Today: {sign(day_pl)} ({sign(day_pct)}%){spy_note}\n"
            )
            for p in pos_list:
                upl = float(p.get("unrealized_pl", 0))
                whatsapp_msg += f"  {p['symbol']}: {sign(upl)}\n"
            if proposals:
                whatsapp_msg += f"\n⚡ {len(proposals)} pending proposal(s) — check dashboard"
            try:
                requests.post(f"{cfg['atq_url']}/whatsapp/send", json={
                    "message": whatsapp_msg,
                }, timeout=10)
                log.info("WhatsApp digest sent via ATQ proxy")
            except Exception as e:
                log.warning(f"WhatsApp send failed: {e}")

    except Exception as e:
        log.error(f"Digest failed: {e}")


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

            # Daily digest: weekdays at configured UTC hour (default 21:30 = 4:30pm ET)
            now_utc = datetime.now(timezone.utc)
            is_weekday = now_utc.weekday() < 5
            try:
                cfg = get_app_settings(conn)
                digest_hour = cfg["digest_hour_utc"]
                digest_minute = cfg["digest_minute_utc"]
            except Exception:
                digest_hour, digest_minute = 21, 30
            is_digest_time = now_utc.hour == digest_hour and now_utc.minute >= digest_minute
            last_digest_date = get_last_digest_date(conn)
            if is_weekday and is_digest_time and last_digest_date != now_utc.date():
                send_digest(conn)
                set_last_digest_date(conn, now_utc.date())
        except Exception as e:
            log.error(f"Ingest cycle failed: {e}")
        finally:
            conn.close()
        log.info(f"Sleeping {INTERVAL_SECONDS}s")
        time.sleep(INTERVAL_SECONDS)

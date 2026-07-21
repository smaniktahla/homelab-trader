#!/usr/bin/env python3
"""Scheduled ingest: price history, news, order reconciliation, and universe scan."""

import os, time, logging, smtplib
import psycopg2
import requests
from datetime import datetime, timezone, date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from signals import compute_signals
from scanner import seed_universe, scan_universe, promote_demote
from market_regime import compute_market_regime, save_market_context
from outcomes import update_signal_outcomes
from earnings import sync_earnings_calendar
from postmortem import run_postmortem_review

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_DSN = os.environ["DATABASE_URL"]
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET", "")
ALPACA_BASE = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
ALPACA_DATA_BASE = "https://data.alpaca.markets"
HOURLY_LOOKBACK_DAYS = 2   # short incremental window; ON CONFLICT DO NOTHING makes
                           # re-fetching overlap harmless, same idiom as ingest_prices()'s
                           # 5d incremental daily pull — no per-symbol cursor to track
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
        "smtp_user":              s.get("smtp_user") or _SMTP_ENV_USER,
        "smtp_pass":              s.get("smtp_pass") or _SMTP_ENV_PASS,
        "digest_to":              s.get("digest_to") or s.get("smtp_user") or _SMTP_ENV_USER,
        "atq_url":                s.get("atq_url") or ATQ_URL,
        "notify_email":           s.get("notify_email", "true") == "true",
        "notify_whatsapp":        s.get("notify_whatsapp", "true") == "true",
        "digest_hour_utc":        int(s.get("digest_hour_utc", "21")),
        "digest_minute_utc":      int(s.get("digest_minute_utc", "30")),
        "morning_hour_utc":       int(s.get("morning_hour_utc", "13")),
        "morning_minute_utc":     int(s.get("morning_minute_utc", "30")),
        "alert_stop_loss":        s.get("alert_stop_loss", "true") == "true",
        "alert_portfolio_drop":   s.get("alert_portfolio_drop", "true") == "true",
        "alert_portfolio_drop_pct": float(s.get("alert_portfolio_drop_pct", "3.0")),
        "alert_high_score":       s.get("alert_high_score", "true") == "true",
        "alert_high_score_min":   float(s.get("alert_high_score_min", "80")),
        "alert_circuit_breaker":  s.get("alert_circuit_breaker", "true") == "true",
    }


def get_kv(conn, key):
    """Read a single value from app_settings."""
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key=%s", (key,))
        row = cur.fetchone()
    if row and row[0]:
        return row[0]
    return None

def set_kv(conn, key, value):
    """Write a single value to app_settings."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            (key, str(value))
        )
    conn.commit()

def get_last_digest_date(conn):
    v = get_kv(conn, "last_digest_date")
    if v:
        try:
            return date.fromisoformat(v)
        except ValueError:
            pass
    return None

def set_last_digest_date(conn, d: date):
    set_kv(conn, "last_digest_date", d.isoformat())

def get_last_postmortem_at(conn):
    v = get_kv(conn, "last_postmortem_at")
    if v:
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            pass
    return None

def set_last_postmortem_at(conn, dt: datetime):
    set_kv(conn, "last_postmortem_at", dt.isoformat())

def get_last_morning_date(conn):
    v = get_kv(conn, "last_morning_date")
    if v:
        try:
            return date.fromisoformat(v)
        except ValueError:
            pass
    return None

def set_last_morning_date(conn, d: date):
    set_kv(conn, "last_morning_date", d.isoformat())

def alert_throttled(conn, alert_key, hours=4):
    """Return True if we've already sent this alert within the throttle window."""
    v = get_kv(conn, f"alert_sent_{alert_key}")
    if v:
        try:
            last = datetime.fromisoformat(v)
            if (datetime.now(timezone.utc) - last).total_seconds() < hours * 3600:
                return True
        except ValueError:
            pass
    return False

def mark_alert_sent(conn, alert_key):
    set_kv(conn, f"alert_sent_{alert_key}", datetime.now(timezone.utc).isoformat())

def get_market_status():
    """
    Returns (is_trading_day, is_currently_open, next_open_str).
    Uses Alpaca /v2/calendar to determine if today is a scheduled trading day.
    """
    today_str = date.today().isoformat()
    try:
        r = requests.get(f"{ALPACA_BASE}/v2/calendar",
                         params={"start": today_str, "end": today_str},
                         headers=ALPACA_HEADERS, timeout=8)
        r.raise_for_status()
        calendar = r.json()
        is_trading_day = len(calendar) > 0
    except Exception as e:
        log.warning(f"Could not check market calendar: {e}")
        is_trading_day = True  # assume trading on failure

    try:
        r2 = requests.get(f"{ALPACA_BASE}/v2/clock", headers=ALPACA_HEADERS, timeout=8)
        r2.raise_for_status()
        clock = r2.json()
        is_open = clock.get("is_open", False)
        next_open = clock.get("next_open", "")[:10]
    except Exception:
        is_open, next_open = False, ""

    return is_trading_day, is_open, next_open


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
                # Full 1y backfill whenever history is thinner than ~3 months
                # of trading days, not just on a symbol's literal first ingest —
                # a symbol that got demoted from the watchlist after a partial
                # ingest (e.g. only 5d) would otherwise never catch up even if
                # it's re-promoted or bought later. 5d incremental otherwise.
                cur.execute("SELECT COUNT(*) FROM price_history WHERE symbol=%s", (sym,))
                count = cur.fetchone()[0]
                yf_range = "1y" if count < 65 else "5d"
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


def ingest_hourly_prices(conn, symbols):
    """Keep price_history_hourly fresh for the given symbols. Source is
    Alpaca directly (not Yahoo) — a single intraday source avoids the
    cross-source timestamp-alignment problem backfill_alpaca.py's daily
    _normalize_ts exists to solve. Short incremental window, same idiom as
    ingest_prices()'s 5d daily pull: ON CONFLICT DO NOTHING makes
    re-fetching overlap harmless, so there's no per-symbol cursor to track.
    Nothing reads price_history_hourly yet — this just keeps it from going
    stale immediately after a one-off backfill_intraday_alpaca.py run. See
    docs/thesis-horizons-and-intraday-data.md."""
    if not symbols:
        return
    start = (datetime.now(timezone.utc) - timedelta(days=HOURLY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    symbols = sorted(symbols)
    with conn.cursor() as cur:
        for i in range(0, len(symbols), 8):
            batch = symbols[i:i + 8]
            try:
                r = requests.get(f"{ALPACA_DATA_BASE}/v2/stocks/bars",
                                  headers=ALPACA_HEADERS,
                                  params={"symbols": ",".join(batch), "timeframe": "1Hour",
                                          "start": start, "limit": 10000, "feed": "iex",
                                          "adjustment": "split"},
                                  timeout=30)
                r.raise_for_status()
                bars_by_symbol = r.json().get("bars") or {}
            except Exception as e:
                log.warning(f"Hourly price ingest failed for batch {batch}: {e}")
                continue
            for sym, bars in bars_by_symbol.items():
                n = 0
                for b in bars:
                    if b.get("o") is None or b.get("c") is None:
                        continue
                    ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(timezone.utc)
                    cur.execute("""
                        INSERT INTO price_history_hourly (symbol, ts, open, high, low, close, volume)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (symbol, ts) DO NOTHING
                    """, (sym, ts, b["o"], b["h"], b["l"], b["c"], b.get("v")))
                    n += cur.rowcount
                log.info(f"Hourly prices for {sym}: {len(bars)} bars, {n} new rows")
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


def send_notification(cfg, subject, html_body, whatsapp_text, log_label="notification"):
    """Send email + WhatsApp based on cfg toggles."""
    if cfg["notify_email"] and cfg["smtp_user"] and cfg["smtp_pass"]:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = cfg["smtp_user"]
            msg["To"] = cfg["digest_to"]
            msg.attach(MIMEText(html_body, "html"))
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
                s.login(cfg["smtp_user"], cfg["smtp_pass"])
                s.sendmail(cfg["smtp_user"], cfg["digest_to"], msg.as_string())
            log.info(f"{log_label} email sent to {cfg['digest_to']}")
        except Exception as e:
            log.error(f"{log_label} email failed: {e}")

    if cfg["notify_whatsapp"] and whatsapp_text:
        try:
            requests.post(f"{cfg['atq_url']}/whatsapp/send",
                          json={"message": whatsapp_text}, timeout=10)
            log.info(f"{log_label} WhatsApp sent")
        except Exception as e:
            log.warning(f"{log_label} WhatsApp failed: {e}")


def send_holiday_digest(conn, cfg, next_open=""):
    """Send a brief 'markets closed today' message on weekday holidays."""
    today = date.today().strftime("%B %d, %Y")
    next_open_str = f" Markets reopen {next_open}." if next_open else ""

    html = f"""
    <div style="font-family:sans-serif;background:#0d0f1a;color:#e8eaf6;padding:24px;max-width:600px">
      <h2 style="margin:0 0 8px">📅 Markets Closed — {today}</h2>
      <p style="color:#888">It's a market holiday today — no trading activity, no new signals.{next_open_str}</p>
      <p style="color:#555;font-size:11px;margin-top:20px">
        <a href="http://10.10.10.13:8100" style="color:#4f8ef7">Open Dashboard</a>
      </p>
    </div>"""
    whatsapp = f"📅 Markets closed today ({today}).{next_open_str} No new signals — enjoy the day!"
    send_notification(cfg, f"📅 Markets Closed — {today}", html, whatsapp, "holiday")


def send_morning_digest(conn, cfg):
    """Send pre-market morning briefing."""
    try:
        acct = requests.get(f"{ALPACA_BASE}/v2/account", headers=ALPACA_HEADERS, timeout=10).json()
        pos_list = requests.get(f"{ALPACA_BASE}/v2/positions", headers=ALPACA_HEADERS, timeout=10).json()

        portfolio_value = float(acct.get("portfolio_value", 0))
        cash = float(acct.get("cash", 0))

        with conn.cursor() as cur:
            cur.execute("SELECT symbol, side, qty, signal_score FROM trade_proposals WHERE decision IS NULL ORDER BY signal_score DESC LIMIT 5")
            proposals = cur.fetchall()
            cur.execute("""
                SELECT symbol, price, rsi, buy_score FROM universe_scan
                WHERE buy_score >= 50 AND scanned_at > NOW() - INTERVAL '12 hours'
                ORDER BY buy_score DESC, rsi ASC LIMIT 5
            """)
            top_buys = cur.fetchall()
            cur.execute("SELECT key, value FROM signal_params WHERE key='stop_loss_pct'")
            sl_row = cur.fetchone()
            stop_loss_pct = float(sl_row[1]) if sl_row else 0.08

        today = date.today().strftime("%B %d, %Y")
        sign = lambda v: ("+" if v >= 0 else "") + f"{v:.2f}"

        # Positions table
        pos_rows = ""
        stop_alerts = []
        for p in pos_list:
            plpc = float(p.get("unrealized_plpc", 0)) * 100
            upl = float(p.get("unrealized_pl", 0))
            color = "#2ecc71" if upl >= 0 else "#e74c3c"
            if float(p.get("unrealized_plpc", 0)) <= -stop_loss_pct:
                stop_alerts.append(p["symbol"])
            pos_rows += f"""
            <tr>
              <td style="padding:5px 12px;font-weight:600">{p['symbol']}</td>
              <td style="padding:5px 12px">{float(p['qty']):.0f}</td>
              <td style="padding:5px 12px">${float(p['current_price']):.2f}</td>
              <td style="padding:5px 12px;color:{color}">{sign(upl)} ({sign(plpc)}%)</td>
            </tr>"""

        stop_html = ""
        if stop_alerts:
            stop_html = f"<p style='color:#e74c3c;font-weight:600'>⚠️ Near stop-loss: {', '.join(stop_alerts)}</p>"

        prop_html = ""
        if proposals:
            rows = "".join(f"<tr><td style='padding:4px 12px'>{p[0]}</td><td style='padding:4px 12px'>{p[1]}</td><td style='padding:4px 12px'>{p[3]}</td></tr>" for p in proposals)
            prop_html = f"""
            <h3 style="color:#aaa;font-size:13px;margin:20px 0 8px">PENDING PROPOSALS ({len(proposals)})</h3>
            <table style="border-collapse:collapse;background:#1a1d27;border-radius:8px;width:100%">
              <tr style="color:#888;font-size:11px"><td style="padding:4px 12px">SYMBOL</td><td style="padding:4px 12px">SIDE</td><td style="padding:4px 12px">SCORE</td></tr>
              {rows}
            </table>"""

        watch_html = ""
        if top_buys:
            badges = " ".join(f"<span style='background:#1a2a4a;color:#4f8ef7;border:1px solid #2a4a7a;padding:3px 10px;border-radius:20px;font-size:.82rem;font-weight:600'>{r[0]} RSI {float(r[2]):.0f}</span>" for r in top_buys)
            watch_html = f"<h3 style='color:#aaa;font-size:13px;margin:20px 0 8px'>WATCH TODAY</h3><div style='display:flex;flex-wrap:wrap;gap:6px'>{badges}</div>"

        html = f"""
        <div style="font-family:sans-serif;background:#0d0f1a;color:#e8eaf6;padding:24px;max-width:600px">
          <h2 style="margin:0 0 4px">☀️ Morning Briefing — {today}</h2>
          <p style="color:#888;margin:0 0 20px;font-size:13px">Portfolio: ${portfolio_value:,.2f} &nbsp;·&nbsp; Cash: ${cash:,.2f}</p>
          {stop_html}
          <h3 style="color:#aaa;font-size:13px;margin:0 0 8px">YOUR POSITIONS</h3>
          <table style="border-collapse:collapse;width:100%;background:#1a1d27;border-radius:8px">
            <tr style="color:#888;font-size:11px">
              <td style="padding:5px 12px">SYMBOL</td><td style="padding:5px 12px">QTY</td>
              <td style="padding:5px 12px">PRICE</td><td style="padding:5px 12px">UNREALIZED P&L</td>
            </tr>
            {pos_rows}
          </table>
          {prop_html}
          {watch_html}
          <p style="color:#555;font-size:11px;margin-top:24px">
            <a href="http://10.10.10.13:8100" style="color:#4f8ef7">Open Dashboard</a>
          </p>
        </div>"""

        pos_summary = " | ".join(f"{p['symbol']} {sign(float(p['unrealized_plpc'])*100)}%" for p in pos_list[:5])
        whatsapp = f"☀️ *Morning Briefing — {today}*\nPortfolio: ${portfolio_value:,.0f} · Cash: ${cash:,.0f}\n{pos_summary}"
        if stop_alerts:
            whatsapp += f"\n⚠️ Near stop-loss: {', '.join(stop_alerts)}"
        if proposals:
            whatsapp += f"\n⚡ {len(proposals)} pending proposal(s)"
        if top_buys:
            whatsapp += f"\n👀 Watch: {', '.join(r[0] for r in top_buys[:3])}"

        send_notification(cfg, f"☀️ Morning Briefing — {today}", html, whatsapp, "morning digest")

    except Exception as e:
        log.error(f"Morning digest failed: {e}")


def check_alerts(conn, cfg):
    """Check for drastic conditions each cycle and send immediate alerts."""
    try:
        acct = requests.get(f"{ALPACA_BASE}/v2/account", headers=ALPACA_HEADERS, timeout=10).json()
        pos_list = requests.get(f"{ALPACA_BASE}/v2/positions", headers=ALPACA_HEADERS, timeout=10).json()
        portfolio_value = float(acct.get("portfolio_value", 0))
        last_equity = float(acct.get("last_equity", portfolio_value))
    except Exception as e:
        log.warning(f"Alert check: could not fetch account data: {e}")
        return

    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM signal_params WHERE key IN ('stop_loss_pct')")
        params = {r[0]: float(r[1]) for r in cur.fetchall()}
    stop_loss_pct = params.get("stop_loss_pct", 0.08)

    # ── Stop-loss alerts ──────────────────────────────────────────────────
    if cfg["alert_stop_loss"]:
        for p in pos_list:
            plpc = float(p.get("unrealized_plpc", 0))
            if plpc <= -stop_loss_pct:
                sym = p["symbol"]
                key = f"stoploss_{sym}"
                if not alert_throttled(conn, key, hours=4):
                    pct_str = f"{plpc*100:.1f}%"
                    subject = f"🚨 Stop-Loss Alert: {sym} is down {pct_str}"
                    html = f"""
                    <div style="font-family:sans-serif;background:#0d0f1a;color:#e8eaf6;padding:24px;max-width:500px">
                      <h2 style="color:#e74c3c;margin:0 0 12px">🚨 Stop-Loss Alert</h2>
                      <p><strong>{sym}</strong> is down <strong style="color:#e74c3c">{pct_str}</strong> — at or past your {round(stop_loss_pct*100)}% stop-loss threshold.</p>
                      <p style="color:#888;margin-top:12px">Consider selling to limit further losses. Current price: ${float(p['current_price']):.2f}</p>
                      <p style="margin-top:20px"><a href="http://10.10.10.13:8100/symbol/{sym}" style="color:#4f8ef7">View {sym} →</a></p>
                    </div>"""
                    whatsapp = f"🚨 *Stop-Loss Alert: {sym}*\nDown {pct_str} — past your {round(stop_loss_pct*100)}% threshold. Consider selling.\nPrice: ${float(p['current_price']):.2f}\nDashboard: http://10.10.10.13:8100/symbol/{sym}"
                    send_notification(cfg, subject, html, whatsapp, f"stop-loss alert {sym}")
                    mark_alert_sent(conn, key)

    # ── Portfolio drop alert ──────────────────────────────────────────────
    if cfg["alert_portfolio_drop"] and last_equity > 0:
        day_drop_pct = (portfolio_value - last_equity) / last_equity * 100
        threshold = -cfg["alert_portfolio_drop_pct"]
        if day_drop_pct <= threshold and not alert_throttled(conn, "portfolio_drop", hours=4):
            subject = f"📉 Portfolio Down {day_drop_pct:.1f}% Today"
            html = f"""
            <div style="font-family:sans-serif;background:#0d0f1a;color:#e8eaf6;padding:24px;max-width:500px">
              <h2 style="color:#e74c3c;margin:0 0 12px">📉 Portfolio Alert</h2>
              <p>Your portfolio is down <strong style="color:#e74c3c">{day_drop_pct:.1f}%</strong> today.</p>
              <p style="color:#888">Value: ${portfolio_value:,.2f} (was ${last_equity:,.2f})</p>
              <p style="margin-top:20px"><a href="http://10.10.10.13:8100" style="color:#4f8ef7">Open Dashboard →</a></p>
            </div>"""
            whatsapp = f"📉 *Portfolio Alert*\nDown {day_drop_pct:.1f}% today.\nValue: ${portfolio_value:,.0f} (was ${last_equity:,.0f})\nhttp://10.10.10.13:8100"
            send_notification(cfg, subject, html, whatsapp, "portfolio drop alert")
            mark_alert_sent(conn, "portfolio_drop")

    # ── High-score signal alert ───────────────────────────────────────────
    if cfg["alert_high_score"]:
        min_score = cfg["alert_high_score_min"]
        with conn.cursor() as cur:
            # Secondary sort: volume spike ratio (today ÷ 30d avg).
            # Elevated volume on a dip is a capitulation signal; a normally-high-volume
            # stock trading at its usual level is not.
            cur.execute("""
                SELECT s.symbol, s.signal_type, s.score, s.rationale,
                       COALESCE((
                           SELECT ph.volume
                           FROM price_history ph
                           WHERE ph.symbol = s.symbol
                           ORDER BY ph.ts DESC LIMIT 1
                       ), 0) AS latest_volume,
                       COALESCE((
                           SELECT AVG(ph.volume)
                           FROM price_history ph
                           WHERE ph.symbol = s.symbol
                             AND ph.ts > NOW() - INTERVAL '30 days'
                       ), 1) AS avg_volume
                FROM signals s
                WHERE s.score >= %s AND s.generated_at > NOW() - INTERVAL '2 hours'
                ORDER BY s.score DESC,
                         LEAST(4.0,
                           COALESCE((SELECT ph2.volume FROM price_history ph2 WHERE ph2.symbol = s.symbol ORDER BY ph2.ts DESC LIMIT 1), 0)::FLOAT
                           / NULLIF((SELECT AVG(ph3.volume) FROM price_history ph3 WHERE ph3.symbol = s.symbol AND ph3.ts > NOW() - INTERVAL '30 days'), 0)
                         ) DESC NULLS LAST
                LIMIT 5
            """, (min_score,))
            hot_signals = cur.fetchall()

        if hot_signals:
            alert_key = f"highscore_{date.today().isoformat()}"
            if not alert_throttled(conn, alert_key, hours=6):
                # Capital context — acct already fetched at top of check_alerts
                buying_power  = float(acct.get("buying_power", acct.get("cash", 0)))
                open_count    = len(pos_list)
                with conn.cursor() as cur:
                    cur.execute("SELECT value FROM signal_params WHERE key='max_open_positions'")
                    row = cur.fetchone()
                max_open = int(float(row[0])) if row else 5
                slots_left = max(0, max_open - open_count)

                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT p.value * COALESCE(m.alloc_modifier, 1.0)
                        FROM signal_params p
                        LEFT JOIN market_context m ON TRUE
                        WHERE p.key = 'trade_allocation_pct'
                        LIMIT 1
                    """)
                    row = cur.fetchone()
                effective_alloc = float(row[0]) if row else 0.05
                trade_dollars = buying_power * effective_alloc

                capital_html = f"""
                  <table style="width:100%;border-collapse:collapse;margin-bottom:16px;font-size:.88rem">
                    <tr style="border-bottom:1px solid #2a2d3e">
                      <td style="padding:6px 0;color:#888">Buying power</td>
                      <td style="padding:6px 0;text-align:right;color:#e8eaf6"><strong>${buying_power:,.0f}</strong></td>
                    </tr>
                    <tr style="border-bottom:1px solid #2a2d3e">
                      <td style="padding:6px 0;color:#888">Position slots remaining</td>
                      <td style="padding:6px 0;text-align:right;color:{'#4caf50' if slots_left > 0 else '#e74c3c'}"><strong>{slots_left} of {max_open}</strong></td>
                    </tr>
                    <tr>
                      <td style="padding:6px 0;color:#888">Est. trade size (regime-adjusted)</td>
                      <td style="padding:6px 0;text-align:right;color:#e8eaf6"><strong>${trade_dollars:,.0f}</strong></td>
                    </tr>
                  </table>"""

                def _vol_fmt(v):
                    v = int(v or 0)
                    if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
                    if v >= 1_000: return f"{v/1_000:.0f}K"
                    return str(v)

                def _spike_label(latest, avg):
                    avg = float(avg or 1)
                    if avg == 0: return ""
                    ratio = min(float(latest or 0) / avg, 4.0)
                    capped = ratio >= 4.0
                    color = "#f7c94f" if 1.5 <= ratio <= 4.0 else ("#888" if ratio < 1.5 else "#e74c3c")
                    label = f"{ratio:.1f}×" + ("+ vol (event?)" if capped else f"× vol ({_vol_fmt(latest)})")
                    return f"<span style='color:{color};font-size:.8rem'> · {label}</span>"

                signal_items = "".join(
                    f"<li style='margin-bottom:10px'>"
                    f"<strong>{r[0]}</strong> — {r[1]} (score {r[2]})"
                    f"{_spike_label(r[4], r[5])}"
                    f"<br><span style='color:#888;font-size:.82rem'>{r[3]}</span></li>"
                    for r in hot_signals
                )
                subject = f"⚡ High-Confidence Signal: {hot_signals[0][0]}"
                html = f"""
                <div style="font-family:sans-serif;background:#0d0f1a;color:#e8eaf6;padding:24px;max-width:500px">
                  <h2 style="color:#f7c94f;margin:0 0 12px">⚡ High-Confidence Signal</h2>
                  <p style="margin:0 0 16px">Strong signal(s) detected above your {min_score:.0f} score threshold:</p>
                  {capital_html}
                  <ul style="color:#ccc;margin:12px 0;padding-left:18px">
                    {signal_items}
                  </ul>
                  <p style="margin-top:16px"><a href="http://10.10.10.13:8100" style="color:#4f8ef7">Review Proposals →</a></p>
                </div>"""

                def _spike_str(latest, avg):
                    avg = float(avg or 1)
                    if not avg: return ""
                    ratio = min(float(latest or 0) / avg, 4.0)
                    return f"{ratio:.1f}×vol" + ("+" if ratio >= 4.0 else "")

                lines = "\n".join(
                    f"  {r[0]} score={r[2]} {_spike_str(r[4], r[5])}" for r in hot_signals
                )
                whatsapp = (
                    f"⚡ *High-Confidence Signal*\n"
                    f"Buying power: ${buying_power:,.0f} · {slots_left}/{max_open} slots · ~${trade_dollars:,.0f}/trade\n"
                    f"{lines}\nReview: http://10.10.10.13:8100"
                )
                send_notification(cfg, subject, html, whatsapp, "high-score alert")
                mark_alert_sent(conn, alert_key)

    # ── Circuit breaker alert ────────────────────────────────────────────
    if cfg["alert_circuit_breaker"]:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT drawdown_pct, high_water_mark FROM portfolio_snapshots
                ORDER BY snapshot_at DESC LIMIT 1
            """)
            row = cur.fetchone()
        if row:
            drawdown_pct, hwm = float(row[0]), float(row[1])
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM signal_params WHERE key='circuit_breaker_drawdown_pct'")
                r2 = cur.fetchone()
            threshold = float(r2[0]) if r2 else 0.15

            if drawdown_pct >= threshold and not alert_throttled(conn, "circuit_breaker", hours=12):
                pct_str = f"{drawdown_pct*100:.1f}%"
                subject = f"🛑 Circuit Breaker: Portfolio down {pct_str} from peak"
                html = f"""
                <div style="font-family:sans-serif;background:#0d0f1a;color:#e8eaf6;padding:24px;max-width:500px">
                  <h2 style="color:#e74c3c;margin:0 0 12px">🛑 Circuit Breaker Active</h2>
                  <p>Portfolio drawdown is <strong style="color:#e74c3c">{pct_str}</strong> from its
                     all-time high of <strong>${hwm:,.2f}</strong> — past the {round(threshold*100)}% threshold.</p>
                  <p style="color:#888;margin-top:12px">New BUY proposals are paused until the portfolio
                     recovers above the threshold. SELL proposals continue as normal. No positions are
                     liquidated automatically.</p>
                  <p style="margin-top:20px"><a href="http://10.10.10.13:8100" style="color:#4f8ef7">Open Dashboard →</a></p>
                </div>"""
                whatsapp = (
                    f"🛑 *Circuit Breaker Active*\n"
                    f"Drawdown {pct_str} from all-time high ${hwm:,.0f} — past {round(threshold*100)}% threshold.\n"
                    f"New BUY proposals paused. Sells continue. Nothing liquidated automatically.\n"
                    f"http://10.10.10.13:8100"
                )
                send_notification(cfg, subject, html, whatsapp, "circuit breaker alert")
                mark_alert_sent(conn, "circuit_breaker")


def get_extra_price_symbols(conn):
    """Symbols that need price_history kept fresh even after falling off the
    watchlist: currently-held positions and symbols with an open (undecided)
    proposal. The universe scanner demotes watchlist symbols independently of
    whether they're held or proposed, so without this a bought/proposed
    symbol's price chart silently goes stale the moment it's demoted.
    Widens only the price/news ingest set — signal generation still runs on
    the watchlist alone."""
    symbols = set()
    try:
        symbols.update(get_positions().keys())
    except Exception as e:
        log.warning(f"Could not fetch positions for price ingest: {e}")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT symbol FROM trade_proposals WHERE decision IS NULL")
            symbols.update(row[0] for row in cur.fetchall())
    except Exception as e:
        log.warning(f"Could not fetch open-proposal symbols for price ingest: {e}")
    return symbols


def check_new_proposal_alerts(conn, cfg):
    """Immediate WhatsApp+email alert for every newly-created trade_proposals
    row, buy or sell. Distinct from check_alerts()'s high-score signal alert
    below, which only queries the `signals` table — exit-driven proposals
    (thesis_complete/stop_loss/time_stop/regime_deterioration) are inserted
    straight into trade_proposals by signals.py and never touch `signals` at
    all, so that older alert can't see them. This is what would have caught
    the 2026-07-21 incident where three thesis_complete sell proposals sat
    unnoticed for 8 days. Runs every cycle regardless of market hours (a
    proposal existing is a DB fact, not a live-market condition) and alerts
    once per proposal id — a proposal is a discrete event, not a recurring
    condition like the other alerts, so this doesn't use alert_throttled's
    hours-based re-arm; the same key is reused permanently."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, symbol, side, rationale, signal_score, exit_reason
            FROM trade_proposals
            WHERE decision IS NULL
            ORDER BY proposed_at ASC
        """)
        proposals = cur.fetchall()

    for p in proposals:
        pid, sym, side, rationale, score, exit_reason = (
            p if isinstance(p, (list, tuple)) else
            (p["id"], p["symbol"], p["side"], p["rationale"], p["signal_score"], p["exit_reason"])
        )
        alert_key = f"proposal_{pid}"
        if alert_throttled(conn, alert_key, hours=24 * 365):
            continue

        label = "SELL" if side == "sell" else "BUY"
        reason_tag = f" [{exit_reason}]" if exit_reason else ""
        subject = f"📋 New Proposal: {label} {sym}{reason_tag}"
        html = f"""
        <div style="font-family:sans-serif;background:#0d0f1a;color:#e8eaf6;padding:24px;max-width:500px">
          <h2 style="margin:0 0 12px">📋 New Trade Proposal</h2>
          <p><strong>{label} {sym}</strong>{reason_tag} — score {score}</p>
          <p style="color:#888;margin-top:12px">{rationale}</p>
          <p style="margin-top:20px"><a href="http://10.10.10.13:8100" style="color:#4f8ef7">Review &amp; Approve →</a></p>
        </div>"""
        whatsapp = f"📋 *New Proposal: {label} {sym}*{reason_tag}\nScore: {score}\n{rationale}\nhttp://10.10.10.13:8100"
        send_notification(cfg, subject, html, whatsapp, f"new proposal {sym}")
        mark_alert_sent(conn, alert_key)
        log.info(f"New-proposal alert sent: {label} {sym}{reason_tag} (proposal id {pid})")


def run_once(conn, last_universe_scan):
    symbols = get_watchlist(conn)
    price_symbols = sorted(set(symbols) | get_extra_price_symbols(conn))
    log.info(f"Watchlist ({len(symbols)} symbols): {symbols}")
    ingest_prices(conn, price_symbols)
    ingest_hourly_prices(conn, price_symbols)
    ingest_news(conn, price_symbols)

    # Update market regime before running signals so gating is current
    try:
        ctx = compute_market_regime()
        save_market_context(conn, ctx)
    except Exception as e:
        log.warning(f"Market regime update failed: {e}")

    sync_earnings_calendar(conn)
    compute_signals(conn, symbols)
    reconcile_orders(conn)
    update_signal_outcomes(conn)

    # Every cycle, regardless of market hours — a proposal existing is a DB
    # fact, not a live-market condition, unlike the other alerts below.
    try:
        check_new_proposal_alerts(conn, get_app_settings(conn))
    except Exception as e:
        log.warning(f"New-proposal alert check failed: {e}")

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

            now_utc = datetime.now(timezone.utc)
            is_weekday = now_utc.weekday() < 5
            cfg = get_app_settings(conn)

            is_trading_day, is_currently_open, next_open = get_market_status()

            if is_weekday:
                today = now_utc.date()

                # ── Morning digest ────────────────────────────────────────────
                m_hour, m_min = cfg["morning_hour_utc"], cfg["morning_minute_utc"]
                past_morning = now_utc.hour > m_hour or (now_utc.hour == m_hour and now_utc.minute >= m_min)
                if past_morning and get_last_morning_date(conn) != today:
                    if is_trading_day:
                        send_morning_digest(conn, cfg)
                    else:
                        send_holiday_digest(conn, cfg, next_open)
                    set_last_morning_date(conn, today)

                # ── Evening digest (market days only) ─────────────────────────
                e_hour, e_min = cfg["digest_hour_utc"], cfg["digest_minute_utc"]
                past_evening = now_utc.hour > e_hour or (now_utc.hour == e_hour and now_utc.minute >= e_min)
                if past_evening and get_last_digest_date(conn) != today and is_trading_day:
                    send_digest(conn)
                    set_last_digest_date(conn, today)

                # ── Weekly strategy postmortem ─────────────────────────────────
                last_postmortem = get_last_postmortem_at(conn)
                if last_postmortem is None or (now_utc - last_postmortem).days >= 7:
                    try:
                        result = run_postmortem_review(conn)
                        subject = "📊 Weekly Strategy Review"
                        html = f"<p>{result['finding']}</p><p>N resolved: {result['n_resolved']}</p>"
                        wa_text = f"{subject}\n{result['finding']}"
                        send_notification(cfg, subject, html, wa_text, "postmortem review")
                        set_last_postmortem_at(conn, now_utc)
                    except Exception as e:
                        log.error(f"Postmortem review failed: {e}")

                # ── Alerts (every cycle on active market days) ────────────────
                if is_trading_day and is_currently_open:
                    check_alerts(conn, cfg)

        except Exception as e:
            log.error(f"Ingest cycle failed: {e}")
        finally:
            conn.close()
        log.info(f"Sleeping {INTERVAL_SECONDS}s")
        time.sleep(INTERVAL_SECONDS)

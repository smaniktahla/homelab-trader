from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional
import psycopg2, psycopg2.extras, os, requests as http, secrets
from datetime import datetime, timezone

DB_DSN = os.environ["DATABASE_URL"]
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET", "")
ALPACA_BASE = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

_security = HTTPBasic()
_AUTH_USER = os.environ.get("INVEST_USER", "invest")
_AUTH_PASS = os.environ.get("INVEST_PASS", "")

def _check_auth(creds: HTTPBasicCredentials = Depends(_security)):
    ok = (
        secrets.compare_digest(creds.username.encode(), _AUTH_USER.encode()) and
        secrets.compare_digest(creds.password.encode(), _AUTH_PASS.encode())
    )
    if not ok:
        raise HTTPException(status_code=401, detail="Unauthorized",
                            headers={"WWW-Authenticate": "Basic realm=invest"})

_global_deps = [Depends(_check_auth)] if _AUTH_PASS else []
app = FastAPI(title="invest-api", dependencies=_global_deps)
templates = Jinja2Templates(directory="/app/templates")

def db():
    return psycopg2.connect(DB_DSN, cursor_factory=psycopg2.extras.RealDictCursor)

def alpaca(method, path, **kwargs):
    r = http.request(method, f"{ALPACA_BASE}{path}", headers=ALPACA_HEADERS, timeout=10, **kwargs)
    r.raise_for_status()
    return r.json()


# ── Data endpoints ────────────────────────────────────────────────────────────

@app.get("/api/watchlist")
def get_watchlist():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT symbol, name, added_at FROM watchlist ORDER BY symbol")
        return cur.fetchall()

@app.get("/api/prices/{symbol}")
def get_prices(symbol: str, days: int = 30):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT ts, open, high, low, close, volume
            FROM price_history WHERE symbol = %s
            ORDER BY ts DESC LIMIT %s
        """, (symbol.upper(), days))
        rows = cur.fetchall()
        rows.reverse()
        return rows

@app.get("/api/news/{symbol}")
def get_news(symbol: str, limit: int = 20):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT headline, source, url, published_at, summary, sentiment_score
            FROM news WHERE symbol = %s ORDER BY published_at DESC LIMIT %s
        """, (symbol.upper(), limit))
        return cur.fetchall()

@app.get("/api/signals")
def get_signals(limit: int = 50):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, signal_type, score, rationale, generated_at, acted_on
            FROM signals ORDER BY generated_at DESC LIMIT %s
        """, (limit,))
        return cur.fetchall()

@app.get("/api/signals/latest")
def get_signals_latest():
    """Latest buy and sell signal per symbol for dashboard display."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (s.symbol, s.signal_type)
                s.symbol, s.signal_type, s.score, s.rationale, s.generated_at,
                u.rsi
            FROM signals s
            LEFT JOIN universe_scan u ON u.symbol = s.symbol
            ORDER BY s.symbol, s.signal_type, s.generated_at DESC
        """)
        rows = cur.fetchall()
        # pivot to {symbol: {buy: {...}, sell: {...}}}
        result = {}
        for row in rows:
            sym = row["symbol"]
            side = "buy" if "buy" in row["signal_type"] else "sell"
            if sym not in result:
                result[sym] = {}
            rat = row["rationale"] or ""
            if "ranging" in rat:
                regime = "ranging"
            elif "uptrend" in rat or "trending_up" in rat:
                regime = "trending_up"
            elif "downtrend" in rat or "trending_down" in rat:
                regime = "trending_down"
            else:
                regime = None
            result[sym][side] = {
                "score": float(row["score"]) if row["score"] is not None else 0,
                "rationale": rat,
                "generated_at": row["generated_at"].isoformat() if row["generated_at"] else None,
                "rsi": float(row["rsi"]) if row["rsi"] is not None else None,
                "regime": regime,
            }
        return result

@app.get("/api/trades")
def get_trades(limit: int = 200):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM trades ORDER BY traded_at DESC LIMIT %s
        """, (limit,))
        return cur.fetchall()

@app.get("/api/positions")
def get_positions():
    positions = alpaca("GET", "/v2/positions")
    return [{
        "symbol": p["symbol"],
        "qty": float(p["qty"]),
        "avg_entry_price": float(p["avg_entry_price"]),
        "current_price": float(p["current_price"]),
        "market_value": float(p["market_value"]),
        "cost_basis": float(p["cost_basis"]),
        "unrealized_pl": float(p["unrealized_pl"]),
        "unrealized_plpc": round(float(p["unrealized_plpc"]) * 100, 2),
        "side": p["side"],
    } for p in positions]

@app.get("/api/account")
def get_account():
    a = alpaca("GET", "/v2/account")
    return {
        "equity": float(a["equity"]),
        "cash": float(a["cash"]),
        "buying_power": float(a["buying_power"]),
        "portfolio_value": float(a["portfolio_value"]),
    }

@app.get("/api/proposals")
def get_proposals():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM trade_proposals
            WHERE decision IS NULL
            ORDER BY proposed_at DESC
        """)
        return cur.fetchall()

@app.get("/api/summary")
def get_summary():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (symbol) symbol, close, ts
                FROM price_history ORDER BY symbol, ts DESC
            ),
            prev AS (
                SELECT DISTINCT ON (ph.symbol) ph.symbol, ph.close
                FROM price_history ph
                JOIN latest l ON l.symbol = ph.symbol AND ph.ts < l.ts
                ORDER BY ph.symbol, ph.ts DESC
            )
            SELECT l.symbol, l.close AS price, l.ts AS as_of,
                   ROUND(((l.close - p.close) / p.close * 100)::numeric, 2) AS day_pct
            FROM latest l LEFT JOIN prev p ON p.symbol = l.symbol
            ORDER BY l.symbol
        """)
        return cur.fetchall()


# ── Trade execution ───────────────────────────────────────────────────────────

class TradeRequest(BaseModel):
    symbol: str
    side: str          # buy | sell
    qty: float
    notes: Optional[str] = None
    source: str = "manual"
    proposal_id: Optional[int] = None

@app.post("/api/trade")
def execute_trade(req: TradeRequest):
    if req.side not in ("buy", "sell"):
        raise HTTPException(400, "side must be buy or sell")
    if req.qty <= 0:
        raise HTTPException(400, "qty must be positive")

    # Submit to Alpaca
    order = alpaca("POST", "/v2/orders", json={
        "symbol": req.symbol.upper(),
        "qty": str(req.qty),
        "side": req.side,
        "type": "market",
        "time_in_force": "gtc",
    })

    filled_price = float(order.get("filled_avg_price") or order.get("limit_price") or 0)
    filled_qty = float(order.get("filled_qty") or req.qty)

    # Log to DB
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trades (symbol, side, qty, price, notional, order_id, traded_at, notes, source, status, proposal_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            req.symbol.upper(), req.side, filled_qty, filled_price,
            filled_qty * filled_price, order["id"],
            datetime.now(timezone.utc), req.notes, req.source,
            order["status"], req.proposal_id
        ))
        trade_id = cur.fetchone()["id"]
        conn.commit()

        # If this was from a proposal, mark it decided
        if req.proposal_id:
            cur.execute("""
                UPDATE trade_proposals SET decision='approved', decided_at=NOW(), decided_by='human'
                WHERE id=%s
            """, (req.proposal_id,))
            conn.commit()

    return {"trade_id": trade_id, "order_id": order["id"], "status": order["status"]}

class ProposalDecision(BaseModel):
    decision: str          # approved | rejected
    qty: Optional[float] = None
    rejection_reason: Optional[str] = None

@app.patch("/api/proposals/{proposal_id}")
def decide_proposal(proposal_id: int, body: ProposalDecision):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM trade_proposals WHERE id=%s", (proposal_id,))
        p = cur.fetchone()
        if not p:
            raise HTTPException(404, "proposal not found")
        if p["decision"]:
            raise HTTPException(409, "already decided")

        if body.decision == "approved":
            # Execute the trade
            trade_qty = body.qty or p["qty"]
            if not trade_qty:
                raise HTTPException(400, "qty required for approval (proposal has no default qty)")
            order = alpaca("POST", "/v2/orders", json={
                "symbol": p["symbol"], "qty": str(trade_qty),
                "side": p["side"], "type": "market", "time_in_force": "gtc",
            })
            filled_price = float(order.get("filled_avg_price") or 0)
            filled_qty = float(order.get("filled_qty") or p["qty"])
            cur.execute("""
                INSERT INTO trades (symbol, side, qty, price, notional, order_id, traded_at, source, status, proposal_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (p["symbol"], p["side"], filled_qty, filled_price,
                  filled_qty * filled_price, order["id"],
                  datetime.now(timezone.utc), "model_approved", order["status"], proposal_id))
            # update proposal qty if it was null
            cur.execute("UPDATE trade_proposals SET qty=%s WHERE id=%s AND qty IS NULL", (trade_qty, proposal_id))

        cur.execute("""
            UPDATE trade_proposals
            SET decision=%s, decided_at=NOW(), decided_by='human', rejection_reason=%s
            WHERE id=%s
        """, (body.decision, body.rejection_reason, proposal_id))
        conn.commit()

    return {"status": "ok", "decision": body.decision}


# ── Universe / Leaderboard ───────────────────────────────────────────────────

@app.get("/api/leaderboard")
def get_leaderboard(limit: int = 30, side: str = "both"):
    with db() as conn, conn.cursor() as cur:
        if side == "buy":
            order_col = "buy_score"
        elif side == "sell":
            order_col = "sell_score"
        else:
            order_col = "GREATEST(buy_score, sell_score)"
        cur.execute(f"""
            SELECT u.symbol, u.price, u.rsi, u.buy_score, u.sell_score, u.regime, u.scanned_at,
                   w.symbol IS NOT NULL AS on_watchlist,
                   w.pinned
            FROM universe_scan u
            LEFT JOIN watchlist w ON w.symbol = u.symbol
            WHERE GREATEST(u.buy_score, u.sell_score) > 0
            ORDER BY {order_col} DESC
            LIMIT %s
        """, (limit,))
        return cur.fetchall()

@app.get("/api/universe/stats")
def get_universe_stats():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS total FROM universe")
        total = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) AS scanned FROM universe_scan WHERE scanned_at > NOW() - INTERVAL '6 hours'")
        scanned = cur.fetchone()["scanned"]
        cur.execute("SELECT MAX(scanned_at) AS last_scan FROM universe_scan")
        last_scan = cur.fetchone()["last_scan"]
        return {"total": total, "scanned_recently": scanned, "last_scan": last_scan}


# ── Signal parameters ────────────────────────────────────────────────────────

@app.get("/api/signal-params")
def get_signal_params():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT key, value, description FROM signal_params ORDER BY key")
        return cur.fetchall()

class ParamUpdate(BaseModel):
    value: float

@app.patch("/api/signal-params/{key}")
def update_signal_param(key: str, body: ParamUpdate):
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE signal_params SET value=%s WHERE key=%s RETURNING key", (body.value, key))
        if not cur.fetchone():
            raise HTTPException(404, f"param '{key}' not found")
        conn.commit()
    return {"key": key, "value": body.value}


# ── Dashboard UI ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT symbol, name FROM watchlist ORDER BY symbol")
        watchlist = cur.fetchall()
    return templates.TemplateResponse("dashboard.html", {"request": request, "watchlist": watchlist})

@app.get("/symbol/{symbol}", response_class=HTMLResponse)
def symbol_page(request: Request, symbol: str):
    return templates.TemplateResponse("symbol.html", {"request": request, "symbol": symbol.upper()})


# ── User Profile / Wizard ─────────────────────────────────────────────────────

PROFILE_PRESETS = {
    "conservative": {"trade_allocation_pct": 0.03, "stop_loss_pct": 0.05, "score_proposal_min": 55.0},
    "balanced":     {"trade_allocation_pct": 0.05, "stop_loss_pct": 0.08, "score_proposal_min": 40.0},
    "aggressive":   {"trade_allocation_pct": 0.08, "stop_loss_pct": 0.12, "score_proposal_min": 30.0},
}


@app.get("/api/profile")
def get_profile():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM user_profile ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    return dict(row) if row else None


class ProfileCreate(BaseModel):
    risk_profile: str
    time_horizon: str
    account_value: float
    cash_reserve: float
    investable: float
    trade_allocation_pct: float
    max_open_positions: int
    stop_loss_pct: float
    score_proposal_min: float
    notes: Optional[str] = None


@app.post("/api/profile")
def save_profile(body: ProfileCreate):
    if body.risk_profile not in PROFILE_PRESETS:
        raise HTTPException(400, "risk_profile must be conservative | balanced | aggressive")
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM user_profile")
        cur.execute(
            "INSERT INTO user_profile "
            "(risk_profile, time_horizon, account_value, cash_reserve, investable, "
            "trade_allocation_pct, max_open_positions, stop_loss_pct, score_proposal_min, notes) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (body.risk_profile, body.time_horizon, body.account_value, body.cash_reserve,
             body.investable, body.trade_allocation_pct, body.max_open_positions,
             body.stop_loss_pct, body.score_proposal_min, body.notes)
        )
        for key, val in {
            "trade_allocation_pct": body.trade_allocation_pct,
            "max_open_positions": float(body.max_open_positions),
            "stop_loss_pct": body.stop_loss_pct,
            "score_proposal_min": body.score_proposal_min,
        }.items():
            cur.execute("UPDATE signal_params SET value=%s WHERE key=%s", (val, key))
        conn.commit()
    return {"status": "ok"}


# ── Portfolio Advisor ─────────────────────────────────────────────────────────

@app.get("/api/advisor")
def get_advisor():
    # --- gather inputs ---
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT key, value FROM signal_params")
        params = {r["key"]: float(r["value"]) for r in cur.fetchall()}

        cur.execute("SELECT * FROM user_profile ORDER BY id DESC LIMIT 1")
        profile = cur.fetchone()

        # market breadth from latest universe scan
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE buy_score >= 50)  AS oversold_n,
                COUNT(*) FILTER (WHERE sell_score >= 50) AS overbought_n,
                COUNT(*) AS total
            FROM universe_scan
            WHERE scanned_at > NOW() - INTERVAL '8 hours'
        """)
        breadth = cur.fetchone()

        cur.execute("""
            SELECT symbol, price, rsi, buy_score, sell_score
            FROM universe_scan
            WHERE buy_score >= 50
            AND scanned_at > NOW() - INTERVAL '8 hours'
            ORDER BY buy_score DESC, rsi ASC
            LIMIT 8
        """)
        top_buys = cur.fetchall()

    # live positions + account
    try:
        raw_positions = alpaca("GET", "/v2/positions")
        account = alpaca("GET", "/v2/account")
        portfolio_value = float(account["portfolio_value"])
        cash = float(account["cash"])
    except Exception:
        raw_positions, portfolio_value, cash = [], 0, 0

    held_symbols = {p["symbol"] for p in raw_positions}

    # --- compute state ---
    max_pos = int(params.get("max_open_positions", 10))
    stop_loss_pct = params.get("stop_loss_pct", 0.08)
    cash_reserve = float(profile["cash_reserve"]) if profile else 0

    n_positions = len(raw_positions)
    open_slots = max(0, max_pos - n_positions)
    investable_cash = max(0, cash - cash_reserve)
    cash_pct = round(cash / portfolio_value * 100, 1) if portfolio_value else 0

    total_scanned = breadth["total"] or 1
    overbought_pct = round(breadth["overbought_n"] / total_scanned * 100)
    oversold_pct   = round(breadth["oversold_n"]   / total_scanned * 100)
    neutral_pct    = 100 - overbought_pct - oversold_pct

    market_extended = overbought_pct > 50
    market_oversold = oversold_pct > 30

    # stop-loss alerts
    stop_alerts = []
    for p in raw_positions:
        plpc = float(p.get("unrealized_plpc", 0))
        if plpc <= -stop_loss_pct:
            stop_alerts.append({
                "symbol": p["symbol"],
                "plpc": round(plpc * 100, 2),
            })

    # --- stance ---
    if stop_alerts:
        stance = "warning"
    elif open_slots == 0 and market_extended:
        stance = "hold"
    elif open_slots > 0 and not market_extended and top_buys:
        stance = "bullish"
    else:
        stance = "cautious"

    # --- build bullets ---
    bullets = []

    # slot / capital situation
    if open_slots == 0:
        bullets.append({"type": "info", "text": f"At position limit ({n_positions}/{max_pos}). No new buys until a position closes."})
    else:
        per_trade = investable_cash * params.get("trade_allocation_pct", 0.05)
        bullets.append({"type": "info", "text": f"{open_slots} slot{'s' if open_slots != 1 else ''} open — ~${per_trade:,.0f} available per trade (after ${cash_reserve:,.0f} reserve)"})

    # cash utilization
    if cash_pct > 60 and open_slots == 0:
        bullets.append({"type": "caution", "text": f"{cash_pct}% cash sitting idle. Position limit ({max_pos}) is the binding constraint — consider raising max_open_positions."})
    elif cash_pct > 60:
        bullets.append({"type": "info", "text": f"{cash_pct}% cash available — plenty of dry powder."})

    # market breadth
    if market_extended:
        bullets.append({"type": "caution", "text": f"Market extended: {overbought_pct}% of scanned symbols are overbought. Defer new buys or be selective."})
    elif market_oversold:
        bullets.append({"type": "opportunity", "text": f"Broad oversold conditions: {oversold_pct}% of symbols showing buy signals. Good time to deploy capital."})
    else:
        bullets.append({"type": "info", "text": f"Market breadth neutral — {overbought_pct}% overbought, {oversold_pct}% oversold."})

    # top buy candidates — split into new positions vs adding to existing
    new_buys  = [r for r in top_buys if r["symbol"] not in held_symbols]
    add_buys  = [r for r in top_buys if r["symbol"] in held_symbols]

    if open_slots > 0 and (new_buys or add_buys) and not market_extended:
        if new_buys:
            bullets.append({"type": "opportunity",
                "text": f"New position candidates: " + ", ".join(f"{r['symbol']} (RSI {float(r['rsi']):.0f})" for r in new_buys[:3])})
        if add_buys:
            bullets.append({"type": "opportunity",
                "text": f"Consider adding to existing: " + ", ".join(f"{r['symbol']} (RSI {float(r['rsi']):.0f})" for r in add_buys[:3])})
    elif top_buys and market_extended:
        all_cands = new_buys[:2] + add_buys[:1]
        if all_cands:
            bullets.append({"type": "info",
                "text": "Watchlist for when market cools: " + ", ".join(f"{r['symbol']} (RSI {float(r['rsi']):.0f})" for r in all_cands)})

    # stop-loss alerts
    for a in stop_alerts:
        bullets.append({"type": "alert", "text": f"{a['symbol']} is down {a['plpc']}% — at or past stop-loss threshold ({round(stop_loss_pct*100)}%). Consider selling."})

    # headline
    headlines = {
        "warning": f"⚠️ Stop-loss alert on {', '.join(a['symbol'] for a in stop_alerts)}",
        "hold":    f"Hold — position limit reached and market is extended ({overbought_pct}% overbought)",
        "bullish": f"{open_slots} slot{'s' if open_slots != 1 else ''} open and market conditions favor buying",
        "cautious": "Capital available but conditions are mixed — proceed selectively",
    }

    # structured candidates for linked display
    candidates = [
        {
            "symbol": r["symbol"],
            "rsi": round(float(r["rsi"]), 1),
            "buy_score": int(r["buy_score"]),
            "is_held": r["symbol"] in held_symbols,
        }
        for r in top_buys[:6]
    ]

    return {
        "stance": stance,
        "headline": headlines[stance],
        "bullets": bullets,
        "candidates": candidates,
        "market_breadth": {"overbought_pct": overbought_pct, "oversold_pct": oversold_pct, "neutral_pct": neutral_pct},
        "open_slots": open_slots,
        "max_positions": max_pos,
        "n_positions": n_positions,
        "cash_pct": cash_pct,
    }

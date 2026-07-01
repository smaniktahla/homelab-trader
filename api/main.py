from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional
import psycopg2, psycopg2.extras, os, requests as http
from datetime import datetime, timezone

app = FastAPI(title="invest-api")
templates = Jinja2Templates(directory="/app/templates")
DB_DSN = os.environ["DATABASE_URL"]
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET", "")
ALPACA_BASE = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

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
            SELECT DISTINCT ON (symbol, signal_type) symbol, signal_type, score, rationale, generated_at
            FROM signals
            ORDER BY symbol, signal_type, generated_at DESC
        """)
        rows = cur.fetchall()
        # pivot to {symbol: {buy: {...}, sell: {...}}}
        result = {}
        for row in rows:
            sym = row["symbol"]
            side = "buy" if "buy" in row["signal_type"] else "sell"
            if sym not in result:
                result[sym] = {}
            result[sym][side] = {
                "score": float(row["score"]) if row["score"] is not None else 0,
                "rationale": row["rationale"],
                "generated_at": row["generated_at"].isoformat() if row["generated_at"] else None,
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

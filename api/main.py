from fastapi import FastAPI, Request, HTTPException, Depends, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional
import psycopg2, psycopg2.extras, os, requests as http, secrets, time, bisect, json, logging
from datetime import datetime, timezone

from signals import compute_signals, compute_bollinger, fetch_alpaca_portfolio, load_params, load_sector_map
from signal_components import weighted_component_score
import lifecycle_performance
import rule_adherence
from sector_regime import load_latest_sector_regime
from security_regime import load_latest_security_regime
import risk_engine
import trading_permission
import circuit_breaker
import proposal_ranking

log = logging.getLogger(__name__)

DB_DSN = os.environ["DATABASE_URL"]
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET", "")
ALPACA_BASE = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

_security = HTTPBasic(auto_error=False)
_AUTH_USER = os.environ.get("INVEST_USER", "invest")
_AUTH_PASS = os.environ.get("INVEST_PASS", "")
_AGENT_API_KEY = os.environ.get("INVEST_AGENT_API_KEY", "")

if not _AUTH_PASS:
    raise RuntimeError(
        "INVEST_PASS is not set. The API will not start without authentication "
        "because it can place real trades. Set INVEST_PASS in your .env file."
    )

def _check_auth(request: Request, creds: Optional[HTTPBasicCredentials] = Depends(_security)):
    # Service-to-service read access: a valid X-API-Key header grants
    # GET-only access. Scoped by HTTP method (not by path) so a leaked or
    # misused agent key can never place a trade or change settings, no
    # matter which endpoint it's pointed at - every mutating route in this
    # API is POST/PATCH, every read is GET.
    if request.method == "GET" and _AGENT_API_KEY:
        agent_key = request.headers.get("X-API-Key", "")
        if agent_key and secrets.compare_digest(agent_key.encode(), _AGENT_API_KEY.encode()):
            return
    ok = (
        creds is not None and
        secrets.compare_digest(creds.username.encode(), _AUTH_USER.encode()) and
        secrets.compare_digest(creds.password.encode(), _AUTH_PASS.encode())
    )
    if not ok:
        raise HTTPException(status_code=401, detail="Unauthorized",
                            headers={"WWW-Authenticate": "Basic realm=invest"})

app = FastAPI(title="invest-api", dependencies=[Depends(_check_auth)])
templates = Jinja2Templates(directory="/app/templates")

def db():
    return psycopg2.connect(DB_DSN, cursor_factory=psycopg2.extras.RealDictCursor)

def alpaca(method, path, **kwargs):
    r = http.request(method, f"{ALPACA_BASE}{path}", headers=ALPACA_HEADERS, timeout=10, **kwargs)
    if not r.ok:
        try:
            msg = r.json().get("message", r.text)
        except ValueError:
            msg = r.text
        raise HTTPException(r.status_code, f"Alpaca rejected the request: {msg}")
    return r.json()

def _next_open_status(order):
    """Whether this order is genuinely deferred to the next market session,
    vs. just not synchronously reflected as 'filled' yet in the immediate
    POST response. A market order submitted while the market is open
    routinely comes back status='accepted'/'new' for a moment before
    Alpaca fills it a second or two later (see _reconcile_fill) -- that is
    NOT the same thing as an order queued because the market is closed,
    but the old check (order['status'] not in ('filled','partially_filled'))
    treated both cases identically, so a same-day market-hours sell could
    show 'will execute at next open' even though it was about to fill
    normally. Ground truth is the actual market clock, not the order's
    transient status."""
    if order["status"] in ("filled", "partially_filled"):
        return False, None
    try:
        clock = alpaca("GET", "/v2/clock")
    except HTTPException:
        return False, None
    if clock.get("is_open"):
        return False, None
    return True, clock.get("next_open")

_YF_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; invest-agent/1.0)"}

def _fetch_prices_yf(symbol, yf_range="1y"):
    r = http.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
                 params={"interval": "1d", "range": yf_range}, headers=_YF_HEADERS, timeout=15)
    r.raise_for_status()
    result = r.json()["chart"]["result"][0]
    timestamps = result["timestamp"]
    ohlcv = result["indicators"]["quote"][0]
    return [{
        "ts": datetime.fromtimestamp(ts, tz=timezone.utc),
        "open": ohlcv["open"][i], "high": ohlcv["high"][i],
        "low": ohlcv["low"][i], "close": ohlcv["close"][i], "volume": ohlcv["volume"][i],
    } for i, ts in enumerate(timestamps)]

def _backfill_thin_history(cur, symbol):
    """Symbol detail pages are reachable for any symbol that's ever been
    scanned (leaderboard, signal history), not just ones currently on the
    watchlist — the recurring ingest job only ever covers watchlist/held/
    proposed symbols, so a demoted symbol with a partial history was
    otherwise stuck showing the same handful of days forever, regardless of
    the range selected. Backfill on demand here instead: any symbol with
    fewer than ~3 months of rows gets a synchronous 1y fetch before we read
    price_history, so it self-heals the first time its page is viewed."""
    cur.execute("SELECT COUNT(*) AS n FROM price_history WHERE symbol=%s", (symbol,))
    if cur.fetchone()["n"] >= 65:
        return
    try:
        rows = _fetch_prices_yf(symbol, "1y")
    except Exception:
        return  # best-effort — just show whatever's already in the DB
    for row in rows:
        if row["open"] is None or row["close"] is None:
            continue
        cur.execute("""
            INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol, ts) DO NOTHING
        """, (symbol, row["ts"], row["open"], row["high"], row["low"], row["close"], row["volume"]))


# ── Data endpoints ────────────────────────────────────────────────────────────

@app.get("/api/watchlist")
def get_watchlist():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT symbol, name, added_at FROM watchlist ORDER BY symbol")
        return cur.fetchall()

@app.get("/api/prices/{symbol}")
def get_prices(symbol: str, days: int = 30, include_bb: bool = False):
    symbol = symbol.upper()
    with db() as conn, conn.cursor() as cur:
        _backfill_thin_history(cur, symbol)
        conn.commit()

        bb_period, bb_std = 20, 2.0
        if include_bb:
            cur.execute("SELECT key, value FROM signal_params WHERE key IN ('bb_period', 'bb_std')")
            p = {r["key"]: float(r["value"]) for r in cur.fetchall()}
            bb_period, bb_std = int(p.get("bb_period", 20)), p.get("bb_std", 2.0)

        # Fetch extra lookback so the FIRST displayed day still has a full
        # bb_period of trailing closes to compute against -- otherwise the
        # default 2-week view (14 days, period 20) would show no bands at
        # all for its entire range.
        lookback = days + bb_period + 5 if include_bb else days
        cur.execute("""
            SELECT ts, open, high, low, close, volume
            FROM price_history WHERE symbol = %s
            ORDER BY ts DESC LIMIT %s
        """, (symbol, lookback))
        rows = cur.fetchall()
        rows.reverse()

        if include_bb:
            closes = [float(r["close"]) for r in rows]
            for i, r in enumerate(rows):
                bb_upper, bb_middle, bb_lower, _ = compute_bollinger(closes[:i + 1], bb_period, bb_std)
                r["bb_upper"], r["bb_middle"], r["bb_lower"] = bb_upper, bb_middle, bb_lower
            rows = rows[-days:]

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
def get_signals(limit: int = 50, symbol: Optional[str] = None):
    """symbol is optional and filters server-side before LIMIT is applied --
    without it, a single quiet symbol's older signals fall outside whatever
    window `limit` happens to cover globally (500+ symbols scanning hourly
    means the unfiltered latest-100 window can span just a few hours), so
    the symbol page's "Recent Signals" card would show blank even when real
    history exists. See api/templates/symbol.html's loadSignals()."""
    with db() as conn, conn.cursor() as cur:
        if symbol:
            cur.execute("""
                SELECT symbol, signal_type, score, rationale, generated_at, acted_on
                FROM signals WHERE symbol = %s ORDER BY generated_at DESC LIMIT %s
            """, (symbol.upper(), limit))
        else:
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

@app.get("/api/signal-outcomes")
def get_signal_outcomes(symbol: Optional[str] = None, limit: int = 200):
    with db() as conn, conn.cursor() as cur:
        if symbol:
            cur.execute("""
                SELECT * FROM signal_outcomes WHERE symbol=%s
                ORDER BY generated_at DESC LIMIT %s
            """, (symbol.upper(), limit))
        else:
            cur.execute("""
                SELECT * FROM signal_outcomes ORDER BY generated_at DESC LIMIT %s
            """, (limit,))
        return cur.fetchall()

COMPONENT_NAMES = ("technical", "fundamental", "earnings", "news", "options", "macro_fit")

def _component_status(value, weight):
    """live: computed and carries non-zero weight in the actual decision.
    shadow: computed but weight is currently zero (not yet activated).
    unavailable: not yet computed for this symbol/feature_version.
    (stale is a defined status but unreachable in PR #1 — no source has
    an independent freshness threshold yet; every component here shares
    signal_outcomes' own as_of.)"""
    if value is None:
        return "unavailable"
    return "live" if weight > 0 else "shadow"

def _symbol_features_side(cur, symbol, side):
    # Secondary sort on feature_version is not cosmetic: symbol_features is
    # explicitly designed to let a future repair/backfill process insert a
    # new feature_version row at a historical as_of it's re-deriving (see
    # feature_store.py's docstring). Without a tiebreaker, two rows sharing
    # the same as_of would resolve to whichever Postgres happens to return
    # first -- not reliably the newer version -- the moment that scenario
    # actually occurs. feature_version is a plain string ("v1", "v2", ...);
    # sorting it descending assumes lexicographic order tracks version
    # order, true for this naming scheme but worth remembering if that
    # scheme ever changes.
    cur.execute("""
        SELECT * FROM symbol_features
        WHERE symbol=%s AND side=%s
        ORDER BY as_of DESC, feature_version DESC, id DESC LIMIT 1
    """, (symbol.upper(), side))
    row = cur.fetchone()
    if not row:
        return None
    # psycopg2 returns NUMERIC as decimal.Decimal; component_weights (jsonb)
    # decodes as float. weighted_component_score does float arithmetic, so
    # normalize scores to float here rather than teach the shared combiner
    # about the DB driver's type mapping.
    weights = {k: float(v) for k, v in (row["component_weights"] or {}).items()}
    scores = {
        name: (float(row[f"{name}_score"]) if row[f"{name}_score"] is not None else None)
        for name in COMPONENT_NAMES
    }
    composite = weighted_component_score(scores, weights)
    return {
        "as_of": row["as_of"].isoformat() if row["as_of"] else None,
        "feature_version": row["feature_version"],
        "model_version": row["model_version"],
        "data_confidence": float(row["data_confidence"]) if row["data_confidence"] is not None else None,
        "composite_score": composite,
        "component_weights": weights,
        "components": {
            name: {
                "value": float(scores[name]) if scores[name] is not None else None,
                "status": _component_status(scores[name], weights.get(name, 0)),
            }
            for name in COMPONENT_NAMES
        },
    }

@app.get("/api/symbol-features/{symbol}")
def get_symbol_features(symbol: str):
    """Latest technical/fundamental/earnings/news/options/macro_fit component
    breakdown per side. composite_score is always computed through
    weighted_component_score() — the same function scoring.py will use once
    later PRs add real weights — rather than a second hardcoded
    "== technical_score" shortcut, so this endpoint exercises the actual
    combination abstraction even though PR #1's weights make the result
    identical to technical_score today."""
    with db() as conn, conn.cursor() as cur:
        buy = _symbol_features_side(cur, symbol, "buy")
        sell = _symbol_features_side(cur, symbol, "sell")
    return {"symbol": symbol.upper(), "buy": buy, "sell": sell}

@app.get("/api/reviews")
def get_strategy_reviews(limit: int = 20):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM strategy_review_proposals ORDER BY created_at DESC LIMIT %s
        """, (limit,))
        return cur.fetchall()

def _realized_pnl_by_trade_id(cur):
    """Running average-cost basis per symbol (matches positions.avg_entry_price
    convention) over the *entire* filled trade history, so a sell's realized
    P&L is correct even when its matching buy falls outside a limited window.
    Returns {trade_id: (realized_pnl, realized_pnl_pct)} for sell trades only."""
    cur.execute("""
        SELECT id, symbol, side, qty, price FROM trades
        WHERE status='filled' AND price IS NOT NULL
        ORDER BY traded_at ASC, id ASC
    """)
    basis = {}  # symbol -> {qty, total_cost}
    pnl = {}
    for t in cur.fetchall():
        pos = basis.setdefault(t["symbol"], {"qty": 0.0, "total_cost": 0.0})
        qty, price = float(t["qty"]), float(t["price"])
        if t["side"] == "buy":
            pos["qty"] += qty
            pos["total_cost"] += qty * price
        elif t["side"] == "sell" and pos["qty"] > 0:
            avg_cost = pos["total_cost"] / pos["qty"]
            sell_qty = min(qty, pos["qty"])  # guard against oversell/data gaps
            realized = sell_qty * (price - avg_cost)
            pnl[t["id"]] = (round(realized, 2), round((price - avg_cost) / avg_cost * 100, 2), sell_qty)
            pos["qty"] -= sell_qty
            pos["total_cost"] -= sell_qty * avg_cost
    return pnl


def _lifecycle_dates_by_trade_id(cur):
    """opened_at/closed_at of the position_lifecycles row each trade
    belongs to, keyed by trade_id (covers both entry and exit trades) --
    lets Trade History show a sell's paired entry date without
    re-deriving FIFO matching. match_lifecycles() (shared/position_lifecycles.py)
    already did that work, materialized every ingest cycle by
    ingest/build_position_lifecycles.py; this is a pure read-side join via
    position_trades, same pattern _all_closed_lifecycles/_open_lifecycle
    already use. DISTINCT ON ... ORDER BY pl.id ASC picks one (the
    earliest) lifecycle per trade_id for display purposes -- a trade
    belonging to more than one lifecycle is a pyramiding/partial-fill edge
    case the UI doesn't need to enumerate exhaustively.
    Returns {trade_id: {"opened_at": dt, "closed_at": dt|None, "status": str}}."""
    cur.execute("""
        SELECT DISTINCT ON (pt.trade_id)
               pt.trade_id, pl.opened_at, pl.closed_at, pl.status
        FROM position_trades pt
        JOIN position_lifecycles pl ON pl.id = pt.position_lifecycle_id
        ORDER BY pt.trade_id, pl.id ASC
    """)
    return {r["trade_id"]: {"opened_at": r["opened_at"], "closed_at": r["closed_at"], "status": r["status"]}
            for r in cur.fetchall()}


@app.get("/api/trades")
def get_trades(limit: int = 200, start: Optional[str] = None, end: Optional[str] = None):
    """start/end (ISO datetimes) filter to a calendar period -- end is
    exclusive so callers can pass [periodStart, nextPeriodStart) with no
    off-by-one adjustment. limit stays an independent safety cap alongside
    the date window, not a replacement for it."""
    with db() as conn, conn.cursor() as cur:
        pnl_by_id = _realized_pnl_by_trade_id(cur)
        lifecycle_by_id = _lifecycle_dates_by_trade_id(cur)
        if start or end:
            cur.execute("""
                SELECT * FROM trades
                WHERE (%(start)s IS NULL OR traded_at >= %(start)s)
                  AND (%(end)s IS NULL OR traded_at < %(end)s)
                ORDER BY traded_at DESC LIMIT %(limit)s
            """, {"start": start, "end": end, "limit": limit})
        else:
            cur.execute("""
                SELECT * FROM trades ORDER BY traded_at DESC LIMIT %s
            """, (limit,))
        trades = cur.fetchall()
        for t in trades:
            realized = pnl_by_id.get(t["id"])
            t["realized_pnl"] = realized[0] if realized else None
            t["realized_pnl_pct"] = realized[1] if realized else None
            # Cost basis is only known for part of the sale (e.g. shares held
            # before trade logging started) when this is less than t["qty"].
            t["realized_qty"] = realized[2] if realized else None
            lc = lifecycle_by_id.get(t["id"])
            t["lifecycle_opened_at"] = lc["opened_at"] if lc else None
            t["lifecycle_closed_at"] = lc["closed_at"] if lc else None
            t["lifecycle_status"] = lc["status"] if lc else None
        return trades

_PORTFOLIO_HISTORY_RANGE_DAYS = {"1m": 30, "3m": 90, "6m": 180, "1y": 365, "3y": 1095, "5y": 1825}

@app.get("/api/portfolio-history")
def get_portfolio_history(range: str = "1m"):
    """Portfolio value over time + trade markers, for the dashboard chart.
    Ranges beyond 90 days are downsampled to one (last) snapshot per day —
    hourly resolution isn't useful once the window spans months."""
    days = _PORTFOLIO_HISTORY_RANGE_DAYS.get(range.lower(), 30)
    # Subtracting cumulative modeled trade cost as of each point in time (not
    # just the current total) so the chart reflects cost-adjusted equity
    # throughout, matching /api/account's treatment of the current values.
    with db() as conn, conn.cursor() as cur:
        if days > 90:
            cur.execute("""
                SELECT snapshot_at,
                       portfolio_value - COALESCE(
                           (SELECT SUM(t.cost) FROM trades t WHERE t.traded_at <= snapshot_at), 0
                       ) AS portfolio_value
                FROM (
                    SELECT DISTINCT ON (date_trunc('day', snapshot_at))
                           snapshot_at, portfolio_value
                    FROM portfolio_snapshots
                    WHERE snapshot_at >= NOW() - INTERVAL '%s days'
                    ORDER BY date_trunc('day', snapshot_at), snapshot_at DESC
                ) daily
                ORDER BY snapshot_at
            """, (days,))
        else:
            cur.execute("""
                SELECT snapshot_at,
                       portfolio_value - COALESCE(
                           (SELECT SUM(t.cost) FROM trades t WHERE t.traded_at <= snapshot_at), 0
                       ) AS portfolio_value
                FROM portfolio_snapshots
                WHERE snapshot_at >= NOW() - INTERVAL '%s days'
                ORDER BY snapshot_at
            """, (days,))
        snapshots = cur.fetchall()

        cur.execute("""
            SELECT t.symbol, t.side, t.qty, t.price, t.traded_at,
                   (SELECT ps.portfolio_value FROM portfolio_snapshots ps
                    WHERE ps.snapshot_at <= t.traded_at
                    ORDER BY ps.snapshot_at DESC LIMIT 1)
                   - COALESCE(
                       (SELECT SUM(t2.cost) FROM trades t2 WHERE t2.traded_at <= t.traded_at), 0
                     ) AS portfolio_value
            FROM trades t
            WHERE t.traded_at >= NOW() - INTERVAL '%s days'
            ORDER BY t.traded_at
        """, (days,))
        trades = cur.fetchall()

        # SPY benchmark: "if this same starting capital had just been put
        # into SPY at the start of the visible range." Re-anchored per range
        # (not to account inception), so each button answers "how would SPY
        # have done over just this window" — matching the portfolio line it
        # sits next to.
        #
        # Two assumptions, both settings-controlled (default ON, so the
        # comparison is apples-to-apples out of the box rather than quietly
        # favoring SPY):
        #   spy_include_dividends -- use adjclose (dividend/split-adjusted)
        #     instead of raw close, so this is a true total-return
        #     benchmark, not price-only.
        #   spy_cost_adjust -- subtract a single one-time modeled trade cost
        #     from the whole curve, matching how the portfolio line is
        #     already cost-adjusted. Only one cost (not per-trade like the
        #     actively-traded portfolio), since this models a single
        #     lump-sum buy-and-hold, not repeated trading.
        settings = get_all_settings()
        cost_adjust = settings.get("spy_cost_adjust", "true") == "true"
        include_dividends = settings.get("spy_include_dividends", "true") == "true"

        spy_flat_cost = 0.0
        if cost_adjust:
            cur.execute("SELECT value FROM signal_params WHERE key='trade_cost_flat'")
            row = cur.fetchone()
            spy_flat_cost = float(row["value"]) if row else 0.0

        cur.execute("""
            SELECT DATE(ts) AS d, close, adjclose FROM price_history
            WHERE symbol='SPY' AND ts >= NOW() - INTERVAL '%s days'
            ORDER BY ts ASC
        """, (days,))
        spy_by_date = {}
        for r in cur.fetchall():
            price = float(r["adjclose"]) if (include_dividends and r["adjclose"] is not None) else float(r["close"])
            spy_by_date[r["d"]] = price

    spy_dates_sorted = sorted(spy_by_date)

    def nearest_spy_close(d):
        # Clamp to the earliest available SPY date rather than returning None
        # when `d` precedes all SPY data -- a range's anchor date (e.g. the
        # 1M window's first portfolio snapshot) can fall a day or two before
        # SPY's own earliest row without there being any real gap to report,
        # and a None first_spy_close would otherwise null out spy_value for
        # EVERY point in the series, not just the anchor.
        if not spy_dates_sorted:
            return None
        idx = bisect.bisect_right(spy_dates_sorted, d) - 1
        if idx < 0:
            idx = 0
        return spy_by_date[spy_dates_sorted[idx]]

    first_portfolio_value = float(snapshots[0]["portfolio_value"]) if snapshots else None
    first_spy_close = nearest_spy_close(snapshots[0]["snapshot_at"].date()) if snapshots else None

    for s in snapshots:
        spy_close = nearest_spy_close(s["snapshot_at"].date())
        s["spy_value"] = (
            first_portfolio_value * (spy_close / first_spy_close) - spy_flat_cost
            if spy_close and first_spy_close else None
        )

    return {
        "range": range.lower(), "range_days": days, "snapshots": snapshots, "trades": trades,
        "spy_assumptions": {"cost_adjusted": cost_adjust, "dividends_included": include_dividends},
    }

@app.get("/api/positions")
def get_positions():
    positions = alpaca("GET", "/v2/positions")
    symbols = [p["symbol"] for p in positions]
    names = {}
    if symbols:
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT symbol, name FROM universe WHERE symbol = ANY(%s)", (symbols,))
            names = {r["symbol"]: r["name"] for r in cur.fetchall()}
    # Alpaca's own open orders -- status=open covers everything non-terminal
    # (new/accepted/pending_new/partially_filled/replaced). Surfaced per
    # position so the UI can show "order pending" instead of silently
    # letting a duplicate proposal fail against qty_available with a
    # confusing error (see 2026-07-29 HAS incident: a thesis_complete sell
    # sat unfilled for 12+ hours after-hours-queued, and nothing in the UI
    # showed that until approving a second, duplicate proposal errored out).
    try:
        open_orders = alpaca("GET", "/v2/orders", params={"status": "open"})
    except Exception:
        open_orders = []
    pending_by_symbol = {}
    for o in open_orders:
        pending_by_symbol.setdefault(o["symbol"], []).append({
            "side": o["side"], "qty": float(o["qty"]), "status": o["status"],
            "submitted_at": o.get("submitted_at"),
        })
    return [{
        "symbol": p["symbol"],
        "company_name": names.get(p["symbol"]) or None,
        "qty": float(p["qty"]),
        "avg_entry_price": float(p["avg_entry_price"]),
        "current_price": float(p["current_price"]),
        "market_value": float(p["market_value"]),
        "cost_basis": float(p["cost_basis"]),
        "unrealized_pl": float(p["unrealized_pl"]),
        "unrealized_plpc": round(float(p["unrealized_plpc"]) * 100, 2),
        "side": p["side"],
        "pending_orders": pending_by_symbol.get(p["symbol"], []),
    } for p in positions]

def _all_closed_lifecycles(cur):
    """Every CLOSED position_lifecycles row, across ALL symbols, oldest
    opened_at first, each joined to position_trades to recover its
    entry_trade_ids/exit_trade_ids (grouped via array_agg ... FILTER, since
    a lifecycle can have more than one entry and more than one exit trade —
    pyramiding and partial exits). Already materialized incrementally by
    ingest/build_position_lifecycles.py every ingest cycle, unlike the old
    round_trips.py reconstruction this replaces — no full-ledger walk
    happens at request time anymore."""
    cur.execute("""
        SELECT pl.*,
               COALESCE(array_agg(pt.trade_id) FILTER (WHERE pt.role='entry'), '{}') AS entry_trade_ids,
               COALESCE(array_agg(pt.trade_id) FILTER (WHERE pt.role='exit'), '{}') AS exit_trade_ids
        FROM position_lifecycles pl
        LEFT JOIN position_trades pt ON pt.position_lifecycle_id = pl.id
        WHERE pl.status = 'closed'
        GROUP BY pl.id
        ORDER BY pl.opened_at ASC
    """)
    rows = cur.fetchall()
    lifecycles_by_symbol = {}
    for row in rows:
        lifecycles_by_symbol.setdefault(row["symbol"], []).append(row)
    return lifecycles_by_symbol

def _open_lifecycle(cur, symbol: str):
    """This symbol's single open position_lifecycles row (status='open'),
    or None — a symbol can have at most one open lifecycle by construction
    (match_lifecycles() only starts a new one once the previous fully
    closes). Joined to position_trades the same way _all_closed_lifecycles
    is, for consistency, even though entry/exit trade ids on the open
    lifecycle aren't currently rendered anywhere."""
    cur.execute("""
        SELECT pl.*,
               COALESCE(array_agg(pt.trade_id) FILTER (WHERE pt.role='entry'), '{}') AS entry_trade_ids,
               COALESCE(array_agg(pt.trade_id) FILTER (WHERE pt.role='exit'), '{}') AS exit_trade_ids
        FROM position_lifecycles pl
        LEFT JOIN position_trades pt ON pt.position_lifecycle_id = pl.id
        WHERE pl.symbol = %s AND pl.status = 'open'
        GROUP BY pl.id
    """, (symbol,))
    return cur.fetchone()

def _symbol_unmatched_sell_qty(cur, symbol: str):
    """From position_lifecycle_symbol_status (see ingest/schema.sql) —
    derived by the same match_lifecycles() run that builds
    position_lifecycles/position_trades, persisted specifically so this
    doesn't require re-walking the full ledger per request. 0.0 if this
    symbol has no lifecycle data built for it at all yet."""
    cur.execute("SELECT unmatched_sell_qty FROM position_lifecycle_symbol_status WHERE symbol = %s", (symbol,))
    row = cur.fetchone()
    return float(row["unmatched_sell_qty"]) if row else 0.0

def _live_position_or_none(symbol: str):
    try:
        return alpaca("GET", f"/v2/positions/{symbol}")
    except HTTPException as e:
        if e.status_code == 404:
            return None
        raise

@app.get("/api/symbol-performance/{symbol}")
def get_symbol_performance(symbol: str):
    """Symbol Performance Summary: realized/unrealized/total P&L, completed
    lifecycles, win rate, avg/median return, best/worst trade, capital
    deployed, avg holding period, and this symbol's contribution to
    portfolio gross gains and net P&L.

    Reporting-only — reads position_lifecycles/positions that already
    exist, computes nothing that feeds back into proposal, sizing, or
    execution logic.

    Uses shared/lifecycle_performance.py against the materialized
    position_lifecycles table (true FIFO lot matching, methodology
    explicitly labeled in the response) — Platform Improvements PR A.1,
    replacing the prior shared/round_trips.py average-cost reconstruction.
    Numbers here can legitimately differ from what this endpoint used to
    return for any symbol pyramided into more than once before fully
    exiting; see lifecycle_performance.py's own module docstring.

    Completed-lifecycle statistics come exclusively from this symbol's
    closed position_lifecycles rows — the currently open position (if
    any), sourced live from Alpaca rather than re-derived locally, only
    ever contributes to the separate unrealized/total P&L fields. See
    lifecycle_performance.symbol_summary's own docstring for why live
    Alpaca data is preferred over a second local reconstruction of "what's
    currently held."
    """
    symbol = symbol.upper()
    with db() as conn, conn.cursor() as cur:
        lifecycles_by_symbol = _all_closed_lifecycles(cur)
        open_lifecycle = _open_lifecycle(cur, symbol)
        unmatched_sell_qty = _symbol_unmatched_sell_qty(cur, symbol)
    totals = lifecycle_performance.portfolio_totals(lifecycles_by_symbol)
    live_position = _live_position_or_none(symbol)
    return lifecycle_performance.symbol_summary(
        symbol, lifecycles_by_symbol, totals, unmatched_sell_qty,
        open_lifecycle=open_lifecycle, live_position=live_position,
    )

@app.get("/api/symbol-performance/{symbol}/round-trips")
def get_symbol_round_trips(symbol: str):
    """Full closed-lifecycle history for the table below the summary —
    oldest-first, plus the open lifecycle (if any) called out separately
    so the UI can visually distinguish it rather than mixing it into the
    same rows."""
    symbol = symbol.upper()
    with db() as conn, conn.cursor() as cur:
        lifecycles_by_symbol = _all_closed_lifecycles(cur)
        open_lifecycle = _open_lifecycle(cur, symbol)
    live_position = _live_position_or_none(symbol)
    return lifecycle_performance.round_trips_detail(
        symbol, lifecycles_by_symbol, open_lifecycle=open_lifecycle, live_position=live_position,
    )

def _total_trade_cost(cur):
    cur.execute("SELECT COALESCE(SUM(cost), 0) AS total FROM trades")
    return float(cur.fetchone()["total"])

def _current_trade_cost_flat(cur):
    cur.execute("SELECT value FROM signal_params WHERE key='trade_cost_flat'")
    row = cur.fetchone()
    return float(row["value"]) if row else 0.0

def _resolve_thesis_id(cur, proposal_id: Optional[int] = None):
    """trades.thesis_id has been NOT NULL since migration 001 — every INSERT
    into trades must set it. Proposal-linked trades use the proposal's own
    thesis_id; a bare manual trade (no proposal_id) has no natural thesis to
    attribute to, so it defaults to mean_reversion, today's only active one."""
    if proposal_id:
        cur.execute("SELECT thesis_id FROM trade_proposals WHERE id=%s", (proposal_id,))
        row = cur.fetchone()
        if row and row["thesis_id"]:
            return row["thesis_id"]
    cur.execute("SELECT id FROM theses WHERE slug='mean_reversion'")
    row = cur.fetchone()
    return row["id"] if row else None


def _resolve_trade_thesis_id(cur, proposal_id: Optional[int] = None):
    """trades.trade_thesis_id (PR 9, Hypothesis-Driven Trading Architecture
    epic) -- copied immutably from trade_proposals.trade_thesis_id at fill
    time, same pattern as _resolve_thesis_id above and trades.
    initial_stop_price. Unlike thesis_id there is no mean_reversion-style
    default: a manual trade with no linked proposal, or a proposal from
    before PR 4 / with instantiation disabled, simply has no trade_thesis
    to attribute to -- stays NULL, per §2a's best-effort-hint contract."""
    if proposal_id:
        cur.execute("SELECT trade_thesis_id FROM trade_proposals WHERE id=%s", (proposal_id,))
        row = cur.fetchone()
        if row:
            return row["trade_thesis_id"]
    return None

def _vs_spy_since_inception(cur, current_portfolio_value):
    """Cumulative portfolio return vs. a SPY buy-and-hold of the same
    starting capital, anchored to the account's first portfolio_snapshots
    row rather than whatever range button the chart happens to have
    selected -- this is meant to be a stable top-level stat, not something
    that changes when the user clicks 1M vs 1Y. Same re-basing/
    cost-adjustment/dividend convention as /api/portfolio-history's
    per-range SPY line, just a fixed since-inception window instead of a
    re-anchored one. Returns None (never a fabricated 0%) if there's no
    snapshot or no SPY price history to compare against yet."""
    cur.execute("""
        SELECT snapshot_at, portfolio_value - COALESCE(
            (SELECT SUM(t.cost) FROM trades t WHERE t.traded_at <= snapshot_at), 0
        ) AS portfolio_value
        FROM portfolio_snapshots ORDER BY snapshot_at ASC LIMIT 1
    """)
    first = cur.fetchone()
    if not first or not first["portfolio_value"]:
        return None
    first_value = float(first["portfolio_value"])
    if first_value <= 0:
        return None

    settings = get_all_settings()
    include_dividends = settings.get("spy_include_dividends", "true") == "true"

    cur.execute("""
        SELECT close, adjclose FROM price_history
        WHERE symbol='SPY' AND ts >= %s ORDER BY ts ASC
    """, (first["snapshot_at"],))
    rows = cur.fetchall()
    if not rows:
        return None

    def px(r):
        return float(r["adjclose"]) if (include_dividends and r["adjclose"] is not None) else float(r["close"])

    first_spy, last_spy = px(rows[0]), px(rows[-1])
    if not first_spy:
        return None

    portfolio_return_pct = (current_portfolio_value - first_value) / first_value * 100
    spy_return_pct = (last_spy - first_spy) / first_spy * 100
    return {
        "since": first["snapshot_at"].isoformat(),
        "portfolio_return_pct": round(portfolio_return_pct, 2),
        "spy_return_pct": round(spy_return_pct, 2),
        "alpha_pct": round(portfolio_return_pct - spy_return_pct, 2),
    }


@app.get("/api/account")
def get_account():
    a = alpaca("GET", "/v2/account")
    portfolio_value = float(a["portfolio_value"])
    with db() as conn, conn.cursor() as cur:
        total_cost = _total_trade_cost(cur)
        vs_spy = _vs_spy_since_inception(cur, portfolio_value - total_cost)
    # buying_power is left as Alpaca reports it — it reflects what can
    # actually be spent on the next order, which our modeled cost doesn't
    # change. cash/portfolio_value/equity are the reported performance
    # numbers, so those absorb the cost drag.
    return {
        "equity": float(a["equity"]) - total_cost,
        "cash": float(a["cash"]) - total_cost,
        "buying_power": float(a["buying_power"]),
        "portfolio_value": portfolio_value - total_cost,
        "total_trade_cost": total_cost,
        "vs_spy": vs_spy,
    }

def _build_proposal_price_map(cur, proposals, alpaca_positions, pending_orders):
    """Best-known price per symbol touched by proposal ranking: a
    proposal's own current_price (already the freshest price_history
    join) wins first, falls back to the symbol's live Alpaca position
    price, falls back to a price_history lookup for pending-order-only
    symbols with no other source. Symbols with genuinely no price
    anywhere are simply absent -- proposal_ranking.py treats that as
    unknown-cost and fails open, never guesses."""
    price_map = {}
    for p in proposals:
        if p.get("current_price") is not None:
            price_map[p["symbol"]] = float(p["current_price"])
    for pos in alpaca_positions:
        price_map.setdefault(pos["symbol"], float(pos["current_price"]))
    missing = {o["symbol"] for o in pending_orders} - set(price_map)
    if missing:
        cur.execute("""
            SELECT DISTINCT ON (symbol) symbol, close
            FROM price_history WHERE symbol = ANY(%s)
            ORDER BY symbol, ts DESC
        """, (list(missing),))
        for r in cur.fetchall():
            price_map[r["symbol"]] = float(r["close"])
    return price_map


def _load_latest_risk_decisions(cur, proposal_ids):
    """Latest context='proposal_generated' risk_decisions row per proposal
    -- a read-only signal for proposal_ranking.py, never recomputed or
    overridden here."""
    if not proposal_ids:
        return {}
    cur.execute("""
        SELECT DISTINCT ON (proposal_id) proposal_id, outcome, binding_constraint
        FROM risk_decisions
        WHERE proposal_id = ANY(%s) AND context = 'proposal_generated'
        ORDER BY proposal_id, id DESC
    """, (proposal_ids,))
    return {r["proposal_id"]: {"outcome": r["outcome"], "binding_constraint": r["binding_constraint"]}
            for r in cur.fetchall()}


@app.get("/api/proposals")
def get_proposals():
    # Fetch live buying power so the frontend can show a running cash balance
    buying_power = None
    portfolio_value = None
    try:
        acct = alpaca("GET", "/v2/account")
        buying_power = float(acct.get("buying_power", acct.get("cash", 0)))
        portfolio_value = float(acct.get("portfolio_value", 0)) or None
    except Exception:
        pass

    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT tp.*,
                   ph.close AS current_price
            FROM trade_proposals tp
            LEFT JOIN LATERAL (
                SELECT close FROM price_history
                WHERE symbol = tp.symbol
                ORDER BY ts DESC LIMIT 1
            ) ph ON TRUE
            WHERE tp.decision IS NULL
            ORDER BY tp.proposed_at DESC
        """)
        proposals = cur.fetchall()

        ranking_params = proposal_ranking.load_proposal_ranking_params(conn)
        ranking_params["sector_max_pct"] = load_params(conn).get("sector_max_pct")
        if proposals and ranking_params.get("proposal_ranking_enabled"):
            try:
                alpaca_positions = alpaca("GET", "/v2/positions")
            except Exception:
                alpaca_positions = []
            try:
                open_orders = alpaca("GET", "/v2/orders", params={"status": "open"})
            except Exception:
                open_orders = []
            positions = {p["symbol"]: {"market_value": float(p["market_value"])} for p in alpaca_positions}
            pending_orders = [{"symbol": o["symbol"], "side": o["side"], "qty": float(o["qty"]),
                                "status": o["status"]} for o in open_orders]

            all_symbols = {p["symbol"] for p in proposals} | set(positions) | {o["symbol"] for o in pending_orders}
            sector_map = load_sector_map(conn, all_symbols)
            price_map = _build_proposal_price_map(cur, proposals, alpaca_positions, pending_orders)
            proposal_ids = [p["id"] for p in proposals if p["side"] == "buy"]
            risk_decisions_by_proposal_id = _load_latest_risk_decisions(cur, proposal_ids)

            try:
                proposals = proposal_ranking.rank_proposals(
                    proposals, positions, pending_orders, sector_map,
                    buying_power, portfolio_value, price_map,
                    risk_decisions_by_proposal_id, ranking_params,
                )
            except Exception as e:
                log.warning(f"Proposal ranking failed, returning unranked proposals: {e}")

    return {"buying_power": buying_power, "proposals": proposals}

@app.post("/api/signals/generate")
def generate_recommendations():
    """On-demand re-run of the same signal engine invest-ingest runs hourly
    (compute_signals, shared/signals.py) — for the dashboard's "Generate
    Recommendations" button when the proposals list is empty. Reuses the
    exact production function rather than a second implementation, so this
    can't drift from what the hourly loop actually does. Scores against
    whatever's already in price_history/market_context (as fresh as the
    last hourly ingest cycle) rather than triggering a fresh Yahoo pull
    first — fetch_closes() inside compute_signals still hits Yahoo live per
    symbol, so scoring itself uses current prices; only the regime/earnings
    context could be up to an hour stale."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT symbol FROM watchlist")
        symbols = [r["symbol"] for r in cur.fetchall()]

    started_at = datetime.now(timezone.utc)
    # compute_signals (shared/signals.py) was only ever called from
    # ingest.py's plain psycopg2 connection (default tuple-row cursors) —
    # it relies on positional row[0]-style access throughout. db()'s
    # RealDictCursor breaks that (KeyError: 0), so this needs its own plain
    # connection rather than db()'s dict-cursor one, matching what
    # ingest.py's get_db() actually uses.
    with psycopg2.connect(DB_DSN) as conn:
        compute_signals(conn, symbols)

    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT tp.*, ph.close AS current_price
            FROM trade_proposals tp
            LEFT JOIN LATERAL (
                SELECT close FROM price_history
                WHERE symbol = tp.symbol
                ORDER BY ts DESC LIMIT 1
            ) ph ON TRUE
            WHERE tp.proposed_at >= %s
            ORDER BY tp.proposed_at DESC
        """, (started_at,))
        new_proposals = cur.fetchall()

    return {"new_proposals": new_proposals, "checked_symbols": len(symbols)}

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
            SELECT l.symbol, u.name AS company_name, l.close AS price, l.ts AS as_of,
                   ROUND(((l.close - p.close) / p.close * 100)::numeric, 2) AS day_pct
            FROM latest l
            LEFT JOIN prev p ON p.symbol = l.symbol
            LEFT JOIN universe u ON u.symbol = l.symbol
            ORDER BY l.symbol
        """)
        return cur.fetchall()


# ── Trade execution ───────────────────────────────────────────────────────────

def _reconcile_fill(trade_id: int, order_id: str, expected_qty: float,
                    max_attempts: int = 8, interval_s: float = 3.0):
    """Background task: poll Alpaca until the order fills, then update the trade record."""
    for _ in range(max_attempts):
        time.sleep(interval_s)
        try:
            order = alpaca("GET", f"/v2/orders/{order_id}")
            status = order.get("status", "")
            filled_price = float(order.get("filled_avg_price") or 0)
            filled_qty   = float(order.get("filled_qty")       or 0) or expected_qty
            if filled_price > 0:
                notional = filled_qty * filled_price
                with db() as conn, conn.cursor() as cur:
                    cur.execute("""
                        UPDATE trades
                        SET status=%s, qty=%s, price=%s, notional=%s
                        WHERE id=%s
                    """, (status, filled_qty, filled_price, notional, trade_id))
                    conn.commit()
                return  # done
        except Exception:
            pass  # try again next iteration


class TradeRequest(BaseModel):
    symbol: str
    side: str          # buy | sell
    qty: float
    notes: Optional[str] = None
    source: str = "manual"
    proposal_id: Optional[int] = None

def _record_rule_adherence(cur, context, trade_id, proposal_id, symbol, side, results):
    """Platform Improvements PR C. Persists check_gates()'s full result list
    verbatim -- purely advisory, never read by anything that blocks a trade
    or approval. Callers wrap this in try/except and commit it themselves;
    a failure here must never affect the trade/approval response."""
    cur.execute("""
        INSERT INTO rule_adherence_checks (context, trade_id, proposal_id, symbol, side, rule_results, any_violation)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (context, trade_id, proposal_id, symbol, side, json.dumps(results), rule_adherence.any_violation(results)))


def _clamp_to_risk_engine(conn, cur, context, proposal_id, symbol, requested_qty, price, planned_initial_stop_price):
    """BUY-side only. The one place a human-supplied or proposal-default
    qty gets clamped to what the risk engine actually approves -- see
    docs/risk-engine-architecture-reconciliation.md section C.1 for why
    this exists: prior to this, ProposalDecision.qty and TradeRequest.qty
    could both bypass every sizing constraint entirely, with only an
    advisory (never-blocking) rule_adherence check after the fact. This
    function is the binding check.

    Returns (approved_qty, decision_dict). Raises HTTPException(400) if
    approved_qty is 0 (risk engine rejects the trade outright) --
    deliberately NOT fail-open like rule_adherence, since this is the
    authoritative sizing decision, not an advisory record. A risk-engine
    evaluation failure (e.g. Alpaca unreachable) also raises 400 rather
    than silently falling back to the unclamped requested_qty -- silently
    skipping the one binding check this function exists to provide would
    defeat its entire purpose."""
    try:
        cash, portfolio_value, positions = fetch_alpaca_portfolio()
        p = load_params(conn)

        # Risk Engine PR 3: account-level trading permission is checked
        # first, before any per-trade constraint math -- a paused account
        # (drawdown or loss-streak) blocks every new BUY regardless of how
        # much room this specific trade would otherwise have.
        permission = trading_permission.evaluate_trading_permission(conn, portfolio_value, p)
        if not permission["new_entries_allowed"]:
            decision = {
                "approved_quantity": 0, "outcome": "rejected", "risk_budget_dollars": None,
                "binding_constraint": "trading_permission:" + ",".join(permission["reasons"]),
                "constraint_detail": {"trading_permission": permission},
            }
        else:
            sector_map = load_sector_map(conn, {symbol} | set(positions.keys()))
            open_risk_dollars = risk_engine.load_open_risk_dollars(conn)
            hwm = circuit_breaker.current_high_water_mark(conn)
            drawdown_pct = circuit_breaker.drawdown_pct_of(portfolio_value, hwm) if portfolio_value else 0.0
            drawdown_mult = circuit_breaker.drawdown_size_multiplier(drawdown_pct, p["circuit_breaker_drawdown_pct"])
            decision = risk_engine.evaluate_proposal(
                symbol, price, requested_qty, planned_initial_stop_price,
                cash, portfolio_value, positions, sector_map, open_risk_dollars, p,
                drawdown_multiplier=drawdown_mult,
            )
    except Exception as e:
        raise HTTPException(400, f"Risk engine evaluation failed, refusing to size this trade: {e}")

    _record_risk_decision(cur, context, proposal_id, symbol, "buy", requested_qty, decision)
    # Committed here, immediately, regardless of outcome -- a rejection
    # must still be recorded. Callers using this within a larger
    # transaction that later rolls back on an unrelated error would lose
    # this row too, but that's an acceptable tradeoff: the alternative is
    # this function silently NOT persisting a rejection, which defeats the
    # audit purpose of risk_decisions entirely.
    conn.commit()

    if decision["approved_quantity"] < 1:
        raise HTTPException(
            400,
            f"Risk engine rejected this trade: {decision['binding_constraint']} "
            f"(requested {requested_qty} shares)"
        )
    return decision["approved_quantity"], decision


def _record_risk_decision(cur, context, proposal_id, symbol, side, requested_qty, decision):
    cur.execute("SELECT overall FROM market_context LIMIT 1")
    row = cur.fetchone()
    market_overall = row["overall"] if row else None
    cur.execute("""
        INSERT INTO risk_decisions (
            context, proposal_id, symbol, side, requested_qty, approved_quantity,
            outcome, risk_budget_dollars, binding_constraint, constraint_detail,
            market_regime_at_decision
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        context, proposal_id, symbol, side, requested_qty, decision["approved_quantity"],
        decision["outcome"], decision["risk_budget_dollars"], decision["binding_constraint"],
        json.dumps(decision["constraint_detail"]), market_overall,
    ))

def cancel_resting_stop_orders(symbol):
    """Best-effort: cancels any resting protective stop-leg order for this
    symbol before submitting an UNRELATED sell (thesis_complete/time_stop/
    overbought/regime_deterioration exits, or a manual sell) -- see
    Execution: protective stop orders. Alpaca's own docs don't specify
    whether closing a position through an unrelated order auto-cancels a
    resting OTO stop-loss child leg, so this defensively cancels first
    rather than risk a stale stop later trying to fire against an
    already-closed position. Must run BEFORE any qty_available check for
    the same symbol -- a resting stop order holds shares "unavailable"
    at Alpaca, so checking availability first would incorrectly block a
    legitimate exit that this cancellation would otherwise clear the way
    for. Failures here are logged, never raised -- a cancel failing must
    not block the sell it exists to protect against; an unmatched cancel
    attempt against an order that already filled/expired/was never there
    is expected and harmless, not an error worth surfacing to the caller."""
    try:
        open_orders = alpaca("GET", f"/v2/orders?status=open&symbols={symbol}")
    except Exception as e:
        log.warning(f"Could not check resting stop orders for {symbol}: {e}")
        return
    for order in open_orders or []:
        if order.get("type") in ("stop", "stop_limit"):
            try:
                alpaca("DELETE", f"/v2/orders/{order['id']}")
                log.info(f"Canceled resting stop order {order['id']} for {symbol} before unrelated sell")
            except Exception as e:
                log.warning(f"Could not cancel resting stop order {order['id']} for {symbol}: {e}")


def _stop_price_for_order(ref_price, planned_stop, stop_loss_pct):
    """The price attached to a buy order's OTO stop_loss leg. Prefers the
    proposal's own planned_initial_stop_price (the SAME value
    shared/risk_engine.py's risk-budget sizing already used as
    risk-per-share, and the same value trades.initial_stop_price /
    position_lifecycles already assume was the real stop -- using
    anything else here would make the persisted risk basis a fiction).
    Falls back to the live stop_loss_pct ratio against the reference
    price for a manual trade with no linked proposal at all, same
    formula compute_signals() itself uses for planned_initial_stop_price."""
    if planned_stop is not None:
        return round(planned_stop, 2)
    return round(ref_price * (1 - stop_loss_pct), 2)


@app.post("/api/trade")
def execute_trade(req: TradeRequest, background_tasks: BackgroundTasks):
    if req.side not in ("buy", "sell"):
        raise HTTPException(400, "side must be buy or sell")
    if req.qty <= 0:
        raise HTTPException(400, "qty must be positive")

    order_qty = req.qty
    risk_decision = None
    stop_price_for_order = None

    # Risk engine: BUY only, clamps BEFORE the order reaches Alpaca -- see
    # docs/risk-engine-architecture-reconciliation.md section C.1. Opens its
    # own short-lived connection since the risk decision must be evaluated
    # (and, on rejection, raise) before the main DB block below even starts.
    if req.side == "buy":
        with db() as risk_conn, risk_conn.cursor() as risk_cur:
            initial_stop_price = None
            if req.proposal_id:
                risk_cur.execute("SELECT planned_initial_stop_price FROM trade_proposals WHERE id=%s", (req.proposal_id,))
                row = risk_cur.fetchone()
                initial_stop_price = float(row["planned_initial_stop_price"]) if row and row["planned_initial_stop_price"] is not None else None
            risk_cur.execute("SELECT close FROM price_history WHERE symbol=%s ORDER BY ts DESC LIMIT 1", (req.symbol.upper(),))
            row = risk_cur.fetchone()
            ref_price = float(row["close"]) if row else None
            if ref_price is None:
                raise HTTPException(400, f"No price history for {req.symbol.upper()} -- cannot size this trade")
            order_qty, risk_decision = _clamp_to_risk_engine(
                risk_conn, risk_cur, "manual_trade", req.proposal_id,
                req.symbol.upper(), req.qty, ref_price, initial_stop_price)
            stop_loss_pct = load_params(risk_conn)["stop_loss_pct"]
            stop_price_for_order = _stop_price_for_order(ref_price, initial_stop_price, stop_loss_pct)
    else:
        # Execution: protective stop orders -- an unrelated sell must clear
        # any resting OTO stop-loss child leg BEFORE Alpaca is asked
        # anything else about this symbol (a resting stop holds shares
        # "unavailable", which would otherwise look identical to genuinely
        # having no position).
        cancel_resting_stop_orders(req.symbol.upper())

    # Submit to Alpaca. BUY orders attach a resting OTO stop-loss child leg
    # (Execution: protective stop orders) so the broker enforces the stop
    # in real time rather than relying solely on check_stop_losses()'s
    # hourly poll-and-propose cycle -- that mechanism stays as a backup
    # (e.g. for positions opened before this shipped, or if this
    # submission's stop leg somehow failed to attach).
    order_payload = {
        "symbol": req.symbol.upper(),
        "qty": str(order_qty),
        "side": req.side,
        "type": "market",
        "time_in_force": "gtc",
    }
    if req.side == "buy" and stop_price_for_order is not None:
        order_payload["order_class"] = "oto"
        order_payload["stop_loss"] = {"stop_price": str(stop_price_for_order)}
    order = alpaca("POST", "/v2/orders", json=order_payload)

    filled_price = float(order.get("filled_avg_price") or order.get("limit_price") or 0)
    filled_qty = float(order.get("filled_qty") or 0) or order_qty

    # Market orders placed outside trading hours come back "accepted"/"pending_new",
    # not "filled" — Alpaca queues them for the next open rather than rejecting them.
    # (See _next_open_status: that same transient status can also appear for a
    # split second during market hours while a normal fill is in flight, so the
    # actual market clock -- not just this status -- decides which case it is.)
    booked_for_next_open, next_open = _next_open_status(order)

    # Log to DB
    with db() as conn, conn.cursor() as cur:
        cost = _current_trade_cost_flat(cur)
        thesis_id = _resolve_thesis_id(cur, req.proposal_id)
        trade_thesis_id = _resolve_trade_thesis_id(cur, req.proposal_id)

        # Platform Improvements PR C: rule-adherence bypass detection.
        # POST /api/trade enforces none of compute_signals()'s six
        # buy-side gates itself -- this re-checks them and records which,
        # if any, would currently fail. Purely advisory, fail-open: never
        # affects the response below. Deliberately checked BEFORE this
        # trade is inserted, not after -- buy_cooldown looks for a recent
        # BUY of this symbol in the trades table, and this trade would
        # always trivially satisfy its own check if it were already
        # committed first. (The Alpaca order itself already happened by
        # this point, so position-count/sector-cap gates unavoidably see
        # it -- that race is inherent to how fast a market order fills and
        # isn't fixable the same way. Quantity itself is no longer part of
        # what this advisory check needs to catch -- the risk engine above
        # already clamped it bindingly before the order was ever placed.)
        try:
            adherence_results = rule_adherence.check_gates(
                conn, req.symbol.upper(), req.side, filled_qty, filled_price)
        except Exception as e:
            adherence_results = None
            log.warning(f"Rule-adherence check failed for {req.symbol.upper()} {req.side}: {e}")

        # Platform Improvements PR A: copy the stop price onto the trade,
        # immutably, at fill time -- this is what makes a lifecycle's risk
        # basis fixed at entry even if stop_loss_pct or the proposal's own
        # fields change later. As of Execution: protective stop orders,
        # this is exactly stop_price_for_order -- the SAME value that was
        # (for a buy) attached as the order's own OTO stop_loss leg above,
        # so the persisted risk basis always matches what's actually
        # protecting the position at the broker, including the
        # stop_loss_pct-ratio fallback for a manual buy with no linked
        # proposal (previously NULL/no risk tracking at all for that case).
        # Still None for sells, same as before.
        initial_stop_price = stop_price_for_order
        cur.execute("""
            INSERT INTO trades (symbol, side, qty, price, notional, order_id, traded_at, notes, source, status, proposal_id, cost, thesis_id, initial_stop_price, trade_thesis_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            req.symbol.upper(), req.side, filled_qty, filled_price,
            filled_qty * filled_price, order["id"],
            datetime.now(timezone.utc), req.notes, req.source,
            order["status"], req.proposal_id, cost, thesis_id, initial_stop_price, trade_thesis_id
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

        if adherence_results is not None:
            try:
                _record_rule_adherence(
                    cur, "manual_trade", trade_id, req.proposal_id,
                    req.symbol.upper(), req.side, adherence_results)
                conn.commit()
            except Exception as e:
                log.warning(f"Rule-adherence check failed to record for trade {trade_id}: {e}")

    # Market orders fill within seconds — reconcile fill price in the background
    background_tasks.add_task(_reconcile_fill, trade_id, order["id"], filled_qty)

    result = {
        "trade_id": trade_id, "order_id": order["id"], "status": order["status"],
        "booked_for_next_open": booked_for_next_open, "next_open": next_open,
    }
    if risk_decision is not None:
        result["risk_decision"] = {
            "requested_qty": req.qty, "approved_quantity": risk_decision["approved_quantity"],
            "outcome": risk_decision["outcome"], "binding_constraint": risk_decision["binding_constraint"],
        }
    return result

class ProposalDecision(BaseModel):
    decision: str          # approved | rejected
    qty: Optional[float] = None
    rejection_reason: Optional[str] = None

@app.patch("/api/proposals/{proposal_id}")
def decide_proposal(proposal_id: int, body: ProposalDecision, background_tasks: BackgroundTasks):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM trade_proposals WHERE id=%s", (proposal_id,))
        p = cur.fetchone()
        if not p:
            raise HTTPException(404, "proposal not found")
        if p["decision"]:
            raise HTTPException(409, "already decided")

        new_trade_id = None
        adherence_results = None
        risk_decision = None
        if body.decision == "approved":
            # Execute the trade
            trade_qty = body.qty or p["qty"]
            if not trade_qty:
                raise HTTPException(400, "qty required for approval (proposal has no default qty)")

            # Risk engine: BUY only, clamps BEFORE the order reaches Alpaca --
            # see docs/risk-engine-architecture-reconciliation.md section C.1.
            # This is what makes trade_qty binding rather than whatever
            # body.qty a human supplied (previously unclamped -- only an
            # advisory rule_adherence check ran, after the order already
            # filled). Portfolio state may have moved since compute_signals()
            # sized this proposal (sometimes by days), so this is a genuine
            # re-evaluation, not just replaying the proposal-time decision.
            stop_price_for_order = None
            if p["side"] == "buy":
                ref_price = p["planned_entry_price"]
                if not ref_price:
                    cur.execute("SELECT close FROM price_history WHERE symbol=%s ORDER BY ts DESC LIMIT 1", (p["symbol"],))
                    row = cur.fetchone()
                    ref_price = row["close"] if row else None
                if not ref_price:
                    raise HTTPException(400, f"No price available for {p['symbol']} -- cannot size this trade")
                stop_price = p["planned_initial_stop_price"]
                stop_price = float(stop_price) if stop_price is not None else None
                trade_qty, risk_decision = _clamp_to_risk_engine(
                    conn, cur, "proposal_approval", proposal_id, p["symbol"],
                    trade_qty, float(ref_price), stop_price)
                stop_loss_pct = load_params(conn)["stop_loss_pct"]
                stop_price_for_order = _stop_price_for_order(float(ref_price), stop_price, stop_loss_pct)

            # For sell orders: cancel any resting protective stop first
            # (Execution: protective stop orders -- must run before the
            # qty_available check just below, since a resting stop order
            # holds shares "unavailable" at Alpaca and would otherwise make
            # a legitimate exit look like it has no shares to sell), then
            # verify we hold enough *available* (i.e. not already committed
            # to another open order) long shares to cover the sale. Selling
            # more than available would either open/deepen a short position
            # or get rejected by Alpaca with an opaque 403 — check up front
            # so the rejection reason is clear either way.
            if p["side"] == "sell":
                cancel_resting_stop_orders(p["symbol"])
                try:
                    pos = alpaca("GET", f"/v2/positions/{p['symbol']}")
                    available_qty = float(pos.get("qty_available", pos.get("qty", 0)))
                except HTTPException as e:
                    if e.status_code == 404:
                        available_qty = 0.0
                    else:
                        raise HTTPException(502, f"Could not verify {p['symbol']} position with Alpaca: {e.detail}")
                if available_qty <= 0:
                    raise HTTPException(400, f"No available long position in {p['symbol']} — either none held or fully committed to another pending order. Cannot sell.")
                if trade_qty > available_qty:
                    raise HTTPException(400, f"Sell qty {trade_qty} exceeds available shares {available_qty} for {p['symbol']} (some may be tied up in another pending order). Reduce qty to {available_qty} or less.")

            # BUY orders attach a resting OTO stop-loss child leg -- see
            # execute_trade()'s own comment for why check_stop_losses()
            # stays as a backup rather than being replaced.
            order_payload = {
                "symbol": p["symbol"], "qty": str(trade_qty),
                "side": p["side"], "type": "market", "time_in_force": "gtc",
            }
            if p["side"] == "buy" and stop_price_for_order is not None:
                order_payload["order_class"] = "oto"
                order_payload["stop_loss"] = {"stop_price": str(stop_price_for_order)}
            order = alpaca("POST", "/v2/orders", json=order_payload)
            filled_price = float(order.get("filled_avg_price") or 0)
            filled_qty = float(order.get("filled_qty") or 0) or float(trade_qty)
            booked_for_next_open, next_open = _next_open_status(order)

            # Platform Improvements PR C: re-check the same gates
            # compute_signals() enforced when this proposal was originally
            # created, against state NOW at approval time -- the literal
            # "proposal created, conditions changed, approved anyway" case.
            # Buy-side only: a sell approval already gets a real
            # availability check above (qty_available), a different and
            # more specific question than the six buy-side gates. Checked
            # BEFORE this trade is inserted below, not after -- same
            # self-trip reasoning as execute_trade()'s own ordering (a
            # buy_cooldown check would always trivially find this trade if
            # it were already committed first).
            if p["side"] == "buy":
                try:
                    adherence_results = rule_adherence.check_gates(
                        conn, p["symbol"], "buy", float(trade_qty), filled_price)
                except Exception as e:
                    log.warning(f"Rule-adherence check failed for proposal {proposal_id}: {e}")

            cost = _current_trade_cost_flat(cur)
            cur.execute("""
                INSERT INTO trades (symbol, side, qty, price, notional, order_id, traded_at, source, status, proposal_id, cost, thesis_id, initial_stop_price, trade_thesis_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (p["symbol"], p["side"], filled_qty, filled_price,
                  filled_qty * filled_price, order["id"],
                  datetime.now(timezone.utc), "model_approved", order["status"], proposal_id, cost, p["thesis_id"],
                  stop_price_for_order, p["trade_thesis_id"]))
            new_trade_id = cur.fetchone()["id"]
            # update proposal qty if it was null
            cur.execute("UPDATE trade_proposals SET qty=%s WHERE id=%s AND qty IS NULL", (trade_qty, proposal_id))
            # Reconcile fill in background
            background_tasks.add_task(_reconcile_fill, new_trade_id, order["id"], filled_qty)

        cur.execute("""
            UPDATE trade_proposals
            SET decision=%s, decided_at=NOW(), decided_by='human', rejection_reason=%s
            WHERE id=%s
        """, (body.decision, body.rejection_reason, proposal_id))
        conn.commit()

        # Recorded only after the decision above is safely committed --
        # a failure inserting this advisory row must never poison the
        # transaction the decision itself needs to commit.
        if adherence_results is not None:
            try:
                _record_rule_adherence(
                    cur, "proposal_approval", new_trade_id, proposal_id,
                    p["symbol"], "buy", adherence_results)
                conn.commit()
            except Exception as e:
                log.warning(f"Rule-adherence check failed to record for proposal {proposal_id}: {e}")

    result = {"status": "ok", "decision": body.decision}
    if body.decision == "approved":
        result["booked_for_next_open"] = booked_for_next_open
        result["next_open"] = next_open
        if risk_decision is not None:
            result["risk_decision"] = {
                "requested_qty": body.qty or p["qty"], "approved_quantity": risk_decision["approved_quantity"],
                "outcome": risk_decision["outcome"], "binding_constraint": risk_decision["binding_constraint"],
            }
    return result


# ── Universe / Leaderboard ───────────────────────────────────────────────────

@app.get("/api/leaderboard")
def get_leaderboard(limit: int = 30, side: str = "both"):
    # held_qty flags rows where a sell-scan score is actually actionable --
    # a sell signal for a symbol you don't hold can never become a real
    # proposal (see check_alerts' no_position_held gate), so surfacing
    # which sell rows correspond to real positions (and how many shares)
    # is what makes this list useful at a glance rather than just noise
    # across the whole universe. Also replaces the generic "Watching"
    # badge with actual share count for held symbols -- "on the
    # watchlist" is much less informative than "you own 44 shares."
    try:
        held_qty = {p["symbol"]: p["qty"] for p in get_positions()}
    except Exception:
        held_qty = {}
    with db() as conn, conn.cursor() as cur:
        if side == "buy":
            order_col = "buy_score"
        elif side == "sell":
            order_col = "sell_score"
        else:
            order_col = "GREATEST(buy_score, sell_score)"
        cur.execute(f"""
            SELECT u.symbol, uni.name AS company_name, u.price, u.rsi, u.buy_score, u.sell_score,
                   u.regime, u.scanned_at,
                   w.symbol IS NOT NULL AS on_watchlist,
                   w.pinned
            FROM universe_scan u
            LEFT JOIN watchlist w ON w.symbol = u.symbol
            LEFT JOIN universe uni ON uni.symbol = u.symbol
            WHERE GREATEST(u.buy_score, u.sell_score) > 0
            ORDER BY {order_col} DESC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
    for r in rows:
        r["held_qty"] = held_qty.get(r["symbol"])
        r["is_held"] = r["symbol"] in held_qty
    return rows

@app.get("/api/market-context")
def get_market_context():
    """Latest market regime snapshot — SPY/QQQ trend, VIX, overall regime, trading modifiers."""
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM market_context LIMIT 1")
            row = cur.fetchone()
        if row:
            return dict(row)
    except Exception:
        pass
    return {
        "spy_trend": "unknown", "qqq_trend": "unknown",
        "vix": None, "vix_regime": "unknown",
        "overall": "unknown", "score_modifier": 0, "alloc_modifier": 1.0,
        "rationale": "No market context data yet — runs on next ingest cycle",
        "updated_at": None,
    }

@app.get("/api/regime/sector/{sector}")
def get_sector_regime(sector: str):
    """Latest persisted sector-level regime — absolute trend + relative
    strength vs the market benchmark. Reflects recent measured conditions,
    not a prediction. 'unknown' means no ETF mapping exists for this
    sector name; 'insufficient_data' means the mapping exists but there
    isn't enough price history yet."""
    with db() as conn:
        row = load_latest_sector_regime(conn, sector)
    if row:
        return row
    return {
        "sector": sector, "sector_symbol": None, "classification": "unknown",
        "total_score": None, "absolute_trend_score": None, "relative_strength_score": None,
        "breadth_score": None, "confidence": 0.0, "component_values": {},
        "trading_date": None,
    }

@app.get("/api/regime/security/{symbol}")
def get_security_regime(symbol: str):
    """Latest persisted stock-level regime — absolute trend + relative
    strength vs both its sector and the market. Reflects recent measured
    conditions, not a prediction."""
    with db() as conn:
        row = load_latest_security_regime(conn, symbol)
    if row:
        return row
    return {
        "symbol": symbol, "sector": None, "classification": "insufficient_data",
        "total_score": None, "absolute_trend_score": None, "vs_sector_score": None,
        "vs_market_score": None, "confidence": 0.0, "component_values": {},
        "trading_date": None,
    }

@app.get("/api/global-markets")
def get_global_markets():
    """Global-markets world clock/map data: each configured market plus its
    latest overnight_pct observation. regime is a simple display-only
    +/-0.3% threshold on overnight_pct -- NOT the validated composite the
    world-clock mockup was designed around. See [[project_homelab_investor]]
    2026-07-21/22 notes: score_signal() must not read anything from this
    table until an Experiment-005-style significance test clears it; this
    threshold exists purely to color the dashboard, not to trade on."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT gm.slug, gm.display_name, gm.index_symbol, gm.lat, gm.lon,
                   gm.market_cap_usd_tn, gm.timezone, gm.local_open_hour, gm.local_close_hour,
                   gms.overnight_pct, gms.trading_date
            FROM global_markets gm
            LEFT JOIN LATERAL (
                SELECT overnight_pct, trading_date FROM global_market_signals
                WHERE market_id = gm.id ORDER BY trading_date DESC LIMIT 1
            ) gms ON TRUE
            ORDER BY gm.market_cap_usd_tn DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]

    settings = get_all_settings()
    home_market = settings.get("home_market", "us_nyse")

    for r in rows:
        pct = r["overnight_pct"]
        r["regime"] = "risk_on" if pct is not None and pct > 0.3 else \
                      "risk_off" if pct is not None and pct < -0.3 else "neutral"
        r["is_home"] = r["slug"] == home_market

    return {"home_market": home_market, "markets": rows}

@app.get("/api/search")
def search_symbols(q: str = "", limit: int = 10):
    """Symbol/company-name search for the dashboard search bar. Searches the
    full universe table (~12k symbols, same one the leaderboard/universe
    scanner already covers) rather than just the watchlist -- /symbol/{sym}
    already works for any symbol via _backfill_thin_history, so search
    shouldn't be artificially narrower than what's actually viewable."""
    q = q.strip()
    if not q:
        return []
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, name, exchange, scannable,
                   CASE
                       WHEN symbol = UPPER(%(q)s) THEN 0
                       WHEN symbol ILIKE %(prefix)s THEN 1
                       ELSE 2
                   END AS rank
            FROM universe
            WHERE symbol ILIKE %(prefix)s OR name ILIKE %(contains)s
            ORDER BY rank, scannable DESC, symbol
            LIMIT %(limit)s
        """, {"q": q, "prefix": f"{q}%", "contains": f"%{q}%", "limit": limit})
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


# ── App Settings ─────────────────────────────────────────────────────────────

_SETTINGS_DEFAULTS = {
    "smtp_user":        os.environ.get("SMTP_USER", ""),
    "smtp_pass":        os.environ.get("SMTP_PASS", ""),
    "digest_to":        os.environ.get("DIGEST_TO", os.environ.get("SMTP_USER", "")),
    "atq_url":          os.environ.get("ATQ_URL", "http://10.10.10.226:8700"),
    "notify_email":           "true",
    "notify_whatsapp":        "true",
    "digest_hour_utc":        "21",
    "digest_minute_utc":      "30",
    "morning_hour_utc":       "13",
    "morning_minute_utc":     "30",
    "alert_stop_loss":        "true",
    "alert_portfolio_drop":   "true",
    "alert_portfolio_drop_pct": "3.0",
    "alert_high_score":       "true",
    "alert_high_score_min":   "80",
    "alert_circuit_breaker":  "true",
    "home_market":            "us_nyse",  # slug into global_markets (see migration 002) -- which market gets the star on the world map
    # Portfolio-vs-SPY comparison chart assumptions -- default ON so the
    # comparison is apples-to-apples out of the box, not misleadingly
    # favorable to SPY. See 2026-07-22 session notes.
    "spy_cost_adjust":        "true",  # subtract one-time modeled trade cost from the SPY line, matching how the portfolio line is already cost-adjusted
    "spy_include_dividends":  "true",  # use dividend-adjusted close (adjclose) instead of price-only close for SPY
}

def _ensure_settings_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """)
        for k, v in _SETTINGS_DEFAULTS.items():
            cur.execute(
                "INSERT INTO app_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                (k, v)
            )
        conn.commit()

def get_all_settings():
    with db() as conn:
        _ensure_settings_table(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM app_settings")
            return {r["key"]: r["value"] for r in cur.fetchall()}

@app.get("/api/settings")
def api_get_settings():
    s = get_all_settings()
    # Never expose password in plaintext — return masked version
    result = dict(s)
    if result.get("smtp_pass"):
        result["smtp_pass_set"] = True
        result["smtp_pass"] = ""  # don't send to browser
    else:
        result["smtp_pass_set"] = False
    return result

class SettingsUpdate(BaseModel):
    smtp_user: Optional[str] = None
    smtp_pass: Optional[str] = None
    digest_to: Optional[str] = None
    atq_url: Optional[str] = None
    notify_email: Optional[str] = None
    notify_whatsapp: Optional[str] = None
    digest_hour_utc: Optional[str] = None
    digest_minute_utc: Optional[str] = None
    morning_hour_utc: Optional[str] = None
    morning_minute_utc: Optional[str] = None
    alert_stop_loss: Optional[str] = None
    alert_portfolio_drop: Optional[str] = None
    alert_portfolio_drop_pct: Optional[str] = None
    alert_high_score: Optional[str] = None
    alert_high_score_min: Optional[str] = None
    alert_circuit_breaker: Optional[str] = None
    home_market: Optional[str] = None
    spy_cost_adjust: Optional[str] = None
    spy_include_dividends: Optional[str] = None

@app.post("/api/settings")
def api_save_settings(body: SettingsUpdate):
    updates = {k: v for k, v in body.dict().items() if v is not None}
    # Don't overwrite password if blank string was sent (masked field)
    if "smtp_pass" in updates and updates["smtp_pass"] == "":
        del updates["smtp_pass"]
    with db() as conn:
        _ensure_settings_table(conn)
        with conn.cursor() as cur:
            for k, v in updates.items():
                cur.execute("UPDATE app_settings SET value=%s WHERE key=%s", (v, k))
        conn.commit()
    return {"status": "ok", "updated": list(updates.keys())}

@app.post("/api/settings/test-email")
def test_email():
    import smtplib
    from email.mime.text import MIMEText
    s = get_all_settings()
    smtp_user = s.get("smtp_user", "")
    smtp_pass = s.get("smtp_pass", "")
    digest_to = s.get("digest_to", smtp_user)
    if not smtp_user or not smtp_pass:
        raise HTTPException(400, "SMTP credentials not configured")
    try:
        msg = MIMEText("This is a test email from homelab-trader.")
        msg["Subject"] = "homelab-trader — test email"
        msg["From"] = smtp_user
        msg["To"] = digest_to
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, digest_to, msg.as_string())
        return {"status": "ok", "sent_to": digest_to}
    except Exception as e:
        raise HTTPException(502, f"Email failed: {e}")


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

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request})


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
            # qty_available excludes shares already committed to another open
            # order (e.g. a stop-loss sell already booked after-hours) — a
            # position can still show the full qty while 0 is actually sellable.
            qty_available = float(p.get("qty_available", p.get("qty", 0)))
            stop_alerts.append({
                "symbol": p["symbol"],
                "plpc": round(plpc * 100, 2),
                "qty": float(p["qty"]),
                "qty_available": qty_available,
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

    # stop-loss alerts — actionable when shares are actually available to
    # sell; if they're already committed to another open order, say so
    # instead of offering a redundant (and rejected) approve action.
    for a in stop_alerts:
        base = f"{a['symbol']} is down {a['plpc']}% — at or past stop-loss threshold ({round(stop_loss_pct*100)}%)."
        if a["qty_available"] > 0:
            qty_str = f"{a['qty_available']:g}"
            bullets.append({
                "type": "alert",
                "text": f"{base} Sell {qty_str} shares of {a['symbol']}?",
                "action": {"symbol": a["symbol"], "side": "sell", "qty": a["qty_available"]},
            })
        else:
            bullets.append({
                "type": "alert",
                "text": f"{base} Sell already booked for {a['qty']:g} shares — awaiting next market open.",
            })

    # headline
    headlines = {
        "warning": f"⚠️ Stop-loss alert on {', '.join(a['symbol'] for a in stop_alerts)}",
        "hold":    f"Hold — position limit reached and market is extended ({overbought_pct}% overbought)",
        "bullish": f"{open_slots} slot{'s' if open_slots != 1 else ''} open and market conditions favor buying",
        "cautious": "Capital available but conditions are mixed — proceed selectively",
    }

    # structured candidates for linked display
    per_trade_notional = investable_cash * params.get("trade_allocation_pct", 0.05)
    candidates = []
    for r in top_buys[:6]:
        price = float(r["price"]) if r.get("price") else 0
        suggested_shares = int(per_trade_notional / price) if price > 0 else None
        suggested_notional = round(suggested_shares * price, 2) if suggested_shares else None
        candidates.append({
            "symbol": r["symbol"],
            "rsi": round(float(r["rsi"]), 1),
            "buy_score": int(r["buy_score"]),
            "price": round(price, 2),
            "is_held": r["symbol"] in held_symbols,
            "suggested_shares": suggested_shares,
            "suggested_notional": suggested_notional,
        })

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

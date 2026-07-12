"""
Signal generation: RSI mean reversion + Bollinger Bands + market regime detection.
Based on backtesting research: mean reversion is the only strategy that broadly survives
rigorous walk-forward validation across all asset classes and market conditions.
"""

import logging
import math
import os
import requests

from earnings import earnings_blackout_reason
from circuit_breaker import record_snapshot_and_check

log = logging.getLogger(__name__)

YF_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; invest-agent/1.0)"}

ALPACA_BASE = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET", ""),
}

# Defaults — overridden at runtime by signal_params table
DEFAULTS = {
    "rsi_period": 14,
    "rsi_oversold": 30,
    "rsi_overbought": 70,
    "rsi_strong_oversold": 25,
    "rsi_strong_overbought": 75,
    "bb_period": 20,
    "bb_std": 2.0,
    "score_log_min": 30,
    "score_proposal_min": 65,
    "regime_sma_fast": 50,
    "regime_sma_slow": 200,
    "regime_band": 0.02,
    # Position sizing + risk
    "trade_allocation_pct": 0.05,
    "max_position_pct": 0.20,
    "max_open_positions": 5,
    "stop_loss_pct": 0.08,
    "sector_max_pct": 0.30,
    "earnings_blackout_days": 3,
    "circuit_breaker_drawdown_pct": 0.15,
}

# Relative strength vs SPY and ATR-normalized move: score modifiers layered
# on top of RSI+BB+regime. Tuning constants, not DB params — same precedent
# as the regime multipliers above (0.80/0.60/1.15 etc), which are also
# hardcoded rather than exposed via signal_params.
RS_LOOKBACK_DAYS = 20
ATR_PERIOD = 14

# Reward mean-reversion buys in stocks that haven't cratered relative to the
# market (avoids "falling knife" dips driven by idiosyncratic bad news), and
# reward mean-reversion sells in stocks that haven't been on a genuine
# momentum run (avoids selling into strength that may keep running).
RS_BUY_WEAK_PCT = -15      # underperformance vs SPY beyond this penalizes a buy
RS_BUY_STRONG_PCT = 10     # outperformance vs SPY beyond this rewards a buy
RS_SELL_STRONG_PCT = 15    # outperformance vs SPY beyond this penalizes a sell
RS_SELL_WEAK_PCT = -10     # underperformance vs SPY beyond this rewards a sell
RS_PENALTY_MULT = 0.75
RS_BONUS_MULT = 1.05

# How many true-range units price has moved from the 20-day mean (BB
# midline). A large move relative to a stock's own typical range is more
# conviction-worthy than the same raw % move in a normally-quiet stock.
ATR_STRONG_RATIO = 2.0
ATR_WEAK_RATIO = 0.75
ATR_BOOST_MULT = 1.10
ATR_PENALTY_MULT = 0.85


def load_params(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM signal_params")
            rows = cur.fetchall()
        params = dict(DEFAULTS)
        for row in rows:
            k = row[0] if isinstance(row, (list, tuple)) else row["key"]
            v = float(row[1] if isinstance(row, (list, tuple)) else row["value"])
            params[k] = v
        return params
    except Exception as e:
        log.warning(f"Could not load signal_params, using defaults: {e}")
        return dict(DEFAULTS)


def fetch_closes(symbol, yf_range="1y"):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = requests.get(url, params={"interval": "1d", "range": yf_range},
                     headers=YF_HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    result = data["chart"]["result"][0]
    timestamps = result["timestamp"]
    # Use adjclose: split-adjusted AND dividend-adjusted. quote.close is split-adjusted only.
    adj = result["indicators"].get("adjclose")
    raw_closes = adj[0]["adjclose"] if adj else result["indicators"]["quote"][0]["close"]
    closes = [float(c) for c, ts in zip(raw_closes, timestamps) if c is not None]
    return closes


def fetch_alpaca_portfolio():
    """Return (cash, portfolio_value, positions_dict) from Alpaca."""
    try:
        acct = requests.get(f"{ALPACA_BASE}/v2/account", headers=ALPACA_HEADERS, timeout=10)
        acct.raise_for_status()
        a = acct.json()
        cash = float(a["cash"])
        portfolio_value = float(a["portfolio_value"])

        pos_r = requests.get(f"{ALPACA_BASE}/v2/positions", headers=ALPACA_HEADERS, timeout=10)
        pos_r.raise_for_status()
        positions = {
            p["symbol"]: {
                "qty": float(p["qty"]),
                "avg_entry": float(p["avg_entry_price"]),
                "current_price": float(p["current_price"]),
                "market_value": float(p["market_value"]),
                "unrealized_plpc": float(p["unrealized_plpc"]),
            }
            for p in pos_r.json()
        }
        return cash, portfolio_value, positions
    except Exception as e:
        log.warning(f"Could not fetch Alpaca portfolio: {e}")
        return None, None, {}


def compute_rsi(closes, period):
    period = int(period)
    if len(closes) < period + 2:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _load_recent_closes_from_db(conn, symbol, n):
    """Oldest -> newest closes from already-ingested price_history."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT close FROM price_history
            WHERE symbol=%s ORDER BY ts DESC LIMIT %s
        """, (symbol, n))
        rows = cur.fetchall()
    return [float(r[0]) for r in reversed(rows)]


def _load_recent_ohlc_from_db(conn, symbol, n):
    """Oldest -> newest (high, low, close) from already-ingested price_history."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT high, low, close FROM price_history
            WHERE symbol=%s ORDER BY ts DESC LIMIT %s
        """, (symbol, n))
        rows = cur.fetchall()
    return [(float(h), float(l), float(c)) for h, l, c in reversed(rows)]


def compute_atr(ohlc, period=ATR_PERIOD):
    """Wilder's ATR from a list of (high, low, close) oldest->newest."""
    if len(ohlc) < period + 1:
        return None
    trs = []
    prev_close = ohlc[0][2]
    for h, l, c in ohlc[1:]:
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
        prev_close = c
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def relative_strength_vs_spy(conn, sym, lookback=RS_LOOKBACK_DAYS):
    """sym's %-return over `lookback` days minus SPY's, in percentage points.
    None if either doesn't have enough price_history yet."""
    sym_closes = _load_recent_closes_from_db(conn, sym, lookback + 5)
    spy_closes = _load_recent_closes_from_db(conn, "SPY", lookback + 5)
    if len(sym_closes) < lookback + 1 or len(spy_closes) < lookback + 1:
        return None
    sym_ret = (sym_closes[-1] - sym_closes[-lookback - 1]) / sym_closes[-lookback - 1] * 100
    spy_ret = (spy_closes[-1] - spy_closes[-lookback - 1]) / spy_closes[-lookback - 1] * 100
    return sym_ret - spy_ret


def compute_bollinger(closes, period, num_std):
    period = int(period)
    if len(closes) < period:
        return None, None, None, None
    window = closes[-period:]
    sma = sum(window) / period
    variance = sum((x - sma) ** 2 for x in window) / period
    std = variance ** 0.5
    return sma + num_std * std, sma, sma - num_std * std, std


def detect_regime(closes, fast, slow, band):
    fast, slow = int(fast), int(slow)
    if len(closes) < slow:
        if len(closes) < fast:
            return "unknown"
        sma_fast = sum(closes[-fast:]) / fast
        current = closes[-1]
        if current > sma_fast * (1 + band):
            return "trending_up"
        elif current < sma_fast * (1 - band):
            return "trending_down"
        return "ranging"
    sma_fast = sum(closes[-fast:]) / fast
    sma_slow = sum(closes[-slow:]) / slow
    if sma_fast > sma_slow * (1 + band):
        return "trending_up"
    elif sma_fast < sma_slow * (1 - band):
        return "trending_down"
    return "ranging"


def _apply_atr_modifier(score, price, bb_middle, atr, parts):
    """Magnitude-only modifier: how many ATRs price has moved from the
    20-day mean. Same logic for buy and sell — it's about conviction in
    the size of the dislocation, not its direction."""
    if not atr or bb_middle is None:
        return score
    atr_move = abs(price - bb_middle) / atr
    if atr_move >= ATR_STRONG_RATIO:
        score *= ATR_BOOST_MULT
        parts.append(f"{atr_move:.1f}x ATR from mean — large dislocation (+{int((ATR_BOOST_MULT-1)*100)}%)")
    elif atr_move < ATR_WEAK_RATIO:
        score *= ATR_PENALTY_MULT
        parts.append(f"{atr_move:.1f}x ATR from mean — shallow dislocation ({int((ATR_PENALTY_MULT-1)*100)}%)")
    return score


def score_signal(rsi, price, bb_upper, bb_lower, band_std, bb_middle, regime, side, p,
                  rs_pct=None, atr=None):
    """Return (score 0-100, rationale string) for a given side."""
    score = 0.0
    parts = []
    oversold = p["rsi_oversold"]
    overbought = p["rsi_overbought"]
    strong_oversold = p["rsi_strong_oversold"]
    strong_overbought = p["rsi_strong_overbought"]

    if side == "buy":
        if rsi is not None:
            if rsi < strong_oversold:
                score += 45; parts.append(f"RSI {rsi:.1f} deeply oversold (<{strong_oversold})")
            elif rsi < oversold:
                score += 35; parts.append(f"RSI {rsi:.1f} oversold (<{oversold})")
            elif rsi < oversold + 5:
                score += 20; parts.append(f"RSI {rsi:.1f} near oversold")

        if bb_lower is not None and band_std:
            # Continuous z-score: how many band-stds below the lower band is price?
            # Positive = below band, negative = above band.
            bb_dist = (bb_lower - price) / band_std
            if bb_dist > -0.5:  # within half a std of lower band, or below it
                bb_score = int(max(15, min(45, 20 + 25 * bb_dist)))
                score += bb_score
                if bb_dist >= 0:
                    parts.append(f"Price ${price:.2f} {bb_dist:.2f}σ below lower BB ${bb_lower:.2f} (+{bb_score})")
                else:
                    parts.append(f"Price ${price:.2f} near lower BB ${bb_lower:.2f} ({-bb_dist:.2f}σ above) (+{bb_score})")

        if regime == "ranging":
            score = min(score * 1.15, 100); parts.append("ranging market boosts MR")
        elif regime == "trending_up":
            score *= 0.80; parts.append("uptrend reduces MR reliability")
        elif regime == "trending_down":
            score *= 0.60; parts.append("downtrend — catch-falling-knife risk, heavily penalized")

        if rs_pct is not None:
            if rs_pct < RS_BUY_WEAK_PCT:
                score *= RS_PENALTY_MULT
                parts.append(f"underperforming SPY {rs_pct:+.1f}pp/{RS_LOOKBACK_DAYS}d — possible fundamental weakness")
            elif rs_pct > RS_BUY_STRONG_PCT:
                score *= RS_BONUS_MULT
                parts.append(f"resilient vs SPY ({rs_pct:+.1f}pp) despite oversold — cleaner MR candidate")

        score = _apply_atr_modifier(score, price, bb_middle, atr, parts)

    else:  # sell
        if rsi is not None:
            if rsi > strong_overbought:
                score += 45; parts.append(f"RSI {rsi:.1f} deeply overbought (>{strong_overbought})")
            elif rsi > overbought:
                score += 35; parts.append(f"RSI {rsi:.1f} overbought (>{overbought})")
            elif rsi > overbought - 5:
                score += 20; parts.append(f"RSI {rsi:.1f} near overbought")

        if bb_upper is not None and band_std:
            # Continuous z-score: how many band-stds above the upper band is price?
            bb_dist = (price - bb_upper) / band_std
            if bb_dist > -0.5:
                bb_score = int(max(15, min(45, 20 + 25 * bb_dist)))
                score += bb_score
                if bb_dist >= 0:
                    parts.append(f"Price ${price:.2f} {bb_dist:.2f}σ above upper BB ${bb_upper:.2f} (+{bb_score})")
                else:
                    parts.append(f"Price ${price:.2f} near upper BB ${bb_upper:.2f} ({-bb_dist:.2f}σ below) (+{bb_score})")

        if regime == "ranging":
            score = min(score * 1.15, 100); parts.append("ranging market boosts MR")
        elif regime == "trending_down":
            score *= 0.80; parts.append("downtrend reduces overbought signal reliability")
        elif regime == "trending_up":
            score *= 0.90; parts.append("uptrend — overbought less meaningful")

        if rs_pct is not None:
            if rs_pct > RS_SELL_STRONG_PCT:
                score *= RS_PENALTY_MULT
                parts.append(f"outperforming SPY {rs_pct:+.1f}pp/{RS_LOOKBACK_DAYS}d — momentum may override MR")
            elif rs_pct < RS_SELL_WEAK_PCT:
                score *= RS_BONUS_MULT
                parts.append(f"already lagging SPY ({rs_pct:+.1f}pp) despite overbought — MR more credible")

        score = _apply_atr_modifier(score, price, bb_middle, atr, parts)

    return int(score), "; ".join(parts)


def calc_buy_qty(price, cash, portfolio_value, existing_market_value, p):
    """
    Calculate how many shares to propose buying.
    Respects trade_allocation_pct and max_position_pct.
    Returns (qty, reason) — qty=None if constraints prevent the trade.
    """
    if not cash or not portfolio_value or cash <= 0:
        return None, "no cash data"

    # How much cash to deploy for this trade
    trade_dollars = cash * p["trade_allocation_pct"]

    # Cap so we don't exceed max_position_pct of portfolio in this symbol
    max_dollars_in_symbol = portfolio_value * p["max_position_pct"]
    already_in_symbol = existing_market_value or 0.0
    room = max_dollars_in_symbol - already_in_symbol
    if room <= 0:
        return None, f"already at max position ({p['max_position_pct']*100:.0f}% of portfolio)"

    trade_dollars = min(trade_dollars, room)

    # Can't spend more than we have
    trade_dollars = min(trade_dollars, cash)

    qty = math.floor(trade_dollars / price)
    if qty < 1:
        return None, f"trade allocation ${trade_dollars:.0f} too small to buy 1 share at ${price:.2f}"

    return qty, f"${trade_dollars:.0f} allocation ({p['trade_allocation_pct']*100:.0f}% of ${cash:.0f} cash)"


def _record_outcome(conn, signal_id, sym, side, score, rsi, bb_upper, bb_middle, bb_lower,
                     band_std, market_regime, symbol_regime, price):
    """Insert the signal_outcomes stub row for a scored signal. Defaults to blocked
    until the caller marks it proposed."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO signal_outcomes
                (signal_id, symbol, side, score, rsi, bb_upper, bb_middle, bb_lower, band_std,
                 market_regime, symbol_regime, price_at_signal)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (signal_id, sym, side, score, rsi, bb_upper, bb_middle, bb_lower, band_std,
              market_regime, symbol_regime, price))
        outcome_id = cur.fetchone()[0]
    conn.commit()
    return outcome_id


def _block_outcome(conn, outcome_id, reason):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE signal_outcomes SET proposal_status='blocked', block_reason=%s
            WHERE id=%s
        """, (reason, outcome_id))
    conn.commit()


def _propose_outcome(conn, outcome_id, proposal_id):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE signal_outcomes
            SET proposal_status='proposed', proposal_id=%s, approval_status='pending', block_reason=NULL
            WHERE id=%s
        """, (proposal_id, outcome_id))
    conn.commit()


def _load_sector_map(conn, symbols):
    """symbol -> GICS sector for the given symbols. Symbols with no sector
    (ETFs, unclassified) are simply absent from the returned dict."""
    if not symbols:
        return {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, sector FROM universe
            WHERE symbol = ANY(%s) AND sector IS NOT NULL
        """, (list(symbols),))
        return {r[0]: r[1] for r in cur.fetchall()}


def _sector_cap_block_reason(sym, price, qty, sector_map, positions, portfolio_value, p):
    """Return a block reason string if buying qty*price of sym would push its
    GICS sector over sector_max_pct of the portfolio, else None."""
    sector = sector_map.get(sym)
    if not sector or not portfolio_value:
        return None
    current_sector_value = sum(
        pos["market_value"] for s, pos in positions.items()
        if sector_map.get(s) == sector
    )
    projected_pct = (current_sector_value + qty * price) / portfolio_value
    cap = p["sector_max_pct"]
    if projected_pct > cap:
        return f"sector_cap_exceeded:{sector} ({projected_pct*100:.0f}%>{cap*100:.0f}%)"
    return None


def _open_sell_exists(conn, sym):
    """Return True if an undecided sell proposal already exists for this symbol."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id FROM trade_proposals
            WHERE symbol=%s AND side='sell' AND decision IS NULL
        """, (sym,))
        return cur.fetchone() is not None


def check_stop_losses(conn, positions, p):
    """Create sell proposals for positions that have breached the stop-loss threshold."""
    if not positions:
        return
    stop_pct = p["stop_loss_pct"]
    for sym, pos in positions.items():
        if pos["qty"] < 0:
            continue  # short position — our stop-loss logic only applies to longs
        loss_pct = (pos["avg_entry"] - pos["current_price"]) / pos["avg_entry"]
        if loss_pct >= stop_pct:
            rationale = (
                f"STOP-LOSS: {sym} down {loss_pct*100:.1f}% from avg entry "
                f"${pos['avg_entry']:.2f} → current ${pos['current_price']:.2f} "
                f"(threshold {stop_pct*100:.0f}%)"
            )
            log.warning(f"Stop-loss triggered: {rationale}")
            if _open_sell_exists(conn, sym):
                log.info(f"Stop-loss proposal for {sym} already open, skipping")
                continue
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO trade_proposals (symbol, side, qty, rationale, signal_score, exit_reason)
                    VALUES (%s, 'sell', %s, %s, 99, 'stop_loss')
                """, (sym, pos["qty"], rationale))
            conn.commit()
            log.info(f"Stop-loss PROPOSAL created: sell {pos['qty']} {sym}")


def check_regime_deterioration_sell(conn, positions, market_overall, market_rationale):
    """Defensive de-risking: bear_fear is the most severe market regime bucket
    (bear trend + VIX fear/crisis, per market_regime.py) — exit open longs
    proactively rather than wait for each position to hit its own stop-loss
    one at a time."""
    if market_overall != "bear_fear" or not positions:
        return
    for sym, pos in positions.items():
        if pos["qty"] <= 0:
            continue  # short position or already flat
        if _open_sell_exists(conn, sym):
            log.info(f"Regime-deterioration proposal for {sym} already open, skipping")
            continue
        gain_pct = pos["unrealized_plpc"] * 100
        rationale = (
            f"REGIME DETERIORATION: market regime is bear_fear — {market_rationale}. "
            f"De-risking {sym}: entry ${pos['avg_entry']:.2f} → current ${pos['current_price']:.2f} "
            f"({gain_pct:+.1f}%)"
        )
        log.warning(f"Exit [regime_deterioration]: {rationale}")
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trade_proposals (symbol, side, qty, rationale, signal_score, exit_reason)
                VALUES (%s, 'sell', %s, %s, 92, 'regime_deterioration')
            """, (sym, pos["qty"], rationale))
        conn.commit()
        log.info(f"Regime-deterioration PROPOSAL created: sell {pos['qty']} {sym}")


def check_symbol_exits(conn, sym, price, bb_middle, positions, p):
    """
    Check thesis-complete and time-stop exit conditions for a held symbol.
    Called inside the per-symbol loop once BB is computed.

    thesis_complete: price returned to SMA20 — mean reversion achieved.
    time_stop:       held > 20 trading days without thesis completing.
    """
    if sym not in positions or _open_sell_exists(conn, sym):
        return

    pos = positions[sym]
    qty = pos["qty"]
    if qty < 0:
        return  # short position — exit logic only applies to long positions
    avg_entry = pos["avg_entry"]

    # ── Thesis-complete: price crossed back above SMA20 / BB midline ─────
    if bb_middle is not None and price >= bb_middle:
        gain_pct = (price - avg_entry) / avg_entry * 100
        rationale = (
            f"THESIS COMPLETE: {sym} price ${price:.2f} ≥ SMA20 ${bb_middle:.2f} — "
            f"mean reversion achieved. Entry ${avg_entry:.2f} ({gain_pct:+.1f}%)"
        )
        log.info(f"Exit [thesis_complete]: {rationale}")
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trade_proposals (symbol, side, qty, rationale, signal_score, exit_reason)
                VALUES (%s, 'sell', %s, %s, 90, 'thesis_complete')
            """, (sym, qty, rationale))
        conn.commit()
        return  # don't also check time_stop in the same cycle

    # ── Time-stop: held > ~20 trading days, thesis unresolved ────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT MAX(proposed_at) FROM trade_proposals
            WHERE symbol=%s AND side='buy' AND decision='approved'
        """, (sym,))
        row = cur.fetchone()

    if row and row[0]:
        from datetime import datetime, timezone
        entry_ts = row[0]
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.replace(tzinfo=timezone.utc)
        calendar_days = (datetime.now(timezone.utc) - entry_ts).days
        approx_trading_days = int(calendar_days * 5 / 7)

        if approx_trading_days >= 20:
            gain_pct = (price - avg_entry) / avg_entry * 100
            rationale = (
                f"TIME STOP: {sym} held ~{approx_trading_days} trading days "
                f"({calendar_days} calendar days) without thesis completing. "
                f"Entry ${avg_entry:.2f} → current ${price:.2f} ({gain_pct:+.1f}%)"
            )
            log.warning(f"Exit [time_stop]: {rationale}")
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO trade_proposals (symbol, side, qty, rationale, signal_score, exit_reason)
                    VALUES (%s, 'sell', %s, %s, 85, 'time_stop')
                """, (sym, qty, rationale))
            conn.commit()


def _load_market_context(conn):
    """Load market context from DB. Returns (score_modifier, alloc_modifier, overall)."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT score_modifier, alloc_modifier, overall, rationale FROM market_context LIMIT 1")
            row = cur.fetchone()
        if row:
            return int(row[0] or 0), float(row[1] or 1.0), row[2] or "unknown", row[3] or ""
    except Exception:
        pass
    return 0, 1.0, "unknown", ""


def compute_signals(conn, symbols):
    """Main entry point: compute signals for all symbols, write to DB, create proposals."""
    p = load_params(conn)

    # Load market regime modifiers computed by market_regime.py this cycle
    score_mod, alloc_mod, market_overall, market_rationale = _load_market_context(conn)
    effective_proposal_min = p["score_proposal_min"] + score_mod
    effective_alloc = p["trade_allocation_pct"] * alloc_mod

    log.info(
        f"Signal params: RSI({int(p['rsi_period'])}) oversold={p['rsi_oversold']} overbought={p['rsi_overbought']} "
        f"BB({int(p['bb_period'])},{p['bb_std']}) proposal_min={effective_proposal_min:.0f} "
        f"(base {p['score_proposal_min']:.0f}+regime {score_mod:+d}) "
        f"alloc={effective_alloc*100:.1f}% (base {p['trade_allocation_pct']*100:.0f}%×{alloc_mod:.0%}) "
        f"max_pos={p['max_position_pct']*100:.0f}% max_open={int(p['max_open_positions'])} "
        f"stop_loss={p['stop_loss_pct']*100:.0f}% market={market_overall}"
    )
    if market_rationale:
        log.info(f"Market regime: {market_rationale}")

    # Apply alloc modifier to the working params copy used for position sizing
    p_gated = dict(p)
    p_gated["trade_allocation_pct"] = effective_alloc

    # Fetch live portfolio state from Alpaca
    cash, portfolio_value, positions = fetch_alpaca_portfolio()
    if cash is not None:
        log.info(f"Portfolio: cash=${cash:.2f} total=${portfolio_value:.2f} positions={list(positions.keys())}")

    # Circuit breaker: record this cycle's snapshot against the all-time
    # high-water mark. If drawdown exceeds threshold, buys pause below —
    # sells are never affected, and nothing is liquidated automatically.
    circuit_breaker_active, hwm, drawdown_pct = record_snapshot_and_check(
        conn, portfolio_value, p["circuit_breaker_drawdown_pct"])

    # Stop-loss check on existing positions
    check_stop_losses(conn, positions, p)

    # Bear_fear regime: proactively de-risk open longs rather than wait for
    # each position's own stop-loss to trigger one at a time
    check_regime_deterioration_sell(conn, positions, market_overall, market_rationale)

    # Count open positions for the max_open_positions gate
    open_position_count = len(positions)

    # Sector map for the sector-concentration cap, covers watchlist + any
    # held position not currently in the watchlist slice being scanned
    sector_map = _load_sector_map(conn, set(symbols) | set(positions.keys()))

    for sym in symbols:
        try:
            closes = fetch_closes(sym, "1y")
            if len(closes) < int(p["bb_period"]) + 2:
                log.warning(f"Signals: not enough data for {sym} ({len(closes)} days)")
                continue

            price = closes[-1]
            rsi = compute_rsi(closes, p["rsi_period"])
            bb_upper, bb_middle, bb_lower, band_std = compute_bollinger(closes, p["bb_period"], p["bb_std"])
            regime = detect_regime(closes, p["regime_sma_fast"], p["regime_sma_slow"], p["regime_band"])

            rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"
            bb_str = f"[{bb_lower:.2f},{bb_upper:.2f}]" if bb_lower is not None else "[?]"
            log.info(f"Signals {sym}: price={price:.2f} RSI={rsi_str} BB={bb_str} regime={regime}")

            # Exit condition checks for held positions (thesis_complete, time_stop)
            check_symbol_exits(conn, sym, price, bb_middle, positions, p)

            # Relative strength vs SPY + ATR, computed once per symbol (side-independent)
            rs_pct = relative_strength_vs_spy(conn, sym)
            ohlc = _load_recent_ohlc_from_db(conn, sym, ATR_PERIOD + 10)
            atr = compute_atr(ohlc)

            for side in ("buy", "sell"):
                score, rationale = score_signal(rsi, price, bb_upper, bb_lower, band_std, bb_middle,
                                                 regime, side, p, rs_pct=rs_pct, atr=atr)
                if score < p["score_log_min"]:
                    continue

                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO signals (symbol, signal_type, score, rationale)
                        VALUES (%s, %s, %s, %s)
                        RETURNING id
                    """, (sym, f"rsi_mr_{side}", score, rationale))
                    signal_id = cur.fetchone()[0]
                conn.commit()
                log.info(f"Signal {sym} {side}: score={score} — {rationale}")

                outcome_id = _record_outcome(conn, signal_id, sym, side, score, rsi,
                                              bb_upper, bb_middle, bb_lower, band_std,
                                              market_overall, regime, price)

                if score < effective_proposal_min:
                    if score_mod > 0:
                        log.info(f"Signal {sym} {side}: score {score} below regime-adjusted threshold {effective_proposal_min:.0f} (market={market_overall}), skipped")
                    _block_outcome(conn, outcome_id, "below_proposal_threshold")
                    continue

                # Check for existing open proposal
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id FROM trade_proposals
                        WHERE symbol=%s AND side=%s AND decision IS NULL
                    """, (sym, side))
                    if cur.fetchone():
                        log.info(f"Proposal for {sym} {side} already open, skipping")
                        _block_outcome(conn, outcome_id, "duplicate_open_proposal")
                        continue

                # Position sizing for buy signals
                qty = None
                sizing_note = ""
                if side == "buy":
                    if circuit_breaker_active:
                        log.info(f"Skipping buy proposal for {sym}: circuit breaker active "
                                 f"(drawdown {drawdown_pct*100:.1f}% from HWM ${hwm:,.2f})")
                        _block_outcome(conn, outcome_id, f"circuit_breaker_drawdown:{drawdown_pct*100:.1f}%")
                        continue
                    if open_position_count >= int(p["max_open_positions"]):
                        log.info(
                            f"Skipping buy proposal for {sym}: at max_open_positions "
                            f"({open_position_count}/{int(p['max_open_positions'])})"
                        )
                        _block_outcome(conn, outcome_id, "max_open_positions")
                        continue
                    eb_block = earnings_blackout_reason(conn, sym, p["earnings_blackout_days"])
                    if eb_block:
                        log.info(f"Skipping buy proposal for {sym}: {eb_block}")
                        _block_outcome(conn, outcome_id, eb_block)
                        continue
                    existing_mv = positions.get(sym, {}).get("market_value", 0.0)
                    qty, sizing_note = calc_buy_qty(price, cash, portfolio_value, existing_mv, p_gated)
                    if qty is None:
                        log.info(f"Skipping buy proposal for {sym}: {sizing_note}")
                        _block_outcome(conn, outcome_id, sizing_note)
                        continue
                    sector_block = _sector_cap_block_reason(
                        sym, price, qty, sector_map, positions, portfolio_value, p)
                    if sector_block:
                        log.info(f"Skipping buy proposal for {sym}: {sector_block}")
                        _block_outcome(conn, outcome_id, sector_block)
                        continue
                    regime_note = f" [regime={market_overall}, alloc×{alloc_mod:.0%}]" if alloc_mod != 1.0 else ""
                    rationale = f"{rationale}; sized {qty} shares (~${qty*price:.0f}) — {sizing_note}{regime_note}"
                else:
                    # Only propose sells for long positions we actually hold
                    if sym not in positions or positions[sym]["qty"] < 0:
                        _block_outcome(conn, outcome_id, "no_position_held")
                        continue
                    qty = positions[sym]["qty"]
                    rationale = f"{rationale}; sell full position ({qty} shares)"

                exit_reason = "overbought" if side == "sell" else None
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO trade_proposals (symbol, side, qty, rationale, signal_score, exit_reason)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING id
                    """, (sym, side, qty, rationale, score, exit_reason))
                    proposal_id = cur.fetchone()[0]
                conn.commit()
                log.info(f"PROPOSAL created: {sym} {side} qty={qty} score={score}")
                _propose_outcome(conn, outcome_id, proposal_id)

        except Exception as e:
            log.warning(f"Signals failed for {sym}: {e}")

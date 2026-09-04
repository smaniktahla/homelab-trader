"""
Signal generation: RSI mean reversion + Bollinger Bands + market regime detection.
Based on backtesting research: mean reversion is the only strategy that broadly survives
rigorous walk-forward validation across all asset classes and market conditions.
"""

import json
import logging
import math
import os

import psycopg2.extensions
from psycopg2.extras import Json
import requests

from earnings import earnings_blackout_reason
from circuit_breaker import record_snapshot_and_check, drawdown_size_multiplier
from risk_engine import evaluate_proposal, load_open_risk_dollars
from trading_permission import evaluate_trading_permission
from feature_store import record_symbol_feature_snapshot, attach_feature_snapshot, attach_fundamental_score
from fundamentals import compute_fundamental_score
from hierarchy_regime import snapshot_hierarchy_for_symbol
from regime_scoring import load_regime_scoring_params, compute_regime_adjustment
from market_structure import snapshot_market_structure_for_symbol
from volatility_forecast import load_latest_volatility_forecast, SIZING_ESTIMATOR, SIZING_HORIZON
from structure_scoring import load_structure_scoring_params, compute_structure_adjustment
from relative_strength_risk import load_relative_strength_risk_params, evaluate_relative_strength_risk
from sector_mapping import get_sector_etf
from trade_thesis_stop_resolver import load_stop_resolver_params, resolve_initial_stop_price
from trade_thesis import load_trade_thesis
# trade_thesis_invalidation is imported lazily inside check_thesis_invalidation_sell(),
# not here at module level -- it imports feature_registry.py, which itself imports
# compute_rsi/compute_bollinger from this module (same signals -> X -> feature_registry
# -> signals cycle already worked around for trade_thesis_engine in PR 4).
# trade_thesis_engine is imported lazily inside compute_signals(), not here at
# module level -- feature_registry.py (which trade_thesis_engine.py imports)
# itself imports compute_rsi/compute_bollinger from this module, so a
# top-level import here would be a signals -> trade_thesis_engine ->
# feature_registry -> signals cycle.
from regime_common import load_daily_series, pct_return

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
    "portfolio_stop_loss_pct": 0.05,
    "buy_cooldown_days": 2,
    # Risk engine (shared/risk_engine.py) -- see
    # docs/risk-engine-architecture-reconciliation.md
    "risk_per_trade_pct": 0.01,
    "max_portfolio_open_risk_pct": 0.06,
    "loss_streak_limit": 4,
    # Volatility-sizing overlay (shared/risk_engine.py's volatility_budget
    # candidate) -- VR-2, see docs/volatility-sizing-vr0-reconciliation.md.
    # Off by default; see risk_engine.py::VOLATILITY_SIZING_DEFAULTS for
    # what each key does.
    "volatility_sizing_enabled": 0,
    "volatility_reference_vol": 0.25,
    "volatility_vol_floor": 0.05,
    "volatility_max_multiplier": 1.0,
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


def mean_reversion_thesis_id(conn):
    """signals/signal_outcomes/trade_proposals.thesis_id have been NOT NULL
    since migration 001; this module is entirely the mean_reversion thesis
    (it isn't parameterized by thesis at all), so every insert here uses
    this one lookup rather than threading a thesis slug through everything."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug='mean_reversion'")
        row = cur.fetchone()
    return row[0] if row else None


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

    # Regime's min(score*1.15, 100) cap only bounds the score at that one
    # step -- the ATR modifier runs afterward and can multiply a
    # near-100 score past 100 with nothing downstream to catch it (seen
    # live 2026-07-23: ITW scored 110). Docstring promises 0-100; enforce
    # it for real, once, at the end, rather than threading a cap through
    # every intermediate step.
    score = max(0.0, min(100.0, score))
    return int(score), "; ".join(parts)


def recent_buy_block_reason(conn, symbol, cooldown_days):
    """Return a block reason string if `symbol` had a filled BUY trade within
    cooldown_days, else None. Prevents the scanner from re-proposing (and the
    user re-approving) the same BUY on consecutive cycles just because the
    underlying RSI/BB oversold condition hasn't resolved yet — the position
    was already sized for that signal; a still-oversold RSI the next cycle
    isn't a new opportunity. Explicit tuple cursor regardless of the
    caller's connection default -- now called both from ingest.py's
    tuple-cursor connections and from api/main.py's dict-cursor
    connections (via shared/rule_adherence.py), and this function's own
    row[0] access assumes tuple/positional indexing either way."""
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute("""
            SELECT traded_at FROM trades
            WHERE symbol=%s AND side='buy'
              AND traded_at > NOW() - %s * INTERVAL '1 day'
            ORDER BY traded_at DESC LIMIT 1
        """, (symbol, int(cooldown_days)))
        row = cur.fetchone()
    if row:
        return f"buy_cooldown:last_buy_{row[0].date().isoformat()}"
    return None


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
                     band_std, market_regime, symbol_regime, price, thesis_id):
    """Insert the signal_outcomes stub row for a scored signal. Defaults to blocked
    until the caller marks it proposed. Returns (outcome_id, generated_at) — the
    DB-assigned timestamp is handed back so callers needing a shared "as of" moment
    (e.g. feature_store's symbol_features.as_of) reuse this exact value instead of
    taking a second, independently-drifting datetime.now() reading."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO signal_outcomes
                (signal_id, symbol, side, score, rsi, bb_upper, bb_middle, bb_lower, band_std,
                 market_regime, symbol_regime, price_at_signal, thesis_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id, generated_at
        """, (signal_id, sym, side, score, rsi, bb_upper, bb_middle, bb_lower, band_std,
              market_regime, symbol_regime, price, thesis_id))
        outcome_id, generated_at = cur.fetchone()
    conn.commit()
    return outcome_id, generated_at


def _block_outcome(conn, outcome_id, reason):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE signal_outcomes SET proposal_status='blocked', block_reason=%s
            WHERE id=%s
        """, (reason, outcome_id))
    conn.commit()


def _relative_strength_risk_detail(conn, sym, sector, vs_sector_classification):
    """Human-readable diagnostic for a relative-strength gate rejection --
    "Rejected proposals should remain visible in diagnostics/research
    output, not silently discarded." Deliberately only computed on an
    actual rejection (rare path, evaluate_relative_strength_risk already
    filtered out everything else) rather than for every eligible symbol
    every cycle -- the classification itself is already free (read from
    the hierarchy snapshot computed once per cycle), this only adds one
    extra DB read for the sector ETF's series, and only when actually
    needed. Fails open to a shorter detail string, never raises."""
    etf = get_sector_etf(sector) if sector else None
    if not etf:
        return f"sector={sector or 'unknown'} classification={vs_sector_classification}"
    try:
        _stock_dates, stock_closes = load_daily_series(conn, sym)
        _sector_dates, sector_closes = load_daily_series(conn, etf)
        stock_r20 = pct_return(stock_closes, 20)
        sector_r20 = pct_return(sector_closes, 20)
        if stock_r20 is not None and sector_r20 is not None:
            rel = stock_r20 - sector_r20
            return (f"sector={sector} etf={etf} classification={vs_sector_classification} "
                    f"relative_strength_vs_{etf}={rel:+.1f}%")
    except Exception as e:
        log.warning(f"Relative-strength detail computation failed for {sym}: {e}")
    return f"sector={sector} etf={etf} classification={vs_sector_classification}"


def _record_risk_decision(conn, context, proposal_id, symbol, side, requested_qty, decision, market_overall):
    """Persist one shared/risk_engine.py::evaluate_proposal() result.
    Side-effecting only, never branched on here -- compute_signals()'s
    proposal-creation output (trade_proposals row, qty as the strategy
    sized it) is unchanged by this; risk_decisions is purely an additional,
    parallel record. See docs/risk-engine-architecture-reconciliation.md
    section D for why market_regime_at_decision is audit-only, never an
    input to evaluate_proposal() itself."""
    with conn.cursor() as cur:
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
    conn.commit()


def _propose_outcome(conn, outcome_id, proposal_id):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE signal_outcomes
            SET proposal_status='proposed', proposal_id=%s, approval_status='pending', block_reason=NULL
            WHERE id=%s
        """, (proposal_id, outcome_id))
    conn.commit()


def load_sector_map(conn, symbols):
    """symbol -> GICS sector for the given symbols. Symbols with no sector
    (ETFs, unclassified) are simply absent from the returned dict. Explicit
    tuple cursor regardless of the caller's connection default -- same
    reasoning as recent_buy_block_reason above, now called both from
    ingest.py and api/main.py's rule_adherence path."""
    if not symbols:
        return {}
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute("""
            SELECT symbol, sector FROM universe
            WHERE symbol = ANY(%s) AND sector IS NOT NULL
        """, (list(symbols),))
        return {r[0]: r[1] for r in cur.fetchall()}


def sector_cap_block_reason(sym, price, qty, sector_map, positions, portfolio_value, p):
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
    """Return True if an undecided sell proposal already exists for this
    symbol, OR an already-approved sell hasn't actually settled yet.

    The second check matters: an approved proposal's resulting order can sit
    unfilled at the broker for hours (e.g. submitted after-hours, queued for
    next open) with decision='approved' the whole time -- not "undecided,"
    so the first check alone misses it. Without this, check_symbol_exits
    re-evaluates the same still-true thesis_complete/time_stop condition
    every cycle and creates a fresh duplicate sell proposal for shares that
    are already committed to an in-flight order (seen live 2026-07-29: HAS
    got a second thesis_complete proposal while its first approved sell was
    still sitting unfilled, and approving the duplicate failed against
    Alpaca's qty_available with a confusing error since the position genuinely
    is real, just already spoken for)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id FROM trade_proposals
            WHERE symbol=%s AND side='sell' AND decision IS NULL
        """, (sym,))
        if cur.fetchone() is not None:
            return True
        cur.execute("""
            SELECT id FROM trades
            WHERE symbol=%s AND side='sell'
              AND status NOT IN ('filled', 'canceled', 'expired', 'rejected')
        """, (sym,))
        return cur.fetchone() is not None


def check_stop_losses(conn, positions, p, thesis_id):
    """Create sell proposals for positions that have breached the stop-loss
    threshold. As of "Execution: protective stop orders," buy orders
    attach a real broker-side OTO stop-loss child leg (api/main.py), which
    fires in real time and gets reconciled back into the trades ledger by
    ingest.py's reconcile_broker_stop_fills() -- so in the normal case the
    broker will have already closed a breached position well before this
    function's hourly poll even runs. This stays as a backup safety net
    (positions opened before this shipped with no attached leg, or a case
    where the leg failed to attach) rather than being removed -- it will
    usually just find nothing to do."""
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
                    INSERT INTO trade_proposals (symbol, side, qty, rationale, signal_score, exit_reason, thesis_id)
                    VALUES (%s, 'sell', %s, %s, 99, 'stop_loss', %s)
                """, (sym, pos["qty"], rationale, thesis_id))
            conn.commit()
            log.info(f"Stop-loss PROPOSAL created: sell {pos['qty']} {sym}")


def check_regime_deterioration_sell(conn, positions, market_overall, market_rationale, thesis_id):
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
                INSERT INTO trade_proposals (symbol, side, qty, rationale, signal_score, exit_reason, thesis_id)
                VALUES (%s, 'sell', %s, %s, 92, 'regime_deterioration', %s)
            """, (sym, pos["qty"], rationale, thesis_id))
        conn.commit()
        log.info(f"Regime-deterioration PROPOSAL created: sell {pos['qty']} {sym}")


def check_portfolio_loss_sell(conn, positions, p, thesis_id):
    """Account-wide stop-loss. Measures cumulative loss the same way
    check_stop_losses measures a single position's -- % of cost basis
    (avg_entry * qty), not drawdown from a trailing high-water mark like
    circuit_breaker.py's threshold -- so a slow multi-week bleed and a fast
    multi-day crash of the same total magnitude trigger identically, and
    the two account-wide thresholds stay independently interpretable: this
    one is loss on capital actually deployed; circuit_breaker_drawdown_pct
    is retreat from a peak (and only pauses new entries -- see that
    module's own docstring -- it never sells anything).

    When the combined book crosses portfolio_stop_loss_pct, this proposes
    exiting every position CURRENTLY showing an unrealized loss -- the
    ones actually responsible for the drawdown -- rather than trimming the
    whole book uniformly. A position still green is left alone even while
    the account overall is under water; a position already past its own
    check_stop_losses() threshold is skipped here too, via the same
    _open_sell_exists() dedup every other check_* function in this module
    uses, since that proposal already covers it.

    portfolio_stop_loss_pct is deliberately set BELOW stop_loss_pct
    (default 5% vs. 8%), not above. Absent a gap-down past a check, the
    cost-basis-weighted loss across the whole book is bounded by its
    single worst-held position's own loss -- so a threshold ABOVE
    stop_loss_pct could almost never fire before check_stop_losses()
    already had, on every position simultaneously. Set below it, this
    becomes a genuine backstop: it catches a broad, correlated decline
    across many positions each still individually under their own stop
    (e.g. five positions each down 6%, none of which trips its own 8%
    line), which check_stop_losses() structurally cannot see."""
    if not positions:
        return
    threshold = p["portfolio_stop_loss_pct"]
    longs = {sym: pos for sym, pos in positions.items() if pos["qty"] > 0}
    if not longs:
        return

    total_cost_basis = sum(pos["avg_entry"] * pos["qty"] for pos in longs.values())
    if total_cost_basis <= 0:
        return
    total_unrealized_pl = sum(pos["market_value"] - pos["avg_entry"] * pos["qty"] for pos in longs.values())
    portfolio_loss_pct = -total_unrealized_pl / total_cost_basis
    if portfolio_loss_pct < threshold:
        return

    log.warning(
        f"Portfolio stop-loss triggered: book down {portfolio_loss_pct*100:.1f}% of cost basis "
        f"(threshold {threshold*100:.0f}%) -- exiting positions currently in loss"
    )
    for sym, pos in longs.items():
        if pos["unrealized_plpc"] >= 0:
            continue  # currently profitable -- not part of the loss, leave it
        if _open_sell_exists(conn, sym):
            log.info(f"Portfolio stop-loss proposal for {sym} already open, skipping")
            continue
        rationale = (
            f"PORTFOLIO STOP-LOSS: book down {portfolio_loss_pct*100:.1f}% of cost basis "
            f"(threshold {threshold*100:.0f}%) -- exiting {sym} "
            f"(down {pos['unrealized_plpc']*100:.1f}% from avg entry ${pos['avg_entry']:.2f})"
        )
        log.warning(f"Exit [portfolio_stop_loss]: {rationale}")
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trade_proposals (symbol, side, qty, rationale, signal_score, exit_reason, thesis_id)
                VALUES (%s, 'sell', %s, %s, 96, 'portfolio_stop_loss', %s)
            """, (sym, pos["qty"], rationale, thesis_id))
        conn.commit()
        log.info(f"Portfolio stop-loss PROPOSAL created: sell {pos['qty']} {sym}")


# Exit taxonomy (PR 8, Hypothesis-Driven Trading Architecture epic) --
# per docs/trade-thesis-architecture-reconciliation.md §5's PR 8 bullet:
# normalize/extend exit_reason using the trade_thesis_id threading from
# §2 to distinguish thesis invalidation (structure CHoCH, regime
# deterioration, or the thesis's own invalidation_spec, per PR 5) from a
# blunt price-only emergency stop (check_stop_losses above) or a profit
# target (check_symbol_exits' thesis_complete below). Disabled by default:
# gated separately from trade_thesis_instantiation_enabled (PR 4) so
# turning instantiation on stays purely observational -- creating
# trade_theses rows for audit -- until this flag is ALSO explicitly
# turned on, same staged-rollout discipline as every other PR in this epic.
THESIS_INVALIDATION_EXIT_DEFAULTS = {"thesis_invalidation_exit_enabled": 0}


def load_thesis_invalidation_exit_params(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM signal_params")
            rows = cur.fetchall()
        params = dict(THESIS_INVALIDATION_EXIT_DEFAULTS)
        for row in rows:
            k = row[0] if isinstance(row, (list, tuple)) else row["key"]
            if k not in THESIS_INVALIDATION_EXIT_DEFAULTS:
                continue
            v = row[1] if isinstance(row, (list, tuple)) else row["value"]
            params[k] = float(v)
        return params
    except Exception as e:
        log.warning(f"Could not load thesis invalidation exit params, using defaults: {e}")
        return dict(THESIS_INVALIDATION_EXIT_DEFAULTS)


def _resolve_trade_thesis_id_for_open_position(conn, sym):
    """Best-effort scalar hint (§2a) -- the trade_thesis_id of the most
    recent approved BUY proposal for this symbol, never authoritative
    lot-level ownership (that's position_trades.qty_allocated's job, per
    §2a, once PR 9 wires it). Returns None if no BUY proposal for this
    symbol ever carried one -- thesis instantiation was off when the
    position was opened, or it predates PR 4."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT trade_thesis_id FROM trade_proposals
            WHERE symbol=%s AND side='buy' AND decision='approved' AND trade_thesis_id IS NOT NULL
            ORDER BY proposed_at DESC LIMIT 1
        """, (sym,))
        row = cur.fetchone()
    return row[0] if row else None


def check_thesis_invalidation_sell(conn, positions, thesis_id):
    """For each held long position with a resolvable trade_thesis_id, run
    PR 5's evaluate_thesis_invalidation() and propose an exit distinct
    from check_stop_losses' blunt price-only check -- structure CHoCH,
    regime deterioration, or the thesis's own invalidation_spec can all
    fire before price ever reaches the hard stop level. Positions with no
    resolvable trade_thesis_id are silently skipped -- check_stop_losses/
    check_regime_deterioration_sell remain their only safety nets,
    unchanged."""
    if not positions:
        return
    from trade_thesis_invalidation import evaluate_thesis_invalidation

    for sym, pos in positions.items():
        if pos["qty"] <= 0:
            continue  # short position or already flat
        if _open_sell_exists(conn, sym):
            continue
        trade_thesis_id = _resolve_trade_thesis_id_for_open_position(conn, sym)
        if trade_thesis_id is None:
            continue
        thesis = load_trade_thesis(conn, trade_thesis_id)
        if thesis is None:
            continue
        result = evaluate_thesis_invalidation(conn, thesis)
        if not result.invalidated:
            continue

        gain_pct = pos["unrealized_plpc"] * 100
        rationale = (
            f"THESIS INVALIDATED: {sym} trade_thesis #{trade_thesis_id} -- {', '.join(result.reasons)}. "
            f"Entry ${pos['avg_entry']:.2f} → current ${pos['current_price']:.2f} ({gain_pct:+.1f}%)"
        )
        log.warning(f"Exit [thesis_invalidated]: {rationale}")
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trade_proposals
                    (symbol, side, qty, rationale, signal_score, exit_reason, thesis_id, trade_thesis_id)
                VALUES (%s, 'sell', %s, %s, 93, 'thesis_invalidated', %s, %s)
            """, (sym, pos["qty"], rationale, thesis_id, trade_thesis_id))
        conn.commit()
        log.info(f"Thesis-invalidation PROPOSAL created: sell {pos['qty']} {sym}")


def check_symbol_exits(conn, sym, price, bb_middle, positions, p, thesis_id):
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
                INSERT INTO trade_proposals (symbol, side, qty, rationale, signal_score, exit_reason, thesis_id)
                VALUES (%s, 'sell', %s, %s, 90, 'thesis_complete', %s)
            """, (sym, qty, rationale, thesis_id))
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
                    INSERT INTO trade_proposals (symbol, side, qty, rationale, signal_score, exit_reason, thesis_id)
                    VALUES (%s, 'sell', %s, %s, 85, 'time_stop', %s)
                """, (sym, qty, rationale, thesis_id))
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
    from trade_thesis_engine import load_trade_thesis_engine_params, instantiate_buy_trade_thesis

    p = load_params(conn)
    thesis_id = mean_reversion_thesis_id(conn)
    thesis_invalidation_exit_params = load_thesis_invalidation_exit_params(conn)

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
    # high-water mark, and the resulting drawdown_pct. Whether that alone
    # pauses buys is now decided by trading_permission below (Risk Engine
    # PR 3), not this call's own breaker_active/hwm return values directly.
    _breach_ignored, _hwm_ignored, drawdown_pct = record_snapshot_and_check(
        conn, portfolio_value, p["circuit_breaker_drawdown_pct"])

    # Risk Engine PR 3: the one aggregation point for account-level trading
    # permission -- combines the circuit-breaker drawdown check above with
    # the loss-streak check (shared/trading_permission.py), read-only
    # (doesn't insert its own portfolio_snapshots row, reuses the HWM the
    # call above just recorded). new_entries_allowed replaces the old bare
    # circuit_breaker_active as the buy-gating condition below.
    trading_permission = evaluate_trading_permission(conn, portfolio_value, p)
    drawdown_mult = drawdown_size_multiplier(drawdown_pct or 0.0, p["circuit_breaker_drawdown_pct"])

    # Stop-loss check on existing positions
    check_stop_losses(conn, positions, p, thesis_id)

    # Bear_fear regime: proactively de-risk open longs rather than wait for
    # each position's own stop-loss to trigger one at a time
    check_regime_deterioration_sell(conn, positions, market_overall, market_rationale, thesis_id)

    # Account-wide cumulative loss (cost-basis based, independent of the
    # circuit breaker's HWM-drawdown metric): exit positions currently
    # underwater once the combined book's loss crosses portfolio_stop_loss_pct
    check_portfolio_loss_sell(conn, positions, p, thesis_id)

    # Exit taxonomy (PR 8): dark unless a human has explicitly flipped
    # thesis_invalidation_exit_enabled on, separately from
    # trade_thesis_instantiation_enabled -- see check_thesis_invalidation_sell's
    # own docstring.
    if thesis_invalidation_exit_params["thesis_invalidation_exit_enabled"]:
        check_thesis_invalidation_sell(conn, positions, thesis_id)

    # Count open positions for the max_open_positions gate
    open_position_count = len(positions)

    # Sector map for the sector-concentration cap, covers watchlist + any
    # held position not currently in the watchlist slice being scanned
    sector_map = load_sector_map(conn, set(symbols) | set(positions.keys()))
    regime_params = load_regime_scoring_params(conn)
    structure_params = load_structure_scoring_params(conn)
    rs_risk_params = load_relative_strength_risk_params(conn)
    trade_thesis_engine_params = load_trade_thesis_engine_params(conn)
    stop_resolver_params = load_stop_resolver_params(conn)

    # Portfolio's total planned dollar risk right now, before any of this
    # cycle's proposals -- fed into the risk engine's portfolio-open-risk
    # cap below. Computed once per cycle (same precedent as sector_map
    # above), not re-queried per symbol.
    open_risk_dollars = load_open_risk_dollars(conn)

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
            check_symbol_exits(conn, sym, price, bb_middle, positions, p, thesis_id)

            # Relative strength vs SPY + ATR, computed once per symbol (side-independent)
            rs_pct = relative_strength_vs_spy(conn, sym)
            ohlc = _load_recent_ohlc_from_db(conn, sym, ATR_PERIOD + 10)
            atr = compute_atr(ohlc)

            # Hierarchical regime snapshot + score adjustment -- also
            # side-independent (regime doesn't care whether we're scoring a
            # buy or sell), read from whatever update_hierarchy_regime
            # already computed/stored this cycle rather than recomputed live.
            regime_snapshot = snapshot_hierarchy_for_symbol(conn, sym, sector_map.get(sym), market_overall)
            regime_adj = compute_regime_adjustment(
                market_overall,
                regime_snapshot["sector_regime"]["classification"],
                regime_snapshot["stock_regime"]["vs_sector_classification"],
                regime_params,
            )

            # Market Structure Engine snapshot -- also side-independent,
            # read from whatever update_market_structure already computed/
            # stored this cycle. structure_adj is all-zero unless a human
            # has explicitly flipped structure_scoring_enabled on (default
            # off, same precedent as regime_scoring_enabled) -- see
            # shared/structure_scoring.py.
            market_structure_snapshot = snapshot_market_structure_for_symbol(conn, sym)
            structure_adj = compute_structure_adjustment(market_structure_snapshot, structure_params)

            # Relative-strength risk eligibility filter (shared/relative_strength_risk.py)
            # -- risk control only, never touches score/final_score. Also
            # side-independent (reuses the classification already sitting
            # in regime_snapshot from the hierarchy-regime read above, no
            # extra computation for the common case); only applied on the
            # buy side below. Backed by backtest_results 009/010/011 --
            # see that module's docstring before changing the default.
            vs_sector_classification = regime_snapshot["stock_regime"]["vs_sector_classification"]
            rs_risk_decision = evaluate_relative_strength_risk(vs_sector_classification, rs_risk_params)

            for side in ("buy", "sell"):
                score, rationale = score_signal(rsi, price, bb_upper, bb_lower, band_std, bb_middle,
                                                 regime, side, p, rs_pct=rs_pct, atr=atr)
                if score < p["score_log_min"]:
                    continue

                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO signals (symbol, signal_type, score, rationale, thesis_id)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING id
                    """, (sym, f"rsi_mr_{side}", score, rationale, thesis_id))
                    signal_id = cur.fetchone()[0]
                conn.commit()
                log.info(f"Signal {sym} {side}: score={score} — {rationale}")

                outcome_id, generated_at = _record_outcome(conn, signal_id, sym, side, score, rsi,
                                              bb_upper, bb_middle, bb_lower, band_std,
                                              market_overall, regime, price, thesis_id)

                # Shadow feature snapshot — side-effecting only, never branched on.
                # Runs unconditionally (before any gating check) so blocked signals
                # get shadow features too, not just proposals. Both calls are
                # internally fail-open (see shared/feature_store.py): any failure
                # here is logged and swallowed, never raised, and never alters the
                # score/gating/proposal decision below.
                snap_id = record_symbol_feature_snapshot(conn, sym, side, generated_at, score)
                if snap_id is not None:
                    attach_feature_snapshot(conn, outcome_id, snap_id, score)

                # Shadow fundamental score (PR #2) — same unconditional,
                # side-effecting-only placement as the technical snapshot
                # above. compute_fundamental_score() is a plain DB read
                # (no network call), but wrapped here anyway rather than
                # trusted to never raise, so a read failure can't do
                # anything worse than skip this one component for this one
                # signal — never alters score/gating/proposal below.
                try:
                    fundamental_score = compute_fundamental_score(conn, sym, generated_at)
                except Exception as e:
                    log.warning(f"Fundamental score lookup failed for {sym}: {e}")
                    fundamental_score = None
                if fundamental_score is not None:
                    attach_fundamental_score(conn, snap_id, outcome_id, fundamental_score)

                # final_score == score whenever regime_scoring_enabled AND
                # structure_scoring_enabled are both off (the default) --
                # both compute_*_adjustment functions return all-zero
                # adjustments in that case, so this is a no-op gate change.
                final_score = max(0, min(100, score + regime_adj["total_regime_adjustment"]
                                          + structure_adj["total_structure_adjustment"]))

                if final_score < effective_proposal_min:
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
                trade_thesis_id = None  # PR 4: only ever set on the buy branch below, and only when
                                         # trade_thesis_instantiation_enabled is on -- see shared/trade_thesis_engine.py
                if side == "buy":
                    if not trading_permission["new_entries_allowed"]:
                        reasons = ",".join(trading_permission["reasons"])
                        log.info(f"Skipping buy proposal for {sym}: trading permission denied ({reasons})")
                        _block_outcome(conn, outcome_id, f"trading_permission_denied:{reasons}")
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
                    cd_block = recent_buy_block_reason(conn, sym, p["buy_cooldown_days"])
                    if cd_block:
                        log.info(f"Skipping buy proposal for {sym}: {cd_block}")
                        _block_outcome(conn, outcome_id, cd_block)
                        continue
                    if rs_risk_decision["reject"]:
                        detail = _relative_strength_risk_detail(
                            conn, sym, sector_map.get(sym), vs_sector_classification)
                        log.info(f"Skipping buy proposal for {sym}: {rs_risk_decision['reason']} ({detail})")
                        _block_outcome(conn, outcome_id, f"{rs_risk_decision['reason']}:{detail}")
                        continue
                    existing_mv = positions.get(sym, {}).get("market_value", 0.0)
                    qty, sizing_note = calc_buy_qty(price, cash, portfolio_value, existing_mv, p_gated)
                    if qty is None:
                        log.info(f"Skipping buy proposal for {sym}: {sizing_note}")
                        _block_outcome(conn, outcome_id, sizing_note)
                        continue
                    sector_block = sector_cap_block_reason(
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

                # Platform Improvements PR A: planned risk fields, buy/
                # position-opening proposals only -- sell/exit proposals
                # leave these NULL (not applicable, never coerced to 0).
                # Derived from the SAME stop_loss_pct ratio
                # check_stop_losses() already re-evaluates live every
                # cycle -- this only snapshots what that ratio already
                # implies at proposal time, no new risk-sizing logic.
                planned_entry_price = None
                planned_initial_stop_price = None
                planned_risk_per_share = None
                planned_risk_dollars = None
                if side == "buy":
                    planned_entry_price = price
                    percentage_stop_price = price * (1 - p["stop_loss_pct"])

                    # PR 6 (Structure-Aware Stop Resolver): dark unless a human has
                    # explicitly flipped structure_aware_stop_enabled on (default
                    # off, same precedent as structure_scoring_enabled). Off ->
                    # planned_initial_stop_price is exactly the percentage
                    # calculation this line always did. See
                    # shared/trade_thesis_stop_resolver.py.
                    if stop_resolver_params["structure_aware_stop_enabled"]:
                        planned_initial_stop_price = resolve_initial_stop_price(
                            conn, sym, price, percentage_stop_price, stop_resolver_params)["stop_price"]
                    else:
                        planned_initial_stop_price = percentage_stop_price

                    planned_risk_per_share = price - planned_initial_stop_price
                    planned_risk_dollars = planned_risk_per_share * qty

                    # PR 4 (Evidence Evaluation Engine): dark unless a human has
                    # explicitly flipped trade_thesis_instantiation_enabled on
                    # (default off, same precedent as structure_scoring_enabled).
                    # Failure anywhere inside (grammar, semantic validation,
                    # persistence) returns None -- the proposal below still gets
                    # created, just without a trade_thesis_id, identical to the
                    # flag-off behavior. See shared/trade_thesis_engine.py.
                    if trade_thesis_engine_params["trade_thesis_instantiation_enabled"]:
                        trade_thesis_id = instantiate_buy_trade_thesis(
                            conn, thesis_id, sym, generated_at, p["rsi_oversold"], planned_initial_stop_price)

                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO trade_proposals (
                            symbol, side, qty, rationale, signal_score, exit_reason, thesis_id,
                            trade_thesis_id,
                            planned_entry_price, planned_initial_stop_price,
                            planned_risk_per_share, planned_risk_dollars,
                            regime_snapshot, hierarchy_alignment, base_strategy_score,
                            market_regime_adjustment, sector_regime_adjustment,
                            relative_strength_adjustment, total_regime_adjustment, final_proposal_score,
                            market_structure_snapshot, structure_trend, structure_confidence,
                            structure_score_adjustment
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                    """, (sym, side, qty, rationale, score, exit_reason, thesis_id,
                          trade_thesis_id,
                          planned_entry_price, planned_initial_stop_price,
                          planned_risk_per_share, planned_risk_dollars,
                          Json(regime_snapshot), regime_snapshot["hierarchy_alignment"], score,
                          regime_adj["market_regime_adjustment"], regime_adj["sector_regime_adjustment"],
                          regime_adj["relative_strength_adjustment"], regime_adj["total_regime_adjustment"],
                          final_score,
                          Json(market_structure_snapshot), market_structure_snapshot["trend"],
                          int(round(market_structure_snapshot["confidence"])),
                          structure_adj["total_structure_adjustment"]))
                    proposal_id = cur.fetchone()[0]
                conn.commit()
                log.info(f"PROPOSAL created: {sym} {side} qty={qty} score={score}")
                _propose_outcome(conn, outcome_id, proposal_id)

                # Risk engine: record what it would approve for this
                # proposal's requested qty, right now. Side-effecting only
                # -- never changes qty on the trade_proposals row above.
                # decide_proposal()/execute_trade() re-evaluate again at
                # approval time (portfolio state may have moved since),
                # this is the proposal-time record for audit/comparison.
                if side == "buy":
                    try:
                        # VR-2: only bother loading a forecast when the
                        # overlay is actually enabled -- p.get(...) rather
                        # than p[...] since older signal_params rows/tests
                        # may not have this key at all, in which case the
                        # overlay is inert regardless (see risk_engine.py).
                        volatility_forecast = None
                        if p.get("volatility_sizing_enabled"):
                            volatility_forecast = load_latest_volatility_forecast(
                                conn, sym, SIZING_ESTIMATOR, SIZING_HORIZON
                            )
                        decision = evaluate_proposal(
                            sym, price, qty, planned_initial_stop_price,
                            cash, portfolio_value, positions, sector_map,
                            open_risk_dollars, p, drawdown_multiplier=drawdown_mult,
                            volatility_forecast=volatility_forecast,
                        )
                        _record_risk_decision(conn, "proposal_generated", proposal_id, sym, side,
                                               qty, decision, market_overall)
                    except Exception as e:
                        log.warning(f"Risk engine evaluation failed for {sym}: {e}")

        except Exception as e:
            log.warning(f"Signals failed for {sym}: {e}")
            # A DB constraint violation (or any error mid-transaction)
            # poisons this connection until rolled back -- every subsequent
            # statement on it fails with "current transaction is aborted,"
            # silently breaking every symbol processed after this one in
            # the same cycle. This is the exact mechanism behind the
            # 2026-07-21 8-day silent outage (thesis_id NOT NULL
            # violation). The per-symbol try/except looked like isolation
            # but wasn't, since nothing ever cleared the poisoned state.
            try:
                conn.rollback()
            except Exception:
                pass

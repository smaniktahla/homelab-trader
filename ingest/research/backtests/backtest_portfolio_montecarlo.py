#!/usr/bin/env python3
"""Experiment 003: portfolio-level walk-forward Monte Carlo backtest.

Research artifact, not production logic — same posture as Experiment 002
(backtest_score_calibration.py): reuses signals.py's real scoring functions
directly (imported, not reimplemented) so the backtest can't drift from
what's actually live.

Experiment 002 answers "does an individual signal have edge" via per-signal
forward returns. This answers a different question: "if I'd rewound to a
random date and let the whole system run — position sizing, max open
positions, sector caps, circuit breaker, market-regime gating, the full
exit ladder — what would my portfolio have actually done?" That's
explicitly what Experiment 002's docstring listed as not-yet-included, in
this order: sector-relative returns, position sizing, sector caps, earnings
blackout, circuit breaker, slippage/commission modeling, full rolling
walk-forward. This experiment adds position sizing, sector caps, circuit
breaker, and full walk-forward; earnings blackout is explicitly skipped
(see below) and slippage/commission modeling is still future work.

Picks N random historical start dates, and for each one walks forward
`HORIZON_DAYS` trading days with a simulated portfolio (no Alpaca, no live
DB writes) that mirrors compute_signals()'s gate order:
  circuit breaker -> max_open_positions -> [earnings blackout: skipped]
  -> position sizing (calc_buy_qty) -> sector cap (sector_cap_block_reason)
and the same exit ladder: stop_loss -> regime_deterioration_sell ->
thesis_complete / time_stop -> overbought (sell-side score_signal).

Every proposal that clears the regime-adjusted score_proposal_min is
auto-approved (there's no human in a historical replay) — see the plan
doc for why, and for the intent to make this policy swappable later.

Reused as-is from signals.py (pure, no DB/network side effects):
  compute_rsi, compute_bollinger, compute_atr, detect_regime, score_signal,
  load_params, calc_buy_qty, sector_cap_block_reason, RS_LOOKBACK_DAYS,
  ATR_PERIOD.
Reused as-is from market_regime.py (pure):
  _classify_trend, _classify_vix, classify_overall, SMA_FAST, SMA_SLOW,
  VIX_CALM, VIX_FEAR. classify_overall is the overall bull/bear x
  VIX-bucket table itself — historical_market_context() below now just
  feeds it historical arrays, it no longer hand-duplicates the cascade.
Reused as-is from market_regime_history.py (pure): asof_index.

NOT reusable directly (DB-writing / datetime.now()-coupled in production),
so mirrored here as pure functions operating on simulated state instead:
  check_stop_losses, check_symbol_exits, check_regime_deterioration_sell.

Documented simplifications (same spirit as Experiment 002's own gap list):
  - Universe/watchlist composition uses TODAY's scannable universe (S&P 500
    + core ETFs) for the entire backtest window — survivorship bias, not
    historical index reconstruction.
  - Earnings blackout is skipped — no reliable free historical earnings
    calendar (Finnhub's free tier is forward-looking only).
  - Sector caps use the CURRENT GICS sector mapping — sectors rarely
    change, low risk.
  - Fills are at same-day close, no slippage/commission modeling —
    consistent with how signal_outcomes/Experiment 002 already model
    MAE/MFE off daily OHLC.
  - Requires ^VIX in price_history (run backfill_vix.py once first) —
    without it every day falls back to vix_regime='unknown' -> fail-closed
    'elevated' bucket, same fail-closed behavior compute_market_regime()
    uses live, but silently pessimistic if you forgot the backfill.

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 research/backtests/backtest_portfolio_montecarlo.py
"""

import os
import sys
import json
import logging
import random
import statistics
from datetime import datetime, timezone

import psycopg2

sys.path.insert(0, "/app")
from signals import (compute_rsi, compute_bollinger, compute_atr, detect_regime,
                      score_signal, load_params, calc_buy_qty, sector_cap_block_reason,
                      RS_LOOKBACK_DAYS, ATR_PERIOD)
from market_regime import _classify_trend, _classify_vix, classify_overall, SMA_FAST, SMA_SLOW, VIX_CALM, VIX_FEAR
from market_regime_history import asof_index
from risk_engine import evaluate_proposal
from circuit_breaker import drawdown_pct_of, is_breached, drawdown_size_multiplier
from security_regime import classify_security_regime
from sector_mapping import get_sector_etf
from db_utils import save_backtest_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EXPERIMENT_ID = "003_portfolio_montecarlo"
GIT_COMMIT = os.environ.get("BACKTEST_GIT_COMMIT", "unknown")

DB_DSN = os.environ["DATABASE_URL"]
WINDOW = 260               # trailing trading days fed to RSI/BB/regime/ATR each step (matches Experiment 002)
HORIZON_DAYS = int(os.environ.get("BACKTEST_HORIZON_DAYS", "60"))
MC_RUNS = int(os.environ.get("BACKTEST_MC_RUNS", "40"))
STARTING_CASH = float(os.environ.get("BACKTEST_STARTING_CASH", "100000"))
TIME_STOP_TRADING_DAYS = 20  # hardcoded in signals.py's check_symbol_exits, mirrored here


# ─────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(DB_DSN)


def get_universe_symbols(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM universe WHERE scannable=TRUE ORDER BY symbol")
        return [r[0] for r in cur.fetchall()]


def get_sector_map(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol, sector FROM universe WHERE sector IS NOT NULL")
        return {r[0]: r[1] for r in cur.fetchall()}


def load_sector_series(conn, sector_map, load_series_fn):
    """etf symbol -> series, one load per distinct sector ETF actually
    represented in sector_map. Only needed by callers that pass an
    rs_policy to run_single_backtest() (relative-strength gating/sizing) --
    harmless to load unconditionally, it's just 10-15 ETF series."""
    sector_series = {}
    for sector in set(sector_map.values()):
        etf = get_sector_etf(sector)
        if etf and etf not in sector_series:
            sector_series[etf] = load_series_fn(conn, etf)
    return sector_series


def load_series(conn, symbol):
    """dates (ascending, python date objects), closes, highs, lows."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DATE(ts), close, high, low FROM price_history
            WHERE symbol=%s ORDER BY ts ASC
        """, (symbol,))
        rows = cur.fetchall()
    dates = [r[0] for r in rows]
    closes = [float(r[1]) for r in rows]
    highs = [float(r[2]) if r[2] is not None else float(r[1]) for r in rows]
    lows = [float(r[3]) if r[3] is not None else float(r[1]) for r in rows]
    return dates, closes, highs, lows


# ─────────────────────────────────────────────────────────────────────────
# Pure helpers mirroring signal-generation inputs (relative strength vs SPY)
# ─────────────────────────────────────────────────────────────────────────

def relative_strength(idx, closes, spy_dates, spy_closes, current_date, lookback=RS_LOOKBACK_DAYS):
    if idx is None or idx < lookback:
        return None
    spy_pos = asof_index(spy_dates, current_date)
    if spy_pos is None or spy_pos < lookback:
        return None
    sym_ret = (closes[idx] - closes[idx - lookback]) / closes[idx - lookback] * 100
    spy_ret = (spy_closes[spy_pos] - spy_closes[spy_pos - lookback]) / spy_closes[spy_pos - lookback] * 100
    return sym_ret - spy_ret


def vs_sector_classification(sym, idx, dates, closes, sector_map, sector_series, spy_series):
    """As-of stock-vs-sector relative strength, via the exact pure
    classify_security_regime() the live hierarchical regime system and
    Experiments 007/009 already use -- same function, fed a historical
    slice instead of a live/backfilled DB read. "unknown" (never gated/
    resized) whenever the sector is unmapped or its ETF has no series."""
    sector = sector_map.get(sym)
    etf = get_sector_etf(sector) if sector else None
    if not etf or etf not in sector_series:
        return "unknown"
    sector_dates, sector_closes, _, _ = sector_series[etf]
    spy_dates, spy_closes, _, _ = spy_series
    target_date = dates[idx]
    stock_closes_upto = closes[:idx + 1]
    sec_i = asof_index(sector_dates, target_date)
    sector_closes_upto = sector_closes[:sec_i + 1] if sec_i is not None else []
    spy_i = asof_index(spy_dates, target_date)
    spy_closes_upto = spy_closes[:spy_i + 1] if spy_i is not None else []
    ctx = classify_security_regime(stock_closes_upto, sector_closes_upto, spy_closes_upto)
    return ctx["vs_sector_classification"]


# ─────────────────────────────────────────────────────────────────────────
# Pure historical market regime — mirrors market_regime.compute_market_regime(),
# reusing its classification primitives AND its overall-regime cascade
# (classify_overall) directly, just fed historical arrays instead of a
# live fetch. The cascade itself used to be hand-duplicated here; now both
# paths funnel through the same function so they can't drift.
# ─────────────────────────────────────────────────────────────────────────

def historical_market_context(spy_closes_upto, qqq_closes_upto, vix_level):
    spy_trend, _, _, _ = _classify_trend(spy_closes_upto) if len(spy_closes_upto) >= SMA_FAST else ("unknown", None, None, None)
    qqq_trend, _, _, _ = _classify_trend(qqq_closes_upto) if len(qqq_closes_upto) >= SMA_FAST else ("unknown", None, None, None)
    vix_regime, _ = _classify_vix(vix_level)

    overall, score_modifier, alloc_modifier, _rationale = classify_overall(spy_trend, qqq_trend, vix_regime)
    return overall, score_modifier, alloc_modifier


# ─────────────────────────────────────────────────────────────────────────
# Pure exit-rule mirrors (signals.py's check_stop_losses / check_symbol_exits
# / check_regime_deterioration_sell, without the DB/Alpaca/datetime.now()
# coupling — same rules, params from load_params).
# ─────────────────────────────────────────────────────────────────────────

def stop_loss_hit(position, price, p):
    loss_pct = (position["avg_entry"] - price) / position["avg_entry"]
    return loss_pct >= p["stop_loss_pct"]


def thesis_complete(price, bb_middle):
    return bb_middle is not None and price >= bb_middle


def time_stop_hit(entry_date, current_date):
    calendar_days = (current_date - entry_date).days
    approx_trading_days = int(calendar_days * 5 / 7)
    return approx_trading_days >= TIME_STOP_TRADING_DAYS


# ─────────────────────────────────────────────────────────────────────────
# Simulated portfolio ledger
# ─────────────────────────────────────────────────────────────────────────

def portfolio_value(cash, positions, price_lookup):
    return cash + sum(pos["qty"] * price_lookup(sym) for sym, pos in positions.items())


def execute_buy(ledger, sym, qty, price, current_date, score, rationale, trade_log, risk_dollars=None):
    cost = qty * price
    ledger["cash"] -= cost
    ledger["positions"][sym] = {
        "qty": qty, "avg_entry": price, "entry_date": current_date,
        "risk_dollars": risk_dollars,  # feeds risk_engine.evaluate_proposal's open_risk_dollars for later buys
    }
    trade_log.append({"date": str(current_date), "symbol": sym, "side": "buy", "qty": qty,
                       "price": round(price, 2), "score": score, "rationale": rationale})


def execute_sell(ledger, sym, price, current_date, reason, trade_log):
    pos = ledger["positions"].pop(sym)
    proceeds = pos["qty"] * price
    ledger["cash"] += proceeds
    realized_pct = (price - pos["avg_entry"]) / pos["avg_entry"] * 100
    trade_log.append({"date": str(current_date), "symbol": sym, "side": "sell", "qty": pos["qty"],
                       "price": round(price, 2), "exit_reason": reason,
                       "realized_return_pct": round(realized_pct, 2)})


# ─────────────────────────────────────────────────────────────────────────
# Single walk-forward run
# ─────────────────────────────────────────────────────────────────────────

def run_single_backtest(symbols, series, spy_series, qqq_series, vix_series, sector_map, p, start_i, horizon_days,
                         rs_policy=None, sector_series=None):
    """rs_policy=None (default) reproduces Experiment 003's exact existing
    behavior byte-for-byte -- every line below that references rs_policy is
    a no-op in that case. rs_policy is an optional dict enabling a
    relative-strength gate/resize on top of the identical baseline pipeline
    (same score threshold, same sector cap, same risk engine, same exits):
      {"mode": "gate"} -- skip any BUY where the stock is underperforming
        its own sector (vs_sector_classification) entirely.
      {"mode": "reduce_size", "size_multiplier": 0.5} -- still buy, but at
        `size_multiplier` of the size the identical baseline sizing/risk-
        engine pipeline would have produced.
    sector_series (etf -> series) is required whenever rs_policy is set;
    see load_sector_series()."""
    spy_dates, spy_closes, _, _ = spy_series
    qqq_dates, qqq_closes, _, _ = qqq_series
    vix_dates, vix_closes, _, _ = vix_series

    calendar_dates = spy_dates[start_i:start_i + horizon_days + 1]
    start_date, end_date = calendar_dates[0], calendar_dates[-1]

    ledger = {"cash": STARTING_CASH, "positions": {}}
    trade_log = []
    equity_curve = []
    high_water_mark = STARTING_CASH

    def price_of(sym, current_date):
        dates, closes, _, _ = series[sym]
        idx = asof_index(dates, current_date)
        return closes[idx] if idx is not None else None

    for day_idx, current_date in enumerate(calendar_dates):
        spy_i = asof_index(spy_dates, current_date)
        qqq_i = asof_index(qqq_dates, current_date)
        vix_i = asof_index(vix_dates, current_date)
        vix_level = vix_closes[vix_i] if vix_i is not None else None
        market_overall, score_mod, alloc_mod = historical_market_context(
            spy_closes[:spy_i + 1] if spy_i is not None else [],
            qqq_closes[:qqq_i + 1] if qqq_i is not None else [],
            vix_level,
        )
        effective_proposal_min = p["score_proposal_min"] + score_mod
        p_gated = dict(p)
        p_gated["trade_allocation_pct"] = p["trade_allocation_pct"] * alloc_mod

        # ── Mark-to-market + circuit breaker ────────────────────────────
        # drawdown_pct_of/is_breached (circuit_breaker.py) replace what
        # used to be this loop's own hand-copy of the same formula --
        # Risk Engine PR 3 fixed the same duplication in
        # rule_adherence.py's check_gates(), this is the third and last
        # independent copy in the codebase.
        positions_mv = {s: pos["qty"] * (price_of(s, current_date) or 0) for s, pos in ledger["positions"].items()}
        current_value = ledger["cash"] + sum(positions_mv.values())
        high_water_mark = max(high_water_mark, current_value)
        drawdown_pct = drawdown_pct_of(current_value, high_water_mark)
        circuit_breaker_active = is_breached(drawdown_pct, p["circuit_breaker_drawdown_pct"])
        # cash + per-sector exposure recorded alongside value -- purely
        # additive vs. Experiment 003's original equity_curve shape (extra
        # dict keys), needed downstream for capital-deployed/sector-
        # concentration metrics without re-deriving them from trade_log.
        sector_exposure = {}
        for s, mv in positions_mv.items():
            sec = sector_map.get(s, "Unknown")
            sector_exposure[sec] = sector_exposure.get(sec, 0.0) + mv
        equity_curve.append({
            "date": str(current_date), "value": round(current_value, 2),
            "cash": round(ledger["cash"], 2),
            "sector_exposure": {k: round(v, 2) for k, v in sector_exposure.items()},
        })

        # Symbols sold at any point today are never re-bought today — in
        # production, a sell only ever becomes a PROPOSAL mid-cycle (a human
        # approves it later), so the same compute_signals() cycle that sells
        # a position is working off a positions snapshot from before that
        # sell and can never re-buy it same-cycle. Executing sells
        # immediately (no human in the loop here) would otherwise let a
        # symbol round-trip same-day, which production can't do.
        sold_today = set()

        # ── Global exit checks on held positions: stop-loss first ──────
        for sym in list(ledger["positions"].keys()):
            price = price_of(sym, current_date)
            if price is None:
                continue
            pos = ledger["positions"][sym]
            if stop_loss_hit(pos, price, p):
                execute_sell(ledger, sym, price, current_date, "stop_loss", trade_log)
                sold_today.add(sym)

        # ── Regime-deterioration de-risking (bear_fear only) ───────────
        if market_overall == "bear_fear":
            for sym in list(ledger["positions"].keys()):
                price = price_of(sym, current_date)
                if price is None:
                    continue
                execute_sell(ledger, sym, price, current_date, "regime_deterioration", trade_log)
                sold_today.add(sym)

        # ── Per-symbol: thesis-complete/time-stop exits, then new signals ─
        for sym in symbols:
            dates, closes, highs, lows = series[sym]
            idx = asof_index(dates, current_date)
            if idx is None or idx < WINDOW - 1:
                continue
            lo = idx - WINDOW + 1
            window_closes = closes[lo:idx + 1]
            window_ohlc = list(zip(highs[lo:idx + 1], lows[lo:idx + 1], closes[lo:idx + 1]))
            price = closes[idx]

            bb_upper, bb_middle, bb_lower, band_std = compute_bollinger(window_closes, p["bb_period"], p["bb_std"])

            if sym in ledger["positions"]:
                pos = ledger["positions"][sym]
                if thesis_complete(price, bb_middle):
                    execute_sell(ledger, sym, price, current_date, "thesis_complete", trade_log)
                    sold_today.add(sym)
                elif time_stop_hit(pos["entry_date"], current_date):
                    execute_sell(ledger, sym, price, current_date, "time_stop", trade_log)
                    sold_today.add(sym)

            rsi = compute_rsi(window_closes, p["rsi_period"])
            regime = detect_regime(window_closes, p["regime_sma_fast"], p["regime_sma_slow"], p["regime_band"])
            atr = compute_atr(window_ohlc, ATR_PERIOD)
            rs_pct = relative_strength(idx, closes, spy_dates, spy_closes, current_date)

            # ── Sell-side score (overbought exit) — only if still held ──
            if sym in ledger["positions"]:
                sell_score, sell_rationale = score_signal(rsi, price, bb_upper, bb_lower, band_std, bb_middle,
                                                           regime, "sell", p, rs_pct=rs_pct, atr=atr)
                if sell_score >= effective_proposal_min:
                    execute_sell(ledger, sym, price, current_date, "overbought", trade_log)
                    sold_today.add(sym)
                continue  # don't also evaluate a buy for a symbol we just exited/hold

            # ── Buy-side gate pipeline (mirrors compute_signals order) ──
            if sym in sold_today:
                continue  # no same-day round-trip — production can't sell and re-buy in one cycle either
            if circuit_breaker_active:
                continue
            if len(ledger["positions"]) >= int(p["max_open_positions"]):
                continue
            buy_score, buy_rationale = score_signal(rsi, price, bb_upper, bb_lower, band_std, bb_middle,
                                                     regime, "buy", p, rs_pct=rs_pct, atr=atr)
            if buy_score < effective_proposal_min:
                continue
            # earnings blackout: skipped (see module docstring)

            # Relative-strength gate/resize -- no-op (rs_size_multiplier
            # stays 1.0) unless rs_policy was explicitly passed in. Placed
            # right after the score gate and before sizing/sector-cap/risk-
            # engine, same "another gate in the pipeline" position as every
            # other check here -- the rest of the pipeline is byte-for-byte
            # identical to the rs_policy=None baseline either way.
            rs_size_multiplier = 1.0
            if rs_policy is not None:
                vs_sector = vs_sector_classification(sym, idx, dates, closes, sector_map, sector_series, spy_series)
                if vs_sector == "underperforming_sector":
                    if rs_policy["mode"] == "gate":
                        continue
                    elif rs_policy["mode"] == "reduce_size":
                        rs_size_multiplier = rs_policy.get("size_multiplier", 0.5)

            cur_value = portfolio_value(ledger["cash"], ledger["positions"], lambda s: price_of(s, current_date) or 0)
            requested_qty, _sizing_note = calc_buy_qty(price, ledger["cash"], cur_value, 0.0, p_gated)
            if requested_qty is None:
                continue
            if rs_size_multiplier != 1.0:
                requested_qty = max(1, int(requested_qty * rs_size_multiplier))
            # sector_cap_block_reason (signals.py) expects each position to
            # carry a precomputed market_value, matching the shape
            # get_positions() returns live from Alpaca. The simulated
            # ledger only stores qty/avg_entry/entry_date/risk_dollars, so
            # shape a view with today's mark-to-market value rather than
            # changing what the ledger itself stores (avg_entry/entry_date
            # are read elsewhere assuming the flat shape).
            positions_with_mv = {
                s: {**pos, "market_value": pos["qty"] * (price_of(s, current_date) or 0)}
                for s, pos in ledger["positions"].items()
            }
            sector_block = sector_cap_block_reason(sym, price, requested_qty, sector_map, positions_with_mv, cur_value, p)
            if sector_block:
                continue

            # Risk engine (Platform Improvements: risk engine PR 1) --
            # calc_buy_qty's allocation-based qty above becomes the
            # strategy's requested_qty, same as live compute_signals()
            # treats it; the risk engine may shrink it further on
            # risk-budget/portfolio-open-risk grounds. sector_map is
            # deliberately NOT passed here (empty dict) -- the block-if-
            # would-breach sector_cap_block_reason() check just above
            # remains the sole sector gate in the backtest, preserving its
            # exact existing "skip the trade entirely on any breach"
            # semantics rather than silently switching to the risk
            # engine's own "reduce qty to fit" sector behavior, which is a
            # different policy not yet validated for this backtest.
            planned_stop = price * (1 - p["stop_loss_pct"])
            open_risk_dollars = sum(
                (pos.get("risk_dollars") or 0.0) for pos in ledger["positions"].values()
            )
            # Risk Engine PR 3: same drawdown-based sizing taper live uses --
            # loss-streak halting is NOT mirrored here (would need the
            # backtest to track its own simulated closed-lifecycle streak;
            # left as a documented gap, not silently diverging).
            drawdown_mult = drawdown_size_multiplier(drawdown_pct, p["circuit_breaker_drawdown_pct"])
            decision = evaluate_proposal(
                sym, price, requested_qty, planned_stop, ledger["cash"], cur_value,
                positions_with_mv, {}, open_risk_dollars, p, drawdown_multiplier=drawdown_mult,
            )
            qty = decision["approved_quantity"]
            if qty < 1:
                continue
            risk_dollars = (price - planned_stop) * qty
            execute_buy(ledger, sym, qty, price, current_date, buy_score, buy_rationale, trade_log,
                        risk_dollars=risk_dollars)

    final_value = portfolio_value(ledger["cash"], ledger["positions"], lambda s: price_of(s, end_date) or 0)
    total_return_pct = (final_value - STARTING_CASH) / STARTING_CASH * 100
    spy_start_i = asof_index(spy_dates, start_date)
    spy_end_i = asof_index(spy_dates, end_date)
    spy_return_pct = ((spy_closes[spy_end_i] - spy_closes[spy_start_i]) / spy_closes[spy_start_i] * 100
                       if spy_start_i is not None and spy_end_i is not None else None)

    peak = STARTING_CASH
    max_drawdown_pct = 0.0
    for pt in equity_curve:
        peak = max(peak, pt["value"])
        max_drawdown_pct = max(max_drawdown_pct, (peak - pt["value"]) / peak * 100 if peak else 0.0)

    return {
        "start_date": str(start_date), "end_date": str(end_date),
        "starting_cash": STARTING_CASH, "final_value": round(final_value, 2),
        "total_return_pct": round(total_return_pct, 2),
        "spy_return_pct": round(spy_return_pct, 2) if spy_return_pct is not None else None,
        "excess_vs_spy_pct": round(total_return_pct - spy_return_pct, 2) if spy_return_pct is not None else None,
        "n_trades": len(trade_log), "max_drawdown_pct": round(max_drawdown_pct, 2),
        "trade_log": trade_log,
        "equity_curve": equity_curve,
    }


# ─────────────────────────────────────────────────────────────────────────
# Monte Carlo driver
# ─────────────────────────────────────────────────────────────────────────

def eligible_start_indices(spy_dates, horizon_days):
    return list(range(WINDOW - 1, len(spy_dates) - horizon_days - 1))


def main():
    conn = get_db()
    p = load_params(conn)
    log.info(f"Live params: score_proposal_min={p['score_proposal_min']} stop_loss_pct={p['stop_loss_pct']} "
              f"max_open_positions={p['max_open_positions']} trade_allocation_pct={p['trade_allocation_pct']}")

    symbols = get_universe_symbols(conn)
    sector_map = get_sector_map(conn)
    log.info(f"Loading price history for {len(symbols)} universe symbols + SPY/QQQ/^VIX...")

    series = {}
    for sym in symbols:
        series[sym] = load_series(conn, sym)
    spy_series = series.get("SPY") or load_series(conn, "SPY")
    qqq_series = series.get("QQQ") or load_series(conn, "QQQ")
    vix_series = load_series(conn, "^VIX")
    conn.close()

    if not vix_series[0]:
        log.error("No ^VIX history in price_history — run backfill_vix.py first. "
                   "Continuing anyway; market regime will fail-closed to 'elevated' every day.")
    if not spy_series[0]:
        log.error("No SPY history — cannot compute market regime or benchmark. Aborting.")
        sys.exit(1)

    spy_dates = spy_series[0]
    starts = eligible_start_indices(spy_dates, HORIZON_DAYS)
    if len(starts) < MC_RUNS:
        log.warning(f"Only {len(starts)} eligible start dates for {HORIZON_DAYS}d horizon; running all of them")
    chosen = random.sample(starts, min(MC_RUNS, len(starts)))
    log.info(f"Running {len(chosen)} Monte Carlo backtests, {HORIZON_DAYS}d horizon each, "
              f"${STARTING_CASH:,.0f} starting cash")

    runs = []
    for n, start_i in enumerate(chosen, 1):
        result = run_single_backtest(symbols, series, spy_series, qqq_series, vix_series, sector_map, p,
                                      start_i, HORIZON_DAYS)
        runs.append(result)
        log.info(f"[{n}/{len(chosen)}] {result['start_date']}..{result['end_date']}: "
                  f"return={result['total_return_pct']:+.2f}% spy={result['spy_return_pct']:+.2f}% "
                  f"trades={result['n_trades']} max_dd={result['max_drawdown_pct']:.2f}%")

    returns = [r["total_return_pct"] for r in runs]
    excess = [r["excess_vs_spy_pct"] for r in runs if r["excess_vs_spy_pct"] is not None]
    beat_spy = sum(1 for r in runs if r["excess_vs_spy_pct"] is not None and r["excess_vs_spy_pct"] > 0)

    report = {
        "experiment_id": EXPERIMENT_ID,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": GIT_COMMIT,
        "config": {"horizon_days": HORIZON_DAYS, "mc_runs": len(chosen), "window_days": WINDOW,
                   "starting_cash": STARTING_CASH, "params_used": p},
        "universe_size": len(symbols),
        "data_date_range": [str(spy_dates[0]), str(spy_dates[-1])],
        "summary": {
            "mean_return_pct": round(statistics.mean(returns), 2),
            "median_return_pct": round(statistics.median(returns), 2),
            "stdev_return_pct": round(statistics.stdev(returns), 2) if len(returns) > 1 else 0.0,
            "mean_spy_return_pct": round(statistics.mean(r["spy_return_pct"] for r in runs if r["spy_return_pct"] is not None), 2),
            "mean_excess_vs_spy_pct": round(statistics.mean(excess), 2) if excess else None,
            "pct_runs_beating_spy": round(100 * beat_spy / len(excess), 1) if excess else None,
            "mean_trades_per_run": round(statistics.mean(r["n_trades"] for r in runs), 1),
            "mean_max_drawdown_pct": round(statistics.mean(r["max_drawdown_pct"] for r in runs), 2),
            "worst_max_drawdown_pct": round(max(r["max_drawdown_pct"] for r in runs), 2),
        },
        "runs": runs,
    }

    out_path = "/tmp/backtest_results_003.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info(f"Full results written to {out_path}")

    s = report["summary"]
    save_backtest_result(EXPERIMENT_ID, GIT_COMMIT, report,
                          summary=f"mean_return={s['mean_return_pct']:+.2f}% vs_spy={s['mean_excess_vs_spy_pct']:+.2f}% "
                                  f"beat_spy={s['pct_runs_beating_spy']}% max_dd={s['worst_max_drawdown_pct']:.2f}%")
    log.info("Results also saved to backtest_results table")

    print(f"\n=== Experiment {EXPERIMENT_ID} (commit {GIT_COMMIT[:8]}) ===")
    print(f"Universe: {len(symbols)} symbols | Data: {report['data_date_range'][0]} to {report['data_date_range'][1]}")
    print(f"{len(chosen)} runs, {HORIZON_DAYS}-day horizon, ${STARTING_CASH:,.0f} starting cash each")
    print(f"\nMean return:       {s['mean_return_pct']:+.2f}%  (median {s['median_return_pct']:+.2f}%, stdev {s['stdev_return_pct']:.2f}%)")
    print(f"Mean SPY return:   {s['mean_spy_return_pct']:+.2f}%  (same windows, buy & hold)")
    print(f"Mean excess:       {s['mean_excess_vs_spy_pct']:+.2f}%  |  runs beating SPY: {s['pct_runs_beating_spy']}%")
    print(f"Mean trades/run:   {s['mean_trades_per_run']}")
    print(f"Mean max drawdown: {s['mean_max_drawdown_pct']:.2f}%  (worst: {s['worst_max_drawdown_pct']:.2f}%)")
    print(f"\nPer-run detail:")
    for r in runs:
        print(f"  {r['start_date']}..{r['end_date']}: return={r['total_return_pct']:>+7.2f}%  "
              f"spy={r['spy_return_pct']:>+7.2f}%  excess={r['excess_vs_spy_pct']:>+7.2f}%  "
              f"trades={r['n_trades']:>3}  max_dd={r['max_drawdown_pct']:>6.2f}%")


if __name__ == "__main__":
    main()

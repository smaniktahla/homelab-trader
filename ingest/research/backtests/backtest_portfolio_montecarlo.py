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
  -> position sizing (calc_buy_qty) -> sector cap (_sector_cap_block_reason)
and the same exit ladder: stop_loss -> regime_deterioration_sell ->
thesis_complete / time_stop -> overbought (sell-side score_signal).

Every proposal that clears the regime-adjusted score_proposal_min is
auto-approved (there's no human in a historical replay) — see the plan
doc for why, and for the intent to make this policy swappable later.

Reused as-is from signals.py (pure, no DB/network side effects):
  compute_rsi, compute_bollinger, compute_atr, detect_regime, score_signal,
  load_params, calc_buy_qty, _sector_cap_block_reason, RS_LOOKBACK_DAYS,
  ATR_PERIOD.
Reused as-is from market_regime.py (pure):
  _classify_trend, _classify_vix, SMA_FAST, SMA_SLOW, VIX_CALM, VIX_FEAR.

NOT reusable directly (DB-writing / datetime.now()-coupled in production),
so mirrored here as pure functions operating on simulated state instead:
  check_stop_losses, check_symbol_exits, check_regime_deterioration_sell,
  and compute_market_regime()'s overall bull/bear x VIX-bucket table (the
  classification primitives it calls ARE reused; only the "now" plumbing
  around them is reimplemented).

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
import bisect
import json
import logging
import random
import statistics
from datetime import datetime, timezone

import psycopg2

sys.path.insert(0, "/app")
from signals import (compute_rsi, compute_bollinger, compute_atr, detect_regime,
                      score_signal, load_params, calc_buy_qty, _sector_cap_block_reason,
                      RS_LOOKBACK_DAYS, ATR_PERIOD)
from market_regime import _classify_trend, _classify_vix, SMA_FAST, SMA_SLOW, VIX_CALM, VIX_FEAR
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


def asof_index(dates, target_date):
    """Index of the last date <= target_date, or None if no such date."""
    idx = bisect.bisect_right(dates, target_date) - 1
    return idx if idx >= 0 else None


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


# ─────────────────────────────────────────────────────────────────────────
# Pure historical market regime — mirrors market_regime.compute_market_regime(),
# reusing its classification primitives, just fed historical arrays instead
# of a live fetch.
# ─────────────────────────────────────────────────────────────────────────

def historical_market_context(spy_closes_upto, qqq_closes_upto, vix_level):
    spy_trend, _, _, _ = _classify_trend(spy_closes_upto) if len(spy_closes_upto) >= SMA_FAST else ("unknown", None, None, None)
    qqq_trend, _, _, _ = _classify_trend(qqq_closes_upto) if len(qqq_closes_upto) >= SMA_FAST else ("unknown", None, None, None)
    vix_regime, _ = _classify_vix(vix_level)

    if spy_trend == "bull" and qqq_trend in ("bull", "neutral"):
        market = "bull"
    elif spy_trend == "bear" and qqq_trend in ("bear", "neutral"):
        market = "bear"
    elif spy_trend == "bear" and qqq_trend == "bear":
        market = "bear"
    elif spy_trend == "bull" and qqq_trend == "bull":
        market = "bull"
    else:
        market = "neutral"

    if market == "bull" and vix_regime == "calm":
        return "bull_calm", 0, 1.0
    elif market == "bull" and vix_regime == "elevated":
        return "bull_volatile", 5, 0.85
    elif market == "neutral" and vix_regime in ("calm", "elevated"):
        return "neutral", 10, 0.85
    elif market == "bear" and vix_regime == "calm":
        return "bear_calm", 20, 0.70
    elif market == "bear" and vix_regime == "elevated":
        return "bear_volatile", 28, 0.60
    elif market in ("bear", "neutral") and vix_regime == "fear":
        return "bear_fear", 35, 0.50
    elif vix_regime == "fear":
        return "fear", 25, 0.60
    else:
        return "unknown", 10, 0.85


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


def execute_buy(ledger, sym, qty, price, current_date, score, rationale, trade_log):
    cost = qty * price
    ledger["cash"] -= cost
    ledger["positions"][sym] = {"qty": qty, "avg_entry": price, "entry_date": current_date}
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

def run_single_backtest(symbols, series, spy_series, qqq_series, vix_series, sector_map, p, start_i, horizon_days):
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
        current_value = portfolio_value(ledger["cash"], ledger["positions"], lambda s: price_of(s, current_date) or 0)
        high_water_mark = max(high_water_mark, current_value)
        drawdown_pct = (high_water_mark - current_value) / high_water_mark if high_water_mark else 0.0
        circuit_breaker_active = drawdown_pct >= p["circuit_breaker_drawdown_pct"]
        equity_curve.append({"date": str(current_date), "value": round(current_value, 2)})

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
            cur_value = portfolio_value(ledger["cash"], ledger["positions"], lambda s: price_of(s, current_date) or 0)
            qty, _sizing_note = calc_buy_qty(price, ledger["cash"], cur_value, 0.0, p_gated)
            if qty is None:
                continue
            # _sector_cap_block_reason (signals.py) expects each position to
            # carry a precomputed market_value, matching the shape
            # get_positions() returns live from Alpaca. The simulated
            # ledger only stores qty/avg_entry/entry_date, so shape a view
            # with today's mark-to-market value rather than changing what
            # the ledger itself stores (avg_entry/entry_date are read
            # elsewhere assuming the flat shape).
            positions_with_mv = {
                s: {**pos, "market_value": pos["qty"] * (price_of(s, current_date) or 0)}
                for s, pos in ledger["positions"].items()
            }
            sector_block = _sector_cap_block_reason(sym, price, qty, sector_map, positions_with_mv, cur_value, p)
            if sector_block:
                continue
            execute_buy(ledger, sym, qty, price, current_date, buy_score, buy_rationale, trade_log)

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

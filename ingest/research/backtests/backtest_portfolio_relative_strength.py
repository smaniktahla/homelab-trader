#!/usr/bin/env python3
"""Experiment 010: portfolio-level relative-strength variant comparison.

Research artifact, not production logic. Follow-up to Experiment 009,
which found stock-vs-sector relative strength ("outperforming_sector" vs
"underperforming_sector") carries a real, sizeable, statistically solid
reduction in episode-level downside risk (stop-out rate -6.1pp, 10th-
percentile return +4.2pp, both notable-effect-size and p<0.001) despite
Experiment 007 finding no mean-RETURN edge from it. This experiment asks
the natural next question: does that episode-level risk reduction survive
at the PORTFOLIO level, where position sizing, sector caps, cooldowns,
stop-losses, and correlated-episode overlap can all wash out an effect
that looked clean symbol-by-symbol?

Three variants, run over the EXACT SAME Monte Carlo start dates (paired,
not independently sampled) and the EXACT SAME gate pipeline --
backtest_portfolio_montecarlo.py's run_single_backtest() (Experiment 003,
unchanged except for an additive opt-in rs_policy hook that is a no-op
unless explicitly passed -- see that file's docstring on the parameter):

  1. baseline    -- today's live mean_reversion pipeline, unmodified.
  2. gate         -- identical pipeline, but any BUY where the stock is
                     underperforming its own sector is skipped entirely.
  3. reduce_size  -- identical pipeline, but such a BUY is still taken, at
                     50% of the size the identical baseline sizing/risk-
                     engine chain would have produced.

Same historical price data, same proposal-generation logic, same
buy_cooldown-equivalent same-day-round-trip guard, same sector caps, same
stop-losses, same execution assumptions (same-day-close fills, no
slippage) across all three -- they differ ONLY in the relative-strength
gate/resize branch, which is a no-op for every symbol NOT classified
underperforming_sector on that day.

Metrics per run, aggregated (mean/median/stdev) across the shared MC
start-date pool per variant:
  - total_return_pct, max_drawdown_pct         (as Experiment 003)
  - sharpe / sortino    -- annualized from that run's own daily equity
    returns (rf=0 simplification, noted below); short-window per-run
    values averaged across MC runs, not one long continuous curve.
  - stop_out_rate       -- fraction of exits that were stop_loss
  - n_buys / n_sells / turnover_ratio  -- turnover = total buy+sell
    notional / average portfolio value over the run
  - avg_capital_deployed_pct / avg_cash_pct (cash-drag proxy)
  - avg_sector_concentration_pct -- mean daily share of the single
    largest sector's exposure

Exposure-matched comparison (explicitly requested): the gate variant
structurally holds more cash on average (it skips trades instead of
resizing them), so a naive drawdown/return comparison would conflate
"the relative-strength filter helps" with "holding more cash is safer,
trivially." Alongside the raw metrics, this reports
exposure_adjusted_return_pct and exposure_adjusted_max_drawdown_pct --
each raw figure divided by that run's own avg_capital_deployed_pct/100,
i.e. normalized to "per unit of capital actually put at risk." A variant
that only looks better because it deployed less capital should show that
gap shrink or disappear once exposure-adjusted.

Does NOT modify any production default -- shared/*.py, ingest/schema.sql,
and live signal_params are untouched. Purely a research read, run
against production data.

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 research/backtests/backtest_portfolio_relative_strength.py
"""

import os
import sys
import json
import logging
import random
import statistics
from datetime import datetime, timezone

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_portfolio_montecarlo import (
    get_db, get_universe_symbols, get_sector_map, load_series, load_sector_series,
    run_single_backtest, eligible_start_indices, STARTING_CASH, HORIZON_DAYS, MC_RUNS, WINDOW,
)
from signals import load_params
from db_utils import save_backtest_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EXPERIMENT_ID = "010_portfolio_relative_strength"
GIT_COMMIT = os.environ.get("BACKTEST_GIT_COMMIT", "unknown")
ANNUALIZATION_DAYS = 252

VARIANTS = {
    "baseline": None,
    "gate": {"mode": "gate"},
    "reduce_size_50": {"mode": "reduce_size", "size_multiplier": 0.5},
}


# ─────────────────────────────────────────────────────────────────────────
# Per-run metric extraction
# ─────────────────────────────────────────────────────────────────────────

def _daily_returns(equity_curve):
    vals = [pt["value"] for pt in equity_curve]
    return [(vals[i] - vals[i - 1]) / vals[i - 1] for i in range(1, len(vals)) if vals[i - 1]]


def _sharpe(daily_returns):
    if len(daily_returns) < 2:
        return None
    sd = statistics.pstdev(daily_returns)
    if not sd:
        return None
    return round(statistics.mean(daily_returns) / sd * (ANNUALIZATION_DAYS ** 0.5), 3)


def _sortino(daily_returns):
    if len(daily_returns) < 2:
        return None
    downside = [min(0.0, r) for r in daily_returns]
    dd = (sum(d * d for d in downside) / len(downside)) ** 0.5
    if not dd:
        return None
    return round(statistics.mean(daily_returns) / dd * (ANNUALIZATION_DAYS ** 0.5), 3)


def compute_run_metrics(result):
    equity_curve = result["equity_curve"]
    trade_log = result["trade_log"]

    n_buys = sum(1 for t in trade_log if t["side"] == "buy")
    sells = [t for t in trade_log if t["side"] == "sell"]
    n_sells = len(sells)
    stop_loss_exits = sum(1 for t in sells if t.get("exit_reason") == "stop_loss")
    stop_out_rate = round(stop_loss_exits / n_sells, 4) if n_sells else None

    buy_notional = sum(t["qty"] * t["price"] for t in trade_log if t["side"] == "buy")
    sell_notional = sum(t["qty"] * t["price"] for t in trade_log if t["side"] == "sell")
    avg_value = statistics.mean(pt["value"] for pt in equity_curve) if equity_curve else None
    turnover_ratio = round((buy_notional + sell_notional) / avg_value, 3) if avg_value else None

    invested_fracs = [
        (pt["value"] - pt["cash"]) / pt["value"] for pt in equity_curve if pt["value"]
    ]
    avg_capital_deployed_pct = round(100 * statistics.mean(invested_fracs), 2) if invested_fracs else None
    avg_cash_pct = round(100 - avg_capital_deployed_pct, 2) if avg_capital_deployed_pct is not None else None

    sector_concentrations = []
    for pt in equity_curve:
        if not pt["value"] or not pt["sector_exposure"]:
            sector_concentrations.append(0.0)
            continue
        sector_concentrations.append(max(pt["sector_exposure"].values()) / pt["value"] * 100)
    avg_sector_concentration_pct = round(statistics.mean(sector_concentrations), 2) if sector_concentrations else None

    daily_returns = _daily_returns(equity_curve)
    sharpe = _sharpe(daily_returns)
    sortino = _sortino(daily_returns)

    deployed_frac = (avg_capital_deployed_pct / 100) if avg_capital_deployed_pct else None
    exposure_adjusted_return_pct = (
        round(result["total_return_pct"] / deployed_frac, 2) if deployed_frac else None
    )
    exposure_adjusted_max_drawdown_pct = (
        round(result["max_drawdown_pct"] / deployed_frac, 2) if deployed_frac else None
    )

    return {
        "total_return_pct": result["total_return_pct"],
        "max_drawdown_pct": result["max_drawdown_pct"],
        "excess_vs_spy_pct": result["excess_vs_spy_pct"],
        "sharpe": sharpe,
        "sortino": sortino,
        "stop_out_rate": stop_out_rate,
        "n_buys": n_buys,
        "n_sells": n_sells,
        "turnover_ratio": turnover_ratio,
        "avg_capital_deployed_pct": avg_capital_deployed_pct,
        "avg_cash_pct": avg_cash_pct,
        "avg_sector_concentration_pct": avg_sector_concentration_pct,
        "exposure_adjusted_return_pct": exposure_adjusted_return_pct,
        "exposure_adjusted_max_drawdown_pct": exposure_adjusted_max_drawdown_pct,
    }


_METRIC_KEYS = [
    "total_return_pct", "max_drawdown_pct", "excess_vs_spy_pct", "sharpe", "sortino",
    "stop_out_rate", "n_buys", "n_sells", "turnover_ratio",
    "avg_capital_deployed_pct", "avg_cash_pct", "avg_sector_concentration_pct",
    "exposure_adjusted_return_pct", "exposure_adjusted_max_drawdown_pct",
]


def aggregate_variant(run_metrics_list):
    agg = {}
    for k in _METRIC_KEYS:
        vals = [m[k] for m in run_metrics_list if m[k] is not None]
        if not vals:
            agg[k] = {"mean": None, "median": None, "stdev": None}
            continue
        agg[k] = {
            "mean": round(statistics.mean(vals), 4),
            "median": round(statistics.median(vals), 4),
            "stdev": round(statistics.pstdev(vals), 4) if len(vals) > 1 else 0.0,
        }
    agg["worst_max_drawdown_pct"] = round(max(m["max_drawdown_pct"] for m in run_metrics_list), 2)
    return agg


def main():
    conn = get_db()
    p = load_params(conn)
    log.info(f"Live params: score_proposal_min={p['score_proposal_min']} stop_loss_pct={p['stop_loss_pct']} "
              f"max_open_positions={p['max_open_positions']} trade_allocation_pct={p['trade_allocation_pct']}")

    symbols = get_universe_symbols(conn)
    sector_map = get_sector_map(conn)
    log.info(f"Loading price history for {len(symbols)} universe symbols + SPY/QQQ/^VIX + sector ETFs...")

    series = {}
    for sym in symbols:
        series[sym] = load_series(conn, sym)
    spy_series = series.get("SPY") or load_series(conn, "SPY")
    qqq_series = series.get("QQQ") or load_series(conn, "QQQ")
    vix_series = load_series(conn, "^VIX")
    sector_series = load_sector_series(conn, sector_map, load_series)
    conn.close()
    log.info(f"Loaded {len(sector_series)} sector ETF series: {sorted(sector_series)}")

    if not spy_series[0]:
        log.error("No SPY history -- cannot compute market regime or benchmark. Aborting.")
        sys.exit(1)

    spy_dates = spy_series[0]
    starts = eligible_start_indices(spy_dates, HORIZON_DAYS)
    if len(starts) < MC_RUNS:
        log.warning(f"Only {len(starts)} eligible start dates for {HORIZON_DAYS}d horizon; running all of them")
    chosen = random.sample(starts, min(MC_RUNS, len(starts)))
    log.info(f"Running {len(chosen)} PAIRED Monte Carlo backtests per variant "
              f"({len(VARIANTS)} variants, same start dates each), {HORIZON_DAYS}d horizon, "
              f"${STARTING_CASH:,.0f} starting cash")

    all_results = {}
    for variant_name, rs_policy in VARIANTS.items():
        log.info(f"--- Variant: {variant_name} ---")
        run_metrics = []
        for n, start_i in enumerate(chosen, 1):
            result = run_single_backtest(
                symbols, series, spy_series, qqq_series, vix_series, sector_map, p, start_i, HORIZON_DAYS,
                rs_policy=rs_policy, sector_series=sector_series,
            )
            m = compute_run_metrics(result)
            run_metrics.append(m)
            if n % 10 == 0 or n == len(chosen):
                log.info(f"[{variant_name} {n}/{len(chosen)}] return={m['total_return_pct']:+.2f}% "
                          f"max_dd={m['max_drawdown_pct']:.2f}% trades={m['n_buys'] + m['n_sells']}")
        all_results[variant_name] = {
            "runs": run_metrics,
            "aggregate": aggregate_variant(run_metrics),
        }

    report = {
        "experiment_id": EXPERIMENT_ID,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": GIT_COMMIT,
        "config": {
            "horizon_days": HORIZON_DAYS, "mc_runs": len(chosen), "window_days": WINDOW,
            "starting_cash": STARTING_CASH, "params_used": p, "variants": VARIANTS,
        },
        "universe_size": len(symbols),
        "data_date_range": [str(spy_dates[0]), str(spy_dates[-1])],
        "results": all_results,
    }

    out_path = "/tmp/backtest_results_010.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info(f"Full results written to {out_path}")

    def _fmt(agg, key, suffix="%", digits=2):
        v = agg[key]["mean"]
        return f"{v:+.{digits}f}{suffix}" if v is not None else "n/a"

    summary_line = " | ".join(
        f"{name}: return={_fmt(all_results[name]['aggregate'], 'total_return_pct')} "
        f"max_dd={_fmt(all_results[name]['aggregate'], 'max_drawdown_pct')} "
        f"sharpe={all_results[name]['aggregate']['sharpe']['mean']}"
        for name in VARIANTS
    )
    save_backtest_result(EXPERIMENT_ID, GIT_COMMIT, report, summary=summary_line)
    log.info("Results also saved to backtest_results table")

    print(f"\n=== Experiment {EXPERIMENT_ID} (commit {GIT_COMMIT[:8]}) ===")
    print(f"Universe: {len(symbols)} symbols | Data: {report['data_date_range'][0]} to {report['data_date_range'][1]}")
    print(f"{len(chosen)} PAIRED runs per variant, {HORIZON_DAYS}-day horizon, ${STARTING_CASH:,.0f} starting cash\n")

    header = f"{'metric':<32}" + "".join(f"{name:>18}" for name in VARIANTS)
    print(header)
    print("-" * len(header))

    def _row(label, key, suffix="", digits=2):
        cells = []
        for name in VARIANTS:
            agg = all_results[name]["aggregate"]
            v = agg[key]["mean"] if key in agg else None
            cells.append(f"{v:>+17.{digits}f}{suffix}" if isinstance(v, (int, float)) else f"{'n/a':>18}")
        print(f"{label:<32}" + "".join(cells))

    _row("Mean return", "total_return_pct", "%")
    _row("Mean max drawdown", "max_drawdown_pct", "%")
    print(f"{'Worst max drawdown':<32}" + "".join(
        f"{all_results[n]['aggregate']['worst_max_drawdown_pct']:>+17.2f}%" for n in VARIANTS))
    _row("Mean excess vs SPY", "excess_vs_spy_pct", "%")
    _row("Sharpe (annualized, rf=0)", "sharpe", "", 3)
    _row("Sortino (annualized, rf=0)", "sortino", "", 3)
    _row("Stop-out rate", "stop_out_rate", "", 3)
    _row("Mean buys/run", "n_buys", "", 1)
    _row("Mean sells/run", "n_sells", "", 1)
    _row("Turnover ratio", "turnover_ratio", "", 3)
    _row("Avg capital deployed", "avg_capital_deployed_pct", "%")
    _row("Avg cash (drag proxy)", "avg_cash_pct", "%")
    _row("Avg sector concentration", "avg_sector_concentration_pct", "%")
    print()
    print("--- Exposure-matched (per unit of capital actually deployed) ---")
    _row("Exposure-adj. return", "exposure_adjusted_return_pct", "%")
    _row("Exposure-adj. max drawdown", "exposure_adjusted_max_drawdown_pct", "%")

    print("\nInterpretation: all three variants ran on the exact same paired Monte Carlo start dates, through the")
    print("exact same gate/exit/execution pipeline (backtest_portfolio_montecarlo.py's run_single_backtest,")
    print("unmodified) -- they differ ONLY in whether/how a stock underperforming its own sector gets bought.")
    print("Raw max-drawdown/return comparisons can be misleading if a variant simply holds more cash on average")
    print("(it trades less, so it's trivially 'safer' in dollar terms) -- the exposure-adjusted rows divide by")
    print("avg_capital_deployed_pct/100 to compare on a per-unit-of-capital-at-risk basis instead. Sharpe/Sortino")
    print("are averaged per-run annualized figures from each run's own ~%d-day equity curve (rf=0), not one" % HORIZON_DAYS)
    print("long continuous series -- noisy at this window length, read as a rough cross-variant comparison, not")
    print("a precise risk-adjusted-return estimate. Same caveats as Experiments 003/007/008/009: single")
    print("historical window, survivorship-biased universe, no slippage/commission modeling, no production")
    print("defaults were changed by running this.")


if __name__ == "__main__":
    main()

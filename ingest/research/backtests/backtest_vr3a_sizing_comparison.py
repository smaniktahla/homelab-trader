#!/usr/bin/env python3
"""VR-3a empirical run: does volatility-scaled sizing actually help?

shared/sizing_policy_comparison.py (VR-3a, merged) provides the paired-
opportunity mechanics but deliberately ships with no real-data run --see
its own module docstring. This script is that follow-up: run the existing,
already-live-identical sizing formula (shared/risk_engine.py's VR-2
candidate reuses the same shared/volatility_forecast.py::
volatility_size_multiplier()) against real price_history, for both
strategies currently registered in shared/strategy_registry.py
(bollinger_breakout_continuation, ema_crossover_trend), across the live
scannable universe.

**Why % return is NOT the headline metric here**: with
volatility_max_multiplier capped at 1.0 (VR-2's own paper-eligible default,
reductions only), the volatility-scaled qty is always <= the fixed qty for
the same opportunity. Since return_pct = pnl / (qty * entry_price), scaling
qty down does not mechanically change the per-trade % return (aside from
floor() rounding at small qty) -- comparing mean_return_pct between the two
policies mostly reports a rounding artifact, not a sizing effect. Reported
anyway for completeness, but flagged as such below.

The real question this script measures: for a FIXED account-level capital
commitment (base_notional, spent identically by both policies at entry --
one scales it down before use, one doesn't), does the volatility overlay
change TOTAL dollar return on that committed capital? That is:
  dollar_return_on_base = trade_pnl / base_notional
summed/averaged across all paired trades. This is directly comparable
across policies (same denominator) and captures exactly what a reduced
position size is supposed to do: shrink dollar exposure specifically on
opportunities the estimator judged risky, not uniformly.

Scope, disclosed rather than silently assumed (see
docs/volatility-sizing-vr0-reconciliation.md's own disclosure discipline):
  - Price basis: raw price_history close, split-adjusted only, NOT
    dividend-adjusted (per the reconciliation doc's binding decision).
  - No transaction costs/slippage (shared/backtest_engine.py's own
    documented limitation) -- both policies are affected equally here, so
    the fixed-vs-volatility COMPARISON is not biased by this, but absolute
    pnl figures are not a claim about live tradeable returns.
  - Universe/date-range subset chosen for this run (see CLI args / defaults
    below), NOT the full VR-0 experiment contract's train/holdout split --
    this is explicitly a first empirical look, not VR-0's registered
    confirmatory experiment. No claim of statistical significance is made
    or implied by this script's output.
  - estimator="realized_vol" only in this first pass (not "ewma") to keep
    the first read simple; the harness supports either.
"""

import os
import sys
import json
import math
import logging
import pathlib
import statistics
from datetime import datetime, timezone

_repo_root = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_repo_root / "shared"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg2

from backtest_engine import load_bars
from sizing_policy_comparison import compare_sizing_policies
from strategy_registry import STRATEGIES as REGISTRY_STRATEGIES
from mean_reversion_strategy import make_mean_reversion_strategy
from db_utils import save_backtest_result

# mean_reversion isn't in shared/strategy_registry.py (that registry is for
# the live backtest-visualization UI's backtest_engine.py-native
# strategies; mean_reversion's production form lives in
# shared/signals.py::compute_signals(), DB/portfolio-dependent, not a pure
# bars_seen function). shared/mean_reversion_strategy.py adapts a
# SIMPLIFIED, single-symbol-only proxy of its core RSI/BB entry rule to
# this harness's Strategy interface -- see that module's docstring for
# exactly what's included/omitted. Added here (not registered in
# strategy_registry.py) since it's a research-only proxy, not the real
# thing the live visualization UI should present as "mean_reversion".
STRATEGIES = dict(REGISTRY_STRATEGIES)
STRATEGIES["mean_reversion_proxy"] = {
    "display_name": "Mean Reversion (simplified proxy, RSI/BB only)",
    "make_strategy": make_mean_reversion_strategy,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_DSN = os.environ["DATABASE_URL"]
BASE_NOTIONAL = 10_000.0
ESTIMATOR = "realized_vol"
REFERENCE_VOL = 0.25       # matches prod signal_params default
VOL_FLOOR = 0.05           # matches prod signal_params default
MAX_MULTIPLIER = 1.0       # matches prod signal_params default -- reductions only
START = datetime(2021, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 9, 1, tzinfo=timezone.utc)


def get_db():
    return psycopg2.connect(DB_DSN)


def get_universe_symbols(conn, limit=None):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM universe WHERE scannable=TRUE ORDER BY symbol")
        symbols = [r[0] for r in cur.fetchall()]
    return symbols[:limit] if limit else symbols


def run_one(conn, symbol, strategy_key):
    bars = load_bars(conn, symbol, START, END)
    if len(bars) < 100:
        return None
    spec = STRATEGIES[strategy_key]
    strategy = spec["make_strategy"]()
    try:
        report = compare_sizing_policies(
            bars, strategy, base_notional=BASE_NOTIONAL, estimator=ESTIMATOR,
            reference_vol=REFERENCE_VOL, vol_floor=VOL_FLOOR, max_multiplier=MAX_MULTIPLIER,
        )
    except AssertionError as e:
        log.error("paired-opportunity invariant violated for %s/%s: %s", symbol, strategy_key, e)
        return None
    return report


def summarize(reports_by_strategy):
    """reports_by_strategy: {strategy_key: [SizingComparisonReport, ...]}"""
    summary = {}
    for strategy_key, reports in reports_by_strategy.items():
        fixed_dollar_returns = []
        vol_dollar_returns = []
        fixed_pct_returns = []
        vol_pct_returns = []
        multipliers = []  # volatility_qty / fixed_qty per trade, when fixed_qty > 0
        wins = 0
        total = 0
        symbols_with_trades = 0
        total_fixed_notional_committed = 0.0
        total_vol_notional_committed = 0.0

        for report in reports:
            if not report.paired_trades:
                continue
            symbols_with_trades += 1
            for t in report.paired_trades:
                total += 1
                if t.fixed_pnl > 0:
                    wins += 1
                fixed_dollar_returns.append(t.fixed_pnl / BASE_NOTIONAL)
                vol_dollar_returns.append(t.volatility_pnl / BASE_NOTIONAL)
                if t.fixed_return_pct is not None:
                    fixed_pct_returns.append(t.fixed_return_pct)
                if t.volatility_return_pct is not None:
                    vol_pct_returns.append(t.volatility_return_pct)
                if t.fixed_qty:
                    multipliers.append(t.volatility_qty / t.fixed_qty)
                total_fixed_notional_committed += t.fixed_qty * t.entry_price
                total_vol_notional_committed += t.volatility_qty * t.entry_price

        def _mean(xs):
            return statistics.mean(xs) if xs else None

        def _stdev(xs):
            return statistics.stdev(xs) if len(xs) > 1 else None

        def _sharpe_like(xs):
            m, s = _mean(xs), _stdev(xs)
            return (m / s) if (m is not None and s) else None

        summary[strategy_key] = {
            "symbols_with_trades": symbols_with_trades,
            "trade_count": total,
            "win_rate": (wins / total) if total else None,
            "mean_fixed_return_pct": _mean(fixed_pct_returns),
            "mean_volatility_return_pct": _mean(vol_pct_returns),
            "mean_fixed_dollar_return_on_base_notional": _mean(fixed_dollar_returns),
            "mean_volatility_dollar_return_on_base_notional": _mean(vol_dollar_returns),
            "stdev_fixed_dollar_return_on_base_notional": _stdev(fixed_dollar_returns),
            "stdev_volatility_dollar_return_on_base_notional": _stdev(vol_dollar_returns),
            "sharpe_like_fixed": _sharpe_like(fixed_dollar_returns),
            "sharpe_like_volatility": _sharpe_like(vol_dollar_returns),
            "total_fixed_pnl": sum(fixed_dollar_returns) * BASE_NOTIONAL,
            "total_volatility_pnl": sum(vol_dollar_returns) * BASE_NOTIONAL,
            "mean_size_multiplier_applied": _mean(multipliers),
            "pct_trades_downsized": (
                sum(1 for m in multipliers if m < 0.999) / len(multipliers) if multipliers else None
            ),
            "total_fixed_notional_committed": total_fixed_notional_committed,
            "total_volatility_notional_committed": total_vol_notional_committed,
        }
    return summary


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=60, help="cap on universe symbols (default 60, for runtime)")
    args = parser.parse_args()

    conn = get_db()
    symbols = get_universe_symbols(conn, limit=args.limit)
    log.info("Running VR-3a sizing comparison over %d symbols, %s to %s", len(symbols), START.date(), END.date())

    reports_by_strategy = {key: [] for key in STRATEGIES}
    for i, symbol in enumerate(symbols):
        for strategy_key in STRATEGIES:
            report = run_one(conn, symbol, strategy_key)
            if report is not None:
                reports_by_strategy[strategy_key].append(report)
        if (i + 1) % 20 == 0:
            log.info("...%d/%d symbols done", i + 1, len(symbols))
    conn.close()

    summary = summarize(reports_by_strategy)

    print(json.dumps(summary, indent=2, default=str))

    save_backtest_result(
        experiment_id="vr3a_sizing_comparison_v1",
        git_commit=os.popen("git -C " + str(_repo_root) + " rev-parse HEAD").read().strip(),
        results=summary,
        summary=(
            f"VR-3a first empirical read: {len(symbols)} symbols, "
            f"{START.date()}..{END.date()}, base_notional=${BASE_NOTIONAL:.0f}, "
            f"estimator={ESTIMATOR}, reference_vol={REFERENCE_VOL}, "
            f"vol_floor={VOL_FLOOR}, max_multiplier={MAX_MULTIPLIER}"
        ),
    )
    log.info("Saved to backtest_results (experiment_id=vr3a_sizing_comparison_v1)")


if __name__ == "__main__":
    main()

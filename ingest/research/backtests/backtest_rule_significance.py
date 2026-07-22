#!/usr/bin/env python3
"""Experiment 005: entry-rule statistical significance test (permutation).

Research artifact, not production logic. Answers a question Experiment 002
doesn't: does mean_reversion's entry rule (RSI/BB/regime score >= threshold)
actually carry information, or would picking random entry days on the same
symbols have looked just as good? This is the "rule significance test"
concept described in the "Kimi K3 + Jesse Trade" workflow (an AI agent
validating that a strategy's entry logic isn't noise before trusting any of
its backtest numbers) — see the 2026-07-21 DocMost note on that video for
context.

Method: for each score threshold, build the real signal episodes (imported
directly from backtest_score_calibration.py's backtest_symbol/episode-dedup
logic — zero risk of drifting from what Experiment 002 already validated
against live signals.py). Then build a null distribution by repeatedly
drawing, per symbol, the same NUMBER of random entry days as that symbol
contributed real episodes, computing the pooled mean return/win-rate for
each random draw, and repeating N_PERMUTATIONS times. The real rule is
"significant" at a threshold if its observed statistic sits in the extreme
tail of that null distribution (reported as an empirical one-sided p-value
and a percentile rank) — i.e. random entries on the same symbols, same
count, same period essentially never do this well by chance.

This is a within-symbol permutation test, not a full walk-forward or
market-regime-controlled test — it isolates "does entry TIMING matter" while
holding "which symbols/how many trades" fixed to match the real rule, which
is the right null hypothesis for a screening/timing signal like this one.
It does NOT tell you the strategy is profitable after costs, nor that it
will hold up going forward (Experiment 002's train/holdout split and
Experiment 003's Monte Carlo already cover overfitting and portfolio-level
robustness respectively) — this experiment answers one narrower question:
is the entry rule distinguishable from random chance at all.

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 research/backtests/backtest_rule_significance.py
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

from backtest_score_calibration import (
    get_db, get_universe_symbols, load_series, backtest_symbol,
    apply_episode_dedup, spy_forward_return, WINDOW, FORWARD_DAYS,
    SWEEP_THRESHOLDS,
)
from signals import load_params

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EXPERIMENT_ID = "005_rule_significance"
GIT_COMMIT = os.environ.get("BACKTEST_GIT_COMMIT", "unknown")
N_PERMUTATIONS = int(os.environ.get("N_PERMUTATIONS", "2000"))
RANDOM_SEED = 42
# score_log_min is already the live floor every real signal passes, so
# thresholds below it would test signals that never actually fire.
TEST_THRESHOLDS = SWEEP_THRESHOLDS


def eligible_indices(n):
    return list(range(WINDOW, n - FORWARD_DAYS))


def precompute_returns(dates, closes, spy_dates, spy_closes, eligible):
    """raw_return_20d / excess_return_vs_spy for every eligible entry index,
    computed once per symbol so both the real episodes and every permutation
    draw are O(1) dict lookups instead of recomputing forward returns."""
    cache = {}
    for i in eligible:
        entry = closes[i]
        raw = (closes[i + FORWARD_DAYS] - entry) / entry * 100
        spy_fwd = spy_forward_return(i, dates, spy_dates, spy_closes)
        cache[i] = {
            "raw_return_20d": raw,
            "excess_return_vs_spy": (raw - spy_fwd) if spy_fwd is not None else None,
        }
    return cache


def pooled_stats(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0, "avg_raw_return": None, "avg_excess_return": None, "win_rate": None}
    raw = [r["raw_return_20d"] for r in rows]
    excess = [r["excess_return_vs_spy"] for r in rows if r["excess_return_vs_spy"] is not None]
    wins = sum(1 for x in raw if x > 0)
    return {
        "n": n,
        "avg_raw_return": round(sum(raw) / n, 4),
        "avg_excess_return": round(sum(excess) / len(excess), 4) if excess else None,
        "win_rate": round(100 * wins / n, 2),
    }


def empirical_pvalue(observed, null_vals):
    """One-sided (upper-tail) empirical p-value with the standard +1/+1
    continuity correction so it's never reported as exactly zero."""
    if observed is None or not null_vals:
        return None
    count_ge = sum(1 for v in null_vals if v >= observed)
    return round((1 + count_ge) / (1 + len(null_vals)), 5)


def percentile_rank(observed, null_vals):
    if observed is None or not null_vals:
        return None
    rank = sum(1 for v in null_vals if v < observed)
    return round(100 * rank / len(null_vals), 1)


def run_permutation_test(real_episodes_by_symbol, symbol_cache, symbol_eligible, n_permutations, seed):
    rng = random.Random(seed)
    all_real = [r for eps in real_episodes_by_symbol.values() for r in eps]
    observed = pooled_stats(all_real)

    null_raw, null_excess, null_win = [], [], []
    for _ in range(n_permutations):
        pooled = []
        for symbol, real_eps in real_episodes_by_symbol.items():
            k = len(real_eps)
            if k == 0:
                continue
            eligible = symbol_eligible.get(symbol, [])
            if len(eligible) < k:
                continue  # shouldn't happen (real episodes are a subset of eligible) but guard anyway
            cache = symbol_cache[symbol]
            for i in rng.sample(eligible, k):
                pooled.append(cache[i])
        stats = pooled_stats(pooled)
        if stats["n"] == 0:
            continue
        null_raw.append(stats["avg_raw_return"])
        if stats["avg_excess_return"] is not None:
            null_excess.append(stats["avg_excess_return"])
        null_win.append(stats["win_rate"])

    def summarize(null_vals):
        if not null_vals:
            return None
        return {
            "mean": round(statistics.mean(null_vals), 4),
            "std": round(statistics.pstdev(null_vals), 4) if len(null_vals) > 1 else 0.0,
        }

    return {
        "observed": observed,
        "n_permutations_requested": n_permutations,
        "n_permutations_used": len(null_raw),
        "null_avg_raw_return": summarize(null_raw),
        "null_avg_excess_return": summarize(null_excess),
        "null_win_rate": summarize(null_win),
        "p_value_raw_return": empirical_pvalue(observed["avg_raw_return"], null_raw),
        "p_value_excess_return": empirical_pvalue(observed["avg_excess_return"], null_excess),
        "p_value_win_rate": empirical_pvalue(observed["win_rate"], null_win),
        "percentile_raw_return": percentile_rank(observed["avg_raw_return"], null_raw),
        "percentile_excess_return": percentile_rank(observed["avg_excess_return"], null_excess),
        "percentile_win_rate": percentile_rank(observed["win_rate"], null_win),
    }


def episodes_at_threshold(flat_results, threshold):
    """Filter to score>=threshold, then redo episode dedup fresh (dedup is
    order/gap-dependent, so it must be recomputed per threshold rather than
    reused from a lower-threshold pass)."""
    subset = [r for r in flat_results if r["score"] >= threshold]
    subset.sort(key=lambda r: r["date"])
    apply_episode_dedup(subset)  # mutates in place, sets is_episode_start
    episodes = [r for r in subset if r["is_episode_start"]]
    by_symbol = {}
    for r in episodes:
        by_symbol.setdefault(r["symbol"], []).append(r)
    return by_symbol


def main():
    conn = get_db()
    params = load_params(conn)
    log.info(f"Live params: score_log_min={params['score_log_min']} score_proposal_min={params['score_proposal_min']}")

    symbols = get_universe_symbols(conn)
    log.info(f"Testing significance across {len(symbols)} universe symbols, thresholds={TEST_THRESHOLDS}, "
             f"{N_PERMUTATIONS} permutations each")

    spy_dates, spy_closes, _, _ = load_series(conn, "SPY")
    if not spy_closes:
        log.error("No SPY history — excess-return significance unavailable")

    flat_results = []
    symbol_cache = {}
    symbol_eligible = {}
    for idx, sym in enumerate(symbols):
        dates, closes, highs, lows = load_series(conn, sym)
        if not closes or len(closes) < WINDOW + FORWARD_DAYS + 1:
            continue
        try:
            results = backtest_symbol(sym, dates, closes, highs, lows, spy_dates, spy_closes, params)
        except Exception as e:
            log.warning(f"{sym}: backtest failed: {e}")
            continue
        flat_results.extend(results)

        eligible = eligible_indices(len(closes))
        symbol_eligible[sym] = eligible
        symbol_cache[sym] = precompute_returns(dates, closes, spy_dates, spy_closes, eligible)
        if (idx + 1) % 100 == 0:
            log.info(f"...{idx + 1}/{len(symbols)} symbols prepared")

    conn.close()
    log.info(f"Prepared {len(flat_results)} raw score_log_min+ observations across {len(symbol_cache)} symbols")

    report = {
        "experiment_id": EXPERIMENT_ID,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": GIT_COMMIT,
        "config": {
            "window_days": WINDOW, "forward_days": FORWARD_DAYS,
            "n_permutations": N_PERMUTATIONS, "random_seed": RANDOM_SEED,
            "thresholds_tested": TEST_THRESHOLDS, "params_used": params,
        },
        "universe_size": len(symbols),
        "by_threshold": {},
    }

    for threshold in TEST_THRESHOLDS:
        by_symbol = episodes_at_threshold(flat_results, threshold)
        n_episodes = sum(len(v) for v in by_symbol.values())
        if n_episodes < 20:
            log.info(f"score>={threshold}: only {n_episodes} episodes, skipping (too few for a meaningful permutation test)")
            continue
        result = run_permutation_test(by_symbol, symbol_cache, symbol_eligible, N_PERMUTATIONS, RANDOM_SEED + threshold)
        report["by_threshold"][threshold] = result
        log.info(f"score>={threshold}: n_episodes={result['observed']['n']} "
                 f"p(excess_return)={result['p_value_excess_return']} percentile={result['percentile_excess_return']}")

    with open("/tmp/backtest_results_005.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info("Full results written to /tmp/backtest_results_005.json")

    print(f"\n=== Experiment {EXPERIMENT_ID} (commit {GIT_COMMIT[:8]}) ===")
    print(f"Universe: {len(symbols)} symbols | {N_PERMUTATIONS} permutations per threshold\n")
    print(f"{'score>=':>8}  {'n_ep':>6}  {'avg_excess':>11}  {'null_mean':>10}  {'p_value':>8}  {'pctile':>7}  {'win_rate':>9}  {'null_win':>9}")
    for threshold, r in report["by_threshold"].items():
        obs = r["observed"]
        null_ex = r["null_avg_excess_return"] or {"mean": None}
        null_win = r["null_win_rate"] or {"mean": None}
        print(f"{threshold:>8}  {obs['n']:>6}  {obs['avg_excess_return']:>+10.2f}%  "
              f"{null_ex['mean']:>+9.2f}%  {r['p_value_excess_return']:>8}  {r['percentile_excess_return']:>6.1f}%  "
              f"{obs['win_rate']:>8.1f}%  {null_win['mean']:>8.1f}%")

    print("\nInterpretation: p_value is the fraction of random-entry-timing permutations (same symbols, same trade")
    print("count per symbol) that matched or beat the real rule's average excess return. p < 0.05 (percentile > 95)")
    print("means the entry rule's edge is unlikely to be random chance at that score threshold.")


if __name__ == "__main__":
    main()

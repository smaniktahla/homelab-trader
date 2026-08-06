#!/usr/bin/env python3
"""Experiment 009: Market Structure Engine risk-separation test.

Research artifact, not production logic. Follow-up to Experiment 008
(backtest_market_structure_significance.py), which found no significant
MEAN excess-return difference between structure-trend/CHoCH/BOS groups.
That result only rules out one hypothesis -- that structure predicts
better/worse average return. It says nothing about a genuinely different
hypothesis, raised in review: structure could reduce RISK (volatility,
tail outcomes, stop-out rate) while leaving mean return roughly flat.
This experiment tests that hypothesis directly, on the exact same real
episode pool and the exact same as-of-safe structure classification
Experiment 008 used (classify_episode_structure imported directly from
that module -- zero duplication, zero drift risk).

Seven risk/dispersion metrics per group, same real mean-reversion buy
episodes as Experiment 008 (backtest_score_calibration.backtest_symbol's
stop-loss-aware outcome already computes all of these per episode --
nothing new to compute at the episode level, just grouped differently):
  - stopped_out_rate       -- fraction of episodes that hit the stop
  - avg_mae                -- average maximum adverse excursion
  - avg_mfe                -- average maximum favorable excursion
  - realized_return_stdev  -- dispersion of the stop-loss-aware outcome
  - realized_return_p5     -- 5th percentile (downside tail)
  - realized_return_p10    -- 10th percentile (downside tail)
  - downside_deviation     -- semi-deviation of realized_return below 0

Same permutation-test method as Experiment 007/008 (shuffle group labels,
hold real per-episode outcomes fixed, compare the real group-stat gap
against the shuffled null distribution) but computing all seven stats
from ONE shuffle per permutation rather than reshuffling independently
per metric -- meaningfully cheaper at these group sizes (up to ~8,600
episodes) without changing what's being tested.

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 research/backtests/backtest_market_structure_risk_separation.py
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
    get_db, get_universe_symbols, load_series, backtest_symbol, apply_episode_dedup,
    WINDOW, FORWARD_DAYS,
)
from backtest_market_structure_significance import classify_episode_structure
from backtest_hierarchy_regime_significance import MIN_GROUP_N
from signals import load_params
from regime_common import load_daily_ohlc
from db_utils import save_backtest_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EXPERIMENT_ID = "009_market_structure_risk_separation"
GIT_COMMIT = os.environ.get("BACKTEST_GIT_COMMIT", "unknown")
N_PERMUTATIONS = int(os.environ.get("N_PERMUTATIONS", "2000"))
RANDOM_SEED = 42


def _percentile(values, pct):
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100.0)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _downside_deviation(values, threshold=0.0):
    """Semi-deviation: RMS of the shortfall below `threshold`, zero
    contribution from episodes that cleared it. Standard downside-risk
    measure -- unlike stdev, doesn't penalize upside variance."""
    if not values:
        return 0.0
    downs = [min(0.0, v - threshold) for v in values]
    return (sum(d * d for d in downs) / len(downs)) ** 0.5


def _compute_all_stats(episodes):
    maes = [e["mae"] for e in episodes]
    mfes = [e["mfe"] for e in episodes]
    rets = [e["realized_return"] for e in episodes]
    stopped = [1.0 if e["stopped_out"] else 0.0 for e in episodes]
    return {
        "stopped_out_rate": statistics.mean(stopped),
        "avg_mae": statistics.mean(maes),
        "avg_mfe": statistics.mean(mfes),
        "realized_return_stdev": statistics.pstdev(rets) if len(rets) > 1 else 0.0,
        "realized_return_p5": _percentile(rets, 5),
        "realized_return_p10": _percentile(rets, 10),
        "downside_deviation": _downside_deviation(rets),
    }


# Effect-size thresholds, deliberately separate from p-value significance
# -- a real, non-noise gap can still be too small to act on, and a big gap
# can still fail to clear significance with limited data (worth more data,
# not dismissal). stopped_out_rate is a proportion (points = percentage
# points); every other metric is already in return-percentage units.
_NOTABLE_PP = 5.0     # stopped_out_rate: >=5 percentage points
_NEGLIGIBLE_PP = 2.0  # stopped_out_rate: <2 percentage points
_NOTABLE_PCT = 2.0    # return-based metrics: >=2 percentage points of return
_NEGLIGIBLE_PCT = 0.5  # return-based metrics: <0.5 percentage points


def _effect_label(metric, gap):
    if metric == "stopped_out_rate":
        pp = abs(gap) * 100
        if pp >= _NOTABLE_PP:
            return "notable"
        if pp < _NEGLIGIBLE_PP:
            return "negligible"
        return "modest"
    if abs(gap) >= _NOTABLE_PCT:
        return "notable"
    if abs(gap) < _NEGLIGIBLE_PCT:
        return "negligible"
    return "modest"


def run_group_comparison(group_a, group_b, n_permutations, seed, min_group_n=MIN_GROUP_N):
    """Permutation test for all seven risk metrics at once: one shuffle of
    the pooled episode list per iteration, every metric derived from that
    same shuffle (not seven independent reshuffles)."""
    if len(group_a) < min_group_n or len(group_b) < min_group_n:
        return None
    rng = random.Random(seed)
    combined = list(group_a) + list(group_b)
    n_a = len(group_a)

    observed_a = _compute_all_stats(group_a)
    observed_b = _compute_all_stats(group_b)
    observed_diffs = {k: observed_a[k] - observed_b[k] for k in observed_a}

    null_diffs = {k: [] for k in observed_a}
    for _ in range(n_permutations):
        rng.shuffle(combined)
        stats_a = _compute_all_stats(combined[:n_a])
        stats_b = _compute_all_stats(combined[n_a:])
        for k in observed_a:
            null_diffs[k].append(stats_a[k] - stats_b[k])

    results = {}
    for k in observed_a:
        diffs = null_diffs[k]
        count_ge = sum(1 for d in diffs if abs(d) >= abs(observed_diffs[k]))
        p_value = round((1 + count_ge) / (1 + len(diffs)), 5)
        results[k] = {
            "n_a": len(group_a), "n_b": len(group_b),
            "stat_a": round(observed_a[k], 4), "stat_b": round(observed_b[k], 4),
            "observed_diff": round(observed_diffs[k], 4),
            "effect_size": _effect_label(k, observed_diffs[k]),
            "null_mean_diff": round(statistics.mean(diffs), 4),
            "null_std_diff": round(statistics.pstdev(diffs), 4),
            "p_value_two_sided": p_value,
            "significant": p_value < 0.05,
        }
    return results


def main():
    conn = get_db()
    params = load_params(conn)
    log.info(f"Live params: score_proposal_min={params['score_proposal_min']}")

    symbols = get_universe_symbols(conn)
    spy_dates, spy_closes, _, _ = load_series(conn, "SPY")
    if not spy_closes:
        log.error("No SPY history -- aborting")
        conn.close()
        return

    groups = {
        "trend": {"bullish": [], "bearish": []},
        "choch": {"choch": [], "no_choch": []},
        "bos": {"bos": [], "no_bos": []},
    }
    n_symbols_used = 0

    for idx, sym in enumerate(symbols):
        dates, closes, highs, lows = load_series(conn, sym)
        if not closes or len(closes) < WINDOW + FORWARD_DAYS + 1:
            continue
        try:
            results = backtest_symbol(sym, dates, closes, highs, lows, spy_dates, spy_closes, params)
        except Exception as e:
            log.warning(f"{sym}: backtest failed: {e}")
            continue

        subset = [r for r in results if r["score"] >= params["score_proposal_min"] and r["excess_return_vs_spy"] is not None]
        if not subset:
            continue
        subset.sort(key=lambda r: r["date"])
        apply_episode_dedup(subset)
        episodes = [r for r in subset if r["is_episode_start"]]
        if not episodes:
            continue
        n_symbols_used += 1

        daily_ohlc_all = load_daily_ohlc(conn, sym)
        daily_dates_all = [b[0] for b in daily_ohlc_all]

        for ep in episodes:
            combined = classify_episode_structure(ep["date"], daily_ohlc_all, daily_dates_all)

            if combined["trend"] == "bullish":
                groups["trend"]["bullish"].append(ep)
            elif combined["trend"] == "bearish":
                groups["trend"]["bearish"].append(ep)

            (groups["choch"]["choch"] if combined["choch"] else groups["choch"]["no_choch"]).append(ep)
            (groups["bos"]["bos"] if combined["bos"] else groups["bos"]["no_bos"]).append(ep)

        if (idx + 1) % 50 == 0:
            log.info(f"...{idx + 1}/{len(symbols)} symbols processed")

    conn.close()
    log.info(
        f"Episodes: trend bullish={len(groups['trend']['bullish'])} bearish={len(groups['trend']['bearish'])} "
        f"| choch={len(groups['choch']['choch'])} no_choch={len(groups['choch']['no_choch'])} "
        f"| bos={len(groups['bos']['bos'])} no_bos={len(groups['bos']['no_bos'])}"
    )

    trend_result = run_group_comparison(groups["trend"]["bullish"], groups["trend"]["bearish"], N_PERMUTATIONS, RANDOM_SEED)
    choch_result = run_group_comparison(groups["choch"]["choch"], groups["choch"]["no_choch"], N_PERMUTATIONS, RANDOM_SEED + 1)
    bos_result = run_group_comparison(groups["bos"]["bos"], groups["bos"]["no_bos"], N_PERMUTATIONS, RANDOM_SEED + 2)

    report = {
        "experiment_id": EXPERIMENT_ID,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": GIT_COMMIT,
        "config": {
            "score_proposal_min_used": params["score_proposal_min"],
            "window_days": WINDOW, "forward_days": FORWARD_DAYS,
            "n_permutations": N_PERMUTATIONS, "random_seed": RANDOM_SEED,
            "min_group_n": MIN_GROUP_N,
        },
        "universe_size": len(symbols),
        "n_symbols_with_episodes": n_symbols_used,
        "structure_trend": trend_result,
        "choch_warning": choch_result,
        "bos_confirmation": bos_result,
    }

    with open("/tmp/backtest_results_009.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info("Full results written to /tmp/backtest_results_009.json")

    def _flagged_metrics(result):
        """Metrics worth a second look: either statistically significant,
        or a notable effect size regardless of p-value (small-sample real
        effects deserve more data, not dismissal by p-value alone)."""
        if not result:
            return []
        return [k for k, r in result.items() if r["significant"] or r["effect_size"] == "notable"]

    save_backtest_result(
        EXPERIMENT_ID, GIT_COMMIT, report,
        summary=(
            f"trend flagged={_flagged_metrics(trend_result)} "
            f"| choch flagged={_flagged_metrics(choch_result)} "
            f"| bos flagged={_flagged_metrics(bos_result)}"
        ),
    )
    log.info("Results also saved to backtest_results table")

    print(f"\n=== Experiment {EXPERIMENT_ID} (commit {GIT_COMMIT[:8]}) ===")
    print(f"Universe: {len(symbols)} symbols ({n_symbols_used} contributed episodes) | {N_PERMUTATIONS} permutations\n")

    def _print_group(title, result, label_a, label_b):
        if not result:
            print(f"--- {title}: {label_a} vs {label_b} ---")
            print(f"Too few episodes in one or both groups (need {MIN_GROUP_N}+ each)\n")
            return
        n_a, n_b = next(iter(result.values()))["n_a"], next(iter(result.values()))["n_b"]
        print(f"--- {title}: {label_a} (n={n_a}) vs {label_b} (n={n_b}) ---")
        for metric, r in result.items():
            sig_flag = "SIGNIFICANT" if r["significant"] else "not significant"
            print(f"  {metric:<24} {label_a}={r['stat_a']:>+8.3f}  {label_b}={r['stat_b']:>+8.3f}  "
                  f"gap={r['observed_diff']:>+8.3f}  effect={r['effect_size']:<11} p={r['p_value_two_sided']:.4f}  ({sig_flag})")
            if r["significant"] and r["effect_size"] == "negligible":
                print(f"    -> statistically detectable but tiny -- likely not worth acting on")
            elif not r["significant"] and r["effect_size"] == "notable":
                print(f"    -> real-sized gap but not yet significant -- candidate for more data, not dismissal")
        print()

    _print_group("Top-down structure trend", trend_result, "bullish", "bearish")
    _print_group("CHoCH warning", choch_result, "present", "absent")
    _print_group("BOS confirmation", bos_result, "present", "absent")

    print("Interpretation: each row tests whether that RISK metric (not mean return -- see Experiment 008 for")
    print("that) differs between the two groups more than random label-shuffling would produce. p < 0.05 means")
    print("the structure label carries real information about that specific risk dimension. 'effect' is the raw")
    print(f"gap size independent of p-value (stopped_out_rate: notable >={_NOTABLE_PP:.0f}pp, negligible <{_NEGLIGIBLE_PP:.0f}pp;")
    print(f"return-based metrics: notable >={_NOTABLE_PCT:.1f}pp, negligible <{_NEGLIGIBLE_PCT:.1f}pp) -- a metric can be")
    print("significant-but-negligible (real but not worth acting on) or notable-but-not-yet-significant (worth")
    print("more data before concluding either way). A metric can be significant here even though Experiment 008")
    print("found no mean-return difference -- that's exactly the \"reduces risk without changing average return\"")
    print("hypothesis this experiment exists to test. Still not proof of a tradeable edge after costs, and still")
    print("subject to the same caveats Experiment 007/008 note (single historical window, episodes not fully")
    print("independent across correlated symbols/dates).")


if __name__ == "__main__":
    main()

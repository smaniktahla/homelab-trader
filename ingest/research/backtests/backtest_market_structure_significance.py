#!/usr/bin/env python3
"""Experiment 008: Market Structure Engine significance test.

Research artifact, not production logic. Same standing precedent
Experiment 007 established for shared/regime_scoring.py's
regime_scoring_enabled flag now applies to shared/structure_scoring.py's
structure_scoring_enabled flag (also default 0/off, see PR #41): a
permutation significance test against real history before the switch is
ever flipped on, not just "the badges look reasonable in the dashboard."

Question: for REAL mean-reversion buy episodes (same score_signal()/
compute_rsi/compute_bollinger pipeline Experiments 002/005/007 already
validated, reused directly here, zero drift risk), does the Market
Structure Engine's top-down trend/BOS/CHoCH classification
(shared/market_structure.py) actually predict forward excess return, or
is it just as likely to be noise? Concretely tests the exact three
signals shared/structure_scoring.py's default config assumes matter:
  1. Top-down trend: "bullish" vs "bearish" (structure_trend_bullish=+10,
     structure_trend_bearish=-10)
  2. CHoCH warning present vs absent (structure_choch_penalty=-10)
  3. BOS confirmation present vs absent (structure_bos_bonus=+5)

Method: three independent two-group permutation tests over the SAME real
episode pool, same "shuffle group labels, hold real returns fixed" method
as Experiment 007's two_group_permutation_test (imported directly, not
reimplemented).

Every classification is computed AS-OF the real episode's own entry date
via the PURE classify_timeframe_structure()/combine_timeframe_structures()
functions imported directly from shared/market_structure.py, fed daily
OHLC as-of-sliced via the same regime_common.asof_index bisect helper
every other regime-like backtest here uses (zero lookahead), then
resampled to weekly/monthly via market_structure.resample_weekly/
resample_monthly -- the exact same functions compute_market_structure()
uses live, so there's no risk of this backtest's structure classification
drifting from what's actually running in production.

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 research/backtests/backtest_market_structure_significance.py
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_score_calibration import (
    get_db, get_universe_symbols, load_series, backtest_symbol, apply_episode_dedup,
    WINDOW, FORWARD_DAYS,
)
from backtest_hierarchy_regime_significance import two_group_permutation_test, MIN_GROUP_N
from signals import load_params
from market_structure import classify_timeframe_structure, combine_timeframe_structures, resample_weekly, resample_monthly
from regime_common import load_daily_ohlc, asof_index
from db_utils import save_backtest_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EXPERIMENT_ID = "008_market_structure_significance"
GIT_COMMIT = os.environ.get("BACKTEST_GIT_COMMIT", "unknown")
N_PERMUTATIONS = int(os.environ.get("N_PERMUTATIONS", "5000"))
RANDOM_SEED = 42


def classify_episode_structure(episode_date, daily_ohlc_all, daily_dates_all):
    """Everything needed to bucket one real episode: the combined top-down
    structure classification as-of episode_date, no lookahead. Mirrors
    market_structure.compute_market_structure()'s own resample-then-
    classify-then-combine pipeline exactly, just fed a historical array
    slice instead of a live DB query."""
    idx = asof_index(daily_dates_all, episode_date)
    daily_ohlc = daily_ohlc_all[:idx + 1] if idx is not None else []
    weekly_ohlc = resample_weekly(daily_ohlc)
    monthly_ohlc = resample_monthly(daily_ohlc)
    daily_ctx = classify_timeframe_structure(daily_ohlc)
    weekly_ctx = classify_timeframe_structure(weekly_ohlc)
    monthly_ctx = classify_timeframe_structure(monthly_ohlc)
    return combine_timeframe_structures(monthly_ctx, weekly_ctx, daily_ctx)


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

    trend_episodes = {"bullish": [], "bearish": [], "excluded": 0}
    choch_episodes = {"choch": [], "no_choch": []}
    bos_episodes = {"bos": [], "no_bos": []}
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
            er = ep["excess_return_vs_spy"]

            if combined["trend"] == "bullish":
                trend_episodes["bullish"].append(er)
            elif combined["trend"] == "bearish":
                trend_episodes["bearish"].append(er)
            else:
                trend_episodes["excluded"] += 1

            (choch_episodes["choch"] if combined["choch"] else choch_episodes["no_choch"]).append(er)
            (bos_episodes["bos"] if combined["bos"] else bos_episodes["no_bos"]).append(er)

        if (idx + 1) % 50 == 0:
            log.info(f"...{idx + 1}/{len(symbols)} symbols processed")

    conn.close()
    log.info(
        f"Episodes: trend bullish={len(trend_episodes['bullish'])} bearish={len(trend_episodes['bearish'])} "
        f"excluded={trend_episodes['excluded']} (mixed/insufficient_data) "
        f"| choch={len(choch_episodes['choch'])} no_choch={len(choch_episodes['no_choch'])} "
        f"| bos={len(bos_episodes['bos'])} no_bos={len(bos_episodes['no_bos'])}"
    )

    trend_result = two_group_permutation_test(
        trend_episodes["bullish"], trend_episodes["bearish"], N_PERMUTATIONS, RANDOM_SEED)
    choch_result = two_group_permutation_test(
        choch_episodes["choch"], choch_episodes["no_choch"], N_PERMUTATIONS, RANDOM_SEED + 1)
    bos_result = two_group_permutation_test(
        bos_episodes["bos"], bos_episodes["no_bos"], N_PERMUTATIONS, RANDOM_SEED + 2)

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

    with open("/tmp/backtest_results_008.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info("Full results written to /tmp/backtest_results_008.json")

    def _p(r):
        return r["p_value_two_sided"] if r else "n/a (too few episodes)"

    save_backtest_result(
        EXPERIMENT_ID, GIT_COMMIT, report,
        summary=f"trend: p={_p(trend_result)} | choch: p={_p(choch_result)} | bos: p={_p(bos_result)}",
    )
    log.info("Results also saved to backtest_results table")

    print(f"\n=== Experiment {EXPERIMENT_ID} (commit {GIT_COMMIT[:8]}) ===")
    print(f"Universe: {len(symbols)} symbols ({n_symbols_used} contributed episodes) | {N_PERMUTATIONS} permutations\n")

    def _print_result(title, result, label_a, label_b):
        print(f"--- {title} ---")
        if result:
            r = result
            print(f"{label_a} (n={r['n_a']}): avg_excess_return={r['mean_a']:+.2f}%")
            print(f"{label_b} (n={r['n_b']}): avg_excess_return={r['mean_b']:+.2f}%")
            print(f"observed gap: {r['observed_diff']:+.2f}pp  |  null gap: {r['null_mean_diff']:+.2f}pp ± {r['null_std_diff']:.2f}")
            print(f"p-value (two-sided): {r['p_value_two_sided']}  "
                  f"({'SIGNIFICANT at p<0.05' if r['p_value_two_sided'] < 0.05 else 'not significant at p<0.05'})")
        else:
            print(f"Too few episodes in one or both groups (need {MIN_GROUP_N}+ each)")
        print()

    _print_result("Top-down structure trend (bullish vs bearish)", trend_result, "bullish", "bearish")
    _print_result("CHoCH warning present vs absent", choch_result, "choch", "no_choch")
    _print_result("BOS confirmation present vs absent", bos_result, "bos", "no_bos")

    print("Interpretation: p_value is the fraction of label-shuffled permutations (same real returns, structure")
    print("label reassigned at random, group sizes held fixed) that produced as large a group-mean gap as the")
    print("real one. p < 0.05 means the structure label is unlikely to be uninformative noise -- not proof of a")
    print("tradeable edge after costs, just that it's worth a real paper-trading window with")
    print("structure_scoring_enabled=1 to confirm. p >= 0.05 on any of the three tests means: on this sample,")
    print("that specific signal (trend/CHoCH/BOS) doesn't look distinguishable from random labeling -- do not")
    print("enable its corresponding structure_scoring.py adjustment on the strength of the badge alone.")


if __name__ == "__main__":
    main()

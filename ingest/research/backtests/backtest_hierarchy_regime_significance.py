#!/usr/bin/env python3
"""Experiment 007: hierarchical regime (market x sector x stock) significance test.

Research artifact, not production logic. shared/hierarchy_regime.py's
docstring precedent (following global_market_signals' own rule before this
codebase lets a signal near live scoring) requires an Experiment-005-style
permutation significance test before shared/regime_scoring.py's
regime_scoring_enabled flag (default 0/off) is ever flipped on. This is
that test.

Question: for REAL mean-reversion buy episodes (same score_signal()/
compute_rsi/compute_bollinger pipeline Experiments 002/005 already
validated, reused directly here, zero drift risk), does the market x sector
regime alignment shared/regime_scoring.py assumes matters actually predict
forward excess return? Concretely: regime_scoring.py's config table assumes
a market-bull + sector-bull buy is more trustworthy than a market-bull +
sector-bear buy (a "aligned" vs "misaligned" ranking, +15 vs -10). This
experiment tests whether that ranking holds up against real, out-of-sample
forward returns, or is just as likely to be noise.

Method: two independent two-group permutation tests over the SAME real
episode pool (same "does entry timing matter" spirit as Experiment 005,
but permuting REGIME LABELS across fixed real episodes rather than
resampling entry timing across fixed regime state):

  1. Market x sector ALIGNMENT: "aligned" (market bull + sector bull, or
     market bear + sector bear) vs "misaligned" (market bull + sector bear,
     or market bear + sector bull). Shuffles which episodes are aligned vs
     misaligned (counts held fixed) and recomputes the mean-excess-return
     gap each shuffle -- the real gap is significant if it sits in the tail
     of that null distribution.
  2. Stock-vs-sector RELATIVE STRENGTH: "outperforming_sector" vs
     "underperforming_sector" (shared/security_regime.py's own
     classification), same permutation structure.

Every regime classification is computed AS-OF the real episode's own
entry date (bisect-based as-of slicing, zero lookahead) via the PURE
classify_sector_regime()/classify_security_regime() functions imported
directly from shared/sector_regime.py/shared/security_regime.py -- same
"reuse the live classification code, feed it historical arrays instead of
a live fetch" pattern backtest_portfolio_montecarlo.py's
historical_market_context() and this file's own market-regime lookup
(market_regime_history, already backfilled by an earlier PR, read
directly rather than recomputed) both already establish.

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 research/backtests/backtest_hierarchy_regime_significance.py
"""

import os
import sys
import bisect
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
from signals import load_params, load_sector_map
from sector_regime import classify_sector_regime
from security_regime import classify_security_regime
from regime_scoring import bucket_market_trend, bucket_sector_trend
from regime_common import load_daily_series, asof_index
from sector_mapping import SECTOR_ETF_MAP, get_sector_etf
from db_utils import save_backtest_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EXPERIMENT_ID = "007_hierarchy_regime_significance"
GIT_COMMIT = os.environ.get("BACKTEST_GIT_COMMIT", "unknown")
N_PERMUTATIONS = int(os.environ.get("N_PERMUTATIONS", "5000"))
RANDOM_SEED = 42
MIN_GROUP_N = 20  # below this, a group's mean is too noisy to compare at all


def load_market_regime_history_asof(conn):
    """(dates, overall) ascending, for a bisect-based as-of lookup --
    reuses the already-backfilled market_regime_history table (see
    docs/... market_regime_history PR) directly rather than recomputing
    SPY/QQQ/VIX classification a second time in this script."""
    with conn.cursor() as cur:
        cur.execute("SELECT trading_date, overall FROM market_regime_history ORDER BY trading_date ASC")
        rows = cur.fetchall()
    return [r[0] for r in rows], [r[1] for r in rows]


def market_overall_asof(market_dates, market_overalls, target_date):
    i = asof_index(market_dates, target_date)
    return market_overalls[i] if i is not None else "unknown"


def classify_episode_regime(episode_date, stock_dates, stock_closes, sector_dates, sector_closes,
                             spy_dates, spy_closes, market_dates, market_overalls):
    """Everything needed to bucket one real episode: market_overall (as-of,
    from the backfilled table), sector classification (as-of, pure
    function fed as-of-sliced arrays), stock classification + vs_sector
    (same). No lookahead anywhere -- every slice stops at episode_date."""
    market_overall = market_overall_asof(market_dates, market_overalls, episode_date)

    si = asof_index(stock_dates, episode_date)
    stock_closes_upto = stock_closes[:si + 1] if si is not None else []

    spy_i = asof_index(spy_dates, episode_date)
    spy_closes_upto = spy_closes[:spy_i + 1] if spy_i is not None else []

    sector_closes_upto = []
    if sector_dates is not None:
        sec_i = asof_index(sector_dates, episode_date)
        sector_closes_upto = sector_closes[:sec_i + 1] if sec_i is not None else []

    sector_ctx = classify_sector_regime(sector_closes_upto, spy_closes_upto) if sector_dates is not None else None
    stock_ctx = classify_security_regime(stock_closes_upto, sector_closes_upto, spy_closes_upto)

    sector_classification = sector_ctx["classification"] if sector_ctx else "unknown"
    return {
        "market_overall": market_overall,
        "sector_classification": sector_classification,
        "stock_vs_sector_classification": stock_ctx["vs_sector_classification"],
    }


def two_group_permutation_test(values_a, values_b, n_permutations, seed):
    """Standard two-sided permutation test for a difference in means.
    Shuffles the pooled real values and re-splits into groups of the same
    sizes as the real a/b split, each permutation -- this tests whether
    the LABEL (which group an episode's real return got assigned to)
    carries information, holding the actual observed returns fixed. Two-
    sided since the direction regime_scoring.py's config assumes is itself
    an untested hypothesis, not a given."""
    if len(values_a) < MIN_GROUP_N or len(values_b) < MIN_GROUP_N:
        return None
    rng = random.Random(seed)
    observed_diff = statistics.mean(values_a) - statistics.mean(values_b)
    combined = list(values_a) + list(values_b)
    n_a = len(values_a)

    null_diffs = []
    for _ in range(n_permutations):
        rng.shuffle(combined)
        null_diffs.append(statistics.mean(combined[:n_a]) - statistics.mean(combined[n_a:]))

    count_ge = sum(1 for d in null_diffs if abs(d) >= abs(observed_diff))
    p_value = round((1 + count_ge) / (1 + len(null_diffs)), 5)

    return {
        "n_a": len(values_a), "n_b": len(values_b),
        "mean_a": round(statistics.mean(values_a), 4), "mean_b": round(statistics.mean(values_b), 4),
        "observed_diff": round(observed_diff, 4),
        "null_mean_diff": round(statistics.mean(null_diffs), 4),
        "null_std_diff": round(statistics.pstdev(null_diffs), 4),
        "p_value_two_sided": p_value,
    }


def main():
    conn = get_db()
    params = load_params(conn)
    log.info(f"Live params: score_proposal_min={params['score_proposal_min']}")

    symbols = get_universe_symbols(conn)
    sector_map = load_sector_map(conn, symbols)
    spy_dates, spy_closes, _, _ = load_series(conn, "SPY")
    market_dates, market_overalls = load_market_regime_history_asof(conn)
    if not spy_closes:
        log.error("No SPY history -- aborting")
        conn.close()
        return
    if not market_dates:
        log.error("market_regime_history is empty -- run ingest/backfill_market_regime_history.py first")
        conn.close()
        return

    sector_series = {}  # etf symbol -> (dates, closes), loaded once per distinct sector
    for sector in set(sector_map.values()):
        etf = get_sector_etf(sector)
        if etf and etf not in sector_series:
            sector_series[etf] = load_daily_series(conn, etf)
    log.info(f"Loaded {len(sector_series)} sector ETF series: {sorted(sector_series)}")

    alignment_episodes = {"aligned": [], "misaligned": [], "excluded": 0}
    relstrength_episodes = {"outperforming_sector": [], "underperforming_sector": [], "excluded": 0}
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

        sector = sector_map.get(sym)
        etf = get_sector_etf(sector) if sector else None
        sector_dates, sector_closes = sector_series.get(etf, (None, None)) if etf else (None, None)

        for ep in episodes:
            regime = classify_episode_regime(
                ep["date"], dates, closes, sector_dates, sector_closes,
                spy_dates, spy_closes, market_dates, market_overalls,
            )
            m = bucket_market_trend(regime["market_overall"])
            s = bucket_sector_trend(regime["sector_classification"])
            if m and s and m == s:
                alignment_episodes["aligned"].append(ep["excess_return_vs_spy"])
            elif m and s and m != s and s != "neutral":
                alignment_episodes["misaligned"].append(ep["excess_return_vs_spy"])
            else:
                alignment_episodes["excluded"] += 1

            vs_sector = regime["stock_vs_sector_classification"]
            if vs_sector == "outperforming_sector":
                relstrength_episodes["outperforming_sector"].append(ep["excess_return_vs_spy"])
            elif vs_sector == "underperforming_sector":
                relstrength_episodes["underperforming_sector"].append(ep["excess_return_vs_spy"])
            else:
                relstrength_episodes["excluded"] += 1

        if (idx + 1) % 100 == 0:
            log.info(f"...{idx + 1}/{len(symbols)} symbols processed")

    conn.close()
    log.info(
        f"Episodes: aligned={len(alignment_episodes['aligned'])} "
        f"misaligned={len(alignment_episodes['misaligned'])} "
        f"excluded={alignment_episodes['excluded']} (market/sector regime unclassifiable or sector neutral) "
        f"| outperforming_sector={len(relstrength_episodes['outperforming_sector'])} "
        f"underperforming_sector={len(relstrength_episodes['underperforming_sector'])}"
    )

    alignment_result = two_group_permutation_test(
        alignment_episodes["aligned"], alignment_episodes["misaligned"], N_PERMUTATIONS, RANDOM_SEED)
    relstrength_result = two_group_permutation_test(
        relstrength_episodes["outperforming_sector"], relstrength_episodes["underperforming_sector"],
        N_PERMUTATIONS, RANDOM_SEED + 1)

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
        "market_sector_alignment": alignment_result,
        "stock_vs_sector_relative_strength": relstrength_result,
    }

    with open("/tmp/backtest_results_007.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info("Full results written to /tmp/backtest_results_007.json")

    save_backtest_result(
        EXPERIMENT_ID, GIT_COMMIT, report,
        summary=(
            f"alignment: p={alignment_result['p_value_two_sided'] if alignment_result else 'n/a (too few episodes)'} "
            f"| rel_strength: p={relstrength_result['p_value_two_sided'] if relstrength_result else 'n/a (too few episodes)'}"
        ),
    )
    log.info("Results also saved to backtest_results table")

    print(f"\n=== Experiment {EXPERIMENT_ID} (commit {GIT_COMMIT[:8]}) ===")
    print(f"Universe: {len(symbols)} symbols ({n_symbols_used} contributed episodes) | {N_PERMUTATIONS} permutations\n")

    print("--- Market x Sector alignment (regime_scoring.py's core assumption) ---")
    if alignment_result:
        r = alignment_result
        print(f"aligned (n={r['n_a']}): avg_excess_return={r['mean_a']:+.2f}%")
        print(f"misaligned (n={r['n_b']}): avg_excess_return={r['mean_b']:+.2f}%")
        print(f"observed gap: {r['observed_diff']:+.2f}pp  |  null gap: {r['null_mean_diff']:+.2f}pp ± {r['null_std_diff']:.2f}")
        print(f"p-value (two-sided): {r['p_value_two_sided']}  "
              f"({'SIGNIFICANT at p<0.05' if r['p_value_two_sided'] < 0.05 else 'not significant at p<0.05'})")
    else:
        print(f"Too few episodes in one or both groups (need {MIN_GROUP_N}+ each) -- "
              f"aligned={len(alignment_episodes['aligned'])} misaligned={len(alignment_episodes['misaligned'])}")

    print("\n--- Stock vs sector relative strength ---")
    if relstrength_result:
        r = relstrength_result
        print(f"outperforming_sector (n={r['n_a']}): avg_excess_return={r['mean_a']:+.2f}%")
        print(f"underperforming_sector (n={r['n_b']}): avg_excess_return={r['mean_b']:+.2f}%")
        print(f"observed gap: {r['observed_diff']:+.2f}pp  |  null gap: {r['null_mean_diff']:+.2f}pp ± {r['null_std_diff']:.2f}")
        print(f"p-value (two-sided): {r['p_value_two_sided']}  "
              f"({'SIGNIFICANT at p<0.05' if r['p_value_two_sided'] < 0.05 else 'not significant at p<0.05'})")
    else:
        print(f"Too few episodes in one or both groups (need {MIN_GROUP_N}+ each) -- "
              f"outperforming={len(relstrength_episodes['outperforming_sector'])} "
              f"underperforming={len(relstrength_episodes['underperforming_sector'])}")

    print("\nInterpretation: p_value is the fraction of label-shuffled permutations (same real returns, regime")
    print("label reassigned at random, group sizes held fixed) that produced as large a group-mean gap as the")
    print("real one. p < 0.05 means the regime label is unlikely to be uninformative noise -- i.e. there's a real")
    print("association between hierarchy alignment / relative strength and forward return, not proof of a")
    print("tradeable edge after costs (that's what a real paper-trading window with regime_scoring_enabled=1")
    print("would need to confirm next). p >= 0.05 means: on this sample, this cut doesn't look distinguishable")
    print("from random labeling -- do not enable regime_scoring_enabled on the strength of the badge alone.")


if __name__ == "__main__":
    main()

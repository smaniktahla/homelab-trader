#!/usr/bin/env python3
"""Experiment 011: paired significance test + definition-sensitivity check
for the relative-strength portfolio gate.

Research artifact, not production logic. Two phases, both requested
before this candidate feature earns a production scoping pass (see
Experiment 010's own review):

Phase A -- paired significance test on Experiment 010's already-run 40
window-matched baseline/gate portfolio simulations. Loads the latest
saved "010_portfolio_relative_strength" row from backtest_results
directly (no re-simulation -- the 40 paired runs already exist and cost
~25 minutes to produce once). For each window, computes gate MINUS
baseline for total_return_pct, sharpe, sortino, stop_out_rate,
max_drawdown_pct, and exposure_adjusted_return_pct, then runs a PAIRED
sign-flip permutation test (not the two-group/unpaired shuffle
Experiments 007/008/009 use for independent episode pools -- these 40
windows are paired by construction, same start date, same everything
except the gate, so the correct null hypothesis is "each window's sign
of the gate-vs-baseline difference is a coin flip", tested by randomly
flipping each diff's sign 5000 times and comparing the real mean
difference to that null distribution) plus a percentile bootstrap 95% CI
on the median difference.

Phase B -- definition-sensitivity check, requested explicitly to guard
against overfitting to one arbitrary relative-strength definition: if
"underperforming its sector" only produces this portfolio effect under
exactly the definition Experiment 010 happened to use, that's a red flag,
not a confirmation. Runs THREE fresh, freshly-seeded (reproducible)
paired Monte Carlo comparisons, same baseline pipeline as Experiment 010,
against three vs-sector-classification variants that are DIFFERENT from
each other on purpose (not tuned to make the effect look good -- see each
variant's docstring for why it was chosen):
  - gate_default          -- the real production classify_security_regime,
    identical to Experiment 010's "gate" variant.
  - gate_short_lookback   -- a faster relative-strength window (20d ratio-
    SMA / 10d return, vs. the default's 50d/20d) -- tests whether the
    effect depends on the specific time horizon chosen.
  - gate_strict_threshold -- same windows as default, but requires a
    stronger majority-negative score (<=-2, not merely <0) before gating
    -- a stricter, more conservative bar for "underperforming."
Each variant vs. its own paired baseline goes through the exact same
Phase A significance machinery. Note Phase B's baseline is a FRESH run on
a NEWLY seeded date pool (RANDOM_SEED below), not Experiment 010's saved
baseline -- Experiment 010 didn't fix its random seed, so its exact 40
start dates aren't reproducible after the fact; reusing Phase A's
already-paired data for the primary significance result and running a
freshly seeded, self-consistent 4-way comparison for the sensitivity
check keeps each phase internally valid without silently mixing
differently-sampled window pools.

Does NOT modify any production default. Not part of the recurring ingest
loop. Run manually:
    docker exec invest-ingest python3 research/backtests/backtest_portfolio_relative_strength_significance.py
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_portfolio_montecarlo import (
    get_db, get_universe_symbols, get_sector_map, load_series, load_sector_series,
    run_single_backtest, eligible_start_indices, STARTING_CASH, HORIZON_DAYS, MC_RUNS, WINDOW,
)
from backtest_portfolio_relative_strength import compute_run_metrics
from signals import load_params
from regime_common import ratio_series, sma, slope_positive, pct_return, score_inputs
from db_utils import save_backtest_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EXPERIMENT_ID = "011_relative_strength_significance"
GIT_COMMIT = os.environ.get("BACKTEST_GIT_COMMIT", "unknown")
N_PERMUTATIONS = int(os.environ.get("N_PERMUTATIONS", "5000"))
N_BOOTSTRAP = int(os.environ.get("N_BOOTSTRAP", "5000"))
RANDOM_SEED = 42
MIN_PAIRS = 10

METRICS = ["total_return_pct", "sharpe", "sortino", "stop_out_rate", "max_drawdown_pct",
           "exposure_adjusted_return_pct"]


# ─────────────────────────────────────────────────────────────────────────
# Phase A: paired significance test (generic -- reused by Phase B too)
# ─────────────────────────────────────────────────────────────────────────

def paired_sign_flip_test(diffs, n_permutations, seed):
    """Null hypothesis: each window's gate-vs-baseline difference is
    equally likely to have been + or - (the gate carries no information).
    Flips each diff's sign at random 50/50, recomputes the mean, and
    checks how often that null mean is at least as extreme as the real
    one -- the correct permutation structure for PAIRED data, unlike the
    two-group shuffle Experiments 007/008/009 use for independent
    episodes."""
    if len(diffs) < MIN_PAIRS:
        return None
    rng = random.Random(seed)
    observed_mean = statistics.mean(diffs)
    null_means = []
    for _ in range(n_permutations):
        flipped = [d if rng.random() < 0.5 else -d for d in diffs]
        null_means.append(statistics.mean(flipped))
    count_ge = sum(1 for m in null_means if abs(m) >= abs(observed_mean))
    p_value = round((1 + count_ge) / (1 + len(null_means)), 5)
    return {
        "n": len(diffs),
        "observed_mean_diff": round(observed_mean, 4),
        "observed_median_diff": round(statistics.median(diffs), 4),
        "null_mean_of_means": round(statistics.mean(null_means), 4),
        "null_std_of_means": round(statistics.pstdev(null_means), 4),
        "p_value_two_sided": p_value,
    }


def bootstrap_median_ci(diffs, n_bootstrap, seed, confidence=0.95):
    """Percentile bootstrap CI on the median paired difference -- resample
    the diffs with replacement, take the median each time, report the
    2.5th/97.5th percentiles of that distribution."""
    if len(diffs) < MIN_PAIRS:
        return None
    rng = random.Random(seed)
    n = len(diffs)
    medians = sorted(statistics.median(diffs[rng.randrange(n)] for _ in range(n)) for _ in range(n_bootstrap))
    lo_idx = int((1 - confidence) / 2 * n_bootstrap)
    hi_idx = min(int((1 - (1 - confidence) / 2) * n_bootstrap), n_bootstrap - 1)
    return {
        "median": round(statistics.median(diffs), 4),
        "ci_lower": round(medians[lo_idx], 4),
        "ci_upper": round(medians[hi_idx], 4),
        "confidence": confidence,
    }


def paired_diff_analysis(runs_a, runs_b, metric, seed):
    """runs_a/runs_b: index-aligned lists of per-window metric dicts
    (same window i in both -- caller's responsibility to ensure pairing).
    Returns diffs (b - a) plus the sign-flip test and bootstrap CI."""
    diffs = [
        b[metric] - a[metric] for a, b in zip(runs_a, runs_b)
        if a.get(metric) is not None and b.get(metric) is not None
    ]
    return {
        "test": paired_sign_flip_test(diffs, N_PERMUTATIONS, seed),
        "bootstrap_ci": bootstrap_median_ci(diffs, N_BOOTSTRAP, seed + 1),
    }


def load_latest_experiment_results(experiment_id):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT results, run_at FROM backtest_results WHERE experiment_id=%s ORDER BY run_at DESC LIMIT 1",
                (experiment_id,),
            )
            row = cur.fetchone()
        return (row[0], row[1]) if row else (None, None)
    finally:
        conn.close()


def run_phase_a():
    log.info("--- Phase A: paired significance test on Experiment 010's saved 40 windows ---")
    results, run_at = load_latest_experiment_results("010_portfolio_relative_strength")
    if not results:
        log.error("No saved '010_portfolio_relative_strength' row found in backtest_results -- run "
                   "backtest_portfolio_relative_strength.py first.")
        return None
    log.info(f"Loaded Experiment 010 results from {run_at}")

    baseline_runs = results["results"]["baseline"]["runs"]
    gate_runs = results["results"]["gate"]["runs"]
    n_pairs = min(len(baseline_runs), len(gate_runs))
    log.info(f"{n_pairs} paired windows (index-aligned -- both variants ran the same chosen start dates "
              f"in the same order within that experiment)")

    phase_a = {}
    for i, metric in enumerate(METRICS):
        phase_a[metric] = paired_diff_analysis(baseline_runs[:n_pairs], gate_runs[:n_pairs], metric, RANDOM_SEED + i)
    return {"n_pairs": n_pairs, "source_run_at": str(run_at), "metrics": phase_a}


# ─────────────────────────────────────────────────────────────────────────
# Phase B: definition-sensitivity variants
# ─────────────────────────────────────────────────────────────────────────

def make_rs_classify_variant(rs_sma, return_lookback, underperform_threshold):
    """Builds a (stock_closes, sector_closes, market_closes) ->
    {"vs_sector_classification": ...} function -- same calling convention
    as shared/security_regime.py's classify_security_regime, so it's a
    drop-in for vs_sector_classification()'s classify_fn override. Reuses
    regime_common's primitives directly (ratio_series/sma/slope_positive/
    pct_return/score_inputs), same as classify_security_regime itself
    does -- this is a deliberately smaller vs-sector-only re-derivation
    for the sensitivity check, not a copy of the whole function."""
    def _classify(stock_closes, sector_closes, market_closes):
        if not sector_closes:
            return {"vs_sector_classification": "unknown"}
        rs = ratio_series(stock_closes, sector_closes)
        inputs = {}
        if len(rs) >= rs_sma:
            rs_sma_val = sma(rs, rs_sma)
            inputs["ratio_above_sma"] = (rs[-1] > rs_sma_val) if rs_sma_val else None
            inputs["ratio_slope_positive"] = slope_positive(rs, rs_sma)
        else:
            inputs["ratio_above_sma"] = None
            inputs["ratio_slope_positive"] = None
        stock_r = pct_return(stock_closes, return_lookback)
        sector_r = pct_return(sector_closes, return_lookback)
        inputs["return_beats_sector"] = (
            (stock_r - sector_r) > 0 if (stock_r is not None and sector_r is not None) else None
        )
        score, evidence, _total = score_inputs(inputs)
        if evidence == 0:
            classification = "unknown"
        elif score <= underperform_threshold:
            classification = "underperforming_sector"
        elif score > 0:
            classification = "outperforming_sector"
        else:
            classification = "in_line_with_sector"
        return {"vs_sector_classification": classification}
    return _classify


# rs_sma, return_lookback, underperform_threshold. "default" mirrors
# classify_security_regime's own vs-sector inputs exactly (RS_SMA=50,
# 20d return, score<0 i.e. threshold=-1) -- included as its own variant so
# Phase B's fresh run is directly comparable to itself, not just to
# Experiment 010's differently-sampled windows.
RS_DEFINITIONS = {
    "gate_default": (50, 20, -1),
    "gate_short_lookback": (20, 10, -1),
    "gate_strict_threshold": (50, 20, -2),
}


def run_phase_b():
    log.info("--- Phase B: definition-sensitivity check (fresh seeded paired runs) ---")
    conn = get_db()
    p = load_params(conn)
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

    if not spy_series[0]:
        log.error("No SPY history -- aborting Phase B")
        return None

    spy_dates = spy_series[0]
    starts = eligible_start_indices(spy_dates, HORIZON_DAYS)
    rng = random.Random(RANDOM_SEED)
    chosen = rng.sample(starts, min(MC_RUNS, len(starts)))
    log.info(f"Phase B: {len(chosen)} paired windows (seed={RANDOM_SEED}, reproducible), "
              f"{len(RS_DEFINITIONS) + 1} variants (baseline + {len(RS_DEFINITIONS)} definitions)")

    def _run_variant(rs_policy, rs_classify_fn, label):
        run_metrics = []
        for n, start_i in enumerate(chosen, 1):
            result = run_single_backtest(
                symbols, series, spy_series, qqq_series, vix_series, sector_map, p, start_i, HORIZON_DAYS,
                rs_policy=rs_policy, sector_series=sector_series, rs_classify_fn=rs_classify_fn,
            )
            run_metrics.append(compute_run_metrics(result))
            if n % 10 == 0 or n == len(chosen):
                log.info(f"[{label} {n}/{len(chosen)}] return={run_metrics[-1]['total_return_pct']:+.2f}%")
        return run_metrics

    baseline_runs = _run_variant(None, None, "baseline")

    phase_b = {"n_pairs": len(chosen), "random_seed": RANDOM_SEED, "definitions": {}}
    # Deterministic per-(definition, metric) seed offset -- NOT hash(str),
    # which is randomized per-process by default in Python 3 and would
    # silently break the "reproducible" claim this experiment's docstring
    # makes about Phase B.
    for def_idx, (name, (rs_sma, return_lookback, threshold)) in enumerate(RS_DEFINITIONS.items()):
        classify_fn = make_rs_classify_variant(rs_sma, return_lookback, threshold)
        gate_runs = _run_variant({"mode": "gate"}, classify_fn, name)
        metrics = {
            metric: paired_diff_analysis(baseline_runs, gate_runs, metric,
                                          RANDOM_SEED + 100 * (def_idx + 1) + metric_idx)
            for metric_idx, metric in enumerate(METRICS)
        }
        phase_b["definitions"][name] = {
            "config": {"rs_sma": rs_sma, "return_lookback": return_lookback, "underperform_threshold": threshold},
            "metrics": metrics,
        }
    return phase_b


# ─────────────────────────────────────────────────────────────────────────

def _fmt_test(t):
    if not t:
        return "n/a (too few pairs)"
    flag = "SIGNIFICANT" if t["test"] and t["test"]["p_value_two_sided"] < 0.05 else "not significant"
    if not t["test"]:
        return "n/a"
    ci = t["bootstrap_ci"]
    ci_str = f"[{ci['ci_lower']:+.3f}, {ci['ci_upper']:+.3f}]" if ci else "n/a"
    return (f"mean_diff={t['test']['observed_mean_diff']:+.3f} median_diff={t['test']['observed_median_diff']:+.3f} "
            f"95%CI={ci_str} p={t['test']['p_value_two_sided']:.4f} ({flag})")


def main():
    phase_a = run_phase_a()
    phase_b = run_phase_b()

    report = {
        "experiment_id": EXPERIMENT_ID,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": GIT_COMMIT,
        "config": {
            "n_permutations": N_PERMUTATIONS, "n_bootstrap": N_BOOTSTRAP, "random_seed": RANDOM_SEED,
            "min_pairs": MIN_PAIRS, "metrics_tested": METRICS, "rs_definitions": RS_DEFINITIONS,
        },
        "phase_a_paired_significance": phase_a,
        "phase_b_definition_sensitivity": phase_b,
    }

    out_path = "/tmp/backtest_results_011.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info(f"Full results written to {out_path}")

    def _sig_metrics(phase_metrics):
        return [m for m, r in phase_metrics.items() if r["test"] and r["test"]["p_value_two_sided"] < 0.05]

    summary_parts = [f"phaseA sig={_sig_metrics(phase_a['metrics'])}" if phase_a else "phaseA=n/a"]
    if phase_b:
        for name, d in phase_b["definitions"].items():
            summary_parts.append(f"{name} sig={_sig_metrics(d['metrics'])}")
    save_backtest_result(EXPERIMENT_ID, GIT_COMMIT, report, summary=" | ".join(summary_parts))
    log.info("Results also saved to backtest_results table")

    print(f"\n=== Experiment {EXPERIMENT_ID} (commit {GIT_COMMIT[:8]}) ===\n")

    if phase_a:
        print(f"--- Phase A: paired significance, Experiment 010's {phase_a['n_pairs']} saved windows "
              f"(run_at={phase_a['source_run_at']}) ---")
        for metric in METRICS:
            print(f"  {metric:<28} {_fmt_test(phase_a['metrics'][metric])}")
    else:
        print("--- Phase A: SKIPPED (no saved Experiment 010 results) ---")
    print()

    if phase_b:
        print(f"--- Phase B: definition sensitivity, {phase_b['n_pairs']} fresh seeded windows "
              f"(seed={phase_b['random_seed']}) ---")
        for name, d in phase_b["definitions"].items():
            cfg = d["config"]
            print(f"\n  {name}  (rs_sma={cfg['rs_sma']}d, return_lookback={cfg['return_lookback']}d, "
                  f"underperform_threshold={cfg['underperform_threshold']})")
            for metric in METRICS:
                print(f"    {metric:<26} {_fmt_test(d['metrics'][metric])}")
    else:
        print("--- Phase B: FAILED (see log) ---")

    print("\nInterpretation: Phase A tests whether Experiment 010's already-observed gate-vs-baseline gap, per")
    print("window, is distinguishable from a coin-flip sign assignment (the correct null for PAIRED data -- NOT")
    print("the independent-episode shuffle Experiments 007-009 use). Phase B re-runs the comparison from scratch")
    print("on a freshly seeded (reproducible) window pool under three vs-sector-classification definitions that")
    print("are deliberately DIFFERENT from each other, not tuned toward significance -- if the effect only shows")
    print("up under one exact definition, that's evidence of overfitting, not a real effect. A metric significant")
    print("across Phase A AND all of Phase B's definitions is much stronger evidence than either alone.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Experiment 012: volume-confirmation hypothesis significance test.

Volume & Volume Profile epic, PR E (raw-volume hypothesis experiments,
Workstream B) -- see the 2026-08-30 DocMost epic note. Research artifact,
not production logic; same "disposable hypothesis until proven" status as
every other script in this directory.

Question: on days with an unusually large price move, does unusually high
relative volume (rvol, shared/volume_metrics.py::relative_volume) predict
different forward continuation/reversal behavior than a normal/low-volume
move of the same size? "Large move" and "high volume" are both
parameterized thresholds, not tuned to one number and declared a win (see
MOVE_THRESHOLDS/RVOL_HIGH_THRESHOLD below).

Method: within the population of days where |daily return| >=
move_threshold (the "matched move without the volume condition" baseline
the epic spec requires), split into high_rvol (rvol >= RVOL_HIGH_THRESHOLD)
and low_rvol (rvol < RVOL_HIGH_THRESHOLD) groups, then compare forward
returns across FORWARD_HORIZONS bars. Significance is a label-permutation
test (shuffle the high/low group assignment within the matched-move
population, N_PERMUTATIONS times) on the difference in mean forward
return -- the same empirical p-value / percentile-rank machinery
backtest_rule_significance.py already established, applied to a two-group
comparison instead of a real-vs-null-episodes comparison. An unconditional
baseline (all eligible days, no move or volume filter) is also reported
for context, per the epic's "baselines mandatory" requirement, though it
is not itself permutation-tested -- it answers "how unusual is any of
this," not "is the volume split real."

Feed/source discipline (mandatory per the epic spec): price_history mixes
'yahoo' (dominant, long history) and 'alpaca_iex' (secondary, ~6.5-week
backfill) volume, tagged by PR A's `source` column -- see the epic's
2026-08-30 audit note. This script filters to exactly one `source` value
per run (default 'yahoo', by far the larger sample) and never pools rows
across sources; 'unknown' (pre-migration) rows are excluded because their
provenance is indeterminate, not because they're assumed bad. Session
definition: daily close-to-close bars, i.e. full-session (not
intraday-RTH) granularity -- there is no minute/trade-level data in this
repo yet (confirmed by PR C's investigation), so this experiment cannot
and does not attempt an intraday session boundary.

Lookahead safety: rvol[i] is relative_volume(volumes[:i+1], RVOL_PERIOD),
the same self-inclusive as-of-bar-t window every other indicator in this
repo uses (compute_bollinger, compute_atr, ema) -- known only as of day
i's own close, exactly when the triggering move itself is known.

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 research/backtests/backtest_volume_confirmation.py
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

from volume_metrics import relative_volume
from db_utils import save_backtest_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EXPERIMENT_ID = "012_volume_confirmation"
GIT_COMMIT = os.environ.get("BACKTEST_GIT_COMMIT", "unknown")
N_PERMUTATIONS = int(os.environ.get("N_PERMUTATIONS", "2000"))
N_BOOTSTRAP = 1000
RANDOM_SEED = 42

SOURCE = os.environ.get("VOLUME_SOURCE", "yahoo")
RVOL_PERIOD = 20
RVOL_HIGH_THRESHOLD = 2.0
MOVE_THRESHOLDS = [3.0, 5.0]  # % absolute daily close-to-close return
FORWARD_HORIZONS = [1, 3, 5, 10, 20]
MIN_GROUP_SIZE = 20  # below this a permutation test is too noisy to trust


def get_db():
    import psycopg2
    return psycopg2.connect(os.environ["DATABASE_URL"])


def get_universe_symbols(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM universe WHERE scannable=TRUE ORDER BY symbol")
        return [r[0] for r in cur.fetchall()]


def load_series(conn, symbol, source):
    """dates/closes/volumes for one symbol, restricted to a single
    `source` value -- never pools yahoo and alpaca_iex volume in the same
    run (see module docstring)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DATE(ts), close, volume FROM price_history
            WHERE symbol=%s AND source=%s ORDER BY ts ASC
        """, (symbol, source))
        rows = cur.fetchall()
    dates = [r[0] for r in rows]
    closes = [float(r[1]) for r in rows]
    volumes = [float(r[2]) for r in rows]
    return dates, closes, volumes


def daily_return_pct(closes, i):
    if i == 0 or closes[i - 1] == 0:
        return None
    return (closes[i] - closes[i - 1]) / closes[i - 1] * 100


def forward_return_pct(closes, i, horizon):
    if i + horizon >= len(closes) or closes[i] == 0:
        return None
    return (closes[i + horizon] - closes[i]) / closes[i] * 100


def precompute_observations(dates, closes, volumes):
    """One row per eligible day i (enough trailing history for rvol, enough
    leading history for the largest forward horizon): the triggering day's
    return/rvol plus every horizon's forward return, computed once so every
    threshold/group split below is a cheap filter over the same cache."""
    n = len(closes)
    max_horizon = max(FORWARD_HORIZONS)
    obs = []
    for i in range(RVOL_PERIOD, n - max_horizon):
        move = daily_return_pct(closes, i)
        if move is None:
            continue
        rvol = relative_volume(volumes[: i + 1], RVOL_PERIOD)
        if rvol is None:
            continue
        fwd = {h: forward_return_pct(closes, i, h) for h in FORWARD_HORIZONS}
        if any(v is None for v in fwd.values()):
            continue
        obs.append({"date": dates[i], "move": move, "rvol": rvol, "fwd": fwd})
    return obs


def cohens_d(a, b):
    if len(a) < 2 or len(b) < 2:
        return None
    pooled_std = statistics.pstdev(a + b)
    if pooled_std == 0:
        return None
    return round((statistics.mean(a) - statistics.mean(b)) / pooled_std, 4)


def bootstrap_ci(values, rng, n_resamples=N_BOOTSTRAP):
    if len(values) < 2:
        return None
    means = []
    n = len(values)
    for _ in range(n_resamples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(0.025 * n_resamples)]
    hi = means[int(0.975 * n_resamples) - 1]
    return {"lo": round(lo, 4), "hi": round(hi, 4)}


def continuation_probability(rows, horizon):
    """Fraction of rows whose forward return at `horizon` shares the sign
    of the triggering day's own move (0-day return excluded, undefined
    sign)."""
    signed = [(r["move"], r["fwd"][horizon]) for r in rows if r["move"] != 0]
    if not signed:
        return None
    hits = sum(1 for move, fwd in signed if (move > 0) == (fwd > 0))
    return round(100 * hits / len(signed), 2)


def group_stats(rows, horizon, rng):
    fwd = [r["fwd"][horizon] for r in rows]
    n = len(fwd)
    if n == 0:
        return {"n": 0}
    return {
        "n": n,
        "mean_fwd_return": round(statistics.mean(fwd), 4),
        "median_fwd_return": round(statistics.median(fwd), 4),
        "continuation_pct": continuation_probability(rows, horizon),
        "ci_95": bootstrap_ci(fwd, rng),
    }


def permutation_test(high, low, horizon, n_permutations, seed):
    """Label-shuffle test on the difference in mean forward return between
    the high-rvol and low-rvol groups, both drawn from the same
    matched-move population -- the epic's mandatory 'matched move without
    the volume condition' baseline, tested directly rather than merely
    reported alongside."""
    rng = random.Random(seed)
    high_fwd = [r["fwd"][horizon] for r in high]
    low_fwd = [r["fwd"][horizon] for r in low]
    if len(high_fwd) < MIN_GROUP_SIZE or len(low_fwd) < MIN_GROUP_SIZE:
        return None
    observed = statistics.mean(high_fwd) - statistics.mean(low_fwd)
    pooled = high_fwd + low_fwd
    n_high = len(high_fwd)
    null_diffs = []
    for _ in range(n_permutations):
        rng.shuffle(pooled)
        null_diffs.append(statistics.mean(pooled[:n_high]) - statistics.mean(pooled[n_high:]))
    count_ge = sum(1 for d in null_diffs if abs(d) >= abs(observed))
    p_value = round((1 + count_ge) / (1 + len(null_diffs)), 5)
    return {
        "observed_diff": round(observed, 4),
        "null_mean": round(statistics.mean(null_diffs), 4),
        "null_std": round(statistics.pstdev(null_diffs), 4),
        "p_value_two_sided": p_value,
        "n_permutations": len(null_diffs),
    }


def run_for_threshold(all_obs, move_threshold, rng):
    matched = [o for o in all_obs if abs(o["move"]) >= move_threshold]
    high = [o for o in matched if o["rvol"] >= RVOL_HIGH_THRESHOLD]
    low = [o for o in matched if o["rvol"] < RVOL_HIGH_THRESHOLD]
    result = {
        "move_threshold_pct": move_threshold,
        "n_matched_move_days": len(matched),
        "n_high_rvol": len(high),
        "n_low_rvol": len(low),
        "by_horizon": {},
    }
    for h in FORWARD_HORIZONS:
        result["by_horizon"][h] = {
            "high_rvol": group_stats(high, h, rng),
            "low_rvol": group_stats(low, h, rng),
            "cohens_d": cohens_d([o["fwd"][h] for o in high], [o["fwd"][h] for o in low]),
            "permutation_test": permutation_test(high, low, h, N_PERMUTATIONS, RANDOM_SEED + int(move_threshold * 100) + h),
        }
    return result


def main():
    conn = get_db()
    symbols = get_universe_symbols(conn)
    log.info(f"Volume confirmation: {len(symbols)} universe symbols, source={SOURCE}, "
             f"move_thresholds={MOVE_THRESHOLDS}, rvol_high_threshold={RVOL_HIGH_THRESHOLD}, "
             f"horizons={FORWARD_HORIZONS}")

    all_obs = []
    per_symbol_counts = {}
    for idx, sym in enumerate(symbols):
        dates, closes, volumes = load_series(conn, sym, SOURCE)
        if len(closes) < RVOL_PERIOD + max(FORWARD_HORIZONS) + 1:
            continue
        obs = precompute_observations(dates, closes, volumes)
        per_symbol_counts[sym] = len(obs)
        all_obs.extend(obs)
        if (idx + 1) % 100 == 0:
            log.info(f"...{idx + 1}/{len(symbols)} symbols prepared")
    conn.close()
    log.info(f"Prepared {len(all_obs)} eligible day-observations across {len(per_symbol_counts)} symbols "
             f"(source={SOURCE})")

    unconditional_fwd = {h: [o["fwd"][h] for o in all_obs] for h in FORWARD_HORIZONS}
    unconditional_baseline = {
        h: {
            "n": len(unconditional_fwd[h]),
            "mean_fwd_return": round(statistics.mean(unconditional_fwd[h]), 4) if unconditional_fwd[h] else None,
        }
        for h in FORWARD_HORIZONS
    }

    rng = random.Random(RANDOM_SEED)
    report = {
        "experiment_id": EXPERIMENT_ID,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": GIT_COMMIT,
        "reproducibility": {
            "source": SOURCE,
            "session_definition": "daily_close_to_close",
            "rvol_period": RVOL_PERIOD,
            "rvol_high_threshold": RVOL_HIGH_THRESHOLD,
            "move_thresholds_pct": MOVE_THRESHOLDS,
            "forward_horizons": FORWARD_HORIZONS,
            "n_permutations": N_PERMUTATIONS,
            "n_bootstrap": N_BOOTSTRAP,
            "random_seed": RANDOM_SEED,
            "universe_size": len(symbols),
        },
        "unconditional_baseline_by_horizon": unconditional_baseline,
        "by_move_threshold": {},
    }

    for move_threshold in MOVE_THRESHOLDS:
        result = run_for_threshold(all_obs, move_threshold, rng)
        report["by_move_threshold"][move_threshold] = result
        log.info(f"move>={move_threshold}%: matched={result['n_matched_move_days']} "
                 f"high_rvol={result['n_high_rvol']} low_rvol={result['n_low_rvol']}")

    with open("/tmp/backtest_results_012.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info("Full results written to /tmp/backtest_results_012.json")

    summary_parts = []
    for move_threshold, result in report["by_move_threshold"].items():
        h20 = result["by_horizon"].get(20, {})
        perm = h20.get("permutation_test")
        p = perm["p_value_two_sided"] if perm else None
        summary_parts.append(f"move>={move_threshold}%: n={result['n_matched_move_days']} p(20d)={p}")
    save_backtest_result(EXPERIMENT_ID, GIT_COMMIT, report, summary=" | ".join(summary_parts))
    log.info("Results also saved to backtest_results table")

    print(f"\n=== Experiment {EXPERIMENT_ID} (commit {GIT_COMMIT[:8]}, source={SOURCE}) ===")
    for move_threshold, result in report["by_move_threshold"].items():
        print(f"\nmove >= {move_threshold}%  (matched={result['n_matched_move_days']}, "
              f"high_rvol={result['n_high_rvol']}, low_rvol={result['n_low_rvol']})")
        print(f"{'horizon':>8}  {'high_mean':>10}  {'low_mean':>10}  {'cohens_d':>9}  {'p_value':>8}")
        for h in FORWARD_HORIZONS:
            hh = result["by_horizon"][h]
            perm = hh["permutation_test"]
            p = perm["p_value_two_sided"] if perm else "n/a"
            hi_mean = hh["high_rvol"].get("mean_fwd_return", "n/a")
            lo_mean = hh["low_rvol"].get("mean_fwd_return", "n/a")
            print(f"{h:>7}d  {hi_mean!s:>10}  {lo_mean!s:>10}  {hh['cohens_d']!s:>9}  {p!s:>8}")

    print("\nInterpretation: p_value is a two-sided label-permutation test on the difference in mean forward")
    print("return between high-rvol and low-rvol days, both drawn from the same matched-move population (same")
    print("|move| threshold). p < 0.05 means the volume split's difference is unlikely to be due to chance.")
    print("This does NOT establish causality, profitability after costs, or robustness out-of-sample.")


if __name__ == "__main__":
    main()

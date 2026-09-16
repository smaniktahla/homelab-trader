#!/usr/bin/env python3
"""Experiment 014: effort-vs-result ("potential absorption") hypothesis
significance test.

Volume & Volume Profile epic, PR E, third and final raw-volume research
family (after Experiment 012's confirmation and Experiment 013's
divergence). Research artifact, not production logic.

Question: on days with unusually high relative volume but unusually small
price displacement -- high "effort," low "result" -- does the market
behave differently afterward (bigger subsequent moves, i.e. a "coiled
spring") than on days with the same high volume but normal/large
displacement? The epic spec is explicit that this must NOT be encoded as
"absorption" being an established fact from OHLCV alone -- there is no
buy/sell-side attribution possible from bars, only a volume/range
co-occurrence -- so this script tests "high effort/low result" strictly
as a statistical label, never asserts a mechanism, and makes no directional
claim (an effort/result day has no inherent up/down direction, unlike
confirmation's move or divergence's extreme) -- the outcome measured is
forward move MAGNITUDE (|forward return|), not sign.

Definitions (parameterized, not tuned to one number and declared a win):
  "High effort" = volume_zscore(volumes, RVOL_PERIOD) > Z_THRESHOLD
    (shared/volume_metrics.py -- same z-score every other volume
    experiment in this epic uses).
  "Low result" = true_range[i] / atr[i] < TR_RATIO_THRESHOLD, i.e. today's
    own range is small relative to this symbol's own recent typical range
    (regime-relative, not a fixed cross-symbol % -- avoids biasing toward
    low-priced/low-volatility names the way an absolute threshold would).

Two groups, both drawn from the "high effort" (high-volume) population --
holding the volume condition fixed and varying only displacement, the
natural matched-baseline the epic's "baselines mandatory" requirement
calls for:
  effort_result_candidates: high effort AND low result.
  high_volume_normal_range: high effort, NOT low result.

Reuses Experiment 012's cohens_d/bootstrap_ci/permutation_test (pure,
sign-agnostic helpers -- they only ever compute means/differences over
whatever `fwd` values a row carries) applied to |forward return| instead
of signed forward return, since there is no direction to test here.

ATR is computed via shared/supertrend_strategy.py's own private
_wilder_atr_series() helper (Wilder's ATR, full series in one O(n) pass)
rather than re-deriving Wilder smoothing a third time in this repo --
already reused cross-module the same way (shared/strategy_registry.py
imports the sibling _supertrend_bands() for its chart overlay).

Feed/source discipline, lookahead safety (rvol/zscore/ATR all use
self-inclusive as-of-bar-t windows), and the mandatory-baselines
convention are identical to Experiments 012/013 -- see backtest_
volume_confirmation.py's docstring for the full rationale.

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 research/backtests/backtest_volume_effort_result.py
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

from volume_metrics import volume_zscore
from supertrend_strategy import _wilder_atr_series
from backtest_volume_confirmation import (
    get_db, get_universe_symbols, forward_return_pct,
    cohens_d, bootstrap_ci, permutation_test,
    SOURCE, RVOL_PERIOD, FORWARD_HORIZONS,
)
from db_utils import save_backtest_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EXPERIMENT_ID = "014_volume_effort_result"
GIT_COMMIT = os.environ.get("BACKTEST_GIT_COMMIT", "unknown")
N_PERMUTATIONS = int(os.environ.get("N_PERMUTATIONS", "2000"))
RANDOM_SEED = 44

ATR_PERIOD = 14
Z_THRESHOLD = 1.5
TR_RATIO_THRESHOLD = 0.6  # today's true range < 60% of its own recent ATR


def load_series_ohlcv(conn, symbol, source):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DATE(ts), close, high, low, volume FROM price_history
            WHERE symbol=%s AND source=%s ORDER BY ts ASC
        """, (symbol, source))
        rows = cur.fetchall()
    dates = [r[0] for r in rows]
    closes = [float(r[1]) for r in rows]
    highs = [float(r[2]) for r in rows]
    lows = [float(r[3]) for r in rows]
    volumes = [float(r[4]) for r in rows]
    return dates, closes, highs, lows, volumes


def true_range(highs, lows, closes, i):
    if i == 0:
        return highs[i] - lows[i]
    return max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))


def precompute_effort_result(dates, closes, highs, lows, volumes):
    n = len(closes)
    max_horizon = max(FORWARD_HORIZONS)
    atr = _wilder_atr_series(highs, lows, closes, ATR_PERIOD)
    start = max(RVOL_PERIOD, ATR_PERIOD + 1)
    rows = []
    for i in range(start, n - max_horizon):
        if atr[i] is None or atr[i] == 0:
            continue
        zscore = volume_zscore(volumes[: i + 1], RVOL_PERIOD)
        if zscore is None or zscore <= Z_THRESHOLD:
            continue  # not a "high effort" day at all -- excluded from both groups
        tr_ratio = true_range(highs, lows, closes, i) / atr[i]
        fwd_abs = {h: abs(forward_return_pct(closes, i, h)) if forward_return_pct(closes, i, h) is not None else None
                   for h in FORWARD_HORIZONS}
        if any(v is None for v in fwd_abs.values()):
            continue
        rows.append({
            "date": dates[i],
            "low_result": tr_ratio < TR_RATIO_THRESHOLD,
            "fwd": fwd_abs,
        })
    return rows


def magnitude_stats(rows, horizon, rng):
    values = [r["fwd"][horizon] for r in rows]
    n = len(values)
    if n == 0:
        return {"n": 0}
    return {
        "n": n,
        "mean_abs_fwd_return": round(statistics.mean(values), 4),
        "median_abs_fwd_return": round(statistics.median(values), 4),
        "ci_95": bootstrap_ci(values, rng),
    }


def main():
    conn = get_db()
    symbols = get_universe_symbols(conn)
    log.info(f"Volume effort/result: {len(symbols)} universe symbols, source={SOURCE}, "
             f"z_threshold={Z_THRESHOLD}, tr_ratio_threshold={TR_RATIO_THRESHOLD}, horizons={FORWARD_HORIZONS}")

    all_rows = []
    per_symbol_counts = {}
    for idx, sym in enumerate(symbols):
        dates, closes, highs, lows, volumes = load_series_ohlcv(conn, sym, SOURCE)
        if len(closes) < max(RVOL_PERIOD, ATR_PERIOD + 1) + max(FORWARD_HORIZONS) + 1:
            continue
        rows = precompute_effort_result(dates, closes, highs, lows, volumes)
        per_symbol_counts[sym] = len(rows)
        all_rows.extend(rows)
        if (idx + 1) % 100 == 0:
            log.info(f"...{idx + 1}/{len(symbols)} symbols prepared")
    conn.close()
    log.info(f"Prepared {len(all_rows)} high-effort day-observations across {len(per_symbol_counts)} symbols "
             f"(source={SOURCE})")

    candidates = [r for r in all_rows if r["low_result"]]
    baseline = [r for r in all_rows if not r["low_result"]]

    rng = random.Random(RANDOM_SEED)
    report = {
        "experiment_id": EXPERIMENT_ID,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": GIT_COMMIT,
        "reproducibility": {
            "source": SOURCE,
            "session_definition": "daily_close_to_close",
            "rvol_period": RVOL_PERIOD,
            "atr_period": ATR_PERIOD,
            "z_threshold": Z_THRESHOLD,
            "tr_ratio_threshold": TR_RATIO_THRESHOLD,
            "forward_horizons": FORWARD_HORIZONS,
            "n_permutations": N_PERMUTATIONS,
            "random_seed": RANDOM_SEED,
            "universe_size": len(symbols),
        },
        "n_effort_result_candidates": len(candidates),
        "n_high_volume_normal_range_baseline": len(baseline),
        "by_horizon": {},
    }

    for h in FORWARD_HORIZONS:
        report["by_horizon"][h] = {
            "effort_result_candidates": magnitude_stats(candidates, h, rng),
            "high_volume_normal_range_baseline": magnitude_stats(baseline, h, rng),
            "cohens_d": cohens_d([r["fwd"][h] for r in candidates], [r["fwd"][h] for r in baseline]),
            "permutation_test": permutation_test(candidates, baseline, h, N_PERMUTATIONS, RANDOM_SEED + h),
        }

    with open("/tmp/backtest_results_014.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info("Full results written to /tmp/backtest_results_014.json")

    h20 = report["by_horizon"].get(20, {})
    perm = h20.get("permutation_test")
    p20 = perm["p_value_two_sided"] if perm else None
    save_backtest_result(
        EXPERIMENT_ID, GIT_COMMIT, report,
        summary=f"candidates={len(candidates)} baseline={len(baseline)} p(20d)={p20}",
    )
    log.info("Results also saved to backtest_results table")

    print(f"\n=== Experiment {EXPERIMENT_ID} (commit {GIT_COMMIT[:8]}, source={SOURCE}) ===")
    print(f"effort_result_candidates={len(candidates)}  high_volume_normal_range_baseline={len(baseline)}\n")
    print(f"{'horizon':>8}  {'candidate_mean':>14}  {'baseline_mean':>14}  {'cohens_d':>9}  {'p_value':>8}")
    for h in FORWARD_HORIZONS:
        hh = report["by_horizon"][h]
        perm = hh["permutation_test"]
        p = perm["p_value_two_sided"] if perm else "n/a"
        cand_mean = hh["effort_result_candidates"].get("mean_abs_fwd_return", "n/a")
        base_mean = hh["high_volume_normal_range_baseline"].get("mean_abs_fwd_return", "n/a")
        print(f"{h:>7}d  {cand_mean!s:>14}  {base_mean!s:>14}  {hh['cohens_d']!s:>9}  {p!s:>8}")

    print("\nInterpretation: means are of |forward return| (magnitude, not direction -- effort/result candidates")
    print("have no inherent up/down bias). p_value is a two-sided label-permutation test on the difference in")
    print("mean |forward return| between the two groups, both already conditioned on high volume. This is a")
    print("statistical co-occurrence test only -- it does NOT establish 'absorption', a mechanism, causality,")
    print("profitability after costs, or out-of-sample robustness.")


if __name__ == "__main__":
    main()

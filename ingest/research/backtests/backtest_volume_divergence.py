#!/usr/bin/env python3
"""Experiment 013: volume-divergence hypothesis significance test.

Volume & Volume Profile epic, PR E (raw-volume hypothesis experiments,
Workstream B), second raw-volume family after Experiment 012 (volume
confirmation). Research artifact, not production logic.

Question: does a new N-day price extreme made on declining volume
participation reverse more often than an extreme made with stable/rising
participation? The epic spec is explicit that "declining volume" must be
"precisely and multiply defined, not selected post-hoc" -- so this script
tests it under two independent, pre-registered definitions rather than one
number tuned until it looks good:

  Definition A (rvol_trend): rvol at the extreme bar is lower than rvol was
  DECLINE_LOOKBACK bars earlier -- participation has been fading into the
  extreme, a relative/trend-based definition.

  Definition B (rvol_absolute): rvol at the extreme bar is below 1.0 (its
  own trailing average) -- an absolute/level-based definition, independent
  of what rvol was doing before.

An extreme qualifies as "declining volume" only if it satisfies a
definition on its own terms; the two are reported and permutation-tested
separately (not OR'd/AND'd together), so a result that only shows up under
one definition is visible as such rather than laundered into a single
combined finding.

Reuses the exact permutation/effect-size/CI machinery Experiment 012 (backtest_
volume_confirmation.py) already established -- group_stats/cohens_d/
bootstrap_ci/permutation_test are pure statistical helpers with no
confirmation-specific logic in them, so importing rather than duplicating
avoids a second implementation to keep in sync. "Reversal" here is encoded
via the same continuation_probability() helper: each extreme's `move` field
is set to its direction (+1 new high / -1 new low) rather than a %
price-move, so continuation_pct means "the extreme's direction persisted"
and 100 - continuation_pct is the reversal rate the hypothesis is actually
about.

Feed/source discipline, lookahead safety, and the mandatory-baselines
convention are identical to Experiment 012 -- see that module's docstring
for the full rationale; not repeated here.

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 research/backtests/backtest_volume_divergence.py
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
from backtest_volume_confirmation import (
    get_db, get_universe_symbols, load_series, forward_return_pct,
    group_stats, cohens_d, permutation_test,
    SOURCE, RVOL_PERIOD, FORWARD_HORIZONS, MIN_GROUP_SIZE,
)
from db_utils import save_backtest_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EXPERIMENT_ID = "013_volume_divergence"
GIT_COMMIT = os.environ.get("BACKTEST_GIT_COMMIT", "unknown")
N_PERMUTATIONS = int(os.environ.get("N_PERMUTATIONS", "2000"))
RANDOM_SEED = 43

EXTREME_LOOKBACK = 20  # "new N-day high/low" window, self-inclusive of the extreme bar itself
DECLINE_LOOKBACK = 5   # Definition A: rvol vs rvol this many bars earlier


def is_new_high(closes, i, lookback):
    lo = max(0, i - lookback + 1)
    return closes[i] == max(closes[lo: i + 1])


def is_new_low(closes, i, lookback):
    lo = max(0, i - lookback + 1)
    return closes[i] == min(closes[lo: i + 1])


def precompute_extremes(dates, closes, volumes):
    """One row per day i that is a new EXTREME_LOOKBACK-day high or low,
    with enough trailing history for both rvol definitions and enough
    leading history for the largest forward horizon. `move` is the
    extreme's direction (+1/-1), not a % price move -- see module
    docstring for why continuation_probability() can be reused directly."""
    n = len(closes)
    max_horizon = max(FORWARD_HORIZONS)
    start = max(RVOL_PERIOD, EXTREME_LOOKBACK, DECLINE_LOOKBACK + RVOL_PERIOD)
    rows = []
    for i in range(start, n - max_horizon):
        direction = 1 if is_new_high(closes, i, EXTREME_LOOKBACK) else (
            -1 if is_new_low(closes, i, EXTREME_LOOKBACK) else 0)
        if direction == 0:
            continue
        rvol_now = relative_volume(volumes[: i + 1], RVOL_PERIOD)
        rvol_prior = relative_volume(volumes[: i - DECLINE_LOOKBACK + 1], RVOL_PERIOD)
        if rvol_now is None or rvol_prior is None:
            continue
        fwd = {h: forward_return_pct(closes, i, h) for h in FORWARD_HORIZONS}
        if any(v is None for v in fwd.values()):
            continue
        rows.append({
            "date": dates[i], "move": direction,
            "declining_trend": rvol_now < rvol_prior,   # Definition A
            "declining_absolute": rvol_now < 1.0,       # Definition B
            "fwd": fwd,
        })
    return rows


def run_for_definition(all_rows, definition_key, rng):
    declining = [r for r in all_rows if r[definition_key]]
    stable_or_rising = [r for r in all_rows if not r[definition_key]]
    result = {
        "definition": definition_key,
        "n_declining": len(declining),
        "n_stable_or_rising": len(stable_or_rising),
        "by_horizon": {},
    }
    for h in FORWARD_HORIZONS:
        result["by_horizon"][h] = {
            "declining": group_stats(declining, h, rng),
            "stable_or_rising": group_stats(stable_or_rising, h, rng),
            "cohens_d": cohens_d([r["fwd"][h] for r in declining], [r["fwd"][h] for r in stable_or_rising]),
            "permutation_test": permutation_test(
                declining, stable_or_rising, h, N_PERMUTATIONS,
                RANDOM_SEED + hash(definition_key) % 1000 + h,
            ),
        }
    return result


def main():
    conn = get_db()
    symbols = get_universe_symbols(conn)
    log.info(f"Volume divergence: {len(symbols)} universe symbols, source={SOURCE}, "
             f"extreme_lookback={EXTREME_LOOKBACK}, decline_lookback={DECLINE_LOOKBACK}, "
             f"horizons={FORWARD_HORIZONS}")

    all_rows = []
    per_symbol_counts = {}
    for idx, sym in enumerate(symbols):
        dates, closes, volumes = load_series(conn, sym, SOURCE)
        if len(closes) < max(RVOL_PERIOD, EXTREME_LOOKBACK) + max(FORWARD_HORIZONS) + DECLINE_LOOKBACK + 1:
            continue
        rows = precompute_extremes(dates, closes, volumes)
        per_symbol_counts[sym] = len(rows)
        all_rows.extend(rows)
        if (idx + 1) % 100 == 0:
            log.info(f"...{idx + 1}/{len(symbols)} symbols prepared")
    conn.close()
    log.info(f"Prepared {len(all_rows)} new-extreme observations across {len(per_symbol_counts)} symbols "
             f"(source={SOURCE})")

    rng = random.Random(RANDOM_SEED)
    report = {
        "experiment_id": EXPERIMENT_ID,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": GIT_COMMIT,
        "reproducibility": {
            "source": SOURCE,
            "session_definition": "daily_close_to_close",
            "rvol_period": RVOL_PERIOD,
            "extreme_lookback": EXTREME_LOOKBACK,
            "decline_lookback": DECLINE_LOOKBACK,
            "forward_horizons": FORWARD_HORIZONS,
            "n_permutations": N_PERMUTATIONS,
            "random_seed": RANDOM_SEED,
            "universe_size": len(symbols),
        },
        "by_definition": {},
    }

    for definition_key in ("declining_trend", "declining_absolute"):
        result = run_for_definition(all_rows, definition_key, rng)
        report["by_definition"][definition_key] = result
        log.info(f"{definition_key}: declining={result['n_declining']} stable_or_rising={result['n_stable_or_rising']}")

    with open("/tmp/backtest_results_013.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info("Full results written to /tmp/backtest_results_013.json")

    summary_parts = []
    for definition_key, result in report["by_definition"].items():
        h20 = result["by_horizon"].get(20, {})
        perm = h20.get("permutation_test")
        p = perm["p_value_two_sided"] if perm else None
        summary_parts.append(f"{definition_key}: n_declining={result['n_declining']} p(20d)={p}")
    save_backtest_result(EXPERIMENT_ID, GIT_COMMIT, report, summary=" | ".join(summary_parts))
    log.info("Results also saved to backtest_results table")

    print(f"\n=== Experiment {EXPERIMENT_ID} (commit {GIT_COMMIT[:8]}, source={SOURCE}) ===")
    for definition_key, result in report["by_definition"].items():
        print(f"\n{definition_key}  (declining={result['n_declining']}, "
              f"stable_or_rising={result['n_stable_or_rising']})")
        print(f"{'horizon':>8}  {'decl_reversal%':>14}  {'stable_reversal%':>16}  {'cohens_d':>9}  {'p_value':>8}")
        for h in FORWARD_HORIZONS:
            hh = result["by_horizon"][h]
            perm = hh["permutation_test"]
            p = perm["p_value_two_sided"] if perm else "n/a"
            decl_cont = hh["declining"].get("continuation_pct")
            stable_cont = hh["stable_or_rising"].get("continuation_pct")
            decl_rev = round(100 - decl_cont, 2) if decl_cont is not None else "n/a"
            stable_rev = round(100 - stable_cont, 2) if stable_cont is not None else "n/a"
            print(f"{h:>7}d  {decl_rev!s:>14}  {stable_rev!s:>16}  {hh['cohens_d']!s:>9}  {p!s:>8}")

    print("\nInterpretation: *_reversal% is 100 - continuation_pct (the fraction of extremes whose forward return")
    print("at that horizon shares the extreme's own direction). p_value is a two-sided label-permutation test on")
    print("the difference in mean forward return between the declining-volume group and everything else, per")
    print("definition. This does NOT establish causality, profitability after costs, or out-of-sample robustness.")


if __name__ == "__main__":
    main()

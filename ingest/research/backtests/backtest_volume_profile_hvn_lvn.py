#!/usr/bin/env python3
"""Experiment 017: HVN/LVN dwell-time hypothesis significance test.

Volume & Volume Profile epic, PR F, third and final Volume-Profile-
specific hypothesis family (after Experiment 015's value-area re-entry
and Experiment 016's POC retest) -- completes the epic's own suggested
implementation sequence. Research artifact, not production logic.

Question: does price entering a high-volume node (HVN) of the previous
session's profile dwell there longer than in a matched/typical-volume
region, and does it traverse a low-volume node (LVN) faster than that
same matched region? The epic spec is explicit this needs a quantitative
traversal/dwell-time definition, not "price rips through low volume" as
an unquantified impression -- so this script defines dwell length as a
strictly countable quantity (consecutive hourly bars whose close falls in
the same profile bucket) and classifies bucket type by that bucket's own
volume percentile within the previous session's profile, never by
inspecting today's outcome.

Definitions:
  Node classification: among a session's non-empty profile buckets (PR C's
  shared/volume_profile.py buckets), the top HVN_PCTL fraction by volume
  are HVN, the bottom LVN_PCTL fraction are LVN, everything else is the
  matched baseline ("typical volume") region -- the epic's own "matched
  regions" comparison, not an unconditional population.
  Dwell segment: a maximal run of consecutive today-session hourly bars
  whose close lands in the same previous-session bucket. Its length (bar
  count) is the traversal/dwell-time observation; its node type is
  whichever class (HVN/LVN/baseline) that bucket belongs to.

Two independent comparisons, both against the same baseline population:
  (A) HVN dwell length vs. baseline dwell length (hypothesis: HVN >
      baseline).
  (B) LVN dwell length vs. baseline dwell length (hypothesis: LVN <
      baseline, i.e. faster traversal).

Session/profile discipline identical to Experiments 015/016: previous-
session profile fully known before today's session opens (no lookahead),
reuses Experiment 015's load_hourly_sessions() and Experiment 012's
cohens_d/bootstrap_ci/permutation_test (generic over any r["fwd"][key]
value -- "dwell" here is a segment's bar count, same reuse pattern
Experiments 014/016 already applied to other non-return metrics).

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 research/backtests/backtest_volume_profile_hvn_lvn.py
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from volume_profile import compute_volume_profile
from backtest_volume_confirmation import cohens_d, bootstrap_ci, permutation_test
from backtest_volume_profile_value_area_reentry import get_db, get_universe_symbols, load_hourly_sessions
from db_utils import save_backtest_result
import random

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EXPERIMENT_ID = "017_volume_profile_hvn_lvn"
GIT_COMMIT = os.environ.get("BACKTEST_GIT_COMMIT", "unknown")
N_PERMUTATIONS = int(os.environ.get("N_PERMUTATIONS", "2000"))
N_BOOTSTRAP = 1000
RANDOM_SEED = 47

HVN_PCTL = 0.75  # top 25% of non-empty buckets by volume
LVN_PCTL = 0.25  # bottom 25% of non-empty buckets by volume
MIN_GROUP_SIZE = 20


def classify_buckets(buckets):
    """Returns {bucket_index: 'hvn'|'lvn'|'baseline'} for non-empty
    buckets only -- empty (zero-volume) buckets are excluded from
    classification entirely, since they likely reflect a price gap never
    traded rather than a meaningful low-participation zone."""
    nonzero = [(i, b.volume) for i, b in enumerate(buckets) if b.volume > 0]
    if len(nonzero) < 4:
        return {}
    volumes_sorted = sorted(v for _, v in nonzero)
    n = len(volumes_sorted)
    hvn_cutoff = volumes_sorted[int(HVN_PCTL * n)] if int(HVN_PCTL * n) < n else volumes_sorted[-1]
    lvn_cutoff = volumes_sorted[int(LVN_PCTL * n)]
    classes = {}
    for i, v in nonzero:
        if v >= hvn_cutoff:
            classes[i] = "hvn"
        elif v <= lvn_cutoff:
            classes[i] = "lvn"
        else:
            classes[i] = "baseline"
    return classes


def bucket_index_for_price(profile, price):
    if not profile.buckets or profile.bucket_count == 0:
        return None
    lo = profile.buckets[0].price_low
    hi = profile.buckets[-1].price_high
    if hi <= lo:
        return None
    width = (hi - lo) / profile.bucket_count
    idx = int((price - lo) / width)
    return max(0, min(idx, profile.bucket_count - 1))


def dwell_segments(today_bars, profile, bucket_classes):
    """Walks today's closes, groups consecutive bars landing in the same
    previous-session bucket into segments, and returns one row per
    segment whose bucket was classified (unclassified/empty buckets are
    skipped -- they carry no HVN/LVN/baseline label to compare)."""
    segments = []
    current_idx, current_len = None, 0
    for bar in today_bars:
        idx = bucket_index_for_price(profile, bar.close)
        if idx == current_idx:
            current_len += 1
        else:
            if current_idx is not None and current_idx in bucket_classes:
                segments.append({"node_type": bucket_classes[current_idx], "dwell": float(current_len)})
            current_idx, current_len = idx, 1
    if current_idx is not None and current_idx in bucket_classes:
        segments.append({"node_type": bucket_classes[current_idx], "dwell": float(current_len)})
    return segments


def precompute_events(sessions):
    all_segments = []
    for idx in range(1, len(sessions)):
        prev_day, prev_bars, prev_feeds = sessions[idx - 1]
        _, today_bars, _ = sessions[idx]
        if not prev_bars or not today_bars:
            continue
        profile = compute_volume_profile(prev_bars, prev_feeds)
        if profile.poc is None:
            continue
        bucket_classes = classify_buckets(profile.buckets)
        if not bucket_classes:
            continue
        all_segments.extend(dwell_segments(today_bars, profile, bucket_classes))
    return all_segments


def group_stats(rows, rng):
    values = [r["dwell"] for r in rows]
    n = len(values)
    if n == 0:
        return {"n": 0}
    return {
        "n": n,
        "mean_dwell_bars": round(sum(values) / n, 4),
        "median_dwell_bars": sorted(values)[n // 2],
        "ci_95": bootstrap_ci(values, rng),
    }


def compare(group_a, group_b, rng, seed):
    return {
        "n_a": len(group_a), "n_b": len(group_b),
        "stats_a": group_stats(group_a, rng),
        "stats_b": group_stats(group_b, rng),
        "cohens_d": cohens_d([r["dwell"] for r in group_a], [r["dwell"] for r in group_b]),
        "permutation_test": (
            permutation_test(
                [{"fwd": {"dwell": r["dwell"]}} for r in group_a],
                [{"fwd": {"dwell": r["dwell"]}} for r in group_b],
                "dwell", N_PERMUTATIONS, seed,
            ) if len(group_a) >= MIN_GROUP_SIZE and len(group_b) >= MIN_GROUP_SIZE else None
        ),
    }


def main():
    conn = get_db()
    symbols = get_universe_symbols(conn)
    log.info(f"HVN/LVN dwell time: {len(symbols)} universe symbols, hvn_pctl={HVN_PCTL}, lvn_pctl={LVN_PCTL}")

    all_segments = []
    per_symbol_counts = {}
    for idx, sym in enumerate(symbols):
        sessions = load_hourly_sessions(conn, sym)
        if len(sessions) < 2:
            continue
        segments = precompute_events(sessions)
        per_symbol_counts[sym] = len(segments)
        all_segments.extend(segments)
        if (idx + 1) % 100 == 0:
            log.info(f"...{idx + 1}/{len(symbols)} symbols prepared")
    conn.close()
    log.info(f"Prepared {len(all_segments)} classified dwell segments across "
             f"{sum(1 for v in per_symbol_counts.values() if v > 0)} symbols "
             f"(small-sample caveat: price_history_hourly has only a few weeks of real depth)")

    hvn = [s for s in all_segments if s["node_type"] == "hvn"]
    lvn = [s for s in all_segments if s["node_type"] == "lvn"]
    baseline = [s for s in all_segments if s["node_type"] == "baseline"]

    rng = random.Random(RANDOM_SEED)
    report = {
        "experiment_id": EXPERIMENT_ID,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": GIT_COMMIT,
        "reproducibility": {
            "source_table": "price_history_hourly",
            "feed": "alpaca_iex",
            "session_definition": "calendar_day_utc_of_hourly_bars",
            "hvn_pctl": HVN_PCTL,
            "lvn_pctl": LVN_PCTL,
            "n_permutations": N_PERMUTATIONS,
            "n_bootstrap": N_BOOTSTRAP,
            "random_seed": RANDOM_SEED,
            "universe_size": len(symbols),
        },
        "n_hvn_segments": len(hvn), "n_lvn_segments": len(lvn), "n_baseline_segments": len(baseline),
        "comparison_A_hvn_vs_baseline": compare(hvn, baseline, rng, RANDOM_SEED + 1),
        "comparison_B_lvn_vs_baseline": compare(lvn, baseline, rng, RANDOM_SEED + 2),
    }

    with open("/tmp/backtest_results_017.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info("Full results written to /tmp/backtest_results_017.json")

    perm_a = report["comparison_A_hvn_vs_baseline"]["permutation_test"]
    perm_b = report["comparison_B_lvn_vs_baseline"]["permutation_test"]
    summary = (
        f"A(HVN-vs-baseline) n_a={len(hvn)} n_b={len(baseline)} p={perm_a['p_value_two_sided'] if perm_a else None} | "
        f"B(LVN-vs-baseline) n_a={len(lvn)} n_b={len(baseline)} p={perm_b['p_value_two_sided'] if perm_b else None}"
    )
    save_backtest_result(EXPERIMENT_ID, GIT_COMMIT, report, summary=summary)
    log.info("Results also saved to backtest_results table")

    print(f"\n=== Experiment {EXPERIMENT_ID} (commit {GIT_COMMIT[:8]}) ===\n")
    for key, label in (
        ("comparison_A_hvn_vs_baseline", "A: HVN dwell vs baseline dwell (hypothesis: HVN > baseline)"),
        ("comparison_B_lvn_vs_baseline", "B: LVN dwell vs baseline dwell (hypothesis: LVN < baseline)"),
    ):
        c = report[key]
        perm = c["permutation_test"]
        p = perm["p_value_two_sided"] if perm else "n/a (< min group size)"
        print(f"{label}")
        print(f"  n_a={c['n_a']} mean_dwell_a={c['stats_a'].get('mean_dwell_bars')}  "
              f"n_b={c['n_b']} mean_dwell_b={c['stats_b'].get('mean_dwell_bars')}  "
              f"cohens_d={c['cohens_d']}  p_value={p}\n")

    print("Interpretation: dwell is the number of consecutive hourly bars price spent inside one previous-session")
    print("profile bucket before moving to another. p_value is a two-sided label-permutation test on the")
    print("difference in mean dwell length between the node-type group and the matched baseline (typical-volume")
    print("buckets). Small-sample caveat: price_history_hourly has only a few weeks of real depth per PR C's own")
    print("finding -- inspect n_a/n_b before trusting either p_value. Neither comparison establishes causality,")
    print("profitability after costs, or out-of-sample robustness.")


if __name__ == "__main__":
    main()

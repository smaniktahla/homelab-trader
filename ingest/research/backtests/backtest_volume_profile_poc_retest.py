#!/usr/bin/env python3
"""Experiment 016: POC-retest hypothesis significance test ("first touch
matters most").

Volume & Volume Profile epic, PR F, second Volume-Profile-specific
hypothesis family (after Experiment 015's value-area re-entry). Research
artifact, not production logic.

Question: does the first return to an established POC (after meaningful
separation) hold/bounce more often than subsequent retests of the same
POC, or than a retest of an arbitrary comparable level in the same
session's value area? Directly tests the "first touch matters most"
claim, via two independent comparisons rather than one:
  (A) first retest of POC vs. second+ retest of POC (same level, same
      session, ordinal position varies).
  (B) first retest of POC vs. first "retest" of a random comparable level
      inside the same session's value area (same ordinal position, level
      varies) -- the epic's mandatory "random matched levels" baseline.

Definitions, precisely and independently specified (not selected
post-hoc):
  Profile formation window: the previous session's own hourly bars,
  exactly as Experiment 015 uses it -- fully known before today's session
  opens, no lookahead.
  Meaningful separation: price must move at least SEPARATION_FRAC of the
  previous value area's half-width away from the level before a
  subsequent touch counts as a new (not the same) retest -- otherwise a
  single dwell period at the level would be miscounted as many retests.
  Retest tolerance: a close within TOLERANCE_FRAC of the half-width
  counts as "touching" the level.
  Reaction/failure ("holds" vs "fails"): HOLD_HORIZON bars after a
  retest, did price move back to the side it approached from (hold/
  bounce) or through to the other side (fail)? A binary per-event
  outcome, not a continuous return -- the claim being tested is about
  which side of the level price ends up on, not how far it moved.

All retest detection, separation state, and reaction measurement happen
strictly within the entry session (same session-boundary discipline as
Experiment 015) -- reuses that module's load_hourly_sessions() rather
than re-deriving the hourly/session-grouping logic a second time, and
Experiment 012's cohens_d/bootstrap_ci/permutation_test (generic,
sign-agnostic helpers that only ever read r["fwd"][key] -- "hold" is
just a 0.0/1.0 float under an arbitrary key here, same reuse pattern
Experiment 014 already applied to |forward return|).

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 research/backtests/backtest_volume_profile_poc_retest.py
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

from volume_profile import compute_volume_profile
from backtest_volume_confirmation import cohens_d, bootstrap_ci, permutation_test
from backtest_volume_profile_value_area_reentry import get_db, get_universe_symbols, load_hourly_sessions
from db_utils import save_backtest_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EXPERIMENT_ID = "016_volume_profile_poc_retest"
GIT_COMMIT = os.environ.get("BACKTEST_GIT_COMMIT", "unknown")
N_PERMUTATIONS = int(os.environ.get("N_PERMUTATIONS", "2000"))
RANDOM_SEED = 46

SEPARATION_FRAC = 0.5
TOLERANCE_FRAC = 0.1
HOLD_HORIZON = 3  # bars after a retest to check the hold/fail outcome
MIN_GROUP_SIZE = 20
RANDOM_LEVEL_MIN_FRAC = 0.15  # random comparable level drawn from this middle band of the
RANDOM_LEVEL_MAX_FRAC = 0.85  # value area, so it doesn't coincide with VAH/VAL edges or POC


def detect_retests(today_bars, level, half_width, rng_offset):
    """Walks today's closes and returns a list of {ordinal, approach_from,
    hold} events for `level`, honoring SEPARATION_FRAC/TOLERANCE_FRAC/
    HOLD_HORIZON exactly as the module docstring specifies."""
    if half_width <= 0:
        return []
    separation_threshold = SEPARATION_FRAC * half_width
    tolerance = TOLERANCE_FRAC * half_width
    closes = [b.close for b in today_bars]
    n = len(closes)

    events = []
    ordinal = 0
    armed = abs(closes[0] - level) > separation_threshold  # already separated at session open?
    for i in range(1, n):
        dist = closes[i] - level
        if armed and abs(dist) <= tolerance:
            if i + HOLD_HORIZON < n:
                approach_from = 1 if closes[i - 1] > level else -1
                final_close = closes[i + HOLD_HORIZON]
                held = (final_close > level) if approach_from == 1 else (final_close < level)
                ordinal += 1
                events.append({"ordinal": ordinal, "hold": 1.0 if held else 0.0})
            armed = False
        elif not armed and abs(dist) > separation_threshold:
            armed = True
    return events


def pick_random_level(val, vah, rng):
    lo = val + RANDOM_LEVEL_MIN_FRAC * (vah - val)
    hi = val + RANDOM_LEVEL_MAX_FRAC * (vah - val)
    return rng.uniform(lo, hi)


def precompute_events(sessions, rng):
    poc_events = []       # every retest-of-POC event, with its ordinal
    random_events = []    # every retest-of-random-level event, with its ordinal
    for idx in range(1, len(sessions)):
        prev_day, prev_bars, prev_feeds = sessions[idx - 1]
        today_day, today_bars, _ = sessions[idx]
        if not prev_bars or len(today_bars) < HOLD_HORIZON + 2:
            continue
        profile = compute_volume_profile(prev_bars, prev_feeds)
        if profile.poc is None or profile.vah is None or profile.val is None:
            continue
        half_width = (profile.vah - profile.val) / 2

        for e in detect_retests(today_bars, profile.poc, half_width, 0):
            e["date"] = str(today_day)
            poc_events.append(e)

        random_level = pick_random_level(profile.val, profile.vah, rng)
        for e in detect_retests(today_bars, random_level, half_width, 1):
            e["date"] = str(today_day)
            random_events.append(e)
    return poc_events, random_events


def group_stats(rows, rng):
    values = [r["hold"] for r in rows]
    n = len(values)
    if n == 0:
        return {"n": 0}
    return {
        "n": n,
        "hold_rate_pct": round(100 * sum(values) / n, 2),
        "ci_95": bootstrap_ci(values, rng),
    }


def compare(group_a, group_b, rng, seed):
    return {
        "n_a": len(group_a), "n_b": len(group_b),
        "stats_a": group_stats(group_a, rng),
        "stats_b": group_stats(group_b, rng),
        "cohens_d": cohens_d([r["hold"] for r in group_a], [r["hold"] for r in group_b]),
        "permutation_test": (
            permutation_test(
                [{"fwd": {"hold": r["hold"]}} for r in group_a],
                [{"fwd": {"hold": r["hold"]}} for r in group_b],
                "hold", N_PERMUTATIONS, seed,
            ) if len(group_a) >= MIN_GROUP_SIZE and len(group_b) >= MIN_GROUP_SIZE else None
        ),
    }


def main():
    conn = get_db()
    symbols = get_universe_symbols(conn)
    log.info(f"POC retest: {len(symbols)} universe symbols, separation_frac={SEPARATION_FRAC}, "
             f"tolerance_frac={TOLERANCE_FRAC}, hold_horizon={HOLD_HORIZON}")

    rng = random.Random(RANDOM_SEED)
    all_poc_events, all_random_events = [], []
    per_symbol_counts = {}
    for idx, sym in enumerate(symbols):
        sessions = load_hourly_sessions(conn, sym)
        if len(sessions) < 2:
            continue
        poc_events, random_events = precompute_events(sessions, rng)
        per_symbol_counts[sym] = len(poc_events)
        all_poc_events.extend(poc_events)
        all_random_events.extend(random_events)
        if (idx + 1) % 100 == 0:
            log.info(f"...{idx + 1}/{len(symbols)} symbols prepared")
    conn.close()
    log.info(f"Prepared {len(all_poc_events)} POC-retest events and {len(all_random_events)} "
             f"random-level-retest events across {sum(1 for v in per_symbol_counts.values() if v > 0)} symbols "
             f"(small-sample caveat: price_history_hourly has only a few weeks of real depth)")

    first_poc = [e for e in all_poc_events if e["ordinal"] == 1]
    subsequent_poc = [e for e in all_poc_events if e["ordinal"] >= 2]
    first_random = [e for e in all_random_events if e["ordinal"] == 1]

    report = {
        "experiment_id": EXPERIMENT_ID,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": GIT_COMMIT,
        "reproducibility": {
            "source_table": "price_history_hourly",
            "feed": "alpaca_iex",
            "session_definition": "calendar_day_utc_of_hourly_bars",
            "separation_frac": SEPARATION_FRAC,
            "tolerance_frac": TOLERANCE_FRAC,
            "hold_horizon_bars": HOLD_HORIZON,
            "random_level_band": [RANDOM_LEVEL_MIN_FRAC, RANDOM_LEVEL_MAX_FRAC],
            "n_permutations": N_PERMUTATIONS,
            "random_seed": RANDOM_SEED,
            "universe_size": len(symbols),
        },
        "comparison_A_first_vs_subsequent_poc_retest": compare(first_poc, subsequent_poc, rng, RANDOM_SEED + 1),
        "comparison_B_first_poc_vs_first_random_level": compare(first_poc, first_random, rng, RANDOM_SEED + 2),
    }

    with open("/tmp/backtest_results_016.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info("Full results written to /tmp/backtest_results_016.json")

    perm_a = report["comparison_A_first_vs_subsequent_poc_retest"]["permutation_test"]
    perm_b = report["comparison_B_first_poc_vs_first_random_level"]["permutation_test"]
    summary = (
        f"A(first-vs-subsequent-POC) n_a={len(first_poc)} n_b={len(subsequent_poc)} "
        f"p={perm_a['p_value_two_sided'] if perm_a else None} | "
        f"B(first-POC-vs-first-random) n_a={len(first_poc)} n_b={len(first_random)} "
        f"p={perm_b['p_value_two_sided'] if perm_b else None}"
    )
    save_backtest_result(EXPERIMENT_ID, GIT_COMMIT, report, summary=summary)
    log.info("Results also saved to backtest_results table")

    print(f"\n=== Experiment {EXPERIMENT_ID} (commit {GIT_COMMIT[:8]}) ===\n")
    for key, label in (
        ("comparison_A_first_vs_subsequent_poc_retest", "A: first POC retest vs subsequent POC retests"),
        ("comparison_B_first_poc_vs_first_random_level", "B: first POC retest vs first random-level retest"),
    ):
        c = report[key]
        perm = c["permutation_test"]
        p = perm["p_value_two_sided"] if perm else "n/a (< min group size)"
        print(f"{label}")
        print(f"  n_a={c['n_a']} hold_rate_a={c['stats_a'].get('hold_rate_pct')}%  "
              f"n_b={c['n_b']} hold_rate_b={c['stats_b'].get('hold_rate_pct')}%  "
              f"cohens_d={c['cohens_d']}  p_value={p}\n")

    print("Interpretation: hold_rate is the fraction of retest events where price ended up on the side it")
    print("approached from HOLD_HORIZON bars later (a bounce) rather than the opposite side (a break-through).")
    print("p_value is a two-sided label-permutation test on the difference in mean hold rate between the two")
    print("groups. Small-sample caveat: price_history_hourly has only a few weeks of real depth per PR C's own")
    print("finding -- inspect n_a/n_b before trusting either p_value. Neither comparison establishes causality,")
    print("profitability after costs, or out-of-sample robustness.")


if __name__ == "__main__":
    main()

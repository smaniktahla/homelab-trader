#!/usr/bin/env python3
"""Experiment 015: previous value-area re-entry hypothesis significance
test.

Volume & Volume Profile epic, PR F (Volume Profile hypothesis
experiments, Workstream B) -- the first of the three Volume-Profile-
specific families (POC retest, HVN/LVN, value-area re-entry), and the one
the epic spec itself flags as "the cleanest hypothesis, high priority."
Research artifact, not production logic.

Question: when a session opens outside the previous session's value area
(above VAH or below VAL) and then confirms re-entry (closes back inside),
does it go on to reach the previous session's POC before invalidating,
more often than it would reach an arbitrary comparable level in the same
range? "Confirmed re-entry" is tested under two independent, pre-
registered acceptance definitions (1 close back inside vs. 2 consecutive
closes back inside), per the epic spec's same "precisely and multiply
defined" bar Experiment 013 (divergence) already applied to its own
"declining volume" definition.

Data source and session definition: built on shared/volume_profile.py's
existing engine (PR C), which is itself built on price_history_hourly --
100% Alpaca IEX hourly bars (see ingest.py::ingest_hourly_prices, single
source, no cross-feed mixing possible for this table). A "session" here
is one calendar day's hourly bars (grouped by bar.ts.date(), UTC -- US
market hours don't cross UTC midnight, so this is a safe proxy for a
trading session at hourly granularity); the previous session's profile
(POC/VAH/VAL) is computed once per (symbol, day) pair from that day's own
bars via compute_volume_profile(), fully known by the time the next
session opens -- no lookahead. All re-entry confirmation, target, and
invalidation checks happen strictly within the SAME session as the open
(never crossing into a later day), keeping session boundaries explicit
and never silently mixed, per the epic's requirement -- an unresolved
event (session ends before hitting target or invalidation) is recorded as
its own outcome, not carried into the next session's data.

Baseline (mandatory per the epic spec, "random comparable levels"): for
every re-entry event, the SAME set of subsequent bars is also checked
against the value-area midpoint ((VAH+VAL)/2) as a naive comparable
target -- distinct from POC whenever volume within the value area is
skewed. This directly tests "does POC specifically matter" rather than
"does *some* point in the range get revisited," via a paired sign-
permutation test on the two targets' hit rates (paired because both
targets are evaluated against the exact same events, not independent
samples -- the sign-flip null shuffles which target is "real" per event).

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 research/backtests/backtest_volume_profile_value_area_reentry.py
"""

import os
import sys
import json
import logging
import random
import statistics
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_engine import Bar
from volume_profile import compute_volume_profile
from db_utils import save_backtest_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EXPERIMENT_ID = "015_volume_profile_value_area_reentry"
GIT_COMMIT = os.environ.get("BACKTEST_GIT_COMMIT", "unknown")
N_PERMUTATIONS = int(os.environ.get("N_PERMUTATIONS", "2000"))
N_BOOTSTRAP = 1000
RANDOM_SEED = 45

ACCEPTANCE_DEFINITIONS = ("1_close", "2_consecutive_closes")
MIN_EVENTS = 20


def get_db():
    import psycopg2
    return psycopg2.connect(os.environ["DATABASE_URL"])


def get_universe_symbols(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM universe WHERE scannable=TRUE ORDER BY symbol")
        return [r[0] for r in cur.fetchall()]


def load_hourly_sessions(conn, symbol):
    """Returns an ordered list of (date, bars, feeds) per calendar day --
    the session grouping this experiment's lookahead-safety and baseline
    definitions above depend on."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ts, open, high, low, close, volume, source
            FROM price_history_hourly
            WHERE symbol=%s ORDER BY ts ASC
        """, (symbol,))
        rows = cur.fetchall()

    by_day = defaultdict(list)
    for ts, o, h, l, c, v, source in rows:
        bar = Bar(symbol=symbol, ts=ts, open=float(o), high=float(h), low=float(l),
                  close=float(c), volume=float(v) if v is not None else 0.0)
        by_day[ts.date()].append((bar, source))

    sessions = []
    for day in sorted(by_day):
        pairs = sorted(by_day[day], key=lambda p: p[0].ts)
        bars = [p[0] for p in pairs]
        feeds = [p[1] for p in pairs]
        sessions.append((day, bars, feeds))
    return sessions


def find_reentry_event(today_bars, val, vah, definition):
    """First bar index in today_bars (>=1, since index 0 is the opening
    bar that triggered the "opened outside" condition) satisfying the
    given acceptance definition. Returns None if never confirmed within
    the session."""
    for i in range(1, len(today_bars)):
        inside = val <= today_bars[i].close <= vah
        if not inside:
            continue
        if definition == "1_close":
            return i
        if definition == "2_consecutive_closes":
            if i >= 2 and val <= today_bars[i - 1].close <= vah:
                return i
    return None


def track_outcome(today_bars, reentry_idx, direction, target, invalidation_level):
    """From the bar AFTER re-entry confirmation to the end of the session:
    did price reach `target` before crossing back through
    `invalidation_level`? Both target and invalidation_level are prices;
    `direction` is +1 (opened above, re-entering downward, target below
    entry) or -1 (opened below, re-entering upward, target above entry).
    Returns (reached_target: bool, bars_to_resolution: int|None,
    invalidated: bool)."""
    for k in range(reentry_idx + 1, len(today_bars)):
        close = today_bars[k].close
        hit_target = (close <= target) if direction == 1 else (close >= target)
        hit_invalidation = (close >= invalidation_level) if direction == 1 else (close <= invalidation_level)
        if hit_target and hit_invalidation:
            # Both crossed in the same bar (wide bar/gap) -- target
            # resolution takes precedence since it's the question being
            # asked; genuinely ambiguous bars are rare enough not to need
            # a tie-break beyond a documented convention.
            return True, k - reentry_idx, False
        if hit_target:
            return True, k - reentry_idx, False
        if hit_invalidation:
            return False, k - reentry_idx, True
    return False, None, False  # session ended unresolved


def precompute_events(sessions):
    events = []
    for idx in range(1, len(sessions)):
        prev_day, prev_bars, prev_feeds = sessions[idx - 1]
        today_day, today_bars, _ = sessions[idx]
        if not prev_bars or len(today_bars) < 2:
            continue
        profile = compute_volume_profile(prev_bars, prev_feeds)
        if profile.poc is None or profile.vah is None or profile.val is None:
            continue

        open_price = today_bars[0].open
        if open_price > profile.vah:
            direction = 1  # opened above, re-entering downward
            invalidation_level = profile.vah  # breaking back above VAH un-does the re-entry
        elif open_price < profile.val:
            direction = -1
        else:
            continue

        midpoint = (profile.vah + profile.val) / 2
        if direction == -1:
            invalidation_level = profile.val

        for definition in ACCEPTANCE_DEFINITIONS:
            reentry_idx = find_reentry_event(today_bars, profile.val, profile.vah, definition)
            if reentry_idx is None:
                continue
            hit_poc, bars_poc, inval_poc = track_outcome(
                today_bars, reentry_idx, direction, profile.poc, invalidation_level)
            hit_mid, bars_mid, inval_mid = track_outcome(
                today_bars, reentry_idx, direction, midpoint, invalidation_level)
            events.append({
                "date": str(today_day), "definition": definition, "direction": direction,
                "hit_poc": hit_poc, "bars_to_poc": bars_poc,
                "hit_midpoint": hit_mid, "bars_to_midpoint": bars_mid,
            })
    return events


def bootstrap_ci(values, rng, n_resamples=N_BOOTSTRAP):
    if len(values) < 2:
        return None
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_resamples))
    return {"lo": round(means[int(0.025 * n_resamples)], 4), "hi": round(means[int(0.975 * n_resamples) - 1], 4)}


def paired_sign_permutation_test(diffs, n_permutations, seed):
    """Null: which of the two targets (POC vs. midpoint) is "real" is
    arbitrary per event, so randomly flipping each diff's sign should
    reproduce the same distribution if there's no real difference. Two-
    sided empirical p-value on the mean of `diffs` (per-event hit_poc -
    hit_midpoint, each in {-1, 0, 1})."""
    if len(diffs) < MIN_EVENTS:
        return None
    rng = random.Random(seed)
    observed = statistics.mean(diffs)
    null_means = []
    for _ in range(n_permutations):
        flipped = [d if rng.random() < 0.5 else -d for d in diffs]
        null_means.append(statistics.mean(flipped))
    count_ge = sum(1 for m in null_means if abs(m) >= abs(observed))
    return {
        "observed_mean_diff": round(observed, 4),
        "null_mean": round(statistics.mean(null_means), 4),
        "null_std": round(statistics.pstdev(null_means), 4),
        "p_value_two_sided": round((1 + count_ge) / (1 + len(null_means)), 5),
        "n_permutations": len(null_means),
    }


def summarize_definition(events, definition, rng):
    subset = [e for e in events if e["definition"] == definition]
    n = len(subset)
    result = {"definition": definition, "n_events": n}
    if n == 0:
        return result

    hit_poc_rate = round(100 * sum(1 for e in subset if e["hit_poc"]) / n, 2)
    hit_mid_rate = round(100 * sum(1 for e in subset if e["hit_midpoint"]) / n, 2)
    diffs = [int(e["hit_poc"]) - int(e["hit_midpoint"]) for e in subset]
    poc_times = [e["bars_to_poc"] for e in subset if e["bars_to_poc"] is not None]
    mid_times = [e["bars_to_midpoint"] for e in subset if e["bars_to_midpoint"] is not None]

    result.update({
        "hit_poc_rate_pct": hit_poc_rate,
        "hit_midpoint_rate_pct": hit_mid_rate,
        "median_bars_to_poc": statistics.median(poc_times) if poc_times else None,
        "median_bars_to_midpoint": statistics.median(mid_times) if mid_times else None,
        "hit_rate_diff_ci_95": bootstrap_ci([float(d) for d in diffs], rng),
        "paired_permutation_test": paired_sign_permutation_test(diffs, N_PERMUTATIONS, RANDOM_SEED + hash(definition) % 1000),
    })
    return result


def main():
    conn = get_db()
    symbols = get_universe_symbols(conn)
    log.info(f"Volume Profile value-area re-entry: {len(symbols)} universe symbols, "
             f"acceptance_definitions={ACCEPTANCE_DEFINITIONS}")

    all_events = []
    per_symbol_counts = {}
    for idx, sym in enumerate(symbols):
        sessions = load_hourly_sessions(conn, sym)
        if len(sessions) < 2:
            continue
        events = precompute_events(sessions)
        per_symbol_counts[sym] = len(events)
        all_events.extend(events)
        if (idx + 1) % 100 == 0:
            log.info(f"...{idx + 1}/{len(symbols)} symbols prepared")
    conn.close()
    log.info(f"Prepared {len(all_events)} re-entry events across "
             f"{sum(1 for v in per_symbol_counts.values() if v > 0)} symbols with any events "
             f"(price_history_hourly has only a few weeks of real depth -- small-sample caveat is expected)")

    rng = random.Random(RANDOM_SEED)
    report = {
        "experiment_id": EXPERIMENT_ID,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": GIT_COMMIT,
        "reproducibility": {
            "source_table": "price_history_hourly",
            "feed": "alpaca_iex",
            "session_definition": "calendar_day_utc_of_hourly_bars",
            "acceptance_definitions": ACCEPTANCE_DEFINITIONS,
            "n_permutations": N_PERMUTATIONS,
            "n_bootstrap": N_BOOTSTRAP,
            "random_seed": RANDOM_SEED,
            "universe_size": len(symbols),
        },
        "n_total_events": len(all_events),
        "by_definition": {},
    }

    for definition in ACCEPTANCE_DEFINITIONS:
        result = summarize_definition(all_events, definition, rng)
        report["by_definition"][definition] = result
        log.info(f"{definition}: n_events={result['n_events']} "
                 f"hit_poc_rate={result.get('hit_poc_rate_pct')} hit_mid_rate={result.get('hit_midpoint_rate_pct')}")

    with open("/tmp/backtest_results_015.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info("Full results written to /tmp/backtest_results_015.json")

    summary_parts = []
    for definition, result in report["by_definition"].items():
        perm = result.get("paired_permutation_test")
        p = perm["p_value_two_sided"] if perm else None
        summary_parts.append(f"{definition}: n={result['n_events']} p={p}")
    save_backtest_result(EXPERIMENT_ID, GIT_COMMIT, report, summary=" | ".join(summary_parts))
    log.info("Results also saved to backtest_results table")

    print(f"\n=== Experiment {EXPERIMENT_ID} (commit {GIT_COMMIT[:8]}) ===")
    for definition, result in report["by_definition"].items():
        print(f"\n{definition}: n_events={result['n_events']}")
        if result["n_events"] == 0:
            continue
        perm = result.get("paired_permutation_test")
        p = perm["p_value_two_sided"] if perm else "n/a (< min events)"
        print(f"  hit_poc_rate={result['hit_poc_rate_pct']}%  hit_midpoint_rate={result['hit_midpoint_rate_pct']}%  "
              f"p_value={p}")
        print(f"  median_bars_to_poc={result['median_bars_to_poc']}  median_bars_to_midpoint={result['median_bars_to_midpoint']}")

    print("\nInterpretation: hit_poc_rate/hit_midpoint_rate are the fraction of confirmed re-entry events that")
    print("reached that target before invalidating (or the session ending unresolved). p_value is a paired")
    print("sign-permutation test on whether POC specifically is reached more/less often than a naive value-area")
    print("midpoint on the SAME events. Small-sample caveat: price_history_hourly has only a few weeks of real")
    print("depth per the epic's own PR C documentation -- treat n_events accordingly before trusting p_value.")
    print("This does NOT establish causality, profitability after costs, or out-of-sample robustness.")


if __name__ == "__main__":
    main()

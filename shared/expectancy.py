"""
Trade-level expectancy math -- Platform Improvements PR B (expectancy
reporting). Only ever scoped in conversation before this PR (see
docs/research-backtesting-improvements.md's references to "PR B's
segmentation"); this module is the first actual implementation.

Distinct from ingest/postmortem.py's existing bucket_stats (which runs over
signal_outcomes.forward_return_20d -- hypothetical, fixed-window, one row
per generated signal, large N) -- this module computes real trade-level
expectancy from shared/position_lifecycles.py's PositionLifecycle rows
(actual filled trades, small N today: 25 lifecycles total as of this PR,
most missing realized_r since that field only populates for trades placed
since Platform Improvements PR A shipped on 2026-08-03). Both are legitimate,
different measurements -- see ingest/postmortem.py for how they're kept
separate in the same review row rather than conflated.

Dollar expectancy (mean net_pnl) is always computed. R-multiple expectancy
is computed ONLY over the subset of a segment's lifecycles that have a
non-null realized_r, reported alongside its own n_with_r denominator --
never silently averaged over a near-empty R sample and mistaken for "the"
expectancy. profit_factor is None (undefined), not infinity, when a segment
has zero losing trades. Every field mirrors the existing NULL-vs-zero
discipline used throughout this codebase (symbol_features, round_trips.py
(removed)/lifecycle_performance.py, position_lifecycles.py).

No DB access here -- callers (ingest/postmortem.py) fetch and join rows,
then pass them in, matching the pure-function style already established by
shared/lifecycle_performance.py and shared/position_lifecycles.py.
"""

MIN_LIFECYCLE_N_PRELIMINARY = 5    # below this: "insufficient" -- don't trust at all
MIN_LIFECYCLE_N_ESTABLISHED = 20   # below this (but >= preliminary): "preliminary" -- directional only

HOLDING_PERIOD_BUCKETS = [
    ("<1d", 0, 1),
    ("1-5d", 1, 5),
    ("5-20d", 5, 20),
    ("20d+", 20, float("inf")),
]


def holding_period_bucket(holding_days):
    """Maps a float holding-period (in days) to one of HOLDING_PERIOD_BUCKETS'
    labels, or None if holding_days is None (e.g. a lifecycle missing
    opened_at/closed_at, which shouldn't happen for a real closed lifecycle
    but is handled the same NULL-safe way postmortem.py's own
    _score_bucket handles a missing score)."""
    if holding_days is None:
        return None
    for label, lo, hi in HOLDING_PERIOD_BUCKETS:
        if lo <= holding_days < hi:
            return label
    return None


def _sample_quality(n):
    if n < MIN_LIFECYCLE_N_PRELIMINARY:
        return "insufficient"
    if n < MIN_LIFECYCLE_N_ESTABLISHED:
        return "preliminary"
    return "established"


def bucket_stats(rows):
    """rows: list of (bucket_key, net_pnl, realized_r_or_None) -- one tuple
    per closed lifecycle already assigned to a segment bucket by the caller
    (bucket_key must never be None; callers filter those out first, same
    convention ingest/postmortem.py's own _score_bucket-based grouping
    already uses).

    Returns {bucket: {n, win_rate, avg_win, avg_loss, profit_factor,
    expectancy_dollars, n_with_r, expectancy_r, sample_quality}}.
    """
    buckets = {}
    for bucket, net_pnl, r in rows:
        buckets.setdefault(bucket, []).append((net_pnl, r))

    out = {}
    for bucket, pairs in buckets.items():
        n = len(pairs)
        pnls = [p for p, _ in pairs]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_profit = sum(wins)
        gross_loss = sum(losses)  # <= 0, or 0.0 if no losses
        r_values = [r for _, r in pairs if r is not None]

        out[bucket] = {
            "n": n,
            "win_rate": round(100 * len(wins) / n, 1),
            "avg_win": round(gross_profit / len(wins), 2) if wins else None,
            "avg_loss": round(gross_loss / len(losses), 2) if losses else None,
            "profit_factor": round(gross_profit / abs(gross_loss), 2) if gross_loss < 0 else None,
            "expectancy_dollars": round(sum(pnls) / n, 2),
            "n_with_r": len(r_values),
            "expectancy_r": round(sum(r_values) / len(r_values), 3) if r_values else None,
            "sample_quality": _sample_quality(n),
        }
    return out

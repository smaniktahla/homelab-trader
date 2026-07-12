"""
Weekly strategy postmortem: calibration review over resolved signal_outcomes.

Recomputes score/regime/approval-bucket win rates from scratch each run (not
an incremental diff) so the read benefits from all accumulated history, not
just the last week's slice. Writes one advisory row to
strategy_review_proposals per run — it never touches signal_params directly.
A human reviews the finding and, if they agree, applies the change themselves
via the existing PATCH /api/signal-params/{key} endpoint.
"""

import json
import logging

log = logging.getLogger(__name__)

WINDOW_DAYS = 180          # how far back to look for resolved outcomes
MIN_BUCKET_N = 15          # minimum observations before a bucket is trusted
MIN_GAP_PP = 15.0          # min win-rate gap (percentage points) to propose a change

SCORE_BUCKETS = [
    ("30-49", 30, 50),
    ("50-64", 50, 65),
    ("65-79", 65, 80),
    ("80+", 80, 1000),
]


def _bucket_stats(rows):
    """rows: list of (bucket_key, forward_return_20d). Returns {bucket: {n, win_rate, avg_return}}."""
    buckets = {}
    for bucket, ret in rows:
        buckets.setdefault(bucket, []).append(ret)
    out = {}
    for bucket, rets in buckets.items():
        n = len(rets)
        wins = sum(1 for r in rets if r > 0)
        out[bucket] = {
            "n": n,
            "win_rate": round(100 * wins / n, 1),
            "avg_return": round(sum(rets) / n, 2),
        }
    return out


def _fetch_resolved(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT score, symbol_regime, approval_status, forward_return_20d, generated_at
            FROM signal_outcomes
            WHERE side = 'buy'
              AND forward_return_20d IS NOT NULL
              AND generated_at >= NOW() - INTERVAL '%s days'
            ORDER BY generated_at ASC
        """, (WINDOW_DAYS,))
        return cur.fetchall()


def _score_bucket(score):
    if score is None:
        return None
    for label, lo, hi in SCORE_BUCKETS:
        if lo <= score < hi:
            return label
    return None


def _propose_score_threshold_change(conn, score_stats):
    """If low score buckets clearly underperform high buckets with enough N,
    propose raising score_proposal_min to the boundary of the better bucket."""
    ordered = [(label, lo, hi) for label, lo, hi in SCORE_BUCKETS if label in score_stats]
    trusted = [(label, lo, hi) for label, lo, hi in ordered if score_stats[label]["n"] >= MIN_BUCKET_N]
    if len(trusted) < 2:
        return None

    worst_label, worst_lo, _ = trusted[0]
    best_label, best_lo, _ = max(trusted, key=lambda t: score_stats[t[0]]["win_rate"])
    if best_lo <= worst_lo:
        return None  # already proposing the floor, nothing to raise

    gap = score_stats[best_label]["win_rate"] - score_stats[worst_label]["win_rate"]
    if gap < MIN_GAP_PP:
        return None

    with conn.cursor() as cur:
        cur.execute("SELECT value FROM signal_params WHERE key='score_proposal_min'")
        row = cur.fetchone()
    current = float(row[0]) if row else 30.0
    if current >= best_lo:
        return None  # threshold already at/above the proposed floor

    return {
        "proposed_param": "score_proposal_min",
        "current_value": current,
        "proposed_value": float(best_lo),
        "reason": (
            f"score bucket {worst_label} won {score_stats[worst_label]['win_rate']}% "
            f"(N={score_stats[worst_label]['n']}) vs {best_label} at "
            f"{score_stats[best_label]['win_rate']}% (N={score_stats[best_label]['n']}), "
            f"a {gap:.1f}pp gap over the {MIN_GAP_PP}pp threshold"
        ),
    }


def run_postmortem_review(conn):
    rows = _fetch_resolved(conn)
    n = len(rows)

    if n < MIN_BUCKET_N:
        finding = f"Insufficient data: {n} resolved buy signals in the last {WINDOW_DAYS}d (need {MIN_BUCKET_N}+ per bucket). Skipping calibration check."
        _insert_review(conn, n, {}, finding, None)
        return {"n_resolved": n, "finding": finding, "proposal": None}

    score_rows = [(_score_bucket(float(s)), float(r)) for s, _, _, r, _ in rows if s is not None]
    score_rows = [(b, r) for b, r in score_rows if b is not None]
    score_stats = _bucket_stats(score_rows)

    regime_rows = [(reg, float(r)) for _, reg, _, r, _ in rows if reg]
    regime_stats = _bucket_stats(regime_rows)

    approval_rows = [(ap, float(r)) for _, _, ap, r, _ in rows if ap in ("approved", "rejected")]
    approval_stats = _bucket_stats(approval_rows)

    metric_summary = {
        "score_buckets": score_stats,
        "symbol_regime": regime_stats,
        "approval_status": approval_stats,
    }

    proposal = _propose_score_threshold_change(conn, score_stats)

    if proposal:
        finding = (
            f"Score calibration gap found: {proposal['reason']}. "
            f"Suggest raising score_proposal_min from {proposal['current_value']} to {proposal['proposed_value']}."
        )
    else:
        finding = f"No calibration change proposed this cycle (N={n} resolved). Bucket stats logged for trend-watching."

    _insert_review(conn, n, metric_summary, finding, proposal)
    return {"n_resolved": n, "finding": finding, "proposal": proposal}


def _insert_review(conn, n_resolved, metric_summary, finding, proposal):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO strategy_review_proposals
                (window_start, window_end, n_resolved, metric_summary, finding,
                 proposed_param, current_value, proposed_value)
            VALUES (NOW() - INTERVAL '%s days', NOW(), %s, %s, %s, %s, %s, %s)
        """, (
            WINDOW_DAYS, n_resolved, json.dumps(metric_summary), finding,
            proposal["proposed_param"] if proposal else None,
            proposal["current_value"] if proposal else None,
            proposal["proposed_value"] if proposal else None,
        ))
    conn.commit()
    log.info(f"Postmortem review recorded: {finding}")

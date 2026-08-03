"""
Weekly strategy postmortem: calibration review over resolved signal_outcomes,
plus (Platform Improvements PR B) a real trade-level expectancy report over
closed position_lifecycles.

Recomputes score/regime/approval-bucket win rates from scratch each run (not
an incremental diff) so the read benefits from all accumulated history, not
just the last week's slice. Writes one advisory row to
strategy_review_proposals per run — it never touches signal_params directly.
A human reviews the finding and, if they agree, applies the change themselves
via the existing PATCH /api/signal-params/{key} endpoint.

The lifecycle-based expectancy segments (see _fetch_closed_lifecycles/
_lifecycle_segments below) are a deliberately SEPARATE, independent
computation from the score/regime/approval buckets above: signal_outcomes is
hypothetical (fixed 20-day forward return from every generated signal,
whether or not it was ever traded) while position_lifecycles is real filled
trades (currently a much smaller sample). They're merged into the same
metric_summary JSONB under lifecycle_*-prefixed keys so both render in the
same Strategy Review UI row without conflating what each one actually
measures — see shared/expectancy.py's own module docstring for the full
rationale.
"""

import json
import logging

import psycopg2.extras

import expectancy

log = logging.getLogger(__name__)

WINDOW_DAYS = 180          # how far back to look for resolved outcomes
MIN_BUCKET_N = 15          # minimum observations before a bucket is trusted
MIN_RETURN_GAP_PCT = 2.0   # min avg forward-return gap (pct) to propose a change

SCORE_BUCKETS = [
    ("30-49", 30, 50),
    ("50-64", 50, 65),
    ("65-79", 65, 80),
    ("80+", 80, 1000),
]


def _bucket_stats(rows):
    """rows: list of (bucket_key, forward_return_20d).
    Returns {bucket: {n, win_rate, avg_return, avg_win, avg_loss}}.
    avg_win/avg_loss are None when a bucket has no observations on that side —
    win_rate alone can hide a bucket that wins often but small and loses rarely but big.
    """
    buckets = {}
    for bucket, ret in rows:
        buckets.setdefault(bucket, []).append(ret)
    out = {}
    for bucket, rets in buckets.items():
        n = len(rets)
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]
        out[bucket] = {
            "n": n,
            "win_rate": round(100 * len(wins) / n, 1),
            "avg_return": round(sum(rets) / n, 2),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
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


def _fetch_closed_lifecycles(conn):
    """One row per CLOSED position_lifecycles entry, joined for the extra
    segmentation dimensions the lifecycle table itself doesn't carry:
    thesis slug, sector (from universe), market_regime (from the first
    entry trade's originating signal_outcomes row, if any -- NULL for
    manual trades or ones with no linked signal), and exit_reason (from the
    final exit trade's originating trade_proposals row, if any). Explicit
    dict cursor regardless of the caller's connection default -- same
    discipline ingest/build_position_lifecycles.py already established,
    since ingest.py's own connections default to plain tuple cursors."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                pl.symbol, pl.opened_at, pl.closed_at, pl.net_pnl, pl.realized_r,
                th.slug AS thesis_slug,
                u.sector,
                first_entry.market_regime,
                last_exit.exit_reason
            FROM position_lifecycles pl
            LEFT JOIN theses th ON th.id = pl.thesis_id
            LEFT JOIN universe u ON u.symbol = pl.symbol
            LEFT JOIN LATERAL (
                SELECT so.market_regime
                FROM position_trades pt
                JOIN trades t ON t.id = pt.trade_id
                JOIN signal_outcomes so ON so.proposal_id = t.proposal_id
                WHERE pt.position_lifecycle_id = pl.id AND pt.role = 'entry'
                ORDER BY t.traded_at ASC
                LIMIT 1
            ) first_entry ON true
            LEFT JOIN LATERAL (
                SELECT tp.exit_reason
                FROM position_trades pt
                JOIN trades t ON t.id = pt.trade_id
                JOIN trade_proposals tp ON tp.id = t.proposal_id
                WHERE pt.position_lifecycle_id = pl.id AND pt.role = 'exit'
                ORDER BY t.traded_at DESC
                LIMIT 1
            ) last_exit ON true
            WHERE pl.status = 'closed'
        """)
        return cur.fetchall()


def _holding_days(opened_at, closed_at):
    if not opened_at or not closed_at:
        return None
    return (closed_at - opened_at).total_seconds() / 86400


def _lifecycle_segments(rows):
    """Builds the seven Platform Improvements PR B segment groupings from
    closed-lifecycle rows (as returned by _fetch_closed_lifecycles). Each
    dimension filters out rows where its own key is unresolvable -- same
    "skip unresolved" convention _score_bucket's own callers already use --
    so e.g. a manual trade with no linked signal_outcomes still contributes
    to every OTHER dimension, just not lifecycle_market_regime. Dropped
    dimensions (model_version, feature_version -- no data path to real
    trades exists yet; approval_status, score bucket -- already covered by
    the signal-level buckets above, not meaningful for trades that were, by
    definition, all approved) are a deliberate v1 scoping decision, not an
    oversight."""
    def _rows_for(key_fn):
        out = []
        for r in rows:
            key = key_fn(r)
            if key is None:
                continue
            net_pnl = float(r["net_pnl"])
            realized_r = float(r["realized_r"]) if r["realized_r"] is not None else None
            out.append((key, net_pnl, realized_r))
        return out

    return {
        "lifecycle_thesis": expectancy.bucket_stats(_rows_for(lambda r: r["thesis_slug"])),
        "lifecycle_symbol": expectancy.bucket_stats(_rows_for(lambda r: r["symbol"])),
        "lifecycle_exit_reason": expectancy.bucket_stats(_rows_for(lambda r: r["exit_reason"])),
        "lifecycle_market_regime": expectancy.bucket_stats(_rows_for(lambda r: r["market_regime"])),
        "lifecycle_sector": expectancy.bucket_stats(_rows_for(lambda r: r["sector"])),
        "lifecycle_holding_period": expectancy.bucket_stats(
            _rows_for(lambda r: expectancy.holding_period_bucket(_holding_days(r["opened_at"], r["closed_at"])))
        ),
        "lifecycle_calendar_period": expectancy.bucket_stats(
            _rows_for(lambda r: r["closed_at"].strftime("%Y-%m") if r["closed_at"] else None)
        ),
    }


def _propose_score_threshold_change(conn, score_stats):
    """If low score buckets clearly underperform high buckets with enough N,
    propose raising score_proposal_min to the boundary of the better bucket.

    Ranks buckets by avg_return (expectancy), not win_rate: a bucket can win
    often on small moves and lose rarely on big ones and still be the worse
    bucket to hold. win_rate alone would pick that bucket as "best" and raise
    the threshold in the wrong direction.
    """
    ordered = [(label, lo, hi) for label, lo, hi in SCORE_BUCKETS if label in score_stats]
    trusted = [(label, lo, hi) for label, lo, hi in ordered if score_stats[label]["n"] >= MIN_BUCKET_N]
    if len(trusted) < 2:
        return None

    worst_label, worst_lo, _ = trusted[0]
    best_label, best_lo, _ = max(trusted, key=lambda t: score_stats[t[0]]["avg_return"])
    if best_lo <= worst_lo:
        return None  # already proposing the floor, nothing to raise

    gap = score_stats[best_label]["avg_return"] - score_stats[worst_label]["avg_return"]
    if gap < MIN_RETURN_GAP_PCT:
        return None

    with conn.cursor() as cur:
        cur.execute("SELECT value FROM signal_params WHERE key='score_proposal_min'")
        row = cur.fetchone()
    current = float(row[0]) if row else 30.0
    if current >= best_lo:
        return None  # threshold already at/above the proposed floor

    worst, best = score_stats[worst_label], score_stats[best_label]
    return {
        "proposed_param": "score_proposal_min",
        "current_value": current,
        "proposed_value": float(best_lo),
        "reason": (
            f"score bucket {worst_label} avg_return {worst['avg_return']}% "
            f"(win_rate {worst['win_rate']}%, avg_win {worst['avg_win']}, avg_loss {worst['avg_loss']}, "
            f"N={worst['n']}) vs {best_label} at {best['avg_return']}% "
            f"(win_rate {best['win_rate']}%, avg_win {best['avg_win']}, avg_loss {best['avg_loss']}, "
            f"N={best['n']}), a {gap:.1f}pp avg-return gap over the {MIN_RETURN_GAP_PCT}pp threshold"
        ),
    }


def run_postmortem_review(conn):
    rows = _fetch_resolved(conn)
    n = len(rows)

    # Independent of the signal-level sample below -- a slow week for new
    # signals doesn't mean there's nothing to say about real trade
    # expectancy, and vice versa. Always computed, in both branches below.
    lifecycle_rows = _fetch_closed_lifecycles(conn)
    lifecycle_segments = _lifecycle_segments(lifecycle_rows)
    lifecycle_note = (
        f" | Trade-level expectancy: {len(lifecycle_rows)} closed position(s) "
        f"across {len(lifecycle_segments)} segment views."
    )

    if n < MIN_BUCKET_N:
        finding = (
            f"Insufficient data: {n} resolved buy signals in the last {WINDOW_DAYS}d "
            f"(need {MIN_BUCKET_N}+ per bucket). Skipping calibration check."
        ) + lifecycle_note
        _insert_review(conn, n, dict(lifecycle_segments), finding, None)
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
        **lifecycle_segments,
    }

    proposal = _propose_score_threshold_change(conn, score_stats)

    if proposal:
        finding = (
            f"Score calibration gap found: {proposal['reason']}. "
            f"Suggest raising score_proposal_min from {proposal['current_value']} to {proposal['proposed_value']}."
        ) + lifecycle_note
    else:
        finding = f"No calibration change proposed this cycle (N={n} resolved). Bucket stats logged for trend-watching." + lifecycle_note

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

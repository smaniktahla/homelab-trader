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

Platform Improvements PR C adds a third, again independent, computation:
rule_adherence_by_context/rule_adherence_by_rule, aggregating
rule_adherence_checks (see shared/rule_adherence.py) -- how often a manual
trade or stale proposal approval bypassed one of compute_signals()'s
buy-side gates. Same "always computed, merged in under its own prefixed
keys" treatment as the lifecycle segments, and for the same reason: a slow
week for any one of these three data sources says nothing about the other
two.

Risk Engine PR 2 adds a fourth: risk_decision_by_context/
risk_decision_by_binding_constraint, aggregating risk_decisions (see
shared/risk_engine.py) -- how often the (now-binding, as of PR 1) risk
engine reduces or rejects a proposed trade, which single constraint binds
most often, and how much of a strategy's requested quantity actually
clears. Same independent-source treatment as the other three.
"""

import json
import logging
from collections import Counter

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
    manual trades or ones with no linked signal), exit_reason (from the
    final exit trade's originating trade_proposals row, if any), and (PR 11,
    Hypothesis-Driven Trading Architecture epic) hypothesis_type from the
    lifecycle's linked trade_thesis, if any -- thesis_slug above is the
    STRATEGY FAMILY (theses.slug, e.g. 'mean_reversion'), hypothesis_type
    is the falsifiable per-opportunity hypothesis a single strategy family
    can instantiate more than one of (e.g. 'mean_reversion_oversold' vs.
    'mean_reversion_overbought', per shared/trade_thesis.py's
    HYPOTHESIS_TYPES) -- a genuinely different, finer-grained axis, not a
    duplicate of thesis_slug. Explicit dict cursor regardless of the
    caller's connection default -- same discipline
    ingest/build_position_lifecycles.py already established, since
    ingest.py's own connections default to plain tuple cursors."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                pl.symbol, pl.opened_at, pl.closed_at, pl.net_pnl, pl.realized_r,
                th.slug AS thesis_slug,
                tt.hypothesis_type,
                u.sector,
                first_entry.market_regime,
                last_exit.exit_reason
            FROM position_lifecycles pl
            LEFT JOIN theses th ON th.id = pl.thesis_id
            LEFT JOIN trade_theses tt ON tt.id = pl.trade_thesis_id
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
        # PR 11: hypothesis_type (per-opportunity, e.g. 'mean_reversion_oversold'),
        # not trade_thesis_id itself -- a trade_thesis instance is
        # per-opportunity (§3 of docs/trade-thesis-architecture-reconciliation.md),
        # so bucketing by its raw id would put ~one lifecycle in every
        # bucket and never accumulate the sample size expectancy.bucket_stats
        # needs to say anything. hypothesis_type is the axis multiple
        # lifecycles actually share.
        "lifecycle_hypothesis_type": expectancy.bucket_stats(_rows_for(lambda r: r["hypothesis_type"])),
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


def _fetch_rule_adherence_checks(conn):
    """All rule_adherence_checks rows within WINDOW_DAYS -- same window
    convention _fetch_resolved above uses, though this is a genuinely
    independent data source (see run_postmortem_review). rule_results is a
    JSONB column; psycopg2 deserializes it to a plain list of dicts
    automatically, no json.loads needed."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT context, rule_results, any_violation
            FROM rule_adherence_checks
            WHERE checked_at >= NOW() - INTERVAL '%s days'
            ORDER BY checked_at ASC
        """, (WINDOW_DAYS,))
        return cur.fetchall()


def _rule_adherence_segments(rows):
    """Two views over the same rule_adherence_checks rows -- Platform
    Improvements PR C:

    rule_adherence_by_context: one bucket per context ('manual_trade' /
    'proposal_approval') -- n, n_with_violation, violation_rate_pct, and
    which single rule was violated most often within that context.

    rule_adherence_by_rule: one bucket per individual gate name, across
    BOTH contexts combined -- n_checks, n_failed, fail_rate_pct. Answers
    "which single rule gets bypassed most often" regardless of whether it
    happened via a manual trade or a stale proposal approval.
    """
    by_context = {}
    rule_totals = Counter()
    rule_failures = Counter()

    for row in rows:
        context = row["context"]
        results = row["rule_results"]
        violated_rules = [r["rule"] for r in results if not r["passed"]]

        bucket = by_context.setdefault(
            context, {"n": 0, "n_with_violation": 0, "_violated_rule_counts": Counter()})
        bucket["n"] += 1
        if row["any_violation"]:
            bucket["n_with_violation"] += 1
            bucket["_violated_rule_counts"].update(violated_rules)

        for r in results:
            rule_totals[r["rule"]] += 1
            if not r["passed"]:
                rule_failures[r["rule"]] += 1

    context_out = {}
    for context, b in by_context.items():
        most_common = b["_violated_rule_counts"].most_common(1)
        context_out[context] = {
            "n": b["n"],
            "n_with_violation": b["n_with_violation"],
            "violation_rate_pct": round(100 * b["n_with_violation"] / b["n"], 1) if b["n"] else None,
            "most_common_violated_rule": most_common[0][0] if most_common else None,
        }

    rule_out = {}
    for rule, n_checks in rule_totals.items():
        n_failed = rule_failures.get(rule, 0)
        rule_out[rule] = {
            "n_checks": n_checks,
            "n_failed": n_failed,
            "fail_rate_pct": round(100 * n_failed / n_checks, 1) if n_checks else None,
        }

    return {
        "rule_adherence_by_context": context_out,
        "rule_adherence_by_rule": rule_out,
    }


def _fetch_risk_decisions(conn):
    """All risk_decisions rows (shared/risk_engine.py, Risk Engine PR 1)
    within WINDOW_DAYS -- same window convention every other independent
    data source in this file uses."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT context, outcome, binding_constraint, requested_qty, approved_quantity
            FROM risk_decisions
            WHERE decided_at >= NOW() - INTERVAL '%s days'
            ORDER BY decided_at ASC
        """, (WINDOW_DAYS,))
        return cur.fetchall()


def _risk_decision_segments(rows):
    """Two views over risk_decisions -- Risk Engine PR 2, same "always
    computed, independent data source" treatment as lifecycle/adherence
    segments above:

    risk_decision_by_context: one bucket per context
    ('proposal_generated' / 'proposal_approval' / 'manual_trade') -- n,
    n_reduced, n_rejected, and avg_fill_ratio_pct (approved_quantity /
    requested_qty, averaged over non-rejected decisions only -- a
    rejection has approved_quantity=0 by definition, which would just
    drag every context's ratio toward 0 without saying anything new; its
    rate is already captured by rejected_rate_pct).

    risk_decision_by_binding_constraint: one bucket per constraint name
    that actually bound a decision (position_allocation, risk_budget,
    portfolio_open_risk, sector_exposure, buying_power, or a rejection
    reason like no_portfolio_data) -- counted only over reduced/rejected
    decisions, since an approved decision has binding_constraint=NULL by
    definition (see shared/risk_engine.py). Answers "which single
    constraint most often limits sizing," same shape/intent as
    rule_adherence_by_rule above but for the binding risk engine instead
    of the advisory bypass-detection layer.
    """
    by_context = {}
    constraint_counts = Counter()

    for row in rows:
        b = by_context.setdefault(row["context"], {"n": 0, "n_reduced": 0, "n_rejected": 0, "_fill_ratios": []})
        b["n"] += 1
        if row["outcome"] == "rejected":
            b["n_rejected"] += 1
        else:
            if row["outcome"] == "reduced":
                b["n_reduced"] += 1
            if row["requested_qty"]:
                b["_fill_ratios"].append(float(row["approved_quantity"]) / float(row["requested_qty"]))

        if row["binding_constraint"] and row["outcome"] != "approved":
            constraint_counts[row["binding_constraint"]] += 1

    context_out = {}
    for context, b in by_context.items():
        ratios = b["_fill_ratios"]
        context_out[context] = {
            "n": b["n"],
            "n_reduced": b["n_reduced"],
            "n_rejected": b["n_rejected"],
            "reduced_rate_pct": round(100 * b["n_reduced"] / b["n"], 1) if b["n"] else None,
            "rejected_rate_pct": round(100 * b["n_rejected"] / b["n"], 1) if b["n"] else None,
            "avg_fill_ratio_pct": round(100 * sum(ratios) / len(ratios), 1) if ratios else None,
        }

    constraint_out = {constraint: {"n_binding": n} for constraint, n in constraint_counts.items()}

    return {
        "risk_decision_by_context": context_out,
        "risk_decision_by_binding_constraint": constraint_out,
    }


def _fetch_trade_theses_statuses(conn):
    """PR 12 (Hypothesis-Driven Trading Architecture epic): every
    trade_theses row's current (hypothesis_type, status) -- a fifth
    independent data source, same "always computed, merged under its own
    prefixed key" treatment as the four above. Deliberately NOT windowed
    by WINDOW_DAYS like the others: this is a current-state snapshot (how
    do hypotheses of each type actually resolve, right now), not a
    windowed event tally -- an old still-open thesis is exactly as
    relevant to that question as a new one, so excluding it by age would
    misrepresent the resolution rate rather than just narrow the sample."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT hypothesis_type, status FROM trade_theses")
        return cur.fetchall()


def _hypothesis_status_segments(rows):
    """Status distribution per hypothesis_type, straight from trade_theses
    -- genuinely different data from PR 11's lifecycle_hypothesis_type
    (which is P&L, derived from CLOSED position_lifecycles only). A trade
    thesis can resolve (invalidated or completed) without a linked
    position_lifecycles row ever closing -- e.g. invalidated before the
    position was ever opened -- so this answers "how do hypotheses of
    this type actually resolve" independent of whether a trade's P&L is
    known yet. See shared/trade_thesis.py::STATUSES for the vocabulary;
    'invalidated'/'completed'/'superseded' are terminal (per
    shared/trade_thesis_reevaluation.py), 'proposed'/'active'/'weakening'
    are not."""
    _TERMINAL = {"invalidated", "completed", "superseded"}

    counts_by_type = {}
    for r in rows:
        hyp = r["hypothesis_type"]
        if hyp is None:
            continue
        counts_by_type.setdefault(hyp, Counter())[r["status"]] += 1

    out = {}
    for hyp, status_counts in counts_by_type.items():
        total = sum(status_counts.values())
        resolved = sum(n for status, n in status_counts.items() if status in _TERMINAL)
        completed = status_counts.get("completed", 0)
        out[hyp] = {
            "total": total,
            "by_status": dict(status_counts),
            "resolution_rate_pct": round(100 * resolved / total, 1) if total else None,
            # Of theses that HAVE resolved, what fraction resolved as
            # 'completed' (thesis played out) rather than 'invalidated'/
            # 'superseded' -- None (not 0) while nothing has resolved yet,
            # same "unknown, not zero" convention this codebase uses
            # everywhere else for a denominator of zero.
            "completion_rate_of_resolved_pct": round(100 * completed / resolved, 1) if resolved else None,
        }
    return {"hypothesis_status_by_type": out}


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

    # Same independence principle as lifecycle_segments above -- a third,
    # unrelated data source (Platform Improvements PR C).
    adherence_rows = _fetch_rule_adherence_checks(conn)
    adherence_segments = _rule_adherence_segments(adherence_rows)
    adherence_note = f" | Rule adherence: {len(adherence_rows)} manual trade/approval check(s) in window."

    # Fourth independent data source (Risk Engine PR 2) -- risk_decisions
    # is written by shared/risk_engine.py at proposal-generation AND
    # approval time (two rows per approved buy), so len(risk_rows) counts
    # decisions, not trades.
    risk_rows = _fetch_risk_decisions(conn)
    risk_segments = _risk_decision_segments(risk_rows)
    risk_note = f" | Risk engine: {len(risk_rows)} sizing decision(s) in window."

    # Fifth independent data source (PR 12, Hypothesis-Driven Trading
    # Architecture epic) -- trade_theses status distribution, not windowed
    # (see _fetch_trade_theses_statuses' own docstring).
    hypothesis_rows = _fetch_trade_theses_statuses(conn)
    hypothesis_segments = _hypothesis_status_segments(hypothesis_rows)
    n_hypothesis_types = len(hypothesis_segments["hypothesis_status_by_type"])
    hypothesis_note = f" | Hypotheses: {len(hypothesis_rows)} trade thesis instance(s) across {n_hypothesis_types} type(s)."

    if n < MIN_BUCKET_N:
        finding = (
            f"Insufficient data: {n} resolved buy signals in the last {WINDOW_DAYS}d "
            f"(need {MIN_BUCKET_N}+ per bucket). Skipping calibration check."
        ) + lifecycle_note + adherence_note + risk_note + hypothesis_note
        _insert_review(conn, n, {**lifecycle_segments, **adherence_segments, **risk_segments, **hypothesis_segments}, finding, None)
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
        **adherence_segments,
        **risk_segments,
        **hypothesis_segments,
    }

    proposal = _propose_score_threshold_change(conn, score_stats)

    if proposal:
        finding = (
            f"Score calibration gap found: {proposal['reason']}. "
            f"Suggest raising score_proposal_min from {proposal['current_value']} to {proposal['proposed_value']}."
        ) + lifecycle_note + adherence_note + risk_note + hypothesis_note
    else:
        finding = (
            f"No calibration change proposed this cycle (N={n} resolved). Bucket stats logged for trend-watching."
            + lifecycle_note + adherence_note + risk_note + hypothesis_note
        )

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

"""
Live Thesis Re-Evaluation, PR 10 of the Hypothesis-Driven Trading
Architecture epic -- per docs/trade-thesis-architecture-reconciliation.md
§4/§5's PR 10 bullet. Follows ingest/postmortem.py's existing discipline:
re-evaluation writes an append-only row to trade_thesis_evaluations
(evaluated_at, state, evidence_diff, triggering_condition), and
trade_theses.status becomes a denormalized read of "most recent
evaluation's state," refreshed by this same job -- recompute-from-history-
and-write-one-summary-row, not incremental mutation. status is the one
trade_theses field PR 1's immutability contract (§4) explicitly excludes.

trade_thesis_evaluations.state reuses trade_thesis.STATUSES 1:1 -- no
separate richer enum. PR 1 §6 deferred this choice to PR 10; a richer
taxonomy has no defined trigger anywhere in this epic, so there's nothing
to justify one yet.

Per-evaluation transition, reusing PR 5's evaluate_thesis_invalidation()
and PR 5's own evaluate_condition_tree() against success_spec (no new
evaluator, no new evidence source):
- PR 5 says invalidated               -> 'invalidated'
- else success_spec evaluates True    -> 'completed'
- else                                -> 'active' (promotes 'proposed' on
  a thesis's first evaluation; re-confirms an already-'active' one)

'weakening' is part of trade_thesis.STATUSES (PR 1) but has no defined
trigger anywhere in this epic -- not produced by this PR, flagged rather
than invented.

Only theses NOT already in a terminal state (invalidated/completed/
superseded) are re-evaluated -- once terminal, a thesis's history is
closed, matching the append-only/no-incremental-mutation discipline.

Nothing here creates a trade_proposals row or otherwise changes trading
behavior -- that's shared/signals.py::check_thesis_invalidation_sell()
(PR 8), a separate caller of the same PR 5 evaluator. This module only
maintains the thesis's own lifecycle record.
"""

import json
import logging
from datetime import datetime, timezone

from trade_thesis import STATUSES, load_trade_thesis
from trade_thesis_invalidation import evaluate_condition_tree, evaluate_thesis_invalidation

log = logging.getLogger(__name__)

_TERMINAL_STATES = frozenset({"invalidated", "completed", "superseded"})


def _next_state(conn, thesis, as_of):
    invalidation_result = evaluate_thesis_invalidation(conn, thesis, as_of=as_of)
    if invalidation_result.invalidated:
        evidence_diff = {
            "invalidation_reasons": invalidation_result.reasons,
            "success_spec_met": None,
        }
        return "invalidated", evidence_diff, ", ".join(invalidation_result.reasons)

    success_spec_met = evaluate_condition_tree(conn, thesis.success_spec, thesis.symbol, as_of)
    if success_spec_met is True:
        evidence_diff = {"invalidation_reasons": [], "success_spec_met": True}
        return "completed", evidence_diff, "success_spec_triggered"

    evidence_diff = {"invalidation_reasons": [], "success_spec_met": success_spec_met}
    return "active", evidence_diff, None


def reevaluate_trade_thesis(conn, trade_thesis_id, as_of=None):
    """Re-evaluate one trade_theses row: appends a trade_thesis_evaluations
    row and, if the state changed, updates trade_theses.status to match.
    Fail-open -- returns None (never raises) if the thesis doesn't exist,
    is already terminal, or the write fails. Returns the new evaluation
    row's id on success."""
    as_of = as_of or datetime.now(timezone.utc)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM trade_theses WHERE id=%s", (trade_thesis_id,))
            row = cur.fetchone()
        if row is None:
            return None
        previous_state = row[0]
        if previous_state in _TERMINAL_STATES:
            return None

        thesis = load_trade_thesis(conn, trade_thesis_id)
        if thesis is None:
            return None

        new_state, evidence_diff, triggering_condition = _next_state(conn, thesis, as_of)
        if new_state not in STATUSES:
            log.warning(f"trade_thesis_reevaluation: computed illegal state '{new_state}' for {trade_thesis_id}, skipping")
            return None

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trade_thesis_evaluations
                    (trade_thesis_id, evaluated_at, previous_state, state, evidence_diff, triggering_condition)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (trade_thesis_id, as_of, previous_state, new_state,
                  json.dumps(evidence_diff), triggering_condition))
            evaluation_id = cur.fetchone()[0]

            if new_state != previous_state:
                cur.execute("UPDATE trade_theses SET status=%s WHERE id=%s", (new_state, trade_thesis_id))
        conn.commit()
        return evaluation_id
    except Exception as e:
        log.warning(f"trade_thesis_reevaluation: failed for trade_thesis_id={trade_thesis_id}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def reevaluate_active_trade_theses(conn):
    """Batch entry point, called once per ingest cycle -- re-evaluates
    every trade_theses row not already in a terminal state. Fail-open per
    thesis: one failure never blocks the rest. Returns the count of
    evaluations successfully written."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM trade_theses WHERE status NOT IN %s",
            (tuple(_TERMINAL_STATES),),
        )
        ids = [r[0] for r in cur.fetchall()]

    count = 0
    for trade_thesis_id in ids:
        if reevaluate_trade_thesis(conn, trade_thesis_id) is not None:
            count += 1
    return count

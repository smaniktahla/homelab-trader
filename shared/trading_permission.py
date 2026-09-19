"""
Trading-permission aggregation -- Risk Engine PR 3. See
docs/risk-engine-architecture-reconciliation.md section F (PR 3) and the
brief's own required shape:

    {"new_entries_allowed": bool, "scope": "account", "reasons": [...]}

Before this, "can we open a new position" was one ad hoc boolean
(circuit_breaker_active) computed independently in two places
(shared/signals.py::compute_signals() and shared/rule_adherence.py::
check_gates(), the latter hand-copying the drawdown formula -- fixed in
shared/circuit_breaker.py this same PR). This module is the one place
that combines every account-level halt condition into a single decision;
individual services (circuit_breaker.py, this module's own loss-streak
check) still emit their own halt condition, but only evaluate_trading_permission()
aggregates them into what callers actually act on.

Scope is always "account" today -- every halt condition implemented so
far is portfolio-wide (drawdown, loss streak). The strategy/sector/symbol
scopes the brief describes as "where practical" have no real halt
condition to aggregate yet (buy_cooldown and earnings_blackout are
per-symbol GATES already enforced directly in compute_signals(), not
account-wide PAUSES -- folding them into this shape would misrepresent a
single-symbol block as an account-wide one). Extending scope is future
work once a genuine account-vs-narrower distinction exists.
"""

import logging

import psycopg2.extensions

from circuit_breaker import current_high_water_mark, drawdown_pct_of, is_breached

log = logging.getLogger(__name__)

TRADING_PERMISSION_DEFAULTS = {
    "loss_streak_limit": 4,  # consecutive losing closed lifecycles before new entries pause
}


def current_loss_streak(conn):
    """Count of consecutive losing (net_pnl <= 0) CLOSED position_lifecycles,
    most-recently-closed first, stopping at the first winner (or the first
    lifecycle with net_pnl exactly 0.0, treated as a loss -- a scratch
    trade breaks a winning streak's momentum claim just as much as a real
    loss does, and this codebase's own win_rate convention elsewhere
    (shared/expectancy.py's `wins = [p for p in pnls if p > 0]`) already
    treats 0 as not-a-win).

    Excludes a lifecycle entirely (neither breaks nor extends the streak --
    it is simply skipped, as if it never closed) if ANY of its exit trades
    was explicitly marked counts_toward_loss_streak=FALSE -- the actual
    fix for the 2026-09-19 incident where a manual portfolio-cleanup sell
    tripped the same circuit breaker meant to catch an automated losing
    streak (see trades.counts_toward_loss_streak's schema comment).
    counts_toward_loss_streak IS NULL (pre-migration history, or a trade
    where the question doesn't apply) is treated as counting, same as
    this function's behavior before that column existed.

    Explicit tuple cursor regardless of the caller's connection default,
    same reasoning as every other shared module's DB functions in this
    codebase."""
    streak = 0
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute("""
            SELECT pl.net_pnl FROM position_lifecycles pl
            WHERE pl.status='closed' AND pl.net_pnl IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM position_trades pt
                  JOIN trades t ON t.id = pt.trade_id
                  WHERE pt.position_lifecycle_id = pl.id
                    AND pt.role = 'exit'
                    AND t.counts_toward_loss_streak = FALSE
              )
            ORDER BY pl.closed_at DESC
        """)
        for (net_pnl,) in cur.fetchall():
            if float(net_pnl) <= 0:
                streak += 1
            else:
                break
    return streak


def get_active_override(conn):
    """The current active trading_permission_overrides row (not revoked,
    not expired), or None. "Active" is evaluated at call time, not
    cached -- an override with a past expires_at is treated exactly as if
    it had been explicitly revoked."""
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute("""
            SELECT id, created_at, created_by, reason, expires_at
            FROM trading_permission_overrides
            WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY created_at DESC LIMIT 1
        """)
        row = cur.fetchone()
    if row is None:
        return None
    # isoformat()'d up front, not left as datetime objects -- this dict
    # flows straight into json.dumps(constraint_detail) in
    # api/main.py::_record_risk_decision() with no default=str handler,
    # so a raw datetime here would raise at record-time the first time an
    # override is ever active.
    return {
        "id": row[0], "created_by": row[2], "reason": row[3],
        "created_at": row[1].isoformat(),
        "expires_at": row[4].isoformat() if row[4] is not None else None,
    }


def create_override(conn, created_by, reason, expires_at=None):
    """Records a deliberate human decision to resume new entries early.
    Requires non-empty created_by/reason -- this is an audited safety-
    override action, not a config toggle, so it must always say who and
    why. Returns the new row's id, or None if created_by/reason is empty
    or the insert fails."""
    if not created_by or not reason:
        log.warning("trading_permission: create_override requires non-empty created_by and reason")
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trading_permission_overrides (created_by, reason, expires_at)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (created_by, reason, expires_at))
            row = cur.fetchone()
        conn.commit()
        return row[0] if row else None
    except Exception as e:
        log.warning(f"trading_permission: create_override failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def revoke_override(conn, override_id, revoked_by):
    """Revokes an active override early (before its expires_at, or if it
    has none). Returns True if a row was actually revoked, False if the
    id doesn't exist, is already revoked, or revoked_by is empty."""
    if not revoked_by:
        log.warning("trading_permission: revoke_override requires a non-empty revoked_by")
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE trading_permission_overrides
                SET revoked_at=NOW(), revoked_by=%s
                WHERE id=%s AND revoked_at IS NULL
            """, (revoked_by, override_id))
            updated = cur.rowcount
        conn.commit()
        return updated > 0
    except Exception as e:
        log.warning(f"trading_permission: revoke_override failed for id={override_id}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def evaluate_trading_permission(conn, portfolio_value, p):
    """The one aggregation point for account-level trading permission.
    Returns {"new_entries_allowed": bool, "scope": "account", "reasons":
    [...], "override": {...} | None}. Never touches sells or existing
    positions -- same "brake on NEW risk only" principle circuit_breaker.py's
    own module docstring already establishes; this aggregates that
    principle across multiple conditions rather than introducing a new
    one.

    `reasons` always reflects the raw halt conditions, regardless of any
    active override -- an override changes whether new entries are
    ALLOWED, it never hides why they were blocked. `new_entries_allowed`
    is True if there are no halt reasons, OR if a human has recorded an
    active override (get_active_override()) -- callers that only check
    `new_entries_allowed` (the pre-existing contract) get transparently
    correct behavior either way; callers that want to show "why" or
    "overridden by whom" read `reasons`/`override` directly.
    """
    reasons = []

    hwm = current_high_water_mark(conn)
    drawdown_pct = drawdown_pct_of(portfolio_value, hwm) if portfolio_value else 0.0
    if is_breached(drawdown_pct, p["circuit_breaker_drawdown_pct"]):
        reasons.append("portfolio_drawdown_limit")

    streak = current_loss_streak(conn)
    if streak >= p["loss_streak_limit"]:
        reasons.append("loss_streak_limit")

    override = get_active_override(conn)

    return {
        "new_entries_allowed": len(reasons) == 0 or override is not None,
        "scope": "account",
        "reasons": reasons,
        "override": override,
    }

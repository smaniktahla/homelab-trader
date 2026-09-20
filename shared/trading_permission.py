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


def current_loss_streak(conn, window_days=0):
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

    window_days > 0 (signal_params.loss_streak_window_days, the "cooldown
    window") only counts lifecycles that CLOSED within the last that-many
    days, so an old losing run ages out and the pause lifts on its own
    instead of persisting until a win -- which new entries being blocked
    made unreachable except via a manual override. 0 disables the window
    (the original, unbounded behavior).

    Explicit tuple cursor regardless of the caller's connection default,
    same reasoning as every other shared module's DB functions in this
    codebase."""
    window_days = int(window_days or 0)   # signal_params values arrive as floats; make_interval needs an int
    streak = 0
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute("""
            SELECT pl.net_pnl FROM position_lifecycles pl
            WHERE pl.status='closed' AND pl.net_pnl IS NOT NULL
              AND (%s <= 0 OR pl.closed_at > NOW() - make_interval(days => %s))
              AND NOT EXISTS (
                  SELECT 1 FROM position_trades pt
                  JOIN trades t ON t.id = pt.trade_id
                  WHERE pt.position_lifecycle_id = pl.id
                    AND pt.role = 'exit'
                    AND t.counts_toward_loss_streak = FALSE
              )
            ORDER BY pl.closed_at DESC
        """, (window_days, window_days))
        for (net_pnl,) in cur.fetchall():
            if float(net_pnl) <= 0:
                streak += 1
            else:
                break
    return streak


def current_breadth_down_streak(conn, days, min_decline_pct=0.0):
    """Breadth of a down streak across what is currently HELD: how many open
    positions (position_lifecycles status='open', one per symbol) have
    closed lower than the prior daily close on each of the last `days`
    daily bars in a row AND fallen at least `min_decline_pct` (a fraction,
    e.g. 0.02) cumulatively over that run -- without a size floor, three
    consecutive lower closes of a few basis points counts as a "streak" and
    the rule trips on noise. Mark-to-market and forward-looking, unlike
    current_loss_streak()'s realized closed trades -- it clears by itself
    as soon as holdings stop making consecutive lower closes, with no
    win required. A symbol without days+1 daily bars can't be in a streak.

    Returns {"in_streak": int, "held": int, "symbols": [...]}.
    Same explicit tuple cursor convention as the rest of this module."""
    days = int(days)
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute("SELECT DISTINCT symbol FROM position_lifecycles WHERE status='open'")
        held = [r[0] for r in cur.fetchall()]
        in_streak = []
        for sym in held:
            cur.execute("SELECT close FROM price_history WHERE symbol=%s AND close IS NOT NULL "
                        "ORDER BY ts DESC LIMIT %s", (sym, days + 1))
            closes = [float(r[0]) for r in cur.fetchall()]  # newest first
            if (days >= 1 and len(closes) == days + 1 and all(closes[i] < closes[i + 1] for i in range(days))
                    and (closes[days] - closes[0]) / closes[days] >= min_decline_pct):
                in_streak.append(sym)
    return {"in_streak": len(in_streak), "held": len(held), "symbols": sorted(in_streak)}


def breadth_breached(breadth, pct, min_positions):
    """True when at least `pct` of held positions AND at least
    `min_positions` of them are in the down streak. The count floor exists
    because with a handful of holdings a pure percentage degenerates to
    "one stock tripped the whole account" (1 of 5 = 20%)."""
    if not breadth["held"]:
        return False
    return (breadth["in_streak"] >= max(1, int(min_positions))
            and breadth["in_streak"] / breadth["held"] >= pct)


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

    streak = current_loss_streak(conn, p.get("loss_streak_window_days", 0))
    if streak >= p["loss_streak_limit"]:
        reasons.append("loss_streak_limit")

    breadth = None
    if p.get("breadth_streak_enabled", 0):
        breadth = current_breadth_down_streak(conn, p.get("breadth_streak_days", 3),
                                              p.get("breadth_streak_min_decline_pct", 0.0))
        if breadth_breached(breadth, p.get("breadth_streak_pct", 0.20), p.get("breadth_streak_min_positions", 2)):
            reasons.append("breadth_down_streak")

    override = get_active_override(conn)

    result = {
        "new_entries_allowed": len(reasons) == 0 or override is not None,
        "scope": "account",
        "reasons": reasons,
        "override": override,
    }
    if breadth is not None:   # only when the breadth trigger ran, so the result shape is unchanged when it is off
        result["breadth"] = breadth
    return result

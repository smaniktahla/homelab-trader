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
    treats 0 as not-a-win). Explicit tuple cursor regardless of the
    caller's connection default, same reasoning as every other shared
    module's DB functions in this codebase."""
    streak = 0
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute("""
            SELECT net_pnl FROM position_lifecycles
            WHERE status='closed' AND net_pnl IS NOT NULL
            ORDER BY closed_at DESC
        """)
        for (net_pnl,) in cur.fetchall():
            if float(net_pnl) <= 0:
                streak += 1
            else:
                break
    return streak


def evaluate_trading_permission(conn, portfolio_value, p):
    """The one aggregation point for account-level trading permission.
    Returns {"new_entries_allowed": bool, "scope": "account", "reasons": [...]}.
    Never touches sells or existing positions -- same "brake on NEW risk
    only" principle circuit_breaker.py's own module docstring already
    establishes; this aggregates that principle across multiple
    conditions rather than introducing a new one.
    """
    reasons = []

    hwm = current_high_water_mark(conn)
    drawdown_pct = drawdown_pct_of(portfolio_value, hwm) if portfolio_value else 0.0
    if is_breached(drawdown_pct, p["circuit_breaker_drawdown_pct"]):
        reasons.append("portfolio_drawdown_limit")

    streak = current_loss_streak(conn)
    if streak >= p["loss_streak_limit"]:
        reasons.append("loss_streak_limit")

    return {
        "new_entries_allowed": len(reasons) == 0,
        "scope": "account",
        "reasons": reasons,
    }

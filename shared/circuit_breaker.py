"""
Portfolio Circuit Breaker: PRD v1.1 #4. Tracks the portfolio's all-time
high-water mark (since tracking began) each cycle. If drawdown from that
peak exceeds a configurable threshold, new BUY proposals are paused while
SELL proposals continue unaffected. Never liquidates positions
automatically — this is purely a brake on adding new risk during a bad
drawdown. Auto-resumes on its own once portfolio_value recovers above the
threshold, since the check is recomputed live every cycle rather than
latched into a stored on/off state.
"""

import logging

import psycopg2.extensions

log = logging.getLogger(__name__)


def drawdown_pct_of(portfolio_value, hwm):
    """Pure. (high_water_mark - portfolio_value) / high_water_mark, or 0.0
    if hwm is falsy -- extracted so record_snapshot_and_check() below and
    shared/trading_permission.py compute this identically instead of each
    hand-copying the formula (this file's own is_breached() had two
    independent implementations of exactly this before Risk Engine PR 3:
    this function and rule_adherence.check_gates()'s inline copy)."""
    return (hwm - portfolio_value) / hwm if hwm else 0.0


def is_breached(drawdown_pct, drawdown_threshold):
    """Pure. The single predicate every drawdown-gated check in this
    codebase should call instead of hand-copying `>=` -- see
    drawdown_pct_of() above for the other half of the formula this
    replaces two independent copies of."""
    return drawdown_pct >= drawdown_threshold


def drawdown_size_multiplier(drawdown_pct, drawdown_threshold, floor=0.5):
    """Pure. Linear taper from 1.0 (no drawdown) down to `floor` as
    drawdown_pct approaches drawdown_threshold -- risk_engine.py's
    per-trade risk budget shrinks smoothly on the way toward the circuit
    breaker's hard stop, rather than jumping straight from full size to
    a total halt exactly at the threshold. is_breached() (the hard stop)
    is a SEPARATE, harder line: once actually breached, trading_permission
    blocks new entries outright regardless of this multiplier -- this
    function only tapers sizing on approach, it never itself blocks
    anything. Clamped to [floor, 1.0] so a drawdown beyond the threshold
    (still possible for one cycle before the hard stop is observed
    elsewhere) doesn't produce a negative or >1.0 multiplier."""
    if drawdown_threshold <= 0 or drawdown_pct <= 0:
        return 1.0
    taper = 1.0 - (1.0 - floor) * (drawdown_pct / drawdown_threshold)
    return max(floor, min(1.0, taper))


def record_snapshot_and_check(conn, portfolio_value, drawdown_threshold):
    """Record a portfolio_snapshots row and return
    (breaker_active, high_water_mark, drawdown_pct). Explicit tuple cursor
    regardless of the caller's connection default -- see
    current_high_water_mark()'s own docstring below for why this matters
    now that this module has a second caller path."""
    if not portfolio_value:
        return False, None, None

    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute("SELECT MAX(high_water_mark) FROM portfolio_snapshots")
        row = cur.fetchone()
        prior_hwm = float(row[0]) if row and row[0] is not None else 0.0

    hwm = max(prior_hwm, portfolio_value)
    drawdown_pct = drawdown_pct_of(portfolio_value, hwm)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO portfolio_snapshots (portfolio_value, high_water_mark, drawdown_pct)
            VALUES (%s, %s, %s)
        """, (portfolio_value, hwm, drawdown_pct))
    conn.commit()

    breaker_active = is_breached(drawdown_pct, drawdown_threshold)
    if breaker_active:
        log.warning(
            f"Circuit breaker ACTIVE: drawdown {drawdown_pct*100:.1f}% from "
            f"high-water mark ${hwm:,.2f} (threshold {drawdown_threshold*100:.0f}%) "
            f"— new BUY proposals paused, sells continue"
        )
    return breaker_active, hwm, drawdown_pct


def current_high_water_mark(conn):
    """Read-only peek at the all-time high-water mark -- unlike
    record_snapshot_and_check() above, never inserts a portfolio_snapshots
    row. Platform Improvements PR C's rule_adherence checks call this
    instead of the function above specifically to avoid polluting the
    once-per-ingest-cycle snapshot cadence that drawdown-duration reporting
    and portfolio-value charting both depend on -- a manual trade or
    proposal approval can happen at any time, not just once an hour.

    Explicit tuple cursor regardless of the caller's connection default --
    this is called from api/main.py's dict-cursor connections (via
    shared/rule_adherence.py), and row[0] access below assumes
    tuple/positional indexing."""
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute("SELECT MAX(high_water_mark) FROM portfolio_snapshots")
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0

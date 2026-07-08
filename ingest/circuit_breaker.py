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

log = logging.getLogger(__name__)


def record_snapshot_and_check(conn, portfolio_value, drawdown_threshold):
    """Record a portfolio_snapshots row and return
    (breaker_active, high_water_mark, drawdown_pct)."""
    if not portfolio_value:
        return False, None, None

    with conn.cursor() as cur:
        cur.execute("SELECT MAX(high_water_mark) FROM portfolio_snapshots")
        row = cur.fetchone()
        prior_hwm = float(row[0]) if row and row[0] is not None else 0.0

    hwm = max(prior_hwm, portfolio_value)
    drawdown_pct = (hwm - portfolio_value) / hwm if hwm else 0.0

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO portfolio_snapshots (portfolio_value, high_water_mark, drawdown_pct)
            VALUES (%s, %s, %s)
        """, (portfolio_value, hwm, drawdown_pct))
    conn.commit()

    breaker_active = drawdown_pct >= drawdown_threshold
    if breaker_active:
        log.warning(
            f"Circuit breaker ACTIVE: drawdown {drawdown_pct*100:.1f}% from "
            f"high-water mark ${hwm:,.2f} (threshold {drawdown_threshold*100:.0f}%) "
            f"— new BUY proposals paused, sells continue"
        )
    return breaker_active, hwm, drawdown_pct

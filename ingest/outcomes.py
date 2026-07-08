"""
Signal outcome backfill: PRD v1.1 #1 (Signal Outcome Tracking).

Two jobs, both idempotent and safe to run every ingest cycle:
  1. Sync approval_status on signal_outcomes from trade_proposals.decision,
     marking proposals that sat undecided too long as 'ignored'.
  2. Backfill forward_return_1d/5d/10d/20d and MAE/MFE from price_history
     once enough trading days have accumulated since the signal.
"""

import logging

log = logging.getLogger(__name__)

IGNORED_AFTER_DAYS = 5      # undecided proposals older than this count as "ignored"
MAX_ROWS_PER_CYCLE = 300    # bound work per ingest cycle
FORWARD_OFFSETS = (("forward_return_1d", 1), ("forward_return_5d", 5),
                    ("forward_return_10d", 10), ("forward_return_20d", 20))


def _forward_returns(conn, symbol, generated_at, price_at_signal):
    """Compute forward returns + MAE/MFE from price_history rows on/after generated_at.
    Row 0 is the anchor (signal-day close); offsets index forward from there."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT close, high, low FROM price_history
            WHERE symbol=%s AND ts >= %s
            ORDER BY ts ASC LIMIT 21
        """, (symbol, generated_at))
        rows = cur.fetchall()

    if len(rows) < 2:
        return {}

    closes = [float(r[0]) for r in rows]
    highs = [float(r[1]) for r in rows]
    lows = [float(r[2]) for r in rows]

    result = {}
    for label, offset in FORWARD_OFFSETS:
        if len(closes) > offset:
            result[label] = (closes[offset] - price_at_signal) / price_at_signal * 100

    window_highs = highs[1:21]
    window_lows = lows[1:21]
    if window_highs:
        result["mfe"] = (max(window_highs) - price_at_signal) / price_at_signal * 100
    if window_lows:
        result["mae"] = (min(window_lows) - price_at_signal) / price_at_signal * 100

    return result


def _sync_approval_status(conn):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE signal_outcomes so
            SET approval_status = CASE
                    WHEN tp.decision = 'approved' THEN 'approved'
                    WHEN tp.decision = 'rejected' THEN 'rejected'
                    WHEN tp.decision IS NULL AND so.generated_at < NOW() - INTERVAL '%s days' THEN 'ignored'
                    ELSE 'pending'
                END,
                rejection_reason = tp.rejection_reason
            FROM trade_proposals tp
            WHERE so.proposal_id = tp.id
              AND so.proposal_status = 'proposed'
              AND so.approval_status IS DISTINCT FROM (
                  CASE WHEN tp.decision = 'approved' THEN 'approved'
                       WHEN tp.decision = 'rejected' THEN 'rejected'
                       WHEN tp.decision IS NULL AND so.generated_at < NOW() - INTERVAL '%s days' THEN 'ignored'
                       ELSE 'pending' END)
        """, (IGNORED_AFTER_DAYS, IGNORED_AFTER_DAYS))
        n = cur.rowcount
    conn.commit()
    if n:
        log.info(f"Signal outcomes: synced approval_status for {n} row(s)")


def _backfill_forward_returns(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, symbol, generated_at, price_at_signal
            FROM signal_outcomes
            WHERE forward_return_20d IS NULL
              AND price_at_signal IS NOT NULL
              AND generated_at < NOW() - INTERVAL '1 hour'
            ORDER BY generated_at ASC
            LIMIT %s
        """, (MAX_ROWS_PER_CYCLE,))
        rows = cur.fetchall()

    updated = 0
    for outcome_id, symbol, generated_at, price_at_signal in rows:
        try:
            vals = _forward_returns(conn, symbol, generated_at, float(price_at_signal))
        except Exception as e:
            log.warning(f"Signal outcomes: forward-return calc failed for {symbol} (id={outcome_id}): {e}")
            continue
        if not vals:
            continue
        set_clause = ", ".join(f"{k}=%s" for k in vals)
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE signal_outcomes SET {set_clause}, outcome_updated_at=NOW()
                WHERE id=%s
            """, (*vals.values(), outcome_id))
        conn.commit()
        updated += 1

    if updated:
        log.info(f"Signal outcomes: backfilled forward returns for {updated} row(s)")


def update_signal_outcomes(conn):
    """Main entry point, called once per ingest cycle."""
    try:
        _sync_approval_status(conn)
    except Exception as e:
        log.warning(f"Signal outcomes: approval_status sync failed: {e}")
    try:
        _backfill_forward_returns(conn)
    except Exception as e:
        log.warning(f"Signal outcomes: forward-return backfill failed: {e}")

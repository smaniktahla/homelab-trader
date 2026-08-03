"""
Idempotent builder for position_lifecycles/position_trades -- Platform
Improvements PR A. Mirrors the price_history -> signals -> signal_outcomes
derivation pattern already established in this codebase: trades stays
append-only and is the only source of truth; this module recomputes the
derived lifecycle tables from it every cycle, safe to re-run any number of
times (truncate-and-rebuild, not incremental patching).

Only fetches price bars for symbols that actually have trades, bounded to
each symbol's own first trade -> now, rather than pulling every symbol's
full history -- position_lifecycles' MAE/MFE walk only needs coverage for
periods a real lifecycle was actually open.
"""

import logging

from position_lifecycles import match_lifecycles

log = logging.getLogger(__name__)


def _fetch_trade_rows(conn):
    """Full ledger, filtered to filled, globally ordered -- same convention
    shared/round_trips.py's callers already use. Joins trade_proposals for
    the planned_* fields (only ever populated on buy proposals -- see
    shared/signals.py); trades.initial_stop_price is already the
    fill-time-immutable copy, no join needed for that one."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT t.id, t.symbol, t.side, t.qty, t.price, t.cost, t.traded_at,
                   t.thesis_id, t.initial_stop_price,
                   tp.planned_entry_price, tp.planned_initial_stop_price,
                   tp.planned_risk_per_share, tp.planned_risk_dollars
            FROM trades t
            LEFT JOIN trade_proposals tp ON tp.id = t.proposal_id
            WHERE t.status = 'filled'
            ORDER BY t.traded_at ASC, t.id ASC
        """)
        return cur.fetchall()


def _fetch_price_bars(conn, symbols_with_earliest_trade):
    """{symbol: [{"ts","high","low","resolution"}, ...]} ascending by ts.
    Prefers price_history_hourly where it has coverage; falls back to daily
    price_history bars tagged 'daily_approximation' for the same window --
    match_lifecycles' excursion walk records which was actually used per
    lifecycle via resolutions_used, so a lifecycle that only ever saw daily
    bars is labeled accordingly rather than silently claiming hourly
    precision it didn't have."""
    bars_by_symbol = {}
    with conn.cursor() as cur:
        for symbol, earliest in symbols_with_earliest_trade.items():
            cur.execute("""
                SELECT ts, high, low FROM price_history_hourly
                WHERE symbol=%s AND ts >= %s
                ORDER BY ts ASC
            """, (symbol, earliest))
            hourly = [{"ts": r[0], "high": r[1], "low": r[2], "resolution": "hourly"} for r in cur.fetchall()]

            cur.execute("""
                SELECT ts, high, low FROM price_history
                WHERE symbol=%s AND ts >= %s
                ORDER BY ts ASC
            """, (symbol, earliest))
            daily = [{"ts": r[0], "high": r[1], "low": r[2], "resolution": "daily"} for r in cur.fetchall()]

            bars = sorted(hourly + daily, key=lambda b: b["ts"])
            if bars:
                bars_by_symbol[symbol] = bars
    return bars_by_symbol


def _rebuild_tables(conn, lifecycles_by_symbol):
    """Truncate-and-rebuild inside one transaction -- never a partial
    write visible to readers mid-rebuild."""
    with conn.cursor() as cur:
        cur.execute("TRUNCATE position_trades, position_lifecycles RESTART IDENTITY")

        for symbol, data in lifecycles_by_symbol.items():
            for lc in data["lifecycles"]:
                cur.execute("""
                    INSERT INTO position_lifecycles (
                        symbol, thesis_id, status, opened_at, closed_at, qty,
                        planned_entry_price, planned_initial_stop_price,
                        planned_risk_per_share, planned_risk_dollars,
                        initial_stop_price, actual_initial_risk_per_share,
                        actual_initial_risk_dollars, entry_notional, exit_notional,
                        total_cost, gross_pnl, net_pnl, realized_r,
                        mae_price, mfe_price, mae_r, mfe_r,
                        excursion_resolution, data_quality_flags
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    RETURNING id
                """, (
                    lc.symbol, lc.thesis_id, lc.status, lc.opened_at, lc.closed_at, lc.qty,
                    lc.planned_entry_price, lc.planned_initial_stop_price,
                    lc.planned_risk_per_share, lc.planned_risk_dollars,
                    lc.initial_stop_price, lc.actual_initial_risk_per_share,
                    lc.actual_initial_risk_dollars, lc.entry_notional, lc.exit_notional,
                    lc.total_cost, lc.gross_pnl, lc.net_pnl, lc.realized_r,
                    lc.mae_price, lc.mfe_price, lc.mae_r, lc.mfe_r,
                    lc.excursion_resolution, lc.data_quality_flags,
                ))
                lifecycle_id = cur.fetchone()[0]

                for pt in lc.position_trades:
                    cur.execute("""
                        INSERT INTO position_trades (position_lifecycle_id, trade_id, role, qty_allocated)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (position_lifecycle_id, trade_id) DO UPDATE
                            SET qty_allocated = position_trades.qty_allocated + EXCLUDED.qty_allocated
                    """, (lifecycle_id, pt["trade_id"], pt["role"], pt["qty_allocated"]))
    conn.commit()


def build_position_lifecycles(conn):
    """Main entry point, called once per ingest cycle -- mirrors
    update_signal_outcomes' placement and fail-open contract. A failure
    here must never affect the proposal/execution path; it only means
    position_lifecycles is stale until the next successful cycle."""
    trade_rows = _fetch_trade_rows(conn)
    if not trade_rows:
        return

    earliest_by_symbol = {}
    for t in trade_rows:
        earliest_by_symbol.setdefault(t["symbol"], t["traded_at"])

    price_bars = _fetch_price_bars(conn, earliest_by_symbol)
    lifecycles_by_symbol = match_lifecycles(trade_rows, price_bars_by_symbol=price_bars)
    _rebuild_tables(conn, lifecycles_by_symbol)

    n_lifecycles = sum(len(d["lifecycles"]) for d in lifecycles_by_symbol.values())
    n_unmatched = sum(1 for d in lifecycles_by_symbol.values() if d["unmatched_sell_qty"] > 1e-6)
    log.info(f"Position lifecycles: rebuilt {n_lifecycles} lifecycle(s) across {len(lifecycles_by_symbol)} symbol(s)"
              + (f", {n_unmatched} with unmatched sell qty" if n_unmatched else ""))

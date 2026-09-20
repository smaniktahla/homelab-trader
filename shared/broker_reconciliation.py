"""
Pure diff between the trades-ledger-derived open position_lifecycles and
the broker's real held set (Alpaca GET /v2/positions).

position_lifecycles is rebuilt from the trades ledger every cycle, so it
can only be as right as the ledger is. Fills that reach the broker without
passing through this app's own POST /api/trade / PATCH /api/proposals/{id}
paths (a resting stop firing while ingest was down past
reconcile_broker_stop_fills()'s lookback window, a sell placed directly in
the Alpaca UI, a buy whose trades row never got written) leave a lifecycle
open that the broker has already closed, or missing entirely for a
position the broker holds. Consumers such as
trading_permission.current_breadth_down_streak() treat open lifecycles as
the held set, so the drift silently skews them.

No DB or network access here -- ingest.reconcile_lifecycles_with_broker()
fetches both sides and passes them in.
"""

_QTY_TOLERANCE = 1e-6  # float qty noise / fractional-share rounding, not a real share tolerance


def diff_open_lifecycles_vs_broker(ledger_qty, broker_qty, tolerance=_QTY_TOLERANCE):
    """ledger_qty / broker_qty: {symbol: qty} for open lifecycles and broker
    positions respectively (long-only -- see shared/position_lifecycles.py).

    Returns {"ledger_only": {sym: ledger_qty}, "broker_only": {sym: broker_qty},
    "qty_mismatch": {sym: (ledger_qty, broker_qty)}}; all three empty means
    the ledger agrees with the broker."""
    ledger_only, broker_only, qty_mismatch = {}, {}, {}
    for sym, lq in ledger_qty.items():
        if lq <= tolerance:
            continue
        bq = broker_qty.get(sym, 0.0)
        if bq <= tolerance:
            ledger_only[sym] = lq
        elif abs(lq - bq) > tolerance:
            qty_mismatch[sym] = (lq, bq)
    for sym, bq in broker_qty.items():
        if bq > tolerance and ledger_qty.get(sym, 0.0) <= tolerance:
            broker_only[sym] = bq
    return {"ledger_only": ledger_only, "broker_only": broker_only, "qty_mismatch": qty_mismatch}


def is_clean(diff):
    return not (diff["ledger_only"] or diff["broker_only"] or diff["qty_mismatch"])

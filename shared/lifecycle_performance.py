"""
Symbol Performance Summary computation from the materialized
position_lifecycles/position_trades tables (Platform Improvements PR A).

Platform Improvements PR A.1: the deliberate, separate follow-up PR A
itself called out for repointing /api/symbol-performance and its
/round-trips sibling at true FIFO lifecycle data, replacing the removed
shared/round_trips.py average-cost reconstruction. The public shape here
(symbol_summary's dict, round_trips_detail's dict) is kept identical to
what round_trips.py used to produce, key-for-key, so
api/templates/symbol.html needed no rewrite -- only a small
methodology-label addition.

The numbers here WILL DIFFER from round_trips.py's old numbers for any
symbol ever pyramided into more than once before fully exiting -- FIFO lot
matching and average-cost blending are different, both correct,
conventions (see shared/position_lifecycles.py's own module docstring).
Not a bug to reconcile away.

No DB access here -- callers (api/main.py) fetch position_lifecycles rows
(already joined against position_trades to get each lifecycle's
entry_trade_ids/exit_trade_ids) plus this symbol's
position_lifecycle_symbol_status.unmatched_sell_qty, and pass them in --
matching the pure-function style already established by
shared/position_lifecycles.py and shared/signal_components.py.
"""

from statistics import mean, median

METHODOLOGY = "position_lifecycle_fifo"


def _net_pnl(lc):
    return float(lc["net_pnl"])


def _return_pct(lc):
    """net_pnl / entry_notional * 100 -- same definition round_trips.py's
    RoundTrip.return_pct used, now computed here since position_lifecycles
    rows don't carry it precomputed. entry_notional is never actually 0
    for a real lifecycle (every lifecycle starts with at least one buy),
    but guarded anyway rather than assuming that invariant holds forever."""
    entry_notional = float(lc["entry_notional"]) if lc["entry_notional"] is not None else 0.0
    if not entry_notional:
        return None
    return round(_net_pnl(lc) / entry_notional * 100, 2)


def _holding_days(lc):
    if not lc["opened_at"] or not lc["closed_at"]:
        return None
    return (lc["closed_at"] - lc["opened_at"]).total_seconds() / 86400


def _trip_summary(lc):
    if lc is None:
        return None
    return {
        "opened_at": lc["opened_at"].isoformat() if lc["opened_at"] else None,
        "closed_at": lc["closed_at"].isoformat() if lc["closed_at"] else None,
        "net_pnl": round(_net_pnl(lc), 2),
        "return_pct": _return_pct(lc),
        "qty": float(lc["qty"]),
    }


def portfolio_totals(lifecycles_by_symbol):
    """Across ALL symbols' CLOSED lifecycles only -- an open lifecycle's
    realized-to-date P&L never enters this, matching round_trips.py's
    prior convention exactly. gross_profit_total sums net_pnl over winning
    lifecycles only; gross_loss_total over losing lifecycles only (a
    negative number, or 0.0 if there are none)."""
    gross_profit_total = 0.0
    gross_loss_total = 0.0
    for rows in lifecycles_by_symbol.values():
        for lc in rows:
            net_pnl = _net_pnl(lc)
            if net_pnl > 0:
                gross_profit_total += net_pnl
            elif net_pnl < 0:
                gross_loss_total += net_pnl
    return {
        "gross_profit_total": round(gross_profit_total, 2),
        "gross_loss_total": round(gross_loss_total, 2),
        "net_pnl_total": round(gross_profit_total + gross_loss_total, 2),
    }


def symbol_contribution(symbol, lifecycles_by_symbol, totals):
    """Same two contribution axes round_trips.py exposed -- see that
    module's own (now-removed) docstring for why contribution_to_net_pnl_pct
    can legitimately exceed 100% or go negative, and why both raw dollar
    figures are included alongside their percentages."""
    rows = lifecycles_by_symbol.get(symbol, [])
    symbol_gross_profit = sum(n for n in (_net_pnl(lc) for lc in rows) if n > 0)
    symbol_net_pnl = sum(_net_pnl(lc) for lc in rows)

    contribution_to_gross_gains_pct = (
        round(symbol_gross_profit / totals["gross_profit_total"] * 100, 2)
        if totals["gross_profit_total"] > 0 else None
    )
    contribution_to_net_pnl_pct = (
        round(symbol_net_pnl / totals["net_pnl_total"] * 100, 2)
        if totals["net_pnl_total"] != 0 else None
    )
    return {
        "contribution_to_gross_gains_pct": contribution_to_gross_gains_pct,
        "contribution_to_net_pnl_pct": contribution_to_net_pnl_pct,
        "symbol_gross_profit": round(symbol_gross_profit, 2),
        "portfolio_gross_profit_total": totals["gross_profit_total"],
        "symbol_net_pnl": round(symbol_net_pnl, 2),
        "portfolio_net_pnl_total": totals["net_pnl_total"],
    }


def _reconciliation_status(open_lifecycle, live_position):
    """Direct port of round_trips.py's equivalent -- ledger qty now sourced
    from the open lifecycle row's own qty field instead of
    OpenEpisode.reconstructed_qty. Exposes ledger-vs-broker disagreement as
    structured data rather than silently picking one source."""
    ledger_status = "open" if open_lifecycle else "flat"
    broker_status = "open" if live_position else "flat"

    if ledger_status == "flat" and broker_status == "flat":
        status, detail = "match", None
    elif ledger_status == "open" and broker_status == "open":
        ledger_qty = float(open_lifecycle["qty"])
        qty_diff = abs(ledger_qty - float(live_position["qty"]))
        if qty_diff > 1e-4:
            status = "qty_mismatch"
            detail = (
                f"Local ledger reconstruction shows {ledger_qty} shares open, "
                f"but the broker reports {live_position['qty']} — some fills (e.g. a pre-ledger "
                f"broker holding, or a trade placed outside this app) aren't reflected in the "
                f"round-trip history below."
            )
        else:
            status, detail = "match", None
    elif ledger_status == "open" and broker_status == "flat":
        status = "ledger_only"
        detail = (
            "Local ledger reconstruction believes a position is still open, but the broker reports "
            "none — likely a manual close outside this app."
        )
    else:  # ledger_status == "flat" and broker_status == "open"
        status = "broker_only"
        detail = (
            "The broker reports an open position, but the local ledger reconstruction sees none — "
            "likely a pre-ledger broker holding with no locally-recorded buy at all."
        )

    return {"ledger_status": ledger_status, "broker_status": broker_status, "status": status, "detail": detail}


def open_position_summary(open_lifecycle, live_position):
    """open_lifecycle's gross_pnl/net_pnl are already realized-to-date
    (0.0 baseline, per shared/position_lifecycles.py's own dataclass
    docstring) -- unlike round_trips.py's OpenEpisode, no extra proration
    is needed here to get partial_realized_pnl."""
    if not open_lifecycle and not live_position:
        return None
    return {
        "opened_at": open_lifecycle["opened_at"].isoformat() if open_lifecycle and open_lifecycle["opened_at"] else None,
        "qty": float(live_position["qty"]) if live_position else (float(open_lifecycle["qty"]) if open_lifecycle else None),
        "avg_entry_price": float(live_position["avg_entry_price"]) if live_position else None,
        "current_price": float(live_position["current_price"]) if live_position else None,
        "unrealized_pnl": float(live_position["unrealized_pl"]) if live_position else None,
        "partial_realized_pnl": round(_net_pnl(open_lifecycle), 2) if open_lifecycle else None,
        "entry_notional": (
            round(float(open_lifecycle["entry_notional"]), 2)
            if open_lifecycle and open_lifecycle["entry_notional"] is not None else None
        ),
    }


def symbol_summary(symbol, lifecycles_by_symbol, totals, unmatched_sell_qty, open_lifecycle=None, live_position=None):
    """Builds the full Symbol Performance Summary payload for one symbol --
    same key set round_trips.symbol_summary used to produce.

    Completed-lifecycle statistics (win rate, avg/median return, best/worst,
    avg holding period) come exclusively from this symbol's closed
    lifecycles -- open_lifecycle and live_position never enter those
    specific figures, by construction, same discipline round_trips.py used.
    """
    rows = lifecycles_by_symbol.get(symbol, [])
    n = len(rows)

    wins = [lc for lc in rows if _net_pnl(lc) > 0]
    losses = [lc for lc in rows if _net_pnl(lc) < 0]
    breakeven = [lc for lc in rows if _net_pnl(lc) == 0]

    returns = [r for r in (_return_pct(lc) for lc in rows) if r is not None]
    best = max(rows, key=_net_pnl) if rows else None
    worst = min(rows, key=_net_pnl) if rows else None
    holding_days_list = [d for d in (_holding_days(lc) for lc in rows) if d is not None]

    realized_pnl_total = sum(_net_pnl(lc) for lc in rows)
    if open_lifecycle is not None:
        realized_pnl_total += _net_pnl(open_lifecycle)

    unrealized_pnl = float(live_position["unrealized_pl"]) if live_position else 0.0

    # "Capital deployed" = sum of every entry lot's notional across this
    # symbol's whole lifecycle history (closed + the open one, if any) --
    # NOT peak concurrent capital, NOT average capital held over time. Same
    # definition and same "capital_deployed_methodology" label round_trips.py
    # used -- only the data source changed.
    capital_deployed = sum(float(lc["entry_notional"] or 0) for lc in rows)
    if open_lifecycle is not None:
        capital_deployed += float(open_lifecycle["entry_notional"] or 0)

    contribution = symbol_contribution(symbol, lifecycles_by_symbol, totals)
    reconciliation = _reconciliation_status(open_lifecycle, live_position)

    methodology_status = "partial" if unmatched_sell_qty > 1e-6 else "complete"

    return {
        "symbol": symbol,
        "methodology": METHODOLOGY,
        "methodology_status": methodology_status,
        "unmatched_sell_qty": unmatched_sell_qty,
        "completed_round_trips": n,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else None,
        "avg_return_pct": round(mean(returns), 2) if returns else None,
        "median_return_pct": round(median(returns), 2) if returns else None,
        "best_trip": _trip_summary(best),
        "worst_trip": _trip_summary(worst),
        "avg_holding_days": round(mean(holding_days_list), 1) if holding_days_list else None,
        "capital_deployed": round(capital_deployed, 2),
        "capital_deployed_methodology": "sum_of_entry_notionals",
        "realized_pnl": round(realized_pnl_total, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_pnl": round(realized_pnl_total + unrealized_pnl, 2),
        "open_position": open_position_summary(open_lifecycle, live_position),
        "reconciliation": reconciliation,
        **contribution,
    }


def round_trips_detail(symbol, lifecycles_by_symbol, open_lifecycle=None, live_position=None):
    """Full closed-lifecycle history for the table below the summary --
    oldest-first, plus the open lifecycle (if any) called out separately,
    same shape round_trips.py's get_symbol_round_trips consumer used."""
    rows = lifecycles_by_symbol.get(symbol, [])
    return {
        "symbol": symbol,
        "methodology": METHODOLOGY,
        "round_trips": [
            {
                "status": "closed",
                "entry_trade_ids": lc.get("entry_trade_ids") or [],
                "exit_trade_ids": lc.get("exit_trade_ids") or [],
                "opened_at": lc["opened_at"].isoformat() if lc["opened_at"] else None,
                "closed_at": lc["closed_at"].isoformat() if lc["closed_at"] else None,
                "qty": float(lc["qty"]),
                "entry_notional": float(lc["entry_notional"]) if lc["entry_notional"] is not None else None,
                "net_pnl": round(_net_pnl(lc), 2),
                "return_pct": _return_pct(lc),
                "holding_days": _holding_days(lc),
            }
            for lc in rows
        ],
        "open_position": open_position_summary(open_lifecycle, live_position),
    }

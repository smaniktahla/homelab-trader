"""
Rule-adherence bypass detection -- Platform Improvements PR C.

shared/signals.py's compute_signals() enforces six buy-side gates for the
AUTOMATED pipeline (circuit breaker, max_open_positions, sector cap, buy
cooldown, earnings blackout, position sizing), logging only the FIRST one
that fails via signal_outcomes.block_reason. But POST /api/trade (manual
trades) and PATCH /api/proposals/{id} (proposal approval) enforce NONE of
these -- a human can bypass every one of them with zero record. This
module re-checks all six gates against CURRENT live state at the moment of
a manual trade or approval, purely advisory: it never blocks anything,
only records which gate(s), if any, would currently fail.

Reuses signals.py's own gate-check functions directly (recent_buy_block_reason,
sector_cap_block_reason, load_sector_map, fetch_alpaca_portfolio,
load_params) rather than reimplementing them -- exactly one source of
truth for what each gate means, no drift risk between the automated and
manual/adherence paths. Position sizing gets one small new function here
since the question is shaped differently: "does this already-known qty
violate max_position_pct" vs. calc_buy_qty's "solve for a qty that fits" --
but uses the identical cap formula.

Deliberately does NOT call circuit_breaker.record_snapshot_and_check() --
that inserts a portfolio_snapshots row every call, and this module runs at
unpredictable times (whenever a human trades or approves something), which
would pollute the once-per-ingest-cycle snapshot cadence other reporting
(drawdown-duration, portfolio value charting) depends on. Uses
trading_permission.evaluate_trading_permission() instead (Risk Engine PR
3), which itself only reads via circuit_breaker.current_high_water_mark()'s
read-only peek, same non-polluting principle.

The "trading_permission" gate below (renamed from "circuit_breaker" as of
Risk Engine PR 3) mirrors exactly what compute_signals() now enforces --
account-level drawdown OR loss-streak, aggregated -- not just drawdown
alone, so a human bypassing a loss-streak pause is caught here too, not
silently missed.

Only meaningful going forward -- like position_lifecycles.realized_r,
there is no way to retroactively reconstruct portfolio state at a past
manual trade's exact moment, so trades before this shipped simply have no
adherence record. That's correct and expected, not a gap to backfill.

Unlike compute_signals()'s own gate evaluation (which short-circuits at
the first failing gate, since only one reason is needed to block), every
gate here is evaluated unconditionally -- the point is the full checklist,
not just the first failure.
"""

from earnings import earnings_blackout_reason
from signals import (
    fetch_alpaca_portfolio, load_params, recent_buy_block_reason,
    sector_cap_block_reason, load_sector_map,
)
from trading_permission import evaluate_trading_permission


def _position_sizing_violation(symbol, qty, price, existing_market_value, portfolio_value, p):
    """Does buying qty*price of symbol push it over max_position_pct of the
    portfolio? Same cap formula calc_buy_qty() uses (max_dollars_in_symbol =
    portfolio_value * max_position_pct), but answering a different question:
    calc_buy_qty() solves for a qty that fits; this checks an
    already-known, already-executed qty against the same cap."""
    if not portfolio_value:
        return None
    max_dollars_in_symbol = portfolio_value * p["max_position_pct"]
    projected = (existing_market_value or 0.0) + qty * price
    if projected > max_dollars_in_symbol:
        return (
            f"position_sizing_exceeded:{symbol} "
            f"(${projected:.0f}>{max_dollars_in_symbol:.0f} cap)"
        )
    return None


def check_gates(conn, symbol, side, qty, price):
    """Re-evaluate every gate compute_signals() enforces for buys, against
    CURRENT live Alpaca/DB state, for the given symbol/side/qty/price.
    Returns a list of {"rule": str, "passed": bool, "detail": str|None}
    dicts, one per gate, always in the same order.

    Sell side only gets the trivial "position held" check -- sells are
    intentionally unrestricted by the automated pipeline too, so there is
    nothing else to re-check.
    """
    cash, portfolio_value, positions = fetch_alpaca_portfolio()

    if side == "sell":
        held = symbol in positions and positions[symbol]["qty"] > 0
        return [{
            "rule": "position_held", "passed": held,
            "detail": None if held else "no_position_held",
        }]

    p = load_params(conn)
    results = []

    permission = evaluate_trading_permission(conn, portfolio_value, p)
    results.append({
        "rule": "trading_permission", "passed": permission["new_entries_allowed"],
        "detail": None if permission["new_entries_allowed"] else "trading_permission:" + ",".join(permission["reasons"]),
    })

    open_count = len(positions)
    max_open_ok = open_count < int(p["max_open_positions"])
    results.append({
        "rule": "max_open_positions", "passed": max_open_ok,
        "detail": None if max_open_ok else f"max_open_positions:{open_count}/{int(p['max_open_positions'])}",
    })

    eb_block = earnings_blackout_reason(conn, symbol, p["earnings_blackout_days"])
    results.append({"rule": "earnings_blackout", "passed": eb_block is None, "detail": eb_block})

    cd_block = recent_buy_block_reason(conn, symbol, p["buy_cooldown_days"])
    results.append({"rule": "buy_cooldown", "passed": cd_block is None, "detail": cd_block})

    existing_mv = positions.get(symbol, {}).get("market_value", 0.0)
    sizing_block = _position_sizing_violation(symbol, qty, price, existing_mv, portfolio_value, p)
    results.append({"rule": "position_sizing", "passed": sizing_block is None, "detail": sizing_block})

    sector_map = load_sector_map(conn, {symbol} | set(positions.keys()))
    sector_block = sector_cap_block_reason(symbol, price, qty, sector_map, positions, portfolio_value, p)
    results.append({"rule": "sector_cap", "passed": sector_block is None, "detail": sector_block})

    return results


def any_violation(results):
    return any(not r["passed"] for r in results)

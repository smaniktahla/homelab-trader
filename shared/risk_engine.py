"""
Portfolio risk engine -- the one authoritative source for approved_quantity
and per-trade risk sizing. See docs/risk-engine-architecture-reconciliation.md
for the ownership boundaries this enforces: the strategy proposes a side,
entry, and stop; this module is the only thing that decides how many shares
that becomes.

evaluate_proposal() is pure (no DB, no network) and deliberately reuses the
same allocation-cap and sector-cap formulas shared/signals.py's
calc_buy_qty()/sector_cap_block_reason() already established, composed with
two NEW constraints that didn't exist anywhere before this module: a
per-trade risk budget (dollars at risk, from the proposal's own planned
stop) and a portfolio-wide open-risk cap (summed across currently open
position_lifecycles). This is what both shared/signals.py (live) and
backtest_portfolio_montecarlo.py (historical) call, so live and backtest
sizing can never drift apart -- same reasoning as classify_overall()
(market_regime.py) and calc_buy_qty() itself already established.

Per docs/risk-engine-architecture-reconciliation.md section C.3: this
module takes ZERO regime input. market_regime.py's alloc_modifier already
shrinks what the strategy proposes before this module ever sees it (it's
baked into the requested_qty this module receives) -- applying a second,
independent regime-based reduction here would double-penalize the same
bearish regime, which the reconciliation doc explicitly flags as a pattern
to avoid compounding without an explicit, separately-tested policy.

load_open_risk_dollars() is the one DB-touching function in this module
(same mixed pure-calc/thin-DB-read pattern shared/circuit_breaker.py
already uses) -- live callers use it; the backtest computes its own
equivalent from its simulated ledger instead, since it has no
position_lifecycles table to query.
"""

import math

import psycopg2.extensions

# Policy defaults -- mirrors shared/signals.py's DEFAULTS precedent (also
# overridden at runtime by signal_params, same load_params() call site).
RISK_ENGINE_DEFAULTS = {
    "risk_per_trade_pct": 0.01,           # 1% of portfolio at risk per new position
    "max_portfolio_open_risk_pct": 0.06,  # 6% of portfolio at risk across all open positions combined
}


def load_open_risk_dollars(conn):
    """Sum of actual_initial_risk_dollars across currently OPEN
    position_lifecycles -- the portfolio's total planned dollar risk right
    now, before evaluating a new proposal. NULL risk (lifecycles opened
    before the risk basis existed, or manual trades with no linked
    proposal) contributes 0, not an unknown -- COALESCE, since "we don't
    know this position's risk" must not silently shrink the reported open
    risk total below its real value. Explicit tuple cursor regardless of
    the caller's connection default, same reasoning as every other shared
    module's DB functions in this codebase (ingest.py uses tuple cursors,
    api/main.py uses dict cursors)."""
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute("""
            SELECT COALESCE(SUM(actual_initial_risk_dollars), 0)
            FROM position_lifecycles WHERE status='open'
        """)
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def _reject(detail, reason):
    return {
        "approved_quantity": 0,
        "outcome": "rejected",
        "risk_budget_dollars": None,
        "binding_constraint": reason,
        "constraint_detail": detail,
    }


def evaluate_proposal(symbol, price, requested_qty, planned_initial_stop_price,
                       cash, portfolio_value, positions, sector_map, open_risk_dollars, p):
    """
    Evaluate one BUY sizing request and return a risk decision dict:
      {
        "approved_quantity": int,
        "outcome": "approved" | "reduced" | "rejected",
        "risk_budget_dollars": float | None,
        "binding_constraint": str | None,   # None only when outcome == "approved"
        "constraint_detail": {...},          # every limit checked, not just the binding one
      }

    requested_qty: the strategy's own sized qty (an upper bound -- this
    function never approves more than this, only the same or less).
    planned_initial_stop_price: from trade_proposals; may be None (e.g. a
    manual trade with no linked proposal) -- risk-budget and
    portfolio-open-risk sizing are both skipped when None (recorded in
    constraint_detail as "skipped"), falling back to buying-power/
    allocation/sector sizing only.
    open_risk_dollars: caller-supplied (see load_open_risk_dollars above)
    -- this function has no DB access itself.
    """
    detail = {}

    if not cash or not portfolio_value or cash <= 0 or portfolio_value <= 0 or not price or price <= 0:
        return _reject(detail, "no_portfolio_data")

    # ── Buying power ──────────────────────────────────────────────────────
    max_affordable = math.floor(cash / price)
    detail["buying_power"] = {"cash": cash, "max_affordable_qty": max_affordable}
    if max_affordable < 1:
        return _reject(detail, "insufficient_buying_power")

    # ── Position allocation cap (same formula calc_buy_qty() uses) ─────────
    max_dollars_in_symbol = portfolio_value * p["max_position_pct"]
    existing_market_value = positions.get(symbol, {}).get("market_value", 0.0)
    room = max_dollars_in_symbol - existing_market_value
    alloc_qty = math.floor(room / price) if room > 0 else 0
    detail["position_allocation"] = {
        "max_position_pct": p["max_position_pct"], "room_dollars": room, "qty": alloc_qty,
    }

    risk_per_share = None
    if planned_initial_stop_price is not None and price > planned_initial_stop_price:
        risk_per_share = price - planned_initial_stop_price

    # ── Per-trade risk budget ───────────────────────────────────────────────
    risk_budget_dollars = portfolio_value * p["risk_per_trade_pct"]
    risk_qty = None
    if risk_per_share:
        risk_qty = math.floor(risk_budget_dollars / risk_per_share)
        detail["risk_budget"] = {
            "risk_per_trade_pct": p["risk_per_trade_pct"],
            "risk_budget_dollars": risk_budget_dollars,
            "risk_per_share": risk_per_share, "qty": risk_qty,
        }
    else:
        detail["risk_budget"] = {"skipped": "no_planned_stop_price"}

    # ── Portfolio open-risk cap ──────────────────────────────────────────────
    max_open_risk_dollars = portfolio_value * p["max_portfolio_open_risk_pct"]
    remaining_open_risk = max_open_risk_dollars - (open_risk_dollars or 0.0)
    open_risk_qty = None
    if risk_per_share:
        open_risk_qty = math.floor(remaining_open_risk / risk_per_share) if remaining_open_risk > 0 else 0
        detail["portfolio_open_risk"] = {
            "max_portfolio_open_risk_pct": p["max_portfolio_open_risk_pct"],
            "current_open_risk_dollars": open_risk_dollars or 0.0,
            "remaining_dollars": remaining_open_risk, "qty": open_risk_qty,
        }
    else:
        detail["portfolio_open_risk"] = {"skipped": "no_planned_stop_price"}

    # ── Sector exposure cap (same formula sector_cap_block_reason() uses) ────
    sector = sector_map.get(symbol)
    sector_qty = None
    if sector:
        current_sector_value = sum(
            pos["market_value"] for s, pos in positions.items() if sector_map.get(s) == sector
        )
        sector_room = portfolio_value * p["sector_max_pct"] - current_sector_value
        sector_qty = math.floor(sector_room / price) if sector_room > 0 else 0
        detail["sector_exposure"] = {
            "sector": sector, "sector_max_pct": p["sector_max_pct"],
            "current_sector_value": current_sector_value, "room_dollars": sector_room, "qty": sector_qty,
        }
    else:
        detail["sector_exposure"] = {"skipped": "no_sector_mapped"}

    # ── Combine: approved qty is the tightest of every applicable constraint ──
    candidates = {"requested": requested_qty, "buying_power": max_affordable, "position_allocation": alloc_qty}
    if risk_qty is not None:
        candidates["risk_budget"] = risk_qty
    if open_risk_qty is not None:
        candidates["portfolio_open_risk"] = open_risk_qty
    if sector_qty is not None:
        candidates["sector_exposure"] = sector_qty

    binding_name = min(candidates, key=candidates.get)
    approved_quantity = max(0, int(candidates[binding_name]))

    if approved_quantity < 1:
        return _reject(detail, binding_name)

    outcome = "approved" if approved_quantity >= requested_qty else "reduced"
    return {
        "approved_quantity": approved_quantity,
        "outcome": outcome,
        "risk_budget_dollars": risk_budget_dollars,
        "binding_constraint": None if outcome == "approved" else binding_name,
        "constraint_detail": detail,
    }

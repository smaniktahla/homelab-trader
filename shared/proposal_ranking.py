"""
Portfolio-aware proposal ranking -- presentation/ordering layer only.

Answers a second question the strategy layer doesn't: not "is this a
valid trade" (shared/signals.py::score_signal() already answers that),
but "should this trade get capital right now, given everything else
already owned or pending." Purely additive and read-only: it never
touches signal_score/final_proposal_score, never changes qty, and never
blocks or auto-rejects a proposal -- approve/reject stays 100%
human-driven via the existing PATCH /api/proposals/{id}. Called from
api/main.py::get_proposals() as a post-processing pass over the same
rows that endpoint already returns, so it can't affect
compute_signals()'s insert-time output or trip
tests/test_fixture_equivalence*.py.

Sector clustering here is presented strictly as a mechanical redundancy/
exposure-tracking convenience ("this sector is already represented,
here's the strongest candidate among duplicates") -- NOT as a claim that
clustering or the relative-strength-style scoring modules improve
returns. Whether approving multiple simultaneous same-sector proposals
actually hurts portfolio quality is an open, unanswered research question
(docs/research-todo-sector-clustering.md) with no backtest run yet; this
module must not be described anywhere as having settled that question.

Unlike relative_strength_risk_mode/regime_scoring_enabled/
structure_scoring_enabled (all default OFF because they can change WHAT
gets bought or HOW MUCH), proposal_ranking_enabled defaults ON: this
module only reorders/labels rows for display, the same category as
shared/market_structure.py (always-on, no gate, because it never touches
score/gating/sizing). The flag exists as a display-only kill switch, not
a decision-safety one.
"""

import logging

log = logging.getLogger(__name__)

PROPOSAL_RANKING_DEFAULTS = {
    "proposal_ranking_enabled": 1,
    # Fraction of sector_max_pct at which a sector is treated as "heavily
    # represented" for opportunity-cost/tiering purposes, ahead of actually
    # breaching the hard cap (which sector_cap_block_reason() already
    # enforces at buy-gate time, elsewhere).
    "proposal_ranking_saturation_watch_threshold": 0.75,
}

# Tier 1 = highest priority, 5 = lowest. Named so the cascade in
# _assign_tier() reads as intent, not magic numbers.
TIER_HIGHEST = 1
TIER_GOOD = 2
TIER_REASONABLE = 3
TIER_LOW = 4
TIER_MINIMAL = 5

STARS_BY_TIER = {
    TIER_HIGHEST: "★★★★★",
    TIER_GOOD: "★★★★☆",
    TIER_REASONABLE: "★★★☆☆",
    TIER_LOW: "★★☆☆☆",
    TIER_MINIMAL: "★☆☆☆☆",
}

LABEL_BY_TIER = {
    TIER_HIGHEST: "Highest Priority",
    TIER_GOOD: "Good opportunity",
    TIER_REASONABLE: "Reasonable trade",
    TIER_LOW: "Valid setup, lower priority",
    TIER_MINIMAL: "Little incremental value",
}

ACTION_BY_TIER = {
    TIER_HIGHEST: "Strong Buy",
    TIER_GOOD: "Buy",
    TIER_REASONABLE: "Watch",
    TIER_LOW: "Watch",
    TIER_MINIMAL: "Skip",
}


def load_proposal_ranking_params(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM signal_params")
            rows = cur.fetchall()
        params = dict(PROPOSAL_RANKING_DEFAULTS)
        for row in rows:
            k = row[0] if isinstance(row, (list, tuple)) else row["key"]
            if k not in PROPOSAL_RANKING_DEFAULTS:
                continue
            v = row[1] if isinstance(row, (list, tuple)) else row["value"]
            params[k] = float(v)
        return params
    except Exception as e:
        log.warning(f"Could not load proposal-ranking params, using defaults: {e}")
        return dict(PROPOSAL_RANKING_DEFAULTS)


def _proposal_score(p):
    """Prefer final_proposal_score (regime/structure-adjusted) when present,
    falling back to the base signal_score -- most proposal rows have both,
    but the simpler exit-path INSERTs (stop_loss/thesis_complete/etc.) in
    compute_signals() only ever populate signal_score."""
    if p.get("final_proposal_score") is not None:
        return float(p["final_proposal_score"])
    if p.get("signal_score") is not None:
        return float(p["signal_score"])
    return 0.0


def _build_sector_clusters(buy_proposals, sector_map):
    """Group buy proposals by GICS sector, each group sorted by score desc
    (symbol asc tiebreak, for determinism). A proposal whose symbol has no
    sector mapping becomes its own singleton cluster -- same fail-open
    convention as shared/sector_mapping.py::get_sector_etf -- never forced
    into a false grouping.

    Returns dict[cluster_id -> list[proposal]], insertion order = first
    sector seen (stable across calls given the same input order since dict
    preserves insertion order).
    """
    clusters = {}
    for p in buy_proposals:
        sector = sector_map.get(p["symbol"])
        cluster_id = f"sector:{sector}" if sector else f"symbol:{p['symbol']}"
        clusters.setdefault(cluster_id, []).append(p)
    for cluster_id, members in clusters.items():
        members.sort(key=lambda p: (-_proposal_score(p), p["symbol"]))
    return clusters


def _effective_sector_dollars(sector, positions, pending_orders, sector_map, price_map):
    """Current sector $ exposure, ASSUMING pending orders execute: held
    market_value in the sector, plus notional of pending BUY orders on
    symbols not already reflected as a position, minus notional of pending
    SELL orders on symbols currently held (floored at 0 -- a sell can't
    make sector exposure negative). Closes the gap that
    shared/signals.py::sector_cap_block_reason() only ever sees currently
    FILLED positions, never orders still in flight.
    """
    if not sector:
        return 0.0
    total = sum(
        pos["market_value"] for sym, pos in positions.items()
        if sector_map.get(sym) == sector
    )
    for o in pending_orders:
        if sector_map.get(o["symbol"]) != sector:
            continue
        price = price_map.get(o["symbol"])
        if price is None:
            continue
        notional = float(o["qty"]) * price
        if o["side"] == "buy" and o["symbol"] not in positions:
            total += notional
        elif o["side"] == "sell" and o["symbol"] in positions:
            total = max(0.0, total - notional)
    return total


def _projected_buying_power(buying_power, pending_orders, price_map):
    """Alpaca's own buying_power already reserves cash against open BUY
    orders (a broker-side Reg-T hold at submission time) -- subtracting
    pending buys again here would double-count the same hold. Pending SELL
    orders' proceeds, by contrast, are NOT reflected in buying_power until
    the shares actually leave the position, so those get added back. This
    mirrors the double-counting caution shared/risk_engine.py documents
    for alloc_modifier vs. its own sizing math -- same failure mode, a
    different pair of layers.
    """
    if buying_power is None:
        return None
    projected = buying_power
    for o in pending_orders:
        if o["side"] != "sell":
            continue
        price = price_map.get(o["symbol"])
        if price is None:
            continue
        projected += float(o["qty"]) * price
    return projected


def _simulate_afford(ordered_buy_proposals, projected_buying_power, price_map):
    """Walks buy proposals in a single global priority order (caller
    decides the order -- best-in-cluster first, then score desc across
    clusters) and greedily decrements a running budget. A proposal is
    unaffordable if its cost exceeds whatever budget remains at the point
    it's reached; the budget is NOT decremented for an unaffordable one,
    so a later, cheaper proposal still gets evaluated against what's left.

    This is an assumed-sequential-fill projection for buying-power DISPLAY
    only -- it is not an execution plan, not an optimizer, and makes no
    claim about which order a human should actually approve proposals in.

    Returns dict[proposal_id -> bool].
    """
    affordable = {}
    if projected_buying_power is None:
        return {p["id"]: True for p in ordered_buy_proposals}
    remaining = projected_buying_power
    for p in ordered_buy_proposals:
        price = price_map.get(p["symbol"])
        qty = p.get("qty")
        if price is None or qty is None:
            affordable[p["id"]] = True  # unknown cost -- fail open, never guess a rejection
            continue
        cost = float(qty) * price
        if cost <= remaining:
            remaining -= cost
            affordable[p["id"]] = True
        else:
            affordable[p["id"]] = False
    return affordable


def _assign_tier(cluster_rank, cluster_size, saturation_ratio, risk_outcome, affordable, watch_threshold):
    """Deterministic cascade, first match wins -- same style as
    ingest/market_regime.py::classify_overall()'s branch table."""
    if risk_outcome == "rejected" or not affordable:
        return TIER_MINIMAL
    if cluster_rank == 1:
        if risk_outcome == "reduced" or saturation_ratio >= watch_threshold:
            return TIER_GOOD
        return TIER_HIGHEST
    if saturation_ratio >= 1.0:
        return TIER_MINIMAL
    if saturation_ratio >= watch_threshold or risk_outcome == "reduced":
        return TIER_LOW
    if cluster_rank <= max(2, cluster_size // 3):
        return TIER_REASONABLE
    return TIER_LOW


def _opportunity_cost_note(cluster_label, other_symbols, saturation_ratio, watch_threshold):
    if not other_symbols:
        return None
    others = ", ".join(other_symbols)
    if saturation_ratio >= watch_threshold:
        return f"Competes with: {others} — sector already heavily represented"
    return f"Competes with: {others} — {cluster_label} candidates also open"


def rank_proposals(proposals, positions, pending_orders, sector_map,
                    buying_power, portfolio_value, price_map,
                    risk_decisions_by_proposal_id, params):
    """Pure. Returns a NEW list of shallow-copied proposal dicts, each with
    additive-only ranking fields; never mutates the inputs, never touches
    signal_score/final_proposal_score/rationale/qty.

    proposals: list of dicts (id, symbol, side, qty, signal_score,
        final_proposal_score, current_price, ...) -- shape of a single
        trade_proposals row as GET /api/proposals already returns it.
    positions: dict[symbol -> {"market_value": float}].
    pending_orders: list of {"symbol", "side", "qty", "status"} -- ALL open
        orders account-wide, not just ones tied to an existing position.
    sector_map: dict[symbol -> GICS sector string], covering proposal +
        position + pending-order symbols.
    buying_power: float | None, live Alpaca buying power.
    portfolio_value: float | None.
    price_map: dict[symbol -> float], best-known price for every symbol
        touched above. Symbols absent here are treated as unknown-cost
        (fails open, never guessed) rather than penalized.
    risk_decisions_by_proposal_id: dict[proposal_id -> {"outcome":...,
        "binding_constraint":...}], latest risk_decisions row per proposal
        (context='proposal_generated'). Read-only signal -- this module
        never recomputes or overrides shared/risk_engine.py's sizing.
    params: dict from load_proposal_ranking_params(conn).

    Returns proposals unchanged (deep-copy-free passthrough of unrelated
    fields) when proposal_ranking_enabled is falsy.
    """
    if not params.get("proposal_ranking_enabled"):
        return list(proposals)
    if not proposals:
        return list(proposals)

    watch_threshold = params.get("proposal_ranking_saturation_watch_threshold",
                                  PROPOSAL_RANKING_DEFAULTS["proposal_ranking_saturation_watch_threshold"])
    sector_max_pct = params.get("sector_max_pct")

    sells = [dict(p) for p in proposals if p.get("side") == "sell"]
    for p in sells:
        p["priority_tier"] = TIER_HIGHEST
        p["priority_stars"] = STARS_BY_TIER[TIER_HIGHEST]
        p["priority_label"] = LABEL_BY_TIER[TIER_HIGHEST]
        p["recommended_action"] = "Sell"
        p["cluster_id"] = None
        p["cluster_label"] = None
        p["cluster_rank"] = None
        p["competes_with"] = []
        p["opportunity_cost_note"] = None

    buy_source = [p for p in proposals if p.get("side") == "buy"]
    clusters = _build_sector_clusters(buy_source, sector_map)

    # Global priority order for the buying-power simulation: each cluster's
    # best candidate first (rank 1 across all clusters), then everything
    # else by descending score. rank_by_id lets both this ordering and the
    # per-proposal tier assignment below share one source of truth for
    # "which rank is this proposal within its own cluster."
    rank_by_id = {}
    for members in clusters.values():
        for rank, p in enumerate(members, start=1):
            rank_by_id[p["id"]] = rank
    ordering = sorted(
        buy_source,
        key=lambda p: (0 if rank_by_id[p["id"]] == 1 else 1, -_proposal_score(p), p["symbol"]),
    )

    projected_buying_power = _projected_buying_power(buying_power, pending_orders, price_map)
    affordable_by_id = _simulate_afford(ordering, projected_buying_power, price_map)

    buys = []
    for cluster_id, members in clusters.items():
        cluster_size = len(members)
        sector = sector_map.get(members[0]["symbol"])
        cluster_label = f"{sector} ({cluster_size})" if sector and cluster_size > 1 else None
        sector_dollars = _effective_sector_dollars(sector, positions, pending_orders, sector_map, price_map)
        saturation_ratio = (sector_dollars / (portfolio_value * sector_max_pct)
                             if sector and portfolio_value and sector_max_pct else 0.0)

        for rank, p in enumerate(members, start=1):
            row = dict(p)
            decision = risk_decisions_by_proposal_id.get(p["id"], {})
            risk_outcome = decision.get("outcome")
            affordable = affordable_by_id.get(p["id"], True)

            tier = _assign_tier(rank, cluster_size, saturation_ratio, risk_outcome, affordable, watch_threshold)
            other_symbols = [m["symbol"] for m in members if m["symbol"] != p["symbol"]] if cluster_size > 1 else []

            row["priority_tier"] = tier
            row["priority_stars"] = STARS_BY_TIER[tier]
            row["priority_label"] = LABEL_BY_TIER[tier]
            row["recommended_action"] = ACTION_BY_TIER[tier]
            row["cluster_id"] = cluster_id if cluster_size > 1 else None
            row["cluster_label"] = cluster_label
            row["cluster_rank"] = rank if cluster_size > 1 else None
            row["competes_with"] = other_symbols if rank > 1 else []
            row["opportunity_cost_note"] = (
                _opportunity_cost_note(cluster_label, other_symbols, saturation_ratio, watch_threshold)
                if rank > 1 and cluster_size > 1 else None
            )
            buys.append(row)

    # Preserve a stable, priority-ordered result: buys by tier then score,
    # sells last (unchanged relative order).
    buys.sort(key=lambda p: (p["priority_tier"], -_proposal_score(p), p["symbol"]))
    return buys + sells

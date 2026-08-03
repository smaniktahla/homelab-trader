"""
FIFO position-lifecycle matching, R-multiple computation, and pathwise
MAE/MFE -- Platform Improvements PR A (position lifecycles / R-multiple
foundation). See docs/session-handoff-2026-07-31.md sections 3/4/6.

Distinct from shared/round_trips.py's average-cost reconstruction (still
what /api/symbol-performance serves as of this PR -- see that module's own
docstring): round_trips.py blends every entry into one running average
cost and has no concept of planned risk. This module does true FIFO lot
matching -- each entry keeps its own price/cost/stop until consumed by a
sell, oldest lot first -- and carries a real planned-vs-actual risk basis
per lifecycle plus pathwise MAE/MFE. Swapping /api/symbol-performance over
to this module is a deliberate, separate follow-up PR, not part of this
one -- see round_trips.py's own docstring for why.

Long-only. Nothing in this codebase supports short positions as of this
PR; a sell with no locally-open qty is an oversell/pre-ledger-holding case
(see unmatched_sell_qty), never a short entry.

R-multiples are normalized against a lifecycle's INITIAL entry risk only
(actual_initial_risk_per_share, from the first fill's own linked
proposal) -- not renegotiated as pyramid adds occur, matching conventional
R-multiple practice: a trade's R is defined by its original risk, not a
moving target. Pathwise MAE/MFE, by contrast, tracks a time-varying
weighted-average cost basis as the excursion reference (recomputed at
every pyramid entry, per the "time-varying qty and cost basis" framing in
the handoff doc) -- the dollar excursion itself is measured against a
moving basis; R-normalizing that excursion still divides by the fixed
initial risk-per-share, for the same reason realized_r does.

Missing risk data (no linked proposal, e.g. a manual trade) means every
planned/actual risk and R field is None -- never coerced to 0 or skipped
silently, consistent with every other NULL-vs-zero decision already made
in this codebase (symbol_features, fundamental_facts, round_trips.py's own
cost proration).

No DB access here -- callers fetch and join rows, then pass them in,
matching the pure-function style already established by round_trips.py
and shared/signal_components.py. price_bars_by_symbol lets the MAE/MFE
walk stay pure and unit-testable without a live DB connection.
"""

from dataclasses import dataclass, field

_EPSILON = 1e-9  # float qty noise guard, not a real share-count tolerance


@dataclass
class Lot:
    """One still-open (or partially-open) FIFO entry lot, oldest-first."""
    trade_id: int
    qty_remaining: float
    price: float
    cost: float                    # trades.cost for this entry trade, full (prorated at exit time)
    traded_at: object
    thesis_id: object
    initial_stop_price: object     # trades.initial_stop_price; None if no linked proposal


@dataclass
class PositionLifecycle:
    symbol: str
    status: str                              # 'open' | 'closed'
    thesis_id: object                        # None if ambiguous -- see data_quality_flags
    opened_at: object
    closed_at: object                        # None while open
    qty: float
    planned_entry_price: object
    planned_initial_stop_price: object
    planned_risk_per_share: object
    planned_risk_dollars: object
    initial_stop_price: object               # from the FIRST entry fill only
    actual_initial_risk_per_share: object
    actual_initial_risk_dollars: object
    entry_notional: float
    exit_notional: float                     # realized-to-date; 0.0 if nothing sold yet, partial if still open
    total_cost: float                        # entry cost prorated to shares actually sold + full exit cost
    gross_pnl: float                         # realized-to-date; 0.0 baseline, partial if still open
    net_pnl: float                           # realized-to-date; 0.0 baseline, partial if still open
    realized_r: object                       # None if risk unknown or zero
    mae_price: object
    mfe_price: object
    mae_r: object
    mfe_r: object
    excursion_resolution: object             # 'hourly' | 'daily_approximation' | None
    data_quality_flags: list
    entry_trade_ids: list = field(default_factory=list)
    exit_trade_ids: list = field(default_factory=list)
    position_trades: list = field(default_factory=list)   # [{trade_id, role, qty_allocated}]


def match_lifecycles(trade_rows, price_bars_by_symbol=None):
    """trade_rows: iterable of dicts with id, symbol, side, qty, price,
    cost, traded_at, thesis_id, initial_stop_price, plus planned_entry_price
    / planned_initial_stop_price / planned_risk_per_share /
    planned_risk_dollars (only meaningfully populated on buy trades with a
    linked proposal -- None otherwise). Already filtered to status='filled',
    already ordered traded_at ASC, id ASC, GLOBALLY across all symbols --
    same convention as round_trips.reconstruct, so a lifecycle's entry is
    never missed just because it falls outside some limited slice.

    price_bars_by_symbol: optional {symbol: [{"ts", "high", "low",
    "resolution"}, ...]} sorted ascending by ts, covering at least the
    union of all this symbol's lifecycle holding windows. Pathwise MAE/MFE
    is skipped (all excursion fields stay None) for a symbol not present
    here -- callers that don't need MAE/MFE can omit this entirely.

    Returns {symbol: {"lifecycles": [PositionLifecycle, ...],
    "unmatched_sell_qty": float}}. lifecycles is oldest -> newest; a symbol
    with qty back at exactly zero at the end of the ledger has no open
    lifecycle in the list. unmatched_sell_qty is the lifetime total of sell
    quantity that could not be matched to any locally-known FIFO lot
    (pre-ledger broker holdings, data gaps) -- nonzero means this symbol's
    lifecycle history is provably incomplete, same limitation round_trips.py
    already documents and surfaces via methodology_status.
    """
    fifo = {}          # symbol -> list[Lot], oldest first
    building = {}       # symbol -> in-progress lifecycle accumulator dict, or None
    unmatched = {}      # symbol -> float
    results = {}

    def _start_building(symbol, first_trade):
        return {
            "thesis_ids_seen": set(),
            "opened_at": first_trade["traded_at"],
            "planned_entry_price": first_trade.get("planned_entry_price"),
            "planned_initial_stop_price": first_trade.get("planned_initial_stop_price"),
            "planned_risk_per_share": first_trade.get("planned_risk_per_share"),
            "planned_risk_dollars": first_trade.get("planned_risk_dollars"),
            "initial_stop_price": first_trade.get("initial_stop_price"),
            "first_fill_price": float(first_trade["price"]),
            "entry_notional": 0.0,
            "exit_notional": 0.0,
            "entry_cost_total": 0.0,
            "exit_cost_total": 0.0,
            "entry_qty_total": 0.0,
            "sold_qty_so_far": 0.0,
            "gross_pnl": 0.0,
            "entry_trade_ids": [],
            "exit_trade_ids": [],
            "position_trades": [],
            "_entry_fills": [],   # [{"qty", "price", "traded_at"}, ...] -- feeds the excursion walk's weighted-avg cost basis
        }

    def _actual_initial_risk(b):
        stop = b["initial_stop_price"]
        if stop is None:
            return None, None
        risk_per_share = abs(b["first_fill_price"] - float(stop))
        risk_dollars = risk_per_share * b["_first_fill_qty"]
        return risk_per_share, risk_dollars

    def _prorated_entry_cost(b):
        """Only the entry commission attributable to shares actually sold
        so far -- same principle round_trips.py already established
        (charging 100% of an entry's cost as realized the instant a
        position opens, before any shares are sold, would count cost
        against still-open shares' cost basis). Reduces to the full
        entry_cost_total exactly when sold_qty_so_far reaches
        entry_qty_total (a fully closed lifecycle)."""
        if not b["entry_qty_total"]:
            return 0.0
        return b["entry_cost_total"] * (b["sold_qty_so_far"] / b["entry_qty_total"])

    def _flush(symbol, b, status, closed_at):
        thesis_ids = b["thesis_ids_seen"]
        flags = []
        thesis_id = next(iter(thesis_ids)) if len(thesis_ids) == 1 else None
        if len(thesis_ids) > 1:
            flags.append("concurrent_multi_thesis_symbol")

        risk_per_share, risk_dollars = _actual_initial_risk(b)
        # gross_pnl/net_pnl/exit_notional represent REALIZED-TO-DATE, not
        # "final only once closed" -- a still-open lifecycle that has had a
        # partial exit has a real, well-defined realized P&L on the shares
        # actually sold so far (0.0, not None, if nothing has been sold
        # yet -- that's a known fact, not missing data). Entry cost is
        # prorated to the fraction of shares actually sold, same principle
        # as round_trips.py's own proration, so an open lifecycle's
        # reported cost never front-loads commission against shares that
        # haven't been sold yet.
        gross_pnl = round(b["gross_pnl"], 2)
        total_cost = round(_prorated_entry_cost(b) + b["exit_cost_total"], 2)
        net_pnl = round(gross_pnl - total_cost, 2)
        realized_r = round(net_pnl / risk_dollars, 3) if risk_dollars else None

        mae_price, mfe_price, mae_r, mfe_r, resolution = _walk_excursion(
            symbol, b, closed_at, risk_per_share, price_bars_by_symbol,
        )

        remaining_qty = sum(l.qty_remaining for l in fifo.get(symbol, []))
        lc = PositionLifecycle(
            symbol=symbol, status=status, thesis_id=thesis_id,
            opened_at=b["opened_at"], closed_at=closed_at,
            qty=remaining_qty if status == "open" else b.get("_closed_qty"),
            planned_entry_price=b["planned_entry_price"],
            planned_initial_stop_price=b["planned_initial_stop_price"],
            planned_risk_per_share=b["planned_risk_per_share"],
            planned_risk_dollars=b["planned_risk_dollars"],
            initial_stop_price=b["initial_stop_price"],
            actual_initial_risk_per_share=(
                round(risk_per_share, 4) if risk_per_share is not None else None
            ),
            actual_initial_risk_dollars=(
                round(risk_dollars, 2) if risk_dollars is not None else None
            ),
            entry_notional=round(b["entry_notional"], 2),
            exit_notional=round(b["exit_notional"], 2),
            total_cost=total_cost,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            realized_r=realized_r,
            mae_price=mae_price, mfe_price=mfe_price, mae_r=mae_r, mfe_r=mfe_r,
            excursion_resolution=resolution,
            data_quality_flags=flags,
            entry_trade_ids=list(b["entry_trade_ids"]),
            exit_trade_ids=list(b["exit_trade_ids"]),
            position_trades=list(b["position_trades"]),
        )
        results.setdefault(symbol, []).append(lc)

    for t in trade_rows:
        symbol = t["symbol"]
        qty, price, cost = float(t["qty"]), float(t["price"]), float(t.get("cost") or 0)
        lots = fifo.setdefault(symbol, [])
        b = building.get(symbol)

        if t["side"] == "buy":
            if b is None:
                b = _start_building(symbol, t)
                b["_first_fill_qty"] = qty
                building[symbol] = b
            lots.append(Lot(
                trade_id=t["id"], qty_remaining=qty, price=price, cost=cost,
                traded_at=t["traded_at"], thesis_id=t.get("thesis_id"),
                initial_stop_price=t.get("initial_stop_price"),
            ))
            b["thesis_ids_seen"].add(t.get("thesis_id"))
            b["entry_notional"] += qty * price
            b["entry_cost_total"] += cost
            b["entry_qty_total"] += qty
            b["entry_trade_ids"].append(t["id"])
            b["position_trades"].append({"trade_id": t["id"], "role": "entry", "qty_allocated": qty})
            b["_entry_fills"].append({"qty": qty, "price": price, "traded_at": t["traded_at"]})

        elif t["side"] == "sell":
            if b is None or not lots:
                # Nothing open locally at all -- pre-ledger broker holding
                # or an oversell that already exhausted every FIFO lot.
                # Never fabricate a matching entry -- tally instead, same
                # principle round_trips.py already established.
                unmatched[symbol] = unmatched.get(symbol, 0.0) + qty
                continue

            remaining_to_sell = qty
            while remaining_to_sell > _EPSILON and lots:
                lot = lots[0]
                take = min(remaining_to_sell, lot.qty_remaining)
                b["gross_pnl"] += take * (price - lot.price)
                b["exit_notional"] += take * price
                b["sold_qty_so_far"] += take
                lot.qty_remaining -= take
                remaining_to_sell -= take
                b["position_trades"].append({"trade_id": t["id"], "role": "exit", "qty_allocated": take})
                if lot.qty_remaining <= _EPSILON:
                    lots.pop(0)

            # Cost is charged once against the exit trade as a whole,
            # regardless of how many FIFO lots it happened to consume -- a
            # single sell fill has one commission, not one per lot.
            b["exit_cost_total"] += cost
            b["exit_trade_ids"].append(t["id"])

            if remaining_to_sell > _EPSILON:
                # This sell's qty exceeded every locally-known open lot --
                # tally the excess, same as the no-lots-at-all case above.
                unmatched[symbol] = unmatched.get(symbol, 0.0) + remaining_to_sell

            if not lots:
                # Closed qty is the total entry qty for this lifecycle --
                # every share that came in has now gone back out.
                b["_closed_qty"] = sum(
                    p["qty_allocated"] for p in b["position_trades"] if p["role"] == "entry"
                )
                _flush(symbol, b, status="closed", closed_at=t["traded_at"])
                building[symbol] = None

    # Anything still open at the end of the ledger.
    for symbol, b in building.items():
        if b is not None and fifo.get(symbol):
            _flush(symbol, b, status="open", closed_at=None)

    return {
        symbol: {
            "lifecycles": results.get(symbol, []),
            "unmatched_sell_qty": round(unmatched.get(symbol, 0.0), 6),
        }
        for symbol in set(list(results.keys()) + list(unmatched.keys()))
    }


def _walk_excursion(symbol, b, closed_at, risk_per_share, price_bars_by_symbol):
    """Pathwise MAE/MFE against a time-varying weighted-average cost basis.

    Walks this symbol's price bars across the holding window, tracking a
    running weighted-average cost basis that updates at every entry lot's
    traded_at (matching entry_trade_ids' notional/qty, not the FIFO
    consumption order used for P&L -- excursion is about "what was my
    blended cost while this bar was in play," which is a portfolio-level
    fact independent of which specific lot a later sell happens to
    consume). mae_r/mfe_r divide by the FIXED initial risk-per-share (see
    module docstring) -- the moving basis affects the dollar excursion,
    not the R-normalization denominator.

    Returns (mae_price, mfe_price, mae_r, mfe_r, resolution). All None if
    no price bars are available for this symbol/window at all.
    """
    if not price_bars_by_symbol or symbol not in price_bars_by_symbol:
        return None, None, None, None, None

    bars = price_bars_by_symbol[symbol]
    window_end = closed_at if closed_at is not None else (bars[-1]["ts"] if bars else None)
    window_start = b["opened_at"]
    if window_end is None:
        return None, None, None, None, None

    # Rebuild the weighted-average cost basis trajectory from this
    # lifecycle's own entry notionals -- position_trades already has every
    # entry's (trade_id, qty_allocated); pairing that back to price/traded_at
    # would need the original trade rows again, so instead the caller-level
    # builder is expected to have folded traded_at/price into
    # b["entry_trade_ids"] ordering via the same trade_rows this function's
    # caller already walked. Simpler and just as correct: approximate the
    # running basis using entry_notional / cumulative qty at each known
    # entry point, which is exactly what a weighted average is.
    cost_basis = None
    cum_qty = 0.0
    cum_notional = 0.0
    resolutions_used = set()
    mae_excursion = None   # most negative (price - cost_basis), i.e. worst
    mfe_excursion = None   # most positive
    mae_price = None
    mfe_price = None

    entries = sorted(b.get("_entry_fills", []), key=lambda e: e["traded_at"])
    entry_idx = 0

    for bar in bars:
        if bar["ts"] < window_start or bar["ts"] > window_end:
            continue
        while entry_idx < len(entries) and entries[entry_idx]["traded_at"] <= bar["ts"]:
            e = entries[entry_idx]
            cum_qty += e["qty"]
            cum_notional += e["qty"] * e["price"]
            entry_idx += 1
        if cum_qty <= _EPSILON:
            continue
        cost_basis = cum_notional / cum_qty
        resolutions_used.add(bar["resolution"])

        low_excursion = float(bar["low"]) - cost_basis
        high_excursion = float(bar["high"]) - cost_basis
        if mae_excursion is None or low_excursion < mae_excursion:
            mae_excursion = low_excursion
            mae_price = float(bar["low"])
        if mfe_excursion is None or high_excursion > mfe_excursion:
            mfe_excursion = high_excursion
            mfe_price = float(bar["high"])

    if mae_price is None:
        return None, None, None, None, None

    resolution = (
        "hourly" if resolutions_used == {"hourly"}
        else "daily_approximation" if resolutions_used
        else None
    )
    mae_r = round(mae_excursion / risk_per_share, 3) if risk_per_share else None
    mfe_r = round(mfe_excursion / risk_per_share, 3) if risk_per_share else None
    return round(mae_price, 4), round(mfe_price, 4), mae_r, mfe_r, resolution

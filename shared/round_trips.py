"""
Average-cost reconstruction of per-symbol round trips from the raw `trades`
ledger.

This is an explicit stand-in for the position_lifecycles work (see the
Platform Improvements roadmap: R-multiple tracking, expectancy reporting) --
it is NOT that. It treats each symbol as one continuously blended
weighted-average-cost position (same convention as
api/main.py::_realized_pnl_by_trade_id, which this module's walk logic
mirrors) and segments it into discrete "round trips" at every point qty
returns exactly to zero. It has no concept of planned entry/stop/risk --
those don't exist until position_lifecycles ships. METHODOLOGY is exported
specifically so every caller surfaces it alongside any figure this module
produces, rather than letting a reconstructed number quietly masquerade as
lifecycle-grade data once that work lands.

Terminology note, because "gross" is used two different ways in this
codebase's own roadmap discussion: RoundTrip.pnl_before_costs is this one
trip's P&L before trading costs are subtracted (cost-basis sense of
"gross"). portfolio_totals()'s gross_profit_total/gross_loss_total is a
different axis entirely -- the sum of *already cost-adjusted* net_pnl
across only the winning (or only the losing) round trips, matching the
profit-factor convention (gross_profit / abs(gross_loss)) from the
Strategy Expectancy roadmap item. Do not conflate the two.

No DB access here -- callers fetch rows and pass them in, matching the
pure-function style already established by shared/signal_components.py.
"""

from dataclasses import dataclass, field
from statistics import mean, median

METHODOLOGY = "average_cost_reconstruction"

_EPSILON = 1e-9  # float qty noise guard, not a real share-count tolerance


@dataclass
class RoundTrip:
    symbol: str
    entry_trade_ids: list
    exit_trade_ids: list
    opened_at: object          # datetime
    closed_at: object          # datetime
    qty: float                 # total quantity that passed through this round trip
    entry_notional: float      # capital committed: sum of entry qty * entry price
    pnl_before_costs: float    # gross, in the cost-basis sense -- see module docstring
    total_cost: float          # sum of trades.cost across every entry + exit trade in this trip
    net_pnl: float             # pnl_before_costs - total_cost
    return_pct: float          # net_pnl / entry_notional * 100
    holding_days: float


@dataclass
class OpenEpisode:
    symbol: str
    entry_trade_ids: list
    partial_exit_trade_ids: list
    opened_at: object                # datetime
    reconstructed_qty: float         # what the local ledger walk believes is still open
    entry_notional: float            # capital committed via entries in this still-open episode
    partial_realized_pnl_before_costs: float
    partial_realized_cost: float
    partial_realized_net_pnl: float  # already-banked P&L from partial exits within this episode


def reconstruct(trade_rows):
    """trade_rows: iterable of row objects/dicts with id, symbol, side, qty,
    price, cost, traded_at -- already filtered to status='filled', already
    ordered traded_at ASC, id ASC, GLOBALLY across all symbols (matching
    _realized_pnl_by_trade_id's existing query -- this uses the full ledger,
    never a limited/paginated slice, so a round trip's entry is never missed
    just because it falls outside whatever window a UI happens to display).

    Returns {symbol: {"round_trips": [RoundTrip, ...], "open_episode": OpenEpisode | None}}.
    round_trips is oldest -> newest. A symbol with qty back at exactly zero
    at the end of the ledger has open_episode=None.
    """
    state = {}  # symbol -> running walk state
    result = {}

    def _get_state(symbol):
        return state.setdefault(symbol, {
            "qty": 0.0, "total_cost_basis": 0.0,
            "entry_trade_ids": [], "partial_exit_trade_ids": [],
            "opened_at": None, "entry_notional": 0.0, "entry_qty_total": 0.0,
            "entry_cost_total": 0.0, "sold_qty_so_far": 0.0,
            "realized_before_costs": 0.0, "exit_cost_total": 0.0,
        })

    def _prorated_entry_cost(s):
        """Only the entry commission attributable to shares that have
        actually been sold so far. Charging 100% of an entry's cost as
        "realized" the instant a position opens -- before any shares are
        sold -- would be wrong: that cost belongs to the still-open
        shares' (locally untracked, deferred-to-Alpaca) cost basis, not to
        anything realized yet. This reduces to the full entry_cost_total
        exactly when sold_qty_so_far reaches entry_qty_total (a fully
        closed round trip), so the open-episode and completed-trip
        formulas agree at the boundary."""
        if not s["entry_qty_total"]:
            return 0.0
        return s["entry_cost_total"] * (s["sold_qty_so_far"] / s["entry_qty_total"])

    def _flush_round_trip(symbol, s, closed_at):
        total_cost = s["entry_cost_total"] + s["exit_cost_total"]  # sold_qty_so_far == entry_qty_total here
        rt = RoundTrip(
            symbol=symbol,
            entry_trade_ids=list(s["entry_trade_ids"]),
            exit_trade_ids=list(s["partial_exit_trade_ids"]),
            opened_at=s["opened_at"],
            closed_at=closed_at,
            qty=s["entry_qty_total"],
            entry_notional=s["entry_notional"],
            pnl_before_costs=round(s["realized_before_costs"], 2),
            total_cost=round(total_cost, 2),
            net_pnl=round(s["realized_before_costs"] - total_cost, 2),
            return_pct=(
                round((s["realized_before_costs"] - total_cost) / s["entry_notional"] * 100, 2)
                if s["entry_notional"] else None
            ),
            holding_days=(closed_at - s["opened_at"]).total_seconds() / 86400 if s["opened_at"] else None,
        )
        result.setdefault(symbol, {"round_trips": [], "open_episode": None})
        result[symbol]["round_trips"].append(rt)

    for t in trade_rows:
        symbol = t["symbol"]
        s = _get_state(symbol)
        qty, price, cost = float(t["qty"]), float(t["price"]), float(t.get("cost") or 0)

        if t["side"] == "buy":
            if s["qty"] <= _EPSILON:
                # Starting (or restarting, post-flush) a fresh episode.
                s["opened_at"] = t["traded_at"]
                s["entry_trade_ids"] = []
                s["partial_exit_trade_ids"] = []
                s["entry_notional"] = 0.0
                s["entry_qty_total"] = 0.0
                s["entry_cost_total"] = 0.0
                s["sold_qty_so_far"] = 0.0
                s["realized_before_costs"] = 0.0
                s["exit_cost_total"] = 0.0
            s["qty"] += qty
            s["total_cost_basis"] += qty * price
            s["entry_trade_ids"].append(t["id"])
            s["entry_notional"] += qty * price
            s["entry_qty_total"] += qty
            s["entry_cost_total"] += cost

        elif t["side"] == "sell" and s["qty"] > _EPSILON:
            avg_cost = s["total_cost_basis"] / s["qty"]
            sell_qty = min(qty, s["qty"])  # guard against oversell/data gaps, same as _realized_pnl_by_trade_id
            realized = sell_qty * (price - avg_cost)
            s["realized_before_costs"] += realized
            s["exit_cost_total"] += cost
            s["sold_qty_so_far"] += sell_qty
            s["partial_exit_trade_ids"].append(t["id"])
            s["qty"] -= sell_qty
            s["total_cost_basis"] -= sell_qty * avg_cost

            if s["qty"] <= _EPSILON:
                _flush_round_trip(symbol, s, closed_at=t["traded_at"])
                s["qty"] = 0.0
                s["total_cost_basis"] = 0.0
        # a sell while s["qty"] <= 0 (nothing open locally -- e.g. a
        # pre-ledger broker holding) is silently ignored by this
        # methodology; it has no entry to attribute P&L against. This is a
        # known, documented limitation of average-cost reconstruction, not
        # a bug -- position_lifecycles' explicit data_quality_flags is the
        # real fix, not something this stand-in module should approximate.

    for symbol, s in state.items():
        result.setdefault(symbol, {"round_trips": [], "open_episode": None})
        if s["qty"] > _EPSILON:
            partial_cost = _prorated_entry_cost(s) + s["exit_cost_total"]
            result[symbol]["open_episode"] = OpenEpisode(
                symbol=symbol,
                entry_trade_ids=list(s["entry_trade_ids"]),
                partial_exit_trade_ids=list(s["partial_exit_trade_ids"]),
                opened_at=s["opened_at"],
                reconstructed_qty=round(s["qty"], 6),
                entry_notional=round(s["entry_notional"], 2),
                partial_realized_pnl_before_costs=round(s["realized_before_costs"], 2),
                partial_realized_cost=round(partial_cost, 2),
                partial_realized_net_pnl=round(s["realized_before_costs"] - partial_cost, 2),
            )

    return result


def portfolio_totals(reconstruction):
    """Across ALL symbols' COMPLETED round trips only -- open episodes
    (realized or not) never enter this. gross_profit_total is the sum of
    net_pnl over winning trips only; gross_loss_total over losing trips
    only (a negative number, or 0.0 if there are none). net_pnl_total is
    their sum. See module docstring for why "gross" here is not the same
    axis as RoundTrip.pnl_before_costs."""
    gross_profit_total = 0.0
    gross_loss_total = 0.0
    for data in reconstruction.values():
        for rt in data["round_trips"]:
            if rt.net_pnl > 0:
                gross_profit_total += rt.net_pnl
            elif rt.net_pnl < 0:
                gross_loss_total += rt.net_pnl
    return {
        "gross_profit_total": round(gross_profit_total, 2),
        "gross_loss_total": round(gross_loss_total, 2),
        "net_pnl_total": round(gross_profit_total + gross_loss_total, 2),
    }


def symbol_contribution(symbol, reconstruction, totals):
    """contribution_to_gross_gains_pct: this symbol's own winning round
    trips' net_pnl, as a % of the portfolio's gross_profit_total. None if
    the portfolio has no gross profit at all (nothing to be a share of).

    contribution_to_net_pnl_pct: this symbol's total net_pnl across all its
    round trips, as a % of portfolio net_pnl_total. Can legitimately exceed
    100% or be negative -- documented explicitly, not a bug, when other
    symbols' losses (or gains) move the shared denominator. None only when
    net_pnl_total is exactly 0 (can't express a share of zero)."""
    data = reconstruction.get(symbol, {"round_trips": []})
    symbol_gross_profit = sum(rt.net_pnl for rt in data["round_trips"] if rt.net_pnl > 0)
    symbol_net_pnl = sum(rt.net_pnl for rt in data["round_trips"])

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
    }


def symbol_summary(symbol, reconstruction, totals, live_position=None):
    """Builds the full Symbol Performance Summary payload for one symbol.

    Completed-trade statistics (win rate, avg/median return, best/worst,
    avg holding period) are computed EXCLUSIVELY from this symbol's
    completed round trips -- open_episode and live_position never enter
    those specific figures, by construction (they're simply not read when
    computing them). Unrealized and total P&L are separate, clearly-keyed
    fields built from live_position (sourced from Alpaca by the caller --
    this module never fetches it) and open_episode respectively.

    live_position: the dict this app's own /api/positions already returns
    for this symbol (qty, avg_entry_price, current_price, unrealized_pl,
    market_value), or None if nothing is currently held. Passed in rather
    than re-derived locally because Alpaca's own live position accounting
    is the authoritative "what do I currently hold" answer elsewhere in
    this app already -- duplicating that logic here via the local ledger
    walk would risk a second, potentially-drifting answer to the same
    question.
    """
    data = reconstruction.get(symbol, {"round_trips": [], "open_episode": None})
    round_trips = data["round_trips"]
    open_episode = data["open_episode"]
    n = len(round_trips)

    wins = [rt for rt in round_trips if rt.net_pnl > 0]
    losses = [rt for rt in round_trips if rt.net_pnl < 0]
    breakeven = [rt for rt in round_trips if rt.net_pnl == 0]

    returns = [rt.return_pct for rt in round_trips if rt.return_pct is not None]
    best_trip = max(round_trips, key=lambda rt: rt.net_pnl) if round_trips else None
    worst_trip = min(round_trips, key=lambda rt: rt.net_pnl) if round_trips else None
    holding_days = [rt.holding_days for rt in round_trips if rt.holding_days is not None]

    realized_pnl_total = sum(rt.net_pnl for rt in round_trips)
    realized_pnl_total += open_episode.partial_realized_net_pnl if open_episode else 0.0

    unrealized_pnl = float(live_position["unrealized_pl"]) if live_position else 0.0

    capital_deployed = sum(rt.entry_notional for rt in round_trips)
    capital_deployed += open_episode.entry_notional if open_episode else 0.0

    contribution = symbol_contribution(symbol, reconstruction, totals)

    data_quality_note = None
    if open_episode and live_position:
        qty_diff = abs(open_episode.reconstructed_qty - float(live_position["qty"]))
        if qty_diff > 1e-4:
            data_quality_note = (
                f"Local ledger reconstruction shows {open_episode.reconstructed_qty} shares open, "
                f"but the broker reports {live_position['qty']} — some fills (e.g. a pre-ledger "
                f"broker holding, or a trade placed outside this app) aren't reflected in the "
                f"round-trip history below."
            )
    elif open_episode and not live_position:
        data_quality_note = (
            "Local ledger reconstruction believes a position is still open, but the broker reports "
            "none — likely a manual close outside this app."
        )

    return {
        "symbol": symbol,
        "methodology": METHODOLOGY,
        "completed_round_trips": n,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else None,
        "avg_return_pct": round(mean(returns), 2) if returns else None,
        "median_return_pct": round(median(returns), 2) if returns else None,
        "best_trip": _trip_summary(best_trip),
        "worst_trip": _trip_summary(worst_trip),
        "avg_holding_days": round(mean(holding_days), 1) if holding_days else None,
        "capital_deployed": round(capital_deployed, 2),
        "realized_pnl": round(realized_pnl_total, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_pnl": round(realized_pnl_total + unrealized_pnl, 2),
        "open_position": open_episode_summary(open_episode, live_position),
        "data_quality_note": data_quality_note,
        **contribution,
    }


def _trip_summary(rt):
    if rt is None:
        return None
    return {
        "opened_at": rt.opened_at.isoformat() if rt.opened_at else None,
        "closed_at": rt.closed_at.isoformat() if rt.closed_at else None,
        "net_pnl": rt.net_pnl,
        "return_pct": rt.return_pct,
        "qty": rt.qty,
    }


def open_episode_summary(open_episode, live_position):
    if not open_episode and not live_position:
        return None
    return {
        "opened_at": open_episode.opened_at.isoformat() if open_episode and open_episode.opened_at else None,
        "qty": float(live_position["qty"]) if live_position else (open_episode.reconstructed_qty if open_episode else None),
        "avg_entry_price": float(live_position["avg_entry_price"]) if live_position else None,
        "current_price": float(live_position["current_price"]) if live_position else None,
        "unrealized_pnl": float(live_position["unrealized_pl"]) if live_position else None,
        "partial_realized_pnl": open_episode.partial_realized_net_pnl if open_episode else None,
        "entry_notional": open_episode.entry_notional if open_episode else None,
    }

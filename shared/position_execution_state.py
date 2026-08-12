"""
Position Execution State, PR A of the exit/protection-state series --
per docs/position-exit-state-investigation.md §5's PR A bullet: replace
the dashboard's single "any open order = sell pending" inference with an
explicit classification over the 4 states that were being collapsed
together (§1 of that doc).

This module is deliberately pure and DB-free for its core classifier --
same "compute once, tested standalone" split shared/market_structure.py
already established -- so it can be exercised with hand-built inputs, no
disposable-Postgres fixture required. `load_open_sell_proposal()` is the
one DB-touching helper, kept thin, mirroring the read-only helpers already
in shared/trade_thesis.py.

Ships dark: nothing in api/main.py or ingest.py calls this yet (PR C's
job, per the doc). Orthogonal to trade_theses.status (PR 1-12,
Hypothesis-Driven Trading epic) -- that's a strategy-judgment axis ("is
the hypothesis still true?"); this is an execution-mechanics axis ("what
is the broker doing with the shares right now?"). See the doc's §4 for
why these are deliberately not merged into one enum.

`closing` (case 4 -- a broker stop that has already fired but hasn't been
reconciled into the local trades ledger yet) is NOT classified here. It
requires data /api/positions cannot see (the position is already gone
from Alpaca's own /v2/positions by the time this state would apply) --
that's PR B's job, per the doc.
"""

STATE_OWNED = "owned"
STATE_PROTECTED = "protected"
STATE_EXIT_RECOMMENDED = "exit_recommended"
STATE_SELL_PENDING = "sell_pending"
STATE_CLOSING = "closing"

VALID_STATES = (STATE_OWNED, STATE_PROTECTED, STATE_EXIT_RECOMMENDED,
                 STATE_SELL_PENDING, STATE_CLOSING)

# Alpaca order `type` values that represent a resting protective stop
# (case 1) rather than a submitted, actively-executing exit (case 3).
_STOP_ORDER_TYPES = ("stop", "stop_limit")


def classify_position_execution_state(pending_orders, open_sell_proposal=None):
    """Pure classifier. No DB, no Alpaca call -- everything it needs is
    passed in.

    pending_orders: list of dicts for this symbol's OPEN Alpaca orders
        only (status='open' per api/main.py::get_positions()'s existing
        query), each with at least {"side", "type"}. Safe to pass the
        exact shape api/main.py already builds for `pending_orders` per
        position.
    open_sell_proposal: the symbol's open (decision IS NULL, side='sell')
        trade_proposals row, as a dict, or None if there isn't one. Only
        presence/absence matters here -- callers needing the rationale/
        exit_reason for display read it off this same dict (PR C).

    Precedence, most urgent first:
      1. sell_pending  -- a submitted market sell order is actually
         in flight. This beats an open proposal because approving a
         proposal is exactly what PRODUCES the market order (api/main.py
         decides the proposal to 'approved' in the same transaction that
         submits the order) -- by the time both could coexist, the
         proposal is no longer "open" (decision is no longer NULL), so in
         practice these two are already mutually exclusive today. Ordered
         defensively anyway, not load-bearing on that invariant holding.
      2. exit_recommended -- an open strategy-layer proposal exists,
         regardless of whether a resting protective stop (case 1) is also
         sitting under the same position -- a human decision is queued
         either way, and that's more urgent to surface than "protected."
      3. protected -- a resting stop order (case 1), no case 2/3 activity.
      4. owned -- no protection and nothing pending. Deliberately its own
         state (not lumped into "protected") -- a position with no stop
         attached at all (pre-dating "Execution: protective stop orders,"
         or a stop that failed to attach) is meaningfully different from
         one that's actively protected, and collapsing them back together
         would recreate the exact kind of information loss this PR series
         exists to undo.
    """
    pending_orders = pending_orders or []

    has_pending_market_sell = any(
        o.get("side") == "sell" and o.get("type") == "market"
        for o in pending_orders
    )
    if has_pending_market_sell:
        return STATE_SELL_PENDING

    if open_sell_proposal is not None:
        return STATE_EXIT_RECOMMENDED

    has_resting_stop = any(
        o.get("side") == "sell" and o.get("type") in _STOP_ORDER_TYPES
        for o in pending_orders
    )
    if has_resting_stop:
        return STATE_PROTECTED

    return STATE_OWNED


def find_closing_fills(closed_orders, known_open_symbols, known_trade_order_ids):
    """PR B of the exit/protection-state series -- per
    docs/position-exit-state-investigation.md's PR B bullet: surface case
    4 (a broker stop that has fired but hasn't been reconciled into the
    local `trades` ledger yet) instead of letting the position silently
    disappear.

    Pure filter, no DB/Alpaca IO -- same split as
    classify_position_execution_state(). Mirrors the exact filtering
    ingest.py::reconcile_broker_stop_fills() already uses to find
    broker-initiated stop fills, but read-only and framed as "what should
    the UI show right now" rather than "what should get written to
    trades" -- the two call sites (this one, live per-request; that one,
    hourly batch) are intentionally independent so a slow ingest cycle
    can never delay what the dashboard shows.

    closed_orders: Alpaca orders with status='closed' (any terminal
        status), e.g. from GET /v2/orders?status=closed&after=<lookback>.
    known_open_symbols: set of symbols currently held per Alpaca's own
        /v2/positions -- a symbol still open obviously isn't "closing."
    known_trade_order_ids: set of trades.order_id already present locally
        -- once reconcile_broker_stop_fills() (or any other path) has
        written the trade, this stops being a gap to report; it's just a
        normal closed position at that point, visible in Trade History.

    Returns the subset of closed_orders that are still in the gap: a
    filled protective stop, for a symbol no longer open at the broker,
    with no local trades row yet."""
    return [
        o for o in closed_orders
        if o.get("status") == "filled"
        and o.get("type") in _STOP_ORDER_TYPES
        and o.get("side") == "sell"
        and float(o.get("filled_qty") or 0) > 0
        and o.get("symbol") not in known_open_symbols
        and o.get("id") not in known_trade_order_ids
    ]


def load_open_sell_proposal(conn, symbol):
    """The symbol's open (undecided) sell proposal, if any -- at most one
    can exist at a time per _open_sell_exists()'s own invariant
    (shared/signals.py), which every proposal-creating function already
    checks before inserting. Read-only, fail-open shape mirrors
    shared/trade_thesis.py::load_trade_thesis()."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM trade_proposals
            WHERE symbol=%s AND side='sell' AND decision IS NULL
            ORDER BY proposed_at DESC LIMIT 1
        """, (symbol,))
        row = cur.fetchone()
    return dict(row) if row else None

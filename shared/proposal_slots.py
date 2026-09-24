"""
Slot-aware limits on open BUY proposals.

Found live 2026-09-24: the max_open_positions gate in
signals.compute_signals() only counts FILLED positions, so open proposals
reserved nothing. With one slot left, every symbol scoring above the
proposal threshold got a proposal each cycle -- 99 open buys (about
$294k) against roughly $38k of cash and a single free slot, each with its
own email + WhatsApp alert (20-50 alerts a day, from about 1-2 before the
2026-09-19 unpause). Almost none could be approved.

This module bounds the open BUY proposal set to
    free_slots + open_buy_proposal_buffer
and keeps the highest-scored ones. Two entry points:

* trim_surplus_buy_proposals(): once per cycle, before generation --
  rejects the lowest-scored surplus (clears an existing backlog, and
  handles capacity shrinking when a position fills).
* signals.compute_signals() uses allowed_open_buys()/weakest() to let a
  new signal in only if there is room, or if it outscores the weakest
  open buy (which is then displaced). A rejected symbol therefore does not
  regenerate every cycle -- it can only come back by beating a survivor.

Sells/exit proposals are never touched: they are time-sensitive and
already gated by actually holding the position.
"""

import psycopg2.extensions

DEFAULT_BUFFER = 2   # extra open buys kept beyond free slots, so there is a short menu to choose from


def free_slots(max_open_positions, position_count):
    return max(0, int(max_open_positions) - int(position_count))


def allowed_open_buys(max_open_positions, position_count, buffer=DEFAULT_BUFFER):
    """How many open BUY proposals may exist at once. 0 when there is no
    free slot at all: nothing can be approved, so nothing should sit open."""
    slots = free_slots(max_open_positions, position_count)
    return 0 if slots == 0 else slots + max(0, int(buffer))


def load_open_buys(conn):
    """[(proposal_id, score)] for open buy proposals, highest score first.
    Score is final_proposal_score, falling back to signal_score."""
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute("""
            SELECT id, COALESCE(final_proposal_score, signal_score, 0)
            FROM trade_proposals
            WHERE side='buy' AND decision IS NULL
            ORDER BY COALESCE(final_proposal_score, signal_score, 0) DESC, id ASC
        """)
        return [(r[0], float(r[1])) for r in cur.fetchall()]


def weakest(open_buys):
    """Lowest-scored (id, score); ties go to the newest id, so an older
    proposal is not displaced by a newer one of equal score."""
    return min(open_buys, key=lambda b: (b[1], -b[0])) if open_buys else None


def reject_proposal(conn, proposal_id, reason):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE trade_proposals
            SET decision='rejected', decided_at=NOW(), decided_by='system', rejection_reason=%s
            WHERE id=%s AND decision IS NULL
        """, (reason, proposal_id))
        n = cur.rowcount
    conn.commit()
    return n


def trim_surplus_buy_proposals(conn, max_open_positions, position_count, buffer=DEFAULT_BUFFER):
    """Rejects the lowest-scored open buys beyond allowed_open_buys().
    Returns [(id, symbol, score)] rejected. Caller decides what to do when
    the position count is unknown -- pass nothing rather than a guess."""
    allowed = allowed_open_buys(max_open_positions, position_count, buffer)
    open_buys = load_open_buys(conn)
    surplus = open_buys[allowed:]
    if not surplus:
        return []
    slots = free_slots(max_open_positions, position_count)
    reason = (f"Auto-rejected: {len(open_buys)} open buys exceed the {allowed} allowed "
              f"({slots} free position slot(s) + {int(buffer)} buffer); lower-scored than the ones kept")
    if slots == 0:
        reason = (f"Auto-rejected: at max_open_positions ({int(position_count)}/{int(max_open_positions)}), "
                  "no open slot to buy into")
    with conn.cursor() as cur:
        cur.execute("SELECT id, symbol FROM trade_proposals WHERE id = ANY(%s)", ([b[0] for b in surplus],))
        symbols = dict(cur.fetchall())
    rejected = []
    for pid, score in surplus:
        if reject_proposal(conn, pid, reason):
            rejected.append((pid, symbols.get(pid), score))
    return rejected

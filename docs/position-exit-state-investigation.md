# Position Exit/Protection State — Investigation

Design note, same reasoning as `docs/trade-thesis-architecture-reconciliation.md`
and `docs/risk-engine-architecture-reconciliation.md`: resolves a conceptual
collision before writing code. **No implementation in this PR — investigation
and PR-series proposal only.**

## 0. The collision

The dashboard's Positions table currently derives one boolean —
`pending_orders.length > 0` (`api/main.py::get_positions()`, ~L527-556) — and
renders it as a single badge, `⏳ sell pending`, regardless of what that open
order actually is. Four genuinely different situations get flattened into
that one signal:

1. **A broker-side resting GTC protective stop.** The position is owned,
   working normally, and *more* protected than a position with no stop at
   all. Nothing is "pending" about it — it's the steady state for every
   position bought through this app.
2. **A strategy-layer exit signal awaiting human approval.** A row in
   `trade_proposals` with `side='sell'`, `decision IS NULL`. Nothing has been
   submitted to the broker. This is a decision queued for a human, not an
   order queued at a broker.
3. **A submitted, unfilled exit.** A market sell order (from
   `POST /api/trade` or `PATCH /api/proposals/{id}` with `decision=approved`)
   that Alpaca hasn't reported as filled yet. This is the only case that is
   genuinely "sell pending" in the plain-English sense.
4. **A broker stop that has fired.** The position is gone from Alpaca's own
   `/v2/positions` the instant the stop fills — but the local `trades`
   ledger doesn't know about it until `ingest.py::reconcile_broker_stop_fills()`
   next runs (hourly cycle, 2-hour lookback). There's a window, up to ~1
   hour, where the position has actually closed but nothing in this app's
   own data says so yet.

All four currently produce the *same* UI badge (or, for case 4, produce no
badge at all and the position simply vanishes from `/api/positions` with no
explanation). This doc traces each one through code and DB state, explains
the second-order confusion it caused this session (why clicking Sell looked
like it was "waiting," why a same-day fill showed an after-hours toast, why
APP shows two different stop percentages), and proposes a PR series to give
positions an explicit state instead of inferring one from order-presence.

## 1. Case-by-case trace

### Case 1 — Resting broker-side protective stop ("Protected")

**Created**: `api/main.py::execute_trade()` (buy path) and
`decide_proposal()` (buy-approval path) attach an OTO child leg —
`order_class: "oto"`, `stop_loss: {stop_price: ...}` — to every market BUY.
The stop price comes from `_stop_price_for_order()`:

```python
def _stop_price_for_order(ref_price, planned_stop, stop_loss_pct):
    if planned_stop is not None:
        return round(planned_stop, 2)
    return round(ref_price * (1 - stop_loss_pct), 2)
```

`planned_stop` is `trade_proposals.planned_initial_stop_price` — a **dollar
amount**, computed once, at proposal-creation time, from the proposal's
`planned_entry_price` (see §3 for why this matters).

**Lives at the broker** as a `type: "stop"`, `status: "new"` order. Alpaca
holds the covered shares as `qty_available: 0` while it rests (confirmed
live — every symbol with a resting stop shows `qty_available: "0"` even
though `qty` is the full position).

**Terminates** into either case 4 (fires on its own) or gets canceled by
`cancel_resting_stop_orders()` (`api/main.py`) the moment *any* unrelated
sell — manual Sell click or proposal approval — runs for that symbol. That
function runs deliberately before the qty_available check in both
`execute_trade()` and `decide_proposal()`, precisely so a resting stop can
never block a legitimate human-initiated exit.

**What the UI shows today**: identical `⏳ sell pending` badge as cases 2/3.
**What it should show**: `Protected` (or similar) — this is an *owned*
position, not a pending sale.

### Case 2 — Strategy-layer exit signal awaiting approval

**Created** by one of five functions in `shared/signals.py`, each inserting
into `trade_proposals` with `side='sell'`, `decision=NULL`:

| Function | `exit_reason` | Trigger |
|---|---|---|
| `check_stop_losses()` | `stop_loss` | `(avg_entry - current) / avg_entry >= stop_loss_pct`, live config, against the REAL avg entry |
| `check_regime_deterioration_sell()` | `regime_deterioration` | market regime == `bear_fear` |
| `check_symbol_exits()` | `thesis_complete` | price crossed back above SMA20/BB midline |
| `check_symbol_exits()` | `time_stop` | held ~20 trading days without completing |
| `check_thesis_invalidation_sell()` | `thesis_invalidated` | PR 5/8 invalidation eval — **dark, see §4** |

Each is guarded by `_open_sell_exists()` (no duplicate proposal while one is
already pending or an order is already open) — this is also, incidentally,
why the dashboard's badge logic conflates cases 1 and 2: a resting stop
(case 1) makes `_open_sell_exists()` return true, which correctly suppresses
a duplicate proposal, but the *display* layer has no separate signal for
"there's a proposal" vs. "there's a resting stop."

`check_stop_losses()`'s own docstring says it explicitly: *"the broker will
have already closed a breached position well before this function's hourly
poll even runs... it will usually just find nothing to do."* It is designed
to be a rare backup path. The APP proposal that triggered this whole
investigation firing was itself informative — see §3.

**Terminates** via `PATCH /api/proposals/{id}` (approve → case 3, or reject →
`decision='rejected'`, no order ever touched).

**What the UI shows today**: same `⏳ sell pending` badge, keyed off
`pending_orders` (open broker orders) — but a case-2 proposal with no
resting stop underneath it (e.g. a fresh `thesis_complete` proposal on a
position with no stop leg at all, pre-dating "Execution: protective stop
orders") produces **no badge whatsoever**, even though it's the one case
most directly requiring the user's attention.

### Case 3 — Submitted, unfilled exit ("genuinely Sell pending")

**Created** the instant `execute_trade()` or `decide_proposal()` (approve
path) POSTs a market sell order. Both paths call `cancel_resting_stop_orders()`
first (killing any case-1 order for that symbol), then submit
`{"type": "market", "side": "sell", "time_in_force": "gtc"}`.

**Lives** for however long Alpaca takes to fill it — normally under two
seconds during market hours (verified live this session: APP and DTE sells
both filled ~1s after submission). The `booked_for_next_open` toast bug
(fixed this session, PR #67) came from exactly this window: the code used to
treat the order's synchronous non-`filled` POST response as proof of
after-hours deferral, when it was actually just this brief in-flight state.

**Terminates** into a `filled` trade row (background task
`_reconcile_fill()`) or, rarely, `canceled`/`rejected`.

**What the UI shows today**: same badge as 1 and 2, but this is the *only*
one of the four where "sell pending" is actually the correct plain-English
description.

### Case 4 — Triggered broker stop, not yet reconciled ("Closing")

**Created** when Alpaca autonomously fires a resting case-1 stop leg. This
happens entirely at the broker — no `api/main.py` code runs.

**Lives** in a genuine local-data gap: Alpaca's `/v2/positions` drops the
symbol immediately (so `/api/positions` stops returning it — the position
just vanishes from the dashboard with no state shown at all, not even a
"closing" placeholder), but the local `trades` table has no row for the
fill until `ingest.py::reconcile_broker_stop_fills()` next runs. That
function polls `GET /v2/orders?status=closed` over a 2-hour lookback,
filters for `type in (stop, stop_limit)` + `side=sell` + `filled_qty > 0`,
and backfills a `trades` row with `source='broker_stop'`, `notes='Broker-
initiated protective stop-loss fill, auto-reconciled (no proposal/
approval)'`. It runs once per ingest cycle (hourly), so this gap is normally
well under an hour but is not instantaneous.

**Terminates** into a normal `filled` trade row, then a closed
`position_lifecycles` row via the existing lifecycle-builder.

**What the UI shows today**: nothing. The position disappears from
Positions with no visible state. Trade History / P&L won't reflect it until
the next `reconcile_broker_stop_fills()` cycle. This is the state the user's
target mockup calls `Closing — stop triggered, awaiting fill` — it doesn't
exist as a rendered state anywhere right now.

## 2. Where the collapse actually happens

Single point of collapse: **`api/main.py::get_positions()`**, which annotates
each Alpaca position with `pending_orders` (all open orders at Alpaca for
that symbol, regardless of `type`), and **`dashboard.html::renderPositions()`**,
which reads `pending_orders[0]` and renders one badge whether that order is a
resting `stop` (case 1) or a submitted `market` sell (case 3). Case 2 has no
representation in `/api/positions` at all — it lives only in
`trade_proposals`, fetched separately by the Proposals section of the
dashboard, with no cross-reference back to the Positions table. Case 4 has no
representation anywhere until reconciled.

This is exactly the bug class the user named: presence of *any* open order
is being used as a proxy for "something is pending," when the four things
that can produce an open order (or its absence, for case 4) have unrelated
meanings.

## 3. Why APP shows ~12% and ~16% — traced, not guessed

Live data (`trades`, `trade_proposals`, ids 80/94/87/100):

| | Value | Source |
|---|---|---|
| Proposal `#94` (BUY) `planned_entry_price` | $334.24 | price at proposal-creation time, 2026-08-06 |
| Proposal `#94` `planned_initial_stop_price` | $294.14 | `$334.24 × (1 - 0.12)` — **12% below the planned entry**, `stop_loss_pct=0.12` from `signal_params` |
| Trade `#80` (BUY fill) `price` | $350.21 | actual fill, 2026-08-08 — **two days after** the proposal, price gapped up before it filled |
| Trade `#80` `initial_stop_price` | $294.14 | copied verbatim from the proposal's `planned_initial_stop_price` — **never recomputed against the actual fill price** |
| Broker resting stop order | $294.14 | same dollar figure, attached to the BUY order at fill time |
| Today's proposal `#100` (`stop_loss`) | triggered at $308.15, i.e. `$350.21 × (1 - 0.12)` | `check_stop_losses()`, 2026-08-12, computed live against `avg_entry=$350.21` (the REAL fill price) |

**`stop_loss_pct` never changed — it's 0.12 in both computations.** The
discrepancy is entirely that the broker stop is a **frozen dollar amount**
anchored to the *planned* entry price at proposal time, while
`check_stop_losses()` recomputes the percentage **live, against the actual
average entry price** every cycle. APP's proposal-to-fill gap (2 days, price
moved from $334.24 to $350.21 — a 4.8% move) is what turned "12% below
where we planned to buy" into "16% below where we actually bought." This is
a real, identifiable gap in `_stop_price_for_order()` / the buy-approval
path: `planned_initial_stop_price` is used as-is even when the eventual
fill price differs materially from `planned_entry_price`. It is not a
config-drift issue and not a bug in `check_stop_losses()` — both numbers are
individually correct for what they each measure; they measure different
things (intent-at-proposal-time vs. reality-at-fill-time), and nothing
currently reconciles them once a fill price diverges from the plan.

**Per your instruction, this doc does not propose unifying the two.** They
are legitimately different concepts — a broker-side worst-case backstop
sized to what was *planned*, and a live evaluation against what actually
happened — and collapsing them would remove information (the "how much did
reality already drift from the plan" signal), not add it. What's actually
missing is *visibility* — the drift is currently invisible until, as
happened here, a signal-layer check quietly fires 4 points inside a stop
that looked like it had more room than it did. See PR D below.

## 4. Hypothesis-Driven Trading epic — is this a second exit-state framework?

Checked against the live DB, not just the merged code, because the two
currently disagree:

- **`trade_theses` and `trade_thesis_evaluations` do not exist on the live
  Postgres instance** (`to_regclass('public.trade_theses')` returns `NULL`).
  `trades`/`trade_proposals` also don't yet have `trade_thesis_id` columns
  live, despite the epic doc recommending PR 1 add them. PR 1-12 merged into
  `main` (2026-08-10) but the live TrueNAS DB has not been migrated —
  `schema.sql` auto-applies on `invest-ingest` restart per the existing
  deploy convention, so either that restart hasn't happened since the merge,
  or the new tables aren't yet in `schema.sql`'s idempotent-apply path.
  **Worth a fast follow-up to confirm before anything in Phase 4+ is
  considered "live."**
- **All three epic feature flags are off** (`signal_params` has no rows for
  `trade_thesis_instantiation_enabled`, `thesis_invalidation_exit_enabled`,
  `structure_aware_stop_enabled` — all default to 0). So even once migrated,
  `check_thesis_invalidation_sell()` (`exit_reason='thesis_invalidated'`)
  and the structure-aware stop resolver are dark. Every exit that has fired
  on this account so far is `stop_loss`, `thesis_complete`, or a manual
  approval — the pre-epic paths.
- **The epic's state machine and this investigation's state machine are
  different layers, not competitors.** `trade_theses.status`
  (`proposed/active/weakening/invalidated/completed/superseded`, per
  `docs/trade-thesis-architecture-reconciliation.md` §1) answers *"is the
  original hypothesis for this trade still true?"* — a judgment about
  evidence and strategy. What this investigation is about — Protected /
  Exit recommended / Sell pending / Closing — answers *"what is the current
  broker-execution state of the shares?"* A thesis can be `invalidated`
  (strategy says get out) while the position is still `Protected` (no order
  submitted yet) or already `Closing` (stop fired) — genuinely orthogonal
  axes. **Recommendation: do not merge them into one enum.** The position
  execution-state PR series below should reuse `exit_reason` (already
  shared vocabulary — `stop_loss`/`thesis_complete`/`time_stop`/
  `regime_deterioration`/`thesis_invalidated`) as the *reason* a case-2 or
  case-3 state exists, but the state itself (`protected`/`exit_recommended`/
  `sell_pending`/`closing`/`owned`) is a new, small, orthogonal concept that
  the epic doesn't define and shouldn't need to.

## 5. Proposed PR series

Small, sequenced, no threshold unification (per your instruction), building
toward the target UI:

```
APP — Exit recommended
P/L -12.4% · hard stop $294.14 (-4.1% away)

AEP — Owned · Protected
P/L -0.4% · hard stop $109.88 (-11.6% away)

XYZ — Sell pending
Exit approved 10:32 AM · broker order submitted

ABC — Closing
Stop triggered · awaiting fill
```

**PR A — `position_execution_state()`, pure function, no DB/UI change.**
New `shared/position_execution_state.py`: given a position, its open
Alpaca orders, and any open `trade_proposals` row for that symbol, returns
one of `owned` / `protected` / `exit_recommended` / `sell_pending` /
`closing`. Pure classification, same "compute once, tested standalone"
shape as `market_structure.py`. Ships dark — computed and tested, not
wired into any endpoint yet. This is the PR that actually resolves the
four-cases-into-one-badge collapse from §2, by making the four cases first-
class return values instead of an inferred boolean.

- `owned`: no stop attached, no proposal, no open order (legacy positions
  bought before "Execution: protective stop orders," or a stop that failed
  to attach — worth its own badge, since it's the one case with *no*
  protection at all).
- `protected`: exactly a case-1 resting stop, no case-2/3 activity.
- `exit_recommended`: an open case-2 `trade_proposals` row
  (`decision IS NULL`), regardless of whether a case-1 stop also happens to
  be resting underneath it.
- `sell_pending`: a case-3 submitted, unfilled sell order at Alpaca.
- `closing`: reserved for case-4 — see PR B, this can't be detected from
  `/api/positions` alone since Alpaca has already dropped the position by
  the time this state would apply.

**PR B — Surface case 4 instead of silently dropping it.** Currently
nothing shows a `closing` position because it's gone from Alpaca's
`/v2/positions` response entirely. Add a short-lived local marker: when
`reconcile_broker_stop_fills()` (`ingest.py`) or a new lightweight check
finds a `filled` stop order at Alpaca with no corresponding `trades` row
yet, that's exactly the `closing` window — expose it (e.g. a small
`closing_positions` table or reusing `trades` with an interim `status`)
so `/api/positions` can synthesize a `closing` row for a symbol that's
disappeared from Alpaca but hasn't finished reconciling locally, instead of
that symbol just vanishing. Smallest-footprint approach: worth scoping
precisely in its own PR rather than guessing schema here.

**PR C — Wire `position_execution_state()` into `/api/positions` +
dashboard.** `GET /api/positions` gains a `state` field per position (from
PR A) plus the underlying data the UI needs to render your target mockup:
protective stop price + distance-to-stop (already computable from data
`/api/positions` already returns — `current_price` vs. the resting order's
`stop_price`, just not surfaced today), and for `exit_recommended`, the
triggering proposal's rationale/exit_reason. Dashboard: keep the sortable
Status column from PR #66/#67 (per your note — it's the right shape), but
replace the pending-badge string with a state-driven render:
`Owned` / `Owned · Protected` / `Exit recommended` / `Sell pending` /
`Closing`, each with the one-line detail from the mockup. `statusSortVal`
becomes state-ordinal-first, then time-in-state, so sorting groups by
urgency (Closing/Exit recommended first) rather than just by pending-since
timestamp.

**PR D — Surface stop-drift, don't unify it.** Per §3: expose, for any
`protected` or `exit_recommended` position, the delta between the resting
broker stop's implied percentage-from-actual-entry and the live
`stop_loss_pct` config — e.g. "protected at -16.0% from actual entry (stop
was sized off a $334.24 planned entry, filled at $350.21)." Purely
informational, no behavior change, no threshold changes. This is what
would have made APP's situation legible three days ago instead of only
surfacing as a same-day stop_loss proposal.

**PR E (separate, flagged as a fast follow, not blocking A-D) — Confirm
Hypothesis-Driven Trading epic DB state.** Check whether
`ingest/schema.sql` actually contains `trade_theses`/
`trade_thesis_evaluations`/the `trade_thesis_id` columns post-merge, and if
so, whether `invest-ingest` has been restarted since 2026-08-10 to apply
them. If the schema is present but unapplied, this is a one-restart fix; if
missing from `schema.sql` entirely, that's its own bug report against PR
1/9's stated scope. Not part of the exit-state work, but adjacent enough
that it should be resolved before Phase 4+ of that epic is assumed to be
live.

## 6. Explicitly not doing here

- Not unifying the 12%/16% (or any) stop thresholds — §3, per your
  instruction.
- Not building a second thesis/invalidation state machine — §4.
- Not implementing anything above — this doc is the trace + proposal only.

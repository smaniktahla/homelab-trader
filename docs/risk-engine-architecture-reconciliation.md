# Risk Engine — Architecture Reconciliation

**Status**: Accepted (compatibility phase complete, PR 1 scoped)
**Date**: 2026-08-04
**Purpose**: Pre-implementation inventory, overlap analysis, and integration plan for a
portfolio risk engine, produced before writing any risk-engine code, per the ownership
model and compatibility constraints supplied for this work.

All facts below were verified directly against the repository (`main` at `c6489d1`) and
the live production DB (ubuntu-box, 10.10.10.13) on 2026-08-04 — file paths, line-level
behavior, live `signal_params` values, and live row counts, not recalled from memory or
from prior session handoffs.

---

## 0. A load-bearing correction to the premise

The brief asks to "pay particular attention to the hierarchical sector-regime PR." **No
such PR exists.** Checked exhaustively: `gh pr list --state all` (28 PRs, #1–#28, all
merged or the two currently-open-none), full-history `git log --all --oneline --grep`,
and a repo-wide grep for `sector_regime`, `stock_regime`, `regime_hierarchy`,
`hierarchical` — zero hits anywhere in code, docs, commit messages, or branch names.

What *does* exist is **market-level** regime only: `ingest/market_regime.py`
(SPY/QQQ/VIX → one `overall` bucket, e.g. `bull_volatile`) plus the `market_context`
(single live row) and `market_regime_history` (per-day, shipped today, PR #27) tables. There
is no sector-level regime, no stock-level regime, and no hierarchy connecting them. Sector
*mapping* exists (`universe.sector`, flat GICS text) but sector *regime* — a
bullish/bearish/neutral classification per sector — does not.

This changes the shape of the integration problem: there is no existing sector/stock
regime service to reuse or conflict with. The regime-analysis layer as specified (market +
sector + stock regime, relative strength, hierarchy alignment) is **mostly unbuilt**. Only
its market-level tier and the relative-strength-vs-SPY calculation exist today. I'm
treating this as ground truth for the rest of this document, not as something to route
around quietly — see §C for why this matters for scope.

---

## A. Existing component inventory

| Concern | File(s) | Current ownership |
|---|---|---|
| Sector mapping | `ingest/scanner.py` (writes `universe.sector`), `shared/signals.py::load_sector_map()` | Flat GICS text per symbol, no hierarchy, no regime |
| Market regime | `ingest/market_regime.py`, `shared/market_regime_history.py` | `classify_overall()` (pure), live single-row + per-day history. **No sector/stock tier.** |
| Relative strength | `shared/signals.py::relative_strength_vs_spy()` (lines 209–218) | 20-day return vs SPY, used only as a score modifier in `score_signal()` — not persisted, not a regime concept |
| Proposal generation | `shared/signals.py::compute_signals()` (lines 657–891) | Strategy layer today — RSI/BB scoring, sizing, gating, and DB insert all in one function |
| Proposal scoring | `shared/signals.py::score_signal()` (lines 269–361) | Strategy layer — 0–100 score, regime-adjusted via `score_modifier` |
| Position sizing | `shared/signals.py::calc_buy_qty()` (lines 388–416) | Strategy layer computes final `qty`; reused as-is by the Monte Carlo backtest (already shared) |
| Stop-loss creation | `shared/signals.py::check_stop_losses()` (lines 522–547); `planned_initial_stop_price` written in `compute_signals()` (lines 850–858) | Strategy layer computes a *planned* stop (a snapshot of `stop_loss_pct`); **no broker-side protective order is ever submitted** — see §C |
| Stop-loss management | `shared/signals.py::check_stop_losses()` re-evaluates live price vs. entry every ingest cycle | Strategy layer, polling-based, generates a SELL **proposal**, not an order |
| Profit targets / exits | `shared/signals.py::check_symbol_exits()` (thesis_complete, time_stop), `check_regime_deterioration_sell()` | Strategy layer, all proposal-generating, none order-submitting |
| Position sizing persistence | `trade_proposals.qty`, `trade_proposals.planned_*` (schema.sql:498–502) | Written once at proposal time by the strategy; **no separate `approved_quantity` field exists anywhere** |
| Portfolio exposure limits | `shared/signals.py::calc_buy_qty()` (`max_position_pct`), `sector_cap_block_reason()` (line 475) | Strategy layer, both pure functions operating on live Alpaca positions |
| Circuit breaker | `shared/circuit_breaker.py` | Single boolean, drawdown vs. all-time high-water mark, buys only |
| Trade outcome tracking | `shared/position_lifecycles.py`, `shared/expectancy.py`, `shared/lifecycle_performance.py`, `ingest/build_position_lifecycles.py`, `ingest/outcomes.py` | Position-tracking + outcome layers, already well-separated (see §A.1 below) |
| Order/fill reconciliation | `api/main.py::_reconcile_fill()` (line 761) | Execution layer, background polling of Alpaca order status |
| Human proposal approval | `api/main.py::decide_proposal()` (line 909) | Execution layer — this is also where `qty` can be silently overridden, see §C |
| Backtesting | `ingest/research/backtests/backtest_portfolio_montecarlo.py` | Reuses `calc_buy_qty`, `sector_cap_block_reason`, `classify_overall` directly from the shared/live modules — **already** the shared-domain pattern §7 of the brief asks for, for what exists today |
| Broker integration | `shared/signals.py::fetch_alpaca_portfolio()`, `api/main.py::alpaca()` | Thin REST wrapper, market orders only, no bracket/OCO |
| Earnings blackout | `shared/earnings.py::earnings_blackout_reason()` | Standalone gate, reused identically by both the automated pipeline and `rule_adherence.py` |
| Strategy score calibration | `ingest/postmortem.py`, `ingest/research/backtests/backtest_score_calibration.py` | Outcome/journal layer, operates on `signal_outcomes`, advisory only |
| Rule-adherence / bypass detection | `shared/rule_adherence.py`, `rule_adherence_checks` table | **Advisory only, never blocks** — re-checks the same 6 gates `compute_signals()` enforces, against manual trades / approvals, purely for audit |

### A.1 The outcome/journal layer is already well-factored

This is worth stating plainly because the brief's "avoid parallel `trade_result` /
`trade_outcome` / `journal_outcome`" warning is largely **already satisfied**:

- `position_lifecycles` (table) + `shared/position_lifecycles.py` (pure FIFO matcher) is
  the one position-tracking/outcome source of truth. Built by
  `ingest/build_position_lifecycles.py`, idempotent, truncate-and-rebuild from the
  append-only `trades` ledger.
- `shared/expectancy.py` computes trade-level expectancy **directly from
  `PositionLifecycle` rows** — it does not create a second outcome table.
- `shared/lifecycle_performance.py` is the read-side/API summary layer, including
  broker-vs-local reconciliation status (`_reconciliation_status()`, line 116) — this is
  literally the "position tracker owns broker-vs-local reconciliation" responsibility
  from the spec, already built.
- `ingest/postmortem.py`'s own `bucket_stats` is a **different, intentionally separate**
  concept (hypothetical signal-level forward returns, large N) from
  `expectancy.py`'s real trade-level P&L (small N) — documented as such in
  `expectancy.py`'s own docstring, not an accidental duplicate.

The existing lifecycle chain is close to the brief's preferred shape:

```
trade_proposals → (fill) → trades → position_lifecycles → expectancy / lifecycle_performance
```

It's missing exactly two links the brief's target shape has: `RegimeSnapshot` (a
formal, referenced snapshot row — today `signal_outcomes.market_regime`/`symbol_regime`
are plain text columns, not a foreign key to anything) and `RiskDecision` (does not exist
at all).

---

## B. Overlap matrix

| New requirement | Existing component | Action |
|---|---|---|
| Sector mapping | `universe.sector` (flat GICS) | **Reuse** — no changes needed |
| Sector regime | *(nothing)* | **Add**, out of scope for PR 1 (see §F) — no existing service to conflict with |
| Market regime snapshot | `market_context` / `market_regime_history` | **Extend** — risk engine reads `market_context.overall` at decision time but must not act on it (§1) |
| Relative strength | `shared/signals.py::relative_strength_vs_spy()` | **Reuse** — already a pure, importable function |
| Approved quantity | `trade_proposals.qty`, computed by `calc_buy_qty()`, silently overridable via `ProposalDecision.qty` | **Refactor** — see §C.1, this is the single largest conflict |
| Per-trade risk budget | *(nothing — `stop_loss_pct` implies a % risk but nothing computes a $ budget)* | **Add** |
| Position allocation limit | `calc_buy_qty()`'s `max_position_pct` cap | **Refactor into risk engine; reuse the cap value from `signal_params`** |
| Portfolio open risk | *(nothing)* | **Add** — no existing concept of summed risk-dollars across open positions |
| Sector exposure limit | `sector_cap_block_reason()` | **Refactor into risk engine** — logic reused verbatim, ownership moves |
| Buying-power / liquidity | `calc_buy_qty()`'s `cash` clamp only; no liquidity/volume check anywhere | **Extend** for buying power (reuse the clamp), **Add** for liquidity |
| Circuit breaker | `shared/circuit_breaker.py`, single boolean | **Extend** — becomes one input into the aggregated permission object (§C.4), not replaced |
| Trading-permission aggregation | *(nothing — one ad hoc boolean, computed twice independently, see §C.4)* | **Add** |
| Stop price | `trade_proposals.planned_initial_stop_price` (PR A, 2026-08-03) | **Reuse** — risk engine consumes this for R-budget math, does not compute its own |
| Protective stop order | *(nothing — no bracket/OCO ever submitted)* | **Add**, out of scope for PR 1 (execution-layer work, see §F) |
| Risk decision persistence | *(nothing)* | **Add** — new `risk_decisions` table |
| Trade outcome / R accounting | `position_lifecycles`, `expectancy.py` | **Reuse** — R-multiple math already exists (`realized_r`), risk engine's `approved_quantity`/budget just becomes another input available to it |
| Backtest sizing | `backtest_portfolio_montecarlo.py` already imports `calc_buy_qty` directly | **Replace** the *call site* (import the new shared risk-decision function instead of `calc_buy_qty` directly) — the shared-domain pattern itself needs no invention, it already exists and works |
| Rule-adherence checks | `shared/rule_adherence.py`, advisory-only | **Deprecate the sizing-specific parts** once the risk engine is authoritative (see §E) — earnings/cooldown/max-open-positions checks stay, since those aren't quantity/exposure concerns |

---

## C. Conflicts and risks — called out plainly

### C.1 Quantity is already calculated/overridden in three independent places, with no binding check

This is the conflict the brief is most worried about, and it is real, live, in production
today:

1. **`shared/signals.py::calc_buy_qty()`** — the automated pipeline computes `qty` once at
   proposal time and writes it to `trade_proposals.qty`.
2. **`api/main.py::decide_proposal()` line 923**: `trade_qty = body.qty or p["qty"]` — a
   human approving a proposal can pass an **arbitrary `qty` in the HTTP request body**,
   completely replacing the strategy's sized value. The only check afterward is
   `rule_adherence.check_gates()`, which is explicitly advisory (module docstring:
   "purely advisory: it never blocks anything, only records"). A human can approve 10x
   the intended size and the system will log a violation *after the Alpaca order is
   already filled*, not before.
3. **`api/main.py::execute_trade()` (`POST /api/trade`)** — fully manual `qty`, no
   calculation at all, same advisory-only post-hoc check.

The backtest is the one place this is *already* correctly unified — it imports
`calc_buy_qty` directly from `signals.py`, so live and historical sizing agree for the
automated path. The conflict is entirely in the **human-override paths**, which the
backtest has no equivalent of.

**Resolution**: `trade_proposals` needs a `risk_decisions` row per proposal (see §D), and
`decide_proposal()`/`execute_trade()` must clamp any human-supplied `qty` to
`risk_decision.approved_quantity` rather than trusting it verbatim. This is a real behavior
change to a live, human-in-the-loop endpoint — flagged for explicit confirmation before
merge, not something to silently tighten.

### C.2 No protective stop order ever reaches the broker

`api/main.py`'s only order submission (`execute_trade()` line 811, `decide_proposal()`
line 946) is a single plain market order: `{"type": "market", "time_in_force": "gtc"}`.
There is no bracket order, no OCO, no separate stop order submitted at fill time. The
"stop-loss" the system relies on is `shared/signals.py::check_stop_losses()` — an hourly
poll that compares live price to entry and, if breached, creates a **sell proposal**
requiring a human to approve it before anything actually executes.

This means the existing `planned_initial_stop_price` (PR A) is informational only — it
does not correspond to any live protective order at the broker. If the ingest cycle is
down, or a human doesn't approve the resulting sell proposal promptly, a position can
lose materially more than `stop_loss_pct` before anything happens. This is a real,
current gap, not something the risk engine PR introduces or need fix — but it means the
risk engine **must not represent `planned_initial_stop_price` as an active protective
stop** in any UI or reporting it touches. Flagging per the brief's own instruction: "If
existing code violates this separation, document the current behavior and propose a
controlled refactor" — the controlled refactor (actual bracket/OCO submission) is
execution-layer work, explicitly out of scope for PR 1 (see §F), but must be named here
so it isn't silently assumed already solved.

### C.3 Regime-score and regime-size adjustments are already both applied today — the exact pattern the brief prohibits

`ingest/market_regime.py::classify_overall()` returns *both* `score_modifier` (raises
`score_proposal_min`) *and* `alloc_modifier` (multiplies `trade_allocation_pct`) for the
same regime bucket. `shared/signals.py::compute_signals()` applies **both**
simultaneously (lines 664–665, 679–680): a `bear_calm` regime both raises the score bar
*and* shrinks position size, live, today.

This is precisely what compatibility constraint §1 says not to do going forward. It
predates this reconciliation and is deliberate, documented, tested behavior (not a bug) —
ripping it out is a live-behavior change with its own blast radius, unrelated to risk
sizing math itself. **Resolution for PR 1**: leave `alloc_modifier` exactly as-is
(untouched, not part of this PR), and ensure the new risk engine's own per-trade risk
budget and `approved_quantity` calculation **take zero regime input** — no
`market_context` read anywhere in the risk engine's core sizing path. The overlap is
real but contained: `alloc_modifier` already shrinks what the *strategy* proposes before
the risk engine ever sees it; the risk engine then evaluates that (already-shrunk)
proposal on pure exposure/budget terms. This avoids compounding the existing
regime-linked reduction with a second, new one, without touching the pre-existing
mechanism in the same PR. Worth a follow-up ticket once the risk engine exists, not
resolved here.

### C.4 Circuit breaker permission logic is already duplicated once

`shared/circuit_breaker.py::record_snapshot_and_check()` computes
`drawdown_pct >= threshold`. `shared/rule_adherence.py::check_gates()` (lines 89–91)
**hand-copies the same formula** rather than calling a shared predicate, because it needs
the boolean without also inserting a `portfolio_snapshots` row. This is a small,
contained duplication today (one boolean, two call sites), but it's the same failure
mode the brief warns about for live/backtest — here it's live/live. **Resolution**:
`circuit_breaker.py` gets a new pure `is_breached(drawdown_pct, threshold)` (or similar)
that both `record_snapshot_and_check()` and `check_gates()` call, and the new
trading-permission aggregator (§D) becomes the third and last caller. Small, contained
fix, bundled into PR 1 since the aggregator needs it anyway.

### C.5 `theses.config` vs `signal_params` — a pre-existing, never-finished migration

Migration 001 (2026-07-13) copied `signal_params` into `theses.config` and left a TODO to
repoint reads there, "not done here." Confirmed still true today: `load_params()` reads
`signal_params` directly; `theses.config` has never been read by application code.
**This matters for where risk-policy config lives** (§F) — extending the already-flat,
already-global `signal_params` table is lower-risk than reviving the
half-finished `theses.config` migration inside this PR. Recommendation: extend
`signal_params` (reuse), leave the `theses.config` migration as a separate, pre-existing,
unrelated TODO — not this PR's problem to finish.

### C.6 No sector/stock regime hierarchy to build against (restated from §0)

Because this doesn't exist, there is no schema, no snapshot format, and no migration
plan to reconcile against for that half of the "regime-analysis layer" as specified. This
is not a conflict in the sense of two implementations disagreeing — it's an absence. I'm
scoping it out of PR 1 entirely (§F) rather than inventing a hierarchy under time pressure
inside a risk-engine PR; building it properly (with its own regime-significance testing,
per this codebase's established discipline — see `backtest_rule_significance.py`'s
precedent for `global_market_signals`) deserves its own PR.

### C.7 Existing tests likely affected

- `tests/test_rule_adherence.py` / `tests/test_api_rule_adherence.py` — if
  `decide_proposal()`/`execute_trade()` start clamping to `risk_decision.approved_quantity`
  (§C.1's resolution), the advisory-only framing in these tests' assertions
  (`any_violation` can be true and the trade still succeeds) needs revisiting for the
  buy/sizing-specific checks, since those become binding. Earnings/cooldown/max-open
  checks stay advisory as they are today.
- `tests/test_fixture_equivalence*.py` — these assert `compute_signals()`'s proposal
  creation path is byte-for-byte unchanged pre/post a given PR. PR 1 must either leave
  `compute_signals()`'s proposal-creation output unchanged (risk decision computed
  *after* proposal insert, as an additional row) or explicitly update these fixtures with
  a documented, reviewed diff — not let them silently drift.
- No test currently exercises `decide_proposal()`'s `body.qty` override path at all
  (confirmed via grep) — this is itself a coverage gap independent of the risk engine,
  worth closing as part of PR 1 since that's exactly the code path being changed.

### C.8 No blocker from an unmerged/incomplete prior PR

Confirmed via `gh pr list --state all`: 28 PRs, all merged, zero open. `main` is clean,
matches the deployed state on ubuntu-box. There is no pending-PR ordering issue.

---

## D. Proposed final architecture

```
Strategy (signals.py)
   → Proposal (trade_proposals, unchanged shape + planned_* fields, already exist)
   → Regime Snapshot (market_context.overall read at decision time — NOT stored as a
                       new table in PR 1; see below)
   → Risk Decision (NEW: shared/risk_engine.py + risk_decisions table)
   → Execution Plan / Broker Orders (api/main.py, unchanged order construction in PR 1)
   → Fills (existing _reconcile_fill)
   → Position (existing position_lifecycles, unchanged)
   → Trade Outcome (existing expectancy.py / lifecycle_performance.py, unchanged)
```

**Regime Snapshot**: the brief wants the risk engine to consume an "immutable
proposal-time regime snapshot" rather than recalculating. Today, `signal_outcomes`
already stores `market_regime`/`symbol_regime` as plain text at signal-generation time —
this *is* the closest existing thing to a regime snapshot, it just isn't formally named
or foreign-keyed. For PR 1, the risk engine reads `market_context.overall` fresh at
decision time (a few seconds to minutes after signal generation, same cycle) rather than
joining back through `signal_outcomes` — simpler, and per §C.3 the risk engine doesn't
act on this value anyway, it's read for **audit/reporting only** (stored on the
`risk_decisions` row so a human can see what regime was active, never used in the budget
math). A formal `proposal_regime_snapshot` table is real, useful, future work (ties into
the sector/stock regime hierarchy from §C.6) — not necessary to unblock PR 1's actual
scope.

**Ownership after PR 1**:

| Layer | Owns | Existing / New |
|---|---|---|
| Strategy | Signal generation, side, entry trigger, stop price, targets, score, requested qty (informational) | Existing (`signals.py`), unchanged |
| Regime analysis | Market regime only (sector/stock regime not built) | Existing (`market_regime.py`), unchanged |
| **Portfolio risk** | **Per-trade risk budget, approved_quantity, position/sector/portfolio exposure limits, buying-power/liquidity checks, approval/reduction/rejection + reasons, risk_decisions persistence** | **NEW — `shared/risk_engine.py`** |
| Execution | Order construction/submission, fill polling | Existing (`api/main.py`), **consumes `risk_decision.approved_quantity`** instead of `trade_proposals.qty`/`body.qty` directly |
| Position tracking | Filled qty, avg entry, lifecycle state, broker-vs-local reconciliation | Existing (`position_lifecycles.py`), unchanged |
| Outcome/journal | Realized P&L, R, MAE/MFE, expectancy | Existing (`expectancy.py`, `lifecycle_performance.py`), unchanged |

---

## E. Migration and compatibility plan

**New tables**:
- `risk_decisions` — one row per proposal at decision time. Columns: `id`, `proposal_id`
  (FK, unique — one decision per proposal, re-decisions create a new row if the proposal
  is re-evaluated, not an update), `symbol`, `side`, `requested_qty` (copied from
  `trade_proposals.qty`, informational), `approved_quantity`, `outcome` (`approved` |
  `reduced` | `rejected`), `risk_budget_dollars`, `binding_constraint` (text — which
  single limit determined the outcome, e.g. `sector_cap`, `portfolio_open_risk`,
  `buying_power`), `constraint_detail` (jsonb — every limit checked and its value, not
  just the binding one, for audit), `market_regime_at_decision` (text, read from
  `market_context.overall`, audit-only per §D), `decided_at`. No FK to a regime-snapshot
  table in PR 1 (§D).

**New columns**: none on existing tables. `trade_proposals.qty` keeps its current
meaning (strategy-requested quantity) — **not silently repurposed**, per the brief's own
explicit warning against that. `risk_decisions.approved_quantity` is the new,
additional, authoritative field.

**Existing fields retained as-is**: `trade_proposals.planned_*` (PR A), `signal_params`
(extended with new risk-policy keys, not replaced), `market_context`/
`market_regime_history` (read-only consumer, no schema change).

**Backfill**: none needed — `risk_decisions` is forward-only, same precedent as
`rule_adherence_checks` and `position_lifecycles.realized_r` (both explicitly
"forward-looking only, no retroactive reconstruction" in their own docstrings/comments).

**Deprecation**: `rule_adherence.py`'s position-sizing and sector-cap checks become
redundant once `decide_proposal()`/`execute_trade()` clamp to
`risk_decision.approved_quantity` — but not removed in PR 1. They stay as a second,
independent verification during the transition (belt-and-suspenders), formally
deprecated in a later PR once the risk engine has run in production long enough to trust.
The earnings/cooldown/max-open-positions checks in the same module are unaffected and
stay permanently (those aren't risk-engine concerns).

**API versioning**: no existing `/api/proposals` or `/api/trade` response shape changes
in a breaking way — `risk_decision` fields are additive to the proposal response
(`GET /api/proposals`), and `decide_proposal()`'s request/response shape is unchanged
(the clamping happens server-side, transparently, with the actual `approved_quantity`
surfaced in the response so the dashboard can show if/when a request was reduced).

**Configuration migration**: new `signal_params` keys (see §F) added via the same
`INSERT ... ON CONFLICT DO NOTHING` pattern every prior PR has used — no separate config
store.

---

## F. Recommended PR decomposition

The brief's own suggested 3-PR split matches this codebase's architecture well — no
changes recommended to that shape. Confirming scope concretely against what actually
exists:

### PR 1 — Core risk engine and position sizing (this PR)
- `signal_params` new keys: `max_portfolio_open_risk_pct`, `risk_per_trade_pct` (or
  equivalent — exact param names decided during implementation, matching this
  codebase's existing naming style)
- `shared/risk_engine.py` — pure functions: `evaluate_proposal(...)` → risk decision
  dict, reusing `sector_cap_block_reason()`, `load_sector_map()`, `calc_buy_qty()`'s cash
  clamp, `circuit_breaker.is_breached()` (new, §C.4)
- `risk_decisions` table + persistence
- Hook into `compute_signals()`: after a proposal is inserted (not instead of), call the
  risk engine and insert the corresponding `risk_decisions` row — proposal-creation
  output itself unchanged, satisfying `test_fixture_equivalence*.py` (§C.7)
- `decide_proposal()`/`execute_trade()`: clamp any qty to `approved_quantity` — **this
  is the one behavior-changing piece**, called out for explicit review
- `backtest_portfolio_montecarlo.py`: swap its direct `calc_buy_qty` import for the new
  risk-engine entry point, so live and backtest share the exact same sizing decision
  going forward, not just the position-sizing sub-piece
- Tests: new `tests/test_risk_engine.py` (pure-function table, same style as
  `test_market_regime_history.py`'s `classify_overall` branch table), plus updates to
  `tests/test_rule_adherence.py`/`test_api_rule_adherence.py` and
  `test_fixture_equivalence*.py` per §C.7

**Excluded from PR 1** (confirmed nothing existing conflicts, safe to defer):
sector/stock regime hierarchy (§C.6, doesn't exist, own PR), protective bracket/OCO
order submission (§C.2, execution-layer, own PR), MAE/MFE dashboards (already exist at
the position level, no risk-engine-specific work needed), Kelly/correlation-matrix
optimization, automated liquidation, `alloc_modifier` cleanup (§C.3, flagged not fixed).

### PR 2 — Trade lifecycle and R accounting
Already substantially built (`position_lifecycles`, `expectancy.py`) — this PR's
remaining scope, if any, is thin: wiring `risk_decisions.risk_budget_dollars` into
`expectancy.py`'s existing R-multiple math as an alternate risk-budget source (today R
comes from `actual_initial_risk_dollars`, derived from the stop price only — a planned
risk *budget* that didn't match the stop-implied risk would be a new, useful signal).
Not blocking PR 1.

### PR 3 — Safeguards and analytics
Trading-permission aggregation (§C.4's `is_breached` becomes the seed of a real
aggregator object, per the brief's `new_entries_allowed`/`scope`/`reasons` shape),
drawdown-based sizing, loss-streak controls, journal analytics, expanded backtest
comparisons. Also where `alloc_modifier`'s regime-coupling (§C.3) should be revisited,
once there's a real risk engine to design the "explicit, tested regime-sizing policy"
the brief allows for as a future possibility.

---

## Deliverable checklist (per the brief's completion criteria)

- [x] Every overlapping component identified — §A/§B
- [x] Final ownership of quantity, stops, regimes, execution, positions, outcomes explicit — §D table
- [x] Existing sector mapping and regime snapshots reused, not duplicated — §B (`universe.sector`, `market_context` both reused as-is)
- [x] One planned source of truth for approved quantity — `risk_decisions.approved_quantity` (§E)
- [x] One planned aggregation point for trading permission — deferred to PR 3, `circuit_breaker.is_breached()` is the shared primitive both PR 1 and PR 3 build on (§C.4)
- [x] Duplicate outcome/journal models avoided — §A.1, already true, confirmed not to regress
- [x] Live and backtest share domain logic — already true for `calc_buy_qty`/sector caps/regime classification; PR 1 extends the same pattern to the new risk-engine entry point
- [x] PR 1 has a reviewable, bounded scope — §F
- [x] Migration/deprecation requirements documented — §E
- [x] No unresolved ambiguity that would let two components make the same trading decision — §C.1's resolution is the one live behavior change required to make this true; documented, not silently shipped

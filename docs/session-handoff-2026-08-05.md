# Session handoff — 2026-08-05

Continuation of `docs/session-handoff-2026-08-04.md` and
`docs/risk-engine-architecture-reconciliation.md` (written this session).
This was almost entirely a risk-engine build session, triggered by the
user's ChatGPT-co-designed risk-engine spec — the spec required a
pre-implementation compatibility phase, which is why the reconciliation
doc exists at all. All facts below were re-verified against GitHub and
the live deployment host (ubuntu-box, 10.10.10.13) immediately before
writing this.

## 1. Repository state

**`main`**: `07a801e` ("Journal analytics: surface risk_decisions in the
Strategy Review dashboard (#36)"). This is exactly what's deployed on
ubuntu-box (`invest-api`/`invest-ingest`, rebuilt and restarted after
every merge this session, confirmed clean each time).

**No open PRs.** Eight PRs this session (#29–#36), all merged, none
abandoned. In merge order:

| # | Title | Notes |
|---|---|---|
| 29 | Risk engine PR 1 — authoritative position sizing + portfolio risk limits | `shared/risk_engine.py`, `risk_decisions` table, clamps `POST /api/trade`/`PATCH /api/proposals/{id}` |
| 30 | *(not authored this session — see section 2)* Hierarchical market/sector/stock regime + merge of PR 1 | Landed mid-session from a parallel branch, see section 2 |
| 31 | Risk engine PR 2 — wire risk_decisions into weekly postmortem | `ingest/postmortem.py` segments |
| 32 | Risk engine PR 3 — trading-permission aggregation + drawdown sizing | `shared/trading_permission.py`, loss-streak control |
| 33 | Fix Decimal/float crash in risk engine | Live bug, caught from a real dashboard screenshot |
| 34 | Experiment 007 — hierarchy regime significance test | + real sector-mapping bug fix (`"Technology"` → `"Information Technology"`) |
| 35 | Execution: protective stop orders | Real broker-side OTO stop-loss, `reconcile_broker_stop_fills()` |
| 36 | Journal analytics — risk_decisions in dashboard | Frontend-only, backend already existed from #31 |

**Test count**: 241 passing (up from 151 at session start), verified on a
genuine fresh clone against a disposable `postgres:16-alpine` before
every merge.

**No uncommitted work anywhere** — confirmed clean on both ubuntu-box and
GitHub immediately before writing this.

## 2. Important correction to the 08-04 handoff's own premise

The 08-04 handoff (and this session's own `docs/risk-engine-architecture-
reconciliation.md`, written early this session) stated **no sector/stock
regime hierarchy existed anywhere in this repo**. That was true when
checked, but **a parallel branch with real hierarchical regime work
existed all along and merged mid-session** (PR #30, authored by the user
with Claude co-authorship, merged 2026-08-05 03:08 — while this session
was mid-deploy of Risk Engine PR 1). ubuntu-box's local checkout was
briefly stale relative to `origin/main` as a result; caught and
resynced immediately (`git fetch` + `git merge --ff-only`), full test
suite re-verified (193 passing at that point), redeployed.

**What PR #30 actually built**: `shared/{regime_common,sector_mapping,
sector_regime,security_regime,regime_scoring,hierarchy_regime}.py` —
market → sector → stock regime classification, persisted to
`sector_regime_history`/`security_regime_history`, snapshotted onto every
proposal (`trade_proposals.regime_snapshot` + score-breakdown columns).
**`regime_scoring_enabled` defaults to 0 (off)** — the hierarchy is
computed and displayed (dashboard badges: "mkt: bull volatile", "sector:
strong bearish", etc.) but influences nothing today. This is
**orthogonal to the risk engine** (score-domain vs. quantity-domain) —
confirmed no conflict, verified by reading `regime_scoring.py` directly.

**Lesson for next time**: `git fetch` + compare against `origin/main`
before trusting any "nothing else exists" finding, even one produced by
exhaustive investigation minutes earlier in the same session — see
`[[feedback_repo_source_of_truth]]` memory, this is a fresh instance of
it, not a new lesson.

## 3. Completed work this session, in build order

### Risk Engine PR 1 (#29)
`shared/risk_engine.py::evaluate_proposal()` — the one authoritative
source for `approved_quantity`. Composes position-allocation cap,
per-trade risk budget (from the proposal's planned stop), portfolio
open-risk cap, and sector cap. **The real conflict it fixed**: quantity
was previously computed/overridden in three independent places
(`calc_buy_qty` at proposal time, an unclamped human override in
`PATCH /api/proposals/{id}`, a fully manual qty in `POST /api/trade`)
with only an advisory, never-blocking `rule_adherence` check after the
fact. Full reconciliation doc: `docs/risk-engine-architecture-
reconciliation.md` (read this before touching risk-engine code again —
it documents the ownership boundaries: strategy proposes, risk engine
sizes, execution submits, position tracker records, outcome layer
measures).

### Risk Engine PR 2 (#31)
Wired `risk_decisions` into the existing weekly postmortem review
(`risk_decision_by_context`/`risk_decision_by_binding_constraint` in
`metric_summary`). Most of the "PR 2" scope in the reconciliation doc's
own decomposition (initial risk persistence, R-multiples, MAE/MFE) was
already built by earlier Platform Improvements PRs — this was
deliberately thin.

### Risk Engine PR 3 (#32)
`shared/trading_permission.py::evaluate_trading_permission()` — account-
level trading-permission aggregation (`{"new_entries_allowed", "scope":
"account", "reasons": [...]}`), combining the existing circuit-breaker
drawdown check with a **new loss-streak control** (pauses new BUY entries
after `loss_streak_limit` — default 4 — consecutive losing closed
`position_lifecycles`). Also extracted `circuit_breaker.py::
drawdown_pct_of()`/`is_breached()` (was hand-duplicated in THREE places:
`rule_adherence.py`, `backtest_portfolio_montecarlo.py`, and
`circuit_breaker.py`'s own two functions) and added
`drawdown_size_multiplier()` (tapers the risk budget toward a 0.5 floor
as drawdown approaches the circuit-breaker threshold — a smooth
de-risking curve, not a hard on/off switch).

### Decimal/float crash fix (#33)
**Live bug, caught from a real dashboard screenshot** the user sent mid-
session: approving a real proposal (any proposal with
`planned_initial_stop_price` set — i.e. every real `compute_signals()`-
generated buy) crashed with `unsupported operand type(s) for -: 'float'
and 'decimal.Decimal'`. `api/main.py` read the DB value straight through
without `float()` conversion. Fixed at both layers (defensive coercion
inside `evaluate_proposal()` itself, plus the two call sites) — 3
regression tests, verified they actually fail against the pre-fix code
before confirming the fix.

### Experiment 007 + sector-mapping fix (#34)
User asked "how do sector-bearish badges show up on BUY proposals,
should I be wary?" — this experiment answers it properly instead of
guessing. Two permutation significance tests (market×sector alignment,
stock-vs-sector relative strength) over real mean-reversion buy episodes
across full history (2016–present), regime classified as-of each
episode's real date (no lookahead). **Result: neither test reached
significance** (p=0.325, p=0.439) despite decent sample sizes (n=4,945
and n=7,679) — the direction matches what `regime_scoring.py`'s config
assumes, but the gap is well within noise. `regime_scoring_enabled=0`
should stay off; report this plainly to the user as "not proven yet," not
spun either direction (see `[[feedback_no_edge_not_negative]]`).

**Real bug caught while building this**: `sector_mapping.py`'s
`SECTOR_ETF_MAP` had key `"Technology"`, but `universe.sector` actually
stores `"Information Technology"` — silently excluded AAPL/MSFT/NVDA and
the rest of Info Tech from sector regime classification since PR #30
shipped (a live dashboard-badge bug, not just a research-script issue).
Fixed; 4 new tests close a total prior coverage gap (nothing had ever
tested this mapping against a real `universe.sector` value).

### Protective stop orders (#35)
The single biggest gap flagged in the reconciliation doc (section C.2):
`check_stop_losses()` only ever created a sell PROPOSAL requiring human
approval — no protective order ever reached the broker. Now:
- Every BUY submits as an Alpaca **OTO order** with a resting `stop_loss`
  child leg, using the exact stop price the risk engine already sized
  against (not a freshly recomputed one — keeps the persisted risk basis
  consistent with what's actually protecting the position). A manual buy
  with no linked proposal now also gets a real stop (fallback: live
  `stop_loss_pct` ratio against the reference price) — previously zero
  protection for that case.
- Any unrelated sell (`thesis_complete`/`time_stop`/`overbought`/
  `regime_deterioration`/manual) **cancels the resting stop-leg order
  first** — Alpaca's docs don't specify whether closing a position via an
  unrelated order auto-cancels a resting OTO child leg, and a resting
  stop holds shares "unavailable" at Alpaca, which would otherwise make a
  legitimate exit look like it has no shares to sell. Verified this
  ordering matters via a dedicated test (cancel must run BEFORE the
  `qty_available` check).
- New `ingest.py::reconcile_broker_stop_fills()` — when the broker's own
  stop fires autonomously, it was never submitted through this app's own
  endpoints, so no `trades` row exists for it unless this function
  creates one. Without it, `position_lifecycles` would silently diverge
  from the real Alpaca account, same failure shape as the 2026-07-21
  `thesis_id` incident via a different root cause.
- `check_stop_losses()` stays as a backup safety net (pre-existing
  positions, or a case where the OTO leg failed to attach), not removed.

**Verified live** (not just tests): confirmed the `GET /v2/orders?
status=open&symbols=...` call works against the real Alpaca paper API,
confirmed `reconcile_broker_stop_fills()` runs clean against production
with zero errors, confirmed a live ingest cycle completes end-to-end with
no tracebacks. **Not yet verified**: an actual live buy carrying a real
resting stop leg (no new buy has fired since deploy) — worth checking the
next time a proposal gets approved.

### Journal analytics (#36)
Pure frontend fix — `risk_decision_by_context`/
`risk_decision_by_binding_constraint` had been computed and tested since
PR #31 but never rendered anywhere. New `_reviewRiskDecisionTable()` in
`dashboard.html`, same pattern as the existing `lifecycle_*`/
`rule_adherence_*` tables. Verified live: triggered a fresh
`run_postmortem_review()` run, confirmed real data now flows through
`/api/reviews` and renders (`proposal_generated`: n=10, `proposal_approval`:
n=3, both 100% fill ratio, 0 reduced/rejected so far — small sample,
expected).

## 4. Unimplemented / next task: asset-history backfill gaps

**User's explicit next task, not started.** Live gap list, re-verified
just before writing this (previous handoffs' "5 symbols, 10 more short"
description is now stale — re-check before trusting these numbers too):

```
symbol   n_rows   first_date    last_date
VIX      0        —             —           <- NOT the same as ^VIX (already fully backfilled, 9,371 rows since 1990)
BDX      0        —             —
BIIB     0        —             —
BF-B     0        —             —
BKNG     0        —             —
HONA     30       2026-06-15    2026-07-28
FDXF     31       2026-05-27    2026-07-10
Q        180      2025-11-03    2026-07-30
PSKY     243      2025-08-07    2026-08-04
BNY      252      2025-07-24    2026-07-24
BLK      253      2025-07-28    2026-07-29
BX       253      2025-07-28    2026-07-29
BMY      254      2025-08-01    2026-08-05
BEN      254      2025-07-03    2026-07-08
BRK-B    254      2025-07-01    2026-07-06
BLDR     257      2025-07-28    2026-08-04
BSX      258      2025-07-01    2026-08-05
BR       258      2025-07-28    2026-08-05
BG       260      2025-07-01    2026-07-23
BRO      271      2025-07-01    2026-07-30
BKR      272      2025-07-01    2026-08-05
SNDK     359      2025-02-13    2026-07-30
```
(query: `universe` symbols with `scannable=TRUE` joined against
`price_history` row counts, filtered to <500 rows — full universe is 523
scannable symbols, so this is the tail.)

**Worth investigating, not yet diagnosed**: the ~250-270-row cluster is
suspiciously concentrated on tickers starting with "B" (BNY, BLK, BX,
BMY, BEN, BRK-B, BLDR, BSX, BR, BG, BRO, BKR all ~1 year of daily data
starting ~July 2025) — looks like it could be a real, systematic gap
(e.g. a batch/pagination bug in `backfill_alpaca.py` that stalled
partway through an alphabetical pass), not five unrelated coincidences.
Confirm/deny this pattern before assuming each symbol needs the same fix.

**`VIX` vs `^VIX`**: these are two different `universe`/`price_history`
symbols. `^VIX` has full history (1990–present, fixed in an earlier
session's backfill_vix.py chunking fix). Plain `VIX` (no caret) has zero
rows — check whether this is a real distinct instrument, a duplicate
universe row that should be removed instead of backfilled, or a
scanner.py bug that's writing two different symbol strings for the same
thing.

## 5. Settled architectural decisions (carried forward + new)

Carried forward from 08-03/08-04, still binding: `trades` append-only,
`position_lifecycles` always derived; missing risk/score/data is `NULL`
never coerced to 0; default deployment is paper trading, no live-money
code path; DB-access code sets its own `cursor_factory` explicitly.

New this session:
- **One authoritative `approved_quantity`**: `risk_decisions` table,
  `shared/risk_engine.py::evaluate_proposal()`. Every buy path (automated
  proposal generation, manual trade, proposal approval, backtest) now
  funnels through the same function.
- **Regime scoring and risk sizing are separate, and must stay that way**:
  `regime_scoring.py` only adjusts proposal *score*; `risk_engine.py`
  takes zero regime input. Confirmed no conflict between PR #30
  (hierarchical regime) and the risk engine PRs. `alloc_modifier`
  (`market_regime.py`, pre-existing) still applies both a score AND a
  size adjustment for the same bearish regime — a known, pre-existing,
  deliberately-not-touched exception (flagged in the reconciliation doc
  section C.3, not fixed this session).
- **`regime_scoring_enabled` stays 0 (off)** — Experiment 007 found no
  significant edge. Don't flip this without new evidence.
- **Every buy gets a real broker-side stop** (OTO order), not just a
  proposal-generating poll. `check_stop_losses()` is now a backup, not
  the primary mechanism.
- **Drawdown-based sizing is a taper, not a cliff**: `risk_budget_dollars`
  shrinks smoothly toward a 0.5 floor as drawdown approaches the circuit
  breaker threshold; the hard stop (`trading_permission`) is a separate,
  harder line.
- **Loss-streak is a new, real halt condition** (default: 4 consecutive
  losing closed lifecycles pauses new entries account-wide) — not
  mirrored in the backtest (`backtest_portfolio_montecarlo.py`), a
  documented, deliberate gap, not silent divergence.

## 6. Open risks and unresolved decisions

Carried forward, still unresolved: same-symbol positions from multiple
theses; partial-fill allocation across lifecycles (still only unit-tested
against synthetic data); intraday MAE/MFE sequencing; broker paper/live
identity verification; capital-graduation plan (still only discussed in
conversation, never written to a doc); Strategy Creation UI (deferred,
needs its own planning session); Options/earnings-scanner work (shelved
per ADR 0001).

New this session:
- **`alloc_modifier`'s dual score+size regime penalty** (section 5 above)
  — flagged twice now (08-03 reconciliation, this handoff), still not
  fixed. Needs its own explicit, tested regime-sizing policy decision if
  it's ever to change, per the reconciliation doc's own compatibility
  constraint #1.
- **`rule_adherence.py`'s position-sizing/sector-cap checks are now fully
  redundant** with the binding risk engine — flagged as a future cleanup
  in the reconciliation doc (section E), not done. Low priority, no
  correctness risk (belt-and-suspenders, not a conflict).
- **No live buy has exercised the new OTO stop-order path yet** — the
  code is tested and the read-side (GET orders) verified against live
  Alpaca, but a real end-to-end "approve a buy → see a resting stop order
  at the broker" hasn't happened. Worth checking next time a proposal is
  approved.
- **Asset-history backfill "B-cluster" pattern** (section 4) — not
  diagnosed, flagged as the most likely lead for whatever's actually
  wrong.

## 7. Operational notes

Environment variables: unchanged, no new ones added this session (all
new `signal_params` keys — `risk_per_trade_pct`, `max_portfolio_open_risk_pct`,
`loss_streak_limit` — are DB-seeded via `schema.sql`, not env vars).

**Deployment state, verified directly**: `invest-api`/`invest-ingest` on
ubuntu-box both rebuilt and restarted after every merge this session,
confirmed clean logs each time. Live production dry-runs performed
against real data for: the risk engine (full decision breakdown against
real portfolio state), the sector-mapping fix (11 ETFs load now, up from
10), `reconcile_broker_stop_fills()` (clean, zero errors), and the
journal-analytics dashboard fix (real `risk_decision_by_context` data
confirmed rendering).

**Merge workflow note**: every merge this session required explicit
per-PR user confirmation before `gh pr merge` — the auto-mode classifier
blocks unprompted merges to this public repo even when a prior merge in
the same session was approved. Don't assume blanket approval carries
forward; ask each time (matches this session's actual pattern, not a new
rule).

**Every PR this session** verified via a genuine fresh clone, full test
suite against a disposable `postgres:16-alpine`, before pushing.

**Nothing this session touched infrastructure/manifest-level facts** — no
new hosts, ports, services, or credentials. The homelab infrastructure
manifest does not need updating from this session's work.

## 8. Next-session bootstrap prompt

Paste this into a fresh session to resume:

```
You're picking up work on smaniktahla/homelab-trader (a paper-trading
research platform, currently deployed on ubuntu-box at 10.10.10.13).
Prior handoffs exist back through docs/session-handoff-2026-07-31.md;
docs/session-handoff-2026-08-05.md (this one) and
docs/risk-engine-architecture-reconciliation.md (also this session) are
the most current and supersede everything earlier on any conflict.

Do not trust any of the following without re-verifying against GitHub
and the working tree first:

- main should be at 07a801e or later, no open PRs. Check `gh pr list` and
  `git log main` before assuming anything below is still true.
- ubuntu-box's live deployment should match main exactly.
- IMPORTANT: this session found a parallel branch (hierarchical regime
  work, PR #30) that existed for days without being visible to `gh pr
  list --state all` until it was opened as a PR mid-session -- always
  `git fetch` and diff against origin/main, don't trust "nothing else
  exists" from a prior session's investigation, even a thorough one.

Architectural invariants (verify before building on them):
- shared/risk_engine.py::evaluate_proposal() is the ONE authoritative
  source for approved_quantity -- every buy path funnels through it.
  Don't let a new code path compute its own qty independently.
- regime_scoring_enabled is 0 (off) -- Experiment 007 (permutation
  significance test) found no significant edge for market x sector
  regime alignment or stock-vs-sector relative strength. Don't enable it
  without new evidence, and don't let any NEW code apply a second,
  independent regime-based size/score adjustment (alloc_modifier already
  does this once, a known pre-existing exception -- don't add a third
  mechanism).
- Every buy submits as an Alpaca OTO order with a real resting stop_loss
  leg. Any sell must cancel a resting stop order for that symbol FIRST,
  before checking qty_available or submitting anything else -- see
  api/main.py::cancel_resting_stop_orders().
- Missing risk/score/data is always NULL, never coerced to zero.
- Default deployment is paper trading; no live-trading code path exists.

Immediate next task (explicit user request, not started): diagnose and
fix the asset-history backfill gaps. See this handoff's section 4 for the
current live gap list (re-run the query first, it may have changed) --
5 symbols (VIX [not ^VIX, which is fine], BDX, BIIB, BF-B, BKNG) have
ZERO price_history rows, and there's a suspicious cluster of ~15
"B"-prefixed tickers all sitting at ~250-270 rows (~1 year, starting
~July 2025) that looks like one systematic bug rather than unrelated
gaps -- investigate that pattern before assuming per-symbol fixes are
needed. Also resolve what "VIX" (no caret) actually is -- a real
instrument needing its own backfill, or a duplicate/bug that should be
removed from universe instead.

Required inspection before writing any plan or code: re-read
ingest/backfill_alpaca.py's actual current batching/pagination logic
(most likely location of a systematic bug), re-run the price_history gap
query fresh, and check scanner.py for how symbols get added to universe
(relevant to the VIX-vs-^VIX question).
```

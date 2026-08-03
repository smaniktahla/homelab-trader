# Session handoff — 2026-08-03

Continuation of `docs/session-handoff-2026-07-31.md`. That handoff's own next-session bootstrap prompt was followed almost exactly: this session picked up the four open PRs it left behind, merged them, then handled a string of UI fixes and finally built Platform Improvements PR A -- the piece both handoffs agree is the most-depended-upon unbuilt work. All facts below were re-verified against GitHub and the live deployment host (ubuntu-box, 10.10.10.13) immediately before writing this, not carried over from memory.

## 1. Repository state

**`main`**: `09213f2` ("Platform Improvements PR A: position lifecycles / R-multiple foundation (#14)"). This is exactly what's deployed on ubuntu-box (`invest-api`/`invest-ingest` containers, rebuilt and restarted this session, running clean).

**No open PRs.** Fourteen PRs total this session (#1-#14), thirteen merged, #3 correctly closed as superseded (its branch was deleted mid-session when PR #1 merged with `--delete-branch`, auto-closing #3 since it was stacked on that branch -- recovered by opening #5 from the same commit, rebased, re-verified, merged in its place). Full list, in merge order: #1 (signal-component shadow infra) -> #5 (SEC EDGAR fundamentals, stacked, redone) -> #2 (Symbol Performance Summary) -> #4 (the 07-31 handoff doc) -> #6 (candlestick toggle) -> #8 (weekend/holiday chart-gap fix) -> #9 (dashboard "hide non-trading days") -> #7 (branch-ref test fix) -> #10 (research roadmap, items 1-6) -> #11 (map/clock-strip label overlap fix) -> #12 (research roadmap, items 7-13) -> #13 (symbol-page company name + Recent Signals height) -> #14 (Platform Improvements PR A).

**Test count**: 78 passing (64 at the start of this session + 14 new from PR A: 12 unit tests for the FIFO matcher, 2 frozen-fixture equivalence tests), verified on a genuine fresh clone against a disposable `postgres:16-alpine` immediately before every merge, same discipline as 07-31's handoff.

## 2. Completed work this session

### Backlog from the 07-31 handoff
PR #1, #2, and a redone fundamentals PR (#5) all merged clean. Full detail already in `docs/session-handoff-2026-07-31.md` section 2 -- not repeated here.

### UI fixes (user-reported, each its own PR)
- **#6 -- Candlestick toggle** on the symbol detail page's price chart (`chartjs-chart-financial` + `chartjs-adapter-date-fns`). Required switching the chart's x-axis from category/index-based to a real time scale, which also let the old index-snapping hack for trade markers get deleted. Hit and fixed a real `chartjs-chart-financial@0.2.1` / Chart.js 4 incompatibility (the plugin predates Chart.js 4 and doesn't feed OHLC data into its auto-scaling -- worked around by computing the y-axis range explicitly).
- **#8 -- Weekend/holiday chart gaps**, a direct regression from #6: `time` scale spaces points by elapsed calendar time, so weekends showed as dead space. Fixed with `timeseries` scale instead (same real timestamps, spaced by index).
- **#9 -- Dashboard "Hide non-trading days" toggle** for the Portfolio Value chart -- a *different* root cause than #8's: `portfolio_snapshots` genuinely records a row every ~hour, 24/7 (confirmed live: 20 consecutive Sunday rows, identical value), so there's no missing data to fix with an axis-type change. Added a self-contained NYSE trading-day calculator (weekends + ~10 annual holidays, Good Friday via the Anonymous Gregorian algorithm) as an opt-in client-side filter -- default view is byte-for-byte unchanged.
- **#11 -- Overlapping city labels** on the Global Markets map and its clock strip. First pass (x-proximity tiering) wasn't sufficient for the tightly-clustered Asia markets -- confirmed by direct inspection that two markers at *different* tiers could still collide, since their real latitudes put them at different natural y before any offset. Replaced with true 2D collision resolution (`gmResolveCollisions`). User later noted the map is "still a bit of a jumble" but acceptable -- **not fully resolved, left as-is by user's own call, not a bug to chase further** unless asked again.
- **#13 -- Symbol page**: company name was fetched by `/api/leaderboard` already but never wired into the `#sym-name` element (one-line fix, `/api/summary` now also joins `universe.name`); Recent Signals list had a hardcoded `max-height:200px` regardless of its sibling card's real height, showing only ~4 rows with dead space below -- changed to `flex:1;min-height:0`.

### Docs
- **#10 + #12 -- `docs/research-backtesting-improvements.md`**, 13 items total (6 written by Claude grounded against actual backtest-script code, 7 more transcribed from the user's own detailed spec and grounded against real schema/code before being added -- e.g. item 7's re-entry risk-budget language is written against the real `max_positions`/`buy_cooldown_days` mechanisms, not invented). All still design-only, no code.

### Test infra
- **#7** -- fixed a test that hardcoded a branch name (`feat/symbol-features-shadow-mode`) as its pre-PR baseline ref; that branch was deleted when PR #1 merged, breaking the test on every fresh clone of `main`. Run as a background task the user kicked off in a separate session; reviewed and independently re-verified (64/64 -> confirmed passing) before merging here.

### Platform Improvements PR A -- position lifecycles / R-multiple foundation
The big one. Full design detail is in the PR #14 description on GitHub; summary:

- **New tables**: `position_lifecycles` (one row per lifecycle, open or closed -- planned vs. actual risk, realized R, pathwise MAE/MFE, `data_quality_flags`) and `position_trades` (normalized join, not an array column, since one trade can be allocated across more than one lifecycle).
- **New columns**: `trade_proposals.planned_entry_price`/`planned_initial_stop_price`/`planned_risk_per_share`/`planned_risk_dollars` (buy proposals only, NULL on sells); `trades.initial_stop_price` (copied immutably from the linked proposal at fill time). Also formalized the undocumented `trades.thesis_id` drift into `schema.sql`.
- **This is the first-ever persisted stop price in the codebase.** Previously stop-loss was only a live percentage re-evaluated every cycle. `planned_initial_stop_price` is deliberately derived from that same existing ratio (`entry_price * (1 - stop_loss_pct)`) -- zero new risk-sizing logic, resolved with the user as an explicit design decision before writing any code.
- **`shared/position_lifecycles.py`**: true FIFO lot matching, distinct from `shared/round_trips.py`'s existing average-cost reconstruction (still what `/api/symbol-performance` serves -- deliberately not swapped in this PR, see section 3). Pathwise MAE/MFE against a **time-varying weighted cost basis** (recomputed at every pyramid entry -- also a resolved design decision, the more-correct but more-complex of two options presented to the user). R-multiples normalize against a lifecycle's fixed *initial* risk only.
- **`ingest/build_position_lifecycles.py`**: idempotent, truncate-and-rebuild, wired into the ingest cycle right after `update_signal_outcomes`, fail-open (same contract).
- **Two real bugs caught by testing** (not shipped on faith): `gross_pnl`/`net_pnl`/`exit_notional` were wrongly forced to `None` for open-but-partially-exited lifecycles (fixed to report realized-to-date, entry cost prorated to shares actually sold -- same principle `round_trips.py` already established); and the builder's three DB-access functions each implicitly relied on whatever cursor style the caller's connection defaulted to (`ingest.py`'s own connection is tuple-based, but the module needs dict access) -- fixed by making every cursor explicit, caught by actually running the builder against seeded synthetic data rather than trusting it worked.
- **Deployed and confirmed working against real trade history**: first live cycle rebuilt 25 lifecycles across 36 symbols, 38 `position_trades` rows, no errors. `realized_r`/risk fields are correctly `NULL` for this pre-existing trade history (none of it has a linked `initial_stop_price`, since that concept didn't exist before this PR) -- R-multiples will populate for trades made from now on.

## 3. Unimplemented plans, ordered by how immediate they are

- **Platform Improvements PR A.1** -- swap `/api/symbol-performance`/`/round-trips` over to read from `position_lifecycles` instead of `round_trips.py`'s average-cost reconstruction. Deliberately deferred out of PR A itself (a consumer-facing behavior change deserving its own mutation-tested verification, not bundled with schema+algorithm). **Nothing currently blocks starting this** -- PR A is merged and has run successfully against real data.
- **Platform Improvements PR B (expectancy reporting) / PR D (exit-policy research)** -- both explicitly hard-depended on PR A's schema per the 07-31 handoff. **That dependency is now cleared** -- neither is started, but both could begin.
- **Launch PR 1-5 (real-money rollout)** -- the 07-31 handoff recorded a user-set sequencing rule: don't start Launch PR work until Platform Improvements PR A lands, because the risk engine has no other source for planned-stop/actual-risk data. **PR A has now landed.** This is a *technical* unblock, not a green light -- starting real-money work is a decision only the user should make explicitly in a fresh conversation, not something a session should infer just because the stated blocking condition cleared. See section 6 for the still-unresolved capital-graduation conversation this ties into.
- **Strategy Creation UI** -- discussed at length this session, not started. User wants a new top-level "Create Strategies" tab (mirroring Strategy Review) plus a dedicated setup/backtest page, with a "find a similar market regime in the last N years" feature for backtesting (buildable now -- regime classification already exists in `symbol_features`/`global_market_signals`, this is mostly a query, not new infrastructure). **User explicitly pushed back on scoping strategies as "just parameterize the existing scripts"** -- wants genuine flexibility (day-trading, long-term, rapid-growth as different strategy *shapes*, not just different tuning knobs). **User's own decision**: handle Platform Improvements first, revisit this as its own PR/planning session afterward. Nothing designed yet beyond that framing -- start with a fresh planning conversation, don't assume the "parameterize existing scripts" scoping that was floated and set aside.
- **Capital-graduation plan** -- user wants to start with ~$1,000 real money, target safe-but-fast growth, then diversify into multiple strategies once proven, but only after they're tested. This is real-money Launch PR territory (section above) and ties directly into the still-unresolved "broker paper/live identity verification" and "live-account mismatch must block all automated orders" open items from the 07-31 handoff (still unresolved, see section 6). **Not written to a design doc yet** -- discussed in conversation only. Worth writing up properly (matching the `docs/*.md` pattern the rest of this roadmap uses) before any Launch PR work starts, so it isn't re-litigated from scratch in a future session.
- **13-item research/backtesting roadmap** (`docs/research-backtesting-improvements.md`) -- all design-only, per the doc's own dependency mapping most items assume PR A's R-multiple fields, which now exist.
- **Signal-integration roadmap remaining work** (news/macro/options/activation PRs), **fundamentals refinement**, **rule-adherence reporting (PR C)** -- unchanged from 07-31, not touched this session.

## 4. Settled architectural decisions

Carried forward from 07-31 (still binding, re-confirmed against the actual code this session, not just assumed):
- `trades` stays append-only; analytical positions are always derived, never hand-maintained. Now concretely true for lifecycles too, not just the round-trips stand-in.
- Planned risk and actual/executed risk are distinct fields at every layer -- **now actually implemented**, not just a stated principle: `planned_initial_stop_price` (proposal) vs. `initial_stop_price` (trade, copied immutably at fill) vs. `actual_initial_risk_per_share`/`actual_initial_risk_dollars` (lifecycle, from the real fill price).
- Missing risk/score data is `NULL`, never zero -- re-enforced multiple times this session (position_lifecycles' entire risk/R/excursion field set, the gross_pnl/net_pnl bug fix below).
- Signal-level MAE/MFE (`signal_outcomes.mae`/`.mfe`, hypothetical, fixed 20-day window, anchored to signal price) and position-level MAE/MFE (`position_lifecycles.mae_price`/`mfe_price`, real, pathwise, time-varying cost basis) are different concepts -- **now both actually exist in the schema**, confirmed not conflated anywhere.
- `round_trips.py`'s average-cost reconstruction remains explicitly temporary and is **still what's live** -- PR A did not swap it out (see section 3, PR A.1).
- Reporting/measurement PRs must not alter execution behavior -- enforced this session exactly as before: frozen-fixture pre/post equivalence test, mutation-tested, for `signals.py`'s proposal-creation changes.
- Default deployment remains paper trading. Nothing this session changed that -- `initial_stop_price` and the new lifecycle tables are informational/reporting additions, not a new execution path.

New this session:
- **Position lifecycles use true FIFO lot matching, not average-cost.** A deliberate, resolved distinction from `round_trips.py` -- each entry lot keeps its own price/cost/stop until consumed oldest-first by a sell.
- **`planned_initial_stop_price` is derived from the existing `stop_loss_pct` ratio, not new risk-sizing logic.** Explicit user decision, made before any code was written -- do not read this as "risk-based position sizing now exists," it doesn't; only a snapshot of what the existing percentage-based mechanism already implies.
- **R-multiples normalize against a lifecycle's fixed *initial* entry risk, never renegotiated on pyramid adds.** Conventional R-multiple practice, explicit design choice.
- **Pathwise MAE/MFE uses a time-varying weighted cost basis**, recomputed at every pyramid entry -- explicit user decision (the more-correct, more-complex of two options presented), matches the excursion reference to what the position's actual blended cost was at each point in time, not just the first entry price.
- **`position_lifecycles.gross_pnl`/`net_pnl`/`exit_notional` represent realized-to-date (0.0 baseline), not "unknown/None while open."** A partially-exited-but-still-open lifecycle has a real, well-defined realized P&L on the shares actually sold -- this was a real bug caught by the test suite mid-session, not a decision made correctly the first time. Entry cost is prorated to shares actually sold, same principle `round_trips.py` already established.
- **DB-access code must never rely on the caller's connection's default cursor style.** `ingest.py`'s connections are plain tuple cursors; `api/main.py`'s are dict cursors. A module that might be called from either context (or that wants to be safely testable in isolation) must request its cursor style explicitly every time. Learned from a real bug in `build_position_lifecycles.py`, caught by actually running it against seeded data.

## 5. Dependencies and sequencing

- **PR A.1** (swap `/api/symbol-performance` to `position_lifecycles`) -- no blockers, not started.
- **Platform Improvements PR B/D** -- schema dependency on PR A is cleared. Not started.
- **Platform Improvements PR C** -- soft-depends on PR A/B, not schema-blocked. Not started.
- **Launch PR 1-5** -- the *technical* blocker (PR A's schema) is cleared, but this remains a user-authorization question, not a green light inferred from schema state. Do not start Launch PR work without the user explicitly re-confirming they want to proceed, in this session or a future one -- see section 3's capital-graduation note.
- **13-item research roadmap** -- most items assume PR A's R-multiple fields, which now exist; still fully design-only otherwise.
- **Strategy Creation UI** -- explicitly deferred by the user until after Platform Improvements work; no schema or design decisions made yet beyond the framing discussion in section 3.

## 6. Open risks and unresolved decisions

Carried forward from 07-31, still unresolved (not touched this session): same-symbol positions from multiple theses (now has a real mechanism -- `data_quality_flags: concurrent_multi_thesis_symbol` -- but the underlying broker-side segregation problem itself is still unresolved, by design); partial-fill allocation across lifecycles (implemented in `position_lifecycles.py` this session, but only unit-tested against synthetic data, never against a real multi-lifecycle partial-fill sequence); intraday MAE/MFE sequencing (the daily-bar-fallback overstatement risk from 07-31 is now concretely realized -- 12 of 36 live symbols this session's first builder run flagged `unmatched_sell_qty`, and most historical lifecycles will use `daily_approximation` resolution since `price_history_hourly` only has recent coverage); broker paper/live identity verification (still open, still relevant, more urgent now that Launch PR work is technically unblocked); fractional-share precision; auth/approver identity; pre-ledger holdings reconstruction (provably incomplete by design, same as before, now also visible directly in `position_lifecycles.unmatched_sell_qty` per-symbol rather than only in `round_trips.py`'s equivalent).

New this session:
- **Historical trade risk data is permanently unrecoverable.** Every trade made before PR A deployed has `initial_stop_price = NULL` and will never get a real value -- `realized_r` and the risk/excursion fields on those lifecycles are correctly `NULL` forever, not a temporary gap. Only trades made from 2026-08-03 onward will have real R-multiple data. Worth knowing before anyone builds an expectancy report (PR B) and wonders why most historical lifecycles show no R.
- **Asset-history backfill gap, discovered but not fixed this session**: of 540 tracked symbols, 5 have zero `price_history` rows at all (`BDX`, `BIIB`, `BKNG`, `BF-B`, `VIX`) and 10 more have only a few weeks to months of data despite being long-established tickers (notably `BABA`, which should have years of history) -- these look like genuine backfill failures, not new listings. Directly relevant to any future "find a similar market regime in the past N years" feature (section 3), which would silently degrade for any affected symbol. Not investigated further; a good small standalone task.
- **The Global Markets map's label overlap is improved but not fully resolved** -- user's own call to leave it ("a bit of a jumble... meh"), not something to proactively revisit.

## 7. Operational notes

Environment variables: unchanged from 07-31's list, no new ones added this session.

**Deployment state, verified directly**: `invest-api`/`invest-ingest` on ubuntu-box both rebuilt and restarted this session, running clean, matching `main` at `09213f2` exactly. The ingest cycle has completed at least twice since deploy with `Position lifecycles: rebuilt 25 lifecycle(s) across 36 symbol(s)` logged both times, no tracebacks.

**Every PR this session** was verified via a genuine `git clone` into a fresh directory, full test suite run against a disposable `postgres:16-alpine` (never the live production DB), before pushing -- same discipline as 07-31, no shortcuts taken despite the volume of PRs.

**Nothing this session touched infrastructure/manifest-level facts** (no new hosts, ports, services, or credentials) -- the homelab infrastructure manifest does not need updating from this session's work.

## 8. Next-session bootstrap prompt

Paste this into a fresh session to resume:

```
You're picking up work on smaniktahla/homelab-trader (a paper-trading
research platform, currently deployed on ubuntu-box at 10.10.10.13). Two
prior handoffs exist on main: docs/session-handoff-2026-07-31.md and
docs/session-handoff-2026-08-03.md (this one, the more current). Read the
08-03 one in full before doing anything else -- it supersedes 07-31 on
every point where they'd otherwise conflict, but 07-31 still has detail
(e.g. the full env var list, PR #1/#2/#3 mechanics) not repeated in 08-03.

Do not trust any of the following as current fact without re-verifying
against GitHub and the working tree first, since state may have changed
since this handoff was written:

- main should be at 09213f2 or later, with no open PRs. Check `gh pr
  list` and `git log main` before assuming anything below is still true.
- ubuntu-box's live deployment should match main exactly -- re-check
  before assuming the schema/code state described here is what's
  actually running.

Architectural invariants (verify these still hold in the code before
building on them -- don't just take this list's word for it):
- trades ledger is append-only; position_lifecycles is DERIVED by an
  idempotent builder (ingest/build_position_lifecycles.py), never
  hand-maintained.
- planned_initial_stop_price is derived from the existing stop_loss_pct
  ratio -- this is NOT real risk-based position sizing. Don't build
  Launch PR work assuming sizing already accounts for risk; it doesn't.
- R-multiples normalize against a lifecycle's fixed INITIAL risk only,
  never renegotiated on pyramid adds. Pathwise MAE/MFE uses a
  time-varying weighted cost basis -- these are two different reference
  conventions, don't conflate them.
- Historical trades (before 2026-08-03) have NULL risk/R data forever --
  this is correct and permanent, not a bug to fix.
- round_trips.py (average-cost) is STILL what /api/symbol-performance
  serves. position_lifecycles (true FIFO) exists and is populated but
  nothing reads it yet -- swapping the API over is explicitly deferred
  (PR A.1), not done.
- Missing risk/score/data is always NULL, never coerced to zero.
- Default deployment is paper trading; there is no live-trading mode in
  the deployed code. Do not wire in real-money execution without the
  user's explicit, fresh authorization -- do not infer it from "the
  Launch PR blocking condition technically cleared."
- DB-access code must set its own cursor_factory explicitly, never rely
  on the caller connection's default (ingest.py uses tuple cursors,
  api/main.py uses dict cursors) -- a real bug this session, don't
  reintroduce it.

Immediate next task: [ASK THE USER]. Candidates, roughly in order of how
immediate they are: (1) PR A.1 -- swap /api/symbol-performance over to
position_lifecycles, no blockers; (2) resume the Strategy Creation UI
planning conversation the user explicitly deferred (see handoff section
3 -- they want genuine strategy-shape flexibility, not just parameterized
existing scripts, plus a "similar market regime" backtesting feature);
(3) write up the capital-graduation plan ($1k real money, safe-fast
growth, then diversify) as a real design doc before any Launch PR work
starts; (4) the asset-history backfill gap (5 symbols with zero data,
10 with implausibly short history) as a small standalone fix; (5)
Platform Improvements PR B/C/D, now schema-unblocked but not started.
Do not assume which one without asking -- there is no single obviously-
next item this time, unlike 07-31's handoff.

Required inspection before writing any plan or code: re-read the actual
current schema, shared/position_lifecycles.py and shared/signals.py's
actual current state, and the actual current test count/suite on
whichever branch you're extending -- this handoff is a snapshot, not a
substitute for looking.
```

## 9. Final verification performed

Before writing this document: fetched `main`'s actual HEAD via the GitHub API, listed every PR (open and closed) with merge status via `gh pr list`, separately SSHed to ubuntu-box to confirm the live deployment's git state, container uptime, and grepped the actual ingest container logs for the `Position lifecycles` rebuild line and any tracebacks across two full cycles. Nothing in this document is asserted from memory alone.

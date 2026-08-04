# Session handoff — 2026-08-04

Continuation of `docs/session-handoff-2026-08-03.md`. That handoff left five
candidates with no single obvious next step; this session worked through
Platform Improvements PR A.1/B/C in full, several small UI/backend fixes, a
detour into evaluating (and shelving) an earnings-volatility-scanner
proposal, and started — but did not finish — a market-regime-history
feature. All facts below were re-verified against GitHub and the live
deployment host (ubuntu-box, 10.10.10.13) immediately before writing this.

## 1. Repository state

**`main`**: `49489c6` ("Merge pull request #24 from
smaniktahla/docs/adr-0001-opra-economic-constraint"). This is exactly what's
deployed on ubuntu-box (`invest-api`/`invest-ingest` containers, rebuilt and
restarted after every merge this session, confirmed clean each time).

**No open PRs.** Ten PRs this session (#15–#24), all merged, none abandoned.
In merge order:

| # | Title | Notes |
|---|---|---|
| 16 | Platform Improvements PR A.1 — swap symbol-performance API to `position_lifecycles` | True FIFO instead of `round_trips.py` average-cost (deleted) |
| 17 | Platform Improvements PR B — trade-level expectancy reporting | New `shared/expectancy.py`, extends `ingest/postmortem.py` |
| 18 | Three UI tweaks | Volume panel on symbol chart, default-checked candlesticks/hide-non-trading-days |
| 19 | Platform Improvements PR C — rule-adherence bypass detection | New `shared/rule_adherence.py`, new `rule_adherence_checks` table |
| 20 | Data source roadmap doc | FRED, FMP, SEC EDGAR insider filings, GDELT — docs only |
| 21 | Scope Recent Signals to the symbol | `GET /api/signals` gained a server-side `symbol` filter |
| 22 | Fix VIX backfill | Yahoo silently downsamples `range=max` to ~monthly for `^VIX`; now chunked. **Backfill already run against production** — `^VIX` now 9,371 rows, 1990-01-02 to 2026-08-03 |
| 23 | Earnings Volatility Scanner PR-002 — options data capability spike | Docs only — real API probe results against the paper account, see section 3 |
| 24 | ADR 0001 — OPRA is an economic constraint | Docs only, establishes `docs/adr/` |

**Test count**: 104 passing (78 at the 08-03 handoff + 26 new across PRs
16/17/19/21/22), verified on a genuine fresh clone against a disposable
`postgres:16-alpine` before every merge, same discipline as every prior
session.

**One uncommitted item**: a local branch `feat/market-regime-history` exists
on ubuntu-box (`~/docker/invest`), checked out from clean `main`, with
**zero commits on it** — work was planned (see section 5) but not yet
written. Safe to `git checkout main` and delete this branch, or just
continue from it; nothing is at risk either way.

## 2. Completed work this session

### Platform Improvements PR A.1 (#16)
Repointed `GET /api/symbol-performance/{symbol}` and its `/round-trips`
sibling at `position_lifecycles` (true FIFO, from PR A) instead of the old
`shared/round_trips.py` average-cost reconstruction, which this PR deletes
entirely along with its test suite. New `shared/lifecycle_performance.py`
reproduces the old response shape key-for-key so `symbol.html` needed only
a one-line label tweak. Numbers genuinely differ from the old endpoint for
any pyramided symbol — documented as expected, not a bug. Also added
`position_lifecycle_symbol_status` (persists per-symbol `unmatched_sell_qty`
from the lifecycle builder, previously only a log line).

### Platform Improvements PR B (#17)
Real trade-level expectancy report over closed `position_lifecycles`,
extending the existing weekly `ingest/postmortem.py` cycle rather than
building new infrastructure. New `shared/expectancy.py`: win rate, avg
win/loss, profit factor, dollar expectancy (always) and R-multiple
expectancy (only over trades with a real stop price, with its own
`n_with_r` denominator). Segmented by thesis/symbol/exit_reason/
market_regime/sector/holding-period/calendar-month. Rendered as a second
table in the Strategy Review UI. Given only ~25 real closed lifecycles
exist, most segments show `sample_quality: insufficient` — expected.

### Platform Improvements PR C (#19)
`shared/signals.py`'s `compute_signals()` enforces six buy-side gates
(circuit breaker, `max_open_positions`, sector cap, buy cooldown, earnings
blackout, position sizing) for the automated pipeline — but `POST
/api/trade` (manual trades) and `PATCH /api/proposals/{id}` (approval)
enforced **none** of them. New `shared/rule_adherence.py::check_gates()`
re-checks all six unconditionally (never short-circuits) against live
state, reusing `signals.py`'s own gate functions (`recent_buy_block_reason`,
`sector_cap_block_reason`, `load_sector_map` — promoted from private names).
New read-only `circuit_breaker.current_high_water_mark()` avoids polluting
the once-per-cycle `portfolio_snapshots` cadence. Checked **before** the
resulting trade is inserted, not after — a real bug caught by testing (checking
after made `buy_cooldown` trivially self-trip on the very trade being
recorded). Bonus fix surfaced: `recent_buy_block_reason`/`load_sector_map`/
`earnings_blackout_reason` all relied on the caller's default cursor
factory — fine when only called from `ingest.py`'s tuple-cursor
connections, broken when called from `api/main.py`'s dict-cursor
connections. Now explicit.

### Small fixes and UI work
- **Volume panel + default-checked toggles (#18)**: symbol page price chart
  gets a volume footer strip (bar dataset, hidden secondary axis scaled to
  5× peak volume). Candlesticks and hide-non-trading-days now default on.
- **`/api/signals` symbol scoping (#21)**: the Recent Signals card fetched
  the global latest-100 signals and filtered client-side — with 500+
  symbols scanning hourly, a quiet symbol's real history (e.g. GLW, last
  signal 2026-07-28) fell outside that window and showed "No signals yet"
  incorrectly. Added a server-side `symbol` param.
- **VIX backfill fix (#22)**: Yahoo's chart API silently downsamples
  `interval=1d` to ~monthly when `range=max` is requested for a
  multi-decade symbol — confirmed empirically (`range=2y` gives real daily
  bars, `range=max` doesn't). Every other symbol in this codebase fetches
  with `range=1y`, so nothing else ever hit this. Fixed by chunking into
  bounded 2-year windows. **Backfill already run against production.**

### Earnings Volatility Scanner — evaluated, shelved on cost (#23, #24)
User brought a 10-PR roadmap (co-designed with ChatGPT) for an earnings
IV/calendar-spread scanner. Revised once during discussion to phase-gate
properly (edge validation before any execution/dashboard/multi-leg work —
matches this codebase's existing "prove it before you build UI around it"
discipline). PR-002 (options data capability spike) was run for real
against the account's Alpaca paper credentials — read-only probes, no
orders placed. **Key finding**: current option-chain snapshots work
(bid/ask/last/volume, `feed=indicative`, free) but have zero Greeks/IV;
`feed=opra` and **every** historical options endpoint (quotes/trades/bars)
return the identical `403: "OPRA agreement is not signed"`. Checking
Alpaca's plan page found this lives behind their **Algo Trader Plus** tier
— **$1,089/year**. **ADR 0001** records the decision: don't upgrade ahead
of any evidence the scanner has an edge; proceed (if at all) on the
already-documented fallback (self-computed IV/Greeks from the free feed,
start accumulating our own point-in-time snapshots now). Options work is
explicitly off the critical path per the user's own direction — nothing
here is scheduled to continue unless revisited.

### Data source roadmap doc (#20)
Captured a separate conversation (FRED, Financial Modeling Prep, SEC EDGAR
insider filings, GDELT) as `docs/data-source-roadmap.md`. Confirmed via
code read: SEC EDGAR is already in use (XBRL `companyconcept` only, no
insider filings) and Yahoo Finance is the sole price source — everything
else (FRED/FMP/GDELT) is net-new. Rough priority order recorded: FRED >
FMP-as-gap-filler > EDGAR insider filings > GDELT.

## 3. New standing preference this session

**"If we calculate it, we should store it."** Stated 2026-08-04, saved to
memory (`feedback_calculate_then_store`). Prompted by discovering
`market_context` (single upserted row, zero history) blocks a
regime-similarity backtesting feature — the same problem
`global_market_signals` already had to solve once for the global-markets
widget, never generalized. Default to append-only history for computed
values going forward in this codebase, unless the data is genuinely large
or high-frequency (raw intraday ticks, not small daily rows).

## 4. Unimplemented / in-progress — market regime history

**Not started beyond planning.** Full design already approved (this
session, `EnterPlanMode` → `ExitPlanMode`, plan content below) but zero
code written. This is next session's obvious starting point.

**Goal**: persist per-day market regime history (currently `market_context`
is a single overwritten row) and expose a `find_similar_regime_days()`
capability — the first concrete piece of Strategy Creation's regime-aware
backtesting (see section 6 for how this fits the bigger picture).

**Approved design** (from the plan file, `/home/salil/.claude/plans/ancient-painting-zebra.md`
on this machine — may not persist across sessions, so summarized fully
here):

1. **New pure function `market_regime.classify_overall(spy_trend, qqq_trend, vix_regime)`**
   — extracts the inline if/elif cascade currently hand-duplicated in
   `ingest/market_regime.py::compute_market_regime()` (live) and
   `ingest/research/backtests/backtest_portfolio_montecarlo.py::historical_market_context()`
   (already solves "classify regime as of an arbitrary historical date" via
   an as-of-index walk over `price_history`, but as a separate hand-copy).
   Pure extraction — same branches/numbers/rationale, verified by unit
   tests before any refactor lands.
2. **New table `market_regime_history`** — one row per trading day
   (`trading_date DATE PRIMARY KEY`), same field shape as `market_context`
   plus `computed_at`. `ON CONFLICT (trading_date) DO UPDATE` — today's row
   can refine intraday, same as `market_context` already does.
3. **New `shared/market_regime_history.py`** — `load_daily_series`/
   `asof_index` (moved out of `backtest_portfolio_montecarlo.py`, currently
   private to that script), `regime_for_date()`, `record_today(conn, ctx)`,
   `find_similar_regime_days(conn, overall, start_date, end_date)` — a
   plain query against the new table, no live recomputation.
4. **New one-off `ingest/backfill_market_regime_history.py`** (same manual
   pattern as `backfill_vix.py`) — walks every SPY trading day from
   2016-07-22 to today.
5. **Hook into `ingest/ingest.py`'s existing fail-open regime block** — one
   more call (`market_regime_history.record_today`) alongside the existing
   `compute_market_regime()`/`save_market_context()` calls.
6. **Refactor `backtest_portfolio_montecarlo.py`** to import the shared
   helpers instead of its own local copies.

**Depth caveat**: SPY history starts 2016-07-22, QQQ only 2020-07-27 (QQQ
is the binding constraint for any day needing both trends — pre-2020 days
will classify `qqq_trend="unknown"`, which the existing cascade already
handles safely, just less differentiated). `^VIX` now has full daily
history back to 1990 (this session's backfill fix).

**Explicitly out of scope for this piece**: any API endpoint or dashboard
UI for the similarity finder (that's Strategy Creation UI's own effort,
later) — this is the data layer only.

**Next-session starting point**: re-enter the plan (or just start
implementing directly, the design is settled) on the `feat/market-regime-history`
branch already checked out on ubuntu-box, or a fresh one if that's been
cleaned up.

## 5. Where Strategy Creation UI actually stands

Confirmed by direct investigation this session (not assumed from the
08-03 handoff, which had this partially wrong): **there is zero pluggable-strategy
infrastructure in this codebase today.** `theses.config` (JSONB) is written
but never read by anything. `congress_shreve_hern` (the second thesis) has
no live scoring path at all — only a standalone offline backtest script.
`compute_signals()` has no thesis parameter; it's unconditionally the one
RSI/BB mean-reversion engine for every symbol, every cycle. "Genuine
strategy-shape flexibility" is 100% net-new architecture, not something
dormant waiting to be wired up.

Talked through with the user what "rapid-growth" as a strategy shape
should mean: **use the multi-component combiner already reserved in
`symbol_features`** (`technical_score`/`fundamental_score`/etc., currently
only `technical_score` populated, combination currently `{"technical": 1.0}`)
to do genuine multi-signal edge discovery, then size positions by
risk-unit/R-multiple-based capital allocation (direct extension of PR A's
R-multiples and PR B's expectancy math). **Real bottleneck named**: only
`technical_score` is populated today — the other five component slots are
all NULL, so "edge discovery across the universe of signals" is currently
narrower than it sounds until the data-source roadmap (section on FRED/
EDGAR-insider/GDELT above) fills in more inputs.

**Agreed sequencing**: build the regime-similarity backtest tool first
(self-contained, no data-source dependency — see section 4), the
edge-discovery/risk-unit-allocation engine second (bottlenecked on real
component data). `day_trading` horizon explicitly deferred — architecture
should leave room for it later, not build it now (needs intraday data
layer + same-day-flatten execution model, a materially bigger lift).

## 6. Settled architectural decisions (carried forward + new)

Carried forward from 08-03, still binding:
- `trades` stays append-only; `position_lifecycles` always derived, never
  hand-maintained.
- Missing risk/score/data is always `NULL`, never coerced to zero.
- Default deployment is paper trading; no live-trading code path exists.
- DB-access code must set its own `cursor_factory` explicitly, never rely
  on the caller's default — reinforced hard this session (PR C's real bug,
  fixed in `recent_buy_block_reason`/`load_sector_map`/
  `earnings_blackout_reason`).

New this session:
- **"If we calculate it, we should store it"** (section 3) — the new
  standing default for this codebase's ingest-cycle computations.
- **Reporting-side gate checks must be checked BEFORE the record they'd
  self-reference is written**, not after — PR C's real bug (buy_cooldown
  self-tripping), a pattern worth remembering for any future "re-check
  against recent history" advisory logic.
- **Options/multi-leg execution stays structurally separate from the
  existing single-leg equity FIFO lifecycle model, if ever built** — per
  the (shelved) earnings-scanner roadmap's own Phase Two framing, not
  contradicted by anything decided this session.
- **Real-money Launch PR work remains unauthorized** — nothing this
  session touched that gate; still requires the user's fresh, explicit
  sign-off in whatever session that's revisited.

## 7. Open risks and unresolved decisions

Carried forward from 08-03, still unresolved (not touched this session):
same-symbol positions from multiple theses; partial-fill allocation across
lifecycles (implemented, still only unit-tested against synthetic data);
intraday MAE/MFE sequencing (daily-bar-fallback overstatement risk);
broker paper/live identity verification; fractional-share precision;
auth/approver identity; pre-ledger holdings reconstruction; capital-graduation
plan (still only discussed in conversation, never written to a doc).

New this session:
- **Whether upgrading to Alpaca's Algo Trader Plus ($1,089/yr) is worth it
  is explicitly deferred** (ADR 0001) — revisit trigger: either the
  self-computed-Greeks path shows enough promise to justify paying for
  better data, or it proves too noisy to trust at all.
- **The earnings-volatility-scanner roadmap itself is shelved, not
  cancelled** — PR-001 (earnings event data spike) and PR-003/PR-004
  (earnings ingestion + forward snapshot collection) could technically
  proceed in parallel with anything else, since neither depends on the
  OPRA decision, but nothing here is scheduled unless the user asks.
- **`market_regime_history`'s design is approved but unimplemented** — see
  section 4, the concrete next-session task.

## 8. Operational notes

Environment variables: unchanged from prior sessions, no new ones added.

**Deployment state, verified directly**: `invest-api`/`invest-ingest` on
ubuntu-box both rebuilt and restarted after every merge this session,
confirmed clean logs each time (no new tracebacks, `Position lifecycles`
rebuild line unchanged at 25 lifecycles/36 symbols/12 unmatched — no
regression from any of this session's changes).

**Every PR this session** verified via a genuine `git clone` into a fresh
directory, full test suite (104 passing) run against a disposable
`postgres:16-alpine` (never the live production DB), before pushing — same
discipline as every prior session.

**One manual action performed outside the normal PR flow**: the VIX
backfill script (`backfill_vix.py`) was run directly against production
after PR #22 merged — this is expected (it's an explicitly manual,
idempotent, one-off script, same as its original design), not a deviation.

**Nothing this session touched infrastructure/manifest-level facts** (no
new hosts, ports, services, or credentials) — the homelab infrastructure
manifest does not need updating from this session's work.

## 9. Next-session bootstrap prompt

Paste this into a fresh session to resume:

```
You're picking up work on smaniktahla/homelab-trader (a paper-trading
research platform, currently deployed on ubuntu-box at 10.10.10.13). Prior
handoffs: docs/session-handoff-2026-07-31.md, docs/session-handoff-2026-08-03.md,
docs/session-handoff-2026-08-04.md (this one, most current). Read the 08-04
one in full before doing anything else.

Do not trust any of the following as current fact without re-verifying
against GitHub and the working tree first:

- main should be at 49489c6 or later, no open PRs. Check `gh pr list` and
  `git log main` before assuming anything below is still true.
- ubuntu-box's live deployment should match main exactly.
- A local branch `feat/market-regime-history` may exist on ubuntu-box,
  checked out from clean main, with zero commits — safe to reuse or
  delete, nothing is at risk either way.

Architectural invariants (verify these still hold before building on
them):
- DB-access code must set its own cursor_factory explicitly, never rely on
  the caller's default -- a real bug this session (PR C), don't
  reintroduce it.
- "If we calculate it, we should store it" -- new standing default for
  ingest-cycle computations in this codebase (see 08-04 handoff section 3).
- market_context is STILL a single-row table as of this handoff --
  market_regime_history (the fix) is designed but not built. Don't assume
  it exists without checking `\d market_regime_history` or grepping schema.sql.
- There is ZERO pluggable-strategy infrastructure in this codebase --
  theses.config is written but never read, compute_signals() has no
  thesis parameter, congress_shreve_hern has no live scoring path. Don't
  assume any of this exists just because a thesis row exists in the DB.
- Options/earnings-scanner work is shelved per ADR 0001 (docs/adr/0001-opra-economic-constraint.md)
  -- OPRA (Alpaca's $1,089/yr data tier) gates real Greeks/IV AND all
  historical options data uniformly. Not resumed unless the user asks.

Immediate next task: implement market_regime_history per the full design
already recorded in section 4 of the 08-04 handoff -- this was planned and
approved (EnterPlanMode/ExitPlanMode) but not yet coded. Six pieces:
classify_overall() extraction in ingest/market_regime.py, the new table,
shared/market_regime_history.py, the one-off backfill script, the
ingest.py live-cycle hook, and the backtest_portfolio_montecarlo.py
refactor to use the shared helpers. Follow the same discipline every PR
this session used: fresh-clone test verification before every push, PR
review before merge, rebuild+redeploy+manual-verify after merge.

After that (or instead, if the user redirects): Strategy Creation's second
piece (edge-discovery + risk-unit allocation engine) is bottlenecked on
more symbol_features components being populated (today only
technical_score is real) -- see docs/data-source-roadmap.md for the
data-source work that would unblock it (FRED prioritized first).
```

## 10. Final verification performed

Before writing this document: fetched `main`'s actual HEAD via git log,
listed every PR (open and closed) via `gh pr list`, confirmed zero open
PRs, checked the local branch list for uncommitted work (found
`feat/market-regime-history`, confirmed zero commits on it), re-queried
production `price_history` directly for `^VIX`'s actual row count/date
range to confirm the backfill fix's real-world effect, and re-checked
`invest-api`/`invest-ingest` container state on ubuntu-box. Nothing in
this document is asserted from memory alone.

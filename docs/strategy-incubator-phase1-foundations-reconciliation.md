# Strategy Incubator — Phase 1 (Foundations) Reconciliation

Design note, committed to the repo rather than decided unilaterally in a chat
session — same reasoning as
[`docs/trade-thesis-architecture-reconciliation.md`](trade-thesis-architecture-reconciliation.md),
[`docs/peer-intelligence-architecture-reconciliation.md`](peer-intelligence-architecture-reconciliation.md),
and
[`docs/hypothesis-driven-phase5-6-llm-compiler-investigation.md`](hypothesis-driven-phase5-6-llm-compiler-investigation.md).
**No implementation in this PR — architecture only.**

The Strategy Incubator & Validation Pipeline epic (DocMost note
`0a93c01c-4115-47fe-8fb0-bf055ca17fb0`) was queued 2026-08-28 behind
Hypothesis-Driven Trading's Phase 4, "no PRs yet." That parking condition is
now met (PR1-18 merged and deployed), and the 09-17 Phase 5/6 investigation
found this epic is a genuine *blocker* for Hypothesis-Driven's own remaining
Phase 5/6, not just a sequenced-after epic — see that doc for the full
argument. This note scopes only **Phase 1 ("Foundations")** of the
Incubator's own 5-phase Implementation Priority (§29): strategy versioning,
lifecycle, provenance, freeze, DB migrations, basic API. Phases 2-5
(validation/walk-forward, incubator UI/promotion gates, forward testing,
portfolio research) are explicitly out of scope here.

## 1. What already exists (don't rebuild this)

`ingest/schema.sql` has no `strategies`/`strategy_versions` table today —
confirmed by grep, not assumed. What exists one layer below where this epic
needs to sit:

```
HypothesisType (hypothesis_types, PR13)
    |
    v
CandidateBatch / Candidate (candidate_batches / candidates, PR14)
    |
    v
   ... nothing consumes this yet -- THIS is where Phase 1 needs to attach ...
```

Also relevant, so Phase 1 doesn't duplicate it:
- `backtest_results` (`ingest/schema.sql`, added 2026-07-22): a loose
  `experiment_id` + JSONB blob table, written by every one-off
  `ingest/research/backtests/*.py` script (score calibration, rule
  significance, all six Volume epic hypothesis experiments). It is **not**
  strategy-version-scoped and has no lifecycle concept — it is a durable log
  of "a script ran, here's what it found," not §4's `backtest_runs`/
  `backtest_metrics` schema. Phase 1 should not try to retrofit
  `backtest_results` into the new model; a `StrategyVersion` can reference
  `backtest_results` rows by `experiment_id` for provenance without owning
  or restructuring that table.
- `shared/backtest_engine.py` (PR15): the actual bar-by-bar execution
  engine (`run_backtest()`, `BacktestResult`). This is the *mechanism* a
  future `backtest_runs` row would invoke — Phase 1 does not touch it, and
  no future phase should reimplement it.
- The `theses`/`trades.thesis_id` legacy pair predates `ingest/schema.sql`
  entirely (no tracked `CREATE TABLE theses` anywhere, per an existing
  in-file comment) — a pre-existing bootstrap gap, unrelated to this epic's
  new `strategies` concept. Do not conflate the legacy `theses` with the new
  `strategies`/`strategy_versions` tables; they are different concepts that
  happen to share a word.

## 2. Phase 1 scope, decomposed into 3 PRs

Following this repo's established pattern (grammar/schema first, dark and
tested; API next; live integration last) — same staging
`trade_thesis.py` (PR1) and `backtest_engine.py` (PR15) both used.

### SI-1 — Schema + lifecycle object model (no API, no live wiring)

**Schema** (`ingest/schema.sql`), scoped to the Incubator spec's own §2
minimum-fields list, trimmed to what Phase 1 actually needs (validation/
walk-forward/paper/shadow timestamp columns are declared now, per the
spec's own reasoning for adding `price_history_hourly`'s `source` column
early — "cheap, additive, avoids a second migration" — but are only ever
written by later phases; Phase 1 never populates them):

- `strategies` — `id, strategy_name, strategy_family, description, created_at`.
- `strategy_versions` — `id, strategy_id (FK), version_number, status,
  created_at, created_by, git_commit, code_hash, parameter_hash,
  parameter_frozen_at, parent_strategy_version_id (self-FK), description,
  hypothesis_type, hypothesis_type_version, candidate_id (FK, nullable —
  see SI-3), training_start, training_end, validation_start, validation_end,
  walk_forward_start, walk_forward_end, paper_forward_start,
  paper_forward_end, shadow_live_start, shadow_live_end, approved_at,
  live_start, retired_at`. UNIQUE `(strategy_id, version_number)`.
- `strategy_version_transitions` — append-only audit log per §27:
  `id, strategy_version_id, from_status, to_status, transitioned_at, actor,
  reason, metadata (JSONB)`. Same shape convention as
  `hypothesis_type_changes` (PR13).

**Object model** (`shared/strategy_lifecycle.py`, mirroring
`shared/trade_thesis.py`'s split of grammar/model from persistence):

- `STATUSES` = the §1 lifecycle
  (`RESEARCH, BACKTEST, VALIDATION, WALK_FORWARD, FROZEN, PAPER_FORWARD,
  SHADOW_LIVE, APPROVED, LIVE, MONITORED, RETIRED, REJECTED, SUSPENDED`).
- `VALID_TRANSITIONS`: an explicit adjacency map (e.g.
  `RESEARCH -> {BACKTEST, REJECTED}`, `LIVE -> {SUSPENDED, RETIRED}`,
  any pre-live state `-> REJECTED`), checked by a pure function
  `is_valid_transition(from_status, to_status)` — satisfies §28's explicit
  testing requirement ("valid transitions succeed... invalid ones fail")
  as a unit-testable pure function, not something only exercised via the DB.
- `StrategyVersion` frozen dataclass (same shape convention as
  `TradeThesis`) + `transition(conn, strategy_version_id, to_status, actor,
  reason=None, metadata=None)`: validates the transition against
  `VALID_TRANSITIONS`, writes the new `status` and (for `FROZEN`) stamps
  `parameter_frozen_at`, and unconditionally appends one
  `strategy_version_transitions` row — the transition and its audit record
  happen in one transaction, so there is never a status change without a
  corresponding audit row (§27's actual requirement, not just a nice-to-have).
- `freeze(conn, strategy_version_id, code_hash, parameter_hash, actor)`:
  the explicit Freeze operation (§9) — a thin wrapper over `transition(...,
  to_status="FROZEN")` that additionally requires `code_hash`/
  `parameter_hash` to be supplied (never re-derived at freeze time from
  mutable code — the caller, e.g. SI-3's candidate-registration path,
  computes them from the exact code/params being frozen). Rejects freezing
  a version that isn't in a pre-freeze status.

**Tests**: transition-table correctness (every legal edge succeeds, a
sample of illegal edges — e.g. `RESEARCH -> LIVE` — fail), freeze requires
correct pre-state and stamps `parameter_frozen_at`, every transition writes
exactly one `strategy_version_transitions` row, `(strategy_id,
version_number)` uniqueness enforced. No API, no live-path wiring — same
"computed, tested, not yet load-bearing" staging every other PR1-scale PR in
this repo has used.

### SI-2 — Basic API

`GET /strategies`, `GET /strategies/{id}`, `GET /strategies/{id}/versions`,
`GET /strategy-versions/{id}`, `POST /strategy-versions` (create a new
version under a strategy — RESEARCH status only), `POST
/strategy-versions/{id}/transition` (body: `to_status`, `reason`; calls
`strategy_lifecycle.transition()`), `POST /strategy-versions/{id}/freeze`.
Read-only endpoints follow the existing `hypothesis-types` CRUD
conventions in `api/main.py` (PR13). Still no live-trading wiring — this
makes Phase 1 inspectable/operable via API, not automatically load-bearing.

### SI-3 — Candidate → StrategyVersion registration (closes the Hypothesis-Driven gap)

This is the integration point Hypothesis-Driven's own "PR15 — Strategy
Incubator Integration" was supposed to be and never was (see the Phase 5/6
investigation doc, §2). One function,
`register_candidate_as_strategy_version(conn, candidate_id, strategy_id,
actor)`: loads a `Candidate` row (PR14), creates a new `strategy_versions`
row in `RESEARCH` status with `hypothesis_type`/`hypothesis_type_version`
copied from the candidate's batch (provenance preserved, same "frozen at
generation time" precedent `candidate_batches.hypothesis_type_version`
already established) and `candidate_id` set. Does **not** run a backtest,
score, or promote anything — it only gives a `Candidate` a place to live in
the lifecycle, from which a human (or, once Phase 2 exists, an automated
walk-forward run) can advance it through `BACKTEST -> VALIDATION -> ...`
via SI-1's `transition()`.

Once SI-3 exists, Hypothesis-Driven's Phase 5a (LLM-generated candidates,
per the Phase 5/6 investigation doc) has somewhere real to send its output —
this is the concrete unblock, not just a sequencing preference.

## 3. Explicitly out of scope for Phase 1 (later phases, per §29)

Validation datasets, walk-forward testing, parameter robustness (§5-7,
Phase 2). Incubator UI, promotion gates, rejection tracking, strategy score
(§13, §17-19, Phase 3). Paper-forward/shadow-live execution modes,
backtest-vs-forward divergence (§10-12, Phase 4). Portfolio-level
correlation analysis (§22, Phase 5, also flagged as unresolved overlap with
the Peer Intelligence roadmap in the epic's own 2026-08-28 note — do not
touch that overlap in Phase 1). `backtest_runs`/`backtest_metrics` (§4) are
deferred to whichever phase actually wires a `StrategyVersion` to a real
`run_backtest()` invocation with the full metrics set — SI-3 only registers
a candidate, it does not execute anything.

## 4. Open questions (for the user, not decided here)

1. **`strategy_family` values**: the spec's §20 baseline strategies (Buy
   and Hold SPY, 200-Day SMA, MA Crossover, Daily MACD, RSI Mean Reversion,
   Turn-of-Month) imply `strategies` rows should exist for the *existing*
   strategies too (`mean_reversion`, `bollinger_breakout_continuation`,
   `ema_crossover_trend`, `supertrend`), not only future LLM/candidate-
   generated ones. Should SI-1 or a follow-up PR backfill `strategies` rows
   for what's already live, so the Incubator has something to show on day
   one instead of starting empty?
2. **Actor identity**: `created_by`/`actor` fields assume a caller identity
   string. Is there an existing convention for this in the repo (e.g. does
   any code path already stamp "system" vs. a named session), or should
   Phase 1 introduce one?
3. **Should SI-3 ship in this same Phase 1 batch, or wait?** It's the
   smallest of the three and the one with the clearest immediate payoff
   (unblocking Hypothesis-Driven Phase 5a), but it does create the first
   real consumer of `strategy_versions` before SI-2's API exists to inspect
   the result — fine given this repo's "dark, tested, not yet load-bearing"
   precedent, but flagging in case a different ordering is preferred.

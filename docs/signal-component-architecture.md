# Signal component architecture (multi-source quant signal integration)

Design note, committed to the repo rather than pasted into a chat session —
same reasoning as [`docs/thesis-horizons-and-intraday-data.md`](thesis-horizons-and-intraday-data.md):
a prior design doc for this codebase existed only as a pasted-in-chat
document and was lost. This one covers the PR #1 infrastructure for adding
fundamentals, earnings, news, options and macro as independent signal
components alongside the existing RSI/Bollinger technical score, without
changing what `mean_reversion` actually decides.

## Problem

`shared/signals.py::score_signal()` produces one number per side (`buy`/
`sell`) from RSI + Bollinger Bands + market regime + relative strength +
ATR, all computed inline, and `compute_signals()` acts on that number
directly. There's no way to add a second, independently-sourced signal
(fundamentals, earnings surprise, news sentiment, options IV, macro
regime) without either bolting it into `score_signal()` itself (mixing an
indicator calculation with a database write) or duplicating the technical
score's logic in a second place. Both are worse than the alternative:
give every signal family — technical included — a place to report its
own component score, store all of them per signal, and combine them
through one explicit, testable function.

## Design

### `symbol_features` is thin in PR #1, deliberately

The naive version of this table copies every technical indicator
(`rsi`, `bb_upper/middle/lower`, `atr`, `relative_strength_20d`, ...)
alongside the new component columns. PR #1 does not do this: those fields
already live on `signal_outcomes`, written once by `_record_outcome()`.
Duplicating them into a second table in the same PR that just wrote them
once creates exactly the two-sources-of-truth risk this project explicitly
wants to avoid — `score_signal()` itself is never reimplemented or
recomputed anywhere; its return value is *stored*, not re-derived.
`symbol_features` in PR #1 carries only `technical_score`, the (currently
all-`NULL`) other component scores, and the metadata needed to interpret
them (`feature_version`, `model_version`, `data_confidence`,
`component_weights`, `source_timestamps`). If a future consumer (a
backtest script, say) needs `symbol_features` to be self-sufficient
without joining `signal_outcomes`, that's a deliberate, separately
reviewed addition — not a PR #1 default.

### `side` is part of the snapshot key

The generic version of this schema (see the original planning document)
assumes one scalar technical score per symbol. That isn't what this
codebase computes: `score_signal()` returns an independent score for
`buy` and for `sell` every cycle, and both can independently clear
`score_log_min` and get logged. `symbol_features` is keyed on
`(symbol, as_of, side, feature_version)` — one row per side, mirroring the
`side` column `signal_outcomes` already has for the same reason — rather
than forcing an arbitrary choice of which side's score counts as "the"
technical component.

### `feature_version` vs. `model_version`

Two different things get versioned, deliberately kept separate:

- **`feature_version`** versions the *inputs* — `score_signal()`'s own
  calculation (indicator formulas, lookback periods). `"v1"` means
  "whatever `shared/signals.py::score_signal()` computes as of this PR."
  It's bumped only when that calculation itself changes — not when a new
  component family is added, since that doesn't change how technical is
  computed.
- **`model_version`** versions the *combination logic* — how components
  get turned into a composite. PR #1's combination is the identity
  function on one component (`weights = {"technical": 1.0}`), stored as
  `"mean_reversion_technical_only_v1"`. This exists now specifically so
  that once a later PR introduces a real weighted combination across
  multiple components, old rows are unambiguously distinguishable from
  new ones without inference.

### `as_of` vs. `created_at`

`as_of` is the point-in-time the snapshot's inputs were evaluated at —
in PR #1, this is exactly the `generated_at` timestamp Postgres assigned
to the paired `signal_outcomes` row (read back via `RETURNING id,
generated_at` in `_record_outcome()`, not a second independent
`datetime.now()` call, so the two rows can never drift apart by even a
few milliseconds). `created_at` is pure insert-time bookkeeping. A future
backfill process inserting historical snapshots would set `as_of` in the
past while `created_at` reflects when the backfill actually ran — keeping
these distinct from day one avoids the two ever being silently conflated.

### `data_confidence` is trivial in PR #1, and that's fine

`data_confidence` is `NUMERIC` constrained to `[0, 1]` at the database
level (`CHECK`). In PR #1 it is always exactly `1.0`: a `symbol_features`
row is only ever created after `score_signal()` has already succeeded —
it's on `compute_signals()`'s critical path, never optional — so there is
no partial-confidence case yet to represent. This becomes meaningful
starting with the fundamentals PR, when a component can legitimately be
missing for a given symbol. Documenting the triviality now is meant to
stop `1.0` from later being read as carrying more meaning than it does in
this PR.

### Fail-open persistence, by construction not by convention

`shared/feature_store.py::record_symbol_feature_snapshot()` and
`attach_feature_snapshot()` are both best-effort: any DB exception is
caught, the connection is rolled back, and the function returns `None` /
does nothing — it never raises past its own boundary. This isn't a policy
layered on top of otherwise-normal code; it's made *safe* by an existing
property of `compute_signals()`: every prior statement in the per-symbol,
per-side loop (`signals` insert, `signal_outcomes` insert) already commits
eagerly before feature persistence ever runs. A rollback inside
`feature_store.py` therefore only ever undoes its own uncommitted
statement — nothing else pending is lost. This is directly informed by
the 2026-07-21 outage (`shared/signals.py`, `compute_signals()` — an
unhandled exception mid-loop poisoned the connection's transaction, and
every subsequent symbol silently failed for 8 days because nothing rolled
back the poisoned state). Every new collector added in later PRs must
follow the same pattern: its own try/except, its own rollback, never
sharing an unrecovered failure with the statements around it.

### Why `symbol_features.symbol` has no foreign key

`universe` only contains Alpaca-tradable US equities that passed
`seed_universe()`'s filter (`NYSE/NASDAQ/ARCA/BATS`, no `.`/`/`, ≤5
characters). Index tickers already used elsewhere in this app —
`^VIX`, `^N225`, `^HSI`, `000001.SS` (`global_markets.index_symbol`) —
never appear in `universe` because they can't pass that filter. An FK
from `symbol_features.symbol` to `universe.symbol` would reject a
snapshot for any of those the moment something writes one. `watchlist`
itself has no FK to `universe` either — this codebase already treats
`symbol` as a loosely-shared `TEXT` identity across tables, not a single
strictly-foreign-keyed dimension, and `symbol_features` follows that
existing convention rather than introducing a new, stricter one for
itself alone.

`signal_outcomes.feature_snapshot_id → symbol_features.id`, by contrast,
*is* a real foreign key: it's an internal surrogate id this application
fully owns, not a natural key with an external lifecycle. `ON DELETE SET
NULL` is chosen (rather than `RESTRICT` or `CASCADE`) so that a future
retention/pruning job can remove old snapshots without being blocked, and
without deleting the outcome row's own data just because its snapshot was
pruned.

## What this unlocks, and what it doesn't

This PR does not create a multi-signal decision model. `mean_reversion`'s
proposal decision is, after this PR, still driven by exactly the same
`score_signal()` return value it was driven by before — nothing in the
gating cascade (`score_log_min`, `score_proposal_min`, earnings blackout,
circuit breaker, sector cap, buy cooldown, position sizing) reads
`symbol_features` or any new `signal_outcomes` column. What it creates is
the place later PRs write into: `fundamentals`/`earnings_features`/`news_features`/`macro`
collectors land in shadow mode (computed, stored, zero weight) exactly the
way `congress_shreve_hern` sits in `status = 'backtesting_only'` until its
own filing-date-entry-vs-SPY backtest clears it. No component but
`technical` gets real weight in `component_weights` until its own
validated backtest — the same discipline this repository already holds
new theses to — justifies moving it off zero.

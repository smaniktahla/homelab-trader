# Fundamentals collector (PR #2 of the multi-source signal integration)

Design note, following the precedent of `docs/thesis-horizons-and-intraday-data.md`
and `docs/signal-component-architecture.md`: committed to the repo rather
than left in chat history.

## Scope

Adds a real, working SEC EDGAR fundamentals pipeline in shadow mode:
`fundamental_facts` (raw, point-in-time, append-only) → `shared/fundamentals.py`
(point-in-time scoring) → `symbol_features.fundamental_score` /
`signal_outcomes.fundamental_score` (populated, zero decision weight).
No proposal, gating, or execution behavior changes — proven by
`tests/test_fixture_equivalence_fundamentals.py`, which diffs pre/post-PR#2
`compute_signals()` output against an identical fixture, the same
discipline (including mutation-testing to confirm the diff test actually
has teeth) established for PR #1.

## Two files named `fundamentals`, on purpose kept apart

`shared/fundamentals.py` (scoring, imported directly by
`shared/signals.py`) and `ingest/fundamentals_collector.py` (the SEC
EDGAR collector, imported only by `ingest/ingest.py`) are deliberately
**not** both named `fundamentals.py`. `ingest/Dockerfile` does
`COPY shared/ .` then `COPY ingest/ .` into the same flat `/app`
directory — two files sharing a basename would have the second `COPY`
silently overwrite the first on disk, breaking
`shared/signals.py`'s `from fundamentals import compute_fundamental_score`
at container startup. Caught during development, before it ever reached
a build; documented here so the naming doesn't look accidental or get
"fixed" back into a collision later.

## Data source and its limits

- **Ticker → CIK resolution**: SEC's public `company_tickers.json`, refetched
  on every daily sync (small, ~800KB — not worth a separate cache table).
- **Facts**: SEC EDGAR's `companyconcept` API, one request per (CIK, XBRL
  tag) — `Revenues` (with `RevenueFromContractWithCustomerExcludingAssessedTax`
  as a fallback for ASC 606 filers), `GrossProfit`, `NetIncomeLoss`. Only
  `USD`-denominated observations are kept.
- **`accepted_at` = the filing's own `filed` date.** SEC's `companyconcept`
  API doesn't expose a separate "accepted" timestamp at this granularity.
  Using `filed` is a deliberate, conservative simplification: it's the
  same calendar day as true SEC acceptance for the overwhelming majority
  of filings, and using it can only make point-in-time filtering
  *stricter* than reality (same-day-or-earlier, never later) — so it
  cannot introduce look-ahead bias, only (rarely, by at most a day)
  under-use data that was technically already public.
- **Not every watchlist symbol has SEC data.** ETFs, non-US-domestic
  filers, and anything not in `company_tickers.json` are silently
  skipped — `fundamental_score` stays `NULL` for them, never `0`.
- **Restatements are new rows, not overwrites.** `fundamental_facts` has
  no `UPDATE` path — a later 10-Q/A with a corrected number gets its own
  `accession_number` and its own row. A point-in-time query as of a date
  *before* the restatement was filed will never see the restated value
  — proven in `tests/test_fundamentals.py::test_point_in_time_excludes_facts_filed_after_as_of`.

## Scoring formula is a deliberate first cut

`shared/fundamentals.py::compute_fundamental_score()` combines three
simple threshold-scored inputs — revenue YoY growth, gross margin, net
margin — averaged over whichever are actually computable (never zero-filled
for a missing one). This is explicitly **not** a calibrated model; it
exists to prove the pipeline end-to-end with real data. Refining it —
more metrics, sector-relative normalization, a real statistical fit — is
expected before this component's weight in `component_weights` ever
moves off zero, per the same evidence-based activation discipline
`docs/signal-component-architecture.md` already established for every
other component family.

## What this does NOT do

No proposal-decision code path is touched. `component_weights` on every
`symbol_features`/`signal_outcomes` row stays `{"technical": 1.0}` —
`fundamental_score` being populated changes nothing about what
`compute_signals()` decides. The existing signal-component UI (symbol
page's breakdown panel) already renders any populated-but-zero-weight
component as "shadow" without any changes needed here — that behavior
was built into PR #1's `_component_status()` specifically so a future
component like this one wouldn't require API/UI changes just to show up
correctly.

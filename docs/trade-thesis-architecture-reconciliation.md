# Trade Thesis — Architecture Reconciliation

Design note, committed to the repo rather than pasted into a chat session —
same reasoning as
[`docs/thesis-horizons-and-intraday-data.md`](thesis-horizons-and-intraday-data.md)
and
[`docs/risk-engine-architecture-reconciliation.md`](risk-engine-architecture-reconciliation.md):
this resolves a naming collision surfaced while planning the
Hypothesis-Driven Trading Architecture epic (DocMost, "Epic: Hypothesis-
Driven Trading Architecture"), before any of that epic's PRs are written.
**No implementation in this PR — architecture only.**

## 0. The collision

The epic's PR 1 ("Canonical Machine Thesis Schema") described a per-trade
hypothesis object — entry conditions, evidence, invalidation criteria,
confidence — and said "existing `thesis_id` should be reconciled into this
model, not duplicated." That's wrong on inspection: `theses`
(`ingest/migrations/001_multi_thesis_architecture.sql`) is a **strategy-family
registry** — `mean_reversion`, `congress_shreve_hern`, each with a `horizon`
and `config`. `thesis_id` on `signals`/`trade_proposals`/`trades`/
`signal_outcomes`/`position_lifecycles` means "which strategy family
generated this," and is NOT NULL, indexed, and load-bearing across the whole
system. It does not mean, and has never meant, "what is the falsifiable
hypothesis for this specific opportunity." There is no existing object at
that level at all.

**Resolution: two objects, not one, related by FK, never merged.**

```
theses                          (existing, UNCHANGED)
  id, slug, display_name, status, horizon, config
  = "which strategy family?"
        |
        | thesis_id FK (existing, UNCHANGED — still what it always meant)
        v
trade_theses                    (NEW)
  id, thesis_id (FK -> theses), symbol, ...
  = "which specific, falsifiable hypothesis, for this one opportunity?"
```

Terminology going forward, everywhere in the epic doc and in code/PR
descriptions: **`theses` / `thesis_id` = strategy family** (unchanged
meaning). **`trade_theses` / `trade_thesis_id` = trade thesis instance**
(new). "Hypothesis Schema" → "Trade Thesis Schema." "Hypothesis Lifecycle"
→ "Trade Thesis Lifecycle." Reserve "strategy thesis" / "strategy family"
for the existing `theses` abstraction if a prose disambiguator is ever
needed.

This also clarifies the pipeline is three levels, not two:

```
STRATEGY FAMILY (theses)
      |
SIGNALS / symbol_features  (evidence — existing, unowned by any one thesis)
      |
TRADE THESIS (trade_theses)  — one falsifiable hypothesis for one opportunity
      |
TRADE PROPOSAL(S) / TRADE(S)
```

## 1. `trade_theses` schema (PR 1 scope, illustrative — final field list is PR 1's job)

```sql
CREATE TABLE trade_theses (
    id                  BIGSERIAL PRIMARY KEY,
    thesis_id           BIGINT NOT NULL REFERENCES theses(id),  -- strategy family
    symbol              TEXT NOT NULL,
    schema_version      TEXT NOT NULL,   -- Trade Thesis Schema (grammar) version -- see §1a
    evidence_context    JSONB NOT NULL,  -- versioned, multi-provider evidence lineage -- see §1b
    hypothesis_type     TEXT NOT NULL,   -- constrained vocabulary, PR1
    hypothesis_text     TEXT NOT NULL,   -- human-readable
    entry_conditions    JSONB NOT NULL,  -- grammar defined PR1, semantically validated PR3 -- see §1a
    evidence_snapshot   JSONB NOT NULL,  -- {supporting: [...], contradictory: [...], missing: [...]} -- PR4 output, frozen at creation
    invalidation_spec   JSONB NOT NULL,  -- grammar defined PR1, semantically validated PR3, consumed PR5/6 -- see §1a
    success_spec        JSONB NOT NULL,  -- grammar defined PR1, semantically validated PR3 -- see §1a
    confidence          NUMERIC CHECK (confidence BETWEEN 0 AND 1),
    provenance          JSONB NOT NULL,  -- explicit/inferred/proposed per field (PR17); trivially all-'explicit' through Phase 4
    status              TEXT NOT NULL DEFAULT 'proposed'
                        CHECK (status IN ('proposed','active','weakening','invalidated','completed','superseded')),
    as_of               TIMESTAMPTZ NOT NULL,  -- point-in-time evidence was evaluated
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`as_of` vs `created_at` follows the exact split
`docs/signal-component-architecture.md` already established for
`symbol_features`, for the same reason (a future backfill inserting
historical trade theses would set `as_of` in the past while `created_at`
reflects when the backfill ran).

### 1a. Grammar (PR 1) vs semantic validation (PR 3) vs load-bearing instantiation (PR 4)

Three distinct milestones, not one, previously conflated in the original
epic doc and in this doc's own §5:

- **PR 1 defines the canonical *grammar*** for `entry_conditions`,
  `invalidation_spec`, and `success_spec` — the JSON shape, the set of
  syntactically legal operators/comparators/logical combinators, how a
  condition references a feature identifier. PR 1 does **not** know
  whether any given feature identifier actually exists, is computable
  as-of a date, or resolves without lookahead. A `trade_theses` row can be
  well-formed per PR 1's grammar and still reference a feature that will
  never validate.
- **PR 3 (Trade thesis validator) is what makes a `trade_theses` row
  semantically real** — checks a spec against the Feature Registry (§5,
  PR 2), confirms every referenced feature is in the allowed vocabulary,
  data is available as-of the evaluation date, and no lookahead is
  possible. PR 3 can run standalone against hand-authored or backfilled
  `trade_theses` rows — it does not require the live signal-generation
  path to produce them.
- **PR 4 (Evidence evaluation engine) is the milestone where trade-thesis
  instantiation becomes load-bearing in production** — i.e., where
  `compute_signals()`'s live path actually starts creating `trade_theses`
  rows as a side effect of real evidence crossing a threshold, rather than
  the schema existing but being populated only by tests, backfills, or
  manual entries. Before PR 4 ships, `trade_theses` can exist, be
  well-formed (PR 1) and validated (PR 3), and still be **dark** — exactly
  the same staged-rollout discipline `shared/market_structure.py` already
  uses today (computed, tested, persisted, but "not yet load-bearing for
  any score or gate" per that module's own docstring). §3's "Instantiation"
  bullet below describes PR 4's end-state behavior, not something PR 1
  turns on.

### 1b. `evidence_context` replaces a single `feature_version` scalar

A single `feature_version TEXT` column assumes evidence comes from exactly
one versioned source. It doesn't: `docs/signal-component-architecture.md`
already establishes that `symbol_features` itself is multi-component by
design (`technical_score` today, fundamentals/news/options/macro components
planned, each independently versioned via `feature_version`/
`model_version`), and PR 5's invalidation model additionally pulls from
`market_structure_history` (`CALCULATION_VERSION`) and the three-level
regime infra (`market_regime`/`sector_regime`/`security_regime`, each with
its own versioning) — none of which are `symbol_features` rows at all. A
`trade_theses` row's evidence can legitimately span several of these at
once, each on its own version lineage.

`evidence_context` is JSONB, keyed by provider, one entry per evidence
source actually consulted for this thesis:

```jsonc
{
  "as_of": "2026-08-10",
  "providers": {
    "technical":       {"source": "symbol_features", "feature_version": "v1", "symbol_features_id": 48213},
    "market_structure": {"source": "market_structure_history", "calculation_version": 1, "as_of": "2026-08-10"},
    "market_regime":    {"source": "market_regime_history", "as_of": "2026-08-10"},
    "sector_regime":    {"source": "sector_regime_history", "as_of": "2026-08-10"}
  }
}
```

This is not new invention — it's the same per-component versioning
`symbol_features` already does for its own row, lifted one level so a
`trade_theses` row can reference *multiple* such rows/tables at once
instead of assuming everything funnels through one `symbol_features` row.
PR 2 (Feature Registry, §5) is what formalizes which provider keys are
legal and what each one's version field means — `evidence_context`'s shape
is PR 1 grammar, its allowed contents are PR 2/3's job, same split as §1a.

## 2. Threading — which tables get `trade_thesis_id`

| Table | Column added? | Nullability | Reasoning |
|---|---|---|---|
| `signals` | **No** | n/a | Signals are low-level evidence, not owned by any one trade thesis — multiple signals can feed one thesis, and a thesis may only get instantiated once evidence crosses a threshold. `evidence_snapshot` references the relevant `signal_outcomes`/`symbol_features` rows by id/`as_of`, not a live FK. Matches the user's stated caution about putting it on `signals`. |
| `trade_proposals` | Yes, `trade_thesis_id BIGINT REFERENCES trade_theses(id)` | NULL for proposals with no linked opportunity (manual entries) or where it can't be resolved to one value (see §2a for SELL) | On BUY, unambiguous — the proposal is generated *for* one specific `trade_theses` row. On SELL, `trade_thesis_id` is the **primary/triggering** thesis this exit was evaluated for, not a claim of FIFO share ownership — see §2a, this is the crux of clarification #2. |
| `trades` | Yes, `trade_thesis_id BIGINT` | Copied immutably from `trade_proposals.trade_thesis_id` at insert | Exact same pattern as the existing `trades.initial_stop_price`, copied immutably from `trade_proposals.planned_initial_stop_price` at the moment a trade is inserted. Same scalar-is-not-lot-accounting caveat as the row above applies here too — see §2a. |
| `position_lifecycles` | Yes, `trade_thesis_id BIGINT` | NULL if the lifecycle's constituent trades don't all share one `trade_thesis_id`, flag `concurrent_multi_trade_thesis_position` added to `data_quality_flags` | **Reuses the exact mechanism already in `shared/position_lifecycles.py::_flush()`** for the existing (strategy-level) `thesis_id`: a `trade_thesis_ids_seen` set accumulated per lifecycle across constituent trades, collapsed to a single value if `len(...) == 1` else `None` + a flag. Same code shape, new set, new flag string — see §3. **This is the authoritative layer for exit attribution**, not the scalar columns above — see §2a. |

### 2a. SELL `trade_thesis_id` is a scalar hint, not FIFO ownership

Two facts from the existing codebase mean a single `trade_thesis_id` on a
SELL `trade_proposals`/`trades` row cannot, in general, represent which
opportunity(ies) it actually closes:

1. **SELL proposals are generated per-symbol, aggregate-quantity, not
   per-lot.** `check_stop_losses()`, `check_regime_deterioration_sell()`,
   and `check_symbol_exits()` (`shared/signals.py`) all iterate the broker's
   `positions` dict keyed by symbol and propose selling `pos["qty"]` — the
   whole aggregate position — with no concept of which lot(s) that spans.
2. **A symbol's aggregate position can already legitimately span more than
   one strategy family concurrently** — that's exactly what
   `concurrent_multi_thesis_symbol` exists to flag today. Once
   `trade_theses` exists, the same symbol can equally span more than one
   *trade thesis instance* (e.g. `mean_reversion` holds 100sh of TTD under
   one `trade_theses` row while `congress_shreve_hern` independently holds
   50sh of TTD under a different one) — a single "sell my TTD position"
   proposal for 150sh has no single correct `trade_thesis_id`.

**Resolution:** `trade_thesis_id` on `trade_proposals`/`trades` is
best-effort, informational metadata — "which thesis triggered/was primarily
evaluated for this exit," useful for the common unambiguous case and for
display — never treated as authoritative lot-level attribution. It may be
NULL on a SELL when no single thesis is unambiguously primary. **The
authoritative answer to "which trade thesis does this exit's P&L actually
belong to" is resolved downstream, at the same place cross-strategy
ambiguity is already resolved today**: `position_trades.qty_allocated`
already lets one `trades` row split its quantity across more than one
`position_lifecycles` row (pyramided entries, partial exits — this
capability exists now, per `ingest/schema.sql`'s comment on
`position_trades`). When an exit's shares are allocated across lifecycles
belonging to different `trade_theses` (via §2's `position_lifecycles.
trade_thesis_id`), that allocation — not the scalar column on `trades` — is
the ground truth PR 11/12 must join against for per-thesis realized-R and
outcome attribution. No new join table needed; `position_trades` already
has the right shape for this.

## 3. Cardinality — resolves the pyramiding/partial-fill question

**A `trade_thesis` is per-opportunity, not per-fill or per-proposal.** One
`trade_theses` row can be referenced by more than one `trade_proposals` row
and more than one `trades` row over its life:

- **Instantiation.** This describes PR 4's end-state, live-production
  behavior (per §1a) — not something PR 1 turns on. Once PR 4 ships, a
  `trade_theses` row is created at the moment evidence crosses whatever
  threshold PR 4 defines, in the same code path/transaction that would
  otherwise go straight to `trade_proposals`. The first `trade_proposals`
  row and its `trade_theses` row are created together. Before PR 4,
  `trade_theses` rows can exist (PR 1 grammar, PR 3 validation) without the
  live signal path creating them — dark, same as `market_structure.py`
  today.
- **Pyramiding.** A second BUY proposal adding to the same still-open
  opportunity, while the thesis remains `active` (not yet `invalidated` /
  `completed`), reuses the existing `trade_thesis_id` rather than minting a
  new row — this mirrors how `position_lifecycles` already treats
  pyramided entries as one lifecycle built from multiple `trades` rows via
  `position_trades`.
- **Exits.** SELL proposals that close a position (fully or partially)
  carry `trade_thesis_id` as a best-effort primary/triggering hint, not
  authoritative ownership — see §2a. Required for PR 8's per-thesis
  exit-reason accounting in the common (unambiguous) case; the
  `position_trades`-based attribution in §2a is authoritative when a SELL
  actually spans multiple opportunities.
- **New opportunity, same symbol.** If a symbol goes flat and evidence
  later re-crosses the threshold, that's a brand-new `trade_theses` row,
  even with an identical `hypothesis_type` — thesis identity is scoped to
  one opportunity/holding period, not to the symbol or the strategy
  family.
- **Ambiguity at the lifecycle level.** `build_position_lifecycles.py`
  already handles the equivalent ambiguity one level up — the existing
  `thesis_id` (strategy family) is set to `None` and
  `concurrent_multi_thesis_symbol` flagged when a lifecycle's trades span
  more than one strategy family (`shared/position_lifecycles.py:169-173`).
  Extend the same `_flush()` logic with a second, independent
  `trade_thesis_ids_seen` set for the new column — same collapse-to-
  single-or-None-plus-flag shape, not a new mechanism.

This directly answers the open question from the epic doc: **`trade_thesis`
is per-opportunity**, exactly as guessed. `trade_hypothesis_instance` tied
1:1 to a single fill would have been the wrong model.

## 4. Immutability

Once created, a `trade_theses` row's `entry_conditions`, `evidence_snapshot`,
`invalidation_spec`, `success_spec`, `confidence`, and `provenance` are
**never updated in place** — same snapshot-then-freeze precedent as
`trade_proposals.planned_entry_price` / `planned_initial_stop_price`, copied
immutably down into `trades` and then `position_lifecycles`, and the same
calculate-then-store, append-only-over-computed-values convention already
used elsewhere in this codebase (`symbol_features`, `signal_outcomes`).

`status` is the one field that changes over the thesis's life (PR 10, Live
Thesis Re-Evaluation) — but it should not be a blind `UPDATE trade_theses
SET status = ...` either. Follow `ingest/postmortem.py`'s existing
discipline: re-evaluation writes an **append-only** row to a new
`trade_thesis_evaluations` table (`evaluated_at`, `state`, `evidence_diff`,
`triggering_condition`), and `trade_theses.status` becomes a denormalized
read of "most recent evaluation's state," refreshed by that same job —
recompute-from-history-and-write-one-summary-row, not incremental mutation.
This also directly satisfies PR 9's requirement that "later strategy edits
must not rewrite historical intent."

## 5. What this changes about the epic's PR list

Renames only where noted; scope changes ("build" → "extend/integrate") are
carried over from the earlier repo-recon pass and restated here for the
single source of truth:

- **PR 1 — `trade_theses` schema + object model** (renamed from "Canonical
  Machine Thesis Schema"). Defines the **grammar** only (§1a) — JSON shape
  and syntactically legal operators for `entry_conditions`/
  `invalidation_spec`/`success_spec`, plus `evidence_context`'s
  multi-provider shape (§1b). Does not validate feature existence/
  availability (PR 3) and does not make instantiation load-bearing in
  production (PR 4). Genuinely new — no existing per-opportunity object to
  extend. Threading per §2 (incl. §2a's SELL caveat), cardinality per §3,
  immutability per §4.
- **PR 2 — Feature Registry.** A **metadata/code layer over**
  `symbol_features` and the other evidence providers `evidence_context`
  can reference (`market_structure_history`, the three regime tables) —
  identifier, inputs, output type, data availability, as-of-safe eval
  function, per provider. `symbol_features` itself is **not modified** and
  remains pure observation storage, same as today; the registry describes
  and validates against it (and the other provider tables) rather than
  being folded into it. This still avoids inventing new
  versioning/confidence conventions — the registry's per-provider version
  fields are just formalizing what `symbol_features`'
  `feature_version`/`model_version`/`data_confidence` and
  `market_structure.py`'s `CALCULATION_VERSION` already do independently.
- **PR 3 — Trade thesis validator.** Genuinely new. Semantically validates
  a PR-1-grammar-conformant spec against PR 2's registry (§1a) — feature
  existence, data availability as-of a date, no lookahead. Runs standalone
  against hand-authored/backfilled `trade_theses` rows; does not require
  PR 4's live path.
- **PR 4 — Evidence evaluation engine.** Genuinely new. This is the
  milestone (§1a) where `trade_theses` instantiation becomes load-bearing
  in the live `compute_signals()` path, evaluating against existing
  `symbol_features`/`signal_outcomes`/other registered providers — not a
  new data layer, but real production wiring, unlike PR 1/3 which can ship
  dark.
- **PR 5 — Invalidation model.** Extend the existing (currently inert)
  `shared/market_structure.py` + regime infra (`market_regime.py`,
  `sector_regime.py`, `security_regime.py`, `regime_common.py`) into formal
  invalidation evaluation — do not reimplement structure/regime detection.
- **PR 6 — Structure-aware stop resolver.** Derive
  `thesis_invalidation_price` and feed it into the existing
  `trade_proposals.planned_initial_stop_price` — that column and its
  immutable copy-down into `trades.initial_stop_price` /
  `position_lifecycles` already exist end-to-end.
- **PR 7 — Risk sizing from thesis invalidation.** Small integration PR:
  `shared/risk_engine.py::evaluate_proposal()` already sizes purely from
  `planned_initial_stop_price` + risk-per-trade-pct + open-risk cap +
  sector caps. No new sizing engine — prove it consumes a thesis-derived
  stop correctly.
- **PR 8 — Exit taxonomy.** Normalize/extend the existing
  `trade_proposals.exit_reason` values (`stop_loss`, `thesis_complete`,
  `time_stop`, regime-deterioration sells already exist) using the new
  per-thesis `trade_thesis_id` threading from §2 to distinguish thesis
  invalidation from emergency stop from profit target.
- **PR 9 — Thesis snapshot on proposal/position.** Mostly already true by
  construction once §2/§4 land — `trade_thesis_id` threads through
  immutably the same way `initial_stop_price` already does. Confirm/close
  gaps rather than build fresh.
- **PR 10 — Live thesis re-evaluation.** New `trade_thesis_evaluations`
  table per §4; reuses `ingest/postmortem.py`'s recompute-and-write-summary
  pattern.
- **PR 11 — Post-trade thesis evaluation.** Extend
  `ingest/postmortem.py`'s existing lifecycle-expectancy computation
  (already segments by score/regime/approval-bucket/rule/risk-constraint)
  with a `trade_thesis_id`/`hypothesis_type` segmentation axis, rather than
  building a parallel analytics stack.
- **PR 12 — Hypothesis analytics.** Same extension as PR 11 — new
  segmentation axis on the existing `strategy_review_proposals` /
  Strategy Review pipeline.
- **Phase 4-6 (PR 13-20+).** Remain genuinely new as originally scoped —
  no existing infra to reconcile against.

## 6. Explicitly deferred, not decided here

- Exact `hypothesis_type` vocabulary (PR 1's job).
- Exact operator/comparator/combinator set for `entry_conditions`/
  `invalidation_spec`/`success_spec`'s grammar (PR 1's job, per §1a — must
  be a strict-enough subset that PR 3 can validate mechanically, not a
  free-form escape hatch).
- Exact legal `evidence_context.providers` keys and per-provider version
  field contents (PR 2's job, per §1b).
- Whether `trade_thesis_evaluations.state` values match `trade_theses.status`
  1:1 or are a richer enum (PR 10's job).

No code changes accompany this doc. Next step is scoping PR 1 concretely
against §1-§4 above, informed by §1a/§1b/§2a.

## 7. PR 1 scope (grammar only, per §1a — not implemented yet)

**In scope:**

1. **Migration** — `trade_theses` table per §1, with `evidence_context`
   (§1b) not a scalar `feature_version`. `hypothesis_type` ships with a
   minimal placeholder set sufficient to port `mean_reversion` (full
   vocabulary is explicitly PR 1's deferred item per §6, not PR 2's — it's
   grammar-adjacent enum work, no registry dependency) rather than trying
   to enumerate every future strategy's types now.
2. **`shared/trade_thesis.py`** — object model, same shape as the existing
   `shared/signal_components.py` precedent (typed dataclass, no DB/IO) plus
   a persistence helper mirroring `shared/feature_store.py`'s fail-open,
   best-effort insert pattern (not called from any live path — see below).
   Contains:
   - `TradeThesis` dataclass mirroring the table columns.
   - `TRADE_THESIS_SCHEMA_VERSION` constant (same role as
     `feature_store.py`'s `FEATURE_VERSION`).
   - The condition-grammar constants: legal operators (`gt`/`lt`/`gte`/
     `lte`/`eq`/`between`) and logical combinators (`and`/`or`/`not`) for
     `entry_conditions`/`invalidation_spec`/`success_spec`, and the
     recursive JSON shape a condition tree must satisfy — **syntax only**,
     no check that a referenced feature identifier exists (PR 3's job).
   - `evidence_context` shape validation (§1b's `{as_of, providers: {...}}`
     envelope) — again structural, not "is this provider key registered"
     (PR 2/3's job).
3. **Tests** — schema round-trip (construct → serialize → deserialize),
   grammar-rejection cases (malformed operator/combinator caught at
   construction, not silently accepted), and a test asserting there is no
   update path exposed for the immutable fields (§4) — i.e., the object
   model itself makes the immutability contract structurally hard to
   violate, not just documented.

**Explicit open scope decision, flagging rather than silently picking:**
whether PR 1 also adds the *columns* `trade_proposals.trade_thesis_id` and
`trades.trade_thesis_id` (nullable, unpopulated, dark — no writer wired
yet) versus deferring column addition to PR 4/PR 9 alongside their first
writers. Recommendation: add both columns in PR 1 alongside the migration,
same precedent as `price_history_hourly` ("nothing reads this table yet")
— cheap, additive, avoids a second migration touching the same
high-traffic tables later — but leave `position_lifecycles.trade_thesis_id`
and the `_flush()` `trade_thesis_ids_seen` extension (§2, §3) for PR 9,
since that one requires actual derivation logic, not just a bare column.

**Explicitly out of scope for PR 1** (so it doesn't silently creep in):
Feature Registry integration (PR 2), semantic/lookahead validation (PR 3),
any code path that actually creates a `trade_theses` row in production
(PR 4 — see §1a), `position_lifecycles.trade_thesis_id` derivation (PR 9),
`trade_thesis_evaluations` (PR 10).

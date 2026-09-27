# Hypothesis-Driven Trading — Phase 5a Scoping (LLM candidates + candidate backtest bridge)

Scoping note, no implementation. Follows
[`hypothesis-driven-phase5-6-llm-compiler-investigation.md`](hypothesis-driven-phase5-6-llm-compiler-investigation.md)
(#133), whose three open questions are now answered:

| #133 open question | Decision (2026-09-26) |
|---|---|
| 1. Which model generates candidates | Local Qwen on AI2 (`llama-server`, OpenAI-compatible, `10.10.10.226:8080`) |
| 2. Human review gate before a candidate is stored | No gate. Candidates stay **inert**, carry full **provenance**, and batches are **capped** |
| 3. Does 5a matter before Strategy Incubator exists | Moot: Strategy Incubator Phase 1 (SI-1/2/3) shipped 09-18, so a candidate can now be registered as a `RESEARCH` strategy version |

Scope was also widened on 2026-09-26: Phase 5a now includes a **bridge that backtests a
candidate's condition trees**, since without one an LLM-generated candidate has nowhere to be
evaluated. Candidates are backtested **automatically**, with guardrails (§4).

## 1. What exists today (verified against origin/main 8382932)

- `hypothesis_candidates.generate_candidates(conn, type_key, parameter_spec, generated_by=...)`
  takes `{feature_id: [values...]}` and substitutes each value into matching leaves of the
  type's template trees (Cartesian product). It validates every tree with
  `trade_thesis.validate_condition_tree()` and persists `candidate_batches` / `candidates`.
- `strategy_lifecycle.register_candidate_as_strategy_version()` (SI-3) creates a `RESEARCH`
  strategy version from a candidate. Transitions beyond `RESEARCH` are manual API calls.
- `trade_thesis_invalidation.evaluate_condition_tree(conn, node, symbol, as_of)` gives Kleene
  three-valued evaluation over `feature_registry.evaluate_feature()`. It is correct but does
  **one DB load per feature per (symbol, date)**: fine for live checks, far too slow for
  backtests (about 500 symbols × about 1,500 dates × each leaf).
- **No candidate→backtest path exists.** `GET /api/backtest/{strategy_key}/{symbol}` runs only
  hand-coded strategies from `strategy_registry.py`, one symbol at a time, at default parameters.
- `ingest/research/backtests/backtest_exit_policy_replay.py` (#155) already replays the live
  mean-reversion exits (thesis_complete / overbought / time stop / regime / 12% stop) on
  adjusted bars, with parallel per-symbol workers.
- Prod state: `candidate_batches` / `candidates` / `strategies` / `strategy_versions` are empty
  (see the 09-18 cleanup incident on the roadmap). `hypothesis_types` has 9 rows.

### Which hypothesis types can be backtested historically

| Type | Entry-tree features | Historical data | Bridge v1 |
|---|---|---|---|
| `mean_reversion_oversold` | `technical.rsi_14`, `technical.bb_pct_b` | price_history | ✅ |
| `bollinger_breakout_continuation` | `technical.bb_pct_b` (+ invalidation) | price_history | ✅ |
| `structural_breakout_momentum` | `structural_events.recent_event_type` | structural_events, append-only, confirmation_time from 2016 | ✅ |
| `fvg_reaction_momentum` | `structural_events.recent_event_type` | same | ✅ |
| `structural_support_bounce` | `structural_zones.nearest_support_distance_atr` | zones must be **re-clustered as-of each date** from structural_swings (lookahead guard in feature_registry): expensive | ❌ later |
| `mean_reversion_overbought` | rsi / %B, sell-side | engine is long-only | ❌ (not an entry hypothesis) |
| `ema_crossover_trend`, `supertrend`, `daily_8ema_momentum_retest` | NULL trees (hand-coded strategies) | – | ❌ (nothing to substitute) |

`market_structure.trend_state` is also unusable for backtests: `market_structure_history`
starts 2026-08. The bridge **fails closed** on any unsupported feature: the candidate is
recorded as `unsupported_feature`, never silently evaluated as False.

## 2. PR breakdown

### 5a-1 — Candidate backtest bridge (no LLM; useful on its own)
- **`shared/feature_series.py`:** a vectorised, as-of-safe *series* form of each supported
  feature. It takes a symbol's full bar history once and returns one value per bar. It is built
  on the same math the live `eval_fn` uses (`compute_rsi`, `compute_bollinger`, the
  structural_events read), with **parity tests** asserting that `series[t] == evaluate_feature(as_of=date[t])`
  on sampled dates, so the backtest cannot drift from the live definition. It uses
  **split/dividend-adjusted bars**. (Live `feature_registry` reads raw `close`; this is a
  pre-existing discrepancy, noted not fixed here.)
- **Tree evaluator over series:** the same Kleene semantics as `evaluate_condition_tree`,
  shared through one `_apply_operator`.
- **Candidate backtest:**
  - Entry: `entry_conditions` True at close t, fill at open t+1, flat-only, one position per
    symbol.
  - Exit: `invalidation_spec` / `success_spec` if present, **plus a declared evaluation exit
    policy per family**, identical for every candidate of a type. No type defines
    `success_spec` today, so without a declared exit there would be nothing to exit on.
    Proposed exit families:
    - mean-reversion types: the live MR exits (reusing #155's replay logic);
    - breakout / structural types: invalidation_spec, 12% stop and a fixed 20-bar time stop.
  - Costs: 12.5 bps per round trip.
- **Storage:** new append-only table `candidate_backtests`.
  - Holds `candidate_id`, `run_config` (window, exit policy, costs, universe, git sha,
    code/parameter hash), `metrics` and `status`.
  - Status is one of `complete`, `unsupported_feature`, `insufficient_trades` or `error`.
  - Never updated in place; a re-run appends a new row.
  - `backtest_results` stays reserved for the one-off research scripts.
- **API:** `POST /api/candidates/{id}/backtest` (enqueue) and
  `GET /api/candidates/{id}/backtests`.
- **Metrics:**
  - trade count, distinct weeks, mean and median net return per trade, win rate, profit
    factor, average hold;
  - week-block bootstrap 90% CI of the mean;
  - the same numbers for the **type's default-template candidate** as a reference row;
  - per-era split.

### 5a-2 — LLM candidate generator (Qwen on AI2)
- **`shared/llm_hypothesis_generator.py`.**
  - Prompt contents: the type's catalog description, the template trees, and the
    substitutable features, with their descriptions, value types and current template values.
  - Asks for strict JSON `{"parameter_spec": {feature_id: [values...]}, "rationale": "..."}`.
  - Calls `generate_candidates(..., generated_by="llm:<model>@ai2")`.
- **Validation before generation** (all failures reject the whole batch, logged; nothing is
  truncated silently):
  - keys must be a subset of the template's features;
  - values must be scalars of the feature's type;
  - the Cartesian product must be ≤ **`llm_candidate_batch_cap` (default 12)**.
- **Provenance** is stored per batch in a new `candidate_batches.llm_provenance` JSONB (additive
  column; `generated_by` keeps its meaning):
  - model id as reported by the server (the llama-server alias is cosmetic);
  - endpoint, prompt template version, a hash of the full prompt, and the raw response;
  - sampling parameters, rationale, timestamp.
- **Rate limit:** `llm_candidate_batches_per_day` (default 5).
- **Inert:** nothing registers a strategy version, transitions a lifecycle state, or touches
  `trade_theses`, proposals or orders.
- **Trigger:** on demand only in 5a (`POST /api/hypothesis-types/{key}/llm-candidates`, plus a
  button next to the type). A scheduled generator is a later decision.
- **Failure handling:** fail-open. If Qwen is unreachable or returns malformed JSON, no batch is
  created and the error is returned to the caller. Nothing retries in a loop.

### 5a-3 — Automatic backtesting of new candidates
- A candidate created by **any** generator (human sweep or LLM) gets a `pending` row in
  `candidate_backtests`.
- The ingest cycle runs up to N pending backtests per cycle (default 12), so no new
  infrastructure or daemon is needed.
- Results show on the candidate batch view with the guardrail context below.

## 3. Why automatic backtesting: yes, but it creates a data-mining machine

Backtesting every generated candidate automatically is the right default. An unevaluated
candidate is clutter, and "a human will run it later" has in practice meant never (no
candidate generated since PR14 was ever evaluated, because there was no path to do so).

But an LLM proposing variants, plus automatic evaluation, plus a human scanning a ranked list
**is a multiple-testing loop**. Run enough candidates and the best in-sample backtest looks
good by chance. This is also the survivors-only universe. So automation is only acceptable with
the guardrails below, and these are the part of 5a that matters most.

## 4. Guardrails (proposed; enforced in code, not by convention)

1. **Sealed holdout.** Automatic backtests run only on a discovery window (proposed: data
   start → **2024-12-31**). Data from **2025-01-01** onward is refused by the bridge for any
   automatic or on-demand candidate run. The boundary is a single constant, recorded in every
   `run_config`. The holdout is reserved for the Strategy Incubator's `VALIDATION` /
   `WALK_FORWARD` stages, which are human-triggered and not built yet. It is never used to
   rank candidates.
2. **Every trial is counted.**
   - All results are kept, including failures and `insufficient_trades`.
   - Each result displays the **cumulative number of candidates evaluated for that hypothesis
     type**, so a "best of 60" is never presented as if it were a single test.
   - Deflated-Sharpe or Bonferroni-style adjustment can be computed later from the stored
     count.
3. **No feedback to the generator.** The LLM prompt never contains backtest results in 5a. A
   generate → evaluate → regenerate loop is Phase 6 (autonomous mutation) and stays blocked on
   Strategy Incubator Phase 2.
4. **Inert results.** A backtest result never changes a candidate's or strategy version's
   status. Registering a candidate as a strategy version, and every lifecycle transition,
   stays a human API call.
5. **Reference, not absolute, framing.** Each result is shown next to the type's
   default-template candidate and the bootstrap CI, with a minimum-evidence rule: fewer than
   30 trades or 20 distinct weeks → `insufficient_trades`, and no metrics are shown as if
   meaningful.
6. **Public repo.** Code is mechanism-only. Prompts are generic templates. Generated parameter
   values, rationales and results live in the DB only, never in git or PR text.

## 5. Explicit non-goals for 5a
- Free-text thesis → condition tree compilation (Phase 5b).
- LLM-authored *new* condition trees (only new values for existing template leaves).
- Short / sell-side hypotheses; `structural_zones` features.
- Any path to `trade_theses`, proposals, orders or live signal parameters.
- Phase 6 (mutation / feedback loop).

## 6. Open decisions (proposed defaults in bold)
1. Holdout boundary: **2025-01-01** (discovery 2020/21–2024; about 21 sealed months).
2. Evaluation exit policies per family as in 5a-1: **live MR exits for MR types; invalidation
   spec + 12% stop + 20-bar time stop for breakout / structural types.**
3. PR order: **5a-1 → 5a-2 → 5a-3**. The bridge first, because it is useful for human sweeps
   even without the LLM, and because 5a-3's guardrails need it.

Honest expectation: Qwen proposing threshold values for 2–4 existing templates is a modest
source of ideas, not much beyond a thoughtful grid. Most of 5a's value is the end-to-end
generate → evaluate → review path with trial accounting, which 5b (new trees) and the Strategy
Incubator's later phases can then build on.

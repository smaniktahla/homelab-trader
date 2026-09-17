# Hypothesis-Driven Trading — Phase 5/6 Investigation (LLM Thesis Compiler, Autonomous Mutation)

Investigation note, committed to the repo rather than decided unilaterally in a
chat session — same reasoning as
[`docs/trade-thesis-architecture-reconciliation.md`](trade-thesis-architecture-reconciliation.md)
and
[`docs/peer-intelligence-architecture-reconciliation.md`](peer-intelligence-architecture-reconciliation.md).
**No implementation in this PR — investigation and scoping only.**

PR1-18 of the Hypothesis-Driven Trading Architecture epic are merged and
deployed. The only remaining scope is Phase 5 ("LLM thesis compiler") and
Phase 6 ("autonomous mutation") — both named in the original epic outline but
never given a concrete design, unlike PR1-18's detailed spec
(`docs/trade-thesis-architecture-reconciliation.md`). This doc is that missing
scoping pass.

## 1. What already exists (don't rebuild this)

The full generation path today:

```
HypothesisType (PR13, catalog template)
    |
    v
Candidate (PR14, hypothesis_candidates.py::generate_candidates())
    |
    v
   ... nothing consumes this yet ...
```

`generate_candidates()` already takes a `generated_by` field
(`candidate_batches.generated_by`) — provenance for *who/what* produced a
batch. Today every caller passes a human-authored parameter sweep
(`{"technical.rsi_14": [20, 25, 30]}`); nothing stops a caller from passing
`generated_by="llm:claude-opus-5"` instead. **This means the mechanical
plumbing for "an LLM produces candidates" already exists and needs no new
grammar, no new table, no new validation path** — `generate_candidates()`
still runs every generated tree through
`trade_thesis.validate_condition_tree()` regardless of who supplied the
parameter values.

What does NOT exist: anything that consumes a `Candidate` row. Per PR14's own
module docstring: *"There is no callable backtest-invocation entry point
anywhere in this repo... this module does not attempt to wire candidates into
backtest execution."* A candidate generated today — by a human sweep or,
hypothetically, an LLM — sits in the `candidates` table with no path to a
backtest run, no path to promotion, no path to ever influencing a live
`TradeThesis`.

## 2. The blocking finding: Phase 5/6 depend on a Strategy Incubator that was never built

The epic's own conceptual hierarchy (agreed 2026-08-28, DocMost note
`d8f12b78-a9de-4ddd-9664-05b08667bc01`):

```
HypothesisType -> ResearchExperiment -> StrategyVersion -> TradeThesis -> TradeProposal -> Order/Position
```

`ResearchExperiment` and `StrategyVersion` are not this epic's concepts —
they belong to the separately-scoped **Strategy Incubator & Validation
Pipeline** epic (DocMost note `0a93c01c-4115-47fe-8fb0-bf055ca17fb0`,
`strategies`/`strategy_versions`/`research_experiments`/promotion-gates
schema, its own §23/§1/§13). The original plan was for this epic's "PR15 —
Strategy Incubator Integration" to register `Candidate` rows as
`ResearchExperiment`/`StrategyVersion` objects, handing validation/promotion
to that epic's machinery — explicitly **not** a second, parallel promotion
system ("One strategy-promotion architecture, not two").

**That PR15 was never built.** What actually shipped as PR15-18 (verified via
`git log`/the roadmap) is a different, renumbered decomposition: the
deterministic backtest engine (`shared/backtest_engine.py`), two indicator
strategies, a visualization API, and SuperTrend — genuinely useful work, but
not the Strategy Incubator integration point. The Strategy Incubator epic
itself remains **parked, no PRs written**, per the current roadmap.

Consequence: if Phase 5 built an LLM that generates `Candidate` rows today,
those rows would land in exactly the same dead end every hand-authored sweep
already lands in — no backtest wiring, no promotion path, no way for a result
to ever reach a live `TradeThesis`. Phase 6 ("autonomous mutation") is worse:
mutation implies a feedback loop from backtest/validation results back into
new candidate generation, and there is currently no structured backtest
*result* to feed back from — `backtest_results` (used by the
`ingest/research/backtests/*.py` significance-test scripts, e.g. the Volume
epic's Experiments 012-017) is a one-off JSON blob per manual script run, not
a `StrategyVersion`-keyed, walk-forward-validated result the way the
Incubator spec calls for.

Building Phase 5/6 now, ahead of Strategy Incubator, would either (a)
duplicate a chunk of the Incubator's own promotion/versioning machinery
inside this epic — the exact anti-pattern PR14's own docstring and the
2026-08-28 reconciliation both explicitly ruled out — or (b) produce
LLM-generated candidates that are pure research clutter with no path to
production, which is arguably worse than not building it, since it invites
someone (human or a later session) to eventually promote an LLM-authored
thesis into `TradeThesis`/live trading through some ad-hoc side door instead
of the one real promotion path.

## 3. Recommended re-scoping

**Split Phase 5 into two pieces with different urgency, and defer Phase 6
entirely.**

### Phase 5a — LLM Hypothesis Candidate Generation (buildable now, narrow)

A safe, small slice that needs no Strategy Incubator dependency:

- A new module (e.g. `shared/llm_hypothesis_generator.py`) that prompts an LLM
  with a `HypothesisType`'s catalog description + the current
  `feature_registry.py` provider/feature list, asking it to propose new
  parameter values (or, more ambitiously, an entirely new condition tree) —
  then calls the **existing** `hypothesis_candidates.generate_candidates()`
  with `generated_by="llm:<model-id>"`. No new grammar, no new validation, no
  new promotion path — LLM output is just another source of parameter values
  feeding a mechanism that already exists and is already tested.
- Explicitly out of scope for this slice: executing, backtesting, scoring, or
  promoting anything the LLM generates. It sits in `candidates` exactly like
  a hand-run sweep does today, inert until a human (or, later, the Strategy
  Incubator) does something with it.
- This is genuinely useful on its own — it's a research-idea-generation aid,
  not an autonomous trading system — and doesn't create the dead-end/side-door
  risk in §2 because it produces nothing more consequential than what
  `generate_candidates()` already produces for human-authored sweeps.

### Phase 5b (full "compiler": free-text thesis -> validated grammar) — defer

The more ambitious reading of "LLM thesis compiler" — an LLM writing prose
like *"XYZ looks oversold relative to sector peers with declining volume"* and
the system compiling that directly into a `TradeThesis`-shaped
`entry_conditions` tree — needs no new blocking dependency either (it's the
same `validate_condition_tree()` gate), but is a materially larger prompt-
engineering and reliability problem (structured extraction, hallucinated
feature names, grammar-limitation mismatches like the ones `ema_crossover_trend`
and `supertrend` already hit) than Phase 5a. Recommend treating Phase 5a as
the actual next PR if this is picked up, and scoping 5b only after 5a has
running experience with real feature-registry-aware LLM prompting.

### Phase 6 — Autonomous mutation: **do not start until Strategy Incubator Phase 1-2 exist**

Autonomous mutation is a feedback loop: generate → evaluate → mutate based on
results → repeat. There is currently no `StrategyVersion`/`backtest_runs`
schema for it to read results from, and building one just for this epic would
be the exact duplicate-architecture outcome §2 warns about. Recommend this
stays explicitly blocked on Strategy Incubator's own Phase 1 (versioning/
lifecycle) and Phase 2 (validation/walk-forward) — matching the roadmap's own
current framing of Phase 5-6 as "long-horizon, lowest urgency."

## 4. Open questions (for the user, not decided here)

1. **Which LLM/model** would generate candidates in Phase 5a — a local
   Hermes model (AI1/AI2) or an external API call? Cost/rate-limit
   implications differ a lot between "call Claude/GPT per hypothesis type
   sweep" vs. "call a local model already running for other homelab
   purposes."
2. **Human review gate**: should every LLM-generated candidate require a
   human glance before even sitting in the `candidates` table (e.g. a
   `pending_review` status), or is "inert until a human runs a backtest
   against it" already a sufficient gate? The Incubator spec's own principle
   ("a strategy is a disposable hypothesis until sufficient evidence") argues
   the latter is fine, but worth confirming given this would be the first
   LLM-authored (not human-authored) row in a table that currently has zero
   AI-generated content.
3. **Does Phase 5a matter before Strategy Incubator exists at all?** Given
   §2's dead-end finding, it may be more valuable to prioritize Strategy
   Incubator Phase 1 (versioning/lifecycle foundations) over Phase 5a, since
   Phase 5a's output has nowhere to go either way — the roadmap currently
   lists Strategy Incubator as "parked behind Hypothesis-Driven Phase 4"
   which is now done, so the parking condition has already been met.

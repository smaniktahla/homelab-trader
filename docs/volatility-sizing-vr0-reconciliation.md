# Volatility Forecasting & Risk-Targeted Position Sizing — VR-0 Reconciliation

**Status**: Accepted, with a design addendum (§6.3–§6.6) closing the forecast-contract,
units, fallback, and budget-formula questions before VR-1/VR-2 implementation begins
**Date**: 2026-09-03 (addendum added same day)
**Purpose**: Pre-implementation inventory and integration plan for a volatility-aware
position-sizing overlay, produced before writing any estimator or sizing code, per the
VR-0 requirements in the supplied epic roadmap. This document reconciles that roadmap
against the repository's actual state and records the resulting decisions.

All facts below were verified directly against the repository
(`/home/salil/claude-workspace/homelab-trader`) via direct file inspection — file paths
and line-level behavior, not recalled from memory or prior session handoffs. PR
identifiers (VR-0…VR-6) are epic-local labels, not GitHub PR numbers. This document
authorizes no production behavior change; it is documentation only.

---

## 0. Summary — the roadmap's boundaries mostly hold, with three corrections

The supplied roadmap treated several things as open questions to resolve during VR-0.
Reconciliation against the codebase resolves most of them in favor of **reusing existing
infrastructure rather than building new components**:

- Sizing already has an authoritative single decision point (`risk_engine.py`) that
  generalizes cleanly to a volatility input — no parallel sizing system needed.
- The Hypothesis Library (PR13-15) is real, tested code, not aspirational — but it is
  explicitly *not* the right place to attach risk-policy versioning.
- One shared, lookahead-safe backtest engine exists, but it is single-symbol/unit-qty —
  a portfolio-level extension is a real, separately-trackable dependency, not something
  to invent quietly inside an estimator PR.
- Dividend adjustment does not exist anywhere in the data layer — a real gap, addressed
  by scoping rather than blocking (§3).

---

## 1. Existing sizing and risk architecture (reuse, do not duplicate)

| Concern | File(s) | Current ownership |
|---|---|---|
| Strategy-side sizing | `shared/signals.py::calc_buy_qty()` (`signals.py:416`) | Fixed-fractional (% of cash), capped by `max_position_pct` + sector cap. Called from `compute_signals()` (`signals.py:1153`). |
| Authoritative binding clamp | `shared/risk_engine.py::evaluate_proposal()` (`risk_engine.py:76-212`) | "The one authoritative source for approved_quantity." Computes `min()` across buying-power, position-allocation, stop-distance risk-budget, portfolio-open-risk, and sector-exposure candidates (`risk_engine.py:191-200`). |
| ATR today | `shared/signals.py::compute_atr()` (`signals.py:220`); `shared/market_structure.py::_volatility_context()` (`market_structure.py:314-328`) | **Score/context only.** ATR feeds a proposal-score modifier and a compression/normal/expansion regime label — never sizing. |
| Order rounding | All sizing candidates | `math.floor()` throughout (`risk_engine.py:128,137,150,165,182`); integer shares only, no fractional-share orders. Rounding is always conservative — never rounds up past the computed budget. |
| Stop-distance risk budget | `risk_engine.py:143-150` | `risk_qty = floor(risk_budget_dollars / risk_per_share)`, where `risk_per_share = price - planned_initial_stop_price`. Stop-distance-based (R-multiple), not volatility/ATR-based. This is the closest existing precedent to a volatility-scaled budget — same shape, different input. |
| Execution gate | `api/main.py::_clamp_to_risk_engine()` (`api/main.py:1298-1359`) | Called from both `POST /api/trade` and `PATCH /api/proposals/{id}` — the actual pre-order gate. |

**Architectural invariant carried forward from this inventory** (see §6 for the binding
decision): `risk_engine.evaluate_proposal()` remains the single authoritative quantity
decision point. Volatility sizing is not a parallel engine and does not mutate order
quantity upstream of it.

---

## 2. Hypothesis Library (PR13-15) — real, but not the attachment point for risk policy

**Status confirmed implemented, not aspirational:**

- PR13 = `shared/hypothesis_library.py` — hypothesis-type CRUD, DB-backed
  (`hypothesis_types` table), tested (`tests/test_hypothesis_library.py`).
- PR14 = `shared/hypothesis_candidates.py` — parameter-sweep candidate generation,
  persisted, tested (`tests/test_hypothesis_candidates.py`).
- PR15 = `shared/backtest_engine.py` — see §4.
- PR16 = actual strategies (`bollinger_breakout_strategy.py`, `ema_crossover_strategy.py`).

`hypothesis_library.py`'s own docstring explicitly scopes strategy lifecycle/versioning/
promotion to a separately-scoped, not-yet-built "Strategy Incubator epic." There is no
`ResearchExperiment`, `StrategyVersion`, or `risk_policy` concept anywhere in the
codebase (confirmed by exhaustive grep — zero hits outside comments describing future
work).

The actual strategy-family registry predates the Hypothesis-Driven epic: the `theses`
table (`ingest/migrations/001_multi_thesis_architecture.sql:31-39`), with a `config
JSONB` column explicitly documented as the place for "strategy-specific params."

**Decision (see §6.2): risk-policy configuration attaches to `theses.config`, not to
`hypothesis_types`.** Registering a risk policy as a hypothesis type would conflate "what
directional edge is this" with "how much size does this get" — a distinction the
codebase already deliberately keeps separate (`risk_engine.py` docstring: "the strategy
proposes a side, entry, and stop; this module is the only thing that decides how many
shares that becomes").

---

## 3. Data contract

| Input | Status | Evidence / note |
|---|---|---|
| Daily OHLCV | Available | `price_history` table, Yahoo + Alpaca backfill |
| Split adjustment | Available | Alpaca backfill uses `"adjustment": "split"` (`ingest/backfill_alpaca.py:61-63`) — **continue using as-is, no change** |
| **Dividend adjustment** | **Absent** | No dividend-adjusted prices, no dividend-event table anywhere in the schema |
| VIX | Available, deep | `price_history` (`^VIX`), back to 1990, already feeds `market_regime.py::_classify_vix()` |
| Benchmark / relative strength | Available | `relative_strength_vs_spy()` (`signals.py:237-246`), sector RS in `sector_regime.py`/`relative_strength_risk.py` |
| Volume profile | Available (recent) | `shared/volume_profile.py`, built on hourly bars, documented as an approximation |
| Earnings dates | Available | `earnings_events` table, `shared/earnings.py` |
| Exchange calendar | Available, thin | Alpaca `/v2/calendar` check per ingest cycle; no richer early-close table |
| Hourly bars | Partial | Table exists, ~weeks deep, nothing currently reads it |
| Minute/intraday | Absent | "Reserved value only, nothing built" per repo docs |
| Implied volatility / options data | Absent | No options chain, IV, or IV-rank data anywhere in the schema |

### 3.1 Decision: dividend adjustment does not block the epic

Building a dividend/corporate-actions pipeline is out of scope for VR-1 through VR-3a.
Instead:

- Historical equity curves used in this epic's validation work **are not
  dividend-adjusted**, and every report generated under VR-3a/VR-3b must say so
  explicitly in its methodology section.
- The initial validation universe/period must be scoped so dividend effects are unlikely
  to materially drive the result — e.g. shorter holding-period strategies, or a universe
  weighted toward low/no-dividend names, decided at experiment-registration time
  (VR-0's registered-experiment deliverable), not left implicit.
- No broad total-return claims may be drawn from this epic's initial validation work —
  findings are scoped to price-return risk-adjusted comparisons, not total-return
  performance claims.
- Proper dividend/total-return handling is recorded as a **follow-up dependency**,
  required before any broader portfolio-performance validation beyond this epic's
  initial scope (e.g. before VR-6 paper activation is generalized past the pilot
  strategy/universe).

Split adjustment is unaffected — continue using the existing Alpaca `adjustment: split`
handling as-is.

---

## 4. Backtest/replay engine

**One shared, lookahead-safe engine exists — `shared/backtest_engine.py` (PR15) — but it
is single-symbol and single-unit-qty.** A second, older, portfolio-level engine exists
in parallel and predates the shared-engine convention.

`run_backtest(bars, strategy, *, execution_timing="next_bar_open", qty=1.0)`
(`backtest_engine.py:186-263`):

- Explicit `Signal` vs. `Fill` distinction — every signal is recorded (`actionable: bool`)
  even when it's a no-op; only actionable signals become fills.
- Lookahead-safe by construction: the engine's main loop only ever calls the strategy
  with `bars[:i+1]`; default `"next_bar_open"` timing fills at the next bar's open.
- No transaction costs/slippage (`gross_pnl == net_pnl`).
- No dividend/split adjustment beyond whatever `price_history` already contains.
- No multi-symbol or portfolio-level equity/cash tracking — `qty` is a fixed scalar per
  trade, not a dollar-sized position.

`ingest/research/backtests/backtest_portfolio_montecarlo.py` is the one existing
**portfolio-level** simulation (sector caps, circuit breaker, blackout gates) but is a
separate, hand-rolled ledger, not built on `backtest_engine.py`, with same-bar-close
fills (not lookahead-safe by the newer standard) and no slippage/commission modeling by
its own admission.

### 4.1 Decision: portfolio replay extension stays in-epic, but does not gate the core experiment

**VR-0b (portfolio-level replay extension) remains inside this epic**, extending
`backtest_engine.py`'s primitives (`Bar`/`Signal`/`Fill`/`Trade`) to a multi-symbol,
dollar-denominated, cost-aware ledger. But **VR-3 is split so VR-0b is not on the
critical path for the core volatility-sizing question**:

- **VR-3a (paired-opportunity / trade-level validation)** runs on the existing
  `shared/backtest_engine.py` largely as-is — but "as-is" is not dogma. `run_backtest()`
  currently takes a single fixed `qty=1.0` scalar for the whole run
  (`backtest_engine.py:186`). Comparing *size-weighted* outcomes across policies (the
  explicit point of VR-3a) means each simulated trade needs its own quantity — e.g. a
  per-signal `qty` resolved via a callback or a `Signal.qty` field, rather than one
  constant for the whole backtest. This is a small, additive change confined to a single
  parameter/field within the existing single-symbol, no-ledger, `Bar`/`Signal`/`Fill`
  model — it does not require a cash ledger, multi-symbol tracking, or any of VR-0b's
  scope, and should not be deferred into VR-0b by reflex just because both touch
  `backtest_engine.py`. Confirm the exact shape (callback vs. field) during VR-3a design;
  either is radically smaller than VR-0b.
- **VR-3b (constrained-account replay)** requires VR-0b. It is the only place cash
  competition, simultaneous positions, sector constraints, portfolio open risk, and costs
  are tested together at the account level.

This means the estimator and sizing-policy work (VR-1, VR-2) and the first empirical
read on whether volatility-aware sizing helps (VR-3a) can complete and produce findings
**without waiting on a portfolio-replay-engine build-out**. See §5 for the corrected
dependency diagram.

---

## 5. Corrected dependency diagram

```
VR-0 (this document)
├─ VR-0b: portfolio replay extension (extends backtest_engine.py:
│  multi-symbol ledger, dollar qty, costs)
│   └─ gates VR-3b only
│
└─ VR-1: forecast contract + realized-vol/EWMA
    (independent of VR-0b — proceeds in parallel)
    └─ VR-2: volatility candidate inside risk_engine.evaluate_proposal()
        └─ VR-3a: paired-opportunity validation (backtest_engine.py as-is)
            ├─ VR-4: optional GARCH estimator
            ├─ VR-5: diagnostics
            └─ VR-6: shadow/paper rollout

VR-0b + VR-2
    └─ VR-3b: constrained-account replay
```

VR-1 has no dependency on VR-0b and should start as soon as VR-0 is accepted. VR-0b can
proceed on its own timeline; it only blocks VR-3b, not the epic's first useful milestone
(VR-1 → VR-2 → VR-3a).

---

## 6. Binding decisions

### 6.1 Sizing integration: one authoritative decision point, volatility as another named candidate

`risk_engine.evaluate_proposal()` remains the single authoritative quantity decision
point. No parallel sizing engine is created, and volatility sizing does not mutate order
quantity upstream of the risk engine. Volatility sizing enters the existing
`min()`-of-constraints model as another named candidate, `volatility_budget_qty`, with
its inputs and output captured in the existing `constraint_detail` audit trail
(`risk_decisions.constraint_detail`, JSONB):

```
final_qty = min(
    buying_power_qty,
    position_allocation_qty,
    stop_risk_qty,
    portfolio_open_risk_qty,
    sector_qty,
    volatility_budget_qty,
)
```

This is a direct extension of the pattern already in place (`risk_engine.py:191-200`),
not a new mechanism. It also means VR-5's diagnostics can answer, from data the system
already persists: how often the volatility candidate actually binds, by how much it
changes position size relative to the other candidates, under which volatility regimes
it binds, and whether trades where it binds have better risk-adjusted outcomes.

### 6.2 Risk-policy versioning: `theses.config` JSONB, no new lifecycle table

Risk-policy configuration for the first cut lives in `theses.config` (JSONB), the
existing "strategy-specific params" location (`ingest/migrations/001_multi_thesis_architecture.sql:37`).
No dedicated risk-policy lifecycle/version table is created at this stage.

This preserves the separation the architecture already establishes between:

- hypothesis/edge definition (`hypothesis_types`, PR13),
- strategy/thesis configuration (`theses.config`),
- authoritative risk sizing (`risk_engine.py`),
- persisted risk-decision audit data (`risk_decisions`).

If independent promotion, history, or versioning of risk policies across many theses is
later needed, that can justify a dedicated model — there is no evidence today that
complexity is necessary, and introducing it now would be building ahead of a
demonstrated need.

### 6.3 VR-1 forecast contract — concrete field list and units

The forecast contract is a frozen dataclass (`VolatilityForecast`, following the
`FeatureSpec`/`HypothesisTypeSpec` precedent's `to_json()`/`from_json()` style, not
`dataclasses.asdict()`) persisted to a new `volatility_forecast_history` table and
registered as a `feature_registry.py` provider. Fields, with the units decision each one
locks in:

| Field | Type | Notes |
|---|---|---|
| `symbol` | str | Instrument, matching `price_history.symbol` conventions |
| `timeframe` | str | Bar timeframe the estimate is computed over, e.g. `"1d"`. Kept distinct from `horizon` — an estimator can be fit on daily bars but forecast a 5-day-ahead horizon |
| `as_of` | timestamptz | Decision/observation timestamp — the point in time the forecast is valid *as of*. This is the field VR-1's causality tests anchor to: truncating input data at `as_of` must reproduce the same forecast |
| `available_at` | timestamptz | When the forecast became usable by a downstream consumer (may lag `as_of` by ingestion/compute latency); this is the field the risk engine checks against the proposal's decision timestamp, not `as_of` |
| `horizon` | str/enum | Forecast target window, e.g. `"1d"`, `"5d"`. For a realized-vol/EWMA estimator this is also the window the estimate is meant to be predictive over; for a future IV-derived estimator it maps to the option's time-to-expiry bucket. Nullable/optional interpretation only where an estimator genuinely has no forward horizon concept — none of VR-1's initial estimators fall into that case |
| `estimator` | str | Estimator identity, e.g. `"realized_vol"`, `"ewma"`, `"garch_1_1"`, and later `"implied_vol"` |
| `calculation_version` | int | Versions the calculation logic, same role as `market_structure.py`'s `calculation_version` / `feature_store.py`'s `FEATURE_VERSION` — bumped whenever the estimator's formula, window, or normalization changes |
| `input_cutoff` | timestamptz | Last input observation actually used — distinct from `as_of` so a stale-data situation (last bar older than `as_of`) is detectable rather than silently assumed fresh |
| `daily_vol` | float | **Canonical unit #1** — standard deviation of daily log returns, decimal (not percent), e.g. `0.018` for 1.8%/day. This is the estimator's native output before annualization |
| `annualized_vol` | float | **Canonical unit #2** — `daily_vol * sqrt(sessions_per_year)`, decimal, e.g. `0.29` for 29%/year. `sessions_per_year` defaults to 252 for equities per VR-1's scope; a future crypto adapter documents and uses its own calendar constant, never silently reusing 252 |
| `horizon_vol` | float | **Canonical unit #3** — `daily_vol * sqrt(horizon_sessions)`, the standard deviation of the *cumulative* return over `horizon`, decimal. This is what should be compared against an actual holding-period return, never `annualized_vol` |
| `expected_move_dollars` | float, nullable | `price_at_as_of * horizon_vol`, in dollars, for the one-standard-deviation move over `horizon`. Explicitly documented (per the roadmap's own research contract) as an uncertainty estimate, not VaR, not a maximum loss |
| `percentile` | float, nullable | Trailing percentile rank of `daily_vol` (or `annualized_vol` — same rank either way since the transform is monotonic) among the estimator's own prior valid observations only, per `_percentile_rank()`'s existing convention. Null when there isn't enough trailing history yet — not defaulted to a mid-range value |
| `regime` | str/enum, nullable | Coarse bucket derived from `percentile` (e.g. `compression`/`normal`/`expansion`, reusing `market_structure.py::_volatility_context()`'s existing thresholds/labels for consistency with what's already on dashboards) |
| `observation_count` | int | Number of input observations the estimate is based on — lets a consumer distinguish a fresh, thin-history estimate from a mature one even when `status` is `ok` |
| `status` | str/enum | `ok`, `insufficient_history`, `stale`, `zero_or_nonfinite_input`, `estimator_failed`. This is the field VR-2's fallback logic (§6.4) switches on — never inferred from whether other fields happen to be null |
| `fit_metadata` | jsonb, nullable | Estimator-specific fit diagnostics (e.g. GARCH convergence/persistence, VR-4) — kept separate from the core numeric fields so adding a new estimator's diagnostics never requires a schema change to the shared columns |

**Unit rule, stated once so it cannot drift per-estimator**: every estimator normalizes
its raw output into `daily_vol` first, and `annualized_vol`/`horizon_vol` are always
*derived* from `daily_vol` by the shared contract code (never computed ad hoc inside an
estimator). An estimator is only responsible for producing a correctly-scaled
`daily_vol` plus `sessions_per_year`/`horizon_sessions`; it never emits an annualized or
horizon figure directly. This is the mechanism that prevents the
annualized-vs-horizon-vs-dollar-move confusion the roadmap calls out — there is exactly
one place the conversion happens, not one per estimator. `ATR` remains explicitly out of
this unit system (it's a price-range measure, not a return standard deviation) and is
never substituted for `daily_vol`.

**Implied-volatility coexistence, made concrete**: a future `estimator="implied_vol"` row
populates the same `daily_vol`/`annualized_vol`/`horizon_vol`/`expected_move_dollars`
columns (options pricing gives annualized IV directly — convert down to `daily_vol` at
ingestion, not up from it elsewhere) and leaves the estimator-specific machinery in
`fit_metadata` (strike, expiry, underlying quote used). No schema change is needed to add
it later — this is what makes the coexistence promise concrete rather than aspirational.
Options data remains out of scope for VR-1/VR-2; this section only fixes the contract so
adding it later doesn't require a migration.

### 6.4 Missing-data fallback (decided before VR-2, dark-by-default)

When `evaluate_proposal()` looks up a forecast for a proposal and finds no row, or finds
one with `status != "ok"`, or finds `available_at` later than the proposal's decision
timestamp (not yet usable): **the volatility overlay is skipped and sizing falls back to
current behavior unchanged** — `volatility_budget_qty` is simply omitted from the `min()`
candidate set (equivalent to `+inf`), not substituted with a default value, not derived
from ATR as a stand-in, and the proposal is not rejected on this basis alone.

This is the direct consequence of the dark-by-default rollout convention already used
throughout this codebase (`structure_scoring_enabled`, `relative_strength_risk_mode`,
etc., per §7): an experimental overlay that cannot produce a valid signal must be
invisible, not lossy in either direction. Rejecting new exposure outright on missing
data, or silently falling back to ATR, are both explicitly **not** the default —an
optional, separately-labeled fallback-to-EWMA setting may exist per the original
roadmap's language, but it defaults off and must never be reported as GARCH when GARCH
was the requested estimator. The `risk_decisions.constraint_detail` audit row records
which case occurred (`volatility_forecast_status`, `volatility_overlay_applied: bool`)
so this is auditable, not silent.

### 6.5 `volatility_budget_qty` formula — inverse-volatility scaling, not portfolio-vol targeting

Per the roadmap's original mechanism (carried forward, now stated as the binding
formula rather than an example):

```
multiplier = min(max_multiplier, reference_vol / max(forecast_vol, vol_floor))
volatility_budget_qty = floor((base_notional * multiplier) / price)
```

This is **simple inverse-volatility scaling against a fixed reference volatility**, not
a target-portfolio-vol-contribution model and not a second independent dollar-risk
budget. Concretely:

- `reference_vol` and `forecast_vol` are both `annualized_vol` (§6.3) — same horizon,
  same units, by construction.
- `base_notional` is the *pre-volatility* notional the strategy would otherwise have
  proposed — i.e. `calc_buy_qty()`'s existing sized dollar amount, not portfolio equity
  times a target weight. This is deliberately **not** portfolio-volatility targeting
  (per the roadmap's own explicit non-negotiable: "asset volatility scaling is not
  portfolio volatility targeting").
- Initial paper-eligible settings: `max_multiplier = 1.0` (reductions only, never
  increases exposure or adds leverage), no positive minimum multiplier.
- `vol_floor` is a numerical safeguard against division blow-up on a valid-but-tiny
  forecast, not a mechanism for rescuing an invalid one — an invalid forecast is handled
  entirely by §6.4's status check, upstream of this formula ever running.

**Interaction with `stop_risk_qty`, made explicit**: `stop_risk_qty` already encodes a
dollar-risk budget via stop distance (`risk_budget_dollars / risk_per_share`,
`risk_engine.py:143-150`). `volatility_budget_qty` encodes a *relative* exposure
reduction against a reference volatility, not a second, independent dollar-risk budget —
the two are different mechanisms answering different questions ("how many shares until
a stop-out costs at most $X" vs. "how much should this position shrink because this
asset is currently more volatile than usual"), and both apply via the existing `min()`,
so whichever is currently tighter binds. This is deliberately *not* combined into one
formula (e.g. folding volatility into the stop-distance calculation) — VR-3a's paired
analysis is specifically designed to measure whether the combination double-reduces
exposure in practice (the roadmap's own concern in VR-2's scope text), and that
measurement requires the two to remain distinct, separately-labeled candidates in
`constraint_detail`, not fused before evaluation.

If VR-3a/VR-3b findings later show the two systematically compound in a way that's
worse than either alone, revisiting the formula (e.g. making the volatility multiplier a
function of the stop-implied risk rather than of `base_notional`) is an explicit,
separately-tested follow-up — not something to bake in speculatively now.

### 6.6 VR-2 acceptance criterion: bit-for-bit parity when disabled

Added to VR-2's acceptance criteria: **when the volatility overlay is disabled (feature
flag off) or falls back per §6.4, `evaluate_proposal()`'s `approved_quantity` and every
other returned field must be bit-for-bit identical to current behavior for the same
inputs** — same binding constraint name, same `constraint_detail` shape apart from the
added (but inert) volatility fields, same numeric outputs to the same floating-point
representation. This is the regression guard on the single most central function in the
trading path; a test asserting exact equality against `evaluate_proposal()`'s current
output for a fixed table of inputs, run both pre- and post-change, is required before
VR-2 merges.

---

## 7. Reusable primitives inventory (for VR-1 implementation)

- `shared/market_structure.py::_atr_series()` (`market_structure.py:144`) — full ATR
  series (not just latest value, unlike `signals.py::compute_atr()`). Needs exporting
  (currently private) for VR-1 to import directly.
- `shared/market_structure.py::_percentile_rank()` (`market_structure.py:164`) — trailing
  percentile computation, same needs-exporting note.
- `shared/market_structure.py::_volatility_context()` (`market_structure.py:314-328`) —
  existing compression/normal/expansion regime bucketing over a configurable lookback;
  useful as a cross-check or fallback classification, not a replacement for the VR-1
  forecast record itself.
- Persistence template: frozen dataclass spec → versioned DB table → `feature_registry.py`
  provider registration → config-flag-gated, dark-by-default rollout via `signal_params`
  — the pattern used identically by `feature_store.py`, `hypothesis_library.py`,
  `market_structure.py`, and `market_regime_history.py`. VR-1's `volatility_forecast_history`
  table and estimator config follow this template exactly.

---

## Deliverable checklist (per VR-0's stated acceptance criteria)

- [x] Every proposed integration point references actual code — §1, §2, §4, §7
- [x] Data contract covers price basis, split/dividend treatment, missing data, universe
      limitations — §3
- [x] Shared-engine prerequisite made explicit, scoped as its own dependency (VR-0b), not
      hidden inside an estimator PR — §4.1
- [x] Revised PR dependency map, with VR-1 independent of VR-0b and VR-3 split into
      VR-3a/VR-3b — §5
- [x] Risk-policy versioning attachment point decided (`theses.config`, not
      `hypothesis_types`) — §2, §6.2
- [x] Sizing integration point decided (`risk_engine.evaluate_proposal()` `min()`
      candidate, no parallel engine) — §6.1
- [x] Dividend-adjustment gap addressed by scope/labeling, not left ambiguous — §3.1
- [x] Implied-volatility path kept open without becoming a VR-1/VR-2 dependency — §6.3
- [x] Forecast contract concretely fielded (symbol/timeframe/as_of/horizon/estimator/
      forecast_vol/percentile/regime/calculation_version/status equivalents present) — §6.3
- [x] Canonical units fixed once, derived centrally, never computed ad hoc per estimator — §6.3
- [x] Missing-data fallback decided before VR-2: existing behavior unchanged unless a
      valid forecast is present — §6.4
- [x] `volatility_budget_qty` formula decided (inverse-vol scaling vs. `base_notional`,
      not portfolio-vol targeting or a second dollar-risk budget) and its interaction
      with `stop_risk_qty` made explicit — §6.5
- [x] VR-2 bit-for-bit parity-when-disabled acceptance criterion added — §6.6
- [x] VR-3a's "as-is" reliance on `backtest_engine.py` scoped honestly — a small per-signal
      qty extension is anticipated, not treated as dogma — §4.1
- [x] This document changes documentation only — no code changes made

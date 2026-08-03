# Research and backtesting platform improvements

Design note, following the precedent of `docs/thesis-horizons-and-intraday-data.md`
and the other `docs/*` design notes from this line of work: committed to
the repo rather than left in chat history. Items 1–6 extend two PRs
from the Platform Improvements roadmap that were designed in
conversation but not yet written to a repo doc — **Platform Improvements
PR B (Expectancy Reporting)** and **PR D (Exit-Policy Research)** — plus
one net-new research backlog entry (#6). Items 7–13, added in a second
pass, cover re-entry/setup-episode analysis, a target-allocation
strategy interface, strategy timing contracts, ensemble attribution,
drawdown-duration reporting, research lineage/multiple-testing records,
and backtest data-lineage metadata. None of this is implemented. None
of it changes current live behavior; everything here is either a
backtest/research-script concern, a strategy-interface design, or an
explicitly offline/shadow analysis, same as the rest of the Platform
Improvements sequence.

## Grounding: what actually exists today

Checked directly (not assumed) before writing this: `ingest/research/backtests/`
has four scripts (`backtest_congressional_shadow.py`,
`backtest_portfolio_montecarlo.py`, `backtest_rule_significance.py`,
`backtest_score_calibration.py`) plus a shared `db_utils.py`. **None of
them model slippage or commission.** `backtest_portfolio_montecarlo.py`
says so explicitly in its own docstring: "Fills are at same-day close, no
slippage/commission modeling — see below — and slippage/commission
modeling is still future work." `backtest_rule_significance.py` is
equally explicit that it "does NOT tell you the strategy is profitable
after costs." This means items 1–2 below are net-new infrastructure, not
a refactor of something already duplicated four times — there is nothing
to centralize *from* yet, only the four scripts' independent (currently
absent) fill assumptions to eventually replace with one shared
implementation.

The live app's own cost model (`trades.cost`, flat commission per fill,
`signal_params.trade_cost_flat`) is the one piece of cost-modeling that
does exist in production — any shared fill model should be able to
reproduce that flat-commission behavior as its simplest configuration,
so paper-trading reporting and backtest reporting can eventually share
one cost assumption rather than two independent ones.

## 1. Transaction-cost sensitivity analysis

Extends **Platform Improvements PR B** (Expectancy Reporting), whose
metric set already distinguishes gross P&L from net P&L per round trip
(see the Symbol Performance Summary PR's `pnl_before_costs`/`net_pnl`
split, and PR A's planned gross/net separation) — this generalizes that
same discipline to every backtest and strategy review, not just the live
paper-trading reports.

Requirements:
- Every backtest result and every `strategy_review_proposals` entry
  reports **both** gross and net expectancy (in R, once PR A's R-multiple
  foundation exists; in raw $/% until then) — never net-only, since a
  strategy that only looks profitable net of an optimistic cost
  assumption is exactly the failure mode this exists to catch.
- Cost assumptions (commission, spread, slippage) become **configurable
  parameters** to a backtest run, not a hardcoded constant buried in one
  script — the same "boring, explicit, no magic constants" discipline
  `shared/signals.py`'s `DEFAULTS`/`signal_params` split already follows
  for live trading parameters.
- A single backtest run should be able to report results across **multiple
  friction levels** in one pass (e.g., zero-cost, "optimistic," "realistic,"
  "pessimistic" commission/spread/slippage tuples) — a sensitivity sweep,
  not a single point estimate. This is what makes "break-even slippage"
  (below) computable at all: it's the friction level at which net
  expectancy crosses zero, found by sweeping, not guessed.
- Report **break-even slippage** (the friction level at which net
  expectancy hits zero) and **percentage of gross edge consumed by
  costs** (`(gross_expectancy - net_expectancy) / gross_expectancy`) as
  first-class output fields, not something a human has to derive by hand
  from a table of numbers.

## 2. Shared realistic fill model

New `shared/fill_model.py` (pure functions, no DB access, matching the
established `shared/signal_components.py`/`shared/r_multiple.py`-style
module convention), used by **every** research backtest script and by
Platform Improvements PR D's counterfactual exit-policy simulator — one
implementation, not four independent assumptions living inside four
separate scripts the way cost modeling currently doesn't exist at all in
any of them.

Core rule, already anticipated in PR D's original design ("gap-through-the-stop
fill realism"): **a simulated fill may be worse than the requested
trigger price, but must never be better.** For a long stop-loss:
`fill_price = min(day_open, stop_level)` when the day gaps through the
stop — the fill can only be at-or-worse than the stop level, never
at-or-better, even if the day's later price action would have made a
same-price fill look achievable in hindsight. The symmetric rule applies
to stop entries (buy-stop fills at `max(day_open, trigger_level)`) and to
short-side stops/entries. Centralizing this in one module is what makes
"all backtests use the same implementation" true by construction rather
than by convention four different authors have to remember.

## 3. Intraday ambiguity handling

Also extends PR D, and reuses the `excursion_resolution` concept already
specified for Platform Improvements PR A's pathwise MAE/MFE walk
(`'hourly' | 'daily_approximation'`) — the same fundamental problem
(daily OHLC bars can't tell you the *order* events happened in during the
day) shows up in both places and should use one shared vocabulary, not
two different ad hoc flags.

When a single day's high/low bar crosses **both** a proposed entry and
its stop (or both legs of a paired-order strategy), the actual intraday
sequence — which happened first — is genuinely unknown from daily bars
alone. **Never infer the favorable sequence.** In order of preference:
1. Use finer-grained data (`price_history_hourly`, matching PR A's own
   resolution-fallback approach) if available for that day, resolving the
   ambiguity directly.
2. If only daily bars are available, apply a **conservative, fixed
   ordering rule** (e.g., "the adverse side of the bar is assumed to
   happen first" — worst-case, not best-case, consistent with the
   never-better-than-realistic principle from item 2).
3. Where even a conservative ordering can't produce a meaningful result
   (both interpretations are needed to know whether a trade happened at
   all, not just its P&L), mark the day's outcome as an **explicit
   ambiguous result** rather than silently picking one, or exclude that
   observation from the backtest with the exclusion counted and reported
   (never silently dropped from the sample size).
- Whichever rule fires must be **recorded per observation** (a field
  alongside the trade/backtest row, not just a global assumption noted in
  a docstring) — so a reviewer can later ask "how many of these results
  depended on the conservative-ordering assumption" and get a real
  answer.

## 4. Robustness reporting

Extends **Platform Improvements PR B**'s segmentation (already scoped
there: by thesis, model version, feature version, market regime, symbol
regime, score bucket, holding-period bucket, sector, approval status,
exit reason, calendar period) with an explicit **concentration**
dimension PR B's original design didn't call out on its own:

- Report performance broken out **by symbol, market regime, year, and
  entry-time bucket** (the last one ties into item 5 for intraday
  theses), *and* under each of the cost-sensitivity levels from item 1 —
  a strategy's expectancy at zero cost and at realistic cost can tell two
  very different stories, and robustness reporting should show both side
  by side rather than pick one.
- Report **concentration in the best symbol and the best period** —
  e.g., "% of total gross profit contributed by the single best-performing
  symbol" and "% of total gross profit contributed by the single
  best-performing calendar period" (reusing the same
  winners-only-sum-as-denominator convention already established for the
  Symbol Performance Summary's portfolio-contribution metrics).
- **Do not promote a strategy** (move a thesis from `backtesting_only`
  toward `active`, or move any signal component's weight off zero) **whose
  profitability is dominated by one asset or one period** — this becomes
  an explicit, checkable gate alongside PR B's existing minimum-sample-size
  gating (`insufficient` / `preliminary` / `established`), not just a
  narrative caveat in a review write-up.

## 5. Intraday execution diagnostics

For future intraday theses specifically — this repo already has the
taxonomy for this in `docs/thesis-horizons-and-intraday-data.md`
(`theses.horizon = 'short_term'`, `price_history_hourly` as its data
layer, nothing built on top of it yet). This item is what "built on top
of it" needs to eventually report:

- Record, per simulated (or eventually live) intraday fill: **spread,
  slippage, fill delay, fill rate**, and break these out **by
  time-of-day bucket** (e.g., open, mid-morning, midday, power hour,
  close) — intraday liquidity and spread behavior varies enormously by
  time of day, and an aggregate average would hide exactly the periods
  where a strategy's assumed execution quality is least realistic.
- Report **expectancy by time-of-day bucket** using the same
  gross/net-and-cost-sensitivity framing as item 1, not a separate,
  unrelated metric.
- This item has no schema or code proposed yet — it's a placeholder for
  when a `short_term`-horizon thesis is actually being built, consistent
  with `docs/thesis-horizons-and-intraday-data.md`'s own framing that the
  data layer existing doesn't mean the strategy work exists yet.

## 6. Opening-range breakout research backlog

A **net-new research-only thesis candidate** — NR4/NR7 (narrow-range 4/7
day) and inside-day contraction patterns followed by opening-range
expansion. Explicitly backlog, not even at the `congress_shreve_hern`
precedent's stage yet (that thesis at least has a `theses` row with
`status='backtesting_only'` — this one shouldn't get a `theses` row at
all until real backtesting work begins; registering it prematurely would
overstate how far along it is).

Requirements before any backtesting work starts:
- **Minute-level data**, not `price_history_hourly` — opening-range and
  narrow-range patterns are defined at finer granularity than this
  codebase currently ingests anywhere. No minute-bar ingestion exists
  today; this is a real, currently-unmet data dependency, not just a
  research-script concern.
- **Point-in-time calculations** — same discipline as everything else in
  this roadmap: a backtest of "was today's range narrower than the prior
  N days" must only use data that was actually available at each
  decision point, no different from the point-in-time rules already
  enforced for `fundamental_facts` and signal generation.
- **Complete separation from the current mean-reversion execution path**
  — no shared code path with `shared/signals.py::compute_signals()`
  beyond genuinely common utilities (e.g., a future shared fill model
  from item 2). This is a `day_trading`-horizon-shaped thesis in
  `docs/thesis-horizons-and-intraday-data.md`'s own taxonomy — that doc
  is explicit that `day_trading` is "reserved value only... nothing in
  the codebase acts on it," and this backlog item doesn't change that
  yet.
- **No activation without out-of-sample testing and live execution-cost
  validation** — the same evidence-based activation gate every other
  component/thesis in this roadmap is held to (`docs/signal-component-architecture.md`'s
  "no component but `technical` gets real weight... until its own
  validated backtest," `congress_shreve_hern`'s `backtesting_only`
  status). Opening-range breakout strategies are also unusually
  execution-cost-sensitive (tight stops, fast entries) — item 1's
  break-even-slippage reporting is not optional for this one; it's the
  central question of whether this strategy family is viable at all
  after realistic costs, not a nice-to-have addendum.

## 7. Setup episodes and re-entry analysis

For strategies that permit multiple entries against one underlying setup
(a stopped-out breakout retried after a cooldown, for example), group
those attempts under a shared `setup_episode_id` rather than letting
each attempt read as an unrelated, independent trade — the existing
`buy_cooldown_days` mechanism (`signal_params`, enforced in
`shared/signals.py`) already suppresses same-symbol re-proposals within
a window, but nothing currently links a suppressed-then-later-approved
re-entry back to the setup that originally triggered it, so there is no
way today to ask "did the second attempt at this setup do better or
worse than the first."

Record per attempt: attempt number, the episode's initial setup time,
entry and exit reason, cooldown duration actually observed, cumulative
gross and net P&L, cumulative realized R (once PR A's R-multiple fields
exist), cumulative costs and slippage (once item 2's fill model exists),
maximum attempts allowed, and whether the final episode followed all
rules.

Strategy Review (`strategy_review_proposals`) should be able to compare:
first-entry-only performance, performance including re-entries, the
marginal expectancy of attempt 2, 3, and later specifically (not just
the episode average — a strategy where only attempt 1 ever works is a
materially different finding than one where every attempt contributes
equally), total friction from repeated trading, episode-level drawdown,
and whether re-entry improves net expectancy after costs at all, or
just adds friction for the same eventual outcome.

**Re-entry must be defined deterministically and must consume the same
live-risk budget as any other new entry.** It is not a continuation that
bypasses position, daily-loss, or trade-count limits — `max_positions`
(the same position-count cap the dashboard's advisor card already
enforces, e.g. "At position limit (18/15)") and any daily-loss or
trade-count gate apply to a re-entry exactly as they would to a
brand-new symbol, with no special-cased exemption anywhere in the
approval path.

## 8. Target-allocation strategy interface

Allow future strategies to emit desired portfolio weights or share
quantities directly, rather than being limited to the discrete buy/sell
signals `shared/signals.py::compute_signals()` produces today. A
target-allocation strategy needs to report, at minimum: current
exposure, requested exposure, final risk-capped exposure (after
`max_positions`, the circuit breaker, and any other live risk gate has
had a chance to constrain it), and the resulting required adjustment
(the actual order(s) needed to move from current to risk-capped
exposure). This is a new strategy *shape*, not a replacement for the
existing discrete-signal path — `theses.horizon`'s existing taxonomy
(`long_term` / `short_term` / `day_trading`, see
`docs/thesis-horizons-and-intraday-data.md`) already anticipates more
than one kind of strategy coexisting; a target-allocation interface is
additive to that, not a rewrite of it.

## 9. Strategy timing contracts

Store, per strategy (or per proposal): decision window (how long a
signal is valid to be acted on), execution window (how long an approved
proposal remains fillable), and signal expiry. Block stale or
mistimed proposals — a signal generated before yesterday's close being
approved and filled today, well outside whatever window the strategy
that generated it actually intended, is exactly the kind of
timing-contract violation this is meant to catch. No existing table
tracks this today; `trade_proposals` (undocumented in `schema.sql`,
per the pre-existing gap noted in this repo's operational notes) has
`proposed_at`/`decided_at` but nothing that expresses the strategy's own
intended validity window as distinct from when a human happened to act.

## 10. Ensemble attribution

Once more than one signal-integration component carries real weight
(see `docs/signal-component-architecture.md` — today only `technical`
does; `component_weights` is stored per outcome specifically so this is
answerable once that changes), preserve each strategy or component's
individual contribution to the final target, and separately identify
any risk overlay (the circuit breaker, `max_positions`, a future
launch-risk profile) that modified it after the fact. The goal is being
able to answer "how much of this decision came from the signal versus
how much came from a risk constraint overriding it" after the fact, not
just at decision time.

## 11. Drawdown-duration reporting

Add longest drawdown, time to recovery, and percentage of time
underwater to Strategy Review's metric set. The circuit breaker
(`shared/circuit_breaker.py`) already tracks `high_water_mark` and
`drawdown_pct` continuously in `portfolio_snapshots`, so the raw data to
compute these already exists — this is a reporting gap, not a new data
requirement, unlike most of the other items on this list.

## 12. Research lineage and multiple-testing records

Record rejected experiments and parameter searches, not only winning
backtests. `backtest_results` exists today (`experiment_id`, `run_at`,
`git_commit`, `summary`, `results` JSONB) and every one of the four
current backtest scripts writes to it at the end of a run — but nothing
distinguishes a deliberately-rejected result from an adopted one, and
nothing records the parameter sweep that produced a winning
configuration versus the runs that didn't make the cut. Without that,
a strategy's apparent edge can't be checked for multiple-testing bias
(the more parameter combinations tried, the more likely one looks good
by chance alone) after the fact. Require out-of-sample validation
*after* strategy selection, not folded into the same search that picked
the winning parameters — the same discipline
`docs/signal-component-architecture.md` already holds live component
weights to ("no component but `technical` gets real weight... until its
own validated backtest").

## 13. Backtest data-lineage metadata

Label market data as observed, adjusted, reconstructed, or synthetic.
`price_history.adjclose` (dividend/split-adjusted close, added
alongside the existing raw `close`) is the one piece of this that
already exists and is already labeled by construction — extending the
same discipline to every other data source a backtest might use.
Synthetic leveraged-product histories (a simulated 2x/3x leveraged ETF
history reconstructed from an underlying index, for strategies that
want to backtest further back than the real leveraged product's actual
inception) must document leverage reset frequency, fees, financing
cost, and tracking-error assumptions explicitly — a synthetic history
that silently omits daily-reset compounding effects can make a
leveraged strategy look substantially better in backtest than any real
version of that product would have performed.

## Where this sits in the overall roadmap

Items 1–4 are refinements of **Platform Improvements PR B and PR D**,
which are themselves still design-only (see the conversation history
referenced by `docs/session-handoff-2026-07-31.md` — that handoff notes
PR A/B/C/D exist as designs but PR B/C/D were never written to a repo doc
in the same depth as PR A's schema was). Item 5 is a placeholder tied to
the already-documented `short_term` horizon. Item 6 is a new backlog
entry, deliberately kept at arm's length from any existing execution
path. Items 7 and 10 extend PR B's expectancy/segmentation framing to
re-entries and multi-component ensembles respectively; item 11 is a
reporting-only addition against data the circuit breaker already
collects; items 8 and 9 are new strategy-interface concerns (allocation
targets and timing contracts) that sit alongside, not on top of, the
existing discrete-signal path; items 12 and 13 are research-process
and data-hygiene requirements that apply to every item above rather
than being scoped to any one of them.

None of items 1–11 should be implemented before **Platform
Improvements PR A** (position lifecycles / R-multiple foundation) lands
— items 1, 4, 7, and 10's "gross vs. net," "expectancy," and
R-multiple framing all assume PR A's fields exist, same dependency
already noted for PR B
and PR D in the handoff.

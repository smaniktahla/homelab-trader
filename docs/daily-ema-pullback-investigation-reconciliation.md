# Daily EMA Pullback + Momentum Swing Trading — Investigation & Reconciliation

Investigation note, committed to the repo rather than decided unilaterally in
a chat session — same reasoning as every other reconciliation doc in this
directory. **No implementation in this PR — investigation and scoping
only**, per this epic's own spec, which explicitly requires this pass
("Desired First Step") before any code.

Working name: `daily_ema_pullback_momentum`. A second price-action/trend-
following strategy family, distinct from the intraday Market Structure + FVG
work (Price Structure epic). Daily-timeframe swing trading built around 8
EMA / 20 EMA pullback setups, an H1-H10 hypothesis progression, structured
as a Context → Location → Reaction → Execution framework the spec itself
says should be reusable across strategy families, not new architecture per
family.

## 1. What already exists (per the spec's own checklist)

**EMA**: `shared/market_structure.py::ema(closes, period)` is a pure,
period-agnostic EMA (SMA-seeded, standard smoothing) already used
internally at 20/50/200. `shared/ema_crossover_strategy.py` reuses it as-is
for a crossover strategy. **Directly reusable** for the EMA computation
itself — an 8/20 pullback variant calls `ema(closes, 8)`/`ema(closes, 20)`
today, no new primitive needed. Pullback entry/exit logic (distinct from
crossover logic) needs new code.

**Trend/regime**: `market_structure.py::detect_swings()`,
`_trend_direction()` (HH/HL vs LH/LL), `_trend_strength()` (weak/moderate/
strong off EMA stack/ADX/slope), `classify_timeframe_structure()`, and
`combine_timeframe_structures()` (top-down Monthly/Weekly/Daily) already
exist. `shared/sector_regime.py`/`shared/security_regime.py` classify
sector-/security-level regime vs. benchmarks, persisted daily. **Directly
reusable** as the trend-context gate the spec's own "baseline → trend
filter → slope filter → structure filter" progression calls for.

**Breakout**: `shared/structural_events.py::detect_zone_events()` already
detects breakout/breakdown/failed-breakout/sweep-reclaim against persisted
zones, confirmation-time safe. **Reusable.** **Momentum scoring is a gap**
— no generic momentum primitive exists anywhere in the repo; the two
`hypothesis_types` catalog rows that mention "momentum"
(`structural_breakout_momentum`, `fvg_reaction_momentum`, Price Structure
epic) are explicitly unimplemented per `structural_support_bounce_strategy.py`'s
own docstring. This needs new code, and needs a precise, non-arbitrary
definition — the spec's own Anti-Goals list warns against assuming any one
threshold/window is correct without testing it.

**Relative strength**: `shared/signals.py::relative_strength_vs_spy()`
exists and is directly reusable for RS-vs-SPY. No RS-vs-sector function
exists yet, but `shared/security_regime.py::_relative_strength_inputs()`
is a generic two-series pure helper already used for both sector and market
comparisons internally — extending RS-vs-sector is a thin adaptation of an
existing pattern, not new architecture.

**Volume**: `shared/volume_metrics.py::relative_volume()`/`volume_zscore()`/
`volume_percentile()` are pure, stateless, list-in/value-out — directly
reusable for the spec's "RVOL bucketing" with no adaptation needed beyond a
thin bucket-boundary wrapper.

**Earnings**: `earnings_events` table + `shared/earnings.py::
sync_earnings_calendar()`/`earnings_blackout_reason()` exist and are
reusable as a blackout/gap-context gate. **Caveat**: population is a no-op
if `FINNHUB_API_KEY` isn't configured — reliability for real backtests
depends on confirming that key is actually set in the deployment being
tested against, not assumed.

**VIX/market volatility regime**: `ingest/backfill_vix.py` backfills full
daily `^VIX` history into `price_history` (same shape as any equity
symbol). `ingest/market_regime.py::_classify_vix()`/`classify_overall()`
already buckets VIX into calm/elevated/fear and combines it with SPY/QQQ
trend into a persisted daily market regime
(`shared/market_regime_history.py`). This directly answers the spec's own
open question ("determine whether VIX data already exists") — **it does,
and a market-regime classification already consumes it.** Separately,
`shared/volatility_forecast.py` is a per-symbol (not market-wide) vol
estimator — a different, also-reusable concept, not a duplicate.

**Data granularity / corporate-action adjustment**: `price_history.adjclose`
(dividend/split-adjusted close from Yahoo) was added specifically because
raw OHLC is **not** adjusted, and is backfilled opportunistically
(`ON CONFLICT ... WHERE adjclose IS NULL`), not force-refreshed. This is a
real data-integrity risk, not just a caveat: `market_structure.py`'s swing
detection and `ema()` both currently consume raw, unadjusted OHLC. A stock
split inside a symbol's backtest window would corrupt swing points and EMA
values built from raw high/low/close. Historical depth is whatever has
accumulated since each symbol's first ingest (1y initial backfill, 5d
incremental after), not a guaranteed uniform window across the universe.

**Price Structure epic primitives**: `detect_swings()`/`_cluster_zones()`,
the `confirmation_time`-safe as-of discipline, and the `feature_registry`
provider + DB-backed-strategy-factory pattern
(`structural_support_bounce_strategy.py`) all transfer directly. This is
the concrete answer to the spec's explicit instruction not to duplicate
generic infrastructure just because strategies originated from different
sources — a daily EMA pullback strategy should register through
`feature_registry`/`hypothesis_types` the same way, not invent a parallel
mechanism.

**Backtest engine**: `shared/backtest_engine.py::run_backtest()`'s two
execution timings (`next_bar_open` default, `same_bar_close` legacy) and
the `bars[:i+1]` no-lookahead invariant need zero changes — the spec's four
confirmation-entry variants (A immediate / B close-confirm / C prior-day-
high break / D next-open) all express as strategy-callable logic
differences, not engine differences.

## 2. Architectural decisions needed before coding

1. **Corporate-action adjustment (real risk, not just caution)**: decide
   whether daily-EMA-pullback backtests compute swing/EMA primitives from
   `adjclose`-adjusted synthetic OHLC (scale O/H/L by `adjclose/close` per
   bar) rather than raw OHLC, or whether the universe/date-range is
   restricted to windows verified split-free, or whether affected symbols
   are excluded per-backtest once detected. This wasn't a problem for
   Price Structure/Volume epic work so far because neither has hit a
   split inside a tested window yet — this epic's daily, multi-year-scale
   swing trading is far more likely to.
2. **Momentum scoring definition**: no existing primitive to adapt: needs
   a from-scratch, precisely and testably defined metric (e.g. N-day rate
   of change, RS-vs-SPY slope, or a composite), registered as a
   `feature_registry` provider like everything else in this epic rather
   than hardcoded inside a strategy module — consistent with the "shared
   abstraction across strategy families" instruction.
3. **Earnings data reliance**: confirm `FINNHUB_API_KEY` is actually
   configured in the environment any backtest will run against before H8
   (earnings gap) depends on it — otherwise `earnings_events` may be
   empty and H8 untestable, not merely under-tested.
4. **feature_registry / hypothesis_types integration**: recommend this
   epic's primitives (EMA proximity, momentum score, RVOL bucket, breakout
   context) register as `feature_registry` providers and its H1-H10
   hypotheses register as `hypothesis_types` catalog rows, mirroring Price
   Structure's and Volume's own pattern exactly — not a new registration
   mechanism.
5. **VIX freshness for anything beyond historical backtesting**: the
   existing VIX ingest is confirmed to exist as a full historical
   backfill; whether it's kept incrementally current (matters only if this
   epic's work ever needs live/near-real-time VIX, not for backtesting
   against already-backfilled history) is unconfirmed and out of scope to
   resolve here.

## 3. Proposed bounded first PR (not the whole H1-H10 progression)

Per the spec's explicit instruction not to bundle Position Management
Research into implementation, and this repo's own established convention
(Price Structure/Volume epics shipped in small, individually-reviewable
PRs, not one large drop): propose starting with **H1 only** (8 EMA retest),
the smallest slice that exercises the full stack end to end:

- EMA-proximity primitive (ATR- or %-based "near the 8 EMA," never exact
  touch, per the spec's own explicit instruction) as a `feature_registry`
  provider.
- A `daily_8ema_momentum_retest` strategy module (mirrors
  `structural_support_bounce_strategy.py`'s DB-backed factory pattern),
  built on `shared/backtest_engine.py`, testing confirmation-entry variant
  B (close-confirm) first — the simplest lookahead-safe variant — with A/C/D
  as later, separate PRs once B's plumbing is proven.
- Explicitly does NOT touch corporate-action adjustment (decision #1
  above) — the first PR's universe/date-range should be scoped to symbols/
  windows manually verified split-free, with the adjustment work itself as
  a likely-earlier, separate PR once the user weighs in on decision #1.
- Explicitly does NOT include momentum scoring, RVOL bucketing, breakout
  context, earnings, or VIX conditioning — those are H3+ and come after
  H1/H2 (8 EMA / 20 EMA retest) prove the base mechanism works.

H2 (20 EMA retest, structurally identical to H1 with a different period)
would follow immediately after as a near-copy PR, then H3+ build outward
per the spec's own progression, each its own PR.

## 4. Open questions (for the user, not decided here)

1. Decision #1 (corporate-action adjustment) has no clean default —
   worth a real answer before H1 starts, since it affects every later
   hypothesis in the progression, not just H1.
2. Does the proposed H1-first scoping match what you want as the actual
   next PR, or would you rather see the momentum-scoring gap (decision #2)
   resolved first since it blocks more of the H1-H10 progression than H1
   itself does?
3. Should `daily_ema_pullback_momentum` get its own `theses`-level entry
   (a new strategy family) now, or wait until Strategy Incubator's
   `strategies`/`strategy_versions` (Phase 1, just closed) is the
   registration point instead of the older `hypothesis_types` catalog
   path — i.e., should this epic's very first hypothesis be the first to
   go through the new Strategy Incubator machinery end to end rather than
   bypass it?

# Thesis horizons and intraday data

Design note, committed to the repo rather than pasted into a chat session —
`ingest/migrations/001_multi_thesis_architecture.sql` references a
`congressional-shadow-strategy.md` design doc that was pasted into a Claude
Code session and never saved anywhere; it no longer exists outside a
transcript. This doc exists so the same thing doesn't happen twice.

## Problem

The system only had one time resolution (`price_history`, daily bars) and
one implicit thesis horizon (long-term mean reversion, weeks to months).
Two gaps followed from that:

1. No way to build a strategy that reacts faster than once a day.
2. No way to express, at the `theses` level, what kind of strategy a given
   thesis even is — every thesis implicitly assumed daily data and a
   holding period of weeks.

## The `theses.horizon` taxonomy

`theses` (added in migration 001) gets a `horizon` column:

```sql
horizon TEXT NOT NULL DEFAULT 'long_term'
    CHECK (horizon IN ('long_term', 'short_term', 'day_trading'))
```

| Horizon | Data source | Holding period | Execution model | Status |
|---|---|---|---|---|
| `long_term` | `price_history` (daily) | weeks–months | hourly ingest cycle proposes, human approves later | Built — `mean_reversion` |
| `short_term` | `price_history_hourly` | days | **same** propose → human-approve → execute loop as `long_term` | Data layer built here; no signal engine yet |
| `day_trading` | intraday (minute-level, not built) | intraday, same-day flat | **different** — no human-approval latency, PDT/margin rules, same-day flatten | Reserved value only — nothing built |

The key distinction between `short_term` and `day_trading` isn't just data
resolution — it's execution cadence. `short_term` holds positions over
days, so a human still has time to see a WhatsApp ping and approve a trade
before the opportunity is gone. `day_trading`'s entire thesis lifetime can
be a few hours, which the current propose-then-wait architecture cannot
serve at all. Building `day_trading` means a different signal family
(opening-range breakout, VWAP reversion, etc. — not RSI/Bollinger mean
reversion), a different execution loop, and PDT/margin awareness (Alpaca
enforces the $25k equity threshold for >3 day-trades per 5 days on margin
accounts). None of that exists. The `day_trading` enum value exists so the
taxonomy is future-proofed and a setup flow can offer it as a concept, but
nothing in the codebase acts on it.

## The `price_history` / `price_history_hourly` split

Two tables, not a rename and not a rollup:

- **`price_history`** (existing, untouched) — daily bars. Every consumer
  (`signals.py`'s RSI-14/BB-20/ATR-14, `market_regime.py`'s SMA-50/200,
  `WINDOW=260` in the Experiment 002/003 backtests, the time-stop's
  calendar-to-trading-day conversion) assumes 1 row = 1 trading day.
  Renaming it to `price_history_daily` was considered and rejected — too
  much blast radius (referenced in `signals.py`, `market_regime.py`,
  `outcomes.py`, `ingest.py`, `backfill_alpaca.py`, `api/main.py`,
  `schema.sql`, plus both backtest research scripts, plus live production
  data) for a rename that buys nothing functionally.
- **`price_history_hourly`** (new) — hourly bars, sourced independently
  from Alpaca (not derived from `price_history`, not rolled up into it).
  Nothing reads this table yet. It exists so a future `short_term` signal
  engine has data to backtest against from day one, the same way
  `price_history`'s deep Alpaca backfill (`backfill_alpaca.py`) let
  Experiment 002/003 exist without waiting years for data to accumulate.

Aggregating hourly→daily to derive `price_history` was explicitly rejected:
getting a true session open/high/low/close right from hourly bars is an
easy place to introduce a subtle bug, and there's no upside to that risk
when the existing daily pipeline already works and is the thing the
paper-trading validation window is measuring.

## What this unlocks, and what it doesn't

Building this data layer does **not** create a `short_term` thesis. A
`short_term` thesis is real strategy work: an hourly-resolution signal
module (RSI/BB on hourly closes is a *different* indicator from RSI/BB on
daily closes — not the same strategy at higher resolution), and a backtest
that validates it has edge before it ever proposes a live trade — the same
discipline `congress_shreve_hern` is held to (`status = 'backtesting_only'`
until its filing-date-entry-vs-SPY backtest actually runs). Whoever builds
the first `short_term` thesis should follow the Experiment 002 → Experiment
003 pattern: per-signal forward-return validation first, then full
portfolio walk-forward simulation, before touching `status = 'active'`.

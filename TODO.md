# TODO

Parked items — not scheduled, revisit when it makes sense.

## Repo / housekeeping

- **Update README screenshots.** Dashboard has changed a lot recently
  (global markets world clock/map, Bollinger Bands overlay on the symbol
  page, Trade History split into its own tab) — current screenshots predate
  all of it.
- **Consider renaming the repo.** Currently `homelab-trader`, but the
  actual goal is investing (paper trading as the validation phase before
  real capital, not an end in itself) — `homelab-trader` undersells that.
  Candidates floated: `homelab-invest`, `paper-trader-lab`, `thesis-lab`,
  `signal-lab`. Renaming a GitHub repo changes the clone URL (GitHub
  redirects the old one, but it's not free) — decide deliberately, not
  in passing.

## Dashboard

- **Strategy Health tab.** New top-level tab next to Dashboard / Trade
  History. Three sections, different readiness:
  - Signal accuracy (live `signal_outcomes` forward returns by score
    bucket) — data already exists, just needs a UI.
  - Weekly postmortem history (`strategy_review_proposals`) — same, data
    exists, needs a UI.
  - Backtest experiment results (002/003/005) — persistence layer now
    exists (`backtest_results` table, commit `fdf4cf3`), still needs the
    actual UI to read from it.
- **Hypothesis-builder UI.** Bigger, separate direction: lay out strategy
  parameters in the UI itself and see results, closer to what the Jesse
  MCP / Kimi K3 video workflow did. Not scoped yet — worth thinking about
  once Strategy Health exists to show the *output* of one-off backtests.

## Research / signals

- **Global markets composite significance test.** `global_market_signals`
  is live and ingesting daily, but only has a few days of history so far —
  needs weeks of data before an Experiment-005-style permutation test can
  say anything. Explicitly not wired into `score_signal()` until it clears
  that bar (see `schema.sql` comment above the table).
- **Fix the excess-return metric inconsistency in Experiment 005.**
  `avg_excess_return` uses the raw (not stop-loss-aware) 20d return while
  `realized_return` is stop-loss-aware — likely explains why win rate is
  significant at every threshold but excess-return-vs-SPY isn't at 60-90.
  Making both stop-loss-aware would be a cleaner comparison.
- **Bollinger Band squeeze.** Two possible scopes, deliberately not
  conflated:
  - Stage 1 (cheap): a watchlist alert when a symbol's BB moves inside its
    Keltner Channel — no Keltner Channel calc exists in `signals.py` yet.
  - Stage 2 (real commitment): a full trend-following thesis trading the
    breakout direction, with ADX as a confirmation filter (per the video
    this whole line of work started from). Needs its own significance
    test before touching live scoring, same discipline as everything else.

## Bigger picture

- **Define the paper-to-live graduation bar.** Confirmed 2026-07-22: the
  actual goal is real money, not permanent paper trading. No thesis has
  crossed that line yet. Worth eventually writing down, explicitly, what a
  thesis has to clear (win-rate significance? Monte Carlo percentile
  threshold? minimum trade count?) before it's even a candidate for real
  capital — rather than that bar being implicit/vibes-based when the
  moment actually comes.

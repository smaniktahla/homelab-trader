# ADR 0001: OPRA options data access is an economic constraint, not a technical one

**Status**: Accepted
**Date**: 2026-08-04
**Context**: Earnings Volatility Scanner roadmap, PR-002 (`docs/earnings-scanner-pr002-options-data-spike.md`)

## Context

PR-002's capability spike found that Alpaca's `403: "OPRA agreement is not
signed"` gates the premium current-quote feed (`feed=opra`, real
Greeks/IV) and all historical options data (quotes/trades/bars)
uniformly, on this account's current (free) data plan.

Checking Alpaca's plan page confirms where this actually lives: it's not
a standalone legal agreement flow, it's bundled into a paid tier upgrade.
The free **Basic** plan (current plan) lists "US Stocks, ETFs, Options,
and Crypto" and "9+ Years Historical Data" as included, but "Real-Time
Market Coverage", "All US Stock Exchanges", "10,000 API Calls/Min", and
"Unlimited Symbols on WebSocket" are all struck through (not included).
The paid **Algo Trader Plus** tier — **$1,089/year** (yearly billing, one
month free) — adds exactly those four, real-time coverage among them.

**One real ambiguity, not yet resolved**: the plan page's own marketing
copy claims "Options" and "9+ Years Historical Data" are already included
in the free Basic tier — which appears to contradict PR-002's actual 403
findings on historical option quotes/trades/bars. It's not confirmed from
the pricing page alone whether upgrading to Algo Trader Plus is what
actually signs the OPRA agreement, or whether OPRA is a separate
click-through gated behind (but not automatically satisfied by) the
upgrade. Not investigated further here — moot until the economic decision
below changes.

## Decision

**Do not upgrade to Algo Trader Plus at this time.** $1,089/year is a
real, non-trivial recurring cost for a strategy (the earnings volatility
scanner) that has not yet been built, let alone validated to have any
edge. Per the roadmap's own PR-008 gating discipline — the project only
proceeds to execution/dashboard work if the scanner demonstrates a
robust, investable edge out of sample — spending on premium market data
ahead of any evidence the strategy is worth running would invert that
discipline: paying for infrastructure before the thing it serves has
proven itself.

Proceed on PR-002's already-documented fallback path instead:
self-computed IV/Greeks from the free `indicative` feed's current
bid/ask/underlying data, and start accumulating our own point-in-time
option-chain snapshots now (PR-004) so real historical data exists to
backtest against later, without depending on Alpaca's historical surface
at all.

## Consequences

- No historical options backtest is possible from Alpaca's own data
  until/unless this decision is revisited — PR-007/PR-008 (replay and
  edge validation) can only run against snapshots this project captures
  going forward, not against pre-2026-08 history.
- Self-computed Greeks/IV (Black-Scholes solved against observed
  bid/ask/underlying) carry more estimation error than a real Greeks feed
  would — a known, accepted limitation of the free-tier path, not
  something to silently treat as equivalent.
- A meaningful backtest sample is realistically months-to-years out
  (earnings happen quarterly per symbol), not retroactively available.
- **Revisit trigger**: reconsider the $1,089/year upgrade if/when the
  self-computed-data path either (a) shows enough early promise in
  PR-006/PR-007 to justify paying for better data to validate it
  properly, or (b) the self-computed Greeks/IV prove too noisy to trust
  for scoring at all. Until then, this is explicitly a cost decision, not
  a capability judgment about the scanner idea itself.

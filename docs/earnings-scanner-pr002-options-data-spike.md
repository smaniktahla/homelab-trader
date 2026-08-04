# Earnings Volatility Scanner — PR-002: Options Data Capability Spike

Design/investigation note, committed to the repo rather than left in chat —
same reasoning as `docs/thesis-horizons-and-intraday-data.md` and
`docs/signal-component-architecture.md`. This is the first PR of the
Earnings Volatility Scanner roadmap (co-designed with the user and
ChatGPT, 2026-08-04) — see that roadmap for full context on PR-001
through PR-008 and the deferred Phase Two work (multi-leg execution,
dashboard, Hermes explanations, advanced research).

**Per the roadmap's own exit criteria: no options collector implementation
begins until this spike is complete.** It is complete as of this doc —
findings below are a real go/no-go input, not a preliminary guess.

## Method

Read-only API probes against the account's existing Alpaca **paper**
credentials (`ALPACA_API_KEY`/`ALPACA_API_SECRET`/`ALPACA_BASE_URL`,
already configured for `invest-api`/`invest-ingest`) — no orders placed,
no state mutated. Run 2026-08-04:

1. `GET {ALPACA_BASE_URL}/v2/account` — options entitlement fields.
2. `GET https://data.alpaca.markets/v1beta1/options/snapshots/SPY` — current
   chain snapshot, both `feed=indicative` (default) and `feed=opra`.
3. `GET {ALPACA_BASE_URL}/v2/options/contracts` — reference endpoint,
   `status=active` and `status=expired`, for `SPY`.
4. `GET https://data.alpaca.markets/v1beta1/options/bars` and `/trades` —
   historical data for a contract symbol discovered via #3.

## Capability matrix

| Capability | Status | Evidence |
|---|---|---|
| Paper single-leg execution | Works | Already in production use (equity trading, this whole codebase) |
| Paper multi-leg execution | Available | `options_approved_level: 3`, `options_trading_level: 3` on the account — full Level 3 (multi-leg spreads) already granted |
| Live single/multi-leg execution | Untested | Same account fields presumably apply live; not probed (paper-only by design — see standing safety rule against executing real financial trades) |
| Current chain quotes (bid/ask/last/volume) | Works | `GET /v1beta1/options/snapshots/SPY` returns real `latestQuote`/`latestTrade`/`dailyBar`/`minuteBar` per contract |
| Current IV/Greeks | **Not present** | Same snapshot response has no `greeks` or `impliedVolatility` field at all, on `feed=indicative` (the default/free feed) |
| Current IV/Greeks via `feed=opra` | **Blocked** | `403: "OPRA agreement is not signed"` |
| Historical option quotes | **Blocked** | `403: "OPRA agreement is not signed"` |
| Historical option trades/bars | **Blocked** | Same 403, same reason |
| Historical IV/Greeks | **Blocked** | Moot — the whole historical surface is gated, not just Greeks specifically |
| Expired contracts queryable | **Empty** | `status=expired` for `SPY` returned zero contracts (may be OPRA-gated too, or the reference endpoint simply doesn't surface them this way — not fully isolated) |
| Open interest | **Not populated** | `open_interest`/`open_interest_date` are `null` on every contract from the reference endpoint |
| Rate limits | 200 req/window | `X-Ratelimit-Limit: 200`, decrements per call, `X-Ratelimit-Reset` epoch header present on every response |

## The real finding

It is not "Greeks/IV specifically are missing." **One single account
setting — the OPRA (Options Price Reporting Authority) market data
agreement — gates the premium current-quote feed AND all historical
options data uniformly.** `feed=opra` and every historical endpoint tested
return the identical `403: "OPRA agreement is not signed"`. This is more
restrictive than this roadmap's original assumption that historical
option quotes/trades/bars were reachable from February 2024 onward
without further entitlement — empirically, they are not, on this account,
today.

Signing the OPRA agreement (an Alpaca account-dashboard action — accepting
a data-licensing/fee agreement — not something achievable via API, and not
something this assistant performs on the user's behalf) would very likely
unlock the entire matrix in one shot: real Greeks/IV on the current feed,
full historical quotes/trades/bars, and probably genuine expired-contract
lookups. **Decision pending the user checking OPRA's cost.**

## Exit criteria verdict (per the roadmap's own PR-002 exit criteria)

- **Forward-looking scanner**: buildable now, without OPRA. Current
  bid/ask/underlying price is available on the free `indicative` feed —
  compute IV/Greeks ourselves (Black-Scholes solve against observed
  bid/ask/underlying) rather than trusting a feed this account doesn't
  have.
- **Historical backtest**: not possible today, in any form, even
  bid/ask-only reconstruction — the entire historical options surface is
  403'd, not just the Greeks/IV portion of it.
- **IV and Greeks reconstruction**: partially possible — only from
  *current* bid/ask/underlying data (since historical bid/ask is equally
  blocked), and only forward from whenever we start capturing it.
- **Collection of our own point-in-time dataset**: buildable now, per the
  roadmap's own deferred/fallback framing — start persisting immutable
  chain snapshots on a schedule (PR-004) starting today, and accept that a
  meaningful backtest sample only accumulates over real time (earnings
  happen quarterly per symbol, so a usable sample is realistically months
  to years out, not retroactive) unless OPRA is signed.

## Two paths forward

1. **OPRA signed**: re-run this spike's probes to confirm the unlock,
   then PR-002 is fully closed with the richer capability set — historical
   backtest research (PR-007/PR-008) becomes possible against real
   historical option data, not just newly-collected snapshots.
2. **OPRA not signed**: proceed on the forward-only path the roadmap
   already anticipates as its fallback — self-computed IV/Greeks from
   current data, start PR-003/PR-004 (earnings events + snapshot
   collection) immediately, and treat any near-term "backtest" as
   provisional/small-sample until enough real snapshots accumulate.

Either way, PR-001 (earnings event data spike) and PR-003/PR-004 (earnings
ingestion + forward snapshot collection) can proceed in parallel with the
user's OPRA decision — neither depends on it.

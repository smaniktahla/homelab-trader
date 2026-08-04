# Data source roadmap

Backlog of additional free data sources discussed 2026-08-04, captured here
rather than left in chat — same reasoning as `docs/thesis-horizons-and-intraday-data.md`
and `docs/signal-component-architecture.md`: a prior design doc for this
codebase existed only as a pasted-in-chat document and was lost. None of
these are designed in depth yet; each note below is deliberately just
"what it adds, what it would extend, and the obvious open question" —
scoping happens in its own planning session when picked up, same
discipline as Platform Improvements PR B/C got before being built.

## Already in use (for reference, not new work)

- **SEC EDGAR** — `ingest/fundamentals_collector.py` already pulls the
  XBRL `companyconcept` API for financial-statement facts (revenue,
  margins, etc.), feeding `shared/fundamentals.py`'s scoring. Does **not**
  currently pull insider filings (Form 4) — see below, that's new scope,
  not an extension of what's there.
- **Yahoo Finance** — sole price-history source today (`fetch_closes`,
  `ingest.py`'s OHLCV pull into `price_history`/`price_history_hourly`).

## Candidates

### SEC EDGAR insider filings (Form 4)
Free, same EDGAR access pattern `fundamentals_collector.py` already
authenticates against (`SEC_USER_AGENT` header, rate-limited). Would feed
a genuinely new signal-component family — `docs/signal-component-architecture.md`
reserves component slots for `fundamentals`/`earnings_features`/`news_features`/`macro`
but insider-buying/selling isn't one of the five originally named
(fundamentals, earnings, news, options, macro) and would need its own
slot or fold into `fundamentals`. Open question: Form 4 parsing is
notoriously messier than the XBRL facts API (no clean JSON endpoint,
needs XML parsing per filing) — worth a small research spike on data
quality before committing to a build.

### FRED (macroeconomic data)
Free, no API key friction (FRED's API key is free and instant). Natural
fit for the existing regime-detection machinery (`ingest/market_regime.py`),
which today only classifies market regime from SPY trend + VIX — no real
macro data (rates, inflation, unemployment, yield curve) feeds it at all.
This is the most natural next `macro` component-family candidate
`docs/signal-component-architecture.md` already reserved a slot for.
Open question: which specific series matter for a swing/mean-reversion
equity strategy (Fed funds rate and 10y-2y spread are the obvious
starting candidates) vs. scope-creeping into building a general macro
dashboard nobody asked for.

### Financial Modeling Prep (free tier, gap-filler)
Free tier is rate-limited and narrower than a paid plan, so treat as a
**fallback**, not a primary source — it mostly overlaps what EDGAR
(fundamentals) and Yahoo (prices) already cover. Most concrete use: the
already-documented asset-history backfill gap (5 symbols — BDX, BIIB,
BKNG, BF-B, VIX — with zero `price_history` rows; 10 more, incl. BABA,
with implausibly short history), where Yahoo's chart API is apparently
failing silently for specific tickers. Worth checking whether FMP's free
tier actually covers those specific symbols before building anything —
otherwise this is a spike, not a data source.

### GDELT (global news/events)
Free, but the heaviest lift of the five by far — GDELT's raw feed is a
firehose (the full global event/news database, updated every 15
minutes), not a per-symbol news API like Finnhub (`ingest_news` in
`ingest.py` today). Would need real filtering/entity-resolution work to
turn "global news" into "news relevant to symbol X" before it could feed
the `news` component family. Highest uncertain-signal-value-to-effort
ratio of the five — recommend treating as lowest priority unless a
specific macro-news hypothesis emerges that Finnhub's company-level feed
can't answer.

## Suggested rough priority

1. FRED — cheapest to integrate, clearest fit to an already-reserved
   component slot and already-existing regime-detection consumer.
2. FMP as gap-filler — narrow, bounded scope (just the known backfill
   gap), worth a quick spike to see if it's even viable before building.
3. SEC EDGAR insider filings — real value but messier data than the
   XBRL facts API already in use; needs its own research spike first.
4. GDELT — biggest lift, most open-ended, do last if at all.

Not sequenced against Platform Improvements PR D or the Strategy Creation
UI — those are separate roadmap threads (see
`docs/session-handoff-2026-08-03.md`).

CREATE TABLE IF NOT EXISTS watchlist (
    symbol TEXT PRIMARY KEY,
    name TEXT,
    added_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS price_history (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    open NUMERIC,
    high NUMERIC,
    low NUMERIC,
    close NUMERIC,
    volume BIGINT,
    UNIQUE (symbol, ts)
);

CREATE TABLE IF NOT EXISTS news (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT,
    headline TEXT NOT NULL,
    source TEXT,
    url TEXT,
    published_at TIMESTAMPTZ,
    summary TEXT,
    sentiment_score NUMERIC,
    ingested_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (url)
);

CREATE TABLE IF NOT EXISTS signals (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    score NUMERIC,
    rationale TEXT,
    generated_at TIMESTAMPTZ DEFAULT NOW(),
    acted_on BOOLEAN DEFAULT FALSE
);

INSERT INTO watchlist (symbol, name) VALUES
    ('SPY', 'SPDR S&P 500 ETF'),
    ('QQQ', 'Invesco QQQ (NASDAQ-100)'),
    ('AAPL', 'Apple Inc'),
    ('MSFT', 'Microsoft Corp'),
    ('NVDA', 'NVIDIA Corp')
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS market_context (
    id           INTEGER PRIMARY KEY DEFAULT 1,  -- single-row table, upserted on conflict
    spy_trend    TEXT,
    qqq_trend    TEXT,
    spy_sma50    NUMERIC,
    spy_sma200   NUMERIC,
    spy_vs_sma200_pct NUMERIC,
    qqq_sma50    NUMERIC,
    qqq_sma200   NUMERIC,
    qqq_vs_sma200_pct NUMERIC,
    vix          NUMERIC,
    vix_regime   TEXT,
    overall      TEXT,
    score_modifier INTEGER DEFAULT 0,
    alloc_modifier NUMERIC DEFAULT 1.0,
    rationale    TEXT,
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);

-- Per-trading-day market regime history. market_context above is a single
-- overwritten row (today only) -- this is the durable append-by-day
-- counterpart, the data layer for a "find a similar market regime in the
-- past" backtest-date-finder (see shared/market_regime_history.py). Same
-- "if we calculate it, we should store it" default global_market_signals
-- already established. computed_at can update on a same-day re-run
-- (ON CONFLICT DO UPDATE), same intraday-refine behavior market_context has.
CREATE TABLE IF NOT EXISTS market_regime_history (
    trading_date   DATE PRIMARY KEY,
    spy_trend      TEXT,
    qqq_trend      TEXT,
    vix            NUMERIC,
    vix_regime     TEXT,
    overall        TEXT,
    score_modifier INTEGER,
    alloc_modifier NUMERIC,
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_market_regime_history_overall ON market_regime_history (overall);

-- Hierarchical regime PR: sector- and stock-level counterparts to
-- market_regime_history above, one row per trading_date+sector /
-- trading_date+symbol. component_values holds the individual boolean
-- trend/relative-strength inputs (see shared/sector_regime.py,
-- shared/security_regime.py) so a classification can be explained, not
-- just trusted. calculation_version lets the scoring rules evolve without
-- silently mixing incompatible historical rows.
CREATE TABLE IF NOT EXISTS sector_regime_history (
    trading_date             DATE NOT NULL,
    sector                   TEXT NOT NULL,
    benchmark_symbol         TEXT,
    sector_symbol            TEXT,
    classification           TEXT,
    total_score              NUMERIC,
    absolute_trend_score     NUMERIC,
    relative_strength_score  NUMERIC,
    breadth_score            NUMERIC,
    confidence               NUMERIC,
    component_values         JSONB,
    calculation_version      INTEGER NOT NULL DEFAULT 1,
    computed_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (trading_date, sector)
);

CREATE TABLE IF NOT EXISTS security_regime_history (
    trading_date             DATE NOT NULL,
    symbol                   TEXT NOT NULL,
    benchmark_symbol         TEXT,
    sector                   TEXT,
    classification           TEXT,
    total_score              NUMERIC,
    absolute_trend_score     NUMERIC,
    vs_sector_score          NUMERIC,
    vs_market_score          NUMERIC,
    confidence               NUMERIC,
    component_values         JSONB,
    calculation_version      INTEGER NOT NULL DEFAULT 1,
    computed_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (trading_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_security_regime_history_symbol ON security_regime_history (symbol);

-- exit_reason classifies sell proposals by which rule triggered them:
-- thesis_complete | time_stop | stop_loss | overbought | regime_deterioration |
-- thesis_invalidated | portfolio_stop_loss | manual
-- thesis_invalidated added PR 8 (Hypothesis-Driven Trading Architecture epic,
-- shared/signals.py::check_thesis_invalidation_sell) -- structure/regime
-- deterioration or the thesis's own invalidation_spec, distinct from the
-- blunt price-only stop_loss check above.
ALTER TABLE trade_proposals ADD COLUMN IF NOT EXISTS exit_reason TEXT;

CREATE TABLE IF NOT EXISTS trades (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,          -- buy | sell
    qty NUMERIC NOT NULL,
    price NUMERIC NOT NULL,
    notional NUMERIC,            -- qty * price
    order_id TEXT,               -- alpaca order id
    traded_at TIMESTAMPTZ NOT NULL,
    notes TEXT
);

-- PRD v1.1 #1: Signal Outcome Tracking. One row per scored buy/sell signal
-- (mirrors `signals`), whether it turned into a proposal or was blocked by
-- risk gates, plus forward returns/MAE/MFE backfilled by outcomes.py.
CREATE TABLE IF NOT EXISTS signal_outcomes (
    id                  BIGSERIAL PRIMARY KEY,
    signal_id           BIGINT REFERENCES signals(id),
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,          -- buy | sell
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    score               NUMERIC,
    rsi                 NUMERIC,
    bb_upper            NUMERIC,
    bb_middle           NUMERIC,
    bb_lower            NUMERIC,
    band_std            NUMERIC,
    market_regime       TEXT,                   -- market_context.overall at signal time
    symbol_regime       TEXT,                   -- trending_up | trending_down | ranging
    price_at_signal     NUMERIC,
    proposal_id         BIGINT REFERENCES trade_proposals(id),
    proposal_status     TEXT NOT NULL DEFAULT 'blocked',  -- proposed | blocked
    block_reason        TEXT,
    approval_status     TEXT DEFAULT 'n/a',      -- pending | approved | rejected | ignored | n/a
    rejection_reason    TEXT,
    forward_return_1d   NUMERIC,
    forward_return_5d   NUMERIC,
    forward_return_10d  NUMERIC,
    forward_return_20d  NUMERIC,
    mae                 NUMERIC,                 -- max adverse excursion, %
    mfe                 NUMERIC,                 -- max favorable excursion, %
    outcome_updated_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_signal_outcomes_symbol ON signal_outcomes(symbol);
CREATE INDEX IF NOT EXISTS idx_signal_outcomes_generated_at ON signal_outcomes(generated_at);
CREATE INDEX IF NOT EXISTS idx_signal_outcomes_proposal_id ON signal_outcomes(proposal_id);
CREATE INDEX IF NOT EXISTS idx_signal_outcomes_pending ON signal_outcomes(forward_return_20d) WHERE forward_return_20d IS NULL;

-- Schema-drift fix: universe/universe_scan have existed live (scanner.py)
-- since before this repo tracked them here -- same class of gap as the
-- trades.source/status/proposal_id fix below. Recovered verbatim from the
-- live DB (information_schema/pg_indexes, verified 2026-07-31): PK-only,
-- no FKs, no extra constraints beyond what's declared here. IF NOT EXISTS
-- makes this a no-op against the existing live tables.
-- Declared here (not appended at end-of-file) because the ALTER TABLE
-- universe statement immediately below needs the table to already exist
-- on a from-scratch apply.
--
-- This closes universe/universe_scan's own gap, but does NOT make
-- schema.sql as a whole bootstrappable on a truly empty database: earlier
-- in this file (see the trade_proposals ALTER a few dozen lines up, and
-- theses further down) this file already assumes trade_proposals,
-- signal_params and theses exist, and none of those three have a CREATE
-- TABLE anywhere in this repo -- only in prod's own history and, for
-- theses, in migrations/001_multi_thesis_architecture.sql, which nothing
-- runs automatically on container startup. `docker compose up` on an
-- empty volume still fails before reaching this point. Fixing that is a
-- separate, larger schema-drift cleanup, not part of this PR.
CREATE TABLE IF NOT EXISTS universe (
    symbol      TEXT PRIMARY KEY,
    name        TEXT,
    exchange    TEXT,
    added_at    TIMESTAMPTZ DEFAULT NOW(),
    scannable   BOOLEAN DEFAULT FALSE,
    sector      TEXT
);

-- No FK to universe: scanner.py upserts by symbol independently and never
-- joins PK->PK between these two tables in the live app.
CREATE TABLE IF NOT EXISTS universe_scan (
    symbol      TEXT PRIMARY KEY,
    price       NUMERIC,
    rsi         NUMERIC,
    buy_score   NUMERIC DEFAULT 0,
    sell_score  NUMERIC DEFAULT 0,
    regime      TEXT,
    scanned_at  TIMESTAMPTZ DEFAULT NOW()
);

-- PRD v1.1 #3: Sector Concentration Cap. GICS sector, scraped alongside the
-- S&P 500 constituent list already fetched in scanner.py; NULL for ETFs/
-- unclassified symbols, which the cap check skips.
ALTER TABLE universe ADD COLUMN IF NOT EXISTS sector TEXT;

INSERT INTO signal_params (key, value, description) VALUES
    ('sector_max_pct', 0.30, 'Max portfolio fraction in any single GICS sector (30%)')
ON CONFLICT (key) DO NOTHING;

-- PRD v1.1 #2: Earnings Blackout. Known earnings dates from Finnhub's free
-- calendar endpoint; signals.py blocks new BUY proposals within
-- earnings_blackout_days of a symbol's date (either side).
CREATE TABLE IF NOT EXISTS earnings_events (
    id             BIGSERIAL PRIMARY KEY,
    symbol         TEXT NOT NULL,
    earnings_date  DATE NOT NULL,
    fetched_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (symbol, earnings_date)
);

CREATE INDEX IF NOT EXISTS idx_earnings_events_symbol ON earnings_events(symbol);

INSERT INTO signal_params (key, value, description) VALUES
    ('earnings_blackout_days', 3, 'Block new BUY proposals within N days of a known earnings date, either side')
ON CONFLICT (key) DO NOTHING;

-- PRD v1.1 #4: Portfolio Circuit Breaker. high_water_mark is the running
-- all-time max portfolio value since tracking began (not a fixed account
-- baseline); drawdown_pct is computed against it on every ingest cycle.
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id                BIGSERIAL PRIMARY KEY,
    snapshot_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    portfolio_value   NUMERIC NOT NULL,
    high_water_mark   NUMERIC NOT NULL,
    drawdown_pct      NUMERIC NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_at ON portfolio_snapshots(snapshot_at);

INSERT INTO signal_params (key, value, description) VALUES
    ('circuit_breaker_drawdown_pct', 0.15, 'Pause new BUY proposals if drawdown from all-time high-water mark exceeds this fraction (15%). Sells continue; never liquidates automatically.')
ON CONFLICT (key) DO NOTHING;

-- Weekly postmortem: calibration review over resolved signal_outcomes
-- (forward_return_20d IS NOT NULL). Advisory only — never writes to
-- signal_params itself; a human applies proposed_param/proposed_value via
-- the existing PATCH /api/signal-params/{key} endpoint if they agree.
CREATE TABLE IF NOT EXISTS strategy_review_proposals (
    id               BIGSERIAL PRIMARY KEY,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    window_start     TIMESTAMPTZ,
    window_end       TIMESTAMPTZ,
    n_resolved       INTEGER NOT NULL,
    metric_summary   JSONB,
    finding          TEXT NOT NULL,
    proposed_param   TEXT,
    current_value    NUMERIC,
    proposed_value   NUMERIC,
    status           TEXT NOT NULL DEFAULT 'advisory'  -- advisory | applied | dismissed
);

CREATE INDEX IF NOT EXISTS idx_strategy_review_created_at ON strategy_review_proposals(created_at);

-- Schema-drift fix: these columns exist on the live trades table (added
-- out-of-band at some point) but were never added here. Declaring them
-- IF NOT EXISTS is a no-op on the live DB and keeps a from-scratch deploy
-- (docker compose up on an empty volume) consistent with what main.py's
-- INSERT statements actually reference (source, status, proposal_id).
ALTER TABLE trades ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE trades ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'filled';
ALTER TABLE trades ADD COLUMN IF NOT EXISTS proposal_id BIGINT REFERENCES trade_proposals(id);

-- Trade cost model: flat $ commission modeled per executed trade (paper
-- trading only — Alpaca itself charges $0 in the sandbox). Recorded on the
-- trade row at fill time so changing the setting later doesn't retroactively
-- rewrite the cost of past trades. Deducted from displayed cash/portfolio
-- value (see /api/account, /api/portfolio-history) — never from Alpaca's own
-- account balance, which has no concept of a modeled fee.
ALTER TABLE trades ADD COLUMN IF NOT EXISTS cost NUMERIC NOT NULL DEFAULT 0;

INSERT INTO signal_params (key, value, description) VALUES
    ('trade_cost_flat', 1.00, 'Flat $ commission modeled per executed trade (paper trading only — Alpaca charges $0 itself). Deducted from displayed cash/portfolio value, not from the real Alpaca balance.')
ON CONFLICT (key) DO NOTHING;

-- Thesis horizon taxonomy: long_term (daily data, current mean_reversion
-- thesis) / short_term (hourly data, still human-approved) / day_trading
-- (reserved value only — no signal engine, no execution loop, no PDT/risk
-- model built for it). See docs/thesis-horizons-and-intraday-data.md.
-- Existing theses rows backfill to 'long_term' via the column DEFAULT.
ALTER TABLE theses ADD COLUMN IF NOT EXISTS horizon TEXT NOT NULL
    DEFAULT 'long_term' CHECK (horizon IN ('long_term', 'short_term', 'day_trading'));

-- Hourly OHLC bars — separate from price_history's daily bars, independently
-- sourced from Alpaca (not rolled up into or derived from price_history, no
-- rename of price_history itself). Nothing reads this table yet; it exists
-- so a future short_term thesis has data to backtest against from day one.
-- See docs/thesis-horizons-and-intraday-data.md.
CREATE TABLE IF NOT EXISTS price_history_hourly (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    open NUMERIC,
    high NUMERIC,
    low NUMERIC,
    close NUMERIC,
    volume BIGINT,
    UNIQUE (symbol, ts)
);

-- Leading-indicator international markets for the global-markets dashboard
-- (see 2026-07-22 DocMost session notes). home_market itself lives in
-- app_settings (default 'us_nyse'), not here -- every row here is just a
-- market, the UI decides which one gets the star. index_symbol is a Yahoo
-- ticker, ingested via the existing ingest_prices()/fetch_prices_yf() path
-- into price_history like any other symbol -- no separate fetch mechanism.
-- market_cap_usd_tn is a hand-entered approximate snapshot for marker
-- sizing on the map, not a live feed -- don't use it for anything that
-- trades money.
CREATE TABLE IF NOT EXISTS global_markets (
    id                    BIGSERIAL PRIMARY KEY,
    slug                  TEXT NOT NULL UNIQUE,
    display_name          TEXT NOT NULL,
    index_symbol          TEXT NOT NULL,
    lat                   NUMERIC NOT NULL,
    lon                   NUMERIC NOT NULL,
    market_cap_usd_tn     NUMERIC,
    timezone              TEXT NOT NULL,
    local_open_hour       NUMERIC,
    local_close_hour      NUMERIC,
    is_leading_indicator  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO global_markets (slug, display_name, index_symbol, lat, lon, market_cap_usd_tn, timezone, local_open_hour, local_close_hour, is_leading_indicator) VALUES
    ('us_nyse',   'United States — NYSE / NASDAQ', 'SPY',       40.7,  -74.0, 55.0, 'America/New_York',  9.5,  16.0,  FALSE),
    ('tokyo',     'Japan — Nikkei 225',            '^N225',     35.7,  139.7,  6.5, 'Asia/Tokyo',         9.0,  15.0,  TRUE),
    ('hong_kong', 'Hong Kong — Hang Seng',         '^HSI',      22.3,  114.2,  4.5, 'Asia/Hong_Kong',     9.5,  16.0,  TRUE),
    ('shanghai',  'China — Shanghai Composite',    '000001.SS', 31.2,  121.5,  7.0, 'Asia/Shanghai',      9.5,  15.0,  TRUE),
    ('sydney',    'Australia — ASX 200',           '^AXJO',    -33.9,  151.2,  1.7, 'Australia/Sydney',  10.0,  16.0,  TRUE),
    ('london',    'United Kingdom — FTSE 100',     '^FTSE',     51.5,   -0.1,  3.5, 'Europe/London',      8.0,  16.5,  TRUE),
    ('frankfurt', 'Germany — DAX',                 '^GDAXI',    50.1,    8.7,  2.2, 'Europe/Berlin',      9.0,  17.5,  TRUE),
    ('taiwan',    'Taiwan — TWSE',                 '^TWII',     25.0,  121.5,  1.8, 'Asia/Taipei',        9.0,  13.5,  TRUE),
    ('korea',     'South Korea — KOSPI',           '^KS11',     37.5,  127.0,  1.9, 'Asia/Seoul',         9.0,  15.5,  TRUE),
    ('mumbai',    'India — Sensex',                '^BSESN',    19.1,   72.9,  5.0, 'Asia/Kolkata',       9.25, 15.5,  TRUE)
ON CONFLICT (slug) DO NOTHING;

-- Append-only daily time series -- distinct from market_context (single
-- upserted row, no history -- fine for live scoring, useless for
-- backtesting whether this composite has any edge). Populated by
-- ingest_global_market_signals() each cycle. Not read by score_signal() or
-- any thesis yet -- must clear an Experiment-005-style permutation
-- significance test first (see backtest_rule_significance.py).
CREATE TABLE IF NOT EXISTS global_market_signals (
    id            BIGSERIAL PRIMARY KEY,
    market_id     BIGINT NOT NULL REFERENCES global_markets(id),
    trading_date  DATE NOT NULL,
    overnight_pct NUMERIC,
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (market_id, trading_date)
);

CREATE INDEX IF NOT EXISTS idx_global_market_signals_trading_date ON global_market_signals (trading_date);
CREATE INDEX IF NOT EXISTS idx_global_market_signals_market_id ON global_market_signals (market_id);

-- Durable home for backtest_*.py research script output -- previously each
-- script only wrote JSON to /tmp inside the container, wiped on every
-- restart, with zero UI surface. This is what a future "Strategy Health"
-- dashboard tab reads from. Scripts keep writing their /tmp JSON too (handy
-- for quick local inspection the same session); this table is the durable
-- copy. See 2026-07-22 DocMost session notes.
CREATE TABLE IF NOT EXISTS backtest_results (
    id            BIGSERIAL PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    run_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    git_commit    TEXT,
    summary       TEXT,
    results       JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_backtest_results_experiment_id ON backtest_results (experiment_id);
CREATE INDEX IF NOT EXISTS idx_backtest_results_run_at ON backtest_results (run_at);

-- Dividend/split-adjusted close, alongside the raw close price_history
-- already stored. Yahoo's chart API returns this in the same response
-- (indicators.adjclose, parallel array to indicators.quote[0].close) --
-- no extra fetch needed, just parsing more of what's already coming back.
-- NULL for existing rows until re-fetched; used to make the dashboard's
-- SPY comparison a true total-return benchmark instead of price-only.
-- See 2026-07-22 session notes.
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS adjclose NUMERIC;

-- Buy Cooldown. The RSI/BB oversold condition that triggers a BUY signal can
-- stay true for several consecutive scan cycles — without this, the scanner
-- re-proposes (and the user can re-approve) the same BUY every cycle even
-- though the position was already sized for that signal the first time.
INSERT INTO signal_params (key, value, description) VALUES
    ('buy_cooldown_days', 2, 'Skip new BUY proposals for a symbol within N days of its last filled BUY trade')
ON CONFLICT (key) DO NOTHING;

-- Signal component infrastructure (PR #1 of the multi-source quant signal
-- integration -- see docs/signal-component-architecture.md). Append-only,
-- point-in-time feature snapshots. One row per (symbol, side, as_of,
-- feature_version) because score_signal() already produces an independent
-- score per side, not one scalar per symbol -- this mirrors signal_outcomes'
-- own `side` column rather than inventing a fictional composite.
--
-- PR #1 populates technical_score only; fundamental/earnings/news/options/
-- macro_fit_score are NULL until their own PR lands a real collector. NULL
-- means "not yet computed", never 0 -- a missing optional signal must never
-- read as bearish.
CREATE TABLE IF NOT EXISTS symbol_features (
    id                   BIGSERIAL PRIMARY KEY,
    symbol               TEXT NOT NULL,
    side                 TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    as_of                TIMESTAMPTZ NOT NULL,
    technical_score      NUMERIC,
    fundamental_score    NUMERIC,
    earnings_score       NUMERIC,
    news_score           NUMERIC,
    options_score        NUMERIC,
    macro_fit_score      NUMERIC,
    data_confidence      NUMERIC NOT NULL
        CHECK (data_confidence >= 0 AND data_confidence <= 1),
    feature_version      TEXT NOT NULL,
    model_version        TEXT NOT NULL,
    component_weights    JSONB NOT NULL DEFAULT '{}',
    source_timestamps    JSONB NOT NULL DEFAULT '{}',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, as_of, side, feature_version)
);

CREATE INDEX IF NOT EXISTS idx_symbol_features_symbol_as_of
    ON symbol_features (symbol, as_of DESC);

-- signal_outcomes gets a nullable pointer to the snapshot used, plus copies
-- of the component scores actually attached at signal time so historical
-- rows remain queryable without joining symbol_features (which may later
-- be pruned -- see ON DELETE SET NULL below). feature_snapshot_id is
-- nullable and attachment is best-effort/fail-open: a failed feature
-- write must never block or alter the existing proposal decision path.
ALTER TABLE signal_outcomes
    ADD COLUMN IF NOT EXISTS technical_score    NUMERIC,
    ADD COLUMN IF NOT EXISTS fundamental_score  NUMERIC,
    ADD COLUMN IF NOT EXISTS earnings_score     NUMERIC,
    ADD COLUMN IF NOT EXISTS news_score         NUMERIC,
    ADD COLUMN IF NOT EXISTS options_score      NUMERIC,
    ADD COLUMN IF NOT EXISTS macro_fit_score    NUMERIC,
    ADD COLUMN IF NOT EXISTS data_confidence    NUMERIC
        CHECK (data_confidence IS NULL OR
               (data_confidence >= 0 AND data_confidence <= 1)),
    ADD COLUMN IF NOT EXISTS feature_snapshot_id BIGINT
        REFERENCES symbol_features(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS feature_version    TEXT,
    ADD COLUMN IF NOT EXISTS model_version      TEXT,
    ADD COLUMN IF NOT EXISTS component_weights  JSONB,
    ADD COLUMN IF NOT EXISTS vetoes             JSONB;

-- Fundamentals (PR #2 of the multi-source signal integration -- see
-- docs/signal-component-architecture.md and docs/fundamentals-collector.md).
-- Raw, append-only observations from SEC EDGAR XBRL companyfacts, kept
-- deliberately separate from the derived fundamental_score written into
-- symbol_features/signal_outcomes. accepted_at is the timestamp a
-- point-in-time query must filter on -- a backtest (or a live signal
-- generated at time T) may only use a fact whose accepted_at <= T,
-- regardless of which fiscal period the fact describes or when it was
-- ingested into this table. Never overwrite an existing row: a metric can
-- be legitimately restated in a later filing, which must show up as a
-- SECOND row (new accession_number), not a mutation of the first --
-- history must stay reconstructable exactly as it looked at any past
-- accepted_at, not just as it looks now.
CREATE TABLE IF NOT EXISTS fundamental_facts (
    id                  BIGSERIAL PRIMARY KEY,
    symbol              TEXT NOT NULL,
    metric              TEXT NOT NULL,          -- e.g. 'Revenues', 'GrossProfit', 'NetIncomeLoss' (us-gaap XBRL tag name, unmodified)
    value               NUMERIC,
    unit                TEXT,                    -- e.g. 'USD'
    fiscal_period       TEXT,                    -- e.g. 'Q2-2026', 'FY2025'
    period_start        DATE,
    period_end          DATE,
    filed_at            TIMESTAMPTZ,
    accepted_at         TIMESTAMPTZ,             -- point-in-time filter column -- see comment above
    form_type           TEXT,                    -- '10-Q' | '10-K' | ...
    accession_number    TEXT,                    -- SEC's own filing identifier -- part of the natural key
    source              TEXT NOT NULL DEFAULT 'sec_edgar',
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, metric, fiscal_period, accession_number)
);

CREATE INDEX IF NOT EXISTS idx_fundamental_facts_symbol_metric_accepted
    ON fundamental_facts (symbol, metric, accepted_at DESC);

-- Schema-drift fix: trades.thesis_id has been written by both INSERT INTO
-- trades call sites in api/main.py for some time but was never declared
-- here -- same class of gap as trades.source/status/proposal_id above.
-- No FK to theses (theses itself has no tracked CREATE TABLE -- see the
-- universe/universe_scan comment earlier in this file for the same
-- pre-existing bootstrap gap); adding one here would make a from-scratch
-- apply fail even harder than it already does, not fix anything.
ALTER TABLE trades ADD COLUMN IF NOT EXISTS thesis_id BIGINT;

-- Platform Improvements PR A: position lifecycles / R-multiple foundation.
-- Introduces the first-ever PERSISTED stop price anywhere in this codebase
-- -- until now, stop-loss has only ever been a live percentage
-- (theses.config.stop_loss_pct / signal_params) re-evaluated every cycle
-- against the current price by shared/signals.py::check_stop_losses(),
-- never a stored price level. planned_initial_stop_price is deliberately
-- derived from that SAME existing ratio at proposal time
-- (planned_entry_price * (1 - stop_loss_pct)) -- this is a snapshot of
-- what the existing mechanism already implies, not new risk-sizing logic.
-- Real risk-based position sizing remains a separate, later decision.
--
-- planned_* fields apply to BUY/position-opening proposals only; NULL on
-- sell/exit proposals (not applicable, never coerced to 0) and on manual
-- trades with no linked proposal at all.
ALTER TABLE trade_proposals
    ADD COLUMN IF NOT EXISTS planned_entry_price       NUMERIC,
    ADD COLUMN IF NOT EXISTS planned_initial_stop_price NUMERIC,
    ADD COLUMN IF NOT EXISTS planned_risk_per_share    NUMERIC,
    ADD COLUMN IF NOT EXISTS planned_risk_dollars      NUMERIC;

-- Copied immutably from trade_proposals.planned_initial_stop_price (via
-- proposal_id) at the moment a trade is inserted -- this is what makes a
-- lifecycle's risk basis fixed at entry even if stop_loss_pct or the
-- proposal's own fields change later. NULL for manual trades (no
-- proposal_id).
ALTER TABLE trades ADD COLUMN IF NOT EXISTS initial_stop_price NUMERIC;

-- One row per position lifecycle (open or closed), built by the idempotent
-- ingest/build_position_lifecycles.py from the full trades ledger --
-- trades itself stays append-only and is never annotated beyond the
-- additive columns above; lifecycles are always DERIVED, never
-- hand-maintained, mirroring the price_history -> signals -> signal_outcomes
-- derivation pattern already established in this codebase. Rebuilding is
-- always safe to re-run (truncate-and-rebuild per symbol) since trades is
-- the only source of truth.
--
-- Was distinct, at the time this table was introduced (Platform
-- Improvements PR A), from shared/round_trips.py's average-cost
-- reconstruction -- this is true FIFO lot matching, with a real
-- planned-vs-actual risk basis and pathwise MAE/MFE round_trips.py never
-- had. Platform Improvements PR A.1 repointed /api/symbol-performance at
-- this table and removed round_trips.py, which no longer exists in this
-- codebase -- see shared/lifecycle_performance.py for the current
-- read-side logic.
--
-- All risk/R/excursion fields are NULL when unknown or not applicable --
-- never coerced to 0 -- consistent with every other NULL-vs-zero decision
-- already made in this codebase (symbol_features, fundamental_facts).
CREATE TABLE IF NOT EXISTS position_lifecycles (
    id                              BIGSERIAL PRIMARY KEY,
    symbol                          TEXT NOT NULL,
    thesis_id                       BIGINT,          -- NULL if ambiguous -- see concurrent_multi_thesis_symbol below
    status                          TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    opened_at                       TIMESTAMPTZ NOT NULL,
    closed_at                       TIMESTAMPTZ,      -- NULL while open
    qty                             NUMERIC NOT NULL,
    planned_entry_price             NUMERIC,          -- from the originating proposal; NULL for manual trades
    planned_initial_stop_price      NUMERIC,
    planned_risk_per_share          NUMERIC,
    planned_risk_dollars            NUMERIC,
    initial_stop_price              NUMERIC,          -- copied immutably from trades.initial_stop_price at first entry fill
    actual_initial_risk_per_share   NUMERIC,          -- |first fill price - initial_stop_price|
    actual_initial_risk_dollars     NUMERIC,
    entry_notional                  NUMERIC,
    exit_notional                   NUMERIC,          -- realized-to-date; 0.0 if nothing sold yet, partial if still open
    total_cost                      NUMERIC,          -- entry cost prorated to shares actually sold + full exit cost
    gross_pnl                       NUMERIC,          -- realized-to-date; 0.0 baseline, partial if still open
    net_pnl                         NUMERIC,          -- realized-to-date; 0.0 baseline, partial if still open
    realized_r                      NUMERIC,          -- net_pnl / actual_initial_risk_dollars; NULL if risk unknown or zero
    mae_price                       NUMERIC,          -- worst price reached during holding (pathwise, real)
    mfe_price                       NUMERIC,          -- best price reached during holding (pathwise, real)
    mae_r                           NUMERIC,          -- NULL if actual_initial_risk_dollars unknown
    mfe_r                           NUMERIC,
    excursion_resolution            TEXT CHECK (excursion_resolution IS NULL
                                         OR excursion_resolution IN ('hourly', 'daily_approximation')),
    data_quality_flags              TEXT[] NOT NULL DEFAULT '{}',  -- e.g. 'concurrent_multi_thesis_symbol', 'concurrent_multi_trade_thesis_position', 'pre_ledger_holding_excluded'
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_position_lifecycles_symbol ON position_lifecycles (symbol);
CREATE INDEX IF NOT EXISTS idx_position_lifecycles_status ON position_lifecycles (status);
CREATE INDEX IF NOT EXISTS idx_position_lifecycles_opened_at ON position_lifecycles (opened_at);

-- Normalized join between trades and position_lifecycles -- deliberately
-- NOT an array column (e.g. trades.position_id) on either side, because a
-- single trade can legitimately be allocated across more than one
-- lifecycle (a partial fill split across concurrent lots, or the
-- cross-thesis concurrent-symbol edge case flagged above) and a single
-- lifecycle is built from more than one trade (pyramided entries, partial
-- exits). qty_allocated carries the split when a trade's full qty isn't
-- entirely consumed by one lifecycle.
CREATE TABLE IF NOT EXISTS position_trades (
    id                     BIGSERIAL PRIMARY KEY,
    position_lifecycle_id  BIGINT NOT NULL REFERENCES position_lifecycles(id),
    trade_id               BIGINT NOT NULL REFERENCES trades(id),
    role                   TEXT NOT NULL CHECK (role IN ('entry', 'exit')),
    qty_allocated          NUMERIC NOT NULL,
    UNIQUE (position_lifecycle_id, trade_id)
);

CREATE INDEX IF NOT EXISTS idx_position_trades_lifecycle ON position_trades (position_lifecycle_id);
CREATE INDEX IF NOT EXISTS idx_position_trades_trade ON position_trades (trade_id);

-- Per-symbol unmatched_sell_qty from the same match_lifecycles() run that
-- builds position_lifecycles/position_trades above -- DERIVED, rebuilt in
-- the same truncate-and-rebuild transaction, never hand-maintained. This
-- number (sell quantity that couldn't be matched to any locally-known
-- FIFO lot -- pre-ledger holdings, data gaps) only ever existed as an
-- in-memory value summed into a log line until this table; API consumers
-- (methodology_status/unmatched_sell_qty on /api/symbol-performance) need
-- it per-symbol without re-walking the full trade ledger on every request.
-- One row per symbol that appears in a match_lifecycles() run, including
-- rows with unmatched_sell_qty = 0 -- so a caller can tell "verified zero"
-- apart from "no lifecycle data built for this symbol at all" (absent row).
CREATE TABLE IF NOT EXISTS position_lifecycle_symbol_status (
    symbol               TEXT PRIMARY KEY,
    unmatched_sell_qty   NUMERIC NOT NULL DEFAULT 0,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Platform Improvements PR C: one row per manual trade (POST /api/trade) or
-- proposal approval (PATCH /api/proposals/{id}) -- the two paths that
-- bypass every gate shared/signals.py's compute_signals() enforces for the
-- automated pipeline (circuit breaker, max_open_positions, sector cap, buy
-- cooldown, earnings blackout, position sizing). Advisory only: nothing
-- reads this table to block anything, it exists purely so a bypassed rule
-- leaves a record instead of vanishing silently. Written by
-- shared/rule_adherence.py's check_gates(), called from api/main.py
-- fail-open (a failure here must never affect the trade/approval response).
--
-- Forward-looking only, same precedent as position_lifecycles.realized_r --
-- there is no way to reconstruct exact portfolio state at a past manual
-- trade's moment, so trades before this shipped simply have no row here.
CREATE TABLE IF NOT EXISTS rule_adherence_checks (
    id             BIGSERIAL PRIMARY KEY,
    checked_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    context        TEXT NOT NULL CHECK (context IN ('manual_trade', 'proposal_approval')),
    trade_id       BIGINT REFERENCES trades(id),
    proposal_id    BIGINT REFERENCES trade_proposals(id),
    symbol         TEXT NOT NULL,
    side           TEXT NOT NULL,
    rule_results   JSONB NOT NULL,   -- [{"rule": ..., "passed": bool, "detail": str|null}, ...]
    any_violation  BOOLEAN NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rule_adherence_checks_checked_at ON rule_adherence_checks (checked_at);
CREATE INDEX IF NOT EXISTS idx_rule_adherence_checks_trade_id ON rule_adherence_checks (trade_id);
CREATE INDEX IF NOT EXISTS idx_rule_adherence_checks_proposal_id ON rule_adherence_checks (proposal_id);

-- Hierarchical regime PR: per-proposal snapshot of the market/sector/stock
-- regime hierarchy at proposal time (regime_snapshot mirrors the JSON shape
-- snapshot_hierarchy_for_symbol() returns), plus the score breakdown that
-- keeps base_strategy_score separately visible from any regime-driven
-- adjustment. final_proposal_score == base_strategy_score whenever
-- regime_scoring_enabled is off (the default) -- see shared/regime_scoring.py.
ALTER TABLE trade_proposals
    ADD COLUMN IF NOT EXISTS regime_snapshot             JSONB,
    ADD COLUMN IF NOT EXISTS hierarchy_alignment          TEXT,
    ADD COLUMN IF NOT EXISTS base_strategy_score          INTEGER,
    ADD COLUMN IF NOT EXISTS market_regime_adjustment     INTEGER,
    ADD COLUMN IF NOT EXISTS sector_regime_adjustment     INTEGER,
    ADD COLUMN IF NOT EXISTS relative_strength_adjustment INTEGER,
    ADD COLUMN IF NOT EXISTS total_regime_adjustment      INTEGER,
    ADD COLUMN IF NOT EXISTS final_proposal_score         INTEGER;

-- Hierarchical regime scoring config -- flat signal_params rows, same
-- pattern as sector_max_pct etc. above. Disabled by default: existing
-- proposal generation/gating behavior is unchanged until a human flips
-- regime_scoring_enabled to 1 via PATCH /api/signal-params/regime_scoring_enabled.
INSERT INTO signal_params (key, value, description) VALUES
    ('regime_scoring_enabled', 0, 'Master switch for hierarchical regime score adjustments (0=off, matches pre-PR behavior)'),
    ('regime_mkt_bull_sector_bull', 15, 'Score adjustment: bullish market + bullish sector'),
    ('regime_mkt_bull_sector_neutral', 5, 'Score adjustment: bullish market + neutral sector'),
    ('regime_mkt_bull_sector_bear', -10, 'Score adjustment: bullish market + bearish sector'),
    ('regime_mkt_bear_sector_bull', 0, 'Score adjustment: bearish market + bullish sector'),
    ('regime_mkt_bear_sector_neutral', -10, 'Score adjustment: bearish market + neutral sector'),
    ('regime_mkt_bear_sector_bear', -20, 'Score adjustment: bearish market + bearish sector'),
    ('regime_stock_outperform_sector', 5, 'Score adjustment: stock outperforming its own sector'),
    ('regime_stock_underperform_sector', -5, 'Score adjustment: stock underperforming its own sector')
ON CONFLICT (key) DO NOTHING;

-- Risk engine (see docs/risk-engine-architecture-reconciliation.md). One
-- row per shared/risk_engine.py::evaluate_proposal() call -- the single
-- authoritative record of approved_quantity, distinct from
-- rule_adherence_checks above (which is purely advisory and never
-- constrains anything). A proposal gets its first risk_decisions row when
-- compute_signals() creates it (requested_qty = the strategy's own sized
-- qty); decide_proposal()/execute_trade() re-evaluate at approval time
-- against then-current portfolio state and clamp to that row's
-- approved_quantity -- see the two call sites for why a second evaluation
-- is necessary rather than trusting the proposal-time row (portfolio state
-- can have moved between proposal and human approval, sometimes by days).
-- Forward-looking only, same precedent as rule_adherence_checks and
-- position_lifecycles.realized_r -- no backfill for trades made before
-- this shipped.
CREATE TABLE IF NOT EXISTS risk_decisions (
    id                    BIGSERIAL PRIMARY KEY,
    decided_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    context               TEXT NOT NULL CHECK (context IN ('proposal_generated', 'proposal_approval', 'manual_trade')),
    proposal_id           BIGINT REFERENCES trade_proposals(id),
    symbol                TEXT NOT NULL,
    side                  TEXT NOT NULL,
    requested_qty         NUMERIC NOT NULL,
    approved_quantity     NUMERIC NOT NULL,
    outcome               TEXT NOT NULL CHECK (outcome IN ('approved', 'reduced', 'rejected')),
    risk_budget_dollars   NUMERIC,
    binding_constraint    TEXT,             -- NULL only when outcome = 'approved'
    constraint_detail     JSONB NOT NULL,   -- every limit checked, not just the binding one
    market_regime_at_decision TEXT          -- audit only, see reconciliation doc section C.3/D -- never an input to sizing
);

CREATE INDEX IF NOT EXISTS idx_risk_decisions_decided_at ON risk_decisions (decided_at);
CREATE INDEX IF NOT EXISTS idx_risk_decisions_proposal_id ON risk_decisions (proposal_id);

INSERT INTO signal_params (key, value, description) VALUES
    ('risk_per_trade_pct', 0.01, 'Fraction of portfolio value the risk engine budgets as dollar risk on any single new position (1%)'),
    ('max_portfolio_open_risk_pct', 0.06, 'Fraction of portfolio value the risk engine allows as combined dollar risk across all open positions at once (6%)')
ON CONFLICT (key) DO NOTHING;

-- Risk Engine PR 3: trading-permission aggregation (see
-- shared/trading_permission.py). loss_streak_limit pauses new BUY entries
-- account-wide after this many consecutive losing closed position_lifecycles
-- (net_pnl <= 0) in a row -- never affects sells or existing positions,
-- same "brake on new risk only" principle circuit_breaker_drawdown_pct
-- already established.
INSERT INTO signal_params (key, value, description) VALUES
    ('loss_streak_limit', 4, 'Pause new BUY entries account-wide after this many consecutive losing closed positions in a row')
ON CONFLICT (key) DO NOTHING;

-- Market Structure Engine PR 2 (see shared/market_structure.py). One row
-- per symbol per trading_date, same trading_date+key PRIMARY KEY /
-- component_values-JSONB-plus-flat-columns / calculation_version split as
-- sector_regime_history and security_regime_history above.
-- component_values holds the full combined per-timeframe (monthly/weekly/
-- daily) breakdown so a classification can be explained, not just
-- trusted -- not just the flat columns duplicated here for querying.
CREATE TABLE IF NOT EXISTS market_structure_history (
    trading_date          DATE NOT NULL,
    symbol                TEXT NOT NULL,
    trend                 TEXT,
    confidence             NUMERIC,
    trend_strength        TEXT,
    volatility            TEXT,
    bos                   BOOLEAN,
    choch                 BOOLEAN,
    risk                  TEXT,
    component_values      JSONB,
    calculation_version   INTEGER NOT NULL DEFAULT 1,
    computed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (trading_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_market_structure_history_symbol ON market_structure_history (symbol);

-- Per-proposal snapshot of market structure at proposal time, same
-- "computed once per cycle by update_market_structure(), read back (not
-- recomputed live) at proposal time" pattern as regime_snapshot above.
-- structure_score_adjustment mirrors total_regime_adjustment (see
-- shared/structure_scoring.py) -- 0 for every proposal until a human
-- explicitly flips structure_scoring_enabled on.
ALTER TABLE trade_proposals
    ADD COLUMN IF NOT EXISTS market_structure_snapshot JSONB,
    ADD COLUMN IF NOT EXISTS structure_trend            TEXT,
    ADD COLUMN IF NOT EXISTS structure_confidence        INTEGER,
    ADD COLUMN IF NOT EXISTS structure_score_adjustment  INTEGER;

-- Market Structure Engine scoring config -- flat signal_params rows, same
-- pattern/precedent as the hierarchical regime scoring keys above.
-- Disabled by default: existing proposal generation/gating behavior is
-- unchanged until a human flips structure_scoring_enabled to 1 via
-- PATCH /api/signal-params/structure_scoring_enabled.
INSERT INTO signal_params (key, value, description) VALUES
    ('structure_scoring_enabled', 0, 'Master switch for Market Structure Engine score adjustments (0=off, matches pre-PR behavior)'),
    ('structure_trend_bullish', 10, 'Score adjustment when top-down (monthly+weekly) structure trend is bullish'),
    ('structure_trend_bearish', -10, 'Score adjustment when top-down (monthly+weekly) structure trend is bearish'),
    ('structure_choch_penalty', -10, 'Score adjustment when a change-of-character warning is active on daily or weekly structure'),
    ('structure_bos_bonus', 5, 'Score adjustment when a break-of-structure confirmation is active on daily or weekly structure')
ON CONFLICT (key) DO NOTHING;

-- Price Structure epic PR A (see shared/market_structure.py). Persistent
-- swing/zone tables, replacing the capped-to-5 JSONB nesting inside
-- market_structure_history.component_values as the queryable source of
-- truth for individual swing points and support/resistance zones.
-- market_structure_history itself is untouched -- it stays the daily
-- trend/BOS/CHoCH snapshot table it already was.
--
-- event_time is the swing's own bar date; confirmation_time is the
-- earliest date the swing was actually knowable (event_time + SWING_K
-- confirming bars). Every as-of-safe consumer must filter on
-- confirmation_time, never event_time -- see detect_swings'/
-- classify_timeframe_structure's docstrings in market_structure.py.
CREATE TABLE IF NOT EXISTS structural_swings (
    id                    BIGSERIAL PRIMARY KEY,
    symbol                TEXT NOT NULL,
    timeframe             TEXT NOT NULL,
    swing_type            TEXT NOT NULL,
    event_time            DATE NOT NULL,
    confirmation_time     DATE NOT NULL,
    price                 NUMERIC NOT NULL,
    calculation_version   INTEGER NOT NULL DEFAULT 1,
    computed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, timeframe, swing_type, event_time)
);

CREATE INDEX IF NOT EXISTS idx_structural_swings_symbol_tf
    ON structural_swings (symbol, timeframe, confirmation_time);

-- Persistent support/resistance zone identity: unlike market_structure.py's
-- _cluster_zones() (a fresh greedy re-cluster every call), a zone here is
-- an ongoing entity -- created once, then matched and updated (touch_count,
-- last_touched, refined center_price) on every subsequent confirmed swing
-- that falls within its tolerance, rather than being torn down and rebuilt
-- daily. `active` lets a zone be retired (e.g. superseded by a tighter
-- cluster) without deleting its history.
CREATE TABLE IF NOT EXISTS structural_zones (
    id                    BIGSERIAL PRIMARY KEY,
    symbol                TEXT NOT NULL,
    timeframe             TEXT NOT NULL,
    zone_type             TEXT NOT NULL,
    center_price          NUMERIC NOT NULL,
    upper                 NUMERIC NOT NULL,
    lower                 NUMERIC NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    touch_count           INTEGER NOT NULL DEFAULT 1,
    last_touched          DATE NOT NULL,
    active                BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_structural_zones_symbol_tf
    ON structural_zones (symbol, timeframe, active);

-- Price Structure epic PR B (see shared/structural_events.py). Append-only
-- event log for price/zone interactions (breakout/breakdown/acceptance/
-- rejection/failed_breakout/failed_breakdown/sweep_reclaim) and swing-
-- anchored structure-failure warnings (possible_bullish_structure_failure/
-- possible_bearish_structure_failure). Never rewritten: a later
-- reclassification (e.g. a confirmed breakout that fails) is always a NEW
-- row referencing the earlier one via reference_type='event', not an
-- UPDATE -- the log represents the sequence of facts as they became known,
-- not today's final interpretation of old bars.
--
-- reference_type/reference_id is a typed nullable reference rather than a
-- single FK, since events anchor to different tables depending on type:
-- 'zone' -> structural_zones.id (breakout/breakdown/acceptance/rejection/
-- failed_breakout/failed_breakdown/sweep_reclaim), 'swing' ->
-- structural_swings.id (possible_*_structure_failure), 'event' ->
-- structural_events.id (acceptance/failed_breakout/failed_breakdown,
-- which reference the original breakout/breakdown event they derive from,
-- not the zone directly).
--
-- calculation_version + metadata.params together let a later parameter
-- change (bar-count thresholds) be reconstructed per-event rather than
-- silently reinterpreted -- see EVENT_DETECTION_PARAMS in
-- shared/structural_events.py.
CREATE TABLE IF NOT EXISTS structural_events (
    id                    BIGSERIAL PRIMARY KEY,
    symbol                TEXT NOT NULL,
    timeframe             TEXT NOT NULL,
    event_type            TEXT NOT NULL,
    reference_type        TEXT NOT NULL,
    reference_id          BIGINT NOT NULL,
    event_time            DATE NOT NULL,
    confirmation_time     DATE NOT NULL,
    metadata              JSONB,
    calculation_version   INTEGER NOT NULL DEFAULT 1,
    computed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, timeframe, event_type, reference_type, reference_id, event_time)
);

CREATE INDEX IF NOT EXISTS idx_structural_events_symbol_tf
    ON structural_events (symbol, timeframe, confirmation_time);

-- Price Structure epic PR B2 (see shared/fair_value_gaps.py). Fair Value
-- Gap identity, IMMUTABLE once created -- same "identity table separate
-- from its append-only event log" split PR A/B established for swings/
-- zones/events. A gap's zone bounds and creation date never change after
-- insertion; every later fill-status milestone (partially_entered/
-- midpoint_reached/fully_filled/invalidated/expired) is instead logged as
-- a new structural_events row (reference_type='fvg', reference_id ->
-- this table's id), reusing PR B's append-only infrastructure rather than
-- a second parallel mutable-status system.
--
-- A 3-candle pattern (A, B, C oldest to newest): bullish when
-- high(A) < low(C) (strict -- an exact touch is not a gap), zone =
-- [high(A), low(C)]; bearish when low(A) > high(C), zone =
-- [high(C), low(A)]. confirmation_time = event_time = candle C's own
-- date -- unlike a swing, an FVG needs no bars beyond its own 3rd candle
-- to be confirmed, so there is no separate confirmation delay here.
CREATE TABLE IF NOT EXISTS fair_value_gaps (
    id                    BIGSERIAL PRIMARY KEY,
    symbol                TEXT NOT NULL,
    timeframe             TEXT NOT NULL,
    gap_type              TEXT NOT NULL,
    zone_upper            NUMERIC NOT NULL,
    zone_lower            NUMERIC NOT NULL,
    event_time            DATE NOT NULL,
    confirmation_time     DATE NOT NULL,
    calculation_version   INTEGER NOT NULL DEFAULT 1,
    computed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, timeframe, gap_type, event_time)
);

CREATE INDEX IF NOT EXISTS idx_fair_value_gaps_symbol_tf
    ON fair_value_gaps (symbol, timeframe, confirmation_time);

-- Relative-strength risk eligibility filter (see shared/relative_strength_risk.py).
-- RISK CONTROL ONLY, never a score adjustment -- backed by backtest_results
-- experiment_ids 009/010/011: stock-vs-sector underperformance is
-- associated with a materially higher stop-out rate and worse max
-- drawdown, replicated across an independent window resample and an
-- alternate relative-strength definition. Total return/Sharpe/Sortino
-- improvements from gating did NOT replicate -- do not describe this
-- feature as a return/alpha enhancement anywhere (dashboard, docs, PRs).
-- Disabled by default: existing proposal generation/gating behavior is
-- unchanged until a human flips relative_strength_risk_mode to 1 (gate)
-- via PATCH /api/signal-params/relative_strength_risk_mode.
INSERT INTO signal_params (key, value, description) VALUES
    ('relative_strength_risk_mode', 0, 'Relative-strength risk eligibility filter for mean_reversion buys: 0=off, 1=gate (reject underperforming_sector buys), 2=size_reduce (reserved, not implemented -- currently a no-op). Risk control only, never a score adjustment.'),
    ('relative_strength_risk_size_reduce_pct', 0.5, 'Reserved for relative_strength_risk_mode=2 (size_reduce), not implemented yet')
ON CONFLICT (key) DO NOTHING;

-- Trade Thesis Schema, PR 1 of the Hypothesis-Driven Trading Architecture
-- epic -- see docs/trade-thesis-architecture-reconciliation.md for the full
-- design (§1/§1a/§1b/§7 in particular). `theses`/`thesis_id` above is a
-- STRATEGY FAMILY registry (mean_reversion, congress_shreve_hern) and is
-- UNCHANGED by this table. `trade_theses` is a new, separate object one
-- level down: one falsifiable hypothesis for one specific opportunity,
-- referencing which strategy family produced it via thesis_id (FK, still
-- meaning what it always meant).
--
-- This PR ships the grammar only (shared/trade_thesis.py defines and
-- validates entry_conditions/invalidation_spec/success_spec's JSON shape
-- and evidence_context's provider envelope) -- it does NOT check that a
-- referenced feature identifier exists (PR 2/3's job) and nothing in the
-- live signal-generation path creates trade_theses rows yet (PR 4's job).
-- The table can be dark -- computed, tested, persisted, not load-bearing --
-- same staged-rollout discipline shared/market_structure.py already uses.
CREATE TABLE IF NOT EXISTS trade_theses (
    id                  BIGSERIAL PRIMARY KEY,
    thesis_id           BIGINT NOT NULL REFERENCES theses(id),  -- strategy family (existing meaning)
    symbol              TEXT NOT NULL,
    schema_version      TEXT NOT NULL,   -- Trade Thesis Schema (grammar) version, see shared/trade_thesis.py
    evidence_context    JSONB NOT NULL,  -- {as_of, providers: {...}} multi-provider evidence lineage, see §1b
    hypothesis_type     TEXT NOT NULL,   -- constrained vocabulary, shared/trade_thesis.py
    hypothesis_text     TEXT NOT NULL,   -- human-readable
    entry_conditions    JSONB NOT NULL,  -- grammar defined here, semantically validated PR 3
    evidence_snapshot   JSONB NOT NULL,  -- {supporting: [...], contradictory: [...], missing: [...]}, PR 4 output, frozen at creation
    invalidation_spec   JSONB NOT NULL,  -- grammar defined here, semantically validated PR 3, consumed PR 5/6
    success_spec        JSONB NOT NULL,  -- grammar defined here, semantically validated PR 3
    confidence          NUMERIC CHECK (confidence BETWEEN 0 AND 1),
    provenance          JSONB NOT NULL,  -- explicit/inferred/proposed per field (PR 17); trivially all-'explicit' through Phase 4
    status              TEXT NOT NULL DEFAULT 'proposed'
                        CHECK (status IN ('proposed','active','weakening','invalidated','completed','superseded')),
    as_of               TIMESTAMPTZ NOT NULL,  -- point-in-time evidence was evaluated
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trade_theses_symbol ON trade_theses (symbol);
CREATE INDEX IF NOT EXISTS idx_trade_theses_thesis_id ON trade_theses (thesis_id);
CREATE INDEX IF NOT EXISTS idx_trade_theses_status ON trade_theses (status);

-- trade_thesis_id threading (§2): nullable, unpopulated, no writer wired in
-- this PR -- these columns are dark until PR 4 (trade_proposals, generated
-- for a specific trade_theses row) and PR 9 (trades, copied immutably from
-- trade_proposals at insert, same pattern as trades.initial_stop_price)
-- start setting them. signals and position_lifecycles deliberately do NOT
-- get this column in this PR -- see §2's table for why.
ALTER TABLE trade_proposals ADD COLUMN IF NOT EXISTS trade_thesis_id BIGINT REFERENCES trade_theses(id);
ALTER TABLE trades          ADD COLUMN IF NOT EXISTS trade_thesis_id BIGINT REFERENCES trade_theses(id);

-- Evidence Evaluation Engine (PR 4, shared/trade_thesis_engine.py) -- the
-- master switch for trade-thesis instantiation on the live BUY path.
-- Disabled by default: existing compute_signals() BUY behavior is
-- byte-for-byte unchanged (trade_proposals.trade_thesis_id stays NULL)
-- until a human flips this to 1 via PATCH /api/signal-params/
-- trade_thesis_instantiation_enabled, same precedent as
-- structure_scoring_enabled/relative_strength_risk_mode above.
INSERT INTO signal_params (key, value, description) VALUES
    ('trade_thesis_instantiation_enabled', 0, 'Master switch for creating a trade_theses row alongside a qualifying BUY trade_proposals row (0=off, matches pre-PR-4 behavior)')
ON CONFLICT (key) DO NOTHING;

-- Structure-Aware Stop Resolver (PR 6, shared/trade_thesis_stop_resolver.py)
-- -- derives planned_initial_stop_price from the Market Structure Engine's
-- already-persisted nearest support zone instead of a flat percentage,
-- when a sane one exists. Disabled by default: existing BUY proposal
-- planned_initial_stop_price is byte-for-byte unchanged until a human
-- flips structure_aware_stop_enabled to 1, same precedent as
-- structure_scoring_enabled/trade_thesis_instantiation_enabled above.
INSERT INTO signal_params (key, value, description) VALUES
    ('structure_aware_stop_enabled', 0, 'Master switch for deriving planned_initial_stop_price from Market Structure Engine support zones instead of a flat percentage (0=off, matches pre-PR-6 behavior)'),
    ('max_structure_stop_multiple', 2.5, 'Sanity cap: a structure support zone more than this many times farther from price than the plain percentage stop falls back to the percentage stop instead')
ON CONFLICT (key) DO NOTHING;

-- Exit Taxonomy (PR 8, shared/signals.py::check_thesis_invalidation_sell)
-- -- master switch for proposing sells on trade-thesis invalidation
-- (exit_reason='thesis_invalidated'), distinct from trade_thesis_instantiation_enabled
-- (PR 4): turning instantiation on alone stays purely observational --
-- creating trade_theses rows for audit -- until this flag is ALSO
-- explicitly turned on. Disabled by default, same precedent as every
-- other flag above.
INSERT INTO signal_params (key, value, description) VALUES
    ('thesis_invalidation_exit_enabled', 0, 'Master switch for proposing sells when a held position''s linked trade_thesis looks invalidated (structure CHoCH, regime deterioration, or its own invalidation_spec). 0=off, matches pre-PR-8 behavior. Independent of trade_thesis_instantiation_enabled.')
ON CONFLICT (key) DO NOTHING;

-- Thesis Snapshot on Proposal/Position (PR 9, Hypothesis-Driven Trading
-- Architecture epic) -- per docs/trade-thesis-architecture-reconciliation.md
-- §5's PR 9 bullet: trade_thesis_id threads through immutably the same
-- way initial_stop_price already does. trade_proposals.trade_thesis_id
-- and trades.trade_thesis_id already exist (PR 1); this closes the last
-- gap -- api/main.py now copies trades.trade_thesis_id down from
-- trade_proposals.trade_thesis_id at fill time (mirroring thesis_id/
-- initial_stop_price's existing copy-down), and position_lifecycles gets
-- its own trade_thesis_id, derived by shared/position_lifecycles.py's
-- _flush() the same way it already collapses thesis_id across a
-- lifecycle's constituent trades -- see that function's own comment for
-- why the None-filtering differs from thesis_id's collapse.
ALTER TABLE position_lifecycles ADD COLUMN IF NOT EXISTS trade_thesis_id BIGINT REFERENCES trade_theses(id);

-- Live Thesis Re-Evaluation (PR 10, Hypothesis-Driven Trading Architecture
-- epic) -- per docs/trade-thesis-architecture-reconciliation.md §4/§5's
-- PR 10 bullet. Append-only, same "recompute-from-history-and-write-one-
-- summary-row" discipline ingest/postmortem.py already established --
-- never an UPDATE against a past evaluation. trade_theses.status (the one
-- field PR 1's §4 immutability contract explicitly excludes) becomes a
-- denormalized read of this table's most recent row per trade_thesis_id,
-- refreshed by shared/trade_thesis_reevaluation.py -- not mutated
-- directly by anything else.
CREATE TABLE IF NOT EXISTS trade_thesis_evaluations (
    id                    BIGSERIAL PRIMARY KEY,
    trade_thesis_id       BIGINT NOT NULL REFERENCES trade_theses(id),
    evaluated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    previous_state        TEXT,             -- trade_theses.status before this evaluation; NULL on the first evaluation
    state                 TEXT NOT NULL     -- reuses trade_thesis.STATUSES 1:1 (PR 1 §6 deferred this to PR 10 --
                          CHECK (state IN ('proposed','active','weakening','invalidated','completed','superseded')),
    evidence_diff         JSONB NOT NULL,   -- what this evaluation actually found -- invalidation reasons, success_spec result
    triggering_condition  TEXT              -- human-readable reason for a state change; NULL when state == previous_state
);

CREATE INDEX IF NOT EXISTS idx_trade_thesis_evaluations_trade_thesis_id
    ON trade_thesis_evaluations (trade_thesis_id, evaluated_at DESC);

-- Hypothesis Library, PR 13 of the Hypothesis-Driven Trading Architecture
-- epic. A catalog of hypothesis_type *templates* -- distinct from both
-- `theses` (strategy-family registry: mean_reversion, congress_shreve_hern)
-- and `trade_theses` (one falsifiable hypothesis per opportunity). Neither
-- existing table is touched or FK'd here: a hypothesis type isn't owned by
-- one strategy family, and trade_theses.hypothesis_type stays plain TEXT --
-- shared/trade_thesis.py's grammar layer deliberately doesn't take a live
-- conn, so a DB FK there would force every TradeThesis() construction site
-- to open one. Membership is enforced instead by
-- shared/hypothesis_library.py::is_legal_hypothesis_type()/
-- is_instantiable_hypothesis_type(), called from
-- shared/trade_thesis_validator.py (PR 3) -- see that module for why
-- vocabulary enforcement lives one layer above pure grammar.
--
-- `status` here is catalog-entry lifecycle (active/experimental/deprecated)
-- -- a deliberately different vocabulary from trade_theses.STATUSES
-- (proposed/active/weakening/invalidated/completed/superseded), and NOT a
-- strategy-promotion gate. This axis only answers "is this hypothesis
-- template available for use" -- it never answers "has an implementation of
-- this idea demonstrated a tradable edge," which is the separately-scoped
-- Strategy Incubator epic's job. Conceptual hierarchy this table sits at
-- the bottom of: HypothesisType -> ResearchExperiment -> StrategyVersion ->
-- TradeThesis -> TradeProposal -> Order/Position. PR13 implements only the
-- HypothesisType layer; it must not grow strategy lifecycle, versioning,
-- scoring, or promotion concepts of its own -- those belong one or more
-- layers up.
--
-- Instantiability (shared/hypothesis_library.py::is_instantiable_
-- hypothesis_type()) requires status='active' (or 'experimental' too, for
-- allow_experimental=True research-tooling callers only -- never the live
-- path) AND schema_version == trade_thesis.TRADE_THESIS_SCHEMA_VERSION,
-- checked in Python since Postgres can't reach into the Python constant.
-- register_hypothesis_type() always stamps schema_version from that
-- constant itself, not from caller input -- a catalog entry can never claim
-- conformance to a grammar version it wasn't actually written against. This
-- means an old template automatically stops being live-instantiable the
-- moment TRADE_THESIS_SCHEMA_VERSION bumps, without needing every row
-- touched -- the row stays catalog-visible (list/get) for history, it just
-- fails the instantiability check.
--
-- `version` is a simple counter bumped on update (not full append-only
-- history -- that would start to resemble the separately-tracked Strategy
-- Incubator epic's strategy_versions concept at a different level of
-- abstraction). hypothesis_type_changes below captures a minimal
-- before/after snapshot per change so a mutated template doesn't silently
-- lose what an already-instantiated trade_theses row's hypothesis_type
-- meant at the time -- see that table's comment.
CREATE TABLE IF NOT EXISTS hypothesis_types (
    id                        BIGSERIAL PRIMARY KEY,
    type_key                  TEXT NOT NULL UNIQUE,   -- matches trade_theses.hypothesis_type values, e.g. 'mean_reversion_oversold'
    display_name              TEXT NOT NULL,
    description                TEXT NOT NULL,
    category                  TEXT,                    -- loose grouping only, not an FK
    schema_version             TEXT NOT NULL,           -- stamped from trade_thesis.TRADE_THESIS_SCHEMA_VERSION at write time, not caller-supplied; enforced (not just informational) by is_instantiable_hypothesis_type()
    required_providers        JSONB NOT NULL DEFAULT '[]',  -- list of feature_registry.PROVIDERS ids; validated in Python at write time, not a DB constraint
    default_entry_conditions  JSONB,                   -- optional condition-tree template (PR 14 seed material), same grammar as trade_theses.entry_conditions
    default_invalidation_spec JSONB,
    default_success_spec      JSONB,
    status                    TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'experimental', 'deprecated')),
    version                   INTEGER NOT NULL DEFAULT 1,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hypothesis_types_status ON hypothesis_types (status);

-- Minimal change history for hypothesis_types -- NOT the Strategy
-- Incubator's strategy_versions concept, deliberately lighter-weight: one
-- row per update() call capturing a before/after snapshot of the full spec,
-- so "what did mean_reversion_oversold mean when trade_theses row #4821 was
-- instantiated against it" stays answerable even though hypothesis_types
-- itself is mutated in place. previous_value/new_value store the complete
-- HypothesisTypeSpec dict, not a per-field diff -- simplest thing that
-- preserves provenance without inventing a second versioning system.
CREATE TABLE IF NOT EXISTS hypothesis_type_changes (
    id             BIGSERIAL PRIMARY KEY,
    type_key       TEXT NOT NULL,
    version        INTEGER NOT NULL,  -- hypothesis_types.version AFTER this change was applied
    changed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    changed_by     TEXT,              -- NULL if unattributed (no caller-identity plumbing exists yet at this layer)
    previous_value JSONB NOT NULL,
    new_value      JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hypothesis_type_changes_type_key
    ON hypothesis_type_changes (type_key, changed_at DESC);

-- Seed with today's live HYPOTHESIS_TYPES values so the catalog starts in
-- lockstep -- this migration alone makes zero new hypothesis_type legal.
-- mean_reversion_oversold's default_entry_conditions matches the exact
-- condition tree shared/trade_thesis_engine.py::_build_buy_trade_thesis()
-- already builds live (PR 4); its description is worded to match the OR
-- (either condition fires the entry), not an AND, since that's what the
-- condition tree actually encodes.
INSERT INTO hypothesis_types
    (type_key, display_name, description, category, schema_version, required_providers,
     default_entry_conditions, status)
VALUES
    ('mean_reversion_oversold', 'Mean Reversion — Oversold Bounce',
     'Oversold mean-reversion setup triggered by either low RSI or price near/below the lower Bollinger band, expecting reversion toward the mean.',
     'mean_reversion', 'v1', '["technical"]'::jsonb,
     '{"or": [{"feature": "technical.rsi_14", "op": "lt", "value": 30}, {"feature": "technical.bb_pct_b", "op": "lte", "value": 0.1}]}'::jsonb,
     'active'),
    ('mean_reversion_overbought', 'Mean Reversion — Overbought Fade',
     'RSI/BB overbought entry: price near/above upper Bollinger band with high RSI, expecting reversion toward the mean.',
     'mean_reversion', 'v1', '["technical"]'::jsonb,
     '{"and": [{"feature": "technical.rsi_14", "op": "gt", "value": 70}, {"feature": "technical.bb_pct_b", "op": "gte", "value": 0.9}]}'::jsonb,
     'active')
ON CONFLICT (type_key) DO NOTHING;

-- Hypothesis Candidate Generation (PR 14). Sits between hypothesis_types
-- (PR 13's catalog of templates) and the separately-scoped, not-yet-built
-- Strategy Incubator epic's ResearchExperiment/StrategyVersion concepts --
-- deliberately NOT named research_experiments/strategy_versions so this
-- doesn't squat on that epic's future schema; a later integration PR can
-- migrate rows OUT of these tables into the real Incubator schema once
-- that epic starts, without a naming collision.
--
-- candidate_batches: one row per generation call (one hypothesis_type, one
-- point in time, one parameter-variation spec). hypothesis_type_version is
-- copied from hypothesis_types.version AT GENERATION TIME and never
-- re-derived -- if the catalog entry is edited afterward (bumping its
-- version), already-generated batches keep pointing at the version they
-- were actually generated against, same immutable-provenance contract
-- hypothesis_library.py's docstring establishes for this PR.
CREATE TABLE IF NOT EXISTS candidate_batches (
    id                      BIGSERIAL PRIMARY KEY,
    hypothesis_type         TEXT NOT NULL,     -- hypothesis_types.type_key, not FK'd -- point-in-time
                                                -- copy, not a live join, same reasoning trade_theses.
                                                -- hypothesis_type stays plain TEXT
    hypothesis_type_version INTEGER NOT NULL,  -- hypothesis_types.version AT GENERATION TIME, frozen
    schema_version          TEXT NOT NULL,     -- trade_thesis.TRADE_THESIS_SCHEMA_VERSION at generation time
    parameter_spec          JSONB NOT NULL,    -- caller's input, e.g. {"technical.rsi_14": [20, 25, 30]}
    generated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    generated_by            TEXT                -- NULL if unattributed, same convention as hypothesis_type_changes.changed_by
);

CREATE INDEX IF NOT EXISTS idx_candidate_batches_hypothesis_type ON candidate_batches (hypothesis_type);

-- candidates: one concrete, fully-substituted condition-tree set per
-- parameter combination in a batch's Cartesian product. Not executable --
-- no backtest_results linkage, no scoring/promotion status. Generation +
-- persistence + provenance only, per this PR's scope.
CREATE TABLE IF NOT EXISTS candidates (
    id                 BIGSERIAL PRIMARY KEY,
    batch_id           BIGINT NOT NULL REFERENCES candidate_batches(id),
    parameter_values   JSONB NOT NULL,  -- e.g. {"technical.rsi_14": 25}, one point from the batch's Cartesian product
    entry_conditions   JSONB NOT NULL,  -- substituted, re-validated via trade_thesis.validate_condition_tree()
    invalidation_spec  JSONB,           -- NULL if the template's default_invalidation_spec was NULL
    success_spec       JSONB,           -- NULL if the template's default_success_spec was NULL
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_candidates_batch_id ON candidates (batch_id);

-- Additional hypothesis_types seed rows, PR 16 (Bollinger Breakout
-- Continuation + EMA Crossover Trend strategies, built on PR 15's
-- shared/backtest_engine.py). Metadata/catalog registration only, per
-- PR 13's design -- the actual executable strategy logic lives in
-- shared/bollinger_breakout_strategy.py / shared/ema_crossover_strategy.py,
-- not here.
--
-- bollinger_breakout_continuation: "close > upper_band" is exactly
-- technical.bb_pct_b > 1.0 (same architectural reuse PR 13's original
-- mean_reversion rows already established), so entry_conditions needs no
-- new registered feature. default_invalidation_spec (not success_spec)
-- encodes "close < lower_band" -- matching trade_thesis_engine.py's own
-- precedent that invalidation_spec is "the thesis went wrong," which a
-- failed continuation reverting below the band is, not a success.
--
-- ema_crossover_trend: default_entry_conditions is deliberately NULL. The
-- condition-tree grammar (shared/trade_thesis.py) only expresses "one
-- feature vs. a scalar" -- it has no primitive for "feature A vs. feature
-- B" or "value at t vs. value at t-1", so a crossover cannot be honestly
-- expressed in it. Leaving this NULL (rather than registering a
-- misleading approximate condition) means
-- shared/hypothesis_candidates.py::generate_candidates() correctly
-- refuses to generate candidates for it (its own "no template" rejection,
-- PR 14) -- an accurate reflection of a real grammar limitation, not a
-- bug to work around here.
INSERT INTO hypothesis_types
    (type_key, display_name, description, category, schema_version, required_providers,
     default_entry_conditions, default_invalidation_spec, status)
VALUES
    ('bollinger_breakout_continuation', 'Bollinger Breakout Continuation',
     'A close above the upper Bollinger Band indicates volatility expansion and positive momentum more likely to continue than immediately revert. Exits on a close back below the lower band.',
     'trend_following', 'v1', '["technical"]'::jsonb,
     '{"feature": "technical.bb_pct_b", "op": "gt", "value": 1.0}'::jsonb,
     '{"feature": "technical.bb_pct_b", "op": "lt", "value": 0.0}'::jsonb,
     'active'),
    ('ema_crossover_trend', 'EMA Crossover Trend',
     'A faster moving average (default EMA 20) crossing above a slower moving average (default EMA 21) indicates positive trend persistence. Exits on the mirrored crossunder. Entry/exit expressed as a two-EMA crossover, which the current condition-tree grammar cannot represent as a single-feature-vs-scalar template -- see shared/ema_crossover_strategy.py for the actual executable logic.',
     'trend_following', 'v1', '["technical"]'::jsonb,
     NULL, NULL,
     'active')
ON CONFLICT (type_key) DO NOTHING;

-- Volume & Volume Profile epic, PR A: provenance tracking. price_history
-- has always been an untracked blend of two sources -- the dominant,
-- recurring Yahoo Finance scrape (ingest.py::ingest_prices) and a
-- secondary, manually-run Alpaca IEX-feed backfill
-- (backfill_alpaca.py::store_bars) that deliberately writes into the SAME
-- table/rows via ON CONFLICT DO NOTHING. Neither path was ever marked.
-- Existing rows honestly default to 'unknown' -- there is no way to
-- retroactively determine which source wrote them, so this migration does
-- not guess/reclassify. Going forward, ingest.py/backfill_alpaca.py tag
-- every new row with its real source ('yahoo' | 'alpaca_iex'). Once a row
-- exists, its source is immutable -- neither INSERT's ON CONFLICT clause
-- touches this column, matching the table's existing implicit
-- first-write-wins convention. price_history_hourly gets the same column
-- for consistency even though nothing reads that table yet (schema.sql's
-- own note: "nothing reads this yet") -- cheap to add now, avoids a
-- second migration later.
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE price_history_hourly ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'unknown';

-- Portfolio stop-loss (shared/signals.py::check_portfolio_loss_sell) --
-- account-wide cumulative-loss stop, measured the same way
-- check_stop_losses measures a single position (% of cost basis, i.e.
-- loss from purchase price), NOT drawdown from a trailing high-water mark
-- like circuit_breaker_drawdown_pct. That keeps the two account-wide
-- thresholds independently interpretable: circuit_breaker_drawdown_pct
-- only pauses new BUYs and never sells anything; this one proposes
-- exiting whichever currently-held positions are actually underwater
-- once the combined book's loss crosses the threshold, rather than
-- trimming the whole book (including positions still in profit)
-- uniformly.
INSERT INTO signal_params (key, value, description) VALUES
    ('portfolio_stop_loss_pct', 0.05, 'If the combined book''s unrealized loss exceeds this fraction of total cost basis (5%), propose selling every position currently showing an unrealized loss (not the whole book). Independent of circuit_breaker_drawdown_pct, which only pauses new buys and never sells.')
ON CONFLICT (key) DO NOTHING;

-- Default revised from 0.12 to 0.05, same day. At 0.12 -- ABOVE the 8%
-- per-symbol stop_loss_pct -- this check could almost never fire before
-- individual stops already had: absent a gap-down past a check, the
-- cost-basis-weighted loss across the book is bounded by its single
-- worst-held position's own loss, which check_stop_losses already exits
-- once it crosses stop_loss_pct. Set BELOW stop_loss_pct, it becomes a
-- genuine backstop -- it can catch a broad, correlated decline across
-- many positions each still individually under their own stop, which the
-- per-symbol check structurally cannot see. Guarded to only touch
-- installs still sitting on the original seeded value -- never overwrites
-- a value someone has since tuned via the Settings UI.
UPDATE signal_params SET value = 0.05 WHERE key = 'portfolio_stop_loss_pct' AND value = 0.12;

-- Volatility Forecasting & Risk-Targeted Position Sizing epic, VR-1 (see
-- docs/volatility-sizing-vr0-reconciliation.md). One row per
-- (symbol, as_of, horizon, estimator) -- unlike market_structure_history's
-- one-row-per-symbol-per-day, multiple estimators/horizons can coexist for
-- the same symbol+day (realized_vol and ewma today; garch_1_1 in VR-4,
-- implied_vol in a later phase, all sharing this same table per the
-- reconciliation doc's coexistence design). daily_vol is the canonical
-- native unit; annualized_vol/horizon_vol/expected_move_dollars are always
-- DERIVED from it by shared/volatility_forecast.py, never computed ad hoc
-- per estimator -- see that module's docstring. Not read by any live path
-- yet: shared/risk_engine.py does not consume this table until VR-2.
CREATE TABLE IF NOT EXISTS volatility_forecast_history (
    id                     BIGSERIAL PRIMARY KEY,
    symbol                 TEXT NOT NULL,
    timeframe              TEXT NOT NULL,
    as_of                  DATE NOT NULL,
    available_at           TIMESTAMPTZ NOT NULL,
    horizon                TEXT NOT NULL,
    estimator              TEXT NOT NULL,
    calculation_version    INTEGER NOT NULL DEFAULT 1,
    input_cutoff           DATE,
    daily_vol              NUMERIC,
    annualized_vol         NUMERIC,
    horizon_vol            NUMERIC,
    expected_move_dollars  NUMERIC,
    percentile             NUMERIC,
    regime                 TEXT,
    observation_count      INTEGER NOT NULL DEFAULT 0,
    status                 TEXT NOT NULL CHECK (status IN ('ok', 'insufficient_history', 'stale', 'zero_or_nonfinite_input', 'estimator_failed')),
    fit_metadata           JSONB,
    computed_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, as_of, horizon, estimator)
);

CREATE INDEX IF NOT EXISTS idx_volatility_forecast_history_symbol ON volatility_forecast_history (symbol);
CREATE INDEX IF NOT EXISTS idx_volatility_forecast_history_estimator ON volatility_forecast_history (estimator);

-- Volatility forecast config -- flat signal_params rows, same pattern as
-- every other estimator/scoring module in this codebase. No
-- "*_enabled" switch here yet: VR-1 computes and persists forecasts but
-- nothing reads them for sizing/gating (that gate is VR-2's
-- volatility_sizing_enabled, added when the risk-engine candidate exists
-- to actually toggle).
INSERT INTO signal_params (key, value, description) VALUES
    ('volatility_realized_vol_window', 20, 'Trailing return-observation window for the realized_vol estimator (VR-1)'),
    ('volatility_ewma_lambda', 0.94, 'RiskMetrics-style decay factor for the ewma volatility estimator (VR-1)'),
    ('volatility_ewma_min_periods', 30, 'Minimum trailing returns before the ewma estimator seeds its recurrence (VR-1)'),
    ('volatility_percentile_lookback', 100, 'Trailing observation count used to rank a volatility estimate into a percentile/regime bucket (VR-1)'),
    ('volatility_sessions_per_year', 252, 'Equity trading-sessions-per-year convention used to annualize daily_vol (VR-1)'),
    ('volatility_stale_after_days', 5, 'A forecast whose input_cutoff is more than this many days before as_of is marked status=stale rather than ok (VR-1)')
ON CONFLICT (key) DO NOTHING;

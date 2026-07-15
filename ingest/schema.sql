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

-- exit_reason classifies sell proposals by which rule triggered them:
-- thesis_complete | time_stop | stop_loss | overbought | regime_deterioration | manual
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

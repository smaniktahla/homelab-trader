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

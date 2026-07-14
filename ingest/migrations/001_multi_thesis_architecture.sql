-- 001_multi_thesis_architecture.sql
-- homelab-trader / invest DB (postgresql://invest@10.10.10.201:5432/invest)
--
-- Makes "thesis" a first-class concept so signals/trades/trades/outcomes can be
-- attributed to whichever strategy generated them (mean_reversion,
-- congress_shreve_hern, and anything added later) instead of one implicit
-- global strategy.
--
-- Verified against live schema on ubuntu-box (2026-07-13):
--   signals(id, symbol, signal_type, score, rationale, generated_at, acted_on)
--   trade_proposals(id, symbol, side, qty, rationale, signal_score, proposed_at,
--                    decided_at, decision, decided_by, rejection_reason, exit_reason)
--   trades(id, symbol, side, qty, price, notional, order_id, traded_at, notes,
--          source, status, proposal_id, cost)
--   signal_params(key, value, description)
--   signal_outcomes(id, signal_id, symbol, side, generated_at, score, rsi,
--                    bb_upper, bb_middle, bb_lower, band_std, market_regime,
--                    symbol_regime, price_at_signal, proposal_id, proposal_status,
--                    block_reason, approval_status, rejection_reason,
--                    forward_return_1d/5d/10d/20d, mae, mfe, outcome_updated_at)
--
-- trade_proposals.side already exists ("buy"/"sell") -- no signal_score=99 hack
-- needed to distinguish stop-loss exits from thesis-driven proposals.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Thesis registry
-- ---------------------------------------------------------------------------

CREATE TABLE theses (
    id           bigserial PRIMARY KEY,
    slug         text NOT NULL UNIQUE,          -- 'mean_reversion', 'congress_shreve_hern'
    display_name text NOT NULL,                 -- 'RSI/BB Mean Reversion', 'Congressional Shadow — Shreve/Hern'
    status       text NOT NULL DEFAULT 'active' -- active / paused / backtesting_only
                 CHECK (status IN ('active', 'paused', 'backtesting_only')),
    config       jsonb NOT NULL DEFAULT '{}',   -- strategy-specific params
    created_at   timestamptz NOT NULL DEFAULT now()
);

INSERT INTO theses (slug, display_name, status, config) VALUES
    ('mean_reversion', 'RSI/BB Mean Reversion', 'active', '{}'),
    ('congress_shreve_hern', 'Congressional Shadow — Shreve/Hern', 'backtesting_only', '{
        "members": ["Jefferson Shreve", "Kevin Hern"],
        "mirror_sells": false,
        "trade_allocation_pct": null
    }'::jsonb);

-- Migrate existing signal_params rows into mean_reversion's config so there is
-- one source of truth going forward. signal_params itself is left in place --
-- application code still reads it until that read path is repointed at
-- theses.config (tracked as a post-migration TODO below, not done here).
UPDATE theses
SET config = config || (
    SELECT jsonb_object_agg(key, value)
    FROM signal_params
)
WHERE slug = 'mean_reversion'
  AND EXISTS (SELECT 1 FROM signal_params);

-- ---------------------------------------------------------------------------
-- 2. thesis_id on shared tables, backfilled to mean_reversion (today's only
--    strategy) so nothing existing becomes orphaned.
-- ---------------------------------------------------------------------------

ALTER TABLE signals          ADD COLUMN thesis_id bigint REFERENCES theses(id);
ALTER TABLE trade_proposals  ADD COLUMN thesis_id bigint REFERENCES theses(id);
ALTER TABLE trades           ADD COLUMN thesis_id bigint REFERENCES theses(id);
ALTER TABLE signal_outcomes  ADD COLUMN thesis_id bigint REFERENCES theses(id);

UPDATE signals         SET thesis_id = (SELECT id FROM theses WHERE slug = 'mean_reversion');
UPDATE trade_proposals SET thesis_id = (SELECT id FROM theses WHERE slug = 'mean_reversion');
UPDATE trades          SET thesis_id = (SELECT id FROM theses WHERE slug = 'mean_reversion');
UPDATE signal_outcomes SET thesis_id = (SELECT id FROM theses WHERE slug = 'mean_reversion');

ALTER TABLE signals          ALTER COLUMN thesis_id SET NOT NULL;
ALTER TABLE trade_proposals  ALTER COLUMN thesis_id SET NOT NULL;
ALTER TABLE trades           ALTER COLUMN thesis_id SET NOT NULL;
ALTER TABLE signal_outcomes  ALTER COLUMN thesis_id SET NOT NULL;

CREATE INDEX idx_signals_thesis_id         ON signals (thesis_id);
CREATE INDEX idx_trade_proposals_thesis_id ON trade_proposals (thesis_id);
CREATE INDEX idx_trades_thesis_id          ON trades (thesis_id);
CREATE INDEX idx_signal_outcomes_thesis_id ON signal_outcomes (thesis_id);

-- ---------------------------------------------------------------------------
-- 3. details jsonb -- strategy-specific fields that don't belong as rigid
--    columns on a shared table (filing metadata, primary-source link,
--    committee-overlap context, etc. for congress_shreve_hern; left empty
--    for mean_reversion). This is what lets thesis #3/#4 avoid a migration.
-- ---------------------------------------------------------------------------

ALTER TABLE signals         ADD COLUMN details jsonb NOT NULL DEFAULT '{}';
ALTER TABLE trade_proposals ADD COLUMN details jsonb NOT NULL DEFAULT '{}';
ALTER TABLE signal_outcomes ADD COLUMN details jsonb NOT NULL DEFAULT '{}';

-- ---------------------------------------------------------------------------
-- 4. Congressional PTR filing log -- not every filing becomes a signal (v1
--    mirrors buys only; sells are logged for context per the design doc), so
--    this is its own table rather than forced into `signals`.
-- ---------------------------------------------------------------------------

CREATE TABLE congressional_filings (
    id                  bigserial PRIMARY KEY,
    thesis_id           bigint NOT NULL REFERENCES theses(id),
    member              text NOT NULL,               -- 'Jefferson Shreve', 'Kevin Hern'
    symbol              text NOT NULL,
    side                text NOT NULL CHECK (side IN ('buy', 'sell')),
    transaction_date    date NOT NULL,
    filed_date          date NOT NULL,
    filing_lag_days     integer GENERATED ALWAYS AS (filed_date - transaction_date) STORED,
    disclosed_value_low  numeric,
    disclosed_value_high numeric,
    primary_source_url  text,                        -- resolved House Clerk / eFD filing link
    committee_context   text,                         -- informational only, not a score input
    aggregator_source   text,                         -- which API this was polled from
    signal_id           bigint REFERENCES signals(id), -- set once/if this filing spawns a signal
    ingested_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_congressional_filings_member_symbol
    ON congressional_filings (member, symbol);
CREATE INDEX idx_congressional_filings_filed_date
    ON congressional_filings (filed_date);

-- ---------------------------------------------------------------------------
-- 5. Cross-thesis duplicate-proposal guard.
--    trade_proposals.side already exists, so "open" buy-side proposals in the
--    same symbol -- regardless of which thesis -- are prevented directly via
--    a partial unique index rather than app-side scanning.
--    Only "buy" side is guarded: sells (stop-losses/exits) against an
--    existing position are expected and shouldn't collide with this check.
-- ---------------------------------------------------------------------------

CREATE UNIQUE INDEX uq_open_buy_proposal_per_symbol
    ON trade_proposals (symbol)
    WHERE side = 'buy' AND decision IS NULL;

COMMIT;

-- ---------------------------------------------------------------------------
-- Post-migration TODOs (application layer, not this migration):
--
-- 1. Portfolio-level exposure check must run BEFORE insert into
--    trade_proposals, across ALL theses combined -- e.g.:
--
--      SELECT symbol, SUM(qty * price) AS combined_notional
--      FROM trades
--      WHERE status = 'filled'
--      GROUP BY symbol
--      HAVING SUM(qty * price) > :max_position_notional;
--
--    (draft this as a shared service both strategies call into, not
--    per-strategy duplicated logic -- see design doc "Position Sizing".)
--
-- 2. Repoint mean_reversion's param reads from signal_params -> theses.config
--    (slug = 'mean_reversion') so there is one live source of truth, then
--    stop writing to signal_params. Don't leave both live simultaneously.
--
-- 3. congress_shreve_hern thesis inserted with status = 'backtesting_only' --
--    do not flip to 'active' until the filing-date-entry-vs-SPY backtest
--    (see design doc "Must-Have Before Live") has actually been run.
--
-- 4. Wire congressional_filings ingestion (aggregator poll -> primary-source
--    resolution -> row here -> signal only for buy-side, universe-filtered
--    filings) once the backtest clears the thesis to 'active'.
-- ---------------------------------------------------------------------------

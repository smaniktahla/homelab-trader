-- Test-only shim: recreates the two tables (trade_proposals, signal_params)
-- that live in production but, like universe/universe_scan before this PR,
-- are not tracked in ingest/schema.sql. Recovered from the live DB the same
-- way universe/universe_scan were (information_schema, verified 2026-07-31).
-- Out of scope for this PR to formalize into schema.sql itself (not needed
-- by PR #1-4's actual feature work), but a real test fixture needs a
-- complete, accurate schema to run compute_signals() against.
CREATE TABLE IF NOT EXISTS trade_proposals (
    id               BIGSERIAL PRIMARY KEY,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,
    qty              NUMERIC,
    rationale        TEXT,
    signal_score     NUMERIC,
    proposed_at      TIMESTAMPTZ DEFAULT NOW(),
    decided_at       TIMESTAMPTZ,
    decision         TEXT,
    decided_by       TEXT,
    rejection_reason TEXT
);

CREATE TABLE IF NOT EXISTS signal_params (
    key         TEXT PRIMARY KEY,
    value       NUMERIC NOT NULL,
    description TEXT
);

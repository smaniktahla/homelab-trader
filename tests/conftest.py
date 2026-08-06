import os
import sys
import pathlib

import psycopg2
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared"))

TEST_DSN = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://invest_test:not_a_real_credential@localhost:15432/invest_test"
)

# Tables whose rows are per-cycle/per-signal state and must be wiped between
# tests. signal_params/theses/app_settings/global_markets are reference/
# config data seeded once by schema.sql's own INSERT ... ON CONFLICT
# statements and migration 001 — left alone so every test starts from the
# same config baseline instead of re-seeding it by hand each time.
RESET_TABLES = [
    "symbol_features", "signal_outcomes", "signals", "trade_proposals",
    "trades", "portfolio_snapshots", "price_history", "price_history_hourly",
    "news", "watchlist", "universe", "universe_scan", "earnings_events",
    "market_context", "market_regime_history", "sector_regime_history", "security_regime_history",
    "market_structure_history",
    "congressional_filings", "fundamental_facts",
    "position_trades", "position_lifecycles", "position_lifecycle_symbol_status",
    "rule_adherence_checks", "risk_decisions",
]


def _apply_sql_file(cur, path):
    cur.execute(path.read_text())


_THESES_SPLIT_MARKER = "-- Thesis horizon taxonomy"


@pytest.fixture(scope="session")
def _schema_ready():
    """Apply the fixture shim + real schema.sql + migration 001 once per
    test session against the disposable test Postgres, in the same order
    production actually reached this state: migration 001 was hand-run
    once (creating `theses`), and everything in schema.sql from the
    "Thesis horizon taxonomy" comment onward was added afterward, assuming
    `theses` already exists (its ALTER TABLE theses ADD COLUMN statement
    has no CREATE fallback). schema.sql alone has never been bootstrappable
    on a truly empty DB — this pre-dates and is unrelated to this PR — so a
    from-scratch test DB has to reproduce that same two-step history
    rather than run schema.sql as one blob."""
    schema_sql = (ROOT / "ingest" / "schema.sql").read_text()
    assert _THESES_SPLIT_MARKER in schema_sql, "schema.sql split marker moved or was removed"
    before_theses, after_theses = schema_sql.split(_THESES_SPLIT_MARKER, 1)
    after_theses = _THESES_SPLIT_MARKER + after_theses

    conn = psycopg2.connect(TEST_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        _apply_sql_file(cur, ROOT / "tests" / "fixture_schema.sql")
        cur.execute(before_theses)
        _apply_sql_file(cur, ROOT / "ingest" / "migrations" / "001_multi_thesis_architecture.sql")
        cur.execute(after_theses)
    conn.close()
    return True


@pytest.fixture
def conn(_schema_ready):
    """A fresh connection per test, with all per-cycle tables truncated
    first so each test starts from a known-empty state. Config/reference
    tables (signal_params, theses, app_settings) are left as schema.sql
    seeded them."""
    c = psycopg2.connect(TEST_DSN)
    with c.cursor() as cur:
        cur.execute(f"TRUNCATE {', '.join(RESET_TABLES)} RESTART IDENTITY CASCADE")
    c.commit()
    yield c
    c.close()

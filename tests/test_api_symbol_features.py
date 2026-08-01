"""
Exercises GET /api/symbol-features/{symbol} end to end against a real
Postgres connection (not mocked), specifically to catch DB-driver type
mismatches that pure unit tests miss: psycopg2 returns NUMERIC columns as
decimal.Decimal, while component_weights (jsonb) decodes to float — the
API layer must reconcile that before handing scores to
weighted_component_score(), which does plain float arithmetic.
"""

import os
import sys

import pytest


@pytest.fixture
def api_client(_schema_ready, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ.get(
        "TEST_DATABASE_URL", "postgresql://invest_test:not_a_real_credential@localhost:15432/invest_test"))
    monkeypatch.setenv("INVEST_USER", "test_invest_user")
    monkeypatch.setenv("INVEST_PASS", "test_invest_pass_not_real")

    import pathlib
    api_dir = str(pathlib.Path(__file__).resolve().parent.parent / "api")
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)

    # main.py reads env vars at import time; force a fresh import per test
    # run so it picks up the env vars set above even if some other process
    # already imported a module named "main".
    sys.modules.pop("main", None)
    import main as api_main
    from fastapi.testclient import TestClient
    return TestClient(api_main.app)


def test_symbol_features_no_snapshot_returns_null_sides(api_client, conn):
    r = api_client.get("/api/symbol-features/AAPL", auth=("test_invest_user", "test_invest_pass_not_real"))
    assert r.status_code == 200
    body = r.json()
    assert body == {"symbol": "AAPL", "buy": None, "sell": None}


def test_symbol_features_live_technical_only(api_client, conn):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO symbol_features
                (symbol, side, as_of, technical_score, data_confidence,
                 feature_version, model_version, component_weights, source_timestamps)
            VALUES ('AAPL', 'buy', NOW(), 62, 1.0, 'v1',
                    'mean_reversion_technical_only_v1', '{"technical": 1.0}', '{}')
        """)
    conn.commit()

    r = api_client.get("/api/symbol-features/AAPL", auth=("test_invest_user", "test_invest_pass_not_real"))
    assert r.status_code == 200
    buy = r.json()["buy"]

    assert buy["composite_score"] == 62
    assert buy["data_confidence"] == 1.0
    assert buy["components"]["technical"] == {"value": 62.0, "status": "live"}
    for name in ("fundamental", "earnings", "news", "options", "macro_fit"):
        assert buy["components"][name] == {"value": None, "status": "unavailable"}
    assert r.json()["sell"] is None


def test_symbol_features_picks_highest_feature_version_at_same_as_of(api_client, conn):
    """Regression test for a code-review finding: with no tiebreaker beyond
    as_of, two rows sharing the same as_of (e.g. a future repair process
    re-deriving a historical snapshot under a newer feature_version -- the
    exact scenario record_symbol_feature_snapshot's own docstring
    anticipates) resolved to whichever row Postgres happened to return
    first, not reliably the newer version. Must always prefer the higher
    feature_version, and fall back to id (insertion order) if even that
    ties."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO symbol_features
                (symbol, side, as_of, technical_score, data_confidence,
                 feature_version, model_version, component_weights, source_timestamps)
            VALUES
                ('AAPL', 'buy', '2026-07-31T12:00:00Z', 40, 1.0, 'v1',
                 'mean_reversion_technical_only_v1', '{"technical": 1.0}', '{}'),
                ('AAPL', 'buy', '2026-07-31T12:00:00Z', 90, 1.0, 'v2',
                 'mean_reversion_technical_only_v2', '{"technical": 1.0}', '{}')
        """)
    conn.commit()

    r = api_client.get("/api/symbol-features/AAPL", auth=("test_invest_user", "test_invest_pass_not_real"))
    assert r.status_code == 200
    buy = r.json()["buy"]
    assert buy["feature_version"] == "v2"
    assert buy["composite_score"] == 90

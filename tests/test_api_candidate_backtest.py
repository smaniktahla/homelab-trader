"""
Candidate backtest endpoints (Hypothesis-Driven Phase 5a-1):
POST /api/candidates/{id}/backtest, POST /api/candidate-batches/{id}/backtest,
GET /api/candidates/{id}/backtests.
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
    monkeypatch.setenv("ALPACA_BASE_URL", "https://fake-alpaca.test")

    import pathlib
    api_dir = str(pathlib.Path(__file__).resolve().parent.parent / "api")
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)

    sys.modules.pop("main", None)
    import main as api_main
    from fastapi.testclient import TestClient
    return TestClient(api_main.app)


AUTH = ("test_invest_user", "test_invest_pass_not_real")


def test_unknown_candidate_is_404(api_client, conn):
    r = api_client.post("/api/candidates/999999/backtest", auth=AUTH)
    assert r.status_code == 404


def test_unknown_batch_is_404(api_client, conn):
    r = api_client.post("/api/candidate-batches/999999/backtest", auth=AUTH)
    assert r.status_code == 404


def test_batch_backtest_records_an_unsupported_result_and_lists_it(api_client, conn):
    r = api_client.post("/api/hypothesis-types/structural_support_bounce/candidates", auth=AUTH, json={
        "parameter_spec": {"structural_zones.nearest_support_distance_atr": [0.5, 1.0]},
    })
    assert r.status_code == 200, r.text
    batch_id, cand_ids = r.json()["batch_id"], r.json()["candidate_ids"]

    r = api_client.post(f"/api/candidate-batches/{batch_id}/backtest", auth=AUTH)
    assert r.status_code == 200, r.text
    assert [x["status"] for x in r.json()] == ["unsupported_feature", "unsupported_feature"]

    r = api_client.get(f"/api/candidates/{cand_ids[0]}/backtests", auth=AUTH)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1 and rows[0]["status"] == "unsupported_feature"
    assert rows[0]["run_config"]["holdout_start"] == "2025-01-01"

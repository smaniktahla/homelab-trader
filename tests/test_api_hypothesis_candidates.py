"""
PR 14, Hypothesis-Driven Trading Architecture epic. Candidate generation
endpoints only -- POST /api/hypothesis-types/{type_key}/candidates,
GET /api/candidate-batches(/{batch_id}). Uses api/main.py's own plain
psycopg2.connect(DB_DSN) (not db()'s RealDictCursor -- see the endpoints'
comment in api/main.py for why).
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
SEEDED_TYPE = "mean_reversion_oversold"


def test_post_generate_candidates_creates_batch_and_candidates(api_client, conn):
    r = api_client.post(f"/api/hypothesis-types/{SEEDED_TYPE}/candidates", auth=AUTH, json={
        "parameter_spec": {"technical.rsi_14": [20, 25, 30]},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert "batch_id" in body
    assert len(body["candidate_ids"]) == 3

    get_r = api_client.get(f"/api/candidate-batches/{body['batch_id']}", auth=AUTH)
    assert get_r.status_code == 200
    get_body = get_r.json()
    assert get_body["batch"]["hypothesis_type"] == SEEDED_TYPE
    assert get_body["batch"]["hypothesis_type_version"] == 1
    assert len(get_body["candidates"]) == 3


def test_post_generate_candidates_422_for_unknown_type(api_client, conn):
    r = api_client.post("/api/hypothesis-types/not_a_real_type/candidates", auth=AUTH, json={
        "parameter_spec": {"technical.rsi_14": [25]},
    })
    assert r.status_code == 422


def test_post_generate_candidates_422_for_unmatched_feature(api_client, conn):
    r = api_client.post(f"/api/hypothesis-types/{SEEDED_TYPE}/candidates", auth=AUTH, json={
        "parameter_spec": {"market_regime.overall": [1.0]},
    })
    assert r.status_code == 422


def test_get_candidate_batch_404_for_unknown_id(api_client, conn):
    r = api_client.get("/api/candidate-batches/999999", auth=AUTH)
    assert r.status_code == 404


def test_get_candidate_batches_filters_by_hypothesis_type(api_client, conn):
    api_client.post(f"/api/hypothesis-types/{SEEDED_TYPE}/candidates", auth=AUTH, json={
        "parameter_spec": {"technical.rsi_14": [25]},
    })
    r = api_client.get(f"/api/candidate-batches?hypothesis_type={SEEDED_TYPE}", auth=AUTH)
    assert r.status_code == 200
    assert all(b["hypothesis_type"] == SEEDED_TYPE for b in r.json())
    assert len(r.json()) >= 1


def test_post_generate_candidates_requires_auth(api_client, conn):
    r = api_client.post(f"/api/hypothesis-types/{SEEDED_TYPE}/candidates", json={
        "parameter_spec": {"technical.rsi_14": [25]},
    })
    assert r.status_code == 401

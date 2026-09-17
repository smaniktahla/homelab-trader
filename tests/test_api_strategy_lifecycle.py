"""
SI-2, Strategy Incubator epic Phase 1 ("Foundations"). Basic read/
transition API over shared/strategy_lifecycle.py (SI-1). Uses api/main.py's
own plain psycopg2.connect(DB_DSN) (not db()'s RealDictCursor), same
reasoning as the hypothesis-types/candidates endpoints' own test file.
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


def _create_strategy(api_client, name="api_test_strategy"):
    r = api_client.post("/api/strategies", auth=AUTH, json={
        "strategy_name": name, "strategy_family": "test_family", "description": "for API tests",
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _create_version(api_client, strategy_id):
    r = api_client.post("/api/strategy-versions", auth=AUTH, json={"strategy_id": strategy_id})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_create_strategy_and_get_it(api_client, conn):
    strategy_id = _create_strategy(api_client, name="mean_reversion_api_test")
    r = api_client.get(f"/api/strategies/{strategy_id}", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["strategy_name"] == "mean_reversion_api_test"
    assert body["strategy_family"] == "test_family"


def test_create_strategy_duplicate_name_409(api_client, conn):
    _create_strategy(api_client, name="dup_api_test")
    r = api_client.post("/api/strategies", auth=AUTH, json={
        "strategy_name": "dup_api_test", "strategy_family": "test_family",
    })
    assert r.status_code == 409


def test_get_strategy_404_for_unknown_id(api_client, conn):
    r = api_client.get("/api/strategies/999999", auth=AUTH)
    assert r.status_code == 404


def test_create_strategy_version_starts_at_research(api_client, conn):
    strategy_id = _create_strategy(api_client)
    version_id = _create_version(api_client, strategy_id)
    r = api_client.get(f"/api/strategy-versions/{version_id}", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "RESEARCH"
    assert body["version_number"] == 1
    assert body["strategy_id"] == strategy_id


def test_list_strategy_versions_returns_all(api_client, conn):
    strategy_id = _create_strategy(api_client)
    _create_version(api_client, strategy_id)
    _create_version(api_client, strategy_id)
    r = api_client.get(f"/api/strategies/{strategy_id}/versions", auth=AUTH)
    assert r.status_code == 200
    assert [v["version_number"] for v in r.json()] == [1, 2]


def test_get_strategy_version_404_for_unknown_id(api_client, conn):
    r = api_client.get("/api/strategy-versions/999999", auth=AUTH)
    assert r.status_code == 404


def test_create_strategy_version_422_for_unknown_strategy_id(api_client, conn):
    r = api_client.post("/api/strategy-versions", auth=AUTH, json={"strategy_id": 999999})
    assert r.status_code == 422


def test_valid_transition_succeeds(api_client, conn):
    strategy_id = _create_strategy(api_client)
    version_id = _create_version(api_client, strategy_id)
    r = api_client.post(f"/api/strategy-versions/{version_id}/transition", auth=AUTH, json={
        "to_status": "BACKTEST", "actor": "api_test", "reason": "starting backtest",
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "BACKTEST"

    get_r = api_client.get(f"/api/strategy-versions/{version_id}", auth=AUTH)
    assert get_r.json()["status"] == "BACKTEST"


def test_illegal_transition_422(api_client, conn):
    strategy_id = _create_strategy(api_client)
    version_id = _create_version(api_client, strategy_id)
    r = api_client.post(f"/api/strategy-versions/{version_id}/transition", auth=AUTH, json={
        "to_status": "LIVE",  # can't skip straight to LIVE
    })
    assert r.status_code == 422


def test_freeze_requires_walk_forward_status_422(api_client, conn):
    strategy_id = _create_strategy(api_client)
    version_id = _create_version(api_client, strategy_id)
    r = api_client.post(f"/api/strategy-versions/{version_id}/freeze", auth=AUTH, json={
        "code_hash": "ch123", "parameter_hash": "ph456",
    })
    assert r.status_code == 422


def test_freeze_succeeds_from_walk_forward(api_client, conn):
    strategy_id = _create_strategy(api_client)
    version_id = _create_version(api_client, strategy_id)
    for status in ("BACKTEST", "VALIDATION", "WALK_FORWARD"):
        api_client.post(f"/api/strategy-versions/{version_id}/transition", auth=AUTH, json={"to_status": status})

    r = api_client.post(f"/api/strategy-versions/{version_id}/freeze", auth=AUTH, json={
        "code_hash": "ch123", "parameter_hash": "ph456", "actor": "api_test",
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "FROZEN"

    get_r = api_client.get(f"/api/strategy-versions/{version_id}", auth=AUTH)
    body = get_r.json()
    assert body["status"] == "FROZEN"
    assert body["code_hash"] == "ch123"
    assert body["parameter_hash"] == "ph456"
    assert body["parameter_frozen_at"] is not None


def test_strategy_endpoints_require_auth(api_client, conn):
    assert api_client.get("/api/strategies/1").status_code == 401
    assert api_client.post("/api/strategies", json={"strategy_name": "x", "strategy_family": "y"}).status_code == 401
    assert api_client.get("/api/strategy-versions/1").status_code == 401

"""
PR 13, Hypothesis-Driven Trading Architecture epic. Catalog CRUD endpoints
only -- GET/POST/PATCH /api/hypothesis-types(/{type_key}). These call
shared/hypothesis_library.py via api/main.py's own plain psycopg2.connect(
DB_DSN) (not db()'s RealDictCursor -- see the endpoints' comment in
api/main.py for why), so this also exercises that connection wiring end to
end, not just the underlying library functions (already covered in
tests/test_hypothesis_library.py).
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


def _cleanup(conn, type_key):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM hypothesis_type_changes WHERE type_key=%s", (type_key,))
        cur.execute("DELETE FROM hypothesis_types WHERE type_key=%s", (type_key,))
    conn.commit()


# --- GET -------------------------------------------------------------------

def test_get_hypothesis_types_includes_seeded_rows(api_client, conn):
    r = api_client.get("/api/hypothesis-types", auth=AUTH)
    assert r.status_code == 200
    keys = {row["type_key"] for row in r.json()}
    assert {"mean_reversion_oversold", "mean_reversion_overbought"} <= keys


def test_get_hypothesis_types_filters_by_status(api_client, conn):
    r = api_client.get("/api/hypothesis-types?status=deprecated", auth=AUTH)
    assert r.status_code == 200
    assert all(row["status"] == "deprecated" for row in r.json())


def test_get_hypothesis_type_by_key(api_client, conn):
    r = api_client.get("/api/hypothesis-types/mean_reversion_oversold", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["type_key"] == "mean_reversion_oversold"
    assert body["status"] == "active"


def test_get_hypothesis_type_404_for_unknown_key(api_client, conn):
    r = api_client.get("/api/hypothesis-types/not_a_real_key", auth=AUTH)
    assert r.status_code == 404


# --- POST --------------------------------------------------------------------

def test_create_hypothesis_type(api_client, conn):
    type_key = "test_api_created_type"
    _cleanup(conn, type_key)
    try:
        r = api_client.post("/api/hypothesis-types", auth=AUTH, json={
            "type_key": type_key,
            "display_name": "API Test Type",
            "description": "Created via POST /api/hypothesis-types.",
            "category": "test",
            "required_providers": ["technical"],
        })
        assert r.status_code == 200, r.text
        assert r.json()["type_key"] == type_key

        get_r = api_client.get(f"/api/hypothesis-types/{type_key}", auth=AUTH)
        assert get_r.status_code == 200
        assert get_r.json()["status"] == "active"
    finally:
        _cleanup(conn, type_key)


def test_create_hypothesis_type_duplicate_key_conflicts(api_client, conn):
    r = api_client.post("/api/hypothesis-types", auth=AUTH, json={
        "type_key": "mean_reversion_oversold",
        "display_name": "Duplicate",
        "description": "Should not be allowed.",
    })
    assert r.status_code == 409


def test_create_hypothesis_type_unregistered_provider_rejected(api_client, conn):
    type_key = "test_api_bad_provider_type"
    _cleanup(conn, type_key)
    try:
        r = api_client.post("/api/hypothesis-types", auth=AUTH, json={
            "type_key": type_key,
            "display_name": "Bad Provider",
            "description": "Should fail validation.",
            "required_providers": ["not_a_real_provider"],
        })
        assert r.status_code == 422

        get_r = api_client.get(f"/api/hypothesis-types/{type_key}", auth=AUTH)
        assert get_r.status_code == 404  # never written
    finally:
        _cleanup(conn, type_key)


# --- PATCH -------------------------------------------------------------------

def test_patch_hypothesis_type_updates_category_and_providers(api_client, conn):
    type_key = "test_api_patch_type"
    _cleanup(conn, type_key)
    api_client.post("/api/hypothesis-types", auth=AUTH, json={
        "type_key": type_key,
        "display_name": "Patch Target",
        "description": "Initial.",
        "required_providers": ["technical"],
    })
    try:
        r = api_client.patch(f"/api/hypothesis-types/{type_key}", auth=AUTH, json={
            "category": "updated-category",
            "required_providers": ["market_regime"],
        })
        assert r.status_code == 200, r.text

        get_r = api_client.get(f"/api/hypothesis-types/{type_key}", auth=AUTH)
        body = get_r.json()
        assert body["category"] == "updated-category"
        assert body["required_providers"] == ["market_regime"]
        assert body["version"] == 2
    finally:
        _cleanup(conn, type_key)


def test_patch_hypothesis_type_404_for_unknown_key(api_client, conn):
    r = api_client.patch("/api/hypothesis-types/not_a_real_key", auth=AUTH, json={"category": "x"})
    assert r.status_code == 404


def test_patch_hypothesis_type_rejects_unknown_provider_without_partial_write(api_client, conn):
    type_key = "test_api_patch_reject_type"
    _cleanup(conn, type_key)
    api_client.post("/api/hypothesis-types", auth=AUTH, json={
        "type_key": type_key,
        "display_name": "Original Name",
        "description": "Initial.",
        "required_providers": ["technical"],
    })
    try:
        r = api_client.patch(f"/api/hypothesis-types/{type_key}", auth=AUTH, json={
            "display_name": "Should Not Stick",
            "required_providers": ["not_a_real_provider"],
        })
        assert r.status_code == 422

        get_r = api_client.get(f"/api/hypothesis-types/{type_key}", auth=AUTH)
        body = get_r.json()
        assert body["display_name"] == "Original Name"  # no partial write
        assert body["required_providers"] == ["technical"]
        assert body["version"] == 1
    finally:
        _cleanup(conn, type_key)


def test_patch_hypothesis_type_can_deprecate(api_client, conn):
    type_key = "test_api_deprecate_type"
    _cleanup(conn, type_key)
    api_client.post("/api/hypothesis-types", auth=AUTH, json={
        "type_key": type_key,
        "display_name": "To Deprecate",
        "description": "Initial.",
    })
    try:
        r = api_client.patch(f"/api/hypothesis-types/{type_key}", auth=AUTH, json={"status": "deprecated"})
        assert r.status_code == 200

        active_r = api_client.get("/api/hypothesis-types?status=active", auth=AUTH)
        assert type_key not in {row["type_key"] for row in active_r.json()}
    finally:
        _cleanup(conn, type_key)


# --- auth --------------------------------------------------------------------

def test_get_hypothesis_types_requires_auth(api_client, conn):
    r = api_client.get("/api/hypothesis-types")
    assert r.status_code == 401

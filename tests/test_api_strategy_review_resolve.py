"""
Strategy Review apply/dismiss (POST /api/reviews/{id}/apply|dismiss). Apply
writes signal_params and flips status in one transaction; only an 'advisory'
row can be actioned; a live value that drifted from the review's
current_value is refused as stale.
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

    sys.modules.pop("main", None)
    import main as api_main
    from fastapi.testclient import TestClient
    return TestClient(api_main.app)


AUTH = ("test_invest_user", "test_invest_pass_not_real")


def _live_value(conn, key):
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM signal_params WHERE key=%s", (key,))
        return float(cur.fetchone()[0])


def _seed_review(conn, current, proposed, param="score_proposal_min", status="advisory"):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO strategy_review_proposals
                (n_resolved, finding, proposed_param, current_value, proposed_value, status)
            VALUES (10, 'f', %s, %s, %s, %s) RETURNING id
        """, (param, current, proposed, status))
        rid = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO signal_params (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (param, current))
    conn.commit()
    return rid


def _status(conn, rid):
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM strategy_review_proposals WHERE id=%s", (rid,))
        return cur.fetchone()[0]


def test_apply_updates_param_and_marks_applied(api_client, conn):
    rid = _seed_review(conn, 30, 50)
    r = api_client.post(f"/api/reviews/{rid}/apply", auth=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": rid, "status": "applied", "param": "score_proposal_min", "value": 50.0}
    assert _live_value(conn, "score_proposal_min") == 50.0
    assert _status(conn, rid) == "applied"


def test_apply_twice_conflicts(api_client, conn):
    rid = _seed_review(conn, 30, 50)
    assert api_client.post(f"/api/reviews/{rid}/apply", auth=AUTH).status_code == 200
    assert api_client.post(f"/api/reviews/{rid}/apply", auth=AUTH).status_code == 409


def test_apply_refuses_stale_proposal(api_client, conn):
    rid = _seed_review(conn, 30, 50)
    with conn.cursor() as cur:
        cur.execute("UPDATE signal_params SET value=40 WHERE key='score_proposal_min'")
    conn.commit()
    r = api_client.post(f"/api/reviews/{rid}/apply", auth=AUTH)
    assert r.status_code == 409
    assert "stale" in r.json()["detail"]
    assert _live_value(conn, "score_proposal_min") == 40.0
    assert _status(conn, rid) == "advisory"


def test_dismiss_leaves_param_alone(api_client, conn):
    rid = _seed_review(conn, 30, 50)
    r = api_client.post(f"/api/reviews/{rid}/dismiss", auth=AUTH)
    assert r.status_code == 200, r.text
    assert _live_value(conn, "score_proposal_min") == 30.0
    assert _status(conn, rid) == "dismissed"


def test_apply_without_proposal_is_400(api_client, conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO strategy_review_proposals (n_resolved, finding) VALUES (5, 'none') RETURNING id")
        rid = cur.fetchone()[0]
    conn.commit()
    assert api_client.post(f"/api/reviews/{rid}/apply", auth=AUTH).status_code == 400


def test_unknown_review_is_404(api_client, conn):
    assert api_client.post("/api/reviews/999999/apply", auth=AUTH).status_code == 404

"""
Tests for ingest.py's reconcile_stale_buy_proposals() -- open BUY proposals
older than signal_params.buy_proposal_max_age_days are auto-rejected so they
stop lingering on the dashboard and blocking regeneration.
"""

import os
import sys
import pathlib

import psycopg2.extras


def _import_ingest(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ.get(
        "TEST_DATABASE_URL", "postgresql://invest_test:not_a_real_credential@localhost:15432/invest_test"))
    monkeypatch.setenv("ALPACA_BASE_URL", "https://fake-alpaca.test")
    ingest_dir = str(pathlib.Path(__file__).resolve().parent.parent / "ingest")
    if ingest_dir not in sys.path:
        sys.path.insert(0, ingest_dir)
    sys.modules.pop("ingest", None)
    import ingest
    return ingest


def _proposal(conn, symbol, side, age_days, decision=None):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug='mean_reversion'")
        thesis_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, signal_score, proposed_at, decision, thesis_id)
            VALUES (%s, %s, 1, 70, NOW() - make_interval(days => %s), %s, %s) RETURNING id
        """, (symbol, side, age_days, decision, thesis_id))
        pid = cur.fetchone()[0]
    conn.commit()
    return pid


def _row(conn, pid):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM trade_proposals WHERE id=%s", (pid,))
        return cur.fetchone()


def _set_max_age(conn, value):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO signal_params (key, value) VALUES ('buy_proposal_max_age_days', %s)
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
        """, (value,))
    conn.commit()


def test_expires_old_open_buy_proposal(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    old = _proposal(conn, "AAA", "buy", age_days=17)
    ingest.reconcile_stale_buy_proposals(conn)
    r = _row(conn, old)
    assert r["decision"] == "rejected"
    assert r["decided_by"] == "system"
    assert "Auto-expired" in r["rejection_reason"]


def test_leaves_fresh_buy_proposal_alone(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    fresh = _proposal(conn, "BBB", "buy", age_days=1)
    ingest.reconcile_stale_buy_proposals(conn)
    assert _row(conn, fresh)["decision"] is None


def test_never_touches_sells_or_already_decided(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    old_sell = _proposal(conn, "CCC", "sell", age_days=30)
    approved = _proposal(conn, "DDD", "buy", age_days=30, decision="approved")
    ingest.reconcile_stale_buy_proposals(conn)
    assert _row(conn, old_sell)["decision"] is None
    assert _row(conn, approved)["decision"] == "approved"


def test_param_controls_age_and_zero_disables(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    p = _proposal(conn, "EEE", "buy", age_days=5)
    _set_max_age(conn, 7)
    ingest.reconcile_stale_buy_proposals(conn)
    assert _row(conn, p)["decision"] is None
    _set_max_age(conn, 0)
    ingest.reconcile_stale_buy_proposals(conn)
    assert _row(conn, p)["decision"] is None
    _set_max_age(conn, 4)
    ingest.reconcile_stale_buy_proposals(conn)
    assert _row(conn, p)["decision"] == "rejected"

"""
End-to-end tests of shared/proposal_ranking.py's wiring into
GET /api/proposals (api/main.py::get_proposals()). Read-time only --
never touches compute_signals()'s insert-time output, so
tests/test_fixture_equivalence*.py is unaffected.
"""

import os
import sys

import psycopg2.extras
import pytest
import requests_mock


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

    for mod in ("main", "rule_adherence", "signals", "risk_engine", "proposal_ranking"):
        sys.modules.pop(mod, None)
    import main as api_main
    monkeypatch.setattr(api_main.time, "sleep", lambda s: None)
    from fastapi.testclient import TestClient
    return TestClient(api_main.app)


AUTH = ("test_invest_user", "test_invest_pass_not_real")


def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug='mean_reversion'")
        return cur.fetchone()[0]


def _seed_universe(conn, symbol, sector):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO universe (symbol, sector) VALUES (%s, %s)", (symbol, sector))
    conn.commit()


def _seed_proposal(conn, symbol, side="buy", qty=10, signal_score=70, planned_entry_price=100.0):
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, thesis_id, signal_score, planned_entry_price)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
        """, (symbol, side, qty, thesis_id, signal_score, planned_entry_price))
        proposal_id = cur.fetchone()[0]
    conn.commit()
    return proposal_id


def _seed_price(conn, symbol, close):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO price_history (symbol, ts, close) VALUES (%s, NOW(), %s)
            ON CONFLICT (symbol, ts) DO NOTHING
        """, (symbol, close))
    conn.commit()


def _mock_common_alpaca(m, cash=50000.0, portfolio_value=100000.0, positions=None, open_orders=None):
    m.get("https://fake-alpaca.test/v2/account",
          json={"cash": str(cash), "buying_power": str(cash), "portfolio_value": str(portfolio_value)})
    m.get("https://fake-alpaca.test/v2/positions", json=positions or [])
    m.get("https://fake-alpaca.test/v2/orders", json=open_orders or [])


def test_get_proposals_includes_ranking_fields(api_client, conn):
    _seed_universe(conn, "NI", "Utilities")
    _seed_universe(conn, "AEP", "Utilities")
    _seed_price(conn, "NI", 50.0)
    _seed_price(conn, "AEP", 90.0)
    _seed_proposal(conn, "NI", signal_score=85)
    _seed_proposal(conn, "AEP", signal_score=60)

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m)
        r = api_client.get("/api/proposals", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    by_symbol = {p["symbol"]: p for p in body["proposals"]}

    assert by_symbol["NI"]["cluster_rank"] == 1
    assert by_symbol["NI"]["priority_tier"] == 1
    assert by_symbol["NI"]["cluster_label"] == "Utilities (2)"
    assert by_symbol["AEP"]["cluster_rank"] == 2
    assert by_symbol["AEP"]["opportunity_cost_note"] is not None
    assert "NI" in by_symbol["AEP"]["competes_with"]
    for p in body["proposals"]:
        assert p["recommended_action"] in {"Strong Buy", "Buy", "Watch", "Skip", "Sell"}
        assert "priority_stars" in p


def test_get_proposals_degrades_gracefully_when_alpaca_calls_fail(api_client, conn):
    _seed_universe(conn, "NI", "Utilities")
    _seed_price(conn, "NI", 50.0)
    _seed_proposal(conn, "NI", signal_score=85)

    with requests_mock.Mocker() as m:
        m.get("https://fake-alpaca.test/v2/account", status_code=500)
        m.get("https://fake-alpaca.test/v2/positions", status_code=500)
        m.get("https://fake-alpaca.test/v2/orders", status_code=500)
        r = api_client.get("/api/proposals", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["buying_power"] is None
    assert len(body["proposals"]) == 1
    # Ranking still runs (positions/orders fail open to empty lists) --
    # a single unmapped-cost-free proposal is still just the top tier.
    assert body["proposals"][0]["priority_tier"] == 1


def test_get_proposals_empty_returns_empty_list_no_500(api_client, conn):
    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m)
        r = api_client.get("/api/proposals", auth=AUTH)
    assert r.status_code == 200
    assert r.json()["proposals"] == []


def test_get_proposals_sell_proposal_untouched_by_clustering(api_client, conn):
    _seed_universe(conn, "AAPL", "Information Technology")
    _seed_price(conn, "AAPL", 200.0)
    _seed_proposal(conn, "AAPL", side="sell", qty=5, signal_score=None)

    with requests_mock.Mocker() as m:
        _mock_common_alpaca(m, positions=[{
            "symbol": "AAPL", "qty": "5", "avg_entry_price": "180.0",
            "current_price": "200.0", "market_value": "1000.0", "unrealized_plpc": "0.1",
        }])
        r = api_client.get("/api/proposals", auth=AUTH)
    assert r.status_code == 200
    row = r.json()["proposals"][0]
    assert row["side"] == "sell"
    assert row["recommended_action"] == "Sell"
    assert row["cluster_id"] is None


def test_get_proposals_disabled_flag_returns_unranked_shape(api_client, conn):
    # signal_params is reference/config data, deliberately NOT in
    # conftest.py's RESET_TABLES (so every test starts from the same
    # baseline) -- must restore the flag afterward or every later test in
    # this session would silently run with ranking disabled.
    _seed_universe(conn, "NI", "Utilities")
    _seed_price(conn, "NI", 50.0)
    _seed_proposal(conn, "NI", signal_score=85)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO signal_params (key, value) VALUES ('proposal_ranking_enabled', 0)
            ON CONFLICT (key) DO UPDATE SET value = 0
        """)
    conn.commit()

    try:
        with requests_mock.Mocker() as m:
            _mock_common_alpaca(m)
            r = api_client.get("/api/proposals", auth=AUTH)
        assert r.status_code == 200
        row = r.json()["proposals"][0]
        assert "priority_tier" not in row
    finally:
        with conn.cursor() as cur:
            cur.execute("UPDATE signal_params SET value = 1 WHERE key = 'proposal_ranking_enabled'")
        conn.commit()

"""
Tests ingest/fundamentals.py's collector logic against mocked SEC EDGAR
responses (requests_mock) -- never hits the real SEC API. Focuses on
parsing/upsert correctness and per-symbol failure isolation, not on the
scoring formula (see test_fundamentals.py for that).
"""

import pathlib
import sys

import pytest
import requests
import requests_mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "ingest"))
import fundamentals_collector as ing_fundamentals  # noqa: E402


TICKER_MAP = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
}


def _concept_json(entries):
    return {"units": {"USD": entries}}


AAPL_REVENUES = _concept_json([
    {"val": 1000, "accn": "acc-1", "end": "2026-03-31", "start": "2026-01-01",
     "filed": "2026-04-15", "form": "10-Q", "fy": 2026, "fp": "Q1"},
])
AAPL_GROSS_PROFIT = _concept_json([
    {"val": 400, "accn": "acc-1", "end": "2026-03-31", "start": "2026-01-01",
     "filed": "2026-04-15", "form": "10-Q", "fy": 2026, "fp": "Q1"},
])


@pytest.fixture(autouse=True)
def _sec_user_agent(monkeypatch):
    monkeypatch.setattr(ing_fundamentals, "SEC_USER_AGENT", "homelab-trader test@example.com")


def _mock_sec(m, cik=320193, revenues=AAPL_REVENUES, gross_profit=AAPL_GROSS_PROFIT,
              net_income_status=404):
    m.get(ing_fundamentals.TICKER_MAP_URL, json=TICKER_MAP)
    m.get(ing_fundamentals.COMPANYCONCEPT_URL.format(cik=cik, tag="Revenues"), json=revenues)
    m.get(ing_fundamentals.COMPANYCONCEPT_URL.format(cik=cik, tag="GrossProfit"), json=gross_profit)
    if net_income_status == 404:
        m.get(ing_fundamentals.COMPANYCONCEPT_URL.format(cik=cik, tag="NetIncomeLoss"), status_code=404)
    else:
        m.get(ing_fundamentals.COMPANYCONCEPT_URL.format(cik=cik, tag="NetIncomeLoss"), json=net_income_status)


def test_sync_skipped_when_user_agent_not_set(conn, monkeypatch):
    monkeypatch.setattr(ing_fundamentals, "SEC_USER_AGENT", "")
    with requests_mock.Mocker() as m:
        ing_fundamentals.sync_fundamentals(conn, ["AAPL"])
        assert not m.request_history  # no requests sent at all


def test_sync_inserts_facts_for_known_ticker(conn):
    with requests_mock.Mocker() as m:
        _mock_sec(m)
        ing_fundamentals.sync_fundamentals(conn, ["AAPL"])

    with conn.cursor() as cur:
        cur.execute("SELECT symbol, metric, value, accepted_at FROM fundamental_facts ORDER BY metric")
        rows = cur.fetchall()
    metrics = {r[1]: float(r[2]) for r in rows}
    assert metrics["Revenues"] == 1000
    assert metrics["GrossProfit"] == 400
    assert "NetIncomeLoss" not in metrics  # 404'd -- absent, not zero


def test_sync_uses_filed_date_as_accepted_at(conn):
    with requests_mock.Mocker() as m:
        _mock_sec(m)
        ing_fundamentals.sync_fundamentals(conn, ["AAPL"])
    with conn.cursor() as cur:
        cur.execute("SELECT accepted_at, filed_at FROM fundamental_facts WHERE metric='Revenues'")
        accepted_at, filed_at = cur.fetchone()
    assert accepted_at == filed_at


def test_unknown_ticker_skipped_without_error(conn):
    with requests_mock.Mocker() as m:
        m.get(ing_fundamentals.TICKER_MAP_URL, json=TICKER_MAP)
        ing_fundamentals.sync_fundamentals(conn, ["NOTAREALTICKER"])
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM fundamental_facts")
        assert cur.fetchone()[0] == 0


def test_revenue_tag_alias_fallback(conn):
    """If the classic 'Revenues' tag has no data, the ASC-606 alias tag is
    tried, and either way the fact is stored under the canonical metric
    name 'Revenues' -- callers never need to know which tag it came
    from."""
    with requests_mock.Mocker() as m:
        m.get(ing_fundamentals.TICKER_MAP_URL, json=TICKER_MAP)
        m.get(ing_fundamentals.COMPANYCONCEPT_URL.format(cik=320193, tag="Revenues"), status_code=404)
        m.get(ing_fundamentals.COMPANYCONCEPT_URL.format(
            cik=320193, tag="RevenueFromContractWithCustomerExcludingAssessedTax"), json=AAPL_REVENUES)
        m.get(ing_fundamentals.COMPANYCONCEPT_URL.format(cik=320193, tag="GrossProfit"), status_code=404)
        m.get(ing_fundamentals.COMPANYCONCEPT_URL.format(cik=320193, tag="NetIncomeLoss"), status_code=404)
        ing_fundamentals.sync_fundamentals(conn, ["AAPL"])

    with conn.cursor() as cur:
        cur.execute("SELECT metric, value FROM fundamental_facts")
        rows = cur.fetchall()
    assert rows == [("Revenues", 1000)]


def test_sync_is_idempotent_on_rerun(conn):
    with requests_mock.Mocker() as m:
        _mock_sec(m)
        ing_fundamentals.sync_fundamentals(conn, ["AAPL"])
    with requests_mock.Mocker() as m:
        _mock_sec(m)
        ing_fundamentals.sync_fundamentals(conn, ["AAPL"])  # same accession numbers again

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM fundamental_facts")
        assert cur.fetchone()[0] == 2  # Revenues + GrossProfit, not duplicated


def test_one_symbol_failure_does_not_block_others(conn):
    with requests_mock.Mocker() as m:
        m.get(ing_fundamentals.TICKER_MAP_URL, json=TICKER_MAP)
        # AAPL's companyconcept calls raise a connection-level error.
        m.get(ing_fundamentals.COMPANYCONCEPT_URL.format(cik=320193, tag="Revenues"),
              exc=requests.exceptions.ConnectionError)
        _mock_sec(m, cik=789019)  # MSFT succeeds normally
        ing_fundamentals.sync_fundamentals(conn, ["AAPL", "MSFT"])

    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM fundamental_facts ORDER BY symbol")
        symbols = {r[0] for r in cur.fetchall()}
    assert symbols == {"MSFT"}

    # Connection must still be usable -- proves the AAPL failure rolled
    # back cleanly rather than poisoning the transaction for MSFT.
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)


def test_ignores_non_usd_units(conn):
    mixed = {"units": {
        "shares": [{"val": 99, "accn": "acc-shares", "end": "2026-03-31",
                    "filed": "2026-04-15", "form": "10-Q", "fy": 2026, "fp": "Q1"}],
        "USD": [{"val": 1000, "accn": "acc-usd", "end": "2026-03-31",
                 "filed": "2026-04-15", "form": "10-Q", "fy": 2026, "fp": "Q1"}],
    }}
    with requests_mock.Mocker() as m:
        m.get(ing_fundamentals.TICKER_MAP_URL, json=TICKER_MAP)
        m.get(ing_fundamentals.COMPANYCONCEPT_URL.format(cik=320193, tag="Revenues"), json=mixed)
        m.get(ing_fundamentals.COMPANYCONCEPT_URL.format(cik=320193, tag="GrossProfit"), status_code=404)
        m.get(ing_fundamentals.COMPANYCONCEPT_URL.format(cik=320193, tag="NetIncomeLoss"), status_code=404)
        ing_fundamentals.sync_fundamentals(conn, ["AAPL"])

    with conn.cursor() as cur:
        cur.execute("SELECT value, unit FROM fundamental_facts")
        rows = cur.fetchall()
    assert rows == [(1000, "USD")]

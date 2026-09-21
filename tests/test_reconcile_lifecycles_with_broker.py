"""
Tests for ingest.py's reconcile_lifecycles_with_broker() and
shared/broker_reconciliation.py -- open position_lifecycles (built from
the trades ledger) vs Alpaca's real positions. Found live 2026-09-20:
lifecycles left open for positions the broker had closed, and no lifecycle
for a held position.
"""

import os
import sys
import pathlib
from datetime import datetime, timezone

import pytest
import requests_mock

from broker_reconciliation import diff_open_lifecycles_vs_broker, is_clean

BASE = "https://fake-alpaca.test"


def _import_ingest(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ.get(
        "TEST_DATABASE_URL", "postgresql://invest_test:not_a_real_credential@localhost:15432/invest_test"))
    monkeypatch.setenv("ALPACA_BASE_URL", BASE)
    ingest_dir = str(pathlib.Path(__file__).resolve().parent.parent / "ingest")
    if ingest_dir not in sys.path:
        sys.path.insert(0, ingest_dir)
    sys.modules.pop("ingest", None)
    import ingest
    return ingest


def _thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug='mean_reversion'")
        return cur.fetchone()[0]


def _add_trade(conn, symbol, side, qty, price, order_id, when, status="filled"):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trades (symbol, side, qty, price, notional, order_id, traded_at, source, status, cost, thesis_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'manual',%s,0,%s)
        """, (symbol, side, qty, price, qty * price, order_id, when, status, _thesis_id(conn)))
    conn.commit()


def _open_symbols(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM position_lifecycles WHERE status='open' ORDER BY symbol")
        return [r[0] for r in cur.fetchall()]


def _set_repair(conn, on):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO signal_params (key, value) VALUES ('lifecycle_broker_repair', %s)
                       ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""", (1 if on else 0,))
    conn.commit()


def _order(oid, symbol, side, qty, price, typ="market", status="filled"):
    return {"id": oid, "symbol": symbol, "side": side, "type": typ, "status": status,
            "filled_qty": str(qty), "filled_avg_price": str(price), "filled_at": "2026-09-01T15:00:00Z"}


T0 = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)


# ---- pure diff ----

def test_diff_flags_ledger_only_broker_only_and_mismatch():
    d = diff_open_lifecycles_vs_broker({"ORCL": 5, "SO": 10, "DOV": 4}, {"SO": 10, "CNP": 7, "DOV": 3})
    assert d["ledger_only"] == {"ORCL": 5}
    assert d["broker_only"] == {"CNP": 7}
    assert d["qty_mismatch"] == {"DOV": (4, 3)}
    assert not is_clean(d)


def test_diff_clean_when_matching_within_tolerance():
    assert is_clean(diff_open_lifecycles_vs_broker({"SO": 10.0}, {"SO": 10.0000001}))
    assert is_clean(diff_open_lifecycles_vs_broker({}, {}))


# ---- ingest reconciler ----

def test_detects_drift_without_writing_when_repair_off(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    _add_trade(conn, "ORCL", "buy", 5, 100, "o-orcl-buy", T0)
    _add_trade(conn, "SO", "buy", 10, 80, "o-so-buy", T0)
    ingest.build_position_lifecycles(conn)
    with requests_mock.Mocker() as m:
        m.get(f"{BASE}/v2/positions", json=[{"symbol": "SO", "qty": "10"}, {"symbol": "CNP", "qty": "7"}])
        diff = ingest.reconcile_lifecycles_with_broker(conn)
    assert diff["ledger_only"] == {"ORCL": 5.0}
    assert diff["broker_only"] == {"CNP": 7.0}
    assert m.request_history[-1].path == "/v2/positions"   # never asked for orders
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trades")
        assert cur.fetchone()[0] == 2


def test_clean_state_makes_no_order_calls(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    _add_trade(conn, "SO", "buy", 10, 80, "o-so-buy", T0)
    ingest.build_position_lifecycles(conn)
    _set_repair(conn, True)
    with requests_mock.Mocker() as m:
        m.get(f"{BASE}/v2/positions", json=[{"symbol": "SO", "qty": "10"}])
        assert ingest.reconcile_lifecycles_with_broker(conn) == {
            "ledger_only": {}, "broker_only": {}, "qty_mismatch": {}, "broker_short": {}}
    assert [r.path for r in m.request_history] == ["/v2/positions"]


@pytest.mark.parametrize("failure", [{"status_code": 500}, {"json": {"message": "nope"}}, {"exc": ConnectionError}])
def test_positions_fetch_failure_fails_closed(conn, monkeypatch, failure):
    ingest = _import_ingest(monkeypatch)
    _add_trade(conn, "ORCL", "buy", 5, 100, "o-orcl-buy", T0)
    ingest.build_position_lifecycles(conn)
    _set_repair(conn, True)
    with requests_mock.Mocker() as m:
        m.get(f"{BASE}/v2/positions", **failure)
        assert ingest.reconcile_lifecycles_with_broker(conn) is None   # must not raise
    assert [r.path for r in m.request_history] == ["/v2/positions"]
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trades")
        assert cur.fetchone()[0] == 1
    assert _open_symbols(conn) == ["ORCL"]


def test_repair_closes_orphan_lifecycle_and_opens_missing_one(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    _add_trade(conn, "ORCL", "buy", 5, 100, "o-orcl-buy", T0)
    _add_trade(conn, "SO", "buy", 10, 80, "o-so-buy", T0)
    ingest.build_position_lifecycles(conn)
    _set_repair(conn, True)
    with requests_mock.Mocker() as m:
        m.get(f"{BASE}/v2/positions", json=[{"symbol": "SO", "qty": "10"}, {"symbol": "CNP", "qty": "7"}])
        m.get(f"{BASE}/v2/orders", [
            {"json": [_order("o-orcl-sell", "ORCL", "sell", 5, 95, typ="stop")]},   # ledger_only ORCL -> sell
            {"json": [_order("o-cnp-buy", "CNP", "buy", 7, 50)]},                   # broker_only CNP -> buy
        ])
        diff = ingest.reconcile_lifecycles_with_broker(conn)
    assert diff == {"ledger_only": {}, "broker_only": {}, "qty_mismatch": {}, "broker_short": {}}
    assert _open_symbols(conn) == ["CNP", "SO"]
    with conn.cursor() as cur:
        cur.execute("SELECT order_id, source, counts_toward_loss_streak FROM trades WHERE source='broker_reconcile' ORDER BY order_id")
        rows = cur.fetchall()
    # stop fill keeps default loss-streak accounting (NULL); unknown-provenance buy is excluded (FALSE)
    assert rows == [("o-cnp-buy", "broker_reconcile", False), ("o-orcl-sell", "broker_reconcile", None)]


def test_non_stop_backfilled_sell_does_not_count_toward_loss_streak(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    _add_trade(conn, "ORCL", "buy", 5, 100, "o-orcl-buy", T0)
    ingest.build_position_lifecycles(conn)
    _set_repair(conn, True)
    with requests_mock.Mocker() as m:
        m.get(f"{BASE}/v2/positions", json=[])   # successful empty answer: account is flat
        m.get(f"{BASE}/v2/orders", json=[_order("o-orcl-sell", "ORCL", "sell", 5, 90)])
        ingest.reconcile_lifecycles_with_broker(conn)
    from trading_permission import current_loss_streak
    assert current_loss_streak(conn) == 0    # a real loss, but of unknown provenance -> not counted
    assert _open_symbols(conn) == []


def test_repair_is_idempotent(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    _add_trade(conn, "ORCL", "buy", 5, 100, "o-orcl-buy", T0)
    ingest.build_position_lifecycles(conn)
    _set_repair(conn, True)
    for _ in range(2):
        with requests_mock.Mocker() as m:
            m.get(f"{BASE}/v2/positions", json=[])
            m.get(f"{BASE}/v2/orders", json=[_order("o-orcl-sell", "ORCL", "sell", 5, 95, typ="stop")])
            ingest.reconcile_lifecycles_with_broker(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trades WHERE order_id='o-orcl-sell'")
        assert cur.fetchone()[0] == 1


def test_repair_advances_ledger_row_stuck_short_of_filled(conn, monkeypatch):
    """CNP-style case: a trades row exists but never reached status='filled',
    so build_position_lifecycles ignores it."""
    ingest = _import_ingest(monkeypatch)
    _add_trade(conn, "CNP", "buy", 7, 50, "o-cnp-buy", T0, status="accepted")
    ingest.build_position_lifecycles(conn)
    assert _open_symbols(conn) == []
    _set_repair(conn, True)
    with requests_mock.Mocker() as m:
        m.get(f"{BASE}/v2/positions", json=[{"symbol": "CNP", "qty": "7"}])
        m.get(f"{BASE}/v2/orders", json=[_order("o-cnp-buy", "CNP", "buy", 7, 50.5)])
        ingest.reconcile_lifecycles_with_broker(conn)
    assert _open_symbols(conn) == ["CNP"]
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trades WHERE symbol='CNP'")
        assert cur.fetchone()[0] == 1   # updated in place, no duplicate


def test_orders_fetch_failure_during_repair_changes_nothing(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    _add_trade(conn, "ORCL", "buy", 5, 100, "o-orcl-buy", T0)
    ingest.build_position_lifecycles(conn)
    _set_repair(conn, True)
    with requests_mock.Mocker() as m:
        m.get(f"{BASE}/v2/positions", json=[])
        m.get(f"{BASE}/v2/orders", status_code=503)
        diff = ingest.reconcile_lifecycles_with_broker(conn)
    assert diff["ledger_only"] == {"ORCL": 5.0}
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trades")
        assert cur.fetchone()[0] == 1


def test_broker_short_is_flagged_and_never_backfilled(conn, monkeypatch):
    """Live 2026-09-20: CNP -118 -- a second sell right after the one that
    closed the long flipped the account short. Long-only ledger can't hold
    it; must be surfaced, and repair must not try to 'fix' it."""
    ingest = _import_ingest(monkeypatch)
    _set_repair(conn, True)
    with requests_mock.Mocker() as m:
        m.get(f"{BASE}/v2/positions", json=[{"symbol": "CNP", "qty": "-118"}])
        diff = ingest.reconcile_lifecycles_with_broker(conn)
    assert diff["broker_short"] == {"CNP": -118.0}
    assert not is_clean(diff)
    assert [r.path for r in m.request_history] == ["/v2/positions"]   # no order backfill attempted


def test_stop_fill_lookback_covers_stops_that_fire_long_after_entry(conn, monkeypatch):
    """Alpaca's `after` filters on order creation/submission, and an OTO
    stop leg carries its parent's timestamps -- a 2h window could never see
    a stop firing days later (EIX 2026-08-31)."""
    ingest = _import_ingest(monkeypatch)
    with requests_mock.Mocker() as m:
        m.get(f"{BASE}/v2/orders", json=[_order("stop-leg", "EIX", "sell", 70, 56.13, typ="stop")])
        ingest.reconcile_broker_stop_fills(conn)
    after = datetime.fromisoformat(m.request_history[0].qs["after"][0].upper().replace("Z", "+00:00"))
    assert (datetime.now(timezone.utc) - after).days >= 29


def test_repair_backs_off_from_opened_at_so_entry_time_stop_legs_are_found(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    _add_trade(conn, "EIX", "buy", 70, 68, "o-eix-buy", T0)
    ingest.build_position_lifecycles(conn)
    _set_repair(conn, True)
    with requests_mock.Mocker() as m:
        m.get(f"{BASE}/v2/positions", json=[])
        m.get(f"{BASE}/v2/orders", json=[])
        ingest.reconcile_lifecycles_with_broker(conn)
    after = datetime.fromisoformat(m.request_history[1].qs["after"][0].upper().replace("Z", "+00:00"))
    assert after < T0   # strictly before the ledger row's opened_at

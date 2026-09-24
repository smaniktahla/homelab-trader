"""
Buy proposals that cannot be acted on (found live 2026-09-24): the risk
engine had rejected every open buy at creation (portfolio open risk over
its cap) yet each still sat on the dashboard and was alerted; one was an
add proposed alongside an open time-stop exit for the same symbol; adds
also competed with new names for the proposal slots.

Covers compute_signals() (risk pre-check, exit-conflict, adds outside the
slot cap) and ingest.reconcile_unactionable_buy_proposals().
"""

import os
import sys
import pathlib
from datetime import datetime, timedelta, timezone

import pytest
import requests_mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _dir in (ROOT / "shared", ROOT / "ingest"):
    _p = str(_dir)
    if _p not in sys.path:
        sys.path.insert(0, _p)

sys.modules.pop("signals", None)
import signals   # noqa: E402

BASE = "https://fake-alpaca.test"


def _import_ingest(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ.get(
        "TEST_DATABASE_URL", "postgresql://invest_test:not_a_real_credential@localhost:15432/invest_test"))
    monkeypatch.setenv("ALPACA_BASE_URL", BASE)
    sys.modules.pop("ingest", None)
    import ingest
    # ingest bound whatever module object sys.modules["signals"] pointed at
    # when it imported; other test files re-import signals, so set the base
    # on THAT object, not this file's own reference.
    live_signals = sys.modules["signals"]
    live_signals.ALPACA_BASE = BASE
    live_signals.ALPACA_HEADERS = {"APCA-API-KEY-ID": "", "APCA-API-SECRET-KEY": ""}
    return ingest


@pytest.fixture(autouse=True)
def _restore_signal_params(conn):
    """conftest leaves signal_params as seeded for the whole session, so a
    test that sets a param would leak it into every later test (in any
    file). Snapshot and restore around each test."""
    with conn.cursor() as cur:
        cur.execute("SELECT key, value, description FROM signal_params")
        before = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    conn.commit()
    yield
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SELECT key FROM signal_params")
        for (k,) in cur.fetchall():
            if k not in before:
                cur.execute("DELETE FROM signal_params WHERE key=%s", (k,))
        for k, (v, d) in before.items():
            cur.execute("UPDATE signal_params SET value=%s WHERE key=%s", (v, k))
    conn.commit()


def _thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug='mean_reversion'")
        return cur.fetchone()[0]


def _set_param(conn, key, value):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO signal_params (key, value, description) VALUES (%s,%s,'')
                       ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""", (key, value))
    conn.commit()


def _add_proposal(conn, symbol, side="buy", score=50, qty=10, stop=None):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, thesis_id, signal_score, final_proposal_score,
                                         planned_initial_stop_price)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (symbol, side, qty, _thesis_id(conn), score, score, stop))
        pid = cur.fetchone()[0]
    conn.commit()
    return pid


def _proposals(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol, side, decision, decided_by, rejection_reason FROM trade_proposals ORDER BY id")
        return cur.fetchall()


def _open_lifecycle_with_risk(conn, symbol, risk_dollars):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO position_lifecycles (symbol, status, opened_at, qty, entry_notional, exit_notional,
                                              total_cost, gross_pnl, net_pnl, actual_initial_risk_dollars)
            VALUES (%s,'open',now(),1,0,0,0,0,0,%s)
        """, (symbol, risk_dollars))
    conn.commit()


# ---- compute_signals fixtures (same shape as tests/test_proposal_slots.py) ----

def _yahoo(closes):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return {"chart": {"result": [{
        "timestamp": [int((start + timedelta(days=i)).timestamp()) for i in range(len(closes))],
        "indicators": {"quote": [{"open": closes, "high": [c * 1.01 for c in closes],
                                  "low": [c * 0.99 for c in closes], "close": closes,
                                  "volume": [1_000_000] * len(closes)}],
                       "adjclose": [{"adjclose": closes}]}}]}}


def _buy_closes():
    closes = [180 + 0.05 * ((-1) ** i) for i in range(45)]
    return closes + [closes[-1] * f for f in (0.95, 0.90, 0.85, 0.80, 0.76)]


def _seed_prices(conn, symbol, closes):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO watchlist (symbol, name) VALUES (%s,%s) ON CONFLICT DO NOTHING", (symbol, symbol))
        for i, c in enumerate(closes[-30:]):
            cur.execute("""INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                           VALUES (%s,%s,%s,%s,%s,%s,1000000) ON CONFLICT (symbol, ts) DO NOTHING""",
                        (symbol, base + timedelta(days=i), c, c * 1.01, c * 0.99, c))
        for i in range(30):
            spy = 450 + (i % 3) * 0.5
            cur.execute("""INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                           VALUES ('SPY',%s,%s,%s,%s,%s,5000000) ON CONFLICT (symbol, ts) DO NOTHING""",
                        (base + timedelta(days=i), spy, spy * 1.01, spy * 0.99, spy))
    conn.commit()


@pytest.fixture
def env(conn, monkeypatch):
    monkeypatch.setenv("ALPACA_BASE_URL", BASE)
    signals.ALPACA_BASE = BASE
    signals.ALPACA_HEADERS = {"APCA-API-KEY-ID": "", "APCA-API-SECRET-KEY": ""}
    for k, v in (("score_proposal_min", 10), ("trade_allocation_pct", 0.05), ("max_position_pct", 0.20),
                 ("buy_cooldown_days", 2), ("earnings_blackout_days", 3), ("circuit_breaker_drawdown_pct", 0.15),
                 ("max_open_positions", 10), ("open_buy_proposal_buffer", 2)):
        _set_param(conn, k, v)
    _seed_prices(conn, "AAPL", _buy_closes())
    return conn


def _run(conn, positions=None):
    with requests_mock.Mocker() as m:
        m.get("https://query2.finance.yahoo.com/v8/finance/chart/AAPL", json=_yahoo(_buy_closes()))
        m.get(f"{BASE}/v2/account", json={"cash": "10000", "portfolio_value": "10000"})
        m.get(f"{BASE}/v2/positions", json=positions or [])
        signals.compute_signals(conn, ["AAPL"])


def _aapl_buy_open(conn):
    return [r for r in _proposals(conn) if r[0] == "AAPL" and r[1] == "buy" and r[2] is None]


def _block_reason(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT block_reason FROM signal_outcomes WHERE symbol='AAPL' AND side='buy'")
        row = cur.fetchone()
    return row[0] if row else None


def _risk_decisions(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT outcome, binding_constraint FROM risk_decisions WHERE symbol='AAPL'")
        return cur.fetchall()


def test_buy_the_risk_engine_rejects_is_not_created(env):
    conn = env
    _open_lifecycle_with_risk(conn, "OTHER", 5000)   # open risk far over 6% of a $10k portfolio
    _run(conn)
    assert _aapl_buy_open(conn) == []
    assert _block_reason(conn) == "risk_engine_rejected:portfolio_open_risk"


def test_flag_off_restores_create_then_record_behavior(env):
    conn = env
    _set_param(conn, "skip_risk_rejected_buy_proposals", 0)
    _open_lifecycle_with_risk(conn, "OTHER", 5000)
    _run(conn)
    assert len(_aapl_buy_open(conn)) == 1
    assert _risk_decisions(conn) == [("rejected", "portfolio_open_risk")]


def test_buy_the_risk_engine_accepts_is_still_created_and_recorded_once(env):
    conn = env
    _run(conn)
    assert len(_aapl_buy_open(conn)) == 1
    assert len(_risk_decisions(conn)) == 1


def test_buy_is_skipped_while_an_exit_proposal_is_open_for_the_symbol(env):
    conn = env
    _add_proposal(conn, "AAPL", side="sell", score=85)
    _run(conn)
    assert _aapl_buy_open(conn) == []
    assert _block_reason(conn) == "exit_proposal_open"


def test_add_to_a_held_position_does_not_compete_for_proposal_slots(env):
    """3 max positions, 2 held (one of them AAPL) -> 1 free slot, buffer 0 ->
    1 new-name buy allowed and already taken. AAPL is an add: it takes no
    slot, so it is still created."""
    conn = env
    _set_param(conn, "max_open_positions", 3)
    _set_param(conn, "open_buy_proposal_buffer", 0)
    _add_proposal(conn, "NEWNAME", score=100)
    held = [{"symbol": "AAPL", "qty": "1", "avg_entry_price": "140", "current_price": "137",
             "market_value": "137", "unrealized_plpc": "-0.02"},   # underwater, but inside the stop-loss threshold
            {"symbol": "XYZ", "qty": "5", "avg_entry_price": "50", "current_price": "50",
             "market_value": "250", "unrealized_plpc": "0"}]
    _run(conn, positions=held)
    assert len(_aapl_buy_open(conn)) == 1
    assert [r for r in _proposals(conn) if r[0] == "NEWNAME"][0][2] is None   # nothing displaced


def test_new_name_buy_still_blocked_by_a_full_open_set(env):
    conn = env
    _set_param(conn, "max_open_positions", 3)
    _set_param(conn, "open_buy_proposal_buffer", 0)
    _add_proposal(conn, "NEWNAME", score=100)
    held = [{"symbol": "XYZ", "qty": "5", "avg_entry_price": "50", "current_price": "50",
             "market_value": "250", "unrealized_plpc": "0"},
            {"symbol": "ABC", "qty": "5", "avg_entry_price": "50", "current_price": "50",
             "market_value": "250", "unrealized_plpc": "0"}]
    _run(conn, positions=held)
    assert _aapl_buy_open(conn) == []
    assert _block_reason(conn) == "no_free_proposal_slot"


# ---- ingest.reconcile_unactionable_buy_proposals ----

def _seed_close(conn, symbol, close):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                       VALUES (%s, now(), %s, %s, %s, %s, 1000000)""", (symbol, close, close, close, close))
    conn.commit()


def _account(m, cash="10000", pv="10000", positions=None):
    m.get(f"{BASE}/v2/account", json={"cash": cash, "portfolio_value": pv})
    m.get(f"{BASE}/v2/positions", json=positions or [])


def test_open_buy_with_an_open_exit_for_the_same_symbol_is_rejected(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    _add_proposal(conn, "SO", side="buy", stop=70)
    _add_proposal(conn, "SO", side="sell")
    _add_proposal(conn, "OTHER", side="buy", stop=70)
    with requests_mock.Mocker() as m:
        _account(m)
        ingest.reconcile_unactionable_buy_proposals(conn)
    st = _proposals(conn)
    assert st[0][2] == "rejected" and st[0][3] == "system" and "exit proposal" in st[0][4]
    assert st[1][2] is None and st[2][2] is None   # the sell itself, and an unrelated buy, untouched


def test_open_buy_the_risk_engine_now_rejects_is_rejected(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    _seed_close(conn, "AAA", 50.0)
    _add_proposal(conn, "AAA", qty=10, stop=44.0)
    _open_lifecycle_with_risk(conn, "OTHER", 5000)   # over the 6% portfolio-risk cap
    with requests_mock.Mocker() as m:
        _account(m)
        ingest.reconcile_unactionable_buy_proposals(conn)
    row = _proposals(conn)[0]
    assert row[2] == "rejected" and row[3] == "system" and "portfolio_open_risk" in row[4]


def test_open_buy_the_risk_engine_accepts_is_left_alone(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    _seed_close(conn, "AAA", 50.0)
    _add_proposal(conn, "AAA", qty=10, stop=44.0)
    with requests_mock.Mocker() as m:
        _account(m)
        ingest.reconcile_unactionable_buy_proposals(conn)
    assert _proposals(conn)[0][2] is None


@pytest.mark.parametrize("failure", [{"status_code": 500}, {"exc": ConnectionError}])
def test_risk_check_fails_closed_when_the_account_cannot_be_fetched(conn, monkeypatch, failure):
    ingest = _import_ingest(monkeypatch)
    _seed_close(conn, "AAA", 50.0)
    _add_proposal(conn, "AAA", qty=10, stop=44.0)
    _open_lifecycle_with_risk(conn, "OTHER", 5000)   # would be vetoed if the account were known
    with requests_mock.Mocker() as m:
        m.get(f"{BASE}/v2/account", **failure)
        m.get(f"{BASE}/v2/positions", **failure)
        ingest.reconcile_unactionable_buy_proposals(conn)   # must not raise
    assert _proposals(conn)[0][2] is None


def test_flag_off_skips_the_risk_check_but_still_applies_the_exit_conflict_rule(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    _set_param(conn, "skip_risk_rejected_buy_proposals", 0)
    _seed_close(conn, "AAA", 50.0)
    _add_proposal(conn, "AAA", qty=10, stop=44.0)          # would be vetoed
    _add_proposal(conn, "SO", side="buy", stop=70)
    _add_proposal(conn, "SO", side="sell")
    _open_lifecycle_with_risk(conn, "OTHER", 5000)
    with requests_mock.Mocker() as m:
        _account(m)
        ingest.reconcile_unactionable_buy_proposals(conn)
    st = {(r[0], r[1]): r[2] for r in _proposals(conn)}
    assert st[("AAA", "buy")] is None and st[("SO", "buy")] == "rejected"


def test_proposal_without_a_stop_or_price_is_left_alone(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    _add_proposal(conn, "NOPRICE", qty=10, stop=44.0)      # no price_history at all
    _add_proposal(conn, "NOSTOP", qty=10, stop=None)
    _seed_close(conn, "NOSTOP", 50.0)
    _open_lifecycle_with_risk(conn, "OTHER", 5000)
    with requests_mock.Mocker() as m:
        _account(m)
        ingest.reconcile_unactionable_buy_proposals(conn)
    assert all(r[2] is None for r in _proposals(conn))

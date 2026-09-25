"""
Tests for slot-aware open-BUY-proposal limits (shared/proposal_slots.py,
ingest.reconcile_surplus_buy_proposals, the compute_signals() gate, and the
buy-alert digest). Found live 2026-09-24: one free position slot, 99 open
buy proposals, and 20-50 email+WhatsApp alerts a day.
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

import proposal_slots as ps

BASE = "https://fake-alpaca.test"


def _import_ingest(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ.get(
        "TEST_DATABASE_URL", "postgresql://invest_test:not_a_real_credential@localhost:15432/invest_test"))
    monkeypatch.setenv("ALPACA_BASE_URL", BASE)
    sys.modules.pop("ingest", None)
    import ingest
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


def _add_proposal(conn, symbol, side="buy", score=50, decision=None):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, thesis_id, signal_score, final_proposal_score, decision)
            VALUES (%s,%s,10,%s,%s,%s,%s) RETURNING id
        """, (symbol, side, _thesis_id(conn), score, score, decision))
        pid = cur.fetchone()[0]
    conn.commit()
    return pid


def _state(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol, side, decision, decided_by, rejection_reason FROM trade_proposals ORDER BY id")
        return cur.fetchall()


def _set_param(conn, key, value):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO signal_params (key, value, description) VALUES (%s,%s,'')
                       ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""", (key, value))
    conn.commit()


# ---- pure helpers ----

def test_allowed_open_buys_is_zero_with_no_free_slot():
    assert ps.allowed_open_buys(15, 15, buffer=2) == 0
    assert ps.allowed_open_buys(15, 20, buffer=2) == 0


def test_allowed_open_buys_is_free_slots_plus_buffer():
    assert ps.allowed_open_buys(15, 14, buffer=2) == 3
    assert ps.allowed_open_buys(15, 10, buffer=0) == 5


def test_weakest_breaks_ties_toward_the_newer_proposal():
    assert ps.weakest([(1, 50.0), (2, 50.0), (3, 80.0)]) == (2, 50.0)
    assert ps.weakest([]) is None


# ---- trim ----

def test_trim_keeps_the_highest_scored_and_rejects_the_rest(conn):
    for sym, score in [("AAA", 40), ("BBB", 90), ("CCC", 60), ("DDD", 75), ("EEE", 55)]:
        _add_proposal(conn, sym, score=score)
    rejected = ps.trim_surplus_buy_proposals(conn, max_open_positions=15, position_count=14, buffer=2)  # allowed 3
    assert sorted(r[1] for r in rejected) == ["AAA", "EEE"]
    open_syms = sorted(r[0] for r in _state(conn) if r[2] is None)
    assert open_syms == ["BBB", "CCC", "DDD"]
    reasons = [r[4] for r in _state(conn) if r[2] == "rejected"]
    assert all("Auto-rejected" in x for x in reasons)


def test_trim_never_touches_sells_or_decided_proposals(conn):
    _add_proposal(conn, "SELLME", side="sell", score=1)
    _add_proposal(conn, "DONE", decision="approved", score=1)
    for sym, score in [("AAA", 40), ("BBB", 90)]:
        _add_proposal(conn, sym, score=score)
    ps.trim_surplus_buy_proposals(conn, 15, 14, buffer=0)   # allowed 1
    st = {r[0]: r[2] for r in _state(conn)}
    assert st["SELLME"] is None and st["DONE"] == "approved"
    assert st["BBB"] is None and st["AAA"] == "rejected"


def test_trim_is_a_noop_within_the_allowed_count(conn):
    _add_proposal(conn, "AAA")
    _add_proposal(conn, "BBB")
    assert ps.trim_surplus_buy_proposals(conn, 15, 10, buffer=2) == []


def test_trim_rejects_every_buy_when_at_max_open_positions(conn):
    _add_proposal(conn, "AAA", score=99)
    ps.trim_surplus_buy_proposals(conn, 15, 15, buffer=2)
    (row,) = _state(conn)
    assert row[2] == "rejected" and "max_open_positions" in row[4]


def test_trim_leaves_adds_to_held_positions_out_of_the_ranking(conn):
    """A buy of a symbol already held long takes no free slot, so it must not
    compete with (or be trimmed in favor of) new-name buys."""
    _add_proposal(conn, "HELD", score=10)     # an add, lowest score
    _add_proposal(conn, "NEW1", score=90)
    _add_proposal(conn, "NEW2", score=80)
    ps.trim_surplus_buy_proposals(conn, 15, 14, buffer=0, held_symbols={"HELD"})   # allowed 1 new-name buy
    st = {r[0]: r[2] for r in _state(conn)}
    assert st["HELD"] is None and st["NEW1"] is None and st["NEW2"] == "rejected"


def test_trim_rejects_adds_too_when_there_is_no_free_slot(conn):
    """compute_signals()'s max_open_positions gate blocks adds at the cap, so
    the trim stays consistent with it."""
    _add_proposal(conn, "HELD", score=99)
    ps.trim_surplus_buy_proposals(conn, 15, 15, buffer=2, held_symbols={"HELD"})
    assert _state(conn)[0][2] == "rejected"


# ---- ingest step: fail closed ----

def test_ingest_trim_fails_closed_when_positions_unavailable(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    _set_param(conn, "max_open_positions", 1)
    for sym in ("AAA", "BBB", "CCC", "DDD"):
        _add_proposal(conn, sym)
    for failure in ({"status_code": 500}, {"json": {"message": "nope"}}):
        with requests_mock.Mocker() as m:
            m.get(f"{BASE}/v2/positions", **failure)
            ingest.reconcile_surplus_buy_proposals(conn)   # must not raise
    assert all(r[2] is None for r in _state(conn))


def test_ingest_trim_counts_short_positions_toward_the_cap(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    _set_param(conn, "max_open_positions", 3)
    _set_param(conn, "open_buy_proposal_buffer", 0)
    for sym, score in (("AAA", 50), ("BBB", 60)):
        _add_proposal(conn, sym, score=score)
    with requests_mock.Mocker() as m:   # 2 longs + 1 short = 3 of 3 -> no slot
        m.get(f"{BASE}/v2/positions", json=[
            {"symbol": "X", "qty": "10"}, {"symbol": "Y", "qty": "5"}, {"symbol": "CNP", "qty": "-118"}])
        ingest.reconcile_surplus_buy_proposals(conn)
    assert all(r[2] == "rejected" for r in _state(conn))


# ---- compute_signals gate ----

for _mod in ("signals",):
    sys.modules.pop(_mod, None)
import signals   # noqa: E402


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
def gate_env(conn, monkeypatch):
    monkeypatch.setenv("ALPACA_BASE_URL", BASE)
    signals.ALPACA_BASE = BASE
    signals.ALPACA_HEADERS = {"APCA-API-KEY-ID": "", "APCA-API-SECRET-KEY": ""}
    for k, v in (("score_proposal_min", 10), ("trade_allocation_pct", 0.05), ("max_position_pct", 0.20),
                 ("buy_cooldown_days", 2), ("earnings_blackout_days", 3), ("circuit_breaker_drawdown_pct", 0.15)):
        _set_param(conn, k, v)
    _seed_prices(conn, "AAPL", _buy_closes())
    return conn


def _run(conn):
    with requests_mock.Mocker() as m:
        m.get("https://query2.finance.yahoo.com/v8/finance/chart/AAPL", json=_yahoo(_buy_closes()))
        m.get(f"{BASE}/v2/account", json={"cash": "10000", "portfolio_value": "10000"})
        m.get(f"{BASE}/v2/positions", json=[])
        signals.compute_signals(conn, ["AAPL"])


def _aapl_open(conn):
    return [r for r in _state(conn) if r[0] == "AAPL" and r[2] is None]


def test_gate_blocks_a_new_buy_that_does_not_beat_a_full_open_set(gate_env):
    conn = gate_env
    _set_param(conn, "max_open_positions", 1)
    _set_param(conn, "open_buy_proposal_buffer", 0)
    _add_proposal(conn, "HELD", score=100)          # already fills the one allowed open buy
    _run(conn)
    assert _aapl_open(conn) == []
    with conn.cursor() as cur:
        cur.execute("SELECT block_reason FROM signal_outcomes WHERE symbol='AAPL' AND side='buy'")
        assert cur.fetchone()[0] == "no_free_proposal_slot"


def test_gate_displaces_the_weakest_open_buy_for_a_higher_scored_one(gate_env):
    conn = gate_env
    _set_param(conn, "max_open_positions", 1)
    _set_param(conn, "open_buy_proposal_buffer", 0)
    _add_proposal(conn, "WEAK", score=0)
    _run(conn)
    assert len(_aapl_open(conn)) == 1
    weak = [r for r in _state(conn) if r[0] == "WEAK"][0]
    assert weak[2] == "rejected" and weak[3] == "system" and "displaced" in weak[4]


def test_gate_creates_freely_when_there_is_room(gate_env):
    conn = gate_env
    _set_param(conn, "max_open_positions", 5)
    _set_param(conn, "open_buy_proposal_buffer", 2)
    _add_proposal(conn, "OTHER", score=100)
    _run(conn)
    assert len(_aapl_open(conn)) == 1
    assert [r for r in _state(conn) if r[0] == "OTHER"][0][2] is None   # nothing displaced


# ---- alert digest ----

def _ensure_app_settings(conn):
    """app_settings is created by the app at runtime, not by schema.sql."""
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
        cur.execute("DELETE FROM app_settings WHERE key LIKE 'alert_sent_%'")
    conn.commit()


def _cfg():
    return {"notify_email": True, "notify_whatsapp": True, "smtp_user": "u", "smtp_pass": "p",
            "digest_to": "t@example.test", "atq_url": "http://atq.test"}


def test_new_buy_proposals_are_batched_into_one_alert_and_sells_stay_immediate(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    _ensure_app_settings(conn)
    sent = []
    monkeypatch.setattr(ingest, "send_notification", lambda cfg, subject, html, wa, label="": sent.append(subject))
    for sym, score in (("AAA", 40), ("BBB", 90), ("CCC", 60)):
        _add_proposal(conn, sym, score=score)
    _add_proposal(conn, "SOLD", side="sell", score=85)

    ingest.check_new_proposal_alerts(conn, _cfg())
    assert len(sent) == 2
    assert any("SELL SOLD" in s for s in sent)
    digest = [s for s in sent if "buy proposal" in s][0]
    assert digest.startswith("📋 3 new buy proposals") and digest.index("BBB") < digest.index("CCC") < digest.index("AAA")

    ingest.check_new_proposal_alerts(conn, _cfg())   # each proposal alerts once
    assert len(sent) == 2


def test_no_alert_at_all_when_there_are_no_new_proposals(conn, monkeypatch):
    ingest = _import_ingest(monkeypatch)
    _ensure_app_settings(conn)
    sent = []
    monkeypatch.setattr(ingest, "send_notification", lambda *a, **k: sent.append(1))
    ingest.check_new_proposal_alerts(conn, _cfg())
    assert sent == []

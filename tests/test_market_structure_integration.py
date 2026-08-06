"""
Integration coverage for Market Structure Engine PR 2's wiring across
ingest/ingest.py and shared/signals.py::compute_signals -- proves the
snapshot actually lands on a real trade_proposals row through the real
compute_signals() path, not just via market_structure.py's own direct
unit/DB tests (tests/test_market_structure.py).

Same fixture shape as tests/test_hierarchy_regime_integration.py
(duplicated locally rather than imported, to keep this PR self-contained)
-- a sharp-drop-into-oversold BUY signal against a lowered
score_proposal_min gate.
"""

import sys
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import requests_mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _dir in (ROOT / "shared", ROOT / "ingest"):
    p = str(_dir)
    if p not in sys.path:
        sys.path.insert(0, p)

for _mod in ("market_structure", "hierarchy_regime", "sector_regime", "security_regime",
             "regime_scoring", "regime_common", "sector_mapping", "signals"):
    sys.modules.pop(_mod, None)
import market_structure as ms
import signals


def _make_yahoo_chart_json(closes):
    n = len(closes)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamps = [int((start + timedelta(days=i)).timestamp()) for i in range(n)]
    return {
        "chart": {
            "result": [{
                "timestamp": timestamps,
                "indicators": {
                    "quote": [{
                        "open": closes, "high": [c * 1.01 for c in closes],
                        "low": [c * 0.99 for c in closes], "close": closes,
                        "volume": [1_000_000] * n,
                    }],
                    "adjclose": [{"adjclose": closes}],
                },
            }]
        }
    }


def _bullish_buy_closes():
    closes = [180 + 0.05 * ((-1) ** i) for i in range(45)]
    closes += [closes[-1] * f for f in (0.95, 0.90, 0.85, 0.80, 0.76)]
    return closes


def _seed_signal_fixture(conn, symbol, closes):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO watchlist (symbol, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (symbol, symbol))
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        for i, close in enumerate(closes[-30:]):
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, 1000000)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, (symbol, base + timedelta(days=i), close, close * 1.01, close * 0.99, close))
        for i in range(30):
            spy_close = 450 + (i % 3) * 0.5
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES ('SPY', %s, %s, %s, %s, %s, 5000000)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, (base + timedelta(days=i), spy_close, spy_close * 1.01, spy_close * 0.99, spy_close))
    conn.commit()


def _set_signal_param(conn, key, value):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO signal_params (key, value, description) VALUES (%s, %s, '')
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, value))
    conn.commit()


def _set_low_threshold_gate_params(conn):
    _set_signal_param(conn, "score_proposal_min", 10)
    _set_signal_param(conn, "max_open_positions", 5)
    _set_signal_param(conn, "trade_allocation_pct", 0.05)
    _set_signal_param(conn, "max_position_pct", 0.20)
    _set_signal_param(conn, "buy_cooldown_days", 2)
    _set_signal_param(conn, "earnings_blackout_days", 3)
    _set_signal_param(conn, "circuit_breaker_drawdown_pct", 0.15)


@pytest.fixture
def alpaca_base(monkeypatch):
    base = "https://fake-alpaca.test"
    monkeypatch.setenv("ALPACA_BASE_URL", base)
    signals.ALPACA_BASE = base
    signals.ALPACA_HEADERS = {"APCA-API-KEY-ID": "", "APCA-API-SECRET-KEY": ""}
    return base


def _run_compute_signals(conn, alpaca_base, closes, symbol="AAPL"):
    with requests_mock.Mocker() as m:
        m.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
              json=_make_yahoo_chart_json(closes))
        m.get(f"{alpaca_base}/v2/account", json={"cash": "10000", "portfolio_value": "10000"})
        m.get(f"{alpaca_base}/v2/positions", json=[])
        signals.compute_signals(conn, [symbol])


def test_proposal_gets_insufficient_data_snapshot_when_structure_never_computed(conn, alpaca_base):
    """update_market_structure hasn't run this cycle (ingest.py's job, not
    compute_signals'): the proposal should still get a well-formed
    snapshot -- the graceful insufficient_data shape, not a crash or a
    missing column."""
    closes = _bullish_buy_closes()
    _set_low_threshold_gate_params(conn)
    _seed_signal_fixture(conn, "AAPL", closes)

    _run_compute_signals(conn, alpaca_base, closes)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT market_structure_snapshot, structure_trend, structure_confidence
            FROM trade_proposals WHERE symbol='AAPL' AND side='buy'
        """)
        row = cur.fetchone()
    assert row is not None
    snapshot, structure_trend, structure_confidence = row
    assert structure_trend == "insufficient_data"
    assert structure_confidence == 0
    assert snapshot["symbol"] == "AAPL"


def test_proposal_gets_real_snapshot_when_structure_precomputed_this_cycle(conn, alpaca_base):
    """Mirrors production ordering: ingest.py calls update_market_structure()
    before compute_signals() every cycle. Simulate that ordering directly
    and confirm compute_signals reads back what was already persisted
    rather than recomputing live."""
    closes = _bullish_buy_closes()
    _set_low_threshold_gate_params(conn)
    _seed_signal_fixture(conn, "AAPL", closes)

    ms.update_market_structure(conn, ["AAPL"])
    persisted = ms.load_latest_market_structure(conn, "AAPL")
    assert persisted is not None  # sanity: something was actually stored

    _run_compute_signals(conn, alpaca_base, closes)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT market_structure_snapshot, structure_trend, structure_confidence
            FROM trade_proposals WHERE symbol='AAPL' AND side='buy'
        """)
        row = cur.fetchone()
    assert row is not None
    snapshot, structure_trend, structure_confidence = row
    assert structure_trend == persisted["trend"]
    assert snapshot["trend"] == persisted["trend"]


# ─────────────────────────────────────────────────────────────────────────
# structure_scoring_enabled wiring -- same "prove disabled changes nothing,
# enabled shifts the score" shape as
# test_hierarchy_regime_integration.py's regime_scoring_enabled tests.
# ─────────────────────────────────────────────────────────────────────────

def test_final_score_equals_base_when_structure_scoring_disabled(conn, alpaca_base):
    closes = _bullish_buy_closes()
    _set_low_threshold_gate_params(conn)
    _set_signal_param(conn, "structure_scoring_enabled", 0)
    _seed_signal_fixture(conn, "AAPL", closes)
    ms.store_market_structure_day(conn, date.today(), "AAPL", dict(_STRUCTURE_CTX, trend="bullish"))

    _run_compute_signals(conn, alpaca_base, closes)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT base_strategy_score, final_proposal_score, structure_score_adjustment, structure_trend
            FROM trade_proposals WHERE symbol='AAPL' AND side='buy'
        """)
        row = cur.fetchone()
    assert row is not None
    base_score, final_score, structure_adj, structure_trend = row
    assert structure_adj == 0
    assert final_score == base_score
    assert structure_trend == "bullish"  # snapshot still recorded even though it isn't scored


def test_final_score_diverges_when_structure_scoring_enabled(conn, alpaca_base):
    closes = _bullish_buy_closes()
    _set_low_threshold_gate_params(conn)
    _set_signal_param(conn, "structure_scoring_enabled", 1)
    _seed_signal_fixture(conn, "AAPL", closes)
    ms.store_market_structure_day(conn, date.today(), "AAPL", dict(_STRUCTURE_CTX, trend="bullish"))

    _run_compute_signals(conn, alpaca_base, closes)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT base_strategy_score, final_proposal_score, structure_score_adjustment
            FROM trade_proposals WHERE symbol='AAPL' AND side='buy'
        """)
        row = cur.fetchone()
    assert row is not None
    base_score, final_score, structure_adj = row
    assert structure_adj == 10  # structure_trend_bullish default
    assert final_score == min(100, base_score + 10)
    assert final_score != base_score


_STRUCTURE_CTX = {
    "trend": "bullish", "confidence": 80, "trend_strength": "strong", "volatility": "normal",
    "bos": False, "choch": False, "risk": "low", "summary": "test",
    "monthly": {"trend_direction": "higher_highs_higher_lows"},
    "weekly": {"trend_direction": "higher_highs_higher_lows"},
    "daily": {"trend_direction": "higher_highs_higher_lows"},
}

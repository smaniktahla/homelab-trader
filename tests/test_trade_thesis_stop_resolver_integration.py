"""
Integration coverage for PR 6's wiring into shared/signals.py::compute_signals
-- proves structure_aware_stop_enabled actually controls what
trade_proposals.planned_initial_stop_price gets set to for a real BUY
signal, through the real compute_signals() path.

Same fixture shape as tests/test_trade_thesis_engine_integration.py
(duplicated locally, per that file's own precedent, to keep this PR
self-contained).
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

for _mod in ("trade_thesis_stop_resolver", "market_structure", "signals"):
    sys.modules.pop(_mod, None)
import signals
import market_structure as ms

_STRUCTURE_CTX = {
    "trend": "bullish", "confidence": 80, "trend_strength": "strong", "volatility": "normal",
    "bos": False, "choch": False, "risk": "low", "summary": "test",
    "monthly": {"trend_direction": "higher_highs_higher_lows"},
    "weekly": {"trend_direction": "higher_highs_higher_lows"},
    "daily": {"trend_direction": "higher_highs_higher_lows"},
}


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


def _insert_market_structure(conn, symbol, trading_date, nearest_support):
    ctx = dict(_STRUCTURE_CTX, nearest_support=nearest_support)
    ms.store_market_structure_day(conn, trading_date, symbol, ctx)


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


def test_planned_stop_is_percentage_when_flag_disabled(conn, alpaca_base):
    closes = _bullish_buy_closes()
    _set_low_threshold_gate_params(conn)
    _set_signal_param(conn, "structure_aware_stop_enabled", 0)
    _set_signal_param(conn, "stop_loss_pct", 0.08)
    _seed_signal_fixture(conn, "AAPL", closes)
    # Support recorded but must be ignored while the flag is off.
    _insert_market_structure(conn, "AAPL", date.today(), {"price": closes[-1] * 0.5, "touch_count": 3})

    _run_compute_signals(conn, alpaca_base, closes)

    with conn.cursor() as cur:
        cur.execute("SELECT planned_initial_stop_price FROM trade_proposals WHERE symbol='AAPL' AND side='buy'")
        stop_price = cur.fetchone()[0]
    expected = closes[-1] * (1 - 0.08)
    assert float(stop_price) == pytest.approx(expected, rel=1e-6)


def test_planned_stop_uses_structure_support_when_flag_enabled(conn, alpaca_base):
    closes = _bullish_buy_closes()
    _set_low_threshold_gate_params(conn)
    _set_signal_param(conn, "structure_aware_stop_enabled", 1)
    _set_signal_param(conn, "stop_loss_pct", 0.08)
    _seed_signal_fixture(conn, "AAPL", closes)

    price = closes[-1]
    percentage_stop = price * (1 - 0.08)
    # A support level between the percentage stop and price -- sane and tighter.
    support_price = price - (price - percentage_stop) * 0.5
    _insert_market_structure(conn, "AAPL", date.today(), {"price": support_price, "touch_count": 4})

    _run_compute_signals(conn, alpaca_base, closes)

    with conn.cursor() as cur:
        cur.execute("SELECT planned_initial_stop_price FROM trade_proposals WHERE symbol='AAPL' AND side='buy'")
        stop_price = cur.fetchone()[0]
    assert float(stop_price) == pytest.approx(support_price, rel=1e-6)


def test_falls_back_to_percentage_when_support_exceeds_sanity_cap(conn, alpaca_base):
    closes = _bullish_buy_closes()
    _set_low_threshold_gate_params(conn)
    _set_signal_param(conn, "structure_aware_stop_enabled", 1)
    _set_signal_param(conn, "stop_loss_pct", 0.08)
    _seed_signal_fixture(conn, "AAPL", closes)

    price = closes[-1]
    # Absurdly distant support (90% below price) -- must be rejected by the
    # sanity cap and fall back to the plain percentage stop.
    _insert_market_structure(conn, "AAPL", date.today(), {"price": price * 0.1, "touch_count": 1})

    _run_compute_signals(conn, alpaca_base, closes)

    with conn.cursor() as cur:
        cur.execute("SELECT planned_initial_stop_price FROM trade_proposals WHERE symbol='AAPL' AND side='buy'")
        stop_price = cur.fetchone()[0]
    expected = price * (1 - 0.08)
    assert float(stop_price) == pytest.approx(expected, rel=1e-6)

"""
Integration coverage for the relative-strength risk eligibility filter's
wiring across shared/signals.py::compute_signals -- proves the gate
actually blocks a real buy proposal end to end, not just at the pure
evaluate_relative_strength_risk() unit level (tests/test_relative_strength_risk.py).

Same fixture shape as tests/test_hierarchy_regime_integration.py /
tests/test_market_structure_integration.py (a sharp-drop-into-oversold
BUY signal against a lowered score_proposal_min gate).
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

for _mod in ("relative_strength_risk", "hierarchy_regime", "sector_regime", "security_regime",
             "regime_scoring", "regime_common", "sector_mapping", "signals"):
    sys.modules.pop(_mod, None)
import security_regime as sec_regime
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


def _seed_stock_regime(conn, symbol, vs_sector_score):
    """Seeds security_regime_history so hierarchy_regime.snapshot_hierarchy_for_symbol's
    vs_sector_classification derives to underperforming/outperforming --
    that derivation (hierarchy_regime._vs_sector_classification_from_score)
    buckets purely on the sign of vs_sector_score, same convention this
    feature's gate relies on."""
    sec_regime.store_security_regime_day(conn, date.today(), symbol, {
        "symbol": symbol, "sector": "Technology", "benchmark_symbol": "SPY",
        "classification": "neutral", "total_score": vs_sector_score,
        "absolute_trend_score": 0, "vs_sector_score": vs_sector_score, "vs_market_score": 0,
        "confidence": 0.8, "component_values": {},
    })


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


def _get_buy_proposal(conn, symbol="AAPL"):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM trade_proposals WHERE symbol=%s AND side='buy'", (symbol,))
        return cur.fetchone()


def _get_block_reason(conn, symbol="AAPL"):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT block_reason FROM signal_outcomes
            WHERE symbol=%s AND side='buy' ORDER BY id DESC LIMIT 1
        """, (symbol,))
        row = cur.fetchone()
        return row[0] if row else None


def test_underperforming_buy_proceeds_when_filter_disabled(conn, alpaca_base):
    closes = _bullish_buy_closes()
    _set_low_threshold_gate_params(conn)
    _set_signal_param(conn, "relative_strength_risk_mode", 0)
    _seed_signal_fixture(conn, "AAPL", closes)
    _seed_stock_regime(conn, "AAPL", vs_sector_score=-1)

    _run_compute_signals(conn, alpaca_base, closes)

    assert _get_buy_proposal(conn) is not None, "filter is off -- proposal must be created as before"


def test_underperforming_buy_rejected_when_gate_enabled(conn, alpaca_base):
    closes = _bullish_buy_closes()
    _set_low_threshold_gate_params(conn)
    _set_signal_param(conn, "relative_strength_risk_mode", 1)
    _seed_signal_fixture(conn, "AAPL", closes)
    _seed_stock_regime(conn, "AAPL", vs_sector_score=-1)

    _run_compute_signals(conn, alpaca_base, closes)

    assert _get_buy_proposal(conn) is None, "underperforming_sector buy must be blocked, not silently allowed"
    block_reason = _get_block_reason(conn)
    assert block_reason is not None
    assert block_reason.startswith("relative_strength_risk_gate")
    assert "classification=underperforming_sector" in block_reason


def test_outperforming_buy_proceeds_when_gate_enabled(conn, alpaca_base):
    closes = _bullish_buy_closes()
    _set_low_threshold_gate_params(conn)
    _set_signal_param(conn, "relative_strength_risk_mode", 1)
    _seed_signal_fixture(conn, "AAPL", closes)
    _seed_stock_regime(conn, "AAPL", vs_sector_score=1)

    _run_compute_signals(conn, alpaca_base, closes)

    assert _get_buy_proposal(conn) is not None, "outperforming stocks must never be gated"


def test_unmapped_sector_buy_proceeds_when_gate_enabled(conn, alpaca_base):
    """Sector lookup failure (no security_regime_history row at all --
    vs_sector_classification derives to 'unknown') must fail open, never
    reject a valid trade merely because sector data is unavailable."""
    closes = _bullish_buy_closes()
    _set_low_threshold_gate_params(conn)
    _set_signal_param(conn, "relative_strength_risk_mode", 1)
    _seed_signal_fixture(conn, "AAPL", closes)
    # No _seed_stock_regime call -- simulates missing/unavailable classification.

    _run_compute_signals(conn, alpaca_base, closes)

    assert _get_buy_proposal(conn) is not None, "missing classification data must fail open, not reject"

"""
Integration coverage for PR 4's wiring into shared/signals.py::compute_signals
-- proves trade_thesis_instantiation_enabled actually controls whether a
real BUY signal gets a trade_theses row and a populated
trade_proposals.trade_thesis_id, through the real compute_signals() path.

Same fixture shape as tests/test_market_structure_integration.py
(duplicated locally rather than imported, per that file's own precedent,
to keep this PR self-contained).
"""

import sys
import pathlib
from datetime import datetime, timedelta, timezone

import pytest
import requests_mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _dir in (ROOT / "shared", ROOT / "ingest"):
    p = str(_dir)
    if p not in sys.path:
        sys.path.insert(0, p)

for _mod in ("trade_thesis", "trade_thesis_validator", "trade_thesis_engine",
             "feature_registry", "signals"):
    sys.modules.pop(_mod, None)
import signals
from trade_thesis import TradeThesis
from trade_thesis_validator import validate_trade_thesis


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


def test_trade_thesis_id_stays_null_when_flag_disabled(conn, alpaca_base):
    closes = _bullish_buy_closes()
    _set_low_threshold_gate_params(conn)
    _set_signal_param(conn, "trade_thesis_instantiation_enabled", 0)
    _seed_signal_fixture(conn, "AAPL", closes)

    _run_compute_signals(conn, alpaca_base, closes)

    with conn.cursor() as cur:
        cur.execute("SELECT trade_thesis_id FROM trade_proposals WHERE symbol='AAPL' AND side='buy'")
        row = cur.fetchone()
        cur.execute("SELECT count(*) FROM trade_theses")
        thesis_count = cur.fetchone()[0]
    assert row is not None
    assert row[0] is None
    assert thesis_count == 0


def test_trade_thesis_created_and_linked_when_flag_enabled(conn, alpaca_base):
    closes = _bullish_buy_closes()
    _set_low_threshold_gate_params(conn)
    _set_signal_param(conn, "trade_thesis_instantiation_enabled", 1)
    _seed_signal_fixture(conn, "AAPL", closes)

    _run_compute_signals(conn, alpaca_base, closes)

    with conn.cursor() as cur:
        cur.execute("SELECT trade_thesis_id FROM trade_proposals WHERE symbol='AAPL' AND side='buy'")
        trade_thesis_id = cur.fetchone()[0]
    assert trade_thesis_id is not None

    with conn.cursor() as cur:
        cur.execute("""
            SELECT thesis_id, symbol, hypothesis_type, status
            FROM trade_theses WHERE id = %s
        """, (trade_thesis_id,))
        thesis_id, symbol, hypothesis_type, status = cur.fetchone()
    assert symbol == "AAPL"
    assert hypothesis_type == "mean_reversion_oversold"
    assert status == "proposed"

    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug = 'mean_reversion'")
        assert thesis_id == cur.fetchone()[0]


def test_created_trade_thesis_passes_the_pr3_validator(conn, alpaca_base):
    """End-to-end sanity: whatever compute_signals()'s live path actually
    persists must itself pass PR 3's semantic validator against the same
    live DB state -- not just be well-formed per PR 1's grammar."""
    closes = _bullish_buy_closes()
    _set_low_threshold_gate_params(conn)
    _set_signal_param(conn, "trade_thesis_instantiation_enabled", 1)
    _seed_signal_fixture(conn, "AAPL", closes)

    _run_compute_signals(conn, alpaca_base, closes)

    with conn.cursor() as cur:
        cur.execute("SELECT trade_thesis_id FROM trade_proposals WHERE symbol='AAPL' AND side='buy'")
        trade_thesis_id = cur.fetchone()[0]
    assert trade_thesis_id is not None

    with conn.cursor() as cur:
        cur.execute("""
            SELECT thesis_id, symbol, schema_version, evidence_context, hypothesis_type,
                   hypothesis_text, entry_conditions, evidence_snapshot, invalidation_spec,
                   success_spec, confidence, provenance, status, as_of
            FROM trade_theses WHERE id = %s
        """, (trade_thesis_id,))
        row = cur.fetchone()
    thesis = TradeThesis(
        thesis_id=row[0], symbol=row[1], schema_version=row[2], evidence_context=row[3],
        hypothesis_type=row[4], hypothesis_text=row[5], entry_conditions=row[6],
        evidence_snapshot=row[7], invalidation_spec=row[8], success_spec=row[9],
        confidence=float(row[10]) if row[10] is not None else None, provenance=row[11],
        status=row[12], as_of=row[13],
    )

    result = validate_trade_thesis(conn, thesis)
    assert result.is_valid, result.errors


def test_sell_proposals_never_get_a_trade_thesis_id(conn, alpaca_base):
    """PR 4 only instantiates on the BUY path (per §2a -- SELL attribution
    is deferred). A held position's stop-loss/exit sell proposals must
    never get a trade_thesis_id even with the flag on."""
    closes = _bullish_buy_closes()
    _set_low_threshold_gate_params(conn)
    _set_signal_param(conn, "trade_thesis_instantiation_enabled", 1)
    _seed_signal_fixture(conn, "AAPL", closes)

    with requests_mock.Mocker() as m:
        m.get("https://query2.finance.yahoo.com/v8/finance/chart/AAPL",
              json=_make_yahoo_chart_json(closes))
        m.get(f"{alpaca_base}/v2/account", json={"cash": "10000", "portfolio_value": "10000"})
        entry_price = closes[-1] * 1.5
        m.get(f"{alpaca_base}/v2/positions", json=[
            {
                "symbol": "AAPL", "qty": "10", "avg_entry_price": str(entry_price),
                "current_price": str(closes[-1]), "market_value": str(closes[-1] * 10),
                "unrealized_plpc": str((closes[-1] - entry_price) / entry_price),
            },
        ])
        signals.compute_signals(conn, ["AAPL"])

    with conn.cursor() as cur:
        cur.execute("SELECT trade_thesis_id FROM trade_proposals WHERE symbol='AAPL' AND side='sell'")
        rows = cur.fetchall()
    assert rows, "expected at least one sell proposal (stop-loss) from a losing position"
    assert all(r[0] is None for r in rows)

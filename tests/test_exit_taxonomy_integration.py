"""
Integration coverage for PR 8's wiring into shared/signals.py::compute_signals
-- proves thesis_invalidation_exit_enabled actually controls whether a held
position with a resolvable trade_thesis_id gets a real
exit_reason='thesis_invalidated' sell proposal, through the real
compute_signals() path.

No per-symbol price history is seeded here -- check_thesis_invalidation_sell
(like check_stop_losses/check_regime_deterioration_sell before it) runs
against the broker's live positions dict, before the per-symbol scoring
loop. compute_signals() is called with an empty symbols list so the
scoring loop itself never runs -- these tests are entirely about the
position-level exit checks.
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

for _mod in ("trade_thesis", "trade_thesis_invalidation", "market_structure", "signals"):
    sys.modules.pop(_mod, None)
import signals
import market_structure as ms
from trade_thesis import TradeThesis, record_trade_thesis

_STRUCTURE_CTX = {
    "trend": "bullish", "confidence": 80, "trend_strength": "strong", "volatility": "normal",
    "bos": False, "choch": True, "risk": "high", "summary": "test",
    "monthly": {"trend_direction": "higher_highs_higher_lows"},
    "weekly": {"trend_direction": "higher_highs_higher_lows"},
    "daily": {"trend_direction": "higher_highs_higher_lows"},
}


def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug = 'mean_reversion'")
        return cur.fetchone()[0]


def _seed_trade_thesis(conn, thesis_id, symbol="AAPL"):
    thesis = TradeThesis(
        thesis_id=thesis_id,
        symbol=symbol,
        hypothesis_type="mean_reversion_oversold",
        hypothesis_text="test seed thesis",
        entry_conditions={"feature": "technical.rsi_14", "op": "lt", "value": 30},
        # Deliberately references a feature with no seeded data (no
        # price_history in this file) -- evaluates to None/unknown on its
        # own. Invalidation in these tests comes from the structure CHoCH
        # signal below, not this spec, proving the exit check doesn't
        # depend on the raw price feed being available.
        invalidation_spec={"feature": "technical.close", "op": "lt", "value": 1.0},
        success_spec={"feature": "technical.bb_pct_b", "op": "gte", "value": 0.5},
        evidence_context={
            "as_of": date.today().isoformat(),
            "providers": {"technical": {"source": "symbol_features", "feature_version": "v1"}},
        },
        provenance={"entry_conditions": "explicit"},
        as_of=datetime.now(timezone.utc),
    )
    row_id = record_trade_thesis(conn, thesis)
    assert row_id is not None
    return row_id


def _seed_approved_buy_proposal(conn, symbol, thesis_id, trade_thesis_id):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, rationale, signal_score, decision, thesis_id, trade_thesis_id)
            VALUES (%s, 'buy', 10, 'seed', 80, 'approved', %s, %s)
        """, (symbol, thesis_id, trade_thesis_id))
    conn.commit()


def _set_signal_param(conn, key, value):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO signal_params (key, value, description) VALUES (%s, %s, '')
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, value))
    conn.commit()


@pytest.fixture(autouse=True)
def _reset_flag_after(conn):
    yield
    _set_signal_param(conn, "thesis_invalidation_exit_enabled", 0)


@pytest.fixture
def alpaca_base(monkeypatch):
    base = "https://fake-alpaca.test"
    monkeypatch.setenv("ALPACA_BASE_URL", base)
    signals.ALPACA_BASE = base
    signals.ALPACA_HEADERS = {"APCA-API-KEY-ID": "", "APCA-API-SECRET-KEY": ""}
    return base


def _run_with_position(conn, alpaca_base, symbol="AAPL", qty="10", entry=100.0, current=105.0):
    with requests_mock.Mocker() as m:
        m.get(f"{alpaca_base}/v2/account", json={"cash": "10000", "portfolio_value": "10000"})
        m.get(f"{alpaca_base}/v2/positions", json=[{
            "symbol": symbol, "qty": qty, "avg_entry_price": str(entry),
            "current_price": str(current), "market_value": str(current * float(qty)),
            "unrealized_plpc": str((current - entry) / entry),
        }])
        signals.compute_signals(conn, [])


def _sell_proposals(conn, symbol="AAPL"):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT exit_reason, trade_thesis_id, qty FROM trade_proposals
            WHERE symbol=%s AND side='sell'
        """, (symbol,))
        return cur.fetchall()


def test_no_exit_proposal_when_flag_disabled(conn, alpaca_base):
    thesis_id = _mean_reversion_thesis_id(conn)
    trade_thesis_id = _seed_trade_thesis(conn, thesis_id)
    _seed_approved_buy_proposal(conn, "AAPL", thesis_id, trade_thesis_id)
    ms.store_market_structure_day(conn, date.today(), "AAPL", _STRUCTURE_CTX)
    _set_signal_param(conn, "thesis_invalidation_exit_enabled", 0)

    _run_with_position(conn, alpaca_base)

    assert _sell_proposals(conn) == []


def test_exit_proposal_created_when_flag_enabled_and_thesis_invalidated(conn, alpaca_base):
    thesis_id = _mean_reversion_thesis_id(conn)
    trade_thesis_id = _seed_trade_thesis(conn, thesis_id)
    _seed_approved_buy_proposal(conn, "AAPL", thesis_id, trade_thesis_id)
    ms.store_market_structure_day(conn, date.today(), "AAPL", _STRUCTURE_CTX)  # choch=True
    _set_signal_param(conn, "thesis_invalidation_exit_enabled", 1)

    _run_with_position(conn, alpaca_base)

    rows = _sell_proposals(conn)
    assert len(rows) == 1
    exit_reason, linked_trade_thesis_id, qty = rows[0]
    assert exit_reason == "thesis_invalidated"
    assert linked_trade_thesis_id == trade_thesis_id
    assert float(qty) == 10.0


def test_no_exit_proposal_when_position_has_no_resolvable_thesis(conn, alpaca_base):
    # No approved BUY proposal at all for this symbol -- thesis
    # instantiation was never on, or predates PR 4. Structure CHoCH is
    # still present, but there's nothing to resolve/evaluate against.
    ms.store_market_structure_day(conn, date.today(), "AAPL", _STRUCTURE_CTX)
    _set_signal_param(conn, "thesis_invalidation_exit_enabled", 1)

    _run_with_position(conn, alpaca_base)

    assert _sell_proposals(conn) == []


def test_no_exit_proposal_when_thesis_not_invalidated(conn, alpaca_base):
    thesis_id = _mean_reversion_thesis_id(conn)
    trade_thesis_id = _seed_trade_thesis(conn, thesis_id)
    _seed_approved_buy_proposal(conn, "AAPL", thesis_id, trade_thesis_id)
    # No structure snapshot, no regime data, invalidation_spec unevaluable
    # (no price_history) -- evaluate_thesis_invalidation should report
    # invalidated=False (in fact "unknown"), not manufacture a false positive.
    _set_signal_param(conn, "thesis_invalidation_exit_enabled", 1)

    _run_with_position(conn, alpaca_base)

    assert _sell_proposals(conn) == []


def test_no_duplicate_exit_proposal_when_one_already_open(conn, alpaca_base):
    thesis_id = _mean_reversion_thesis_id(conn)
    trade_thesis_id = _seed_trade_thesis(conn, thesis_id)
    _seed_approved_buy_proposal(conn, "AAPL", thesis_id, trade_thesis_id)
    ms.store_market_structure_day(conn, date.today(), "AAPL", _STRUCTURE_CTX)
    _set_signal_param(conn, "thesis_invalidation_exit_enabled", 1)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, rationale, signal_score, thesis_id)
            VALUES ('AAPL', 'sell', 10, 'already open', 50, %s)
        """, (thesis_id,))
    conn.commit()

    _run_with_position(conn, alpaca_base)

    rows = _sell_proposals(conn)
    assert len(rows) == 1  # the pre-existing one, no new thesis_invalidated row added

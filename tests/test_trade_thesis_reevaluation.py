from datetime import date, datetime, timedelta, timezone

import pytest

from trade_thesis import TradeThesis, record_trade_thesis
from trade_thesis_reevaluation import reevaluate_active_trade_theses, reevaluate_trade_thesis

SYMBOL = "AAPL"
_AS_OF = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug = 'mean_reversion'")
        return cur.fetchone()[0]


def _seed_trade_thesis(conn, thesis_id, symbol=SYMBOL, success_spec=None, invalidation_spec=None, status="proposed"):
    thesis = TradeThesis(
        thesis_id=thesis_id,
        symbol=symbol,
        hypothesis_type="mean_reversion_oversold",
        hypothesis_text="test seed",
        entry_conditions={"feature": "technical.rsi_14", "op": "lt", "value": 30},
        invalidation_spec=invalidation_spec or {"feature": "technical.close", "op": "lt", "value": 1.0},
        success_spec=success_spec or {"feature": "technical.close", "op": "gte", "value": 999999},
        evidence_context={
            "as_of": _AS_OF.date().isoformat(),
            "providers": {"technical": {"source": "symbol_features", "feature_version": "v1"}},
        },
        provenance={"entry_conditions": "explicit"},
        as_of=_AS_OF,
        status=status,
    )
    row_id = record_trade_thesis(conn, thesis)
    assert row_id is not None
    return row_id


def _status_of(conn, trade_thesis_id):
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM trade_theses WHERE id=%s", (trade_thesis_id,))
        return cur.fetchone()[0]


def _evaluations_of(conn, trade_thesis_id):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT previous_state, state, triggering_condition, evidence_diff
            FROM trade_thesis_evaluations WHERE trade_thesis_id=%s ORDER BY id ASC
        """, (trade_thesis_id,))
        return cur.fetchall()


def test_proposed_thesis_with_no_signal_promotes_to_active(conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    trade_thesis_id = _seed_trade_thesis(conn, thesis_id)

    eval_id = reevaluate_trade_thesis(conn, trade_thesis_id, as_of=_AS_OF)

    assert eval_id is not None
    assert _status_of(conn, trade_thesis_id) == "active"
    rows = _evaluations_of(conn, trade_thesis_id)
    assert len(rows) == 1
    previous_state, state, triggering_condition, evidence_diff = rows[0]
    assert previous_state == "proposed"
    assert state == "active"
    assert triggering_condition is None


def test_already_active_thesis_with_no_signal_stays_active_and_still_logs(conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    trade_thesis_id = _seed_trade_thesis(conn, thesis_id, status="active")

    reevaluate_trade_thesis(conn, trade_thesis_id, as_of=_AS_OF)
    reevaluate_trade_thesis(conn, trade_thesis_id, as_of=_AS_OF)

    assert _status_of(conn, trade_thesis_id) == "active"
    rows = _evaluations_of(conn, trade_thesis_id)
    assert len(rows) == 2  # append-only: every evaluation writes a row, even a no-op
    assert all(r[0] == "active" and r[1] == "active" for r in rows)


def test_thesis_transitions_to_invalidated_on_structure_choch(conn):
    from market_structure import store_market_structure_day
    thesis_id = _mean_reversion_thesis_id(conn)
    trade_thesis_id = _seed_trade_thesis(conn, thesis_id, status="active")
    store_market_structure_day(conn, date(2026, 6, 1), SYMBOL, {
        "trend": "bullish", "confidence": 80, "trend_strength": "strong", "volatility": "normal",
        "bos": False, "choch": True, "risk": "high", "summary": "test",
        "monthly": {"trend_direction": "higher_highs_higher_lows"},
        "weekly": {"trend_direction": "higher_highs_higher_lows"},
        "daily": {"trend_direction": "higher_highs_higher_lows"},
    })

    eval_id = reevaluate_trade_thesis(conn, trade_thesis_id, as_of=_AS_OF)

    assert eval_id is not None
    assert _status_of(conn, trade_thesis_id) == "invalidated"
    rows = _evaluations_of(conn, trade_thesis_id)
    previous_state, state, triggering_condition, evidence_diff = rows[-1]
    assert previous_state == "active"
    assert state == "invalidated"
    assert "structure_choch" in triggering_condition
    assert "structure_choch" in evidence_diff["invalidation_reasons"]


def test_thesis_transitions_to_completed_on_success_spec(conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    trade_thesis_id = _seed_trade_thesis(
        conn, thesis_id, status="active",
        # gte a trivially-low bound -- any real close price satisfies it,
        # so this isolates "does completion actually fire on success_spec"
        # from having to reason about a specific price threshold.
        success_spec={"feature": "technical.close", "op": "gte", "value": -999999},
    )
    # technical.close still needs real price_history to evaluate at all
    # (None otherwise, which would read as "not yet met," not "met").
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
            VALUES (%s, %s, 100, 100, 100, 100, 1000000)
            ON CONFLICT (symbol, ts) DO NOTHING
        """, (SYMBOL, _AS_OF))
    conn.commit()

    eval_id = reevaluate_trade_thesis(conn, trade_thesis_id, as_of=_AS_OF)

    assert eval_id is not None
    assert _status_of(conn, trade_thesis_id) == "completed"
    rows = _evaluations_of(conn, trade_thesis_id)
    previous_state, state, triggering_condition, evidence_diff = rows[-1]
    assert state == "completed"
    assert triggering_condition == "success_spec_triggered"
    assert evidence_diff["success_spec_met"] is True


def test_terminal_thesis_is_not_reevaluated(conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    trade_thesis_id = _seed_trade_thesis(conn, thesis_id, status="invalidated")

    result = reevaluate_trade_thesis(conn, trade_thesis_id, as_of=_AS_OF)

    assert result is None
    assert _evaluations_of(conn, trade_thesis_id) == []
    assert _status_of(conn, trade_thesis_id) == "invalidated"  # unchanged


def test_reevaluate_returns_none_for_unknown_id(conn):
    assert reevaluate_trade_thesis(conn, 999999, as_of=_AS_OF) is None


def test_batch_reevaluate_skips_terminal_theses(conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    active_one = _seed_trade_thesis(conn, thesis_id, symbol="AAPL", status="proposed")
    active_two = _seed_trade_thesis(conn, thesis_id, symbol="MSFT", status="active")
    terminal_one = _seed_trade_thesis(conn, thesis_id, symbol="TSLA", status="completed")

    count = reevaluate_active_trade_theses(conn)

    assert count == 2
    assert _evaluations_of(conn, active_one) != []
    assert _evaluations_of(conn, active_two) != []
    assert _evaluations_of(conn, terminal_one) == []
    assert _status_of(conn, terminal_one) == "completed"

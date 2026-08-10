from datetime import date, datetime, timedelta, timezone

import pytest

from trade_thesis import TradeThesis
from trade_thesis_invalidation import (
    InvalidationResult,
    evaluate_condition_tree,
    evaluate_thesis_invalidation,
)

SYMBOL = "AAPL"
START = date(2026, 1, 1)

_CLOSES = [
    100, 101, 99, 102, 103, 101, 104, 105, 103, 106,
    107, 105, 108, 109, 107, 110, 111, 109, 112, 113,
    111, 114, 115, 113, 116,
]
_AS_OF_DATE = START + timedelta(days=len(_CLOSES) - 1)
_AS_OF = datetime.combine(_AS_OF_DATE, datetime.min.time(), tzinfo=timezone.utc)


def _insert_price_history(conn, symbol, closes, start=START):
    with conn.cursor() as cur:
        for i, close in enumerate(closes):
            d = start + timedelta(days=i)
            cur.execute(
                """
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, ts) DO NOTHING
                """,
                (symbol, d, close, close, close, close, 1_000_000),
            )
    conn.commit()


def _insert_market_structure(conn, symbol, trading_date, trend="bullish", choch=False):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market_structure_history (trading_date, symbol, trend, confidence, choch, bos)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (trading_date, symbol) DO UPDATE SET trend = EXCLUDED.trend, choch = EXCLUDED.choch
            """,
            (trading_date, symbol, trend, 80, choch, False),
        )
    conn.commit()


def _insert_security_regime(conn, symbol, trading_date, classification):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO security_regime_history (trading_date, symbol, classification, total_score)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (trading_date, symbol) DO UPDATE SET classification = EXCLUDED.classification
            """,
            (trading_date, symbol, classification, 0),
        )
    conn.commit()


def _insert_market_regime(conn, trading_date, overall):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO market_regime_history (trading_date, overall)
            VALUES (%s, %s)
            ON CONFLICT (trading_date) DO UPDATE SET overall = EXCLUDED.overall
            """,
            (trading_date, overall),
        )
    conn.commit()


def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug = 'mean_reversion'")
        return cur.fetchone()[0]


def _thesis(thesis_id, invalidation_spec):
    return TradeThesis(
        thesis_id=thesis_id,
        symbol=SYMBOL,
        hypothesis_type="mean_reversion_oversold",
        hypothesis_text="test thesis",
        entry_conditions={"feature": "technical.rsi_14", "op": "lt", "value": 30},
        invalidation_spec=invalidation_spec,
        success_spec={"feature": "technical.bb_pct_b", "op": "gte", "value": 0.5},
        evidence_context={
            "as_of": _AS_OF_DATE.isoformat(),
            "providers": {"technical": {"source": "symbol_features", "feature_version": "v1"}},
        },
        provenance={"entry_conditions": "explicit"},
        as_of=_AS_OF,
    )


# --- evaluate_condition_tree: leaves and operators -----------------------------

def test_leaf_evaluates_true_when_condition_holds(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    node = {"feature": "technical.close", "op": "lt", "value": 200}
    assert evaluate_condition_tree(conn, node, SYMBOL, _AS_OF) is True


def test_leaf_evaluates_false_when_condition_does_not_hold(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    node = {"feature": "technical.close", "op": "gt", "value": 200}
    assert evaluate_condition_tree(conn, node, SYMBOL, _AS_OF) is False


def test_leaf_between_operator(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    node = {"feature": "technical.close", "op": "between", "value": [100, 200]}
    assert evaluate_condition_tree(conn, node, SYMBOL, _AS_OF) is True


def test_leaf_returns_none_when_feature_unregistered(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    node = {"feature": "technical.not_real", "op": "lt", "value": 30}
    assert evaluate_condition_tree(conn, node, SYMBOL, _AS_OF) is None


def test_leaf_returns_none_when_no_data(conn):
    # No price history seeded at all.
    node = {"feature": "technical.close", "op": "lt", "value": 200}
    assert evaluate_condition_tree(conn, node, SYMBOL, _AS_OF) is None


# --- evaluate_condition_tree: three-valued and/or/not --------------------------

def test_and_all_true_is_true(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    node = {"and": [
        {"feature": "technical.close", "op": "lt", "value": 200},
        {"feature": "technical.close", "op": "gt", "value": 50},
    ]}
    assert evaluate_condition_tree(conn, node, SYMBOL, _AS_OF) is True


def test_and_one_false_is_false_even_with_unknown_sibling(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    node = {"and": [
        {"feature": "technical.close", "op": "gt", "value": 999},  # False
        {"feature": "technical.not_real", "op": "lt", "value": 1},  # None
    ]}
    assert evaluate_condition_tree(conn, node, SYMBOL, _AS_OF) is False


def test_and_unknown_with_no_false_is_none(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    node = {"and": [
        {"feature": "technical.close", "op": "lt", "value": 200},  # True
        {"feature": "technical.not_real", "op": "lt", "value": 1},  # None
    ]}
    assert evaluate_condition_tree(conn, node, SYMBOL, _AS_OF) is None


def test_or_one_true_is_true_even_with_unknown_sibling(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    node = {"or": [
        {"feature": "technical.close", "op": "lt", "value": 200},  # True
        {"feature": "technical.not_real", "op": "lt", "value": 1},  # None
    ]}
    assert evaluate_condition_tree(conn, node, SYMBOL, _AS_OF) is True


def test_or_unknown_with_no_true_is_none(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    node = {"or": [
        {"feature": "technical.close", "op": "gt", "value": 999},  # False
        {"feature": "technical.not_real", "op": "lt", "value": 1},  # None
    ]}
    assert evaluate_condition_tree(conn, node, SYMBOL, _AS_OF) is None


def test_not_negates_definite_value(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    node = {"not": {"feature": "technical.close", "op": "gt", "value": 999}}
    assert evaluate_condition_tree(conn, node, SYMBOL, _AS_OF) is True


def test_not_of_unknown_is_unknown(conn):
    node = {"not": {"feature": "technical.not_real", "op": "gt", "value": 1}}
    assert evaluate_condition_tree(conn, node, SYMBOL, _AS_OF) is None


# --- evaluate_thesis_invalidation: end to end ----------------------------------

def test_no_signals_available_reports_unknown(conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _thesis(thesis_id, {"feature": "technical.close", "op": "lt", "value": 90})

    result = evaluate_thesis_invalidation(conn, thesis, as_of=_AS_OF)
    assert result.invalidated is False
    assert result.unknown is True
    assert result.reasons == []


def test_invalidation_spec_triggered_flags_invalidated(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    thesis_id = _mean_reversion_thesis_id(conn)
    # last close is 116 -- 'lt 200' is trivially true
    thesis = _thesis(thesis_id, {"feature": "technical.close", "op": "lt", "value": 200})

    result = evaluate_thesis_invalidation(conn, thesis, as_of=_AS_OF)
    assert result.invalidated is True
    assert "invalidation_spec_triggered" in result.reasons


def test_invalidation_spec_not_triggered_is_not_invalidated_alone(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _thesis(thesis_id, {"feature": "technical.close", "op": "lt", "value": 1})

    result = evaluate_thesis_invalidation(conn, thesis, as_of=_AS_OF)
    assert result.invalidated is False
    assert result.reasons == []
    assert result.unknown is False


def test_structure_choch_flags_invalidated(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    _insert_market_structure(conn, SYMBOL, _AS_OF_DATE, trend="bullish", choch=True)
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _thesis(thesis_id, {"feature": "technical.close", "op": "lt", "value": 1})  # not triggered on its own

    result = evaluate_thesis_invalidation(conn, thesis, as_of=_AS_OF)
    assert result.invalidated is True
    assert "structure_choch" in result.reasons


def test_no_choch_does_not_flag_structure_reason(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    _insert_market_structure(conn, SYMBOL, _AS_OF_DATE, trend="bullish", choch=False)
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _thesis(thesis_id, {"feature": "technical.close", "op": "lt", "value": 1})

    result = evaluate_thesis_invalidation(conn, thesis, as_of=_AS_OF)
    assert result.invalidated is False
    assert not any("structure" in r for r in result.reasons)


def test_bearish_security_regime_flags_invalidated(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    _insert_security_regime(conn, SYMBOL, _AS_OF_DATE, "strong_bearish")
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _thesis(thesis_id, {"feature": "technical.close", "op": "lt", "value": 1})

    result = evaluate_thesis_invalidation(conn, thesis, as_of=_AS_OF)
    assert result.invalidated is True
    assert "security_regime_strong_bearish" in result.reasons


def test_bullish_security_regime_does_not_flag(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    _insert_security_regime(conn, SYMBOL, _AS_OF_DATE, "bullish")
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _thesis(thesis_id, {"feature": "technical.close", "op": "lt", "value": 1})

    result = evaluate_thesis_invalidation(conn, thesis, as_of=_AS_OF)
    assert result.invalidated is False
    assert not any(r.startswith("security_regime") for r in result.reasons)


def test_bear_market_regime_flags_invalidated(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    _insert_market_regime(conn, _AS_OF_DATE, "bear_volatile")
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _thesis(thesis_id, {"feature": "technical.close", "op": "lt", "value": 1})

    result = evaluate_thesis_invalidation(conn, thesis, as_of=_AS_OF)
    assert result.invalidated is True
    assert "market_regime_bear_volatile" in result.reasons


def test_bull_market_regime_does_not_flag(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    _insert_market_regime(conn, _AS_OF_DATE, "bull_calm")
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _thesis(thesis_id, {"feature": "technical.close", "op": "lt", "value": 1})

    result = evaluate_thesis_invalidation(conn, thesis, as_of=_AS_OF)
    assert result.invalidated is False
    assert not any(r.startswith("market_regime") for r in result.reasons)


def test_multiple_signals_all_reported(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    _insert_market_structure(conn, SYMBOL, _AS_OF_DATE, choch=True)
    _insert_security_regime(conn, SYMBOL, _AS_OF_DATE, "bearish")
    _insert_market_regime(conn, _AS_OF_DATE, "bear_calm")
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _thesis(thesis_id, {"feature": "technical.close", "op": "lt", "value": 200})  # also triggers

    result = evaluate_thesis_invalidation(conn, thesis, as_of=_AS_OF)
    assert result.invalidated is True
    assert len(result.reasons) == 4
    assert "invalidation_spec_triggered" in result.reasons
    assert "structure_choch" in result.reasons
    assert "security_regime_bearish" in result.reasons
    assert "market_regime_bear_calm" in result.reasons

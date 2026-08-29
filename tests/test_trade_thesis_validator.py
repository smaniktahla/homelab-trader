from datetime import date, datetime, timedelta, timezone

import pytest

from trade_thesis import TradeThesis
from trade_thesis_validator import extract_referenced_features, validate_trade_thesis

SYMBOL = "AAPL"
START = date(2026, 1, 1)

# 25 daily closes -- enough for both RSI(14) and BB(20) to have real values
# by the last day, same series shape as test_feature_registry.py.
_CLOSES = [
    100, 101, 99, 102, 103, 101, 104, 105, 103, 106,
    107, 105, 108, 109, 107, 110, 111, 109, 112, 113,
    111, 114, 115, 113, 116,
]
_AS_OF_INDEX = 24
_AS_OF_DATE = START + timedelta(days=_AS_OF_INDEX)


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


def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug = 'mean_reversion'")
        return cur.fetchone()[0]


def _valid_thesis(thesis_id, **overrides):
    kwargs = dict(
        thesis_id=thesis_id,
        symbol=SYMBOL,
        hypothesis_type="mean_reversion_oversold",
        hypothesis_text="RSI oversold bounce off lower Bollinger band",
        entry_conditions={
            "and": [
                {"feature": "technical.rsi_14", "op": "lt", "value": 30},
                {"feature": "technical.bb_pct_b", "op": "lte", "value": 0.1},
            ]
        },
        invalidation_spec={"feature": "technical.close", "op": "lt", "value": 90},
        success_spec={"feature": "technical.close", "op": "gte", "value": 130},
        evidence_context={
            "as_of": _AS_OF_DATE.isoformat(),
            "providers": {
                "technical": {"source": "symbol_features", "feature_version": "v1"},
            },
        },
        provenance={"entry_conditions": "explicit"},
        as_of=datetime.combine(_AS_OF_DATE, datetime.min.time(), tzinfo=timezone.utc),
    )
    kwargs.update(overrides)
    return TradeThesis(**kwargs)


# --- extract_referenced_features ---------------------------------------------

def test_extract_referenced_features_collects_across_all_three_fields(conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _valid_thesis(thesis_id)
    assert extract_referenced_features(thesis) == {
        "technical.rsi_14", "technical.bb_pct_b", "technical.close",
    }


def test_extract_referenced_features_walks_nested_combinators(conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _valid_thesis(
        thesis_id,
        entry_conditions={
            "and": [
                {"or": [
                    {"feature": "technical.rsi_14", "op": "lt", "value": 30},
                    {"feature": "technical.bb_pct_b", "op": "lte", "value": 0.1},
                ]},
                {"not": {"feature": "market_regime.overall", "op": "eq", "value": "bearish"}},
            ]
        },
    )
    features = extract_referenced_features(thesis)
    assert "market_regime.overall" in features
    assert "technical.rsi_14" in features
    assert "technical.bb_pct_b" in features


# --- happy path ----------------------------------------------------------------

def test_valid_thesis_with_seeded_history_passes_all_checks(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _valid_thesis(thesis_id)

    result = validate_trade_thesis(conn, thesis)
    assert result.is_valid, result.errors
    assert result.errors == []


# --- hypothesis_type vocabulary (PR 13 -- Hypothesis Library) ---------------
# hypothesis_type membership moved from construction-time (shared/
# trade_thesis.py) to here as of PR 13; see shared/hypothesis_library.py.
# hypothesis_types is reference/config data (not in tests/conftest.py's
# RESET_TABLES, same convention as `theses`), so any test that mutates the
# seeded row must restore it in a finally block.

def test_unregistered_hypothesis_type_fails(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _valid_thesis(thesis_id, hypothesis_type="not_a_registered_type")

    result = validate_trade_thesis(conn, thesis)
    assert not result.is_valid
    assert any("unregistered or inactive hypothesis_type 'not_a_registered_type'" in e for e in result.errors)


def test_deprecated_hypothesis_type_fails(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE hypothesis_types SET status='deprecated' WHERE type_key='mean_reversion_oversold'")
    conn.commit()
    try:
        thesis = _valid_thesis(thesis_id)
        result = validate_trade_thesis(conn, thesis)
        assert not result.is_valid
        assert any("unregistered or inactive hypothesis_type 'mean_reversion_oversold'" in e for e in result.errors)
    finally:
        with conn.cursor() as cur:
            cur.execute("UPDATE hypothesis_types SET status='active' WHERE type_key='mean_reversion_oversold'")
        conn.commit()


def test_schema_version_mismatch_hypothesis_type_fails(conn):
    # An entry that's status='active' but written against a stale grammar
    # version must still fail -- is_instantiable_hypothesis_type() requires
    # BOTH active status AND schema_version == TRADE_THESIS_SCHEMA_VERSION.
    _insert_price_history(conn, SYMBOL, _CLOSES)
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE hypothesis_types SET schema_version='v0_stale' WHERE type_key='mean_reversion_oversold'")
    conn.commit()
    try:
        thesis = _valid_thesis(thesis_id)
        result = validate_trade_thesis(conn, thesis)
        assert not result.is_valid
        assert any("unregistered or inactive hypothesis_type 'mean_reversion_oversold'" in e for e in result.errors)
    finally:
        with conn.cursor() as cur:
            cur.execute("UPDATE hypothesis_types SET schema_version='v1' WHERE type_key='mean_reversion_oversold'")
        conn.commit()


# --- vocabulary checks -----------------------------------------------------

def test_unregistered_feature_in_condition_tree_fails(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _valid_thesis(
        thesis_id,
        invalidation_spec={"feature": "technical.not_a_real_feature", "op": "lt", "value": 90},
    )

    result = validate_trade_thesis(conn, thesis)
    assert not result.is_valid
    assert any("unregistered feature 'technical.not_a_real_feature'" in e for e in result.errors)


def test_unregistered_evidence_provider_fails(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _valid_thesis(
        thesis_id,
        evidence_context={
            "as_of": _AS_OF_DATE.isoformat(),
            "providers": {"bogus_provider": {"source": "nonexistent_table"}},
        },
    )

    result = validate_trade_thesis(conn, thesis)
    assert not result.is_valid
    assert any("unregistered evidence provider 'bogus_provider'" in e for e in result.errors)


def test_provider_source_mismatch_fails(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _valid_thesis(
        thesis_id,
        evidence_context={
            "as_of": _AS_OF_DATE.isoformat(),
            "providers": {"technical": {"source": "wrong_table_name"}},
        },
    )

    result = validate_trade_thesis(conn, thesis)
    assert not result.is_valid
    assert any(
        "provider 'technical' declares source 'wrong_table_name'" in e and "symbol_features" in e
        for e in result.errors
    )


# --- data availability -------------------------------------------------------

def test_feature_with_no_price_history_fails_data_availability(conn):
    # No _insert_price_history call -- the symbol has zero rows.
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _valid_thesis(thesis_id)

    result = validate_trade_thesis(conn, thesis)
    assert not result.is_valid
    assert any("no data available as-of" in e and "technical.rsi_14" in e for e in result.errors)
    assert any("no data available as-of" in e and "technical.close" in e for e in result.errors)


def test_insufficient_history_for_rsi_but_enough_for_close_reports_only_rsi(conn):
    # 5 closes is enough for technical.close but far short of RSI(14)'s minimum.
    _insert_price_history(conn, SYMBOL, _CLOSES[:5])
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _valid_thesis(
        thesis_id,
        as_of=datetime.combine(START + timedelta(days=4), datetime.min.time(), tzinfo=timezone.utc),
        evidence_context={
            "as_of": (START + timedelta(days=4)).isoformat(),
            "providers": {"technical": {"source": "symbol_features", "feature_version": "v1"}},
        },
        entry_conditions={"feature": "technical.rsi_14", "op": "lt", "value": 30},
        invalidation_spec={"feature": "technical.close", "op": "lt", "value": 90},
        success_spec={"feature": "technical.close", "op": "gte", "value": 130},
    )

    result = validate_trade_thesis(conn, thesis)
    assert not result.is_valid
    assert any("technical.rsi_14" in e and "no data available" in e for e in result.errors)
    assert not any("technical.close" in e and "no data available" in e for e in result.errors)


# --- no-lookahead -------------------------------------------------------------

def test_evidence_context_as_of_after_thesis_as_of_fails(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    thesis_id = _mean_reversion_thesis_id(conn)
    future_date = _AS_OF_DATE + timedelta(days=5)
    thesis = _valid_thesis(
        thesis_id,
        evidence_context={
            "as_of": future_date.isoformat(),
            "providers": {"technical": {"source": "symbol_features", "feature_version": "v1"}},
        },
    )

    result = validate_trade_thesis(conn, thesis)
    assert not result.is_valid
    assert any("evidence_context.as_of" in e and "lookahead" in e for e in result.errors)


def test_provider_as_of_after_thesis_as_of_fails(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    thesis_id = _mean_reversion_thesis_id(conn)
    future_date = _AS_OF_DATE + timedelta(days=5)
    thesis = _valid_thesis(
        thesis_id,
        evidence_context={
            "as_of": _AS_OF_DATE.isoformat(),
            "providers": {
                "technical": {"source": "symbol_features", "feature_version": "v1", "as_of": future_date.isoformat()},
            },
        },
    )

    result = validate_trade_thesis(conn, thesis)
    assert not result.is_valid
    assert any("provider 'technical'.as_of" in e and "lookahead" in e for e in result.errors)


def test_provider_as_of_on_or_before_thesis_as_of_passes_lookahead_check(conn):
    _insert_price_history(conn, SYMBOL, _CLOSES)
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _valid_thesis(
        thesis_id,
        evidence_context={
            "as_of": _AS_OF_DATE.isoformat(),
            "providers": {
                "technical": {"source": "symbol_features", "feature_version": "v1", "as_of": _AS_OF_DATE.isoformat()},
            },
        },
    )

    result = validate_trade_thesis(conn, thesis)
    assert result.is_valid, result.errors


# --- result surfaces every failure, not just the first -----------------------

def test_multiple_failures_all_reported_together(conn):
    # No price history seeded (data availability fails) AND an unregistered
    # feature (vocabulary fails) AND a future evidence_context.as_of
    # (lookahead fails) -- all three must appear in one ValidationResult.
    thesis_id = _mean_reversion_thesis_id(conn)
    future_date = _AS_OF_DATE + timedelta(days=5)
    thesis = _valid_thesis(
        thesis_id,
        invalidation_spec={"feature": "technical.not_a_real_feature", "op": "lt", "value": 90},
        evidence_context={
            "as_of": future_date.isoformat(),
            "providers": {"technical": {"source": "symbol_features", "feature_version": "v1"}},
        },
    )

    result = validate_trade_thesis(conn, thesis)
    assert not result.is_valid
    assert any("unregistered feature" in e for e in result.errors)
    assert any("no data available" in e for e in result.errors)
    assert any("lookahead" in e for e in result.errors)
    assert len(result.errors) >= 3

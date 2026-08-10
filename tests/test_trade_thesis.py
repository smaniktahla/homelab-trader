import dataclasses
from datetime import datetime, timezone

import pytest

from trade_thesis import (
    TRADE_THESIS_SCHEMA_VERSION,
    GrammarError,
    TradeThesis,
    load_trade_thesis,
    record_trade_thesis,
    validate_condition_tree,
    validate_evidence_context,
)


def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug = 'mean_reversion'")
        return cur.fetchone()[0]


def _valid_thesis(**overrides):
    kwargs = dict(
        thesis_id=1,
        symbol="AAPL",
        hypothesis_type="mean_reversion_oversold",
        hypothesis_text="RSI oversold bounce off lower Bollinger band",
        entry_conditions={
            "and": [
                {"feature": "rsi_14", "op": "lt", "value": 30},
                {"feature": "bb_pct_b", "op": "lte", "value": 0.1},
            ]
        },
        invalidation_spec={"feature": "close", "op": "lt", "value": 150},
        success_spec={"feature": "close", "op": "gte", "value": 170},
        evidence_context={
            "as_of": "2026-08-10",
            "providers": {
                "technical": {"source": "symbol_features", "feature_version": "v1", "symbol_features_id": 1},
            },
        },
        provenance={"entry_conditions": "explicit"},
        as_of=datetime.now(timezone.utc),
    )
    kwargs.update(overrides)
    return TradeThesis(**kwargs)


# --- schema round-trip ------------------------------------------------------

def test_to_json_from_json_round_trip():
    thesis = _valid_thesis()
    restored = TradeThesis.from_json(thesis.to_json())
    assert restored == thesis


def test_schema_version_defaults_to_current_constant():
    assert _valid_thesis().schema_version == TRADE_THESIS_SCHEMA_VERSION


def test_defaults_are_well_formed():
    thesis = _valid_thesis()
    assert thesis.status == "proposed"
    assert thesis.evidence_snapshot == {"supporting": [], "contradictory": [], "missing": []}
    assert thesis.confidence is None


# --- grammar: condition trees ------------------------------------------------

def test_valid_leaf_condition_accepted():
    validate_condition_tree({"feature": "rsi_14", "op": "lt", "value": 30}, "entry_conditions")


def test_valid_between_condition_accepted():
    validate_condition_tree({"feature": "rsi_14", "op": "between", "value": [20, 40]}, "entry_conditions")


def test_valid_nested_and_or_not_accepted():
    tree = {
        "and": [
            {"or": [
                {"feature": "rsi_14", "op": "lt", "value": 30},
                {"feature": "bb_pct_b", "op": "lte", "value": 0.1},
            ]},
            {"not": {"feature": "market_regime", "op": "eq", "value": "bearish"}},
        ]
    }
    validate_condition_tree(tree, "entry_conditions")


@pytest.mark.parametrize("bad_node", [
    "not a dict",
    {"feature": "rsi_14", "op": "lt"},  # missing value
    {"feature": "rsi_14", "op": "lt", "value": 30, "extra": 1},  # unexpected key
    {"feature": "", "op": "lt", "value": 30},  # empty feature name
    {"feature": "rsi_14", "op": "bogus_op", "value": 30},  # illegal operator
    {"feature": "rsi_14", "op": "between", "value": 30},  # between needs a 2-list
    {"feature": "rsi_14", "op": "between", "value": [30]},  # between needs exactly 2
    {"feature": "rsi_14", "op": "between", "value": ["a", "b"]},  # non-numeric bounds
    {"feature": "rsi_14", "op": "lt", "value": {"nested": 1}},  # non-scalar value
    {"and": {"feature": "rsi_14", "op": "lt", "value": 30}},  # 'and' children must be a list
    {"and": [{"feature": "rsi_14", "op": "lt", "value": 30}]},  # 'and' needs >= 2 children
    {"and": [{"feature": "x", "op": "lt", "value": 1}], "or": []},  # multiple combinator keys
])
def test_grammar_rejects_malformed_condition(bad_node):
    with pytest.raises(GrammarError):
        validate_condition_tree(bad_node, "entry_conditions")


def test_construction_rejects_bad_operator_in_any_condition_field():
    with pytest.raises(GrammarError):
        _valid_thesis(invalidation_spec={"feature": "close", "op": "not_an_op", "value": 150})


# --- grammar: evidence_context ----------------------------------------------

def test_valid_evidence_context_accepted():
    validate_evidence_context({
        "as_of": "2026-08-10",
        "providers": {"technical": {"source": "symbol_features", "feature_version": "v1"}},
    })


@pytest.mark.parametrize("bad_context", [
    "not a dict",
    {"as_of": "2026-08-10"},  # missing providers
    {"providers": {}},  # missing as_of
    {"as_of": "", "providers": {"technical": {"source": "symbol_features"}}},  # empty as_of
    {"as_of": "2026-08-10", "providers": {}},  # empty providers
    {"as_of": "2026-08-10", "providers": {"technical": "not_a_dict"}},
    {"as_of": "2026-08-10", "providers": {"technical": {}}},  # provider missing source
    {"as_of": "2026-08-10", "providers": {"technical": {"source": ""}}},  # empty source
])
def test_grammar_rejects_malformed_evidence_context(bad_context):
    with pytest.raises(GrammarError):
        validate_evidence_context(bad_context)


# --- grammar: other fields ---------------------------------------------------

def test_construction_rejects_illegal_hypothesis_type():
    with pytest.raises(GrammarError):
        _valid_thesis(hypothesis_type="not_a_real_type")


def test_construction_rejects_illegal_status():
    with pytest.raises(GrammarError):
        _valid_thesis(status="not_a_real_status")


@pytest.mark.parametrize("bad_confidence", [-0.1, 1.1])
def test_construction_rejects_out_of_range_confidence(bad_confidence):
    with pytest.raises(GrammarError):
        _valid_thesis(confidence=bad_confidence)


def test_construction_accepts_boundary_confidence():
    assert _valid_thesis(confidence=0.0).confidence == 0.0
    assert _valid_thesis(confidence=1.0).confidence == 1.0


@pytest.mark.parametrize("bad_snapshot", [
    {"supporting": []},  # missing keys
    {"supporting": [], "contradictory": [], "missing": [], "extra": []},  # extra key
    {"supporting": "not_a_list", "contradictory": [], "missing": []},
])
def test_construction_rejects_malformed_evidence_snapshot(bad_snapshot):
    with pytest.raises(GrammarError):
        _valid_thesis(evidence_snapshot=bad_snapshot)


# --- immutability -------------------------------------------------------------

def test_dataclass_is_frozen():
    thesis = _valid_thesis()
    assert dataclasses.is_dataclass(thesis)
    with pytest.raises(dataclasses.FrozenInstanceError):
        thesis.status = "active"
    with pytest.raises(dataclasses.FrozenInstanceError):
        thesis.confidence = 0.9
    with pytest.raises(dataclasses.FrozenInstanceError):
        thesis.entry_conditions = {"feature": "x", "op": "eq", "value": 1}


def test_evolve_produces_new_validated_instance_without_mutating_original():
    thesis = _valid_thesis()
    evolved = thesis.evolve(status="active")
    assert evolved.status == "active"
    assert thesis.status == "proposed"  # original untouched
    assert evolved is not thesis


def test_evolve_still_enforces_grammar():
    thesis = _valid_thesis()
    with pytest.raises(GrammarError):
        thesis.evolve(hypothesis_type="bogus")


# --- persistence helper (fail-open, not wired into any live path) -----------

def test_record_trade_thesis_persists_and_round_trips(conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _valid_thesis(thesis_id=thesis_id)

    row_id = record_trade_thesis(conn, thesis)
    assert row_id is not None

    with conn.cursor() as cur:
        cur.execute("""
            SELECT thesis_id, symbol, schema_version, hypothesis_type, status, confidence
            FROM trade_theses WHERE id = %s
        """, (row_id,))
        row = cur.fetchone()
    assert row == (thesis_id, "AAPL", TRADE_THESIS_SCHEMA_VERSION, "mean_reversion_oversold", "proposed", None)


def test_record_trade_thesis_stores_jsonb_fields_faithfully(conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _valid_thesis(thesis_id=thesis_id)

    row_id = record_trade_thesis(conn, thesis)
    with conn.cursor() as cur:
        cur.execute("SELECT entry_conditions, evidence_context FROM trade_theses WHERE id = %s", (row_id,))
        entry_conditions, evidence_context = cur.fetchone()
    assert entry_conditions == thesis.entry_conditions
    assert evidence_context == thesis.evidence_context


def test_record_trade_thesis_fails_open_on_bad_thesis_id(conn):
    # thesis_id references theses(id) -- a value with no matching row must
    # fail the FK constraint, and the helper must swallow it (return None)
    # rather than raise, same fail-open contract as feature_store.py.
    thesis = _valid_thesis(thesis_id=999999)
    assert record_trade_thesis(conn, thesis) is None

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM trade_theses")
        assert cur.fetchone()[0] == 0


def test_record_trade_thesis_leaves_connection_usable_after_failure(conn):
    # The rollback in record_trade_thesis's except branch must not poison
    # the connection for whatever the caller does next -- same discipline
    # feature_store.py's tests hold it to.
    bad_thesis = _valid_thesis(thesis_id=999999)
    assert record_trade_thesis(conn, bad_thesis) is None

    thesis_id = _mean_reversion_thesis_id(conn)
    good_thesis = _valid_thesis(thesis_id=thesis_id)
    assert record_trade_thesis(conn, good_thesis) is not None


# --- load_trade_thesis (PR 8's read counterpart to record_trade_thesis) -----

def test_load_trade_thesis_round_trips_a_recorded_thesis(conn):
    thesis_id = _mean_reversion_thesis_id(conn)
    thesis = _valid_thesis(thesis_id=thesis_id)
    row_id = record_trade_thesis(conn, thesis)

    loaded = load_trade_thesis(conn, row_id)
    assert loaded is not None
    assert loaded.thesis_id == thesis.thesis_id
    assert loaded.symbol == thesis.symbol
    assert loaded.hypothesis_type == thesis.hypothesis_type
    assert loaded.entry_conditions == thesis.entry_conditions
    assert loaded.invalidation_spec == thesis.invalidation_spec
    assert loaded.success_spec == thesis.success_spec
    assert loaded.evidence_context == thesis.evidence_context
    assert loaded.status == thesis.status


def test_load_trade_thesis_returns_none_for_missing_id(conn):
    assert load_trade_thesis(conn, 999999) is None

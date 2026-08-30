"""
PR 14, Hypothesis-Driven Trading Architecture epic. candidate_batches/
candidates are pure generated research artifacts (comparable to
trade_theses/signals/trade_proposals), not reference data -- they're in
tests/conftest.py's RESET_TABLES, so unlike hypothesis_types no manual
per-test cleanup is needed here.
"""

from hypothesis_library import HypothesisTypeSpec, register_hypothesis_type, update_hypothesis_type
from hypothesis_candidates import (
    generate_candidates,
    get_candidate_batch,
    list_candidate_batches,
    list_candidates,
)

SEEDED_TYPE = "mean_reversion_oversold"  # {"or": [{rsi_14 < 30}, {bb_pct_b <= 0.1}]}


def _register_test_type(conn, type_key="test_candidate_type", **overrides):
    kwargs = dict(
        type_key=type_key,
        display_name="Test Candidate Type",
        description="A test-only hypothesis type for candidate generation tests.",
        category="test",
        schema_version="ignored",
        required_providers=["technical"],
        default_entry_conditions={
            "and": [
                {"feature": "technical.rsi_14", "op": "lt", "value": 30},
                {"feature": "technical.bb_pct_b", "op": "lte", "value": 0.1},
            ]
        },
        default_invalidation_spec=None,
        default_success_spec=None,
        status="active",
        version=1,
    )
    kwargs.update(overrides)
    spec = HypothesisTypeSpec(**kwargs)
    row_id = register_hypothesis_type(conn, spec)
    assert row_id is not None, "test setup: failed to register test hypothesis_type"
    return type_key


def _cleanup(conn, type_key):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM hypothesis_type_changes WHERE type_key=%s", (type_key,))
        cur.execute("DELETE FROM hypothesis_types WHERE type_key=%s", (type_key,))
    conn.commit()


# --- Cartesian product -----------------------------------------------------

def test_single_feature_sweep_produces_one_candidate_per_value(conn):
    result = generate_candidates(conn, SEEDED_TYPE, {"technical.rsi_14": [20, 25, 30]})
    assert result is not None
    batch_id, candidate_ids = result
    assert len(candidate_ids) == 3

    candidates = list_candidates(conn, batch_id)
    assert {c.parameter_values["technical.rsi_14"] for c in candidates} == {20, 25, 30}


def test_multi_feature_sweep_produces_cartesian_product(conn):
    result = generate_candidates(conn, SEEDED_TYPE, {
        "technical.rsi_14": [20, 25],
        "technical.bb_pct_b": [0.05, 0.1, 0.15],
    })
    assert result is not None
    batch_id, candidate_ids = result
    assert len(candidate_ids) == 6  # 2 x 3

    candidates = list_candidates(conn, batch_id)
    combos = {(c.parameter_values["technical.rsi_14"], c.parameter_values["technical.bb_pct_b"]) for c in candidates}
    assert combos == {(20, 0.05), (20, 0.1), (20, 0.15), (25, 0.05), (25, 0.1), (25, 0.15)}


# --- substitution correctness -----------------------------------------------

def test_substitution_only_touches_matching_leaf(conn):
    # SEEDED_TYPE's entry_conditions has an "or" of rsi_14 and bb_pct_b leaves.
    # Sweeping rsi_14 must leave the bb_pct_b leaf's value untouched.
    result = generate_candidates(conn, SEEDED_TYPE, {"technical.rsi_14": [20]})
    assert result is not None
    batch_id, _ = result
    candidates = list_candidates(conn, batch_id)
    assert len(candidates) == 1
    tree = candidates[0].entry_conditions
    leaves = tree["or"]
    rsi_leaf = next(l for l in leaves if l["feature"] == "technical.rsi_14")
    bb_leaf = next(l for l in leaves if l["feature"] == "technical.bb_pct_b")
    assert rsi_leaf["value"] == 20
    assert bb_leaf["value"] == 0.1  # unchanged from the template


def test_null_optional_specs_pass_through_as_null(conn):
    # SEEDED_TYPE has default_invalidation_spec/default_success_spec == None.
    result = generate_candidates(conn, SEEDED_TYPE, {"technical.rsi_14": [25]})
    assert result is not None
    batch_id, _ = result
    candidates = list_candidates(conn, batch_id)
    assert candidates[0].invalidation_spec is None
    assert candidates[0].success_spec is None


def test_between_leaf_left_unsubstituted(conn):
    type_key = "test_between_type"
    _cleanup(conn, type_key)
    _register_test_type(conn, type_key, default_entry_conditions={
        "feature": "technical.rsi_14", "op": "between", "value": [20, 40],
    })
    try:
        result = generate_candidates(conn, type_key, {"technical.rsi_14": [25]})
        assert result is not None
        batch_id, _ = result
        candidates = list_candidates(conn, batch_id)
        assert len(candidates) == 1
        assert candidates[0].entry_conditions == {"feature": "technical.rsi_14", "op": "between", "value": [20, 40]}
    finally:
        _cleanup(conn, type_key)


# --- provenance --------------------------------------------------------------

def test_hypothesis_type_version_frozen_at_generation_time(conn):
    type_key = "test_provenance_type"
    _cleanup(conn, type_key)
    _register_test_type(conn, type_key)
    try:
        result = generate_candidates(conn, type_key, {"technical.rsi_14": [25]})
        assert result is not None
        batch_id, _ = result

        batch = get_candidate_batch(conn, batch_id)
        assert batch.hypothesis_type_version == 1

        assert update_hypothesis_type(conn, type_key, display_name="Renamed")

        # Re-fetch: the batch must still reflect the version at generation
        # time, not the catalog's current (now bumped) version.
        batch_after = get_candidate_batch(conn, batch_id)
        assert batch_after.hypothesis_type_version == 1
        assert batch_after.schema_version == "v1"
    finally:
        _cleanup(conn, type_key)


# --- rejection cases -----------------------------------------------------------

def test_rejects_unknown_type_key(conn):
    assert generate_candidates(conn, "not_a_real_type", {"technical.rsi_14": [25]}) is None


def test_rejects_deprecated_type(conn):
    type_key = "test_deprecated_candidate_type"
    _cleanup(conn, type_key)
    _register_test_type(conn, type_key, status="active")
    try:
        assert update_hypothesis_type(conn, type_key, status="deprecated")
        assert generate_candidates(conn, type_key, {"technical.rsi_14": [25]}) is None
    finally:
        _cleanup(conn, type_key)


def test_experimental_type_is_generatable(conn):
    # allow_experimental=True is baked into generate_candidates() itself --
    # this is the research-only path that flag exists for.
    type_key = "test_experimental_candidate_type"
    _cleanup(conn, type_key)
    _register_test_type(conn, type_key, status="experimental")
    try:
        assert generate_candidates(conn, type_key, {"technical.rsi_14": [25]}) is not None
    finally:
        _cleanup(conn, type_key)


def test_rejects_type_with_no_default_entry_conditions(conn):
    type_key = "test_no_template_type"
    _cleanup(conn, type_key)
    _register_test_type(conn, type_key, default_entry_conditions=None)
    try:
        assert generate_candidates(conn, type_key, {"technical.rsi_14": [25]}) is None
    finally:
        _cleanup(conn, type_key)


def test_rejects_unmatched_sweep_feature(conn):
    # SEEDED_TYPE's templates never reference market_regime.overall.
    assert generate_candidates(conn, SEEDED_TYPE, {"market_regime.overall": ["bullish"]}) is None


def test_ema_crossover_trend_correctly_fails_generation(conn):
    # PR 16's real-world case for the "no default_entry_conditions"
    # rejection above: ema_crossover_trend is seeded with
    # default_entry_conditions=NULL on purpose (a two-EMA crossover can't
    # be expressed in the single-feature-vs-scalar condition grammar), so
    # generate_candidates() must refuse it just like any other
    # no-template type -- proving the honest-NULL registration choice
    # behaves correctly end to end, not just as a schema-level assertion.
    assert generate_candidates(conn, "ema_crossover_trend", {"technical.rsi_14": [25]}) is None


def test_no_partial_batch_left_visible_on_rejection(conn):
    before = len(list_candidate_batches(conn, hypothesis_type=SEEDED_TYPE))
    assert generate_candidates(conn, SEEDED_TYPE, {"market_regime.overall": ["bullish"]}) is None
    after = len(list_candidate_batches(conn, hypothesis_type=SEEDED_TYPE))
    assert after == before


# --- read helpers --------------------------------------------------------------

def test_get_candidate_batch_returns_none_for_unknown_id(conn):
    assert get_candidate_batch(conn, 999999) is None


def test_list_candidates_empty_for_unknown_batch(conn):
    assert list_candidates(conn, 999999) == []


def test_list_candidate_batches_filters_by_hypothesis_type(conn):
    generate_candidates(conn, SEEDED_TYPE, {"technical.rsi_14": [25]})
    batches = list_candidate_batches(conn, hypothesis_type=SEEDED_TYPE)
    assert all(b.hypothesis_type == SEEDED_TYPE for b in batches)
    assert len(batches) >= 1

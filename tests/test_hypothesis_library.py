"""
PR 13, Hypothesis-Driven Trading Architecture epic. hypothesis_types is
reference/config data seeded by ingest/schema.sql's own INSERT ... ON
CONFLICT (same convention as `theses`) -- NOT in tests/conftest.py's
RESET_TABLES, so it persists as-is across the test session. Every test here
that registers or mutates a row uses a test_-prefixed type_key and cleans up
in a finally block, matching that convention rather than fighting it.
"""

from trade_thesis import TRADE_THESIS_SCHEMA_VERSION
from hypothesis_library import (
    HypothesisTypeSpec,
    get_hypothesis_type,
    hypothesis_type_exists,
    is_instantiable_hypothesis_type,
    is_legal_hypothesis_type,
    list_hypothesis_types,
    register_hypothesis_type,
    update_hypothesis_type,
)

SEEDED_TYPE_KEYS = {"mean_reversion_oversold", "mean_reversion_overbought"}
# PR 16 seed rows -- kept as a separate constant (not merged into
# SEEDED_TYPE_KEYS) so ema_crossover_trend's deliberate
# default_entry_conditions=None can be asserted specifically without
# complicating the PR13-era generic-loop test above.
PR16_TYPE_KEYS = {"bollinger_breakout_continuation", "ema_crossover_trend"}
# Price Structure epic PR G seed rows, built on PR A/B/B2's structural_
# zones/structural_events infrastructure via PR C's feature_registry
# providers.
PRICE_STRUCTURE_TYPE_KEYS = {
    "structural_support_bounce", "structural_breakout_momentum", "fvg_reaction_momentum",
}


def _cleanup(conn, type_key):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM hypothesis_type_changes WHERE type_key=%s", (type_key,))
        cur.execute("DELETE FROM hypothesis_types WHERE type_key=%s", (type_key,))
    conn.commit()


def _spec(type_key="test_hypothesis_type", **overrides):
    kwargs = dict(
        type_key=type_key,
        display_name="Test Hypothesis",
        description="A test-only hypothesis type template.",
        category="test",
        schema_version="ignored_should_be_overwritten_by_register",
        required_providers=["technical"],
        default_entry_conditions=None,
        default_invalidation_spec=None,
        default_success_spec=None,
        status="active",
        version=1,
    )
    kwargs.update(overrides)
    return HypothesisTypeSpec(**kwargs)


# --- seed data -----------------------------------------------------------------

def test_seeded_types_are_active_and_instantiable(conn):
    for type_key in SEEDED_TYPE_KEYS:
        assert is_legal_hypothesis_type(conn, type_key)
        assert is_instantiable_hypothesis_type(conn, type_key)
        spec = get_hypothesis_type(conn, type_key)
        assert spec.status == "active"
        assert spec.schema_version == TRADE_THESIS_SCHEMA_VERSION


def test_pr16_seeded_types_are_active_and_instantiable(conn):
    for type_key in PR16_TYPE_KEYS:
        assert is_legal_hypothesis_type(conn, type_key)
        assert is_instantiable_hypothesis_type(conn, type_key)
        spec = get_hypothesis_type(conn, type_key)
        assert spec.status == "active"
        assert spec.schema_version == TRADE_THESIS_SCHEMA_VERSION


def test_bollinger_breakout_continuation_has_a_condition_tree_template(conn):
    spec = get_hypothesis_type(conn, "bollinger_breakout_continuation")
    assert spec.default_entry_conditions == {"feature": "technical.bb_pct_b", "op": "gt", "value": 1.0}
    assert spec.default_invalidation_spec == {"feature": "technical.bb_pct_b", "op": "lt", "value": 0.0}


def test_ema_crossover_trend_has_no_condition_tree_template(conn):
    # Deliberately NULL -- the condition-tree grammar can't express a
    # feature-vs-feature crossover, so registering an approximate
    # single-feature condition would be actively misleading. See
    # shared/ema_crossover_strategy.py for the real (Python) semantics.
    spec = get_hypothesis_type(conn, "ema_crossover_trend")
    assert spec.default_entry_conditions is None
    assert spec.default_invalidation_spec is None


def test_price_structure_seeded_types_are_active_and_instantiable(conn):
    for type_key in PRICE_STRUCTURE_TYPE_KEYS:
        assert is_legal_hypothesis_type(conn, type_key)
        assert is_instantiable_hypothesis_type(conn, type_key)
        spec = get_hypothesis_type(conn, type_key)
        assert spec.status == "active"
        assert spec.schema_version == TRADE_THESIS_SCHEMA_VERSION
        assert all(p in ("structural_zones", "structural_events") for p in spec.required_providers)


def test_structural_support_bounce_has_a_condition_tree_template(conn):
    spec = get_hypothesis_type(conn, "structural_support_bounce")
    assert spec.default_entry_conditions == {
        "feature": "structural_zones.nearest_support_distance_atr", "op": "lt", "value": 0.5}
    assert spec.default_invalidation_spec == {
        "feature": "structural_zones.nearest_support_distance_atr", "op": "gt", "value": 2.0}


def test_structural_breakout_momentum_has_a_condition_tree_template(conn):
    spec = get_hypothesis_type(conn, "structural_breakout_momentum")
    assert spec.default_entry_conditions == {
        "feature": "structural_events.recent_event_type", "op": "eq", "value": "breakout"}
    assert spec.default_invalidation_spec == {
        "feature": "structural_events.recent_event_type", "op": "eq", "value": "failed_breakout"}


def test_fvg_reaction_momentum_has_a_condition_tree_template(conn):
    spec = get_hypothesis_type(conn, "fvg_reaction_momentum")
    assert spec.default_entry_conditions == {
        "feature": "structural_events.recent_event_type", "op": "eq", "value": "fvg_midpoint_reached"}
    assert spec.default_invalidation_spec == {
        "feature": "structural_events.recent_event_type", "op": "eq", "value": "fvg_invalidated"}


# --- list / get / exists --------------------------------------------------------

def test_list_hypothesis_types_returns_seeded_rows(conn):
    types = {s.type_key for s in list_hypothesis_types(conn)}
    assert SEEDED_TYPE_KEYS <= types


def test_list_hypothesis_types_filters_by_status(conn):
    active = list_hypothesis_types(conn, status="active")
    assert all(s.status == "active" for s in active)
    assert SEEDED_TYPE_KEYS <= {s.type_key for s in active}


def test_get_hypothesis_type_returns_none_for_unknown_key(conn):
    assert get_hypothesis_type(conn, "not_a_real_key") is None


def test_hypothesis_type_exists_true_for_seeded_row(conn):
    assert hypothesis_type_exists(conn, "mean_reversion_oversold")


def test_hypothesis_type_exists_false_for_unknown_key(conn):
    assert not hypothesis_type_exists(conn, "not_a_real_key")


def test_hypothesis_type_exists_true_for_deprecated_row_but_not_legal(conn):
    # exists() answers "is this in the catalog at all" (research tooling can
    # browse deprecated entries); is_legal_hypothesis_type() answers "is this
    # usable right now" -- deliberately different questions, see module
    # docstring.
    type_key = "test_exists_deprecated_type"
    _cleanup(conn, type_key)
    row_id = register_hypothesis_type(conn, _spec(type_key, status="deprecated"))
    try:
        assert row_id is not None
        assert hypothesis_type_exists(conn, type_key)
        assert not is_legal_hypothesis_type(conn, type_key)
    finally:
        _cleanup(conn, type_key)


# --- is_legal_hypothesis_type / is_instantiable_hypothesis_type ---------------

def test_is_legal_false_for_unknown_key(conn):
    assert not is_legal_hypothesis_type(conn, "not_a_real_key")


def test_deprecated_type_is_not_legal_or_instantiable(conn):
    type_key = "test_deprecated_type"
    _cleanup(conn, type_key)
    row_id = register_hypothesis_type(conn, _spec(type_key, status="active"))
    try:
        assert row_id is not None
        assert update_hypothesis_type(conn, type_key, status="deprecated")
        assert not is_legal_hypothesis_type(conn, type_key)
        assert not is_instantiable_hypothesis_type(conn, type_key)
    finally:
        _cleanup(conn, type_key)


def test_experimental_type_rejected_by_default_but_allowed_for_research(conn):
    type_key = "test_experimental_type"
    _cleanup(conn, type_key)
    row_id = register_hypothesis_type(conn, _spec(type_key, status="experimental"))
    try:
        assert row_id is not None
        assert not is_legal_hypothesis_type(conn, type_key)
        assert not is_instantiable_hypothesis_type(conn, type_key)  # default allow_experimental=False
        assert is_instantiable_hypothesis_type(conn, type_key, allow_experimental=True)
    finally:
        _cleanup(conn, type_key)


def test_stale_schema_version_is_not_instantiable_even_if_active(conn):
    # Both status='active' AND schema_version match are required.
    type_key = "test_stale_schema_type"
    _cleanup(conn, type_key)
    row_id = register_hypothesis_type(conn, _spec(type_key, status="active"))
    try:
        assert row_id is not None
        with conn.cursor() as cur:
            cur.execute("UPDATE hypothesis_types SET schema_version='v0_stale' WHERE type_key=%s", (type_key,))
        conn.commit()

        assert hypothesis_type_exists(conn, type_key)  # still catalog-visible
        assert not is_legal_hypothesis_type(conn, type_key)
        assert not is_instantiable_hypothesis_type(conn, type_key, allow_experimental=True)
    finally:
        _cleanup(conn, type_key)


# --- register_hypothesis_type ---------------------------------------------------

def test_register_hypothesis_type_persists_and_stamps_current_schema_version(conn):
    type_key = "test_register_type"
    _cleanup(conn, type_key)
    spec = _spec(type_key, schema_version="totally_wrong_version")
    row_id = register_hypothesis_type(conn, spec)
    try:
        assert row_id is not None
        stored = get_hypothesis_type(conn, type_key)
        assert stored.schema_version == TRADE_THESIS_SCHEMA_VERSION
        assert stored.display_name == "Test Hypothesis"
        assert stored.version == 1
    finally:
        _cleanup(conn, type_key)


def test_register_hypothesis_type_rejects_unregistered_provider(conn):
    type_key = "test_bad_provider_type"
    _cleanup(conn, type_key)
    spec = _spec(type_key, required_providers=["not_a_real_provider"])
    row_id = register_hypothesis_type(conn, spec)
    assert row_id is None
    assert get_hypothesis_type(conn, type_key) is None


def test_register_hypothesis_type_rejects_malformed_condition_template(conn):
    type_key = "test_bad_template_type"
    _cleanup(conn, type_key)
    spec = _spec(type_key, default_entry_conditions={"feature": "x", "op": "bogus_op", "value": 1})
    row_id = register_hypothesis_type(conn, spec)
    assert row_id is None
    assert get_hypothesis_type(conn, type_key) is None


def test_register_hypothesis_type_duplicate_key_fails_open(conn):
    row_id = register_hypothesis_type(conn, _spec("mean_reversion_oversold"))
    assert row_id is None  # UNIQUE violation on type_key, fail-open (no raise)
    # Seeded row must be untouched.
    seeded = get_hypothesis_type(conn, "mean_reversion_oversold")
    assert seeded.display_name != "Test Hypothesis"


# --- update_hypothesis_type ------------------------------------------------------

def test_update_hypothesis_type_bumps_version_and_updated_at(conn):
    type_key = "test_update_type"
    _cleanup(conn, type_key)
    register_hypothesis_type(conn, _spec(type_key))
    try:
        before = get_hypothesis_type(conn, type_key)
        assert before.version == 1

        assert update_hypothesis_type(conn, type_key, display_name="Updated Name")

        after = get_hypothesis_type(conn, type_key)
        assert after.version == 2
        assert after.display_name == "Updated Name"
    finally:
        _cleanup(conn, type_key)


def test_update_hypothesis_type_returns_false_for_unknown_key(conn):
    assert not update_hypothesis_type(conn, "not_a_real_key", display_name="x")


def test_update_hypothesis_type_rejects_invalid_change_without_partial_write(conn):
    type_key = "test_reject_update_type"
    _cleanup(conn, type_key)
    register_hypothesis_type(conn, _spec(type_key))
    try:
        ok = update_hypothesis_type(
            conn, type_key,
            display_name="Should Not Stick",
            required_providers=["not_a_real_provider"],
        )
        assert not ok

        after = get_hypothesis_type(conn, type_key)
        assert after.version == 1
        assert after.display_name == "Test Hypothesis"  # unchanged -- no partial write
        assert after.required_providers == ["technical"]
    finally:
        _cleanup(conn, type_key)


def test_update_hypothesis_type_no_writable_fields_returns_false(conn):
    type_key = "test_noop_update_type"
    _cleanup(conn, type_key)
    register_hypothesis_type(conn, _spec(type_key))
    try:
        # schema_version/version are not caller-writable.
        assert not update_hypothesis_type(conn, type_key, schema_version="v2", version=99)
        assert get_hypothesis_type(conn, type_key).version == 1
    finally:
        _cleanup(conn, type_key)


def test_update_hypothesis_type_records_change_history(conn):
    type_key = "test_history_type"
    _cleanup(conn, type_key)
    register_hypothesis_type(conn, _spec(type_key))
    try:
        assert update_hypothesis_type(conn, type_key, display_name="New Name", changed_by="tester")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT version, changed_by, previous_value, new_value FROM hypothesis_type_changes WHERE type_key=%s",
                (type_key,),
            )
            row = cur.fetchone()
        assert row is not None
        version, changed_by, previous_value, new_value = row
        assert version == 2
        assert changed_by == "tester"
        assert previous_value["display_name"] == "Test Hypothesis"
        assert new_value["display_name"] == "New Name"
    finally:
        _cleanup(conn, type_key)

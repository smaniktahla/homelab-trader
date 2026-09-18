"""
SI-1, Strategy Incubator epic Phase 1 ("Foundations"). strategies/
strategy_versions/strategy_version_transitions are generated research
objects (comparable to candidate_batches/candidates), not reference data
-- they're in tests/conftest.py's RESET_TABLES, so no manual per-test
cleanup is needed here, same convention as tests/test_hypothesis_candidates.py.
"""

from hypothesis_candidates import generate_candidates
from strategy_lifecycle import (
    STATUSES,
    VALID_TRANSITIONS,
    freeze,
    get_strategy_by_name,
    get_strategy_version,
    is_valid_transition,
    list_strategy_versions,
    register_candidate_as_strategy_version,
    register_strategy,
    register_strategy_version,
    transition,
)

SEEDED_HYPOTHESIS_TYPE = "mean_reversion_oversold"


def _new_strategy(conn, name="test_strategy", family="test_family"):
    strategy_id = register_strategy(conn, name, family, description="A test-only strategy")
    assert strategy_id is not None
    return strategy_id


# --- is_valid_transition (pure, no DB) ---------------------------------------

def test_full_linear_chain_is_all_valid():
    chain = ["RESEARCH", "BACKTEST", "VALIDATION", "WALK_FORWARD", "FROZEN",
             "PAPER_FORWARD", "SHADOW_LIVE", "APPROVED", "LIVE", "MONITORED", "RETIRED"]
    for a, b in zip(chain, chain[1:]):
        assert is_valid_transition(a, b), f"{a} -> {b} should be valid"


def test_every_pre_live_state_can_reject():
    for state in ("RESEARCH", "BACKTEST", "VALIDATION", "WALK_FORWARD", "FROZEN",
                  "PAPER_FORWARD", "SHADOW_LIVE", "APPROVED"):
        assert is_valid_transition(state, "REJECTED")


def test_live_and_monitored_can_suspend_or_retire():
    assert is_valid_transition("LIVE", "SUSPENDED")
    assert is_valid_transition("LIVE", "RETIRED")
    assert is_valid_transition("MONITORED", "SUSPENDED")
    assert is_valid_transition("MONITORED", "RETIRED")


def test_suspended_can_only_retire():
    assert is_valid_transition("SUSPENDED", "RETIRED")
    assert not is_valid_transition("SUSPENDED", "LIVE")


def test_terminal_states_have_no_outgoing_transitions():
    assert VALID_TRANSITIONS["REJECTED"] == frozenset()
    assert VALID_TRANSITIONS["RETIRED"] == frozenset()
    assert not is_valid_transition("REJECTED", "RESEARCH")
    assert not is_valid_transition("RETIRED", "LIVE")


def test_illegal_skips_are_rejected():
    # Can't skip stages forward...
    assert not is_valid_transition("RESEARCH", "FROZEN")
    assert not is_valid_transition("RESEARCH", "LIVE")
    # ...or go backward.
    assert not is_valid_transition("LIVE", "RESEARCH")
    assert not is_valid_transition("FROZEN", "WALK_FORWARD")


def test_unknown_status_is_never_valid():
    assert not is_valid_transition("NOT_A_REAL_STATUS", "RESEARCH")
    assert not is_valid_transition("RESEARCH", "NOT_A_REAL_STATUS")


def test_every_status_in_transition_table_is_a_known_status():
    # VALID_TRANSITIONS and STATUSES must stay in lockstep -- a status
    # missing from one or the other would silently break is_valid_transition.
    assert set(VALID_TRANSITIONS.keys()) == STATUSES
    for targets in VALID_TRANSITIONS.values():
        assert targets <= STATUSES


# --- register_strategy / register_strategy_version ---------------------------

def test_register_strategy_and_lookup_by_name(conn):
    strategy_id = _new_strategy(conn, name="mean_reversion_test")
    row = get_strategy_by_name(conn, "mean_reversion_test")
    assert row is not None
    assert row[0] == strategy_id
    assert row[1] == "mean_reversion_test"
    assert row[2] == "test_family"


def test_register_strategy_duplicate_name_fails(conn):
    _new_strategy(conn, name="dup_strategy")
    assert register_strategy(conn, "dup_strategy", "test_family") is None


def test_register_strategy_version_starts_at_research_status(conn):
    strategy_id = _new_strategy(conn)
    version_id = register_strategy_version(conn, strategy_id)
    assert version_id is not None
    sv = get_strategy_version(conn, version_id)
    assert sv.status == "RESEARCH"
    assert sv.version_number == 1
    assert sv.strategy_id == strategy_id


def test_register_strategy_version_auto_increments_version_number(conn):
    strategy_id = _new_strategy(conn)
    v1 = register_strategy_version(conn, strategy_id)
    v2 = register_strategy_version(conn, strategy_id)
    v3 = register_strategy_version(conn, strategy_id)
    numbers = [get_strategy_version(conn, v).version_number for v in (v1, v2, v3)]
    assert numbers == [1, 2, 3]


def test_register_strategy_version_carries_provenance_fields(conn):
    strategy_id = _new_strategy(conn)
    version_id = register_strategy_version(
        conn, strategy_id, description="from a candidate sweep",
        created_by="test_actor", git_commit="abc123",
        hypothesis_type="mean_reversion_oversold", hypothesis_type_version=1,
    )
    sv = get_strategy_version(conn, version_id)
    assert sv.description == "from a candidate sweep"
    assert sv.created_by == "test_actor"
    assert sv.git_commit == "abc123"
    assert sv.hypothesis_type == "mean_reversion_oversold"
    assert sv.hypothesis_type_version == 1


def test_list_strategy_versions_returns_all_versions_in_order(conn):
    strategy_id = _new_strategy(conn)
    register_strategy_version(conn, strategy_id)
    register_strategy_version(conn, strategy_id)
    versions = list_strategy_versions(conn, strategy_id)
    assert [v.version_number for v in versions] == [1, 2]


def test_list_strategy_versions_empty_for_unknown_strategy(conn):
    assert list_strategy_versions(conn, 999999) == []


def test_get_strategy_version_none_for_unknown_id(conn):
    assert get_strategy_version(conn, 999999) is None


# --- transition() -------------------------------------------------------------

def test_valid_transition_updates_status_and_writes_audit_row(conn):
    strategy_id = _new_strategy(conn)
    version_id = register_strategy_version(conn, strategy_id)
    assert transition(conn, version_id, "BACKTEST", actor="tester", reason="starting backtest")

    sv = get_strategy_version(conn, version_id)
    assert sv.status == "BACKTEST"

    with conn.cursor() as cur:
        cur.execute("""
            SELECT from_status, to_status, actor, reason, metadata
            FROM strategy_version_transitions WHERE strategy_version_id=%s
        """, (version_id,))
        rows = cur.fetchall()
    assert len(rows) == 1
    from_status, to_status, actor, reason, metadata = rows[0]
    assert (from_status, to_status, actor, reason) == ("RESEARCH", "BACKTEST", "tester", "starting backtest")
    assert metadata == {}


def test_illegal_transition_is_rejected_and_status_unchanged(conn):
    strategy_id = _new_strategy(conn)
    version_id = register_strategy_version(conn, strategy_id)
    assert not transition(conn, version_id, "LIVE")  # can't skip straight to LIVE

    sv = get_strategy_version(conn, version_id)
    assert sv.status == "RESEARCH"  # unchanged

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM strategy_version_transitions WHERE strategy_version_id=%s", (version_id,))
        count = cur.fetchone()[0]
    assert count == 0  # no audit row for a rejected transition


def test_transition_unknown_strategy_version_id_fails(conn):
    assert not transition(conn, 999999, "BACKTEST")


def test_transition_unknown_to_status_fails(conn):
    strategy_id = _new_strategy(conn)
    version_id = register_strategy_version(conn, strategy_id)
    assert not transition(conn, version_id, "NOT_A_REAL_STATUS")


def test_full_chain_to_rejected_at_each_step_writes_one_audit_row_each(conn):
    strategy_id = _new_strategy(conn)
    version_id = register_strategy_version(conn, strategy_id)
    assert transition(conn, version_id, "BACKTEST")
    assert transition(conn, version_id, "VALIDATION")
    assert transition(conn, version_id, "REJECTED", reason="failed validation")

    sv = get_strategy_version(conn, version_id)
    assert sv.status == "REJECTED"
    assert not is_valid_transition("REJECTED", "BACKTEST")  # terminal

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM strategy_version_transitions WHERE strategy_version_id=%s", (version_id,))
        count = cur.fetchone()[0]
    assert count == 3


def test_metadata_round_trips_through_transition(conn):
    strategy_id = _new_strategy(conn)
    version_id = register_strategy_version(conn, strategy_id)
    transition(conn, version_id, "BACKTEST", metadata={"note": "sample metadata", "n": 5})

    with conn.cursor() as cur:
        cur.execute("SELECT metadata FROM strategy_version_transitions WHERE strategy_version_id=%s", (version_id,))
        metadata = cur.fetchone()[0]
    assert metadata == {"note": "sample metadata", "n": 5}


# --- freeze() -------------------------------------------------------------------

def _walk_to(conn, version_id, target_status):
    chain = ["RESEARCH", "BACKTEST", "VALIDATION", "WALK_FORWARD", "FROZEN",
             "PAPER_FORWARD", "SHADOW_LIVE", "APPROVED", "LIVE", "MONITORED", "RETIRED"]
    for a, b in zip(chain, chain[1:]):
        if a == target_status:
            return
        assert transition(conn, version_id, b), f"failed to reach {b}"
        if b == target_status:
            return


def test_freeze_requires_walk_forward_status(conn):
    strategy_id = _new_strategy(conn)
    version_id = register_strategy_version(conn, strategy_id)
    # Still in RESEARCH -- freeze must fail.
    assert not freeze(conn, version_id, "codehash123", "paramhash456")
    assert get_strategy_version(conn, version_id).status == "RESEARCH"


def test_freeze_succeeds_from_walk_forward_and_stamps_hashes(conn):
    strategy_id = _new_strategy(conn)
    version_id = register_strategy_version(conn, strategy_id)
    _walk_to(conn, version_id, "WALK_FORWARD")

    assert freeze(conn, version_id, "codehash123", "paramhash456", actor="tester")

    sv = get_strategy_version(conn, version_id)
    assert sv.status == "FROZEN"
    assert sv.code_hash == "codehash123"
    assert sv.parameter_hash == "paramhash456"
    assert sv.parameter_frozen_at is not None


def test_freeze_rejects_empty_hashes(conn):
    strategy_id = _new_strategy(conn)
    version_id = register_strategy_version(conn, strategy_id)
    _walk_to(conn, version_id, "WALK_FORWARD")

    assert not freeze(conn, version_id, "", "paramhash456")
    assert not freeze(conn, version_id, "codehash123", "")
    assert get_strategy_version(conn, version_id).status == "WALK_FORWARD"


def test_full_chain_to_live_and_monitored(conn):
    strategy_id = _new_strategy(conn)
    version_id = register_strategy_version(conn, strategy_id)
    _walk_to(conn, version_id, "WALK_FORWARD")
    assert freeze(conn, version_id, "ch", "ph")
    for status in ("PAPER_FORWARD", "SHADOW_LIVE", "APPROVED", "LIVE", "MONITORED", "RETIRED"):
        assert transition(conn, version_id, status), f"failed reaching {status}"
    assert get_strategy_version(conn, version_id).status == "RETIRED"

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM strategy_version_transitions WHERE strategy_version_id=%s", (version_id,))
        count = cur.fetchone()[0]
    # WALK_FORWARD reached via 3 transitions (RESEARCH->BACKTEST->VALIDATION->WALK_FORWARD)
    # + freeze (1) + the 6 transitions in the loop above = 10
    assert count == 10


# --- register_candidate_as_strategy_version() (SI-3) --------------------------

def test_register_candidate_as_strategy_version_copies_provenance(conn):
    strategy_id = _new_strategy(conn)
    result = generate_candidates(conn, SEEDED_HYPOTHESIS_TYPE, {"technical.rsi_14": [25]})
    assert result is not None
    batch_id, candidate_ids = result

    version_id = register_candidate_as_strategy_version(conn, candidate_ids[0], strategy_id, actor="tester")
    assert version_id is not None

    sv = get_strategy_version(conn, version_id)
    assert sv.status == "RESEARCH"
    assert sv.strategy_id == strategy_id
    assert sv.candidate_id == candidate_ids[0]
    assert sv.hypothesis_type == SEEDED_HYPOTHESIS_TYPE
    assert sv.hypothesis_type_version == 1
    assert sv.created_by == "tester"


def test_register_candidate_as_strategy_version_unknown_candidate_fails(conn):
    strategy_id = _new_strategy(conn)
    assert register_candidate_as_strategy_version(conn, 999999, strategy_id) is None


def test_register_candidate_as_strategy_version_unknown_strategy_fails(conn):
    result = generate_candidates(conn, SEEDED_HYPOTHESIS_TYPE, {"technical.rsi_14": [25]})
    assert result is not None
    _, candidate_ids = result
    assert register_candidate_as_strategy_version(conn, candidate_ids[0], 999999) is None


def test_registered_candidate_version_can_advance_through_lifecycle(conn):
    # Confirms the registered strategy_version behaves exactly like a
    # hand-created one from here on -- no special-casing in transition().
    strategy_id = _new_strategy(conn)
    result = generate_candidates(conn, SEEDED_HYPOTHESIS_TYPE, {"technical.rsi_14": [25]})
    assert result is not None
    _, candidate_ids = result
    version_id = register_candidate_as_strategy_version(conn, candidate_ids[0], strategy_id)

    assert transition(conn, version_id, "BACKTEST")
    assert get_strategy_version(conn, version_id).status == "BACKTEST"

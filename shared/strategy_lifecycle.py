"""
Strategy Lifecycle, SI-1 of the Strategy Incubator epic's Phase 1
("Foundations") -- see docs/strategy-incubator-phase1-foundations-
reconciliation.md for the full design. Introduces `strategies` (the
strategy-family level, e.g. "mean_reversion") and `strategy_versions` (one
immutable-once-frozen attempt at that family, tracked through an explicit
lifecycle) one layer above shared/hypothesis_candidates.py's `Candidate`
concept (Hypothesis-Driven Trading epic, PR14).

Not wired to candidates/candidate_batches in this PR -- that's SI-3's job
(register_candidate_as_strategy_version()), kept separate so this schema/
object-model PR stays dark and inspectable on its own, same staging
shared/trade_thesis.py (PR1) and shared/backtest_engine.py (PR15) both
used before anything called them from a live or research path. No API
either (SI-2).

Lifecycle, per the Strategy Incubator spec's §1:
    RESEARCH -> BACKTEST -> VALIDATION -> WALK_FORWARD -> FROZEN ->
    PAPER_FORWARD -> SHADOW_LIVE -> APPROVED -> LIVE -> MONITORED -> RETIRED
A version may transition to REJECTED from any pre-LIVE state (the spec's
own wording). LIVE/MONITORED may transition to SUSPENDED or RETIRED.
SUSPENDED -> RETIRED is the only outgoing edge modeled for SUSPENDED in
this PR -- whether a suspended version can resume to LIVE is a policy
decision for a later phase, not decided here (the spec itself doesn't
specify SUSPENDED's outgoing edges). REJECTED and RETIRED are terminal.

VALID_TRANSITIONS is the single source of truth is_valid_transition() and
transition() both read from -- satisfies the spec's §28 testing
requirement ("valid transitions succeed... invalid ones fail") as a pure,
directly unit-testable function, not something only exercised via a live
DB round-trip.

freeze() is the spec's §9 explicit Freeze operation: a thin wrapper over
transition(..., to_status="FROZEN") that additionally requires
code_hash/parameter_hash. Neither is computed here -- the caller supplies
them, derived from the exact code/params being frozen at the moment of the
call. This module has no opinion on how a hash is computed; that's a
concern for whatever code path eventually calls freeze() (SI-3 or later).
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger(__name__)

STATUSES = frozenset({
    "RESEARCH", "BACKTEST", "VALIDATION", "WALK_FORWARD", "FROZEN",
    "PAPER_FORWARD", "SHADOW_LIVE", "APPROVED", "LIVE", "MONITORED",
    "REJECTED", "SUSPENDED", "RETIRED",
})

# Every pre-LIVE state may additionally transition to REJECTED -- spelled
# out per-state below rather than special-cased in is_valid_transition(),
# so the full transition table is readable in one place.
VALID_TRANSITIONS = {
    "RESEARCH": frozenset({"BACKTEST", "REJECTED"}),
    "BACKTEST": frozenset({"VALIDATION", "REJECTED"}),
    "VALIDATION": frozenset({"WALK_FORWARD", "REJECTED"}),
    "WALK_FORWARD": frozenset({"FROZEN", "REJECTED"}),
    "FROZEN": frozenset({"PAPER_FORWARD", "REJECTED"}),
    "PAPER_FORWARD": frozenset({"SHADOW_LIVE", "REJECTED"}),
    "SHADOW_LIVE": frozenset({"APPROVED", "REJECTED"}),
    "APPROVED": frozenset({"LIVE", "REJECTED"}),
    "LIVE": frozenset({"MONITORED", "SUSPENDED", "RETIRED"}),
    "MONITORED": frozenset({"SUSPENDED", "RETIRED"}),
    "SUSPENDED": frozenset({"RETIRED"}),
    "REJECTED": frozenset(),
    "RETIRED": frozenset(),
}


def is_valid_transition(from_status, to_status):
    """Pure function, no DB access -- unit-testable independent of any
    running Postgres. False for an unknown from_status/to_status rather
    than raising, matching this repo's fail-open convention for
    membership checks (e.g. hypothesis_library.is_legal_hypothesis_type())."""
    return to_status in VALID_TRANSITIONS.get(from_status, frozenset())


@dataclass(frozen=True)
class StrategyVersion:
    id: int
    strategy_id: int
    version_number: int
    status: str
    created_at: datetime
    description: str | None = None
    created_by: str | None = None
    git_commit: str | None = None
    code_hash: str | None = None
    parameter_hash: str | None = None
    parameter_frozen_at: datetime | None = None
    parent_strategy_version_id: int | None = None
    hypothesis_type: str | None = None
    hypothesis_type_version: int | None = None
    candidate_id: int | None = None


def register_strategy(conn, strategy_name, strategy_family, description=None):
    """Creates a new strategies row. Returns the new id, or None on
    failure (e.g. strategy_name already exists) -- fail-open, matching
    hypothesis_library.register_hypothesis_type()'s contract."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO strategies (strategy_name, strategy_family, description)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (strategy_name, strategy_family, description))
            row = cur.fetchone()
        conn.commit()
        return row[0] if row else None
    except Exception as e:
        log.warning(f"strategy_lifecycle: register_strategy failed for '{strategy_name}': {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def get_strategy_by_name(conn, strategy_name):
    """Returns (id, strategy_name, strategy_family, description) or None.
    Plain tuple, not a dataclass -- `strategies` has no dedicated object
    model in this PR, only strategy_versions does (that's the thing with
    a lifecycle)."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, strategy_name, strategy_family, description
                FROM strategies WHERE strategy_name=%s
            """, (strategy_name,))
            return cur.fetchone()
    except Exception as e:
        log.warning(f"strategy_lifecycle: get_strategy_by_name failed for '{strategy_name}': {e}")
        return None


def register_strategy_version(conn, strategy_id, *, description=None, created_by=None,
                               git_commit=None, parent_strategy_version_id=None,
                               hypothesis_type=None, hypothesis_type_version=None,
                               candidate_id=None):
    """Creates a new strategy_versions row in RESEARCH status.
    version_number auto-increments per strategy_id (MAX+1, starting at 1)
    -- computed and inserted in one transaction so two concurrent calls
    for the same strategy_id can't race to the same version_number (the
    UNIQUE (strategy_id, version_number) constraint is the actual
    guarantee; the MAX+1 read is just how a caller gets a very-likely-
    correct number without pre-supplying one). Returns the new id, or
    None on any failure."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(MAX(version_number), 0) + 1 FROM strategy_versions WHERE strategy_id=%s
            """, (strategy_id,))
            version_number = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO strategy_versions
                    (strategy_id, version_number, status, description, created_by, git_commit,
                     parent_strategy_version_id, hypothesis_type, hypothesis_type_version, candidate_id)
                VALUES (%s, %s, 'RESEARCH', %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (strategy_id, version_number, description, created_by, git_commit,
                  parent_strategy_version_id, hypothesis_type, hypothesis_type_version, candidate_id))
            row = cur.fetchone()
        conn.commit()
        return row[0] if row else None
    except Exception as e:
        log.warning(f"strategy_lifecycle: register_strategy_version failed for strategy_id={strategy_id}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def get_strategy_version(conn, strategy_version_id):
    """None if the row doesn't exist or on any failure -- fail-open,
    matching trade_thesis.load_trade_thesis()'s contract."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, strategy_id, version_number, status, created_at, description, created_by,
                       git_commit, code_hash, parameter_hash, parameter_frozen_at,
                       parent_strategy_version_id, hypothesis_type, hypothesis_type_version, candidate_id
                FROM strategy_versions WHERE id=%s
            """, (strategy_version_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return StrategyVersion(
            id=row[0], strategy_id=row[1], version_number=row[2], status=row[3], created_at=row[4],
            description=row[5], created_by=row[6], git_commit=row[7], code_hash=row[8],
            parameter_hash=row[9], parameter_frozen_at=row[10], parent_strategy_version_id=row[11],
            hypothesis_type=row[12], hypothesis_type_version=row[13], candidate_id=row[14],
        )
    except Exception as e:
        log.warning(f"strategy_lifecycle: get_strategy_version failed for id={strategy_version_id}: {e}")
        return None


def list_strategy_versions(conn, strategy_id):
    """Empty list (not None) on failure or if the strategy has no
    versions -- fail-open list contract, matching
    hypothesis_candidates.list_candidates()."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, strategy_id, version_number, status, created_at, description, created_by,
                       git_commit, code_hash, parameter_hash, parameter_frozen_at,
                       parent_strategy_version_id, hypothesis_type, hypothesis_type_version, candidate_id
                FROM strategy_versions WHERE strategy_id=%s ORDER BY version_number
            """, (strategy_id,))
            rows = cur.fetchall()
        return [
            StrategyVersion(
                id=r[0], strategy_id=r[1], version_number=r[2], status=r[3], created_at=r[4],
                description=r[5], created_by=r[6], git_commit=r[7], code_hash=r[8],
                parameter_hash=r[9], parameter_frozen_at=r[10], parent_strategy_version_id=r[11],
                hypothesis_type=r[12], hypothesis_type_version=r[13], candidate_id=r[14],
            )
            for r in rows
        ]
    except Exception as e:
        log.warning(f"strategy_lifecycle: list_strategy_versions failed for strategy_id={strategy_id}: {e}")
        return []


def transition(conn, strategy_version_id, to_status, *, actor=None, reason=None, metadata=None,
               _code_hash=None, _parameter_hash=None):
    """Validates `to_status` against the current status via
    is_valid_transition(), then updates strategy_versions.status and
    inserts one strategy_version_transitions row -- in the SAME
    transaction, so a status change and its audit record can never
    diverge. Returns True on success, False on any failure (unknown
    strategy_version_id, illegal transition, or a DB error) -- logs the
    reason via log.warning in every False case.

    _code_hash/_parameter_hash are private to this module -- freeze()
    passes them through to also stamp code_hash/parameter_hash and
    parameter_frozen_at in the same UPDATE; no other caller should pass
    them directly (use freeze() for a FROZEN transition instead)."""
    if to_status not in STATUSES:
        log.warning(f"strategy_lifecycle: unknown to_status '{to_status}'")
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM strategy_versions WHERE id=%s FOR UPDATE", (strategy_version_id,))
            row = cur.fetchone()
            if row is None:
                log.warning(f"strategy_lifecycle: no strategy_version with id={strategy_version_id}")
                conn.rollback()
                return False
            from_status = row[0]
            if not is_valid_transition(from_status, to_status):
                log.warning(f"strategy_lifecycle: illegal transition {from_status} -> {to_status} "
                            f"for strategy_version_id={strategy_version_id}")
                conn.rollback()
                return False

            if to_status == "FROZEN":
                cur.execute("""
                    UPDATE strategy_versions
                    SET status=%s, parameter_frozen_at=NOW(), code_hash=%s, parameter_hash=%s
                    WHERE id=%s
                """, (to_status, _code_hash, _parameter_hash, strategy_version_id))
            else:
                cur.execute("UPDATE strategy_versions SET status=%s WHERE id=%s", (to_status, strategy_version_id))

            cur.execute("""
                INSERT INTO strategy_version_transitions
                    (strategy_version_id, from_status, to_status, actor, reason, metadata)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (strategy_version_id, from_status, to_status, actor, reason,
                  json.dumps(metadata or {})))
        conn.commit()
        return True
    except Exception as e:
        log.warning(f"strategy_lifecycle: transition failed for strategy_version_id={strategy_version_id}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def freeze(conn, strategy_version_id, code_hash, parameter_hash, *, actor=None, reason=None):
    """The spec's §9 explicit Freeze operation. Requires a non-empty
    code_hash/parameter_hash (never derived here -- see module docstring)
    and only succeeds from WALK_FORWARD, per VALID_TRANSITIONS -- the
    "cannot paper-forward test until frozen" contract is enforced by that
    table, not re-checked separately here. Returns True/False, same
    contract as transition()."""
    if not code_hash or not parameter_hash:
        log.warning(f"strategy_lifecycle: freeze requires non-empty code_hash/parameter_hash "
                    f"(strategy_version_id={strategy_version_id})")
        return False
    return transition(conn, strategy_version_id, "FROZEN", actor=actor, reason=reason,
                       _code_hash=code_hash, _parameter_hash=parameter_hash)

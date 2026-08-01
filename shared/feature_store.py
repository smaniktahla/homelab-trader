"""
Persistence for symbol_features / signal_outcomes' component columns, used
directly by shared/signals.py::compute_signals(). Lives in shared/ (not
ingest/) because shared/ is imported by both the ingest and API containers,
and shared/signals.py must not depend on an ingest-only module — that would
reverse the existing dependency direction and could break the API
container's import path.

shared/fundamentals.py (PR #2) is the same pattern one level up: it reads
fundamental_facts (raw collector data written by ingest/fundamentals.py)
and computes a shadow fundamental_score, which compute_signals() passes
into attach_fundamental_score() below. This module still does no scoring
itself — it only persists scores computed elsewhere in shared/.

Every public function here is best-effort and fail-open: on any DB error it
rolls back just its own statement and returns None, and never raises past
its own try/except. compute_signals()'s eager-commit style (it commits
after every signals/signal_outcomes insert before this module is ever
called) makes a plain rollback safe here — there is nothing else pending on
the connection to lose.
"""

import json
import logging

log = logging.getLogger(__name__)

FEATURE_VERSION = "v1"  # versions score_signal()'s calculation itself (RSI/BB/regime/RS/ATR).
                         # Bump only when that calculation changes -- not when a new
                         # component family (fundamentals, news, ...) is added.
MODEL_VERSION = "mean_reversion_technical_only_v1"  # versions the *combination* logic.
                         # PR #1's combination is the identity function (weight
                         # {technical: 1.0}); this label exists so PR #6's real
                         # weighted-combination rows are unambiguous against these.
TECHNICAL_ONLY_WEIGHTS = {"technical": 1.0}  # PR #1's fixed component_weights value.


def record_symbol_feature_snapshot(conn, symbol, side, as_of, technical_score):
    """Best-effort idempotent insert into symbol_features for one
    (symbol, side, as_of, FEATURE_VERSION). Returns the row id (new or
    pre-existing), or None if the write failed for any reason.

    Idempotent via ON CONFLICT DO NOTHING + a follow-up SELECT: a retry,
    partial replay, or future repair process re-computing the same logical
    snapshot must find the existing correct row rather than have its
    paired signal_outcomes row look like snapshotting failed just because
    the snapshot already existed.

    data_confidence is hardcoded 1.0 in PR #1: a symbol_features row is
    only ever created after score_signal() already succeeded (it's on
    compute_signals()'s critical path, never optional), so there is no
    partial-confidence case to represent yet.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO symbol_features
                    (symbol, side, as_of, technical_score, data_confidence,
                     feature_version, model_version, component_weights, source_timestamps)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, as_of, side, feature_version) DO NOTHING
                RETURNING id
            """, (
                symbol, side, as_of, technical_score, 1.0,
                FEATURE_VERSION, MODEL_VERSION,
                json.dumps(TECHNICAL_ONLY_WEIGHTS),
                json.dumps({"technical": as_of.isoformat()}),
            ))
            row = cur.fetchone()
            if row is None:
                cur.execute("""
                    SELECT id FROM symbol_features
                    WHERE symbol=%s AND as_of=%s AND side=%s AND feature_version=%s
                """, (symbol, as_of, side, FEATURE_VERSION))
                row = cur.fetchone()
        conn.commit()
        return row[0] if row else None
    except Exception as e:
        log.warning(f"feature_store: snapshot write failed for {symbol} {side}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def attach_feature_snapshot(conn, outcome_id, feature_snapshot_id, technical_score):
    """Best-effort update of signal_outcomes' component columns for the row
    _record_outcome() already committed. Never raises. A failure here never
    touches the outcome row's core fields (score, block_reason, etc.),
    since those were already committed by the caller before this runs."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE signal_outcomes
                SET feature_snapshot_id=%s, technical_score=%s, data_confidence=%s,
                    feature_version=%s, model_version=%s, component_weights=%s
                WHERE id=%s
            """, (
                feature_snapshot_id, technical_score, 1.0,
                FEATURE_VERSION, MODEL_VERSION, json.dumps(TECHNICAL_ONLY_WEIGHTS),
                outcome_id,
            ))
        conn.commit()
    except Exception as e:
        log.warning(f"feature_store: attach failed for outcome {outcome_id}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


def attach_fundamental_score(conn, feature_snapshot_id, outcome_id, fundamental_score):
    """Best-effort update of fundamental_score on both symbol_features
    (via feature_snapshot_id) and signal_outcomes (via outcome_id), in one
    transaction so they can never disagree with each other. Never raises.
    A failure here never touches either row's other fields (technical
    score, block_reason, etc.), since those were already committed by the
    caller before this runs.

    component_weights is deliberately left untouched (still
    {"technical": 1.0}) -- storing a fundamental_score value here does not
    give it any decision weight. Status ("shadow" vs "unavailable") is
    derived by the API purely from whether component_weights names this
    component with a nonzero weight, which it doesn't yet -- see
    api/main.py::_component_status()."""
    try:
        with conn.cursor() as cur:
            if feature_snapshot_id is not None:
                cur.execute(
                    "UPDATE symbol_features SET fundamental_score=%s WHERE id=%s",
                    (fundamental_score, feature_snapshot_id),
                )
            cur.execute(
                "UPDATE signal_outcomes SET fundamental_score=%s WHERE id=%s",
                (fundamental_score, outcome_id),
            )
        conn.commit()
    except Exception as e:
        log.warning(f"feature_store: fundamental attach failed for outcome {outcome_id}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass

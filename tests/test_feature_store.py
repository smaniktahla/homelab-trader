from datetime import datetime, timezone

from feature_store import (
    FEATURE_VERSION,
    record_symbol_feature_snapshot,
    attach_feature_snapshot,
)


def _as_of():
    return datetime.now(timezone.utc)


def test_record_snapshot_persists_technical_score(conn):
    as_of = _as_of()
    snap_id = record_symbol_feature_snapshot(conn, "AAPL", "buy", as_of, 62)
    assert snap_id is not None

    with conn.cursor() as cur:
        cur.execute("SELECT technical_score, data_confidence, feature_version, side "
                     "FROM symbol_features WHERE id=%s", (snap_id,))
        score, confidence, version, side = cur.fetchone()
    assert score == 62
    assert float(confidence) == 1.0
    assert version == FEATURE_VERSION
    assert side == "buy"


def test_missing_components_stay_null_not_zero(conn):
    as_of = _as_of()
    snap_id = record_symbol_feature_snapshot(conn, "AAPL", "buy", as_of, 62)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT fundamental_score, earnings_score, news_score, options_score, macro_fit_score
            FROM symbol_features WHERE id=%s
        """, (snap_id,))
        row = cur.fetchone()
    assert row == (None, None, None, None, None)


def test_snapshot_write_is_idempotent(conn):
    """A retry / repair process re-computing the same logical snapshot must
    get back the SAME row id, not a duplicate, and the paired outcome must
    not look like snapshotting failed just because the row already existed."""
    as_of = _as_of()
    first_id = record_symbol_feature_snapshot(conn, "AAPL", "buy", as_of, 62)
    second_id = record_symbol_feature_snapshot(conn, "AAPL", "buy", as_of, 62)
    assert first_id == second_id

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM symbol_features WHERE symbol='AAPL' AND as_of=%s", (as_of,))
        assert cur.fetchone()[0] == 1


def test_snapshot_append_only_across_feature_versions(conn, monkeypatch):
    """Two snapshots for the identical (symbol, side, as_of) but different
    feature_version must both persist -- feature_version bumps are additive,
    never overwrite what an older version computed."""
    as_of = _as_of()
    id_v1 = record_symbol_feature_snapshot(conn, "AAPL", "buy", as_of, 62)

    import feature_store
    monkeypatch.setattr(feature_store, "FEATURE_VERSION", "v2")
    id_v2 = record_symbol_feature_snapshot(conn, "AAPL", "buy", as_of, 70)

    assert id_v1 != id_v2
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM symbol_features WHERE symbol='AAPL' AND as_of=%s", (as_of,))
        assert cur.fetchone()[0] == 2


def test_record_snapshot_rolls_back_on_db_exception(conn):
    """Trigger a genuine Postgres error inside the INSERT (side violates the
    CHECK constraint) rather than mocking the driver — a real
    psycopg2.errors.CheckViolation is what "a DB exception" actually looks
    like here. The function must not raise, must return None, must leave no
    partial row, and — critically — the connection must still be usable for
    the next statement afterward (proves an actual rollback happened, not
    just a caught exception on a now-poisoned connection — this is the exact
    failure mode behind the 2026-07-21 outage in shared/signals.py)."""
    result = record_symbol_feature_snapshot(conn, "AAPL", "hold", _as_of(), 62)
    assert result is None

    # Connection must still be usable — this would raise
    # "current transaction is aborted" if the rollback hadn't happened.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM symbol_features")
        assert cur.fetchone()[0] == 0


def test_attach_feature_snapshot_updates_outcome_row(conn):
    as_of = _as_of()
    snap_id = record_symbol_feature_snapshot(conn, "AAPL", "buy", as_of, 62)

    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO signal_outcomes (symbol, side, score, thesis_id)
            VALUES ('AAPL', 'buy', 62, %s) RETURNING id
        """, (thesis_id,))
        outcome_id = cur.fetchone()[0]
    conn.commit()

    attach_feature_snapshot(conn, outcome_id, snap_id, 62)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT feature_snapshot_id, technical_score, feature_version, model_version
            FROM signal_outcomes WHERE id=%s
        """, (outcome_id,))
        row = cur.fetchone()
    assert row[0] == snap_id
    assert row[1] == 62
    assert row[2] == FEATURE_VERSION


def test_attach_feature_snapshot_rolls_back_on_db_exception(conn):
    """feature_snapshot_id=999999 doesn't exist -> a genuine FK violation.
    Must not raise, and the outcome row's other fields (committed earlier
    by the caller, before attach is ever called) must be untouched."""
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO signal_outcomes (symbol, side, score, thesis_id)
            VALUES ('AAPL', 'buy', 62, %s) RETURNING id
        """, (thesis_id,))
        outcome_id = cur.fetchone()[0]
    conn.commit()

    attach_feature_snapshot(conn, outcome_id, 999999, 62)  # must not raise

    with conn.cursor() as cur:
        cur.execute("SELECT feature_snapshot_id, score FROM signal_outcomes WHERE id=%s", (outcome_id,))
        row = cur.fetchone()
    assert row[0] is None
    assert row[1] == 62  # untouched, still committed from before attach ran


def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug='mean_reversion'")
        return cur.fetchone()[0]

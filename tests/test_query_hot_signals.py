"""
Bug fix, 2026-09-02: the high-score alert email queried `signals` and only
excluded rows blocked with signal_outcomes.block_reason='no_position_held'
(a narrow 2026-07-23 fix), so any OTHER block reason -- e.g.
max_open_positions -- still slipped through and produced a "Review
Proposals" email for a signal that never became an actionable
trade_proposals row (found via a live DOV alert with 17/15 positions
already held). Fixed by excluding any non-NULL block_reason. This test
file covers ingest.py::query_hot_signals() directly -- no prior test
coverage existed for this alert logic at all.
"""

import os
import sys
from datetime import datetime, timedelta, timezone


def _import_ingest(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ.get(
        "TEST_DATABASE_URL", "postgresql://invest_test:not_a_real_credential@localhost:15432/invest_test"))
    import pathlib
    ingest_dir = str(pathlib.Path(__file__).resolve().parent.parent / "ingest")
    if ingest_dir not in sys.path:
        sys.path.insert(0, ingest_dir)
    sys.modules.pop("ingest", None)
    import ingest
    return ingest


def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug = 'mean_reversion'")
        return cur.fetchone()[0]


def _insert_signal(conn, symbol, score, signal_type="rsi_mr_buy", generated_at=None):
    generated_at = generated_at or datetime.now(timezone.utc)
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO signals (symbol, signal_type, score, rationale, generated_at, thesis_id) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (symbol, signal_type, score, "test rationale", generated_at, thesis_id),
        )
        signal_id = cur.fetchone()[0]
    conn.commit()
    return signal_id


def _insert_outcome(conn, signal_id, symbol, side, score, block_reason):
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO signal_outcomes (signal_id, symbol, side, score, proposal_status, block_reason, thesis_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (signal_id, symbol, side, score, "blocked" if block_reason else "proposed", block_reason, thesis_id),
        )
    conn.commit()


def test_unblocked_high_score_signal_is_returned(monkeypatch, conn):
    ing = _import_ingest(monkeypatch)
    signal_id = _insert_signal(conn, "GOOD", 91)
    _insert_outcome(conn, signal_id, "GOOD", "buy", 91, block_reason=None)

    rows = ing.query_hot_signals(conn, min_score=80)
    symbols = [r[0] for r in rows]
    assert "GOOD" in symbols


def test_max_open_positions_blocked_signal_is_excluded(monkeypatch, conn):
    # This is the exact bug: a signal blocked by max_open_positions must
    # not appear in the alert query results.
    ing = _import_ingest(monkeypatch)
    signal_id = _insert_signal(conn, "DOV", 91)
    _insert_outcome(conn, signal_id, "DOV", "buy", 91, block_reason="max_open_positions")

    rows = ing.query_hot_signals(conn, min_score=80)
    symbols = [r[0] for r in rows]
    assert "DOV" not in symbols


def test_no_position_held_blocked_signal_is_excluded(monkeypatch, conn):
    # Original 2026-07-23 fix case -- must still work after the generalization.
    ing = _import_ingest(monkeypatch)
    signal_id = _insert_signal(conn, "RTX", 91)
    _insert_outcome(conn, signal_id, "RTX", "sell", 91, block_reason="no_position_held")

    rows = ing.query_hot_signals(conn, min_score=80)
    symbols = [r[0] for r in rows]
    assert "RTX" not in symbols


def test_other_block_reasons_are_also_excluded(monkeypatch, conn):
    ing = _import_ingest(monkeypatch)
    for symbol, reason in [
        ("SEC", "sector_cap"),
        ("CD", "buy_cooldown"),
        ("PERM", "trading_permission_denied:circuit_breaker"),
        ("THR", "below_proposal_threshold"),
        ("DUP", "duplicate_open_proposal"),
    ]:
        signal_id = _insert_signal(conn, symbol, 90)
        _insert_outcome(conn, signal_id, symbol, "buy", 90, block_reason=reason)

    rows = ing.query_hot_signals(conn, min_score=80)
    symbols = [r[0] for r in rows]
    assert symbols == []


def test_below_min_score_is_excluded_even_when_unblocked(monkeypatch, conn):
    ing = _import_ingest(monkeypatch)
    signal_id = _insert_signal(conn, "LOW", 50)
    _insert_outcome(conn, signal_id, "LOW", "buy", 50, block_reason=None)

    rows = ing.query_hot_signals(conn, min_score=80)
    symbols = [r[0] for r in rows]
    assert "LOW" not in symbols


def test_old_signal_outside_two_hour_window_is_excluded(monkeypatch, conn):
    ing = _import_ingest(monkeypatch)
    old_ts = datetime.now(timezone.utc) - timedelta(hours=3)
    signal_id = _insert_signal(conn, "OLD", 91, generated_at=old_ts)
    _insert_outcome(conn, signal_id, "OLD", "buy", 91, block_reason=None)

    rows = ing.query_hot_signals(conn, min_score=80)
    symbols = [r[0] for r in rows]
    assert "OLD" not in symbols


def test_signal_with_no_outcome_row_yet_is_still_returned(monkeypatch, conn):
    # Defensive case: a signal row exists but its signal_outcomes row
    # hasn't been written yet (shouldn't normally happen by alert time,
    # but the query must not accidentally require an outcome row to exist).
    ing = _import_ingest(monkeypatch)
    _insert_signal(conn, "NOOUTCOME", 91)

    rows = ing.query_hot_signals(conn, min_score=80)
    symbols = [r[0] for r in rows]
    assert "NOOUTCOME" in symbols

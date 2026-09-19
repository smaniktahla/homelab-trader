"""
Tests for shared/trading_permission.py -- Risk Engine PR 3's single
aggregation point for account-level trading permission. Against a real
Postgres connection (current_loss_streak/evaluate_trading_permission both
query position_lifecycles/portfolio_snapshots), same reasoning as every
other DB-touching module's tests in this codebase.
"""

import sys
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for d in (ROOT / "shared",):
    p = str(d)
    if p not in sys.path:
        sys.path.insert(0, p)

sys.modules.pop("trading_permission", None)
sys.modules.pop("circuit_breaker", None)
import trading_permission as tp

P = {"circuit_breaker_drawdown_pct": 0.15, "loss_streak_limit": 4}


def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug='mean_reversion'")
        return cur.fetchone()[0]


def _insert_lifecycle(conn, symbol, net_pnl, closed_at, exit_counts_toward_loss_streak=None):
    """exit_counts_toward_loss_streak=None (default) creates no exit trade
    at all -- current_loss_streak()'s NOT EXISTS check is trivially true
    with no exit rows, same as historical data with no position_trades
    linkage. Pass True/False to attach one exit trade with that
    counts_toward_loss_streak value, for the attribution tests below."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO position_lifecycles (symbol, status, opened_at, closed_at, qty, net_pnl)
            VALUES (%s, 'closed', %s, %s, 10, %s)
            RETURNING id
        """, (symbol, closed_at - timedelta(days=1), closed_at, net_pnl))
        lifecycle_id = cur.fetchone()[0]
        if exit_counts_toward_loss_streak is not None:
            cur.execute("""
                INSERT INTO trades (symbol, side, qty, price, traded_at, source, thesis_id, counts_toward_loss_streak)
                VALUES (%s, 'sell', 10, 100.0, %s, 'manual', %s, %s)
                RETURNING id
            """, (symbol, closed_at, _mean_reversion_thesis_id(conn), exit_counts_toward_loss_streak))
            trade_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO position_trades (position_lifecycle_id, trade_id, role, qty_allocated)
                VALUES (%s, %s, 'exit', 10)
            """, (lifecycle_id, trade_id))
    conn.commit()
    return lifecycle_id


def _insert_snapshot(conn, portfolio_value, hwm):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO portfolio_snapshots (portfolio_value, high_water_mark, drawdown_pct)
            VALUES (%s, %s, %s)
        """, (portfolio_value, hwm, (hwm - portfolio_value) / hwm if hwm else 0.0))
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────
# current_loss_streak
# ─────────────────────────────────────────────────────────────────────────

def test_loss_streak_counts_consecutive_recent_losses(conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _insert_lifecycle(conn, "A", -100.0, base)
    _insert_lifecycle(conn, "B", -50.0, base + timedelta(days=1))
    _insert_lifecycle(conn, "C", -25.0, base + timedelta(days=2))
    assert tp.current_loss_streak(conn) == 3


def test_loss_streak_stops_at_most_recent_win(conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _insert_lifecycle(conn, "A", -100.0, base)          # older loss
    _insert_lifecycle(conn, "B", 200.0, base + timedelta(days=1))    # win breaks the streak
    _insert_lifecycle(conn, "C", -50.0, base + timedelta(days=2))    # most recent, a loss
    assert tp.current_loss_streak(conn) == 1


def test_loss_streak_zero_net_pnl_counts_as_loss(conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _insert_lifecycle(conn, "A", 0.0, base)
    assert tp.current_loss_streak(conn) == 1


def test_loss_streak_zero_when_no_closed_lifecycles(conn):
    assert tp.current_loss_streak(conn) == 0


def test_loss_streak_excludes_lifecycle_whose_exit_is_marked_not_counting(conn):
    # The actual fix for the 2026-09-19 incident: a manual sell explicitly
    # marked counts_toward_loss_streak=False is skipped entirely -- it
    # neither breaks nor extends the streak, as if it never closed.
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _insert_lifecycle(conn, "A", -100.0, base, exit_counts_toward_loss_streak=False)
    _insert_lifecycle(conn, "B", -50.0, base + timedelta(days=1))  # no exit row -- counts as before
    assert tp.current_loss_streak(conn) == 1  # only B


def test_loss_streak_counts_lifecycle_whose_exit_is_explicitly_true(conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _insert_lifecycle(conn, "A", -100.0, base, exit_counts_toward_loss_streak=True)
    _insert_lifecycle(conn, "B", -50.0, base + timedelta(days=1), exit_counts_toward_loss_streak=True)
    assert tp.current_loss_streak(conn) == 2


def test_loss_streak_excluded_lifecycle_does_not_break_an_otherwise_continuous_streak(conn):
    # An excluded lifecycle is skipped, not treated as a win that would
    # reset the count -- it's as if it never existed in the sequence.
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _insert_lifecycle(conn, "A", -100.0, base)
    _insert_lifecycle(conn, "B", 500.0, base + timedelta(days=1), exit_counts_toward_loss_streak=False)  # excluded, even though it's a big win
    _insert_lifecycle(conn, "C", -50.0, base + timedelta(days=2))
    assert tp.current_loss_streak(conn) == 2  # A and C, B skipped entirely


def test_loss_streak_ignores_open_lifecycles(conn):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO position_lifecycles (symbol, status, opened_at, qty, net_pnl)
            VALUES ('A', 'open', %s, 10, -100.0)
        """, (datetime(2026, 6, 1, tzinfo=timezone.utc),))
    conn.commit()
    assert tp.current_loss_streak(conn) == 0


# ─────────────────────────────────────────────────────────────────────────
# evaluate_trading_permission
# ─────────────────────────────────────────────────────────────────────────

def test_permission_allowed_when_no_conditions_met(conn):
    result = tp.evaluate_trading_permission(conn, 100_000.0, P)
    assert result == {"new_entries_allowed": True, "scope": "account", "reasons": [], "override": None}


def test_permission_denied_by_drawdown(conn):
    _insert_snapshot(conn, 100_000.0, 200_000.0)  # 50% drawdown, over 15% threshold
    result = tp.evaluate_trading_permission(conn, 100_000.0, P)
    assert result["new_entries_allowed"] is False
    assert result["reasons"] == ["portfolio_drawdown_limit"]


def test_permission_denied_by_loss_streak(conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for i in range(4):
        _insert_lifecycle(conn, f"SYM{i}", -10.0, base + timedelta(days=i))
    result = tp.evaluate_trading_permission(conn, 100_000.0, P)
    assert result["new_entries_allowed"] is False
    assert result["reasons"] == ["loss_streak_limit"]


def test_permission_reports_both_reasons_when_both_conditions_met(conn):
    _insert_snapshot(conn, 100_000.0, 200_000.0)
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for i in range(4):
        _insert_lifecycle(conn, f"SYM{i}", -10.0, base + timedelta(days=i))
    result = tp.evaluate_trading_permission(conn, 100_000.0, P)
    assert result["new_entries_allowed"] is False
    assert set(result["reasons"]) == {"portfolio_drawdown_limit", "loss_streak_limit"}


def test_permission_below_streak_limit_still_allowed(conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for i in range(3):  # one below the default limit of 4
        _insert_lifecycle(conn, f"SYM{i}", -10.0, base + timedelta(days=i))
    result = tp.evaluate_trading_permission(conn, 100_000.0, P)
    assert result["new_entries_allowed"] is True


# ─────────────────────────────────────────────────────────────────────────
# trading-permission override
# ─────────────────────────────────────────────────────────────────────────

def _blocked_by_loss_streak(conn):
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for i in range(4):
        _insert_lifecycle(conn, f"OVR{i}", -10.0, base + timedelta(days=i))


def test_create_override_requires_created_by_and_reason(conn):
    assert tp.create_override(conn, "", "some reason") is None
    assert tp.create_override(conn, "salil", "") is None


def test_get_active_override_none_when_none_created(conn):
    assert tp.get_active_override(conn) is None


def test_create_override_makes_it_active_and_isoformats_timestamps(conn):
    override_id = tp.create_override(conn, "salil", "reviewed the losses, resuming manually")
    assert override_id is not None
    active = tp.get_active_override(conn)
    assert active["id"] == override_id
    assert active["created_by"] == "salil"
    assert active["reason"] == "reviewed the losses, resuming manually"
    assert isinstance(active["created_at"], str)  # isoformat()'d, not a raw datetime
    assert active["expires_at"] is None


def test_override_unblocks_entries_but_reasons_still_reported(conn):
    _blocked_by_loss_streak(conn)
    blocked = tp.evaluate_trading_permission(conn, 100_000.0, P)
    assert blocked["new_entries_allowed"] is False
    assert blocked["reasons"] == ["loss_streak_limit"]
    assert blocked["override"] is None

    tp.create_override(conn, "salil", "reviewed, resuming manually")
    overridden = tp.evaluate_trading_permission(conn, 100_000.0, P)
    assert overridden["new_entries_allowed"] is True
    assert overridden["reasons"] == ["loss_streak_limit"]  # still reported, never hidden
    assert overridden["override"]["created_by"] == "salil"


def test_expired_override_does_not_unblock(conn):
    _blocked_by_loss_streak(conn)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    tp.create_override(conn, "salil", "already expired", expires_at=past)
    result = tp.evaluate_trading_permission(conn, 100_000.0, P)
    assert result["new_entries_allowed"] is False
    assert result["override"] is None


def test_future_expiry_still_active(conn):
    _blocked_by_loss_streak(conn)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    tp.create_override(conn, "salil", "resuming for an hour", expires_at=future)
    result = tp.evaluate_trading_permission(conn, 100_000.0, P)
    assert result["new_entries_allowed"] is True
    assert result["override"]["expires_at"] is not None


def test_revoke_override_deactivates_it(conn):
    _blocked_by_loss_streak(conn)
    override_id = tp.create_override(conn, "salil", "resuming manually")
    assert tp.evaluate_trading_permission(conn, 100_000.0, P)["new_entries_allowed"] is True

    assert tp.revoke_override(conn, override_id, "salil") is True
    result = tp.evaluate_trading_permission(conn, 100_000.0, P)
    assert result["new_entries_allowed"] is False
    assert result["override"] is None


def test_revoke_override_requires_revoked_by(conn):
    override_id = tp.create_override(conn, "salil", "resuming manually")
    assert tp.revoke_override(conn, override_id, "") is False
    assert tp.get_active_override(conn) is not None  # still active


def test_revoke_already_revoked_override_returns_false(conn):
    override_id = tp.create_override(conn, "salil", "resuming manually")
    assert tp.revoke_override(conn, override_id, "salil") is True
    assert tp.revoke_override(conn, override_id, "salil") is False


def test_revoke_unknown_override_id_returns_false(conn):
    assert tp.revoke_override(conn, 999999, "salil") is False


def test_get_active_override_returns_most_recent_when_multiple_exist(conn):
    tp.create_override(conn, "salil", "first")
    second_id = tp.create_override(conn, "salil", "second")
    active = tp.get_active_override(conn)
    assert active["id"] == second_id

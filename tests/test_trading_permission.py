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


def _insert_lifecycle(conn, symbol, net_pnl, closed_at):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO position_lifecycles (symbol, status, opened_at, closed_at, qty, net_pnl)
            VALUES (%s, 'closed', %s, %s, 10, %s)
        """, (symbol, closed_at - timedelta(days=1), closed_at, net_pnl))
    conn.commit()


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
    assert result == {"new_entries_allowed": True, "scope": "account", "reasons": []}


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

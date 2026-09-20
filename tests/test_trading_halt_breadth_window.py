"""
Tests for the 2026-09-20 trading-halt rework in shared/trading_permission.py:
  * loss_streak_window_days -- realized losses age out so the loss-streak
    pause lifts on its own instead of persisting until a win.
  * the breadth trigger -- pause when >= pct of HELD positions (and at least
    min_positions of them) have closed down N daily bars in a row; self-clearing.
Real Postgres, same as test_trading_permission.py.
"""

import sys
import pathlib
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT / "shared") not in sys.path:
    sys.path.insert(0, str(ROOT / "shared"))
sys.modules.pop("trading_permission", None)
sys.modules.pop("circuit_breaker", None)
import trading_permission as tp

NOW = datetime.now(timezone.utc)
BASE_P = {"circuit_breaker_drawdown_pct": 0.15, "loss_streak_limit": 4}
BREADTH_P = {**BASE_P, "breadth_streak_enabled": 1, "breadth_streak_pct": 0.20,
             "breadth_streak_days": 3, "breadth_streak_min_positions": 2}


def _closed_loss(conn, symbol, days_ago, net_pnl=-10.0):
    closed_at = NOW - timedelta(days=days_ago)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO position_lifecycles (symbol, status, opened_at, closed_at, qty, net_pnl)
            VALUES (%s, 'closed', %s, %s, 10, %s)
        """, (symbol, closed_at - timedelta(days=1), closed_at, net_pnl))
    conn.commit()


def _held(conn, symbol, closes_newest_first):
    """An open lifecycle plus daily bars, newest-first closes."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO position_lifecycles (symbol, status, opened_at, qty)
            VALUES (%s, 'open', %s, 10)
        """, (symbol, NOW - timedelta(days=30)))
        for i, close in enumerate(closes_newest_first):
            cur.execute("INSERT INTO price_history (symbol, ts, close) VALUES (%s, %s, %s)",
                        (symbol, NOW - timedelta(days=i), close))
    conn.commit()


DOWN = [97, 98, 99, 100]      # newest first: three consecutive lower closes
FLAT_UP = [100, 99, 98, 97]   # rising
BROKEN = [97, 99, 98, 100]    # an up day in the middle


# ── loss-streak cooldown window ─────────────────────────────────────────

def test_window_zero_is_unbounded_original_behavior(conn):
    for i, s in enumerate("ABCD"):
        _closed_loss(conn, s, days_ago=20 + i)
    assert tp.current_loss_streak(conn) == 4
    assert tp.current_loss_streak(conn, window_days=0) == 4


def test_old_losses_age_out_of_window(conn):
    for i, s in enumerate("ABCD"):
        _closed_loss(conn, s, days_ago=10 + i)
    assert tp.current_loss_streak(conn, window_days=7) == 0
    result = tp.evaluate_trading_permission(conn, None, {**BASE_P, "loss_streak_window_days": 7})
    assert result["new_entries_allowed"] is True
    assert result["reasons"] == []


def test_only_recent_losses_count_inside_window(conn):
    _closed_loss(conn, "A", days_ago=30)
    _closed_loss(conn, "B", days_ago=20)
    _closed_loss(conn, "C", days_ago=2)
    _closed_loss(conn, "D", days_ago=1)
    assert tp.current_loss_streak(conn, window_days=7) == 2
    assert tp.current_loss_streak(conn, window_days=0) == 4


def test_recent_full_streak_still_pauses_within_window(conn):
    for i, s in enumerate("ABCD"):
        _closed_loss(conn, s, days_ago=1 + i)
    result = tp.evaluate_trading_permission(conn, None, {**BASE_P, "loss_streak_window_days": 7})
    assert result["new_entries_allowed"] is False
    assert result["reasons"] == ["loss_streak_limit"]


# ── breadth trigger ─────────────────────────────────────────────────────

def test_min_decline_filters_tiny_drifts(conn):
    _held(conn, "TINY", [99.7, 99.8, 99.9, 100.0])    # three lower closes, -0.3% total
    _held(conn, "REAL", [95, 97, 99, 100])            # -5%
    assert tp.current_breadth_down_streak(conn, 3)["symbols"] == ["REAL", "TINY"]           # no floor
    assert tp.current_breadth_down_streak(conn, 3, min_decline_pct=0.02)["symbols"] == ["REAL"]


def test_breadth_counts_only_consecutive_down_streaks(conn):
    _held(conn, "AAA", DOWN)
    _held(conn, "BBB", FLAT_UP)
    _held(conn, "CCC", BROKEN)
    _held(conn, "DDD", [97, 98])          # not enough bars
    b = tp.current_breadth_down_streak(conn, 3)
    assert b == {"in_streak": 1, "held": 4, "symbols": ["AAA"]}


def test_breadth_trips_at_pct_and_min_count(conn):
    for s in ("A1", "A2"):
        _held(conn, s, DOWN)
    for s in ("B1", "B2", "B3"):
        _held(conn, s, FLAT_UP)
    result = tp.evaluate_trading_permission(conn, None, BREADTH_P)   # 2 of 5 = 40%
    assert result["reasons"] == ["breadth_down_streak"]
    assert result["new_entries_allowed"] is False
    assert result["breadth"]["symbols"] == ["A1", "A2"]


def test_min_positions_floor_stops_single_stock_tripping_small_book(conn):
    _held(conn, "A1", DOWN)
    for s in ("B1", "B2", "B3", "B4"):
        _held(conn, s, FLAT_UP)                                       # 1 of 5 = 20% but count floor is 2
    assert tp.evaluate_trading_permission(conn, None, BREADTH_P)["reasons"] == []
    relaxed = {**BREADTH_P, "breadth_streak_min_positions": 1}
    assert tp.evaluate_trading_permission(conn, None, relaxed)["reasons"] == ["breadth_down_streak"]


def test_below_pct_does_not_trip(conn):
    for s in ("A1", "A2"):
        _held(conn, s, DOWN)
    for i in range(18):
        _held(conn, f"B{i}", FLAT_UP)                                 # 2 of 20 = 10% < 20%
    assert tp.evaluate_trading_permission(conn, None, BREADTH_P)["reasons"] == []


def test_breadth_disabled_and_absent_params_are_inert(conn):
    for s in ("A1", "A2", "A3"):
        _held(conn, s, DOWN)
    assert tp.evaluate_trading_permission(conn, None, {**BREADTH_P, "breadth_streak_enabled": 0})["reasons"] == []
    result = tp.evaluate_trading_permission(conn, None, BASE_P)       # legacy callers' param dicts
    assert result["reasons"] == [] and "breadth" not in result


def test_breadth_clears_itself_when_holdings_recover(conn):
    _held(conn, "A1", DOWN)
    _held(conn, "A2", DOWN)
    assert tp.evaluate_trading_permission(conn, None, BREADTH_P)["new_entries_allowed"] is False
    with conn.cursor() as cur:   # a fresh up-close on both breaks the streak
        for s in ("A1", "A2"):
            cur.execute("INSERT INTO price_history (symbol, ts, close) VALUES (%s, %s, 110)",
                        (s, NOW + timedelta(days=1)))
    conn.commit()
    assert tp.evaluate_trading_permission(conn, None, BREADTH_P)["new_entries_allowed"] is True


def test_override_still_unblocks_breadth_halt(conn):
    _held(conn, "A1", DOWN)
    _held(conn, "A2", DOWN)
    tp.create_override(conn, "salil", "testing")
    result = tp.evaluate_trading_permission(conn, None, BREADTH_P)
    assert result["new_entries_allowed"] is True
    assert result["reasons"] == ["breadth_down_streak"]


def test_float_params_as_loaded_from_signal_params_work(conn):
    """load_params() returns floats; make_interval(days => ...) rejects a
    numeric, so the window must be coerced to int (caught by the API tests)."""
    for i, s in enumerate("ABCD"):
        _closed_loss(conn, s, days_ago=10 + i)
    p = {**BREADTH_P, "loss_streak_window_days": 7.0, "breadth_streak_days": 3.0, "breadth_streak_min_positions": 2.0}
    assert tp.evaluate_trading_permission(conn, None, p)["new_entries_allowed"] is True

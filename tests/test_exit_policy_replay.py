"""
Mechanics of ingest/research/backtests/backtest_exit_policy_replay.py's
simulate_exit(): candidate stops are only active from the bar AFTER the
high that raised them, gaps fill at the open, and a policy with no
candidate stop reproduces the live exits. Synthetic bars, arbitrary
parameters -- not the values of any study.
"""

import datetime as dt
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingest" / "research" / "backtests"))

from backtest_exit_policy_replay import simulate_exit  # noqa: E402

FLOOR = {"name": "floor", "arm_mfe": 0.05, "floor": [[0.05, 0.5]]}
TRAIL = {"name": "trail", "arm_mfe": 0.05, "trail_atr": [[0.05, 1.0]]}
BASE = {"name": "base"}


def _series(rows):
    """rows: (open, high, low, close)."""
    d0 = dt.date(2026, 1, 5)
    return {"d": [d0 + dt.timedelta(days=i) for i in range(len(rows))],
            "o": [r[0] for r in rows], "h": [r[1] for r in rows],
            "l": [r[2] for r in rows], "c": [r[3] for r in rows]}


def _run(rows, policy, close_exit=None, atr=2.0, stop_loss_pct=0.12):
    s = _series(rows)
    n = len(rows)
    return simulate_exit(s, close_exit or [None] * n, [atr] * n, 1, s["o"][1], policy, stop_loss_pct)


def test_floor_raised_by_a_bars_high_does_not_fill_on_that_bar():
    # bar 2 spikes to +10% then trades down to +1% intrabar: the floor it
    # raises (+5%) must not be treated as hit on bar 2 itself
    rows = [(100, 100, 100, 100), (100, 101, 99, 100), (100, 110, 101, 102), (106, 107, 104, 105)]
    r = _run(rows, FLOOR)
    assert r["x"] == 3 and r["reason"] == "candidate_stop"
    assert r["exit_price"] == 105.0  # stop level: 100 * (1 + 0.5 * 0.10)


def test_gap_through_candidate_stop_fills_at_open():
    rows = [(100, 100, 100, 100), (100, 101, 99, 100), (100, 110, 101, 108), (103, 104, 102, 103)]
    r = _run(rows, FLOOR)
    assert r["x"] == 3 and r["exit_price"] == 103


def test_atr_trail_hangs_off_highest_high():
    rows = [(100, 100, 100, 100), (100, 101, 99, 100), (100, 110, 101, 109), (109, 109.5, 107.5, 108)]
    r = _run(rows, TRAIL, atr=2.0)
    assert r["x"] == 3 and r["exit_price"] == 108.0  # 110 - 1.0 * 2.0


def test_unarmed_policy_matches_baseline():
    rows = [(100, 100, 100, 100), (100, 102, 99, 101), (101, 103, 100, 102), (102, 103, 101, 102)]
    close_exit = [None, None, "thesis_complete", None]
    assert _run(rows, FLOOR, close_exit) == _run(rows, BASE, close_exit)
    r = _run(rows, BASE, close_exit)
    assert r["x"] == 3 and r["reason"] == "thesis_complete" and r["exit_price"] == 102


def test_hard_stop_active_on_fill_bar():
    rows = [(100, 100, 100, 100), (100, 100, 85, 90), (90, 91, 89, 90)]
    r = _run(rows, BASE)
    assert r["x"] == 1 and r["reason"] == "hard_stop" and math.isclose(r["exit_price"], 88.0)


def test_open_at_end_of_data_returns_none():
    rows = [(100, 100, 100, 100), (100, 101, 99, 100), (100, 101, 99, 100)]
    assert _run(rows, BASE) is None

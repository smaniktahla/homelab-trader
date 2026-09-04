"""
Tests for shared/price_efficiency.py (Price Structure epic PR D). All
pure functions over in-memory bars -- no DB fixture needed, same
reasoning as tests/test_market_structure.py's pure-function section.
"""

import sys
import pathlib
from datetime import date, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
p = str(ROOT / "shared")
if p not in sys.path:
    sys.path.insert(0, p)

for _mod in ("price_efficiency",):
    sys.modules.pop(_mod, None)
import price_efficiency as pe

D0 = date(2020, 1, 1)


def _bar(o, h, l, c, d=D0):
    return (d, o, h, l, c)


# ─────────────────────────────────────────────────────────────────────────
# candle_body_ratio / candle_wick_ratios
# ─────────────────────────────────────────────────────────────────────────

def test_candle_body_ratio_full_range_directional_bar():
    # open=low, close=high -- no wicks at all, body IS the range
    assert pe.candle_body_ratio(_bar(10, 20, 10, 20)) == 1.0


def test_candle_body_ratio_doji():
    # open == close, non-zero range -- zero body
    assert pe.candle_body_ratio(_bar(15, 20, 10, 15)) == 0.0


def test_candle_body_ratio_none_for_zero_range():
    assert pe.candle_body_ratio(_bar(10, 10, 10, 10)) is None


def test_candle_wick_ratios_hand_computed():
    # range = 20 (10..30). open=15, close=25 -> body [15,25].
    # upper_wick = 30-25=5 -> 5/20=0.25. lower_wick = 15-10=5 -> 0.25.
    upper, lower = pe.candle_wick_ratios(_bar(15, 30, 10, 25))
    assert upper == 0.25
    assert lower == 0.25


def test_candle_wick_ratios_none_for_zero_range():
    assert pe.candle_wick_ratios(_bar(10, 10, 10, 10)) == (None, None)


# ─────────────────────────────────────────────────────────────────────────
# directional_efficiency_ratio
# ─────────────────────────────────────────────────────────────────────────

def test_efficiency_ratio_perfectly_straight_move_is_one():
    closes = [100, 101, 102, 103, 104, 105]
    assert pe.directional_efficiency_ratio(closes, 5) == 1.0


def test_efficiency_ratio_round_trip_is_zero_not_none():
    # ends exactly where it started -- net move 0, but path length > 0,
    # so this is a valid minimal (0.0) result, not the None
    # zero-path-length case tested below.
    closes = [100, 110, 90, 110, 90, 100]
    result = pe.directional_efficiency_ratio(closes, 5)
    assert result == 0.0


def test_efficiency_ratio_hand_computed_partial_chop():
    # window: 100 -> 105 -> 102 -> 108 (lookback=3, so window is closes[-4:])
    closes = [50, 100, 105, 102, 108]
    # net = |108-100| = 8. path = |105-100|+|102-105|+|108-102| = 5+3+6 = 14
    expected = 8 / 14
    result = pe.directional_efficiency_ratio(closes, 3)
    assert abs(result - expected) < 1e-9


def test_efficiency_ratio_none_when_insufficient_history():
    assert pe.directional_efficiency_ratio([100, 101], 5) is None


# ─────────────────────────────────────────────────────────────────────────
# bar_overlap_ratio
# ─────────────────────────────────────────────────────────────────────────

def test_bar_overlap_ratio_full_overlap():
    prev = _bar(10, 20, 10, 15)
    cur = _bar(12, 20, 10, 18)  # identical range -- full overlap
    assert pe.bar_overlap_ratio(cur, prev) == 1.0


def test_bar_overlap_ratio_no_overlap_gap_up():
    prev = _bar(10, 20, 10, 15)
    cur = _bar(25, 30, 25, 28)  # entirely above prev's range -- gap, no overlap
    assert pe.bar_overlap_ratio(cur, prev) == 0.0


def test_bar_overlap_ratio_partial_hand_computed():
    prev = _bar(10, 20, 10, 15)   # range [10,20]
    cur = _bar(15, 25, 15, 20)    # range [15,25], overlap with prev = [15,20] = 5, cur's own range = 10
    assert pe.bar_overlap_ratio(cur, prev) == 0.5


def test_bar_overlap_ratio_none_for_zero_range_current_bar():
    prev = _bar(10, 20, 10, 15)
    cur = _bar(15, 15, 15, 15)
    assert pe.bar_overlap_ratio(cur, prev) is None


# ─────────────────────────────────────────────────────────────────────────
# pullback_depth_pct / pullback_duration_bars
# ─────────────────────────────────────────────────────────────────────────

def _ohlc_from_closes(closes, start=D0):
    return [(start + timedelta(days=i), c, c, c, c) for i, c in enumerate(closes)]


def test_pullback_depth_and_duration_uptrend_hand_computed():
    # window (lookback=5, so last 6 closes): 100,110,108,112,109,105
    # uptrend (105 >= 100). extreme = max = 112 at index 3 (0-based within window).
    # move = 112-100=12. retraced = 112-105=7. depth = 7/12*100.
    closes = [50, 100, 110, 108, 112, 109, 105]
    ohlc = _ohlc_from_closes(closes)
    depth = pe.pullback_depth_pct(ohlc, 5)
    duration = pe.pullback_duration_bars(ohlc, 5)
    assert abs(depth - (7 / 12 * 100)) < 1e-9
    # window has 6 bars (indices 0-5 within window), extreme at window-index 3, last index 5 -> duration=2
    assert duration == 2


def test_pullback_depth_and_duration_downtrend_mirrors_uptrend():
    closes = [150, 100, 90, 92, 88, 91, 95]
    # downtrend (95 <= 100... wait current=95 < start=100 -> downtrend). extreme = min = 88 at window-index 3.
    ohlc = _ohlc_from_closes(closes)
    depth = pe.pullback_depth_pct(ohlc, 5)
    duration = pe.pullback_duration_bars(ohlc, 5)
    # move = 88-100=-12. retraced = 88-95=-7. depth = (-7)/(-12)*100
    assert abs(depth - (-7 / -12 * 100)) < 1e-9
    assert duration == 2


def test_pullback_depth_zero_at_the_extreme_itself():
    # current bar IS the extreme -- no pullback yet
    closes = [50, 100, 105, 110]
    ohlc = _ohlc_from_closes(closes)
    assert pe.pullback_depth_pct(ohlc, 3) == 0.0
    assert pe.pullback_duration_bars(ohlc, 3) == 0


def test_pullback_none_when_insufficient_history():
    ohlc = _ohlc_from_closes([100, 101])
    assert pe.pullback_depth_pct(ohlc, 5) is None
    assert pe.pullback_duration_bars(ohlc, 5) is None


def test_pullback_depth_none_when_window_start_move_is_zero():
    # flat window -- start == every close, "uptrend" branch taken (current >= start),
    # extreme == start, move == 0
    closes = [50, 100, 100, 100, 100]
    ohlc = _ohlc_from_closes(closes)
    assert pe.pullback_depth_pct(ohlc, 3) is None

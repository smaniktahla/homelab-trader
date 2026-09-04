"""
Tests for shared/fair_value_gaps.py (Price Structure epic PR B2).

Structure mirrors tests/test_structural_events.py: pure-function unit
tests against hand-built, hand-verified OHLC series first (bar tuples are
(date, open, high, low, close)), then DB-backed persistence tests.
"""

import sys
import pathlib
from datetime import date, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
p = str(ROOT / "shared")
if p not in sys.path:
    sys.path.insert(0, p)

for _mod in ("fair_value_gaps", "structural_events", "market_structure", "regime_common"):
    sys.modules.pop(_mod, None)
import fair_value_gaps as fvg

D0 = date(2020, 1, 1)


def _bars(closes, highs=None, lows=None, start=D0):
    n = len(closes)
    highs = highs or [c + 0.05 for c in closes]
    lows = lows or [c - 0.05 for c in closes]
    return [(start + timedelta(days=i), closes[i], highs[i], lows[i], closes[i]) for i in range(n)]


# ─────────────────────────────────────────────────────────────────────────
# detect_fair_value_gaps -- pattern detection, strict boundary semantics
# ─────────────────────────────────────────────────────────────────────────

def test_bullish_gap_detected_with_correct_zone_and_confirmation_time():
    # A: high=100, B: irrelevant to the gap test itself, C: low=103 -- high(A)=100 < low(C)=103
    highs = [100, 108, 106]
    lows = [95, 104, 103]
    closes = [98, 107, 104]
    ohlc = _bars(closes, highs=highs, lows=lows)

    gaps = fvg.detect_fair_value_gaps(ohlc)

    assert len(gaps) == 1
    g = gaps[0]
    assert g["gap_type"] == "bullish"
    assert g["zone_lower"] == 100  # high(A)
    assert g["zone_upper"] == 103  # low(C)
    assert g["event_time"] == ohlc[2][0]
    assert g["confirmation_time"] == ohlc[2][0]  # no forward-looking delay


def test_bearish_gap_mirrors_bullish():
    # A: low=110, C: high=105 -- low(A)=110 > high(C)=105
    highs = [112, 108, 105]
    lows = [110, 103, 100]
    closes = [111, 105, 102]
    ohlc = _bars(closes, highs=highs, lows=lows)

    gaps = fvg.detect_fair_value_gaps(ohlc)

    assert len(gaps) == 1
    g = gaps[0]
    assert g["gap_type"] == "bearish"
    assert g["zone_upper"] == 110  # low(A)
    assert g["zone_lower"] == 105  # high(C)


def test_no_gap_when_boundary_exactly_touches():
    # high(A) == low(C) exactly -- zero-width, not a gap (strict inequality required)
    highs = [100, 108, 106]
    lows = [95, 104, 100]  # low(C) = 100 = high(A)
    closes = [98, 107, 103]
    ohlc = _bars(closes, highs=highs, lows=lows)

    assert fvg.detect_fair_value_gaps(ohlc) == []


def test_overlapping_gaps_are_not_merged():
    # two consecutive triples both qualify as bullish gaps -- both kept independently.
    # i=2 (bars 0,1,2): high(0)=100 < low(2)=103. i=3 (bars 1,2,3): high(1)=108 < low(3)=109.
    highs = [100, 108, 106, 115]
    lows = [95, 104, 103, 109]
    closes = [98, 107, 104, 112]
    ohlc = _bars(closes, highs=highs, lows=lows)

    gaps = fvg.detect_fair_value_gaps(ohlc)
    assert len(gaps) == 2
    assert gaps[0]["event_time"] == ohlc[2][0]
    assert gaps[1]["event_time"] == ohlc[3][0]


# ─────────────────────────────────────────────────────────────────────────
# detect_fvg_lifecycle_events -- boundary semantics, non-terminal
# fully_filled, invalidated-after-fully_filled
# ─────────────────────────────────────────────────────────────────────────

BULLISH_GAP = {"gap_type": "bullish", "zone_upper": 110.0, "zone_lower": 100.0, "event_time": D0 + timedelta(days=2)}


def _lifecycle_ohlc(rows, start=D0 + timedelta(days=3)):
    """rows: list of (close, high, low) for bars strictly after the gap's
    creation bar (D0+2)."""
    return [(start + timedelta(days=i), c, h, l, c) for i, (c, h, l) in enumerate(rows)]


def test_partially_entered_fires_on_first_touch_inclusive_boundary():
    # low == zone_upper exactly -- touches() is <=, so this must count
    ohlc = _lifecycle_ohlc([(107, 111, 110.0)])
    events = fvg.detect_fvg_lifecycle_events(ohlc, BULLISH_GAP, gap_id=1)
    types = [e["event_type"] for e in events]
    assert "fvg_partially_entered" in types


def test_midpoint_reached_at_exact_midpoint_boundary():
    # midpoint = 100 + (110-100)*0.5 = 105. low == 105 exactly must count.
    ohlc = _lifecycle_ohlc([(106, 108, 105.0)])
    events = fvg.detect_fvg_lifecycle_events(ohlc, BULLISH_GAP, gap_id=1)
    types = [e["event_type"] for e in events]
    assert "fvg_midpoint_reached" in types
    assert "fvg_fully_filled" not in types  # didn't reach the far edge yet


def test_fully_filled_at_exact_far_boundary_is_not_yet_invalidated():
    # low == zone_lower (100) exactly: fully_filled fires. close == 100
    # exactly (not strictly beyond) must NOT count as invalidated.
    ohlc = _lifecycle_ohlc([(100.0, 108, 100.0)])
    events = fvg.detect_fvg_lifecycle_events(ohlc, BULLISH_GAP, gap_id=1)
    types = [e["event_type"] for e in events]
    assert "fvg_fully_filled" in types
    assert "fvg_invalidated" not in types


def test_fully_filled_and_invalidated_fire_on_the_same_bar_when_close_is_strictly_beyond():
    # a single bar wicks to 95 (through the far edge) and CLOSES at 96
    # (strictly below zone_lower=100) -- both milestones fire together.
    ohlc = _lifecycle_ohlc([(96.0, 108, 95.0)])
    events = fvg.detect_fvg_lifecycle_events(ohlc, BULLISH_GAP, gap_id=1)
    types = [e["event_type"] for e in events]
    assert "fvg_fully_filled" in types
    assert "fvg_invalidated" in types
    assert events[0]["event_time"] == events[1]["event_time"] == ohlc[0][0]


def test_invalidated_can_fire_on_a_later_bar_after_fully_filled_already_fired():
    # bar1 wicks through and fills (low=100) but closes back above (107) --
    # fully_filled only. bar2, later, closes decisively below (98) --
    # invalidated fires then, referencing the same gap, proving
    # fully_filled is not terminal.
    ohlc = _lifecycle_ohlc([
        (107.0, 108, 100.0),
        (98.0, 99, 97.0),
    ])
    events = fvg.detect_fvg_lifecycle_events(ohlc, BULLISH_GAP, gap_id=1)
    filled = next(e for e in events if e["event_type"] == "fvg_fully_filled")
    invalidated = next(e for e in events if e["event_type"] == "fvg_invalidated")
    assert filled["event_time"] == ohlc[0][0]
    assert invalidated["event_time"] == ohlc[1][0]
    assert invalidated["event_time"] > filled["event_time"]


def test_each_milestone_fires_at_most_once():
    # three separate bars all fully fill the gap -- only the first counts.
    ohlc = _lifecycle_ohlc([
        (99.0, 108, 99.0),
        (98.0, 99, 98.0),
        (97.0, 98, 97.0),
    ])
    events = fvg.detect_fvg_lifecycle_events(ohlc, BULLISH_GAP, gap_id=1)
    filled = [e for e in events if e["event_type"] == "fvg_fully_filled"]
    assert len(filled) == 1
    assert filled[0]["event_time"] == ohlc[0][0]


def test_expired_fires_after_expiry_bars_with_zero_touches():
    # 20 bars, none of which ever dip into [100, 110] -- expiry_bars=20 default
    rows = [(200.0 + i, 201.0 + i, 199.0 + i) for i in range(20)]
    ohlc = _lifecycle_ohlc(rows)
    events = fvg.detect_fvg_lifecycle_events(ohlc, BULLISH_GAP, gap_id=1)
    types = [e["event_type"] for e in events]
    assert "fvg_expired" in types
    assert events[[e["event_type"] for e in events].index("fvg_expired")]["event_time"] == ohlc[19][0]


def test_expired_does_not_fire_once_partially_entered():
    rows = [(107.0, 108, 109.0)]  # touches immediately (low=109 <= upper=110)
    rows += [(200.0 + i, 201.0 + i, 199.0 + i) for i in range(25)]  # then drifts away, never fills
    ohlc = _lifecycle_ohlc(rows)
    events = fvg.detect_fvg_lifecycle_events(ohlc, BULLISH_GAP, gap_id=1)
    types = [e["event_type"] for e in events]
    assert "fvg_partially_entered" in types
    assert "fvg_expired" not in types


# ─────────────────────────────────────────────────────────────────────────
# DB persistence
# ─────────────────────────────────────────────────────────────────────────

def test_store_fair_value_gaps_persists_and_is_idempotent(conn):
    gaps = [{
        "gap_type": "bullish", "zone_upper": 110.0, "zone_lower": 100.0,
        "event_time": D0 + timedelta(days=2), "confirmation_time": D0 + timedelta(days=2),
    }]
    ids1 = fvg.store_fair_value_gaps(conn, "XYZ", "daily", gaps)
    ids2 = fvg.store_fair_value_gaps(conn, "XYZ", "daily", gaps)  # re-run must not duplicate

    assert ids1 == ids2
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM fair_value_gaps WHERE symbol='XYZ'")
        assert cur.fetchone()[0] == 1


def test_compute_and_store_fair_value_gaps_persists_gap_and_lifecycle_events(conn):
    with conn.cursor() as cur:
        # A (gap up-side), B, C form a bullish gap; subsequent bars fill it.
        rows = [
            (D0, 98, 100, 95, 98),
            # low raised to 100 (not 104) so bar1/bar3 don't also form an
            # accidental bearish gap (low(bar1) > high(bar3) would fire otherwise)
            (D0 + timedelta(days=1), 107, 108, 100, 107),
            (D0 + timedelta(days=2), 104, 106, 103, 104),  # C: low=103 > high(A)=100 -> bullish gap [100,103]
            (D0 + timedelta(days=3), 101, 102, 100, 101),  # touches (low=100 <= upper=103), fully fills (low<=100)
        ]
        for d, o, h, l, c in rows:
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, ("XYZ", d, o, h, l, c, 1000))
    conn.commit()

    fvg.compute_and_store_fair_value_gaps(conn, "XYZ", "daily", as_of=D0 + timedelta(days=3))

    with conn.cursor() as cur:
        cur.execute("SELECT gap_type, zone_upper, zone_lower FROM fair_value_gaps WHERE symbol='XYZ'")
        gap_rows = cur.fetchall()
        cur.execute("SELECT event_type FROM structural_events WHERE symbol='XYZ' AND reference_type='fvg'")
        event_types = [r[0] for r in cur.fetchall()]

    assert len(gap_rows) == 1
    assert gap_rows[0][0] == "bullish"
    assert "fvg_partially_entered" in event_types
    assert "fvg_fully_filled" in event_types


def test_update_fair_value_gaps_end_to_end(conn):
    import math
    ohlc = []
    price = 50.0
    d = date(2024, 1, 1)
    for i in range(300):
        price *= 1.0015
        c = price + price * 0.05 * math.sin(2 * math.pi * i / 20)
        ohlc.append((d + timedelta(days=i), c, c * 1.01, c * 0.99, c))
    with conn.cursor() as cur:
        for bd, o, h, l, c in ohlc:
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, ("ZIGZAG", bd, o, h, l, c, 1000))
    conn.commit()

    fvg.update_fair_value_gaps(conn, ["ZIGZAG"])

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM fair_value_gaps WHERE symbol='ZIGZAG'")
        count = cur.fetchone()[0]
    assert count > 0

    fvg.update_fair_value_gaps(conn, ["ZIGZAG"])  # idempotent re-run
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM fair_value_gaps WHERE symbol='ZIGZAG'")
        count_again = cur.fetchone()[0]
    assert count_again == count

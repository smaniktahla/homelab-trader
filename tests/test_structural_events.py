"""
Tests for shared/structural_events.py (Price Structure epic PR B).

Structure mirrors tests/test_market_structure.py: pure-function unit tests
against hand-built, hand-verified OHLC series first (bar tuples are
(date, open, high, low, close)), then DB-backed persistence/idempotency/
append-only tests using the conn fixture.
"""

import sys
import pathlib
from datetime import date, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
p = str(ROOT / "shared")
if p not in sys.path:
    sys.path.insert(0, p)

for _mod in ("structural_events", "market_structure", "regime_common"):
    sys.modules.pop(_mod, None)
import structural_events as se
import market_structure as ms

D0 = date(2020, 1, 1)


def _bars(closes, highs=None, lows=None, start=D0):
    """closes-only convenience by default (high=close+0.05, low=close-0.05,
    open=close) -- callers needing real wick behavior pass highs/lows
    explicitly, same length as closes."""
    n = len(closes)
    highs = highs or [c + 0.05 for c in closes]
    lows = lows or [c - 0.05 for c in closes]
    return [(start + timedelta(days=i), closes[i], highs[i], lows[i], closes[i]) for i in range(n)]


RESISTANCE_ZONE = {"id": 501, "zone_type": "resistance", "upper": 10.0, "lower": 9.5}
SUPPORT_ZONE = {"id": 502, "zone_type": "support", "upper": 10.0, "lower": 9.5}


# ─────────────────────────────────────────────────────────────────────────
# breakout / acceptance / failed_breakout bar-counting precision
# ─────────────────────────────────────────────────────────────────────────

def test_breakout_confirms_on_second_qualifying_bar_not_third():
    """breakout_confirmation_bars=2: bar1 first qualifies (event_time),
    bar2 also qualifies -> confirmation_time=bar2, NOT bar3."""
    closes = [9.0, 10.5, 10.6, 9.0]  # bar3 drops back so the streak ends cleanly, not part of this assertion
    ohlc = _bars(closes)
    events = se.detect_zone_events(ohlc, RESISTANCE_ZONE)
    breakouts = [e for e in events if e["event_type"] == "breakout"]
    assert len(breakouts) == 1
    assert breakouts[0]["event_time"] == ohlc[1][0]
    assert breakouts[0]["confirmation_time"] == ohlc[2][0]
    assert breakouts[0]["reference_type"] == "zone" and breakouts[0]["reference_id"] == 501


def test_breakdown_mirrors_breakout_for_support_zone():
    closes = [10.0, 9.4, 9.3, 10.0]
    ohlc = _bars(closes)
    events = se.detect_zone_events(ohlc, SUPPORT_ZONE)
    breakdowns = [e for e in events if e["event_type"] == "breakdown"]
    assert len(breakdowns) == 1
    assert breakdowns[0]["event_time"] == ohlc[1][0]
    assert breakdowns[0]["confirmation_time"] == ohlc[2][0]


def test_acceptance_fires_on_fifth_qualifying_bar_and_references_breakout():
    closes = [9.0, 10.5, 10.6, 10.7, 10.8, 10.9, 11.0]
    ohlc = _bars(closes)
    events = se.detect_zone_events(ohlc, RESISTANCE_ZONE)
    breakout = next(e for e in events if e["event_type"] == "breakout")
    acceptances = [e for e in events if e["event_type"] == "acceptance"]
    assert len(acceptances) == 1
    # 5th qualifying bar including the breakout bar itself = index 5 (bars[1..5])
    assert acceptances[0]["event_time"] == ohlc[5][0]
    assert acceptances[0]["confirmation_time"] == ohlc[5][0]  # same bar, no separate delay
    assert acceptances[0]["reference_type"] == "event"
    assert acceptances[0]["reference_id"] is breakout  # in-memory identity before persistence resolves it


def test_failed_breakout_at_exact_failure_window_boundary_is_emitted():
    """failure_window_bars=5, breakout_confirmation_bars=2: confirmed at
    index 2, so bars 3..7 (5 bars) are within the window. Re-entry at
    index 7 (i - streak_start = 6) must still count."""
    closes = [9.0, 10.5, 10.6, 10.7, 10.8, 10.9, 11.0, 9.8]
    ohlc = _bars(closes)
    events = se.detect_zone_events(ohlc, RESISTANCE_ZONE)
    failed = [e for e in events if e["event_type"] == "failed_breakout"]
    assert len(failed) == 1
    assert failed[0]["event_time"] == ohlc[7][0]
    assert failed[0]["confirmation_time"] == ohlc[7][0]


def test_failed_breakout_one_bar_past_the_window_is_not_emitted():
    """Same shape as above but the re-entry lands one bar later
    (i - streak_start = 7, past the boundary) -- must NOT be flagged as a
    failure of the original breakout."""
    closes = [9.0, 10.5, 10.6, 10.7, 10.8, 10.9, 11.0, 11.1, 9.8]
    ohlc = _bars(closes)
    events = se.detect_zone_events(ohlc, RESISTANCE_ZONE)
    failed = [e for e in events if e["event_type"] == "failed_breakout"]
    assert failed == []


def test_failed_breakout_references_the_original_breakout_event_object():
    closes = [9.0, 10.5, 10.6, 9.8]
    ohlc = _bars(closes)
    events = se.detect_zone_events(ohlc, RESISTANCE_ZONE)
    breakout = next(e for e in events if e["event_type"] == "breakout")
    failed = next(e for e in events if e["event_type"] == "failed_breakout")
    assert failed["reference_type"] == "event"
    assert failed["reference_id"] is breakout


# ─────────────────────────────────────────────────────────────────────────
# rejection / sweep_reclaim -- precedence, no double-counting (point 5)
# ─────────────────────────────────────────────────────────────────────────

def test_plain_touch_without_pierce_is_rejection():
    closes = [9.0, 8.0, 9.0]
    highs = [9.0, 8.0, 9.7]   # touches into [9.5, 10.0] but never crosses upper=10.0
    lows = [8.8, 7.8, 9.2]
    ohlc = _bars(closes, highs=highs, lows=lows)
    events = se.detect_zone_events(ohlc, RESISTANCE_ZONE)
    assert len(events) == 1
    assert events[0]["event_type"] == "rejection"
    assert events[0]["metadata"]["reason"] == "touch"


def test_far_side_pierce_with_same_bar_reclaim_is_sweep_not_rejection():
    closes = [9.0, 9.2]      # bar1's own close already back below lower=9.5 -- same-bar reclaim
    highs = [9.0, 10.3]      # pierces upper=10.0
    lows = [8.8, 9.6]
    ohlc = _bars(closes, highs=highs, lows=lows)
    events = se.detect_zone_events(ohlc, RESISTANCE_ZONE)
    assert len(events) == 1
    assert events[0]["event_type"] == "sweep_reclaim"
    assert events[0]["event_time"] == ohlc[1][0]
    assert events[0]["confirmation_time"] == ohlc[1][0]
    # must not ALSO emit a generic rejection for the same interaction
    assert not any(e["event_type"] == "rejection" for e in events)


def test_far_side_pierce_with_next_bar_reclaim_is_sweep_with_later_confirmation():
    closes = [9.0, 9.7, 9.3]   # bar1 pierces but closes inside the zone; bar2 decisively reclaims below lower
    highs = [9.0, 10.3, 9.4]
    lows = [8.8, 9.6, 9.1]
    ohlc = _bars(closes, highs=highs, lows=lows)
    events = se.detect_zone_events(ohlc, RESISTANCE_ZONE)
    assert len(events) == 1
    assert events[0]["event_type"] == "sweep_reclaim"
    assert events[0]["event_time"] == ohlc[1][0]
    assert events[0]["confirmation_time"] == ohlc[2][0]


def test_far_side_pierce_without_reclaim_within_window_is_rejection():
    # bar1 pierces but never decisively reclaims (stays inside the zone).
    # No bar2 at all -- any bar whose close is still inside [lower, upper]
    # necessarily also overlaps the zone (touches), so isolating "pierce,
    # no reclaim, and nothing else fires" cleanly means not giving the
    # window a second bar to independently register its own touch.
    closes = [9.0, 9.7]
    highs = [9.0, 10.3]
    lows = [8.8, 9.6]
    ohlc = _bars(closes, highs=highs, lows=lows)
    events = se.detect_zone_events(ohlc, RESISTANCE_ZONE)
    assert len(events) == 1
    assert events[0]["event_type"] == "rejection"
    assert events[0]["metadata"]["reason"] == "far_side_pierce_no_reclaim"


# ─────────────────────────────────────────────────────────────────────────
# swing-anchored structure-failure events -- confirmation-time invariant
# ─────────────────────────────────────────────────────────────────────────

def _d(n):
    return D0 + timedelta(days=n)


def test_structure_failure_fires_once_when_close_breaks_last_confirmed_low():
    swing_lows = [
        {"id": 1, "date": _d(1), "confirmation_time": _d(4), "price": 95.0},
        {"id": 2, "date": _d(10), "confirmation_time": _d(13), "price": 100.0},
    ]
    swing_highs = [
        {"id": 10, "date": _d(2), "confirmation_time": _d(5), "price": 110.0},
        {"id": 11, "date": _d(11), "confirmation_time": _d(14), "price": 120.0},
    ]
    closes = {i: 105.0 for i in range(0, 20)}
    closes[7] = 90.0    # dips below low#1 (95) but BEFORE low#2 confirms (d13) -- must not trigger
    closes[16] = 95.0   # after both confirmed (d14) -- must trigger, referencing low#2 (price 100)
    closes[17] = 93.0   # still broken -- must NOT re-trigger a second event for the same swing
    ohlc = [(_d(i), closes[i], closes[i] + 0.1, closes[i] - 0.1, closes[i]) for i in range(0, 20)]

    events = se.detect_structure_failure_events(ohlc, swing_highs, swing_lows)

    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == "possible_bearish_structure_failure"
    assert ev["reference_type"] == "swing" and ev["reference_id"] == 2
    assert ev["event_time"] == _d(16)
    assert ev["confirmation_time"] == _d(16)
    assert ev["metadata"]["prevailing_trend"] == "higher_highs_higher_lows"


def test_structure_failure_mirror_for_bullish_break_of_downtrend():
    swing_highs = [
        {"id": 10, "date": _d(1), "confirmation_time": _d(4), "price": 120.0},
        {"id": 11, "date": _d(10), "confirmation_time": _d(13), "price": 110.0},
    ]
    swing_lows = [
        {"id": 1, "date": _d(2), "confirmation_time": _d(5), "price": 100.0},
        {"id": 2, "date": _d(11), "confirmation_time": _d(14), "price": 95.0},
    ]
    closes = {i: 105.0 for i in range(0, 20)}
    closes[16] = 115.0  # closes above last confirmed high (110) -- bullish break of a downtrend
    ohlc = [(_d(i), closes[i], closes[i] + 0.1, closes[i] - 0.1, closes[i]) for i in range(0, 20)]

    events = se.detect_structure_failure_events(ohlc, swing_highs, swing_lows)

    assert len(events) == 1
    assert events[0]["event_type"] == "possible_bullish_structure_failure"
    assert events[0]["reference_type"] == "swing" and events[0]["reference_id"] == 11


# ─────────────────────────────────────────────────────────────────────────
# DB persistence -- idempotency, cross-run reference resolution, append-
# only behavior
# ─────────────────────────────────────────────────────────────────────────

def _insert_zone(conn, symbol, timeframe, zone_type, upper, lower):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO structural_zones (symbol, timeframe, zone_type, center_price, upper, lower, touch_count, last_touched, active)
            VALUES (%s,%s,%s,%s,%s,%s,1,%s,TRUE) RETURNING id
        """, (symbol, timeframe, zone_type, (upper + lower) / 2, upper, lower, D0))
        zid = cur.fetchone()[0]
    conn.commit()
    return zid


def test_store_structural_events_persists_and_is_idempotent(conn):
    zid = _insert_zone(conn, "XYZ", "daily", "resistance", 10.0, 9.5)
    events = [{
        "event_type": "breakout", "reference_type": "zone", "reference_id": zid,
        "event_time": _d(1), "confirmation_time": _d(2), "metadata": {"params": {}},
    }]
    se.store_structural_events(conn, "XYZ", "daily", events)
    se.store_structural_events(conn, "XYZ", "daily", events)  # re-run must not duplicate

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM structural_events WHERE symbol='XYZ'")
        assert cur.fetchone()[0] == 1


def test_store_structural_events_resolves_event_reference_to_real_row_id(conn):
    zid = _insert_zone(conn, "XYZ", "daily", "resistance", 10.0, 9.5)
    breakout = {
        "event_type": "breakout", "reference_type": "zone", "reference_id": zid,
        "event_time": _d(1), "confirmation_time": _d(2), "metadata": {"params": {}},
    }
    acceptance = {
        "event_type": "acceptance", "reference_type": "event", "reference_id": breakout,
        "event_time": _d(5), "confirmation_time": _d(5), "metadata": {"params": {}},
    }
    se.store_structural_events(conn, "XYZ", "daily", [breakout, acceptance])

    with conn.cursor() as cur:
        cur.execute("SELECT id FROM structural_events WHERE symbol='XYZ' AND event_type='breakout'")
        breakout_id = cur.fetchone()[0]
        cur.execute("SELECT reference_type, reference_id FROM structural_events WHERE symbol='XYZ' AND event_type='acceptance'")
        ref_type, ref_id = cur.fetchone()
    assert ref_type == "event"
    assert ref_id == breakout_id


def test_append_only_across_reruns_never_mutates_the_original_breakout_row(conn):
    """Simulates two separate daily cron cycles: day 1 sees only the
    confirmed breakout; day 5's full replay re-detects the SAME breakout
    (a fresh dict, different Python identity) plus a new failed_breakout
    referencing it. The breakout row must be untouched (same id, same
    event_time) across both runs, and the failed_breakout must resolve to
    that same real row via the natural-key lookup, not a duplicate."""
    zid = _insert_zone(conn, "XYZ", "daily", "resistance", 10.0, 9.5)
    day1_breakout = {
        "event_type": "breakout", "reference_type": "zone", "reference_id": zid,
        "event_time": _d(1), "confirmation_time": _d(2), "metadata": {"params": {}},
    }
    se.store_structural_events(conn, "XYZ", "daily", [day1_breakout])
    with conn.cursor() as cur:
        cur.execute("SELECT id, computed_at FROM structural_events WHERE symbol='XYZ' AND event_type='breakout'")
        breakout_id_day1, computed_at_day1 = cur.fetchone()

    day5_breakout = dict(day1_breakout)  # re-detected fresh, same facts, different object
    day5_failed = {
        "event_type": "failed_breakout", "reference_type": "event", "reference_id": day5_breakout,
        "event_time": _d(7), "confirmation_time": _d(7), "metadata": {"params": {}},
    }
    se.store_structural_events(conn, "XYZ", "daily", [day5_breakout, day5_failed])

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM structural_events WHERE symbol='XYZ' AND event_type='breakout'")
        assert cur.fetchone()[0] == 1  # still exactly one breakout row -- never duplicated or mutated
        cur.execute("SELECT id, computed_at FROM structural_events WHERE symbol='XYZ' AND event_type='breakout'")
        breakout_id_day5, computed_at_day5 = cur.fetchone()
        assert breakout_id_day5 == breakout_id_day1
        assert computed_at_day5 == computed_at_day1  # row genuinely untouched, not re-written
        cur.execute("SELECT reference_id FROM structural_events WHERE symbol='XYZ' AND event_type='failed_breakout'")
        assert cur.fetchone()[0] == breakout_id_day1


def test_update_structural_events_end_to_end(conn):
    """Real wiring smoke test: seed price_history, run
    market_structure.update_market_structure (populates structural_swings/
    structural_zones per PR A), then structural_events.update_structural_events
    on top, and confirm at least some events land."""
    ohlc = []
    price = 50.0
    d = date(2024, 1, 1)
    import math
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

    ms.update_market_structure(conn, ["ZIGZAG"])
    se.update_structural_events(conn, ["ZIGZAG"])

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM structural_events WHERE symbol='ZIGZAG'")
        count = cur.fetchone()[0]
    assert count > 0

    # re-running must not blow up or duplicate anything (idempotent replay)
    se.update_structural_events(conn, ["ZIGZAG"])
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM structural_events WHERE symbol='ZIGZAG'")
        count_again = cur.fetchone()[0]
    assert count_again == count

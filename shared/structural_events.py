"""
Structural event classification (Price Structure epic PR B). Detects
price/zone interaction events (breakout/breakdown/acceptance/rejection/
failed_breakout/failed_breakdown/sweep_reclaim) anchored to persisted
structural_zones (shared/market_structure.py, PR A), plus swing-anchored
structure-failure warnings (possible_bullish_structure_failure/
possible_bearish_structure_failure) anchored to persisted
structural_swings -- the same CHoCH condition market_structure.py's
_detect_bos_choch already computes, now event-logged with its own
confirmation_time instead of only a same-day boolean on
market_structure_history.

Append-only, never retroactive: a breakout confirmed at bar N is a
permanent fact of what was knowable at N. If price later re-enters the
zone, that produces a NEW failed_breakout event referencing the original
breakout event's id (reference_type="event") -- the original breakout row
is never rewritten. Same for acceptance: a breakout that holds long
enough produces a second, later event referencing the first, not a
mutation of it. A future backtest/replay can stop at any historical
as_of and see exactly the events that existed then.

Detection replays each (symbol, timeframe)'s full bar series in
chronological order every run -- same "recompute idempotently from source
data, not incremental per-cycle state" discipline PR A's zone touch-
counting used, rather than maintaining separate in-progress-candidate
state tables. ON CONFLICT DO NOTHING on the natural key
(symbol, timeframe, event_type, reference_type, reference_id, event_time)
makes repeated runs, and historical backfill/replay, safe: nothing here
reads "today's final zone state" and projects it backward. Zones are
themselves always rebuilt from confirmed swings only (see
market_structure.update_structural_zones), so walking the persisted zone
list against the bar series in order is as-of-time-correct by
construction.

market_structure_history's existing bos/choch booleans and
shared/structure_scoring.py are both untouched by this module -- this is
purely additive, feeding the epic's later feature_registry integration
(PR C), not a rewire of the live scoring path. No scoring or live-trading
behavior changes as a result of this module existing.
"""

import logging
import sys
import pathlib
from datetime import date as date_cls

import psycopg2.extensions
from psycopg2.extras import Json

_here = pathlib.Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from regime_common import load_daily_ohlc, asof_index  # noqa: E402
from market_structure import resample_weekly, resample_monthly  # noqa: E402

log = logging.getLogger(__name__)

# calculation_version -> the exact bar-count thresholds that version used.
# A later parameter change adds a new version key rather than mutating
# this one, so every already-stored event's metadata.params (see
# _params_for_version) remains an honest record of what produced it, and
# a re-run under a new version doesn't collide with old rows (different
# calculation_version is not part of the unique key, but the *events*
# should look different enough in practice -- e.g. different
# confirmation_time -- that this hasn't needed special-casing yet; if a
# parameter change ever needs to fully supersede old events for the same
# facts, that's a deliberate follow-up, not silent behavior here).
EVENT_DETECTION_PARAMS = {
    1: {
        # consecutive qualifying closes beyond the zone boundary, INCLUDING
        # the first (event_time) bar, required before a breakout/breakdown
        # is confirmed. confirmation_time lands on the bar where the count
        # reaches this threshold -- e.g. =2 means bar N closes beyond
        # (event_time=N), bar N+1 also does (confirmation_time=N+1), NOT
        # N+2. See detect_zone_events' docstring for the full worked case.
        "breakout_confirmation_bars": 2,
        # consecutive qualifying closes, including the original breakout
        # bar, before an already-confirmed breakout/breakdown additionally
        # gets an acceptance event. acceptance's event_time and
        # confirmation_time are the same bar (the Nth qualifying close) --
        # there's no separate "knowable but not yet confirmed" period for
        # a pure bar-count threshold like this one.
        "acceptance_bars": 5,
        # bars after a breakout/breakdown's OWN confirmation_time within
        # which a close re-entering the zone still counts as a failure of
        # that specific breakout/breakdown, not an unrelated new touch.
        "failure_window_bars": 5,
        # bars after a bar that pierces the zone's far boundary without a
        # confirming close, within which a reclaim close still counts as
        # sweep_reclaim rather than a plain rejection.
        "sweep_reclaim_window_bars": 1,
    },
}
EVENT_CALCULATION_VERSION = 1


def _params_for_version(calculation_version):
    return EVENT_DETECTION_PARAMS[calculation_version]


# ─────────────────────────────────────────────────────────────────────────
# Zone-anchored events: breakout/breakdown/acceptance/rejection/
# failed_breakout/failed_breakdown/sweep_reclaim. Pure function over one
# zone's boundaries and the full bar series -- no DB, no network.
# ─────────────────────────────────────────────────────────────────────────

def detect_zone_events(ohlc, zone, calculation_version=EVENT_CALCULATION_VERSION):
    """ohlc: (date, open, high, low, close) oldest->newest. zone: dict with
    id/zone_type("support"|"resistance")/upper/lower. Returns a list of
    raw event dicts: {event_type, reference_type, reference_id, event_time,
    confirmation_time, metadata}.

    Direction is fixed by zone_type: a resistance zone can only produce
    breakout/acceptance/failed_breakout (price moving up through it) plus
    rejection/sweep_reclaim on approach from below; a support zone the
    mirror image moving down. A single zone is never both in the same
    walk -- this matches how _cluster_zones already separates highs
    (resistance) from lows (support) when building structural_zones.

    Worked example for breakout_confirmation_bars=2: bar N's close is the
    first to close beyond the zone (event_time=N). If bar N+1's close is
    ALSO beyond the zone, the streak has reached 2 qualifying bars and the
    breakout confirms with confirmation_time=N+1 -- not N+2. The count
    includes the event bar itself; "2-bar confirmation" means 2 total
    qualifying bars, not 2 bars *after* the first one.
    """
    params = _params_for_version(calculation_version)
    resistance = zone["zone_type"] == "resistance"
    upper, lower = zone["upper"], zone["lower"]

    def qualifies(bar):
        c = bar[4]
        return c > upper if resistance else c < lower

    def touches(bar):
        h, l = bar[2], bar[3]
        return h >= lower and l <= upper

    def pierces_far_side(bar):
        return bar[2] > upper if resistance else bar[3] < lower

    def reclaimed(bar):
        # A genuine sweep+reclaim needs a decisive round-trip: the close
        # goes all the way back through the zone's NEAR boundary (the one
        # on the pre-approach side), not merely "didn't confirm a break."
        # A close that lands inside the zone (between lower and upper)
        # without confirming a break already falls through to "touches"/
        # rejection -- this function is only asked about bars that
        # pierced the far side, and must not trivially always be true for
        # them, or "rejection" could never be reached.
        c = bar[4]
        return c < lower if resistance else c > upper

    events = []
    n = len(ohlc)
    streak_start = None       # index of the streak's first qualifying bar
    streak_count = 0
    breakout_event = None     # the confirmed breakout/breakdown event dict for the current streak, once emitted
    acceptance_emitted = False

    i = 0
    while i < n:
        bar = ohlc[i]
        if streak_start is not None:
            if qualifies(bar):
                streak_count += 1
                if breakout_event is None and streak_count == params["breakout_confirmation_bars"]:
                    breakout_event = {
                        "event_type": "breakout" if resistance else "breakdown",
                        "reference_type": "zone", "reference_id": zone["id"],
                        "event_time": ohlc[streak_start][0], "confirmation_time": bar[0],
                        "metadata": {"params": params, "zone_upper": upper, "zone_lower": lower},
                    }
                    events.append(breakout_event)
                if (breakout_event is not None and not acceptance_emitted
                        and streak_count == params["acceptance_bars"]):
                    events.append({
                        "event_type": "acceptance",
                        "reference_type": "event", "reference_id": breakout_event,  # resolved to a real id at persist time
                        "event_time": bar[0], "confirmation_time": bar[0],
                        "metadata": {"params": params, "streak_bars": streak_count},
                    })
                    acceptance_emitted = True
            else:
                if (breakout_event is not None
                        and (i - streak_start) <= params["failure_window_bars"] + params["breakout_confirmation_bars"] - 1):
                    events.append({
                        "event_type": "failed_breakout" if resistance else "failed_breakdown",
                        "reference_type": "event", "reference_id": breakout_event,
                        "event_time": bar[0], "confirmation_time": bar[0],
                        "metadata": {"params": params},
                    })
                streak_start, streak_count, breakout_event, acceptance_emitted = None, 0, None, False
                continue  # re-evaluate this same bar as a fresh non-streak bar below
        else:
            if qualifies(bar):
                streak_start, streak_count = i, 1
                if params["breakout_confirmation_bars"] == 1:
                    breakout_event = {
                        "event_type": "breakout" if resistance else "breakdown",
                        "reference_type": "zone", "reference_id": zone["id"],
                        "event_time": bar[0], "confirmation_time": bar[0],
                        "metadata": {"params": params, "zone_upper": upper, "zone_lower": lower},
                    }
                    events.append(breakout_event)
            elif pierces_far_side(bar):
                reclaim_idx = None
                for j in range(i, min(i + params["sweep_reclaim_window_bars"] + 1, n)):
                    if reclaimed(ohlc[j]):
                        reclaim_idx = j
                        break
                if reclaim_idx is not None:
                    events.append({
                        "event_type": "sweep_reclaim",
                        "reference_type": "zone", "reference_id": zone["id"],
                        "event_time": bar[0], "confirmation_time": ohlc[reclaim_idx][0],
                        "metadata": {"params": params},
                    })
                else:
                    events.append({
                        "event_type": "rejection",
                        "reference_type": "zone", "reference_id": zone["id"],
                        "event_time": bar[0], "confirmation_time": bar[0],
                        "metadata": {"params": params, "reason": "far_side_pierce_no_reclaim"},
                    })
            elif touches(bar):
                events.append({
                    "event_type": "rejection",
                    "reference_type": "zone", "reference_id": zone["id"],
                    "event_time": bar[0], "confirmation_time": bar[0],
                    "metadata": {"params": params, "reason": "touch"},
                })
        i += 1

    return events


# ─────────────────────────────────────────────────────────────────────────
# Swing-anchored structure-failure events: possible_bullish_structure_
# failure / possible_bearish_structure_failure. Same trigger condition as
# market_structure._detect_bos_choch's CHoCH branch, replayed bar-by-bar
# so each swing's FIRST violation gets its own event with a real
# confirmation_time, instead of only today's boolean.
# ─────────────────────────────────────────────────────────────────────────

def detect_structure_failure_events(ohlc, swing_highs, swing_lows, calculation_version=EVENT_CALCULATION_VERSION):
    """swing_highs/swing_lows: lists of {id, date, confirmation_time,
    price} from structural_swings, any order. ohlc: full bar series,
    oldest->newest. At each bar, only swings already confirmed as of that
    bar's date participate (confirmation_time <= bar date) -- trend
    direction and the "last swing high/low" are both computed from that
    as-of-safe subset only, mirroring market_structure._trend_direction/
    _detect_bos_choch exactly, just walked forward through time instead
    of evaluated once for "today." Each swing can trigger at most one
    event (the first bar its violation condition holds); subsequent bars
    where the price remains beyond that swing do not re-emit."""
    params = _params_for_version(calculation_version)
    highs_sorted = sorted(swing_highs, key=lambda s: s["date"])
    lows_sorted = sorted(swing_lows, key=lambda s: s["date"])

    events = []
    already_triggered = set()  # swing ids already emitted

    for bar in ohlc:
        bar_date, close = bar[0], bar[4]
        confirmed_highs = [s for s in highs_sorted if s["confirmation_time"] <= bar_date]
        confirmed_lows = [s for s in lows_sorted if s["confirmation_time"] <= bar_date]
        if len(confirmed_highs) < 2 or len(confirmed_lows) < 2:
            continue
        highs_rising = confirmed_highs[-1]["price"] > confirmed_highs[-2]["price"]
        highs_falling = confirmed_highs[-1]["price"] < confirmed_highs[-2]["price"]
        lows_rising = confirmed_lows[-1]["price"] > confirmed_lows[-2]["price"]
        lows_falling = confirmed_lows[-1]["price"] < confirmed_lows[-2]["price"]
        if highs_rising and lows_rising:
            trend = "higher_highs_higher_lows"
        elif highs_falling and lows_falling:
            trend = "lower_highs_lower_lows"
        else:
            continue  # mixed/insufficient -- nothing can be "broken"

        last_high, last_low = confirmed_highs[-1], confirmed_lows[-1]
        if trend == "higher_highs_higher_lows" and close < last_low["price"] and last_low["id"] not in already_triggered:
            events.append({
                "event_type": "possible_bearish_structure_failure",
                "reference_type": "swing", "reference_id": last_low["id"],
                "event_time": bar_date, "confirmation_time": bar_date,
                "metadata": {"params": params, "prevailing_trend": trend, "violated_price": last_low["price"]},
            })
            already_triggered.add(last_low["id"])
        elif trend == "lower_highs_lower_lows" and close > last_high["price"] and last_high["id"] not in already_triggered:
            events.append({
                "event_type": "possible_bullish_structure_failure",
                "reference_type": "swing", "reference_id": last_high["id"],
                "event_time": bar_date, "confirmation_time": bar_date,
                "metadata": {"params": params, "prevailing_trend": trend, "violated_price": last_high["price"]},
            })
            already_triggered.add(last_high["id"])

    return events


# ─────────────────────────────────────────────────────────────────────────
# I/O orchestration -- load zones/swings/bars for one (symbol, timeframe),
# run both detectors, persist. Mirrors market_structure.py's compute/
# store split.
# ─────────────────────────────────────────────────────────────────────────

def _load_active_zones(conn, symbol, timeframe):
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute("""
            SELECT id, zone_type, upper, lower FROM structural_zones
            WHERE symbol=%s AND timeframe=%s AND active=TRUE
        """, (symbol, timeframe))
        rows = cur.fetchall()
    return [{"id": r[0], "zone_type": r[1], "upper": float(r[2]), "lower": float(r[3])} for r in rows]


def _load_swings(conn, symbol, timeframe, swing_type):
    with conn.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute("""
            SELECT id, event_time, confirmation_time, price FROM structural_swings
            WHERE symbol=%s AND timeframe=%s AND swing_type=%s
            ORDER BY event_time ASC
        """, (symbol, timeframe, swing_type))
        rows = cur.fetchall()
    return [{"id": r[0], "date": r[1], "confirmation_time": r[2], "price": float(r[3])} for r in rows]


def _resample_for_timeframe(daily_ohlc, timeframe):
    if timeframe == "daily":
        return daily_ohlc
    if timeframe == "weekly":
        return resample_weekly(daily_ohlc)
    if timeframe == "monthly":
        return resample_monthly(daily_ohlc)
    raise ValueError(f"unknown timeframe: {timeframe}")


def compute_structural_events(conn, symbol, timeframe, as_of=None, calculation_version=EVENT_CALCULATION_VERSION):
    """I/O orchestrator for one (symbol, timeframe): loads price history
    as-of-sliced through as_of (no lookahead into bars beyond the
    evaluation date), the persisted zones, and the persisted swings, then
    runs both detectors. Returns a flat list of raw event dicts (not yet
    persisted) -- store_structural_events resolves each "event"-typed
    reference to a real row id and writes them."""
    target_date = as_of or date_cls.today()
    try:
        daily_all = load_daily_ohlc(conn, symbol)
    except Exception:
        daily_all = []
    dates = [b[0] for b in daily_all]
    idx = asof_index(dates, target_date)
    daily_ohlc = daily_all[:idx + 1] if idx is not None else []
    ohlc = _resample_for_timeframe(daily_ohlc, timeframe)
    if not ohlc:
        return []

    zones = _load_active_zones(conn, symbol, timeframe)
    swing_highs = _load_swings(conn, symbol, timeframe, "high")
    swing_lows = _load_swings(conn, symbol, timeframe, "low")

    events = []
    for zone in zones:
        events.extend(detect_zone_events(ohlc, zone, calculation_version))
    events.extend(detect_structure_failure_events(ohlc, swing_highs, swing_lows, calculation_version))
    return events


def store_structural_events(conn, symbol, timeframe, events, calculation_version=EVENT_CALCULATION_VERSION):
    """Persists a flat event list from compute_structural_events, in
    order, resolving any "event"-typed reference (acceptance/
    failed_breakout/failed_breakdown pointing at the breakout/breakdown
    dict that produced them, not yet a DB id) to the just-inserted row's
    real id first. ON CONFLICT DO NOTHING on the natural key makes this
    safe to call every cycle with the full replayed list -- already-
    stored events are silently skipped, never rewritten."""
    resolved_ids = {}  # id(dict) -> real DB id, for events referencing an in-batch event dict
    with conn.cursor() as cur:
        for ev in events:
            reference_id = ev["reference_id"]
            if ev["reference_type"] == "event" and isinstance(reference_id, dict):
                real_id = resolved_ids.get(id(reference_id))
                if real_id is None:
                    # the referenced event wasn't inserted this call (already
                    # existed from a prior run) -- look it up by natural key.
                    cur.execute("""
                        SELECT id FROM structural_events
                        WHERE symbol=%s AND timeframe=%s AND event_type=%s
                          AND reference_type=%s AND reference_id=%s AND event_time=%s
                    """, (symbol, timeframe, reference_id["event_type"], reference_id["reference_type"],
                          reference_id["reference_id"], reference_id["event_time"]))
                    row = cur.fetchone()
                    real_id = row[0] if row else None
                if real_id is None:
                    continue  # referenced event genuinely missing -- skip, don't insert a dangling reference
                reference_id = real_id

            cur.execute("""
                INSERT INTO structural_events
                    (symbol, timeframe, event_type, reference_type, reference_id,
                     event_time, confirmation_time, metadata, calculation_version)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, timeframe, event_type, reference_type, reference_id, event_time)
                DO NOTHING
                RETURNING id
            """, (symbol, timeframe, ev["event_type"], ev["reference_type"], reference_id,
                  ev["event_time"], ev["confirmation_time"], Json(ev["metadata"]), calculation_version))
            row = cur.fetchone()
            if row is None:
                # already existed -- fetch its id so later events in this
                # same batch can still resolve a reference to it.
                cur.execute("""
                    SELECT id FROM structural_events
                    WHERE symbol=%s AND timeframe=%s AND event_type=%s
                      AND reference_type=%s AND reference_id=%s AND event_time=%s
                """, (symbol, timeframe, ev["event_type"], ev["reference_type"], reference_id, ev["event_time"]))
                row = cur.fetchone()
            if row is not None and ev["reference_type"] == "zone" and ev["event_type"] in ("breakout", "breakdown"):
                resolved_ids[id(ev)] = row[0]
    conn.commit()


def update_structural_events(conn, symbols, timeframes=("daily", "weekly", "monthly")):
    """Compute + persist today's structural events for every symbol and
    timeframe. Each (symbol, timeframe) pair is isolated in its own
    try/except -- one bad pair must never stop the rest, same discipline
    as market_structure.update_market_structure. Expected to run after
    update_market_structure in the same ingest cycle, since it reads that
    call's freshly-persisted structural_zones/structural_swings rows."""
    today = date_cls.today()
    for symbol in symbols:
        for timeframe in timeframes:
            try:
                events = compute_structural_events(conn, symbol, timeframe, as_of=today)
                if events:
                    store_structural_events(conn, symbol, timeframe, events)
            except Exception as e:
                log.warning(f"Structural event update failed for {symbol}/{timeframe}: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass

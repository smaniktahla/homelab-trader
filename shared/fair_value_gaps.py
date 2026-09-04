"""
Fair Value Gap (FVG) detection, Price Structure epic PR B2. A pure
3-candle price-imbalance pattern, unrelated to minute-level/multi-
timeframe data (works fine on daily or hourly bars, see the epic's own
investigation note ruling multi-timeframe alignment infeasible today but
FVGs immediately buildable).

Candles A, B, C, oldest to newest, consecutive:
- bullish gap: high(A) < low(C) (strict -- an exact touch has zero width
  and is not a gap), zone = [high(A), low(C)].
- bearish gap: low(A) > high(C) (strict), zone = [high(C), low(A)].

confirmation_time = event_time = candle C's own date. Unlike a swing
(shared/market_structure.py), an FVG needs no bars beyond its own 3rd
candle to be confirmed -- there is no separate confirmation delay here.

Identity vs. lifecycle, same split PR A/B established for swings/zones/
events: fair_value_gaps is an IMMUTABLE identity table (zone bounds and
creation date never change once inserted). Every fill-status milestone --
partially_entered, midpoint_reached, fully_filled, invalidated, expired --
is logged as a NEW row in the existing structural_events table
(reference_type="fvg", reference_id -> fair_value_gaps.id), reusing PR
B's append-only infrastructure rather than a second parallel mutable-
status system. This also means an FVG's "current fill state" is never
read directly off a mutable column -- a consumer that wants it (or wants
it as-of some historical date) reconstructs it from the ordered
structural_events rows for that gap, the same way structural_zones'
mutable state was ruled unsafe for historical as_of reads in PR C.

Milestones are independently gated (each fires at most once per gap) but
NOT mutually exclusive within a single bar or across time: fully_filled
is not terminal, and invalidated may fire on the same bar as fully_filled
or on a later one -- a single dramatic bar can wick all the way through a
gap (fully_filled) and close beyond it (invalidated) at once, and a gap
that was merely wicked-through earlier can still be closed-through and
invalidated on a later bar. expired only fires while the gap is still in
its "never touched" state (partially_entered has not yet fired) -- a gap
that already started filling doesn't "expire," it has whatever real state
it has.

Detection replays each (symbol, timeframe)'s full bar series in
chronological order every cycle, same idempotent-recompute discipline PR
A/B used, rather than incremental per-cycle state. ON CONFLICT DO NOTHING
on fair_value_gaps' natural key and structural_events' own natural key
both make repeated runs safe.
"""

import logging
import sys
import pathlib
from datetime import date as date_cls

import psycopg2.extensions

_here = pathlib.Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from regime_common import load_daily_ohlc, asof_index  # noqa: E402
from market_structure import resample_weekly, resample_monthly  # noqa: E402
from structural_events import store_structural_events  # noqa: E402

log = logging.getLogger(__name__)

# calculation_version -> thresholds this version used, same versioning
# discipline as structural_events.EVENT_DETECTION_PARAMS. midpoint_fraction
# is the classic ICT/SMC "consequent encroachment" 50% level, exposed as a
# parameter rather than a hard-coded 0.5 so it can be revisited.
FVG_DETECTION_PARAMS = {
    1: {
        # bars after creation, with zero touches, before an untouched gap
        # is marked expired. Only applies from the untouched state -- a
        # gap that has already partially filled never "expires."
        "expiry_bars": 20,
        "midpoint_fraction": 0.5,
    },
}
FVG_CALCULATION_VERSION = 1


def _params_for_version(calculation_version):
    return FVG_DETECTION_PARAMS[calculation_version]


# ─────────────────────────────────────────────────────────────────────────
# Gap detection -- pure function over the bar series, no DB.
# ─────────────────────────────────────────────────────────────────────────

def detect_fair_value_gaps(ohlc):
    """ohlc: (date, open, high, low, close) oldest->newest. Returns a list
    of raw gap dicts: {gap_type, zone_upper, zone_lower, event_time,
    confirmation_time}. Every consecutive (A, B, C) triple is checked
    independently -- overlapping/adjacent gaps are both kept, no
    deduplication or merging (each is its own fact about a specific
    3-candle pattern)."""
    gaps = []
    for i in range(2, len(ohlc)):
        a, c = ohlc[i - 2], ohlc[i]
        a_high, a_low = a[2], a[3]
        c_high, c_low = c[2], c[3]
        if a_high < c_low:
            gaps.append({
                "gap_type": "bullish", "zone_upper": c_low, "zone_lower": a_high,
                "event_time": c[0], "confirmation_time": c[0],
            })
        elif a_low > c_high:
            gaps.append({
                "gap_type": "bearish", "zone_upper": a_low, "zone_lower": c_high,
                "event_time": c[0], "confirmation_time": c[0],
            })
    return gaps


# ─────────────────────────────────────────────────────────────────────────
# Lifecycle detection -- walks bars AFTER a gap's own creation bar,
# producing structural_events-shaped milestone dicts.
# ─────────────────────────────────────────────────────────────────────────

def detect_fvg_lifecycle_events(ohlc, gap, gap_id, calculation_version=FVG_CALCULATION_VERSION):
    """ohlc: full bar series, oldest->newest (only bars strictly after
    gap["event_time"] are examined -- the creation bar itself cannot also
    be a fill/touch bar, since the gap's own zone is defined BY that bar's
    high/low). gap: a dict with gap_type/zone_upper/zone_lower/event_time.
    gap_id: the real fair_value_gaps.id this gap was persisted under.
    Returns a list of structural_events-shaped dicts (event_type,
    reference_type="fvg", reference_id=gap_id, event_time,
    confirmation_time, metadata)."""
    params = _params_for_version(calculation_version)
    bullish = gap["gap_type"] == "bullish"
    upper, lower = gap["zone_upper"], gap["zone_lower"]
    midpoint = lower + (upper - lower) * params["midpoint_fraction"]

    def touches(bar):
        return bar[3] <= upper if bullish else bar[2] >= lower

    def reaches_midpoint(bar):
        return bar[3] <= midpoint if bullish else bar[2] >= midpoint

    def fully_fills(bar):
        return bar[3] <= lower if bullish else bar[2] >= upper

    def invalidates(bar):
        # CLOSE strictly beyond the far boundary -- a decisive close-
        # through, stronger than fully_fills (which only needs an
        # intrabar wick to reach the far edge). A close exactly AT the
        # far boundary counts as fully_filled but not yet invalidated.
        c = bar[4]
        return c < lower if bullish else c > upper

    events = []
    emitted = {"partially_entered": False, "midpoint_reached": False, "fully_filled": False, "invalidated": False}
    bars_since_creation = 0
    expired = False

    for bar in ohlc:
        if bar[0] <= gap["event_time"]:
            continue
        bars_since_creation += 1

        if not emitted["partially_entered"] and touches(bar):
            events.append({
                "event_type": "fvg_partially_entered", "reference_type": "fvg", "reference_id": gap_id,
                "event_time": bar[0], "confirmation_time": bar[0],
                "metadata": {"params": params},
            })
            emitted["partially_entered"] = True
        if not emitted["midpoint_reached"] and reaches_midpoint(bar):
            events.append({
                "event_type": "fvg_midpoint_reached", "reference_type": "fvg", "reference_id": gap_id,
                "event_time": bar[0], "confirmation_time": bar[0],
                "metadata": {"params": params},
            })
            emitted["midpoint_reached"] = True
        if not emitted["fully_filled"] and fully_fills(bar):
            events.append({
                "event_type": "fvg_fully_filled", "reference_type": "fvg", "reference_id": gap_id,
                "event_time": bar[0], "confirmation_time": bar[0],
                "metadata": {"params": params},
            })
            emitted["fully_filled"] = True
        if not emitted["invalidated"] and invalidates(bar):
            events.append({
                "event_type": "fvg_invalidated", "reference_type": "fvg", "reference_id": gap_id,
                "event_time": bar[0], "confirmation_time": bar[0],
                "metadata": {"params": params},
            })
            emitted["invalidated"] = True

        if (not expired and not emitted["partially_entered"]
                and bars_since_creation >= params["expiry_bars"]):
            events.append({
                "event_type": "fvg_expired", "reference_type": "fvg", "reference_id": gap_id,
                "event_time": bar[0], "confirmation_time": bar[0],
                "metadata": {"params": params},
            })
            expired = True

    return events


# ─────────────────────────────────────────────────────────────────────────
# I/O orchestration
# ─────────────────────────────────────────────────────────────────────────

def _resample_for_timeframe(daily_ohlc, timeframe):
    if timeframe == "daily":
        return daily_ohlc
    if timeframe == "weekly":
        return resample_weekly(daily_ohlc)
    if timeframe == "monthly":
        return resample_monthly(daily_ohlc)
    raise ValueError(f"unknown timeframe: {timeframe}")


def store_fair_value_gaps(conn, symbol, timeframe, gaps, calculation_version=FVG_CALCULATION_VERSION):
    """Upserts detected gaps into fair_value_gaps (ON CONFLICT DO NOTHING
    on the natural key -- a gap's identity never changes once created, so
    a re-detected duplicate is a pure no-op) and returns
    {(gap_type, event_time): id} for every gap now present, whether just
    inserted or already existing, so the caller can immediately compute
    lifecycle events against real ids without a second query round-trip
    per gap."""
    ids = {}
    with conn.cursor() as cur:
        for gap in gaps:
            cur.execute("""
                INSERT INTO fair_value_gaps
                    (symbol, timeframe, gap_type, zone_upper, zone_lower, event_time, confirmation_time, calculation_version)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, timeframe, gap_type, event_time) DO NOTHING
                RETURNING id
            """, (symbol, timeframe, gap["gap_type"], gap["zone_upper"], gap["zone_lower"],
                  gap["event_time"], gap["confirmation_time"], calculation_version))
            row = cur.fetchone()
            if row is None:
                cur.execute("""
                    SELECT id FROM fair_value_gaps
                    WHERE symbol=%s AND timeframe=%s AND gap_type=%s AND event_time=%s
                """, (symbol, timeframe, gap["gap_type"], gap["event_time"]))
                row = cur.fetchone()
            ids[(gap["gap_type"], gap["event_time"])] = row[0]
    conn.commit()
    return ids


def compute_and_store_fair_value_gaps(conn, symbol, timeframe, as_of=None,
                                       calculation_version=FVG_CALCULATION_VERSION):
    """I/O orchestrator for one (symbol, timeframe): loads price history
    as-of-sliced through as_of, detects gaps, persists gap identities,
    then walks lifecycle events for every persisted gap and persists
    those via structural_events' own store function."""
    target_date = as_of or date_cls.today()
    try:
        daily_all = load_daily_ohlc(conn, symbol)
    except Exception:
        daily_all = []
    dates = [b[0] for b in daily_all]
    idx = asof_index(dates, target_date)
    daily_ohlc = daily_all[:idx + 1] if idx is not None else []
    ohlc = _resample_for_timeframe(daily_ohlc, timeframe)
    if len(ohlc) < 3:
        return

    gaps = detect_fair_value_gaps(ohlc)
    if not gaps:
        return
    ids = store_fair_value_gaps(conn, symbol, timeframe, gaps, calculation_version)

    lifecycle_events = []
    for gap in gaps:
        gap_id = ids[(gap["gap_type"], gap["event_time"])]
        lifecycle_events.extend(detect_fvg_lifecycle_events(ohlc, gap, gap_id, calculation_version))
    if lifecycle_events:
        store_structural_events(conn, symbol, timeframe, lifecycle_events)


def update_fair_value_gaps(conn, symbols, timeframes=("daily", "weekly", "monthly")):
    """Compute + persist today's fair value gaps (and their lifecycle
    events) for every symbol and timeframe. Each (symbol, timeframe) pair
    is isolated in its own try/except, same discipline as
    market_structure.update_market_structure and
    structural_events.update_structural_events. Expected to run after
    those two in the same ingest cycle."""
    today = date_cls.today()
    for symbol in symbols:
        for timeframe in timeframes:
            try:
                compute_and_store_fair_value_gaps(conn, symbol, timeframe, as_of=today)
            except Exception as e:
                log.warning(f"Fair value gap update failed for {symbol}/{timeframe}: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass

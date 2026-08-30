"""
Volume Profile Calculation Engine, PR C of the Volume & Volume Profile
epic. Price-bucketed volume-at-price: POC (Point of Control), VAH/VAL
(Value Area High/Low), built on shared/backtest_engine.Bar (reused
directly, not a new bar type).

Explicit data-source decision: this engine is built on price_history_hourly,
not individual trades. Research confirmed no minute-bar or trade-level
data exists anywhere in this repo (docs/thesis-horizons-and-intraday-data.md
documents minute-level as "Reserved value only -- nothing built"), and
price_history_hourly's real depth is only ~6.5 weeks as of this PR (hourly
ingest added 2026-07-15) unless the dormant, never-confirmed-run
ingest/backfill_intraday_alpaca.py is run separately -- that's a later
operational decision, not part of this PR. Hourly bars are a meaningful
improvement over approximating from daily-candle volume smeared across a
full day's high-low range (the approximation the epic's spec explicitly
discourages when anything finer is available) while still honestly being
an approximation relative to true trade-level Volume Profile -- documented
here and on every VolumeProfile result's own metadata, never asserted as
ground truth.

Each bar's volume is assigned entirely to ONE price bucket, at that bar's
typical price = (high + low + close) / 3 -- assign-to-single-point per
bar, not distributed across the bar's own range. A documented, deliberate
simplification, not a claim of precision beyond what hourly bars can
support.

Split into a pure calculation function (no DB access) and a thin
DB-loading wrapper, mirroring shared/backtest_engine.py's exact
run_backtest(bars, ...) / load_bars(conn, ...) split.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from backtest_engine import Bar

log = logging.getLogger(__name__)

DEFAULT_BUCKET_COUNT = 50
DEFAULT_VALUE_AREA_PCT = 0.70


@dataclass(frozen=True)
class ProfileBucket:
    price_low: float
    price_high: float
    volume: float


@dataclass(frozen=True)
class VolumeProfile:
    symbol: str
    buckets: list                 # list[ProfileBucket], ascending price order
    poc: float | None             # midpoint price of the max-volume bucket; None if no volume
    vah: float | None
    val: float | None
    total_volume: float
    bucket_count: int
    value_area_pct: float
    source_table: str
    feed: str                     # "alpaca_iex" | "yahoo" | "unknown" | "mixed"
    start: datetime | None
    end: datetime | None
    bar_count: int

    def to_json(self):
        """Explicit dict, not dataclasses.asdict() -- matches
        shared/backtest_engine.py::BacktestResult.to_json()'s style."""
        return {
            "symbol": self.symbol,
            "buckets": [
                {"price_low": b.price_low, "price_high": b.price_high, "volume": b.volume}
                for b in self.buckets
            ],
            "poc": self.poc,
            "vah": self.vah,
            "val": self.val,
            "total_volume": self.total_volume,
            "bucket_count": self.bucket_count,
            "value_area_pct": self.value_area_pct,
            "source_table": self.source_table,
            "feed": self.feed,
            "start": self.start.isoformat() if self.start is not None else None,
            "end": self.end.isoformat() if self.end is not None else None,
            "bar_count": self.bar_count,
        }

    @classmethod
    def from_json(cls, data):
        buckets = [
            ProfileBucket(price_low=b["price_low"], price_high=b["price_high"], volume=b["volume"])
            for b in data["buckets"]
        ]
        return cls(
            symbol=data["symbol"], buckets=buckets, poc=data["poc"], vah=data["vah"], val=data["val"],
            total_volume=data["total_volume"], bucket_count=data["bucket_count"],
            value_area_pct=data["value_area_pct"], source_table=data["source_table"], feed=data["feed"],
            start=datetime.fromisoformat(data["start"]) if data["start"] is not None else None,
            end=datetime.fromisoformat(data["end"]) if data["end"] is not None else None,
            bar_count=data["bar_count"],
        )


def _typical_price(bar):
    return (bar.high + bar.low + bar.close) / 3


def _resolve_feed(feeds):
    distinct = set(feeds)
    if len(distinct) == 0:
        return "unknown"
    if len(distinct) == 1:
        return next(iter(distinct))
    log.warning(f"volume_profile: mixed feeds in one profile window: {sorted(distinct)}")
    return "mixed"


def compute_volume_profile(bars, feeds, *, bucket_count=DEFAULT_BUCKET_COUNT, value_area_pct=DEFAULT_VALUE_AREA_PCT):
    """Pure function, no DB access. `bars`: list[backtest_engine.Bar].
    `feeds`: parallel list[str], each bar's source -- see module docstring
    for why this is checked rather than silently assumed consistent."""
    symbol = bars[0].symbol if bars else ""
    feed = _resolve_feed(feeds)
    start = bars[0].ts if bars else None
    end = bars[-1].ts if bars else None

    if not bars:
        return VolumeProfile(
            symbol=symbol, buckets=[], poc=None, vah=None, val=None, total_volume=0.0,
            bucket_count=bucket_count, value_area_pct=value_area_pct,
            source_table="price_history_hourly", feed=feed, start=start, end=end, bar_count=0,
        )

    price_low = min(b.low for b in bars)
    price_high = max(b.high for b in bars)
    total_volume = sum(b.volume for b in bars)

    if price_high <= price_low or total_volume == 0:
        # Degenerate range (all bars identical price, or zero volume) --
        # a single bucket spanning whatever range exists, no POC/VA.
        buckets = [ProfileBucket(price_low=price_low, price_high=price_high, volume=total_volume)]
        return VolumeProfile(
            symbol=symbol, buckets=buckets, poc=None, vah=None, val=None, total_volume=total_volume,
            bucket_count=1, value_area_pct=value_area_pct,
            source_table="price_history_hourly", feed=feed, start=start, end=end, bar_count=len(bars),
        )

    bucket_width = (price_high - price_low) / bucket_count
    bucket_volumes = [0.0] * bucket_count
    for bar in bars:
        tp = _typical_price(bar)
        idx = int((tp - price_low) / bucket_width)
        idx = min(idx, bucket_count - 1)  # tp == price_high lands in the last bucket, not out of range
        idx = max(idx, 0)
        bucket_volumes[idx] += bar.volume

    buckets = [
        ProfileBucket(price_low=price_low + i * bucket_width, price_high=price_low + (i + 1) * bucket_width, volume=v)
        for i, v in enumerate(bucket_volumes)
    ]

    poc_idx = max(range(bucket_count), key=lambda i: bucket_volumes[i])
    poc = (buckets[poc_idx].price_low + buckets[poc_idx].price_high) / 2

    # Value area: expand outward from POC toward whichever neighboring
    # bucket has more volume, until cumulative volume >= value_area_pct of
    # total. Standard algorithm.
    lo_idx = hi_idx = poc_idx
    accumulated = bucket_volumes[poc_idx]
    target = value_area_pct * total_volume
    while accumulated < target and (lo_idx > 0 or hi_idx < bucket_count - 1):
        expand_lo = bucket_volumes[lo_idx - 1] if lo_idx > 0 else -1
        expand_hi = bucket_volumes[hi_idx + 1] if hi_idx < bucket_count - 1 else -1
        if expand_hi >= expand_lo:
            hi_idx += 1
            accumulated += bucket_volumes[hi_idx]
        else:
            lo_idx -= 1
            accumulated += bucket_volumes[lo_idx]

    val = buckets[lo_idx].price_low
    vah = buckets[hi_idx].price_high

    return VolumeProfile(
        symbol=symbol, buckets=buckets, poc=poc, vah=vah, val=val, total_volume=total_volume,
        bucket_count=bucket_count, value_area_pct=value_area_pct,
        source_table="price_history_hourly", feed=feed, start=start, end=end, bar_count=len(bars),
    )


def load_volume_profile(conn, symbol, start, end, *, bucket_count=DEFAULT_BUCKET_COUNT, value_area_pct=DEFAULT_VALUE_AREA_PCT):
    """Loads price_history_hourly rows for symbol/date-range, ordered by
    ts, and computes a VolumeProfile. Returns None if no bars exist for
    the window -- fail-open on absence, matching this repo's established
    convention (e.g. hypothesis_library.get_hypothesis_type())."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, ts, open, high, low, close, volume, source
            FROM price_history_hourly
            WHERE symbol=%s AND ts >= %s AND ts <= %s
            ORDER BY ts ASC
        """, (symbol, start, end))
        rows = cur.fetchall()

    if not rows:
        return None

    bars = [
        Bar(symbol=r[0], ts=r[1], open=float(r[2]), high=float(r[3]), low=float(r[4]), close=float(r[5]),
            volume=float(r[6]) if r[6] is not None else 0.0)
        for r in rows
    ]
    feeds = [r[7] for r in rows]
    return compute_volume_profile(bars, feeds, bucket_count=bucket_count, value_area_pct=value_area_pct)

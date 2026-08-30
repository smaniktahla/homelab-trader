"""
PR C, Volume & Volume Profile epic. Deterministic tests for the profile
bucketing/POC/value-area math (mostly pure, no DB) plus one DB-backed test
for load_volume_profile(), mirroring test_backtest_engine.py's style.
"""

from datetime import datetime, timedelta, timezone

from backtest_engine import Bar
from volume_profile import VolumeProfile, compute_volume_profile, load_volume_profile

SYMBOL = "VPTEST"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bar(o, h, l, c, v, ts=START):
    return Bar(symbol=SYMBOL, ts=ts, open=o, high=h, low=l, close=c, volume=v)


# Fixture verified by direct execution before writing assertions (see
# session notes) -- price range [98, 108], 4 equal-width buckets of 2.5
# each, typical prices [100, 102, 104, 106] landing one bar per bucket.
_FIXTURE_BARS = [
    _bar(100, 102, 98, 100, 1000),
    _bar(100, 104, 100, 102, 2000),
    _bar(102, 106, 102, 104, 500),
    _bar(104, 108, 104, 106, 300),
]
_FIXTURE_FEEDS = ["alpaca_iex"] * 4


# --- bucketing correctness ------------------------------------------------------

def test_known_fixture_bucket_volumes_and_poc():
    result = compute_volume_profile(_FIXTURE_BARS, _FIXTURE_FEEDS, bucket_count=4, value_area_pct=0.70)
    assert [b.volume for b in result.buckets] == [1000.0, 2000.0, 500.0, 300.0]
    assert result.poc == 101.75


def test_known_fixture_value_area():
    result = compute_volume_profile(_FIXTURE_BARS, _FIXTURE_FEEDS, bucket_count=4, value_area_pct=0.70)
    assert result.val == 98.0
    assert result.vah == 103.0


def test_total_volume_conservation():
    result = compute_volume_profile(_FIXTURE_BARS, _FIXTURE_FEEDS, bucket_count=4)
    assert result.total_volume == sum(b.volume for b in _FIXTURE_BARS)
    assert sum(b.volume for b in result.buckets) == result.total_volume


def test_buckets_are_uniform_width_and_tile_the_range():
    result = compute_volume_profile(_FIXTURE_BARS, _FIXTURE_FEEDS, bucket_count=4)
    widths = [round(b.price_high - b.price_low, 9) for b in result.buckets]
    assert len(set(widths)) == 1  # all equal
    # No gaps/overlaps: each bucket's price_high equals the next one's price_low.
    for i in range(len(result.buckets) - 1):
        assert result.buckets[i].price_high == result.buckets[i + 1].price_low
    assert result.buckets[0].price_low == 98.0
    assert result.buckets[-1].price_high == 108.0


def test_bucket_count_is_honored_exactly():
    result = compute_volume_profile(_FIXTURE_BARS, _FIXTURE_FEEDS, bucket_count=10)
    assert len(result.buckets) == 10
    assert result.bucket_count == 10


# --- feed provenance -----------------------------------------------------------

def test_feed_consistent_when_all_bars_share_one_source():
    result = compute_volume_profile(_FIXTURE_BARS, _FIXTURE_FEEDS)
    assert result.feed == "alpaca_iex"


def test_feed_mixed_when_sources_differ():
    mixed_feeds = ["alpaca_iex", "alpaca_iex", "yahoo", "alpaca_iex"]
    result = compute_volume_profile(_FIXTURE_BARS, mixed_feeds)
    assert result.feed == "mixed"


# --- edge cases ------------------------------------------------------------------

def test_empty_bars_returns_none_metrics_not_a_crash():
    result = compute_volume_profile([], [])
    assert result.buckets == []
    assert result.poc is None
    assert result.vah is None
    assert result.val is None
    assert result.total_volume == 0.0
    assert result.feed == "unknown"


def test_zero_volume_bars_returns_none_poc():
    bars = [_bar(100, 101, 99, 100, 0), _bar(100, 101, 99, 100, 0)]
    result = compute_volume_profile(bars, ["alpaca_iex", "alpaca_iex"])
    assert result.poc is None
    assert result.total_volume == 0.0


def test_degenerate_identical_price_range():
    # All bars at exactly the same price -- price_high == price_low.
    bars = [_bar(100, 100, 100, 100, 500), _bar(100, 100, 100, 100, 300)]
    result = compute_volume_profile(bars, ["alpaca_iex", "alpaca_iex"])
    assert result.total_volume == 800.0
    assert len(result.buckets) == 1


# --- serialization ---------------------------------------------------------------

def test_to_json_from_json_round_trip():
    result = compute_volume_profile(_FIXTURE_BARS, _FIXTURE_FEEDS, bucket_count=4)
    restored = VolumeProfile.from_json(result.to_json())
    assert restored == result


# --- load_volume_profile (DB-backed) ----------------------------------------------

def test_load_volume_profile_matches_direct_compute(conn):
    now = datetime.now(timezone.utc)
    rows = [
        (now - timedelta(hours=3), 100, 102, 98, 100, 1000),
        (now - timedelta(hours=2), 100, 104, 100, 102, 2000),
        (now - timedelta(hours=1), 102, 106, 102, 104, 500),
    ]
    with conn.cursor() as cur:
        for ts, o, h, l, c, v in rows:
            cur.execute(
                "INSERT INTO price_history_hourly (symbol, ts, open, high, low, close, volume, source) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'alpaca_iex') ON CONFLICT (symbol, ts) DO NOTHING",
                (SYMBOL, ts, o, h, l, c, v),
            )
    conn.commit()

    result = load_volume_profile(conn, SYMBOL, now - timedelta(hours=4), now, bucket_count=3)
    assert result is not None
    assert result.bar_count == 3
    assert result.feed == "alpaca_iex"
    assert result.total_volume == 3500.0

    direct_bars = [
        Bar(symbol=SYMBOL, ts=ts, open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v))
        for ts, o, h, l, c, v in rows
    ]
    expected = compute_volume_profile(direct_bars, ["alpaca_iex"] * 3, bucket_count=3)
    assert result.poc == expected.poc
    assert result.vah == expected.vah
    assert result.val == expected.val


def test_load_volume_profile_returns_none_for_empty_window(conn):
    now = datetime.now(timezone.utc)
    result = load_volume_profile(conn, "NODATA_VP", now - timedelta(hours=1), now)
    assert result is None

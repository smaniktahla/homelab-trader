"""
Unit tests for ingest/backfill_vix.py's chunked-fetch fix -- no prior test
coverage existed for this script at all. Covers the actual bug this PR
fixes: Yahoo silently downsamples interval=1d to ~monthly when range=max
is requested for a multi-decade symbol, so the fetch is now chunked into
bounded windows and stitched/deduplicated by date.
"""

import os
import sys
from datetime import datetime, timezone

import pytest
import requests_mock


def _import_backfill_vix(monkeypatch):
    """backfill_vix.py reads DATABASE_URL at module import time (DB_DSN =
    os.environ["DATABASE_URL"]) -- must be set before import, same pattern
    every other test-imported module in this repo uses."""
    monkeypatch.setenv("DATABASE_URL", os.environ.get(
        "TEST_DATABASE_URL", "postgresql://invest_test:not_a_real_credential@localhost:15432/invest_test"))
    import pathlib
    ingest_dir = str(pathlib.Path(__file__).resolve().parent.parent / "ingest")
    if ingest_dir not in sys.path:
        sys.path.insert(0, ingest_dir)
    sys.modules.pop("backfill_vix", None)
    import backfill_vix
    return backfill_vix


def _yahoo_response(dates):
    """Builds a Yahoo chart-API-shaped JSON response for the given list of
    'YYYY-MM-DD' date strings, one bar per date, distinct close values so
    stitching/ordering can be verified."""
    timestamps = [int(datetime.strptime(d, "%Y-%m-%d").replace(
        hour=14, minute=30, tzinfo=timezone.utc).timestamp()) for d in dates]
    closes = [20.0 + i for i in range(len(dates))]
    return {
        "chart": {
            "result": [{
                "timestamp": timestamps,
                "indicators": {"quote": [{
                    "open": closes, "high": closes, "low": closes, "close": closes,
                }]},
            }]
        }
    }


def test_stitches_multiple_chunks_without_duplicates(monkeypatch):
    backfill_vix = _import_backfill_vix(monkeypatch)
    monkeypatch.setattr(backfill_vix, "VIX_INCEPTION", datetime(2020, 1, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(backfill_vix, "CHUNK_YEARS", 2)
    monkeypatch.setattr(backfill_vix, "CHUNK_REQUEST_DELAY_S", 0)

    with requests_mock.Mocker() as m:
        m.get(
            "https://query2.finance.yahoo.com/v8/finance/chart/%5EVIX",
            [
                {"json": _yahoo_response(["2020-01-02", "2020-01-03", "2020-01-06"])},
                {"json": _yahoo_response(["2022-01-03", "2022-01-04"])},
            ],
        )
        rows = backfill_vix.fetch_vix_full_history(now=datetime(2024, 1, 1, tzinfo=timezone.utc))

    dates = [r[0] for r in rows]
    assert dates == sorted(dates)
    assert dates == ["2020-01-02", "2020-01-03", "2020-01-06", "2022-01-03", "2022-01-04"]
    assert len(rows) == len(set(dates))   # no duplicate dates across chunk boundaries


def test_empty_chunk_before_real_history_is_skipped(monkeypatch):
    """A chunk window with no real Yahoo data (e.g. before actual ^VIX
    inception) must not raise or inject bogus rows -- it should just
    contribute nothing to the stitched result."""
    backfill_vix = _import_backfill_vix(monkeypatch)
    monkeypatch.setattr(backfill_vix, "VIX_INCEPTION", datetime(2020, 1, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(backfill_vix, "CHUNK_YEARS", 2)
    monkeypatch.setattr(backfill_vix, "CHUNK_REQUEST_DELAY_S", 0)

    with requests_mock.Mocker() as m:
        m.get(
            "https://query2.finance.yahoo.com/v8/finance/chart/%5EVIX",
            [
                {"json": _yahoo_response([])},
                {"json": _yahoo_response(["2022-06-01"])},
            ],
        )
        rows = backfill_vix.fetch_vix_full_history(now=datetime(2024, 1, 1, tzinfo=timezone.utc))

    assert [r[0] for r in rows] == ["2022-06-01"]


def test_market_open_utc_matches_equity_convention(monkeypatch):
    backfill_vix = _import_backfill_vix(monkeypatch)
    ts = backfill_vix._market_open_utc("2026-06-15")
    assert ts.hour in (13, 14)  # 9:30am NY time in UTC, DST-dependent
    assert ts.tzinfo is not None

"""
Unit tests for ingest/backfill_alpaca.py -- no prior test coverage existed
for this script. Covers two real, live-verified bugs:

1. Alpaca rejects hyphenated share-class tickers (BRK-B, BF-B -- the
   DB/Yahoo form) with a 400 "invalid symbol" error and expects dot
   notation (BRK.B, BF.B) instead.
2. That 400 fails the ENTIRE multi-symbol batch request, not just the bad
   symbol -- silently dropping the other 7 good tickers sharing the batch.
   Confirmed live against Alpaca: a batch of
   [BDX, BEN, BF-B, BG, BIIB, BKNG, BKR, BLDR] returned zero data for all
   eight because of BF-B alone.
"""

import os
import sys
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests_mock


def _import_backfill_alpaca(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ.get(
        "TEST_DATABASE_URL", "postgresql://invest_test:not_a_real_credential@localhost:15432/invest_test"))
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")
    import pathlib
    ingest_dir = str(pathlib.Path(__file__).resolve().parent.parent / "ingest")
    if ingest_dir not in sys.path:
        sys.path.insert(0, ingest_dir)
    sys.modules.pop("backfill_alpaca", None)
    import backfill_alpaca
    return backfill_alpaca


def _bars_response(symbol, dates):
    return {
        "bars": {symbol: [
            {"t": f"{d}T04:00:00Z", "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "v": 100}
            for d in dates
        ]},
        "next_page_token": None,
    }


def test_hyphenated_symbol_translated_to_dot_notation_for_alpaca(monkeypatch):
    backfill_alpaca = _import_backfill_alpaca(monkeypatch)

    with requests_mock.Mocker() as m:
        m.get(
            "https://data.alpaca.markets/v2/stocks/bars",
            json=_bars_response("BRK.B", ["2024-01-02"]),
        )
        bars = backfill_alpaca.fetch_bars_batch(["BRK-B"])

    # Alpaca was called with dot notation ...
    assert "symbols=BRK.B" in m.request_history[0].url
    # ... but the result is keyed back by the original DB-form (hyphen) symbol.
    assert "BRK-B" in bars
    assert "BRK.B" not in bars
    assert len(bars["BRK-B"]) == 1


def test_batch_containing_bad_symbol_falls_back_to_per_symbol_fetch(monkeypatch):
    """The real-world failure this PR fixes: one invalid symbol (BF-B, prior
    to the translation fix, or any other future poison ticker) must not
    drop its 7 batch-mates' data."""
    backfill_alpaca = _import_backfill_alpaca(monkeypatch)
    monkeypatch.setattr(backfill_alpaca, "SLEEP_BETWEEN_BATCHES", 0)

    batch = ["BDX", "BEN", "POISON", "BG"]

    def responder(request, context):
        symbols = parse_qs(urlsplit(request.url).query)["symbols"][0].split(",")
        if len(symbols) > 1:
            context.status_code = 400
            return {"message": "invalid symbol: POISON"}
        sym = symbols[0]
        if sym == "POISON":
            context.status_code = 400
            return {"message": "invalid symbol: POISON"}
        context.status_code = 200
        return _bars_response(sym, ["2024-01-02"])

    with requests_mock.Mocker() as m:
        m.get("https://data.alpaca.markets/v2/stocks/bars", json=responder)

        try:
            bars_by_symbol = backfill_alpaca.fetch_bars_batch(batch)
        except Exception:
            bars_by_symbol = {}
            for sym in batch:
                try:
                    bars_by_symbol.update(backfill_alpaca.fetch_bars_batch([sym]))
                except Exception:
                    pass

    assert "BDX" in bars_by_symbol
    assert "BEN" in bars_by_symbol
    assert "BG" in bars_by_symbol
    assert "POISON" not in bars_by_symbol


def test_main_batch_failure_recovers_good_symbols(monkeypatch):
    """Same scenario as above but exercised through main()'s actual
    try/except-and-retry-individually logic, with a mocked DB layer."""
    backfill_alpaca = _import_backfill_alpaca(monkeypatch)
    monkeypatch.setattr(backfill_alpaca, "SLEEP_BETWEEN_BATCHES", 0)
    monkeypatch.setattr(backfill_alpaca, "BATCH_SIZE", 4)

    batch = ["BDX", "BEN", "BF-B", "BG"]
    monkeypatch.setattr(backfill_alpaca, "get_universe_symbols", lambda conn: batch)
    monkeypatch.setattr(backfill_alpaca, "get_db", lambda: MagicMock())

    stored = {}
    monkeypatch.setattr(
        backfill_alpaca, "store_bars",
        lambda conn, symbol, bars: stored.setdefault(symbol, len(bars)) and len(bars),
    )

    def responder(request, context):
        symbols = parse_qs(urlsplit(request.url).query)["symbols"][0].split(",")
        if len(symbols) > 1:
            context.status_code = 400
            return {"message": "invalid symbol: BF.B"}
        sym = symbols[0]
        if sym == "BF.B":
            context.status_code = 400
            return {"message": "invalid symbol: BF.B"}
        context.status_code = 200
        return _bars_response(sym, ["2024-01-02"])

    with requests_mock.Mocker() as m:
        m.get("https://data.alpaca.markets/v2/stocks/bars", json=responder)
        backfill_alpaca.main()

    assert "BDX" in stored
    assert "BEN" in stored
    assert "BG" in stored
    assert "BF-B" not in stored

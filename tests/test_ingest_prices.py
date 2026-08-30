"""
PR A, Volume & Volume Profile epic. Confirms ingest.py::ingest_prices()
tags every inserted price_history row with source='yahoo' -- mocks
fetch_prices_yf() directly rather than hitting the live Yahoo endpoint,
same style as test_backfill_alpaca.py's approach for the Alpaca side.
"""

import os
import sys
from datetime import datetime, timezone


def _import_ingest(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ.get(
        "TEST_DATABASE_URL", "postgresql://invest_test:not_a_real_credential@localhost:15432/invest_test"))
    import pathlib
    ingest_dir = str(pathlib.Path(__file__).resolve().parent.parent / "ingest")
    if ingest_dir not in sys.path:
        sys.path.insert(0, ingest_dir)
    sys.modules.pop("ingest", None)
    import ingest
    return ingest


def test_ingest_prices_tags_source_as_yahoo(monkeypatch, conn):
    ing = _import_ingest(monkeypatch)
    fake_rows = [
        {
            "ts": datetime(2026, 1, 2, tzinfo=timezone.utc),
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
            "volume": 1000, "adjclose": 10.5,
        },
    ]
    monkeypatch.setattr(ing, "fetch_prices_yf", lambda symbol, yf_range="5d": fake_rows)

    ing.ingest_prices(conn, ["YFTEST"])

    with conn.cursor() as cur:
        cur.execute("SELECT source, volume FROM price_history WHERE symbol=%s", ("YFTEST",))
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "yahoo"
    assert row[1] == 1000


def test_bare_insert_without_source_defaults_to_unknown(conn):
    # Confirms the schema-level DEFAULT itself (not any ingestion code
    # path) for a row inserted the way pre-migration code always did --
    # omitting the source column entirely.
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
            VALUES ('LEGACYTEST', %s, 10.0, 11.0, 9.0, 10.5, 1000)
        """, (datetime(2026, 1, 2, tzinfo=timezone.utc),))
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT source FROM price_history WHERE symbol='LEGACYTEST'")
        row = cur.fetchone()
    assert row[0] == "unknown"

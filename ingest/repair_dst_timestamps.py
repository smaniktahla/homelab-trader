#!/usr/bin/env python3
"""One-time repair for the initial Alpaca backfill run, which hardcoded every
row's ts to 13:30:00Z instead of the correct DST-aware NYSE market-open time
(14:30:00Z for EST-season dates, Nov-Mar). For each row currently stamped
exactly 13:30:00Z, recompute what its timestamp should actually be:
  - if that's already 13:30:00Z (an EDT-season date), leave it alone
  - if a correctly-stamped row already exists at the right time (a genuine
    Yahoo-sourced row for that day), delete the wrong duplicate
  - otherwise, correct the wrong row's ts in place (no data loss — the
    OHLCV values are still valid, only the stamp was wrong)

Safe to re-run (idempotent — a second pass finds nothing left to fix).
Run manually: docker exec invest-ingest python3 repair_dst_timestamps.py
"""

import os
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_DSN = os.environ["DATABASE_URL"]
NY_TZ = ZoneInfo("America/New_York")


def correct_ts_for_date(d):
    return datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY_TZ).astimezone(timezone.utc)


def main():
    conn = psycopg2.connect(DB_DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT id, symbol, ts FROM price_history WHERE ts::time = '13:30:00'")
        rows = cur.fetchall()
    log.info(f"Checking {len(rows)} rows stamped 13:30:00Z")

    checked = fixed = deleted = 0
    with conn.cursor() as cur:
        for row_id, symbol, ts in rows:
            checked += 1
            correct = correct_ts_for_date(ts.date())
            if correct == ts:
                continue  # already correct (genuinely an EDT-season date)

            cur.execute("SELECT id FROM price_history WHERE symbol=%s AND ts=%s", (symbol, correct))
            existing = cur.fetchone()
            if existing:
                cur.execute("DELETE FROM price_history WHERE id=%s", (row_id,))
                deleted += 1
            else:
                cur.execute("UPDATE price_history SET ts=%s WHERE id=%s", (correct, row_id))
                fixed += 1

            if checked % 10000 == 0:
                conn.commit()
                log.info(f"progress: checked={checked} fixed={fixed} deleted={deleted}")

    conn.commit()
    log.info(f"Done. checked={checked} fixed={fixed} deleted={deleted}")
    conn.close()


if __name__ == "__main__":
    main()

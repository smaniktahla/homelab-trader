#!/usr/bin/env python3
"""Regenerates filing_signature_dates.json — reproducibility tool for the
Congressional Shadow backtest (backtest_congressional_shadow.py), not part
of the recurring ingest loop and not something the live system depends on.

Why this exists: the free house-stock-watcher-data mirror this backtest
uses for transaction data (github.com/TattooedHead/house-stock-watcher-data)
has a `disclosure_date` field that turned out NOT to be the real PTR filing
date. Spot-checking two PDFs (filing_id 20022430 and 20034262 for Kevin
Hern) against the actual House Clerk documents showed `disclosure_date`
tracks the form's internal per-transaction "Notification Date" field
(close to real-time, ~1 day after each transaction) — not the "Digitally
Signed" certification date on the last page, which is when the filing
actually became public. Using the wrong field would understate real lag by
weeks and manufacture a fake edge in the backtest. This script pulls the
real signature date directly from each unique filing's PDF instead.

Requires `pdftotext` (poppler-utils) on PATH — not installed in the
invest-ingest container image (python:3.12-slim), so this runs wherever
poppler-utils is available and its output (filing_signature_dates.json) is
committed as a small, frozen data artifact alongside
ptr_transactions_snapshot.json. Signature dates on filed PTRs don't change,
so this is safe to treat as durable ground truth rather than re-scraping
on every backtest run — re-run this script only if the transaction
snapshot is refreshed with new filings.

Run manually (needs poppler-utils: apt install poppler-utils / brew install poppler):
    python3 scrape_ptr_filing_dates.py
"""

import json
import logging
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

HERE = Path(__file__).parent
TRANSACTIONS_SNAPSHOT = HERE / "ptr_transactions_snapshot.json"
OUTPUT = HERE / "filing_signature_dates.json"
SIG_RE = re.compile(r"Digitally Signed:.*?,\s*(\d{2}/\d{2}/\d{4})")
SLEEP_BETWEEN_FETCHES = 0.3


def main():
    if subprocess.run(["which", "pdftotext"], capture_output=True).returncode != 0:
        log.error("pdftotext not found on PATH — install poppler-utils")
        sys.exit(1)

    rows = json.loads(TRANSACTIONS_SNAPSHOT.read_text())
    filings = {r["filing_id"]: r["source_url"] for r in rows}
    log.info(f"{len(filings)} unique filings to fetch")

    results = {}
    failed = []
    for i, (fid, url) in enumerate(filings.items(), 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                pdf_bytes = resp.read()
            text = subprocess.run(["pdftotext", "-layout", "-", "-"], input=pdf_bytes,
                                   capture_output=True, timeout=15).stdout.decode("utf-8", errors="ignore")
            m = SIG_RE.search(text)
            if m:
                results[fid] = m.group(1)
            else:
                failed.append((fid, "no signature date match", url))
        except Exception as e:
            failed.append((fid, str(e), url))
        if i % 10 == 0:
            log.info(f"...{i}/{len(filings)}")
        time.sleep(SLEEP_BETWEEN_FETCHES)

    log.info(f"Resolved {len(results)}/{len(filings)}, failed {len(failed)}")
    for fid, err, url in failed:
        log.warning(f"  FAILED {fid}: {err} ({url})")

    OUTPUT.write_text(json.dumps(results, indent=2))
    log.info(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()

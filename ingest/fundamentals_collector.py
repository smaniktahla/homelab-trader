"""
SEC EDGAR fundamentals collector: PR #2 of the multi-source signal
integration (see docs/fundamentals-collector.md and
docs/signal-component-architecture.md). Collector only -- does no
scoring; see shared/fundamentals.py for the point-in-time scoring
function shared/signals.py calls directly.

No-op if SEC_USER_AGENT isn't set, same "quietly skip rather than crash
the ingest cycle" pattern as earnings.py's FINNHUB_KEY guard. SEC EDGAR
requires an identifying User-Agent on every request
(https://www.sec.gov/os/webmaster-faq#developers) -- there is no way to
call these endpoints anonymously, so this collector is inert without it
rather than sending a request SEC would reject anyway.

Named fundamentals_collector.py, not fundamentals.py, specifically to
avoid colliding with shared/fundamentals.py: ingest/Dockerfile does
`COPY shared/ .` then `COPY ingest/ .` into the same flat /app directory
in the ingest container -- two files with the identical basename would
have the second COPY silently overwrite the first on disk, breaking
shared/signals.py's `from fundamentals import compute_fundamental_score`
at container startup. Caught before it ever reached a build, not after.
"""

import logging
import os
import time

import requests

log = logging.getLogger(__name__)

SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "")
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYCONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json"
REQUEST_DELAY = 0.15  # SEC asks for <=10 req/sec; comfortably under that even with several tags per symbol

# metric name (stored) -> ordered list of us-gaap XBRL tags to try. Some
# companies report revenue under the newer ASC 606 tag instead of the
# classic one -- both are tried, first one with any data wins, and either
# way the fact is stored under the canonical metric name below so
# shared/fundamentals.py's queries never need to know about tag aliasing.
METRIC_TAGS = {
    "Revenues": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    "GrossProfit": ("GrossProfit",),
    "NetIncomeLoss": ("NetIncomeLoss",),
}


def _headers():
    return {"User-Agent": SEC_USER_AGENT}


def _fetch_ticker_cik_map():
    r = requests.get(TICKER_MAP_URL, headers=_headers(), timeout=15)
    r.raise_for_status()
    data = r.json()
    # data is {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    return {row["ticker"]: row["cik_str"] for row in data.values()}


def _fetch_concept(cik, tag):
    r = requests.get(COMPANYCONCEPT_URL.format(cik=cik, tag=tag), headers=_headers(), timeout=15)
    if r.status_code == 404:
        return None  # this company doesn't report this tag -- not an error, just absent
    r.raise_for_status()
    return r.json()


def _extract_observations(concept_json):
    """Yield one dict per USD observation for this concept. Skips
    non-USD units and any entry missing the fields point-in-time
    correctness depends on (accession number, period end, filed date)."""
    if not concept_json:
        return
    for unit_name, entries in concept_json.get("units", {}).items():
        if unit_name != "USD":
            continue
        for e in entries:
            accn, end, filed = e.get("accn"), e.get("end"), e.get("filed")
            if not accn or not end or not filed:
                continue
            fy, fp = e.get("fy"), e.get("fp")
            yield {
                "value": e.get("val"),
                "unit": unit_name,
                "fiscal_period": f"{fp}-{fy}" if fy and fp else None,
                "period_start": e.get("start"),
                "period_end": end,
                "filed_at": filed,
                # SEC's companyconcept API doesn't expose a separate
                # "accepted" timestamp distinct from "filed" at this
                # granularity. Using the filing's own 'filed' date as
                # accepted_at is a deliberate, documented simplification:
                # filed and SEC-accepted fall on the same calendar day for
                # the overwhelming majority of filings, and 'filed' is the
                # conservative choice (same-day-or-earlier than true
                # acceptance, never later) -- so it can only make
                # point-in-time filtering stricter than reality, never
                # introduce look-ahead bias. See docs/fundamentals-collector.md.
                "accepted_at": filed,
                "form_type": e.get("form"),
                "accession_number": accn,
            }


def sync_fundamentals(conn, symbols):
    """Fetch + upsert fundamental facts for each symbol. Best-effort per
    symbol (and per tag within a symbol) -- one symbol's fetch failing
    (delisted ticker, rate limit, malformed response) must never stop the
    rest from being collected, and must never propagate into the caller's
    ingest cycle."""
    if not SEC_USER_AGENT:
        log.info("Fundamentals sync skipped: SEC_USER_AGENT not set")
        return

    try:
        ticker_cik = _fetch_ticker_cik_map()
        time.sleep(REQUEST_DELAY)
    except Exception as e:
        log.warning(f"Fundamentals: could not fetch SEC ticker/CIK map: {e}")
        return

    for sym in symbols:
        cik = ticker_cik.get(sym)
        if cik is None:
            continue  # not in SEC's list -- e.g. an ETF; nothing to collect

        try:
            n = _sync_symbol(conn, sym, cik)
            log.info(f"Fundamentals for {sym}: {n} new fact(s)")
        except Exception as e:
            log.warning(f"Fundamentals sync failed for {sym}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass


def _sync_symbol(conn, symbol, cik):
    n = 0
    with conn.cursor() as cur:
        for metric, tags in METRIC_TAGS.items():
            observations = []
            for tag in tags:
                concept_json = _fetch_concept(cik, tag)
                time.sleep(REQUEST_DELAY)
                observations = list(_extract_observations(concept_json))
                if observations:
                    break  # this alias has data -- don't also try the next one
            for obs in observations:
                cur.execute("""
                    INSERT INTO fundamental_facts
                        (symbol, metric, value, unit, fiscal_period, period_start,
                         period_end, filed_at, accepted_at, form_type, accession_number, source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'sec_edgar')
                    ON CONFLICT (symbol, metric, fiscal_period, accession_number) DO NOTHING
                """, (
                    symbol, metric, obs["value"], obs["unit"], obs["fiscal_period"],
                    obs["period_start"], obs["period_end"], obs["filed_at"],
                    obs["accepted_at"], obs["form_type"], obs["accession_number"],
                ))
                n += cur.rowcount
    conn.commit()
    return n

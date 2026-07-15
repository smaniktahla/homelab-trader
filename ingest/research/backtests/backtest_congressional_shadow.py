#!/usr/bin/env python3
"""Experiment 004: Congressional Shadow strategy (Shreve/Hern) backtest.

Research artifact, not production logic — same posture as Experiments
002/003. Tests the core hypothesis from the design doc ("[homelab-trader]
Congressional Shadow Strategy (Shreve/Hern)", DocMost ID
06d2a203-8b9d-46f9-9ae8-1a881534638c): if you'd bought at the price on the
date each filing actually became public (not the transaction date, and
NOT naively "soon after" — the real lag, which is often close to the
45-day STOCK Act ceiling), would you have beaten SPY over the following
5/20/60 trading days? This is the "Must-Have Before Live" gate the design
doc requires before `theses` row `congress_shreve_hern` can leave
`backtesting_only`.

Data pipeline and why it looks the way it does:

1. Transactions: ptr_transactions_snapshot.json, a frozen snapshot (683
   rows, both members, all types) pulled from the free
   github.com/TattooedHead/house-stock-watcher-data mirror on 2026-07-14.
   Frozen rather than live-refetched — this mirror isn't authoritative
   (the original House Stock Watcher S3 bucket it's meant to replace
   returns 403 as of this writing), and freezing it makes this backtest's
   results reproducible regardless of what happens to that repo later.
2. Filing dates: filing_signature_dates.json, produced by
   scrape_ptr_filing_dates.py. This is the important part — the snapshot's
   own `disclosure_date` field is NOT the real filing date. Spot-checking
   two PDFs against the source showed `disclosure_date` tracks the form's
   internal "Notification Date" (near-real-time, ~1 day after each
   transaction), not the "Digitally Signed" date on the certification page
   (the actual multi-week lag the STOCK Act allows). Using the wrong field
   would understate lag by weeks and manufacture a fake edge — this
   backtest uses the scraped signature date exclusively.
3. Prices: fetched fresh from Alpaca per run (not price_history — that
   table only covers the ~523-symbol S&P500+ETF scannable universe, and
   Shreve/Hern trade well outside it, e.g. AWK, ARCB, BKE).

Scope, matching the design doc's explicit v1 decisions:
  - Buys only (type == "Purchase"). Sells are excluded — the design doc's
    reasoning (tax-loss harvesting, rebalancing, divestment-for-optics are
    all noisy non-signals) applies just as much to backtesting as to live
    trading.
  - Common stock only (asset_type == "Stock") — options/bonds/funds
    excluded, not a like-for-like "buy shares, hold N days" comparison.
  - Entry price = first available trading day's close on/after the real
    filing date (same as-of convention as Experiment 003).
  - No position sizing, no portfolio simulation — this is a per-trade
    signal-level test (Experiment 002's shape), not a portfolio walk-forward
    (Experiment 003's shape). Portfolio-level mechanics are explicitly out
    of scope until this per-trade gate passes.

Sample size warning (see design doc "Known Risks"): two people, ~350-450
resolved trades combined. Wilson 95% CIs are reported throughout and
should be read as wide, not as a hidden caveat.

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 research/backtests/backtest_congressional_shadow.py
"""

import bisect
import json
import logging
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EXPERIMENT_ID = "004_congressional_shadow"
GIT_COMMIT = os.environ.get("BACKTEST_GIT_COMMIT", "unknown")

HERE = Path(__file__).parent
TRANSACTIONS_SNAPSHOT = HERE / "ptr_transactions_snapshot.json"
FILING_DATES = HERE / "filing_signature_dates.json"

ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET", "")
ALPACA_DATA_BASE = "https://data.alpaca.markets"
ALPACA_HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
BATCH_SIZE = 8
SLEEP_BETWEEN_BATCHES = 0.4

FORWARD_OFFSETS = (5, 20, 60)     # trading days
MAX_PLAUSIBLE_LAG_DAYS = 120       # drop rows outside this as likely data errors
MIN_PLAUSIBLE_LAG_DAYS = 0


# ─────────────────────────────────────────────────────────────────────────
# Load + clean transaction data
# ─────────────────────────────────────────────────────────────────────────

def load_clean_trades():
    rows = json.loads(TRANSACTIONS_SNAPSHOT.read_text())
    filing_dates = json.loads(FILING_DATES.read_text())

    trades = []
    dropped_no_filing_date = 0
    dropped_bad_lag = 0
    dropped_not_buy_stock = 0

    for r in rows:
        if r.get("type") != "Purchase" or r.get("asset_type") != "Stock":
            dropped_not_buy_stock += 1
            continue
        fdate_str = filing_dates.get(r["filing_id"])
        if not fdate_str:
            dropped_no_filing_date += 1
            continue
        try:
            txn_date = datetime.strptime(r["transaction_date"], "%m/%d/%Y").date()
            filing_date = datetime.strptime(fdate_str, "%m/%d/%Y").date()
        except Exception:
            dropped_no_filing_date += 1
            continue
        lag_days = (filing_date - txn_date).days
        if not (MIN_PLAUSIBLE_LAG_DAYS <= lag_days <= MAX_PLAUSIBLE_LAG_DAYS):
            dropped_bad_lag += 1
            continue
        trades.append({
            "member": r["representative"], "ticker": r["ticker"],
            "transaction_date": txn_date, "filing_date": filing_date,
            "lag_days": lag_days, "amount_mid": r.get("amount_mid"),
            "filing_id": r["filing_id"], "source_url": r["source_url"],
        })

    log.info(f"Loaded {len(rows)} raw rows -> {len(trades)} clean buy/stock trades "
              f"(dropped: {dropped_not_buy_stock} not-buy/not-stock, "
              f"{dropped_no_filing_date} no resolvable filing date, "
              f"{dropped_bad_lag} implausible lag)")
    return trades


# ─────────────────────────────────────────────────────────────────────────
# Price fetching (Alpaca, fresh — Shreve/Hern trade well outside the
# S&P500+ETF universe price_history covers)
# ─────────────────────────────────────────────────────────────────────────

def fetch_bars_batch(symbols, start):
    params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start,
              "limit": 10000, "feed": "iex", "adjustment": "split"}
    all_bars = {}
    page_token = None
    while True:
        if page_token:
            params["page_token"] = page_token
        r = requests.get(f"{ALPACA_DATA_BASE}/v2/stocks/bars",
                          headers=ALPACA_HEADERS, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for sym, bars in (data.get("bars") or {}).items():
            all_bars.setdefault(sym, []).extend(bars)
        page_token = data.get("next_page_token")
        if not page_token:
            break
    return all_bars


def fetch_price_series(tickers, start):
    """ticker -> (dates, closes) ascending. Batched, same pattern as
    backfill_alpaca.py. Tickers Alpaca has no data for (delisted, ticker
    changed, ETN, etc.) are simply absent from the result."""
    series = {}
    tickers = sorted(tickers)
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        try:
            bars_by_symbol = fetch_bars_batch(batch, start)
        except Exception as e:
            log.warning(f"Price batch {batch} failed: {e}")
            time.sleep(SLEEP_BETWEEN_BATCHES)
            continue
        for sym, bars in bars_by_symbol.items():
            dates, closes = [], []
            for b in bars:
                if b.get("c") is None:
                    continue
                dates.append(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).date())
                closes.append(float(b["c"]))
            if dates:
                series[sym] = (dates, closes)
        time.sleep(SLEEP_BETWEEN_BATCHES)
    log.info(f"Fetched price data for {len(series)}/{len(tickers)} tickers")
    return series


def asof_index(dates, target_date):
    idx = bisect.bisect_left(dates, target_date)
    return idx if idx < len(dates) else None


# ─────────────────────────────────────────────────────────────────────────
# Per-trade forward returns
# ─────────────────────────────────────────────────────────────────────────

def compute_outcome(trade, series, spy_dates, spy_closes):
    sym_data = series.get(trade["ticker"])
    if not sym_data:
        return None
    dates, closes = sym_data
    entry_idx = asof_index(dates, trade["filing_date"])
    if entry_idx is None:
        return None
    entry_price = closes[entry_idx]
    entry_date = dates[entry_idx]

    spy_entry_idx = asof_index(spy_dates, entry_date)
    if spy_entry_idx is None:
        return None
    spy_entry_price = spy_closes[spy_entry_idx]

    result = {**trade, "entry_date": str(entry_date), "entry_price": entry_price}
    for offset in FORWARD_OFFSETS:
        fwd_idx = entry_idx + offset
        spy_fwd_idx = spy_entry_idx + offset
        if fwd_idx < len(closes) and spy_fwd_idx < len(spy_closes):
            ret = (closes[fwd_idx] - entry_price) / entry_price * 100
            spy_ret = (spy_closes[spy_fwd_idx] - spy_entry_price) / spy_entry_price * 100
            result[f"return_{offset}d"] = ret
            result[f"spy_return_{offset}d"] = spy_ret
            result[f"excess_{offset}d"] = ret - spy_ret
    return result


# ─────────────────────────────────────────────────────────────────────────
# Aggregation (Wilson CI, same formula as Experiment 002)
# ─────────────────────────────────────────────────────────────────────────

def wilson_ci(wins, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))) / denom
    return (round(100 * max(0, center - margin), 1), round(100 * min(1, center + margin), 1))


def summarize(outcomes, offset):
    key = f"excess_{offset}d"
    ret_key = f"return_{offset}d"
    rows = [o for o in outcomes if key in o]
    if not rows:
        return None
    rets = [o[ret_key] for o in rows]
    excess = [o[key] for o in rows]
    wins_vs_spy = sum(1 for x in excess if x > 0)
    wins_raw = sum(1 for x in rets if x > 0)
    ci_lo, ci_hi = wilson_ci(wins_vs_spy, len(rows))
    return {
        "n": len(rows),
        "win_rate_raw": round(100 * wins_raw / len(rows), 1),
        "win_rate_vs_spy": round(100 * wins_vs_spy / len(rows), 1),
        "win_rate_vs_spy_ci95": [ci_lo, ci_hi],
        "mean_return_pct": round(statistics.mean(rets), 2),
        "median_return_pct": round(statistics.median(rets), 2),
        "mean_spy_return_pct": round(statistics.mean(o[f"spy_return_{offset}d"] for o in rows), 2),
        "mean_excess_vs_spy_pct": round(statistics.mean(excess), 2),
        "median_excess_vs_spy_pct": round(statistics.median(excess), 2),
    }


def main():
    if not ALPACA_KEY or not ALPACA_SECRET:
        log.error("ALPACA_API_KEY / ALPACA_API_SECRET not set")
        sys.exit(1)

    trades = load_clean_trades()
    if not trades:
        log.error("No clean trades to backtest")
        sys.exit(1)

    lags = [t["lag_days"] for t in trades]
    log.info(f"Filing lag: median={statistics.median(lags):.0f}d mean={statistics.mean(lags):.1f}d "
              f"max={max(lags)}d pct>45d={100*sum(1 for x in lags if x>45)/len(lags):.1f}%")

    tickers = {t["ticker"] for t in trades} | {"SPY"}
    earliest_filing = min(t["filing_date"] for t in trades)
    start = earliest_filing.isoformat()
    log.info(f"Fetching prices for {len(tickers)} tickers from {start}...")
    series = fetch_price_series(tickers, start)

    spy_data = series.get("SPY")
    if not spy_data:
        log.error("No SPY price data — cannot compute benchmark. Aborting.")
        sys.exit(1)
    spy_dates, spy_closes = spy_data

    outcomes = []
    unresolvable = 0
    for t in trades:
        o = compute_outcome(t, series, spy_dates, spy_closes)
        if o is None:
            unresolvable += 1
            continue
        outcomes.append(o)
    log.info(f"Resolved outcomes for {len(outcomes)}/{len(trades)} trades "
              f"({unresolvable} unresolvable — no price data or no as-of match)")

    report = {
        "experiment_id": EXPERIMENT_ID,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": GIT_COMMIT,
        "config": {"forward_offsets_trading_days": FORWARD_OFFSETS,
                   "max_plausible_lag_days": MAX_PLAUSIBLE_LAG_DAYS,
                   "data_source": "github.com/TattooedHead/house-stock-watcher-data (frozen snapshot, 2026-07-14) "
                                   "+ scraped PTR signature dates (filing_signature_dates.json)"},
        "n_clean_trades": len(trades),
        "n_resolved_outcomes": len(outcomes),
        "n_unresolvable": unresolvable,
        "filing_lag_days": {"median": statistics.median(lags), "mean": round(statistics.mean(lags), 1),
                             "max": max(lags), "pct_over_45d": round(100 * sum(1 for x in lags if x > 45) / len(lags), 1)},
        "pooled": {f"{o}d": summarize(outcomes, o) for o in FORWARD_OFFSETS},
        "by_member": {
            member: {f"{o}d": summarize([x for x in outcomes if x["member"] == member], o) for o in FORWARD_OFFSETS}
            for member in ("Jefferson Shreve", "Kevin Hern")
        },
    }

    out_path = "/tmp/backtest_results_004.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info(f"Full results written to {out_path}")

    print(f"\n=== Experiment {EXPERIMENT_ID} (commit {GIT_COMMIT[:8]}) ===")
    print(f"Clean trades: {len(trades)} | Resolved: {len(outcomes)} | Unresolvable: {unresolvable}")
    print(f"Filing lag: median={report['filing_lag_days']['median']:.0f}d mean={report['filing_lag_days']['mean']}d "
          f"max={report['filing_lag_days']['max']}d  pct>45d={report['filing_lag_days']['pct_over_45d']}%")

    print("\n--- POOLED (both members) ---")
    for offset in FORWARD_OFFSETS:
        s = report["pooled"][f"{offset}d"]
        if not s:
            print(f"  {offset}d: no resolved trades")
            continue
        print(f"  {offset}d: N={s['n']:>4}  win_vs_spy={s['win_rate_vs_spy']:>5.1f}% (CI {s['win_rate_vs_spy_ci95']})  "
              f"mean_return={s['mean_return_pct']:>+6.2f}%  mean_spy={s['mean_spy_return_pct']:>+6.2f}%  "
              f"mean_excess={s['mean_excess_vs_spy_pct']:>+6.2f}%")

    for member in ("Jefferson Shreve", "Kevin Hern"):
        print(f"\n--- {member} ---")
        for offset in FORWARD_OFFSETS:
            s = report["by_member"][member][f"{offset}d"]
            if not s:
                print(f"  {offset}d: no resolved trades")
                continue
            print(f"  {offset}d: N={s['n']:>4}  win_vs_spy={s['win_rate_vs_spy']:>5.1f}% (CI {s['win_rate_vs_spy_ci95']})  "
                  f"mean_return={s['mean_return_pct']:>+6.2f}%  mean_spy={s['mean_spy_return_pct']:>+6.2f}%  "
                  f"mean_excess={s['mean_excess_vs_spy_pct']:>+6.2f}%")


if __name__ == "__main__":
    main()

"""
Exit-policy replay: hold the live mean-reversion signal's ENTRIES fixed and
replay each one under alternative EXIT policies, so any difference in
outcome is attributable to the exit alone.

Mechanism only -- this script ships no policy parameters as defaults and no
findings. The policies under test are supplied at run time as a JSON file
(see --policies); the preregistered protocol and results live in DocMost,
not in this repo.

What is replicated from live (shared/exit_policies.py over shared/signals.py,
imported, not copied):
  entry   score_signal(side="buy") on a trailing 252-close window (live
          fetch_closes("1y")), RSI/BB/regime/RS-vs-SPY/ATR(24-bar window)
          exactly as compute_signals() feeds them; gate
          final_score >= score_proposal_min + market score_modifier for
          that date (regime/structure scoring off, as in prod). Signal at
          close t, fill at open t+1. One position per symbol, flat only.
  exits   hard stop stop_loss_pct below the fill (intraday, like the
          live OTO leg); thesis_complete (close >= SMA20); overbought
          (sell-side score over the same gate); time_stop (>= 20 bars
          since the signal bar); regime_deterioration (market overall ==
          bear_fear). Close-based exits fill at the next open.
Not replicated (account level): position/slot caps, risk-engine
open-risk cap, sector caps, earnings blackout, loss-streak/breadth
pauses, portfolio stop, approval lag.

Candidate stop mechanics (all policies): MFE is measured from the fill on
bar highs. A candidate stop is recomputed after each close and is only
active from the next bar, so a level raised by bar t's high can never fill
on bar t (no same-bar high/low ordering assumption). A stop fills at its
level, or at the open when the bar gaps through it. The effective stop is
max(hard stop, candidate stop); every live close-based exit stays active.

Policy JSON: a list of objects, e.g.
  {"name": "...", "arm_mfe": <frac>,
   "floor": [[mfe_from, lock_ratio], ...] | null,
   "trail_atr": [[mfe_from, atr_mult], ...] | null}
`floor` sets stop = fill * (1 + lock * mfe); `trail_atr` sets
stop = highest_high - mult * ATR; for each, the step with the largest
mfe_from <= current MFE applies. Both ratchet monotonically. A policy with
neither is the live baseline.

Inputs are CSV exports (so the run needs no live DB connection):
  prices:  symbol,d,open,high,low,close,adjclose   (price_history)
  regime:  trading_date,overall,score_modifier     (market_regime_history)
OHLC is split/dividend-adjusted by each row's adjclose/close ratio, same as
shared/daily_ema_pullback_strategy.load_adjusted_bars().

Usage:
  python backtest_exit_policy_replay.py --prices p.csv.gz --regime r.csv \
      --policies policies.json --out results.json [--params params.json]
"""

import argparse
import csv
import datetime as dt
import gzip
import json
import os
import sys
from collections import defaultdict
from multiprocessing import Pool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

from signals import DEFAULTS as SIGNAL_DEFAULTS  # noqa: E402
from exit_policies import MarketContext, WARMUP, live_cycle, simulate_exit  # noqa: E402
from volatility_forecast import realized_vol_daily  # noqa: E402
from market_structure import percentile_rank  # noqa: E402

VOL_WINDOW = 20
VOL_PCT_LOOKBACK = 100
POST_EXIT_HORIZONS = (1, 3, 5, 10)

_G = {}  # per-worker globals (market context, params, policies)


def load_prices(path):
    opener = gzip.open if path.endswith(".gz") else open
    by_sym = defaultdict(list)
    with opener(path, "rt") as f:
        for r in csv.DictReader(f):
            sym = r["symbol"]
            if sym.startswith("^") or "." in sym or "=" in sym:
                continue  # indices / non-US listings are never signal candidates
            close = float(r["close"])
            adj = r["adjclose"]
            factor = float(adj) / close if adj not in ("", None) and close else 1.0
            by_sym[sym].append((dt.date.fromisoformat(r["d"]), float(r["open"]) * factor,
                                float(r["high"]) * factor, float(r["low"]) * factor, close * factor))
    out = {}
    for sym, rows in by_sym.items():
        rows.sort()
        out[sym] = {"d": [x[0] for x in rows], "o": [x[1] for x in rows], "h": [x[2] for x in rows],
                    "l": [x[3] for x in rows], "c": [x[4] for x in rows]}
    return out


def load_regime(path):
    dates, vals = [], []
    with open(path) as f:
        for r in csv.DictReader(f):
            dates.append(dt.date.fromisoformat(r["trading_date"]))
            vals.append((r["overall"], int(float(r["score_modifier"] or 0))))
    return dates, vals


def precompute(s, p, ctx, start_date):
    """Per-bar: buy_ok, close_exit_reason, atr. None before warm-up or the
    first signal date."""
    n = len(s["c"])
    buy_ok, close_exit, atr_arr = [False] * n, [None] * n, [None] * n
    for t in range(WARMUP - 1, n):
        ok, reason, atr = live_cycle(s, t, p, ctx)
        atr_arr[t] = atr
        if s["d"][t] < start_date:
            continue
        buy_ok[t], close_exit[t] = ok, reason
    return buy_ok, close_exit, atr_arr


def entry_vol_regime(s, t):
    c = s["c"][max(0, t - VOL_WINDOW - VOL_PCT_LOOKBACK - 1): t + 1]
    vol, _, status = realized_vol_daily(c, VOL_WINDOW)
    if vol is None:
        return "insufficient_data"
    series = []
    for i in range(max(0, len(c) - VOL_PCT_LOOKBACK), len(c)):
        v, _, st = realized_vol_daily(c[: i + 1], VOL_WINDOW)
        if v is not None:
            series.append(v)
    pct = percentile_rank(vol, series) if series else None
    if pct is None:
        return "insufficient_data"
    return "compression" if pct < 25 else ("expansion" if pct > 75 else "normal")


def _init(ctx, p, policies, start_date, cost):
    _G.update(ctx=ctx, p=p, policies=policies, start=start_date, cost=cost)


def run_symbol(args):
    sym, s = args
    p, policies, cost = _G["p"], _G["policies"], _G["cost"]
    buy_ok, close_exit, atr_arr = precompute(s, p, _G["ctx"], _G["start"])
    baseline = next(pl for pl in policies if not pl.get("floor") and not pl.get("trail_atr"))
    rows = []
    n = len(s["c"])
    t = WARMUP - 1
    last_fill = None
    while t < n - 1:
        if not buy_ok[t] or (last_fill and (s["d"][t] - last_fill).days < p["buy_cooldown_days"]):
            t += 1
            continue
        e = t + 1
        fill = s["o"][e]
        last_fill = s["d"][e]
        base = simulate_exit(s, close_exit.__getitem__, atr_arr.__getitem__, e, fill, baseline, p["stop_loss_pct"])
        if base is None:
            break  # still open at end of data -- excluded
        row = {"symbol": sym, "signal_date": s["d"][t].isoformat(), "entry_date": s["d"][e].isoformat(),
               "fill": fill, "vol_regime": entry_vol_regime(s, t), "policies": {}}
        for pl in policies:
            r = base if pl is baseline else simulate_exit(s, close_exit.__getitem__, atr_arr.__getitem__, e, fill, pl, p["stop_loss_pct"])
            if r is None:
                continue
            x = r["x"]
            post = {}
            if r["reason"] == "candidate_stop":
                for k in POST_EXIT_HORIZONS:
                    seg = s["h"][x + 1: x + 1 + k]
                    post[k] = (max(seg) / r["exit_price"] - 1) if len(seg) == k else None
            row["policies"][pl["name"]] = {
                "exit_date": s["d"][x].isoformat(), "reason": r["reason"], "bars": x - e,
                "ret": r["exit_price"] / fill - 1 - cost, "mfe": r["mfe"], "post": post,
            }
        rows.append(row)
        t = base["x"]  # flat from the baseline exit bar onward
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", required=True)
    ap.add_argument("--regime", required=True)
    ap.add_argument("--policies", required=True)
    ap.add_argument("--params", help="JSON of signal_params overrides (e.g. live prod values)")
    ap.add_argument("--start", required=True, help="first signal date YYYY-MM-DD")
    ap.add_argument("--round-trip-cost", type=float, required=True, help="fractional cost per round trip")
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    a = ap.parse_args()

    p = dict(SIGNAL_DEFAULTS)
    if a.params:
        with open(a.params) as f:
            p.update({k: float(v) for k, v in json.load(f).items()})
    with open(a.policies) as f:
        policies = json.load(f)
    prices = load_prices(a.prices)
    regime_dates, regime_vals = load_regime(a.regime)
    ctx = MarketContext(regime_dates, regime_vals, prices["SPY"]["d"], prices["SPY"]["c"])
    start = dt.date.fromisoformat(a.start)

    with Pool(a.workers, initializer=_init, initargs=(ctx, p, policies, start, a.round_trip_cost)) as pool:
        chunks = pool.map(run_symbol, sorted(prices.items()), chunksize=4)
    trades = [r for ch in chunks for r in ch]
    with open(a.out, "w") as f:
        json.dump({"symbols": len(prices), "params": p, "policies": policies, "trades": trades}, f)
    print(f"{len(trades)} closed baseline entries across {len(prices)} symbols -> {a.out}")


if __name__ == "__main__":
    main()

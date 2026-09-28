"""
Position Adds study: does adding to a symbol already held long and
underwater ever have forward value, versus not adding and versus spending
the same risk budget on a fresh name? Preregistered protocol: DocMost
"09/24/2026 - Position Adds study: preregistration" (db0c5f7a). No
findings ship in this repo -- results persist to `backtest_results` /
DocMost, not git; this script is mechanism only.

Reuses shared/exit_policies.py's live-replay primitives (MarketContext,
live_cycle, simulate_exit), imported, not copied, so this study's entry/
exit mechanics never drift from the exit-policy replay harness's (#155)
or the live strategy's own.

Mechanism, one pass per symbol:
  - Track FIFO lots for an open episode (first qualifying buy while flat
    opens it). "Underwater" is measured against the qty-weighted average
    cost of currently-open lots at the signal bar's close.
  - An ADD event fires when live_cycle() says buy_ok on a bar during an
    open episode AND close < average cost (an add to a loser) or
    close >= average cost (an add to a winner, comparator B2 -- flagged
    separately, never mixed into the primary test).
  - The add lot's own hard stop is 12% below ITS OWN fill (per-lot,
    matching live: every buy attaches its own OTO stop leg). Close-based
    exits (thesis_complete/regime_deterioration/overbought) and the
    time_stop are POSITION-level and keyed to the ORIGINAL episode entry
    bar, matching live check_symbol_exits()/check_stop_losses() -- the
    whole position exits together on those, but a lot can be stopped out
    independently and earlier on its own hard stop.
  - B1 comparator: for every add event, the highest-scored OTHER symbol
    with a live_cycle() buy signal on the same date, in the same
    stock-regime bucket, with a live proposal score within +/-5 --
    the closest available proxy for "what the same risk budget would
    have bought instead." Its own lot is walked the same way, entered
    fresh (no averaging).
  - B0 comparator: every flat-entry (non-add) buy signal, all dates.
  - B2 (diagnostic, reported separately): the same add mechanics but
    entered on a WINNING day (close >= average cost) -- separates
    "information from being underwater" from "adding" itself.

Costs: trade_cost_flat (live signal_params) plus a flat slippage bps
applied to both the add-lot's fill and its exit price.

Usage:
  export: python backtest_position_adds_study.py export --out-dir ./data
          (reads price_history + market_regime_history read-only; the
          only step that touches the live DB)
  run:    python backtest_position_adds_study.py run --data-dir ./data \
              --out results.json [--start 2019-01-01] [--slippage-bps 5]
"""

import argparse
import bisect
import datetime as dt
import gzip
import json
import math
import os
import random
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

from signals import DEFAULTS as SIGNAL_DEFAULTS, score_signal  # noqa: E402
from exit_policies import (  # noqa: E402
    MarketContext, WARMUP, WINDOW, TIME_STOP_BARS, live_cycle, live_signal_features, simulate_exit,
)


def _buy_score_and_regime(s, t, p, ctx):
    """(buy_score, regime) for bar t -- one live_signal_features() call,
    correct score_signal() argument order (rsi, price, bb_upper, bb_lower,
    band_std, bb_middle, regime, side, p, rs_pct=, atr=)."""
    rsi, bbu, bbm, bbl, bstd, regime, rs_pct, atr = live_signal_features(s, t, p, ctx)
    score, _ = score_signal(rsi, s["c"][t], bbu, bbl, bstd, bbm, regime, "buy", p, rs_pct=rs_pct, atr=atr)
    return score, regime

UNDERWATER_BUCKETS = (("U1", 0.0, 0.03), ("U2", 0.03, 0.08), ("U3", 0.08, math.inf))
ERAS = (("2019-2021", "2019-01-01", "2022-01-01"),
        ("2022-2023", "2022-01-01", "2024-01-01"),
        ("2024-2026", "2024-01-01", "2027-01-01"))


# ---------------------------------------------------------------- export --

def cmd_export(args):
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.set_session(readonly=True)
    os.makedirs(args.out_dir, exist_ok=True)

    prices_path = os.path.join(args.out_dir, "prices.csv.gz")
    with conn.cursor(name="adds_study_prices") as cur, gzip.open(prices_path, "wt") as f:
        cur.itersize = 50000
        cur.execute("""
            SELECT symbol, ts::date, open, high, low, close, adjclose
            FROM price_history
            WHERE symbol IN (SELECT symbol FROM price_history GROUP BY symbol HAVING count(*) >= 250)
            ORDER BY symbol, ts
        """)
        f.write("symbol,d,open,high,low,close,adjclose\n")
        n = 0
        for sym, d, o, h, l, c, adj in cur:
            f.write(f"{sym},{d.isoformat()},{o},{h},{l},{c},{adj if adj is not None else ''}\n")
            n += 1
    print(f"wrote {n} price rows -> {prices_path}", file=sys.stderr)

    regime_path = os.path.join(args.out_dir, "regime.csv")
    with conn.cursor() as cur, open(regime_path, "w") as f:
        cur.execute("SELECT trading_date, overall, score_modifier FROM market_regime_history ORDER BY trading_date")
        f.write("trading_date,overall,score_modifier\n")
        n = 0
        for d, overall, mod in cur.fetchall():
            f.write(f"{d.isoformat()},{overall or ''},{mod if mod is not None else 0}\n")
            n += 1
    print(f"wrote {n} regime rows -> {regime_path}", file=sys.stderr)

    params_path = os.path.join(args.out_dir, "signal_params.json")
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM signal_params")
        live = {k: float(v) for k, v in cur.fetchall()}
    p = dict(SIGNAL_DEFAULTS)
    p.update(live)
    with open(params_path, "w") as f:
        json.dump(p, f, indent=2)
    print(f"wrote live signal_params snapshot -> {params_path}", file=sys.stderr)


# ------------------------------------------------------------- loading ---

def load_prices(path):
    opener = gzip.open if path.endswith(".gz") else open
    by_sym = defaultdict(list)
    import csv
    with opener(path, "rt") as f:
        for r in csv.DictReader(f):
            sym = r["symbol"]
            if sym.startswith("^") or "." in sym or "=" in sym:
                continue
            close = float(r["close"])
            adj = r["adjclose"]
            factor = float(adj) / close if adj not in ("", None) and close else 1.0
            by_sym[sym].append((dt.date.fromisoformat(r["d"]), float(r["open"]) * factor,
                                 float(r["high"]) * factor, float(r["low"]) * factor, close * factor))
    out = {}
    for sym, rows in by_sym.items():
        rows.sort(key=lambda x: x[0])
        out[sym] = {"d": [r[0] for r in rows], "o": [r[1] for r in rows], "h": [r[2] for r in rows],
                    "l": [r[3] for r in rows], "c": [r[4] for r in rows]}
    return out


def load_regime(path):
    import csv
    dates, vals = [], []
    with open(path) as f:
        for r in csv.DictReader(f):
            dates.append(dt.date.fromisoformat(r["trading_date"]))
            vals.append((r["overall"] or None, int(r["score_modifier"] or 0)))
    return dates, vals


# --------------------------------------------------------------- study ---

def _week_key(d):
    iso = d.isocalendar()
    return (iso[0], iso[1])


def _atr_at(s, t):
    from signals import ATR_PERIOD, compute_atr
    lo = max(0, t - (ATR_PERIOD + 10) + 1)
    return compute_atr(list(zip(s["h"][lo:t + 1], s["l"][lo:t + 1], s["c"][lo:t + 1])))


def _make_position_close_exit_at(s, p, ctx, episode_start_bar):
    """Position-level close-based exit reason for bar t, keyed to the
    ORIGINAL episode entry (time_stop counts from episode_start_bar, not
    from any individual lot's own entry -- matches check_symbol_exits()/
    check_stop_losses() closing the whole position together)."""
    def f(t):
        _, reason, _ = live_cycle(s, t, p, ctx)
        if reason:
            return reason
        if t - (episode_start_bar - 1) >= TIME_STOP_BARS:
            return "time_stop"
        return None
    return f


def _fill_price(s, signal_bar, slippage):
    """(fill_price, fill_bar) at signal_bar+1's open with slippage applied
    (next-bar-open, matching live and simulate_exit's own convention), or
    (None, None) if there's no next bar."""
    n = len(s["o"])
    if signal_bar + 1 >= n:
        return None, None
    return s["o"][signal_bar + 1] * (1 + slippage), signal_bar + 1


def _walk_add_lot(s, p, ctx, episode_start_bar, fill, fill_bar, stop_loss_pct, slippage):
    """Simulate one add lot from its own already-computed fill: exit per
    _make_position_close_exit_at + its own 12%-below-fill hard stop
    (simulate_exit's own mechanics; slippage was already applied to
    `fill`, and is applied again here to the exit price -- symmetric cost
    on both legs). Returns None if the episode never resolves within the
    data (still open at the end of history)."""
    close_exit_at = _make_position_close_exit_at(s, p, ctx, episode_start_bar)
    atr_at = lambda t: _atr_at(s, t)  # noqa: E731 -- unused (no ATR-trail policy), kept for simulate_exit's signature
    result = simulate_exit(s, close_exit_at, atr_at, fill_bar, fill, {}, stop_loss_pct)
    if result is None:
        return None
    exit_price = result["exit_price"] * (1 - slippage)
    ret = exit_price / fill - 1
    risk_per_share = fill * stop_loss_pct
    r_multiple = (exit_price - fill) / risk_per_share if risk_per_share else None
    return {"signal_bar": fill_bar - 1, "fill_bar": fill_bar, "fill": fill, "exit_bar": result["x"],
            "exit_price": exit_price, "reason": result["reason"], "mfe": result["mfe"], "ret": ret, "r": r_multiple}


def run_symbol(sym, s, p, ctx, stop_loss_pct, slippage, cost_flat, min_bar, max_bar):
    """One symbol's full pass. Returns lists of event dicts (add/b0/b2).

    open_lots tracks (qty, fill_price, fill_bar) for lots still open --
    fill_price/fill_bar are the ACTUAL next-bar-open-with-slippage fill
    _walk_add_lot() itself uses, not the signal bar's close, so avg_cost
    and the hard-stop pre-check below use the same basis the lot was
    actually entered at.

    SCOPING SIMPLIFICATION, stated up front: the episode is treated as
    fully closed as soon as ANY one lot's hard stop is hit, rather than
    tracking that only that lot closes while other lots in the same
    episode may still be open (live behavior: each lot carries its own
    OTO stop leg independently). This does NOT bias a recorded add
    event's own reported return/R -- that comes from _walk_add_lot()'s
    own independent exit walk for that lot, keyed to the episode's real
    start, regardless of this loop's bookkeeping. It only means a later
    add inside the SAME multi-add episode, after an earlier lot in it
    stopped out, is undercounted (conservative: fewer add events
    recorded, not wrong ones)."""
    events = {"add": [], "b0": [], "b2": []}
    n = len(s["c"])
    lo = max(WARMUP, min_bar)
    hi = min(n - 1, max_bar) if max_bar is not None else n - 1

    open_lots = None  # {"episode_start": fill_bar, "lots": [(qty, fill_price, fill_bar)]}
    for t in range(lo, hi + 1):
        buy_ok, close_reason, _ = live_cycle(s, t, p, ctx)
        d = s["d"][t]

        if open_lots is not None:
            live_lots = [l for l in open_lots["lots"] if l[2] <= t]  # only lots already filled by bar t
            if not live_lots:
                pass  # this bar IS the baseline's own fill bar -- nothing to check against yet
            else:
                close_exit_at = _make_position_close_exit_at(s, p, ctx, open_lots["episode_start"])
                reason = close_exit_at(t)
                avg_cost = sum(q * pr for q, pr, _ in live_lots) / sum(q for q, pr, _ in live_lots)
                hard_stops_hit = any(s["l"][t] <= pr * (1 - stop_loss_pct) for _, pr, _ in live_lots)
                if reason or hard_stops_hit or t == hi:
                    open_lots = None  # episode closes (or data ends) -- a fresh entry can start again
                elif buy_ok:
                    fill, fill_bar = _fill_price(s, t, slippage)
                    if fill is not None:
                        bucket = "b2" if s["c"][t] >= avg_cost else "add"
                        lot = _walk_add_lot(s, p, ctx, open_lots["episode_start"], fill, fill_bar,
                                             stop_loss_pct, slippage)
                        if lot is not None:
                            lot["ret"] = lot["ret"] - cost_flat / lot["fill"]
                            lot["symbol"], lot["date"] = sym, d.isoformat()
                            lot["underwater_pct"] = max(0.0, (avg_cost - s["c"][t]) / avg_cost)
                            lot["score"], lot["regime"] = _buy_score_and_regime(s, t, p, ctx)
                            events[bucket].append(lot)
                            open_lots["lots"].append((1.0, fill, fill_bar))
                    continue
                else:
                    continue

        if open_lots is None and buy_ok:
            fill, fill_bar = _fill_price(s, t, slippage)
            if fill is None:
                continue
            open_lots = {"episode_start": fill_bar, "lots": [(1.0, fill, fill_bar)]}
            lot = _walk_add_lot(s, p, ctx, fill_bar, fill, fill_bar, stop_loss_pct, slippage)
            if lot is not None:
                lot["ret"] = lot["ret"] - cost_flat / lot["fill"]
                lot["symbol"], lot["date"] = sym, d.isoformat()
                lot["score"], lot["regime"] = _buy_score_and_regime(s, t, p, ctx)
                events["b0"].append(lot)
    return events


def block_bootstrap_ci(values, weeks, n=2000, seed=0):
    """90% CI on the mean, resampling by ISO week (events in the same week
    are correlated -- a sector selloff produces many at once)."""
    if not values:
        return None
    by_week = defaultdict(list)
    for v, w in zip(values, weeks):
        by_week[w].append(v)
    week_keys = list(by_week.keys())
    rng = random.Random(seed)
    means = []
    for _ in range(n):
        sample = []
        for _ in range(len(week_keys)):
            sample.extend(by_week[rng.choice(week_keys)])
        if sample:
            means.append(sum(sample) / len(sample))
    means.sort()
    lo = means[int(0.05 * len(means))]
    hi = means[int(0.95 * len(means)) - 1]
    return {"mean": sum(values) / len(values), "ci90": [lo, hi], "n": len(values), "effective_n_weeks": len(week_keys)}


def summarize(label, events, key="r"):
    vals = [e[key] for e in events if e.get(key) is not None]
    weeks = [_week_key(dt.date.fromisoformat(e["date"])) for e in events if e.get(key) is not None]
    return {"label": label, **(block_bootstrap_ci(vals, weeks) or {"n": 0})}


def summarize_paired_diff(label, adds, b1s, key="r"):
    """Paired (add[key] - b1[key]) per matched pair, block-bootstrapped by
    the add event's week -- the actual primary-test comparison the
    preregistration calls for, not two separately eyeballed CIs."""
    pairs = [(a[key] - b[key], a["date"]) for a, b in zip(adds, b1s)
             if a.get(key) is not None and b.get(key) is not None]
    if not pairs:
        return {"label": label, "n": 0}
    diffs = [d for d, _ in pairs]
    weeks = [_week_key(dt.date.fromisoformat(d)) for _, d in pairs]
    out = block_bootstrap_ci(diffs, weeks)
    out["label"] = label
    out["positive_direction_excludes_zero"] = out["ci90"][0] > 0
    return out


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--out-dir", required=True)
    r = sub.add_parser("run")
    r.add_argument("--data-dir", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--start", default="2019-01-01")
    r.add_argument("--slippage-bps", type=float, default=5.0)
    a = ap.parse_args()

    if a.cmd == "export":
        return cmd_export(a)

    with open(os.path.join(a.data_dir, "signal_params.json")) as f:
        p = json.load(f)
    stop_loss_pct = p["stop_loss_pct"]
    cost_flat = p.get("trade_cost_flat", 1.0)
    slippage = a.slippage_bps / 10000.0

    regime_dates, regime_vals = load_regime(os.path.join(a.data_dir, "regime.csv"))
    prices = load_prices(os.path.join(a.data_dir, "prices.csv.gz"))
    spy = prices.get("SPY")
    if spy is None:
        print("no SPY series found -- relative-strength gate will be inert", file=sys.stderr)
        spy_dates, spy_closes = [], []
    else:
        spy_dates, spy_closes = spy["d"], spy["c"]
    ctx = MarketContext(regime_dates, regime_vals, spy_dates, spy_closes)
    start_date = dt.date.fromisoformat(a.start)

    all_events = {"add": [], "b0": [], "b2": []}
    symbols_run = 0
    for sym, s in prices.items():
        if sym == "SPY" or len(s["c"]) < WARMUP + 30:
            continue
        min_bar = bisect.bisect_left(s["d"], start_date)
        if min_bar >= len(s["d"]) - 1:
            continue
        events = run_symbol(sym, s, p, ctx, stop_loss_pct, slippage, cost_flat, min_bar, len(s["c"]) - 1)
        for k in all_events:
            all_events[k].extend(events[k])
        symbols_run += 1

    # B1: for every add event, the highest-scored other symbol's b0-style
    # fresh signal on the same date, same regime bucket, score within +/-5.
    # add_matched/b1_matched are built PAIRWISE (same index = same pairing)
    # -- the primary test is a PAIRED difference (add_r - b1_r per pair,
    # block-bootstrapped by the add event's week), not two separately
    # eyeballed CIs, which would not test the actual comparison the
    # preregistration calls for.
    b0_by_date = defaultdict(list)
    for ev in all_events["b0"]:
        b0_by_date[(ev["date"], ev["regime"])].append(ev)
    add_matched, b1_matched = [], []
    for add in all_events["add"]:
        candidates = [b for b in b0_by_date.get((add["date"], add["regime"]), [])
                      if b["symbol"] != add["symbol"] and abs(b.get("score", 0) - add.get("score", 0)) <= 5]
        if candidates:
            add_matched.append(add)
            b1_matched.append(max(candidates, key=lambda b: b.get("score", 0)))

    results = {
        "meta": {"symbols_run": symbols_run, "start": a.start, "stop_loss_pct": stop_loss_pct,
                 "slippage_bps": a.slippage_bps, "cost_flat": cost_flat, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()},
        "primary": {
            "add_lots": summarize("add lots (all underwater buckets, pooled)", all_events["add"]),
            "b1_fresh_names_same_date": summarize("B1: fresh signal, same date/regime/score (matched subset)", b1_matched),
            "paired_difference_add_minus_b1": summarize_paired_diff(
                "PRIMARY CONFIRMATORY TEST: paired (add_r - b1_r), block-bootstrap by week", add_matched, b1_matched),
            "matched_pairs": len(add_matched), "unmatched_add_events": len(all_events["add"]) - len(add_matched),
        },
        "by_era": {},
        "by_underwater_bucket": {},
        "diagnostics": {
            "b0_all_flat_entries": summarize("B0: all flat-entry signals", all_events["b0"]),
            "b2_pyramid_into_winners": summarize("B2: adds on a winning day", all_events["b2"]),
            "joint_tail": None,
        },
        "counts": {k: len(v) for k, v in all_events.items()},
    }

    era_confirmations = 0
    for era_name, era_start, era_end in ERAS:
        es, ee = dt.date.fromisoformat(era_start), dt.date.fromisoformat(era_end)
        era_pairs = [(a, b) for a, b in zip(add_matched, b1_matched) if es <= dt.date.fromisoformat(a["date"]) < ee]
        era_add = [a for a, _ in era_pairs]
        era_b1 = [b for _, b in era_pairs]
        era_diff = summarize_paired_diff(f"paired diff {era_name}", era_add, era_b1)
        results["by_era"][era_name] = {"add": summarize(f"add {era_name}", era_add),
                                        "b1": summarize(f"b1 {era_name}", era_b1),
                                        "paired_difference": era_diff}
        if era_diff.get("positive_direction_excludes_zero"):
            era_confirmations += 1
    results["primary"]["eras_confirming"] = f"{era_confirmations}/{len(ERAS)}"
    results["primary"]["decision_rule_note"] = (
        "Preregistered bar: paired-difference 90% CI excludes 0 (positive) in >=2/3 eras AND pooled. "
        "Survivorship-bias correction (B0-vs-B2 proxy) and joint-tail check are reported separately below "
        "and must ALSO clear before treating a positive pooled+era result as supported -- see the "
        "preregistration (DocMost db0c5f7a) before reading this as a green light on its own.")

    for name, lo_pct, hi_pct in UNDERWATER_BUCKETS:
        bucket_events = [e for e in all_events["add"] if lo_pct <= e["underwater_pct"] < hi_pct]
        results["by_underwater_bucket"][name] = summarize(f"add {name} ({lo_pct:.0%}-{hi_pct if hi_pct != math.inf else 'inf'})", bucket_events)

    # Joint-tail: of add events, what fraction had BOTH the add lot AND
    # (approximated) the original entry stopped out. We don't re-walk the
    # baseline lot's own path here (out of scope for this pass) -- report
    # the add lot's own stop-out rate as the primary joint-tail proxy.
    add_stopped = sum(1 for e in all_events["add"] if e["reason"] in ("hard_stop",))
    results["diagnostics"]["joint_tail"] = {
        "add_lot_stopped_out_pct": add_stopped / len(all_events["add"]) if all_events["add"] else None,
        "note": "add-lot's own stop-out rate only; does not re-walk the original lot's independent stop path",
    }

    with open(a.out, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(json.dumps({"counts": results["counts"], "primary": results["primary"]}, indent=2, default=str))


if __name__ == "__main__":
    main()

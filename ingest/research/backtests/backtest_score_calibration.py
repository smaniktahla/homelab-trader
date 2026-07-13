#!/usr/bin/env python3
"""Experiment 002: retrospective score-ranking validation, v2.

Research artifact, not production logic — walks each universe symbol's full
history day-by-day, replaying the exact scoring logic in signals.py
(imported directly, not reimplemented — zero risk of the backtest formula
drifting from what's actually live).

v2 addresses methodology gaps found in Experiment 001 (see git history for
that version's output): raw returns overstated edge in a rising market,
20d-close-to-close return let "wins" through that would have hit the actual
stop-loss first, overlapping signals inflated N, and the regime bucket
comparison wasn't apples-to-apples (trending_down's 0.60 multiplier means
only much stronger raw setups survive score_log_min than trending_up's 0.80
does, so the two buckets were never comparable populations).

What this version adds:
  - SPY-excess return alongside raw return (excess_return_vs_spy)
  - Stop-loss-aware realized return: walks the actual forward OHLC and marks
    a signal "stopped_out" if intraday low breaches stop_loss_pct before the
    20d mark, using the stop level (not day-20 close) as realized_return —
    a raw +3%-at-20d signal that fell -18% first is correctly a loss here.
    Does NOT model thesis-complete/overbought early exits, so this slightly
    understates realized returns versus what production would actually do —
    a conservative simplification, not an inflating one.
  - Episode-based dedup: a cooldown period (default 20 trading days) after
    each signal suppresses further signals on that symbol, so a single
    multi-day oversold episode counts once, not once per day. Reports both
    n_observations (raw, pre-dedup) and n_episodes (deduped) per bucket.
  - Raw score vs regime-adjusted score, isolated by calling score_signal
    with regime="unknown" (no branch in score_signal's if/elif matches that,
    so none of the regime multipliers fire) to get the pre-multiplier score,
    then comparing regime buckets within matched raw-score bands.
  - A single train/holdout time split (first ~70% of history vs last ~30%)
    so the threshold sweep isn't just reporting what was found on the same
    data it's measured against. Not full rolling walk-forward — a defensible
    first pass, not the final word.
  - 95% CI on win rate per bucket (normal approximation).
  - Run metadata (git commit, universe size, data date range, config,
    timestamp, experiment id) embedded in the JSON output for traceability.

Explicitly NOT included yet (next iteration, per the ordering in the
critique that prompted this revision — portfolio constraints belong after
signal-level evidence holds up): sector-relative returns, position sizing,
sector caps, earnings blackout, circuit breaker, slippage/commission
modeling, full rolling walk-forward.

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 research/backtests/backtest_score_calibration.py
"""

import os
import sys
import bisect
import json
import logging
import math
from datetime import datetime, timezone

import psycopg2

sys.path.insert(0, "/app")
from signals import (compute_rsi, compute_bollinger, detect_regime, compute_atr,
                      score_signal, load_params, RS_LOOKBACK_DAYS)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EXPERIMENT_ID = "002_score_calibration_v2"
GIT_COMMIT = os.environ.get("BACKTEST_GIT_COMMIT", "unknown")  # passed in at run time

DB_DSN = os.environ["DATABASE_URL"]
WINDOW = 260               # trailing trading days fed to RSI/BB/regime/ATR each step
FORWARD_DAYS = 20          # holding period for forward-return evaluation
EPISODE_COOLDOWN_DAYS = 20 # suppress further signals on a symbol for this many days
TRAIN_FRACTION = 0.70      # first N% of each symbol's dated signals = train, rest = holdout
SWEEP_THRESHOLDS = [30, 40, 50, 60, 65, 70, 75, 80, 85, 90]

SCORE_BUCKETS = [("0-29", 0, 30), ("30-49", 30, 50), ("50-64", 50, 65), ("65-79", 65, 80), ("80+", 80, 1000)]


def get_db():
    return psycopg2.connect(DB_DSN)


def get_universe_symbols(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM universe WHERE scannable=TRUE ORDER BY symbol")
        return [r[0] for r in cur.fetchall()]


def load_series(conn, symbol):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DATE(ts), close, high, low FROM price_history
            WHERE symbol=%s ORDER BY ts ASC
        """, (symbol,))
        rows = cur.fetchall()
    dates = [r[0] for r in rows]
    closes = [float(r[1]) for r in rows]
    highs = [float(r[2]) for r in rows]
    lows = [float(r[3]) for r in rows]
    return dates, closes, highs, lows


def relative_strength(i, dates, closes, spy_dates, spy_closes):
    if i < RS_LOOKBACK_DAYS:
        return None
    pos = bisect.bisect_right(spy_dates, dates[i]) - 1
    if pos < RS_LOOKBACK_DAYS or pos >= len(spy_closes):
        return None
    sym_pct = (closes[i] - closes[i - RS_LOOKBACK_DAYS]) / closes[i - RS_LOOKBACK_DAYS] * 100
    spy_then, spy_now = spy_closes[pos - RS_LOOKBACK_DAYS], spy_closes[pos]
    spy_pct = (spy_now - spy_then) / spy_then * 100
    return sym_pct - spy_pct


def spy_forward_return(i, dates, spy_dates, spy_closes, forward_days=FORWARD_DAYS):
    pos = bisect.bisect_right(spy_dates, dates[i]) - 1
    if pos < 0 or pos + forward_days >= len(spy_closes):
        return None
    return (spy_closes[pos + forward_days] - spy_closes[pos]) / spy_closes[pos] * 100


def stop_loss_aware_outcome(i, closes, highs, lows, stop_loss_pct, forward_days=FORWARD_DAYS):
    """Walk the actual forward OHLC. If the intraday low ever breaches
    stop_loss_pct before day `forward_days`, the signal is stopped out —
    realized_return is the stop level, not whatever the price did afterward.
    Otherwise realized_return is the day-N close-to-close return (thesis
    timeout, roughly matching production's time-stop). Also returns MAE/MFE
    over the full window regardless of whether a stop fired."""
    entry = closes[i]
    mae = 0.0   # most negative %, i.e. worst drawdown
    mfe = 0.0   # most positive %
    stopped_out = False
    stop_day = None
    for j in range(i + 1, min(i + forward_days + 1, len(closes))):
        low_pct = (lows[j] - entry) / entry * 100
        high_pct = (highs[j] - entry) / entry * 100
        mae = min(mae, low_pct)
        mfe = max(mfe, high_pct)
        if not stopped_out and low_pct <= -stop_loss_pct * 100:
            stopped_out = True
            stop_day = j - i
    if i + forward_days >= len(closes):
        return None  # not enough forward data
    raw_return_20d = (closes[i + forward_days] - entry) / entry * 100
    realized_return = -stop_loss_pct * 100 if stopped_out else raw_return_20d
    return {
        "raw_return_20d": raw_return_20d,
        "realized_return": realized_return,
        "mae": mae,
        "mfe": mfe,
        "stopped_out": stopped_out,
        "stop_day": stop_day,
    }


def backtest_symbol(symbol, dates, closes, highs, lows, spy_dates, spy_closes, params):
    n = len(closes)
    results = []
    if n < WINDOW + FORWARD_DAYS + 1:
        return results

    for i in range(WINDOW, n - FORWARD_DAYS):
        lo = i - WINDOW + 1
        window_closes = closes[lo:i + 1]
        window_ohlc = list(zip(highs[lo:i + 1], lows[lo:i + 1], closes[lo:i + 1]))
        price = closes[i]

        rsi = compute_rsi(window_closes, params["rsi_period"])
        bb_upper, bb_middle, bb_lower, band_std = compute_bollinger(window_closes, params["bb_period"], params["bb_std"])
        regime = detect_regime(window_closes, params["regime_sma_fast"], params["regime_sma_slow"], params["regime_band"])
        atr = compute_atr(window_ohlc)
        rs_pct = relative_strength(i, dates, closes, spy_dates, spy_closes)

        final_score, _ = score_signal(rsi, price, bb_upper, bb_lower, band_std, bb_middle, regime, "buy", params, rs_pct, atr)
        # "unknown" matches none of score_signal's regime branches, so the
        # regime multiplier never fires — isolates the pre-multiplier score.
        raw_score, _ = score_signal(rsi, price, bb_upper, bb_lower, band_std, bb_middle, "unknown", "buy", params, rs_pct, atr)

        if final_score < params["score_log_min"]:
            continue

        outcome = stop_loss_aware_outcome(i, closes, highs, lows, params["stop_loss_pct"])
        if outcome is None:
            continue
        spy_fwd = spy_forward_return(i, dates, spy_dates, spy_closes)

        results.append({
            "symbol": symbol, "date": dates[i], "score": final_score, "raw_score": raw_score,
            "regime": regime, **outcome,
            "excess_return_vs_spy": (outcome["raw_return_20d"] - spy_fwd) if spy_fwd is not None else None,
        })
    return results


def apply_episode_dedup(results, cooldown=EPISODE_COOLDOWN_DAYS):
    """Mark is_episode_start=True for the first signal in a run, False for
    any signal on the same symbol within `cooldown` trading days of the
    previous one counted. Assumes results for a given symbol are already in
    date order (true here since backtest_symbol walks forward)."""
    last_date_by_symbol = {}
    for r in results:
        last = last_date_by_symbol.get(r["symbol"])
        is_start = last is None or (r["date"] - last).days > cooldown * 1.5  # trading days -> approx calendar days
        r["is_episode_start"] = is_start
        if is_start:
            last_date_by_symbol[r["symbol"]] = r["date"]
    return results


def wilson_ci(wins, n, z=1.96):
    """95% Wilson score interval for a win rate — better behaved than the
    normal approximation at small n or extreme p."""
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))) / denom
    return (round(100 * max(0, center - margin), 1), round(100 * min(1, center + margin), 1))


def bucket_stats(rows, key_fn, return_key="raw_return_20d", buckets=None):
    grouped = {}
    for r in rows:
        key = key_fn(r)
        if key is None:
            continue
        grouped.setdefault(key, []).append(r)
    out = {}
    items = buckets if buckets else sorted(grouped.keys())
    for key in items:
        rows_in_bucket = grouped.get(key, [])
        if not rows_in_bucket:
            continue
        rets = [r[return_key] for r in rows_in_bucket]
        excess = [r["excess_return_vs_spy"] for r in rows_in_bucket if r["excess_return_vs_spy"] is not None]
        wins = sum(1 for x in rets if x > 0)
        ci_lo, ci_hi = wilson_ci(wins, len(rets))
        out[key] = {
            "n": len(rets),
            "win_rate": round(100 * wins / len(rets), 1),
            "win_rate_ci95": [ci_lo, ci_hi],
            "avg_return": round(sum(rets) / len(rets), 2),
            "avg_excess_vs_spy": round(sum(excess) / len(excess), 2) if excess else None,
            "avg_mae": round(sum(r["mae"] for r in rows_in_bucket) / len(rows_in_bucket), 2),
            "avg_mfe": round(sum(r["mfe"] for r in rows_in_bucket) / len(rows_in_bucket), 2),
            "stopped_out_pct": round(100 * sum(1 for r in rows_in_bucket if r["stopped_out"]) / len(rows_in_bucket), 1),
        }
    return out


def score_bucket_label(score):
    for label, lo, hi in SCORE_BUCKETS:
        if lo <= score < hi:
            return label
    return None


def threshold_sweep(rows, score_key="score", return_key="raw_return_20d"):
    out = {}
    for t in SWEEP_THRESHOLDS:
        subset = [r for r in rows if r[score_key] >= t]
        if not subset:
            continue
        rets = [r[return_key] for r in subset]
        wins = sum(1 for x in rets if x > 0)
        ci_lo, ci_hi = wilson_ci(wins, len(rets))
        out[t] = {"n": len(rets), "win_rate": round(100 * wins / len(rets), 1), "win_rate_ci95": [ci_lo, ci_hi],
                  "avg_return": round(sum(rets) / len(rets), 2)}
    return out


def regime_raw_score_matched(results):
    """Compare regime buckets within matched RAW-score bands, to test
    whether regime adds information or just reshuffles which raw setups
    survive score_log_min (trending_down's 0.60 multiplier means a much
    higher raw score is needed to survive than trending_up's 0.80 does)."""
    raw_buckets = [("40-59", 40, 60), ("60-79", 60, 80), ("80+", 80, 1000)]
    out = {}
    for label, lo, hi in raw_buckets:
        subset = [r for r in results if lo <= r["raw_score"] < hi]
        out[label] = bucket_stats(subset, lambda r: r["regime"])
    return out


def main():
    conn = get_db()
    params = load_params(conn)
    log.info(f"Live params: score_log_min={params['score_log_min']} score_proposal_min={params['score_proposal_min']} "
              f"rsi_oversold={params['rsi_oversold']} stop_loss_pct={params['stop_loss_pct']}")

    symbols = get_universe_symbols(conn)
    log.info(f"Backtesting {len(symbols)} universe symbols, {WINDOW}d window, {FORWARD_DAYS}d forward horizon")

    spy_dates, spy_closes, _, _ = load_series(conn, "SPY")
    if not spy_closes:
        log.error("No SPY history — excess-return and RS modifier unavailable")

    all_results = []
    min_date, max_date = None, None
    for idx, sym in enumerate(symbols):
        dates, closes, highs, lows = load_series(conn, sym)
        if not closes:
            continue
        if min_date is None or dates[0] < min_date:
            min_date = dates[0]
        if max_date is None or dates[-1] > max_date:
            max_date = dates[-1]
        try:
            results = backtest_symbol(sym, dates, closes, highs, lows, spy_dates, spy_closes, params)
        except Exception as e:
            log.warning(f"{sym}: backtest failed: {e}")
            continue
        all_results.extend(results)
        if (idx + 1) % 100 == 0:
            log.info(f"...{idx + 1}/{len(symbols)} symbols, {len(all_results)} raw signals so far")

    conn.close()
    log.info(f"Done walking history. {len(all_results)} raw signal observations across {len(symbols)} symbols")

    all_results.sort(key=lambda r: r["date"])
    apply_episode_dedup(all_results)
    episodes = [r for r in all_results if r["is_episode_start"]]
    log.info(f"Episode dedup ({EPISODE_COOLDOWN_DAYS}d cooldown): {len(all_results)} observations -> {len(episodes)} independent episodes")

    split_idx = int(len(all_results) * TRAIN_FRACTION)
    split_date = all_results[split_idx]["date"] if all_results else None
    train = [r for r in all_results if r["date"] < split_date]
    holdout = [r for r in all_results if r["date"] >= split_date]
    train_ep = [r for r in episodes if r["date"] < split_date]
    holdout_ep = [r for r in episodes if r["date"] >= split_date]
    log.info(f"Train/holdout split at {split_date}: train n={len(train)} (episodes={len(train_ep)}), "
              f"holdout n={len(holdout)} (episodes={len(holdout_ep)})")

    report = {
        "experiment_id": EXPERIMENT_ID,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": GIT_COMMIT,
        "config": {
            "window_days": WINDOW, "forward_days": FORWARD_DAYS,
            "episode_cooldown_days": EPISODE_COOLDOWN_DAYS, "train_fraction": TRAIN_FRACTION,
            "params_used": params,
        },
        "universe_size": len(symbols),
        "data_date_range": [str(min_date), str(max_date)],
        "split_date": str(split_date),
        "n_observations": len(all_results),
        "n_episodes": len(episodes),

        "score_buckets_all_observations": bucket_stats(
            all_results, lambda r: score_bucket_label(r["score"]), buckets=[b[0] for b in SCORE_BUCKETS]),
        "score_buckets_episodes_only": bucket_stats(
            episodes, lambda r: score_bucket_label(r["score"]), buckets=[b[0] for b in SCORE_BUCKETS]),
        "score_buckets_realized_return": bucket_stats(
            all_results, lambda r: score_bucket_label(r["score"]), return_key="realized_return",
            buckets=[b[0] for b in SCORE_BUCKETS]),

        "threshold_sweep_train": threshold_sweep(train),
        "threshold_sweep_holdout": threshold_sweep(holdout),
        "threshold_sweep_train_realized": threshold_sweep(train, return_key="realized_return"),
        "threshold_sweep_holdout_realized": threshold_sweep(holdout, return_key="realized_return"),

        "regime_matched_by_raw_score": regime_raw_score_matched(all_results),
    }

    with open("/tmp/backtest_results_002.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info("Full results written to /tmp/backtest_results_002.json")

    print(f"\n=== Experiment {EXPERIMENT_ID} (commit {GIT_COMMIT[:8]}) ===")
    print(f"Universe: {len(symbols)} symbols | Data: {min_date} to {max_date} | Split at {split_date}")
    print(f"Observations: {len(all_results)} raw, {len(episodes)} independent episodes ({EPISODE_COOLDOWN_DAYS}d cooldown)")

    print("\n--- Score buckets: ALL OBSERVATIONS, raw 20d return (Experiment 001-comparable) ---")
    for label, s in report["score_buckets_all_observations"].items():
        print(f"  {label:>8}: N={s['n']:>6}  win={s['win_rate']:>5.1f}% (95% CI {s['win_rate_ci95']})  "
              f"avg_return={s['avg_return']:>+6.2f}%  vs_spy={s['avg_excess_vs_spy']:>+6.2f}%")

    print("\n--- Score buckets: EPISODES ONLY (deduped), raw 20d return ---")
    for label, s in report["score_buckets_episodes_only"].items():
        print(f"  {label:>8}: N={s['n']:>6}  win={s['win_rate']:>5.1f}% (95% CI {s['win_rate_ci95']})  "
              f"avg_return={s['avg_return']:>+6.2f}%  vs_spy={s['avg_excess_vs_spy']:>+6.2f}%")

    print("\n--- Score buckets: ALL OBSERVATIONS, STOP-LOSS-AWARE realized return ---")
    for label, s in report["score_buckets_realized_return"].items():
        print(f"  {label:>8}: N={s['n']:>6}  win={s['win_rate']:>5.1f}% (95% CI {s['win_rate_ci95']})  "
              f"avg_realized={s['avg_return']:>+6.2f}%  avg_mae={s['avg_mae']:>+6.2f}%  "
              f"avg_mfe={s['avg_mfe']:>+6.2f}%  stopped_out={s['stopped_out_pct']:>5.1f}%")

    print(f"\n--- Threshold sweep: TRAIN period (raw return) ---")
    for t, s in report["threshold_sweep_train"].items():
        print(f"  score>={t:>3}: N={s['n']:>6}  win={s['win_rate']:>5.1f}% (CI {s['win_rate_ci95']})  avg_return={s['avg_return']:>+6.2f}%")

    print(f"\n--- Threshold sweep: HOLDOUT period (out-of-sample, raw return) ---")
    for t, s in report["threshold_sweep_holdout"].items():
        print(f"  score>={t:>3}: N={s['n']:>6}  win={s['win_rate']:>5.1f}% (CI {s['win_rate_ci95']})  avg_return={s['avg_return']:>+6.2f}%")

    print(f"\n--- Threshold sweep: HOLDOUT period (out-of-sample, STOP-LOSS-AWARE realized return) ---")
    for t, s in report["threshold_sweep_holdout_realized"].items():
        print(f"  score>={t:>3}: N={s['n']:>6}  win={s['win_rate']:>5.1f}% (CI {s['win_rate_ci95']})  avg_realized={s['avg_return']:>+6.2f}%")

    print("\n--- Regime, matched within raw-score bands (tests whether regime adds info or just reshuffles survivors) ---")
    for raw_label, regimes in report["regime_matched_by_raw_score"].items():
        print(f"  raw_score {raw_label}:")
        for regime_label, s in regimes.items():
            print(f"    {regime_label:>14}: N={s['n']:>6}  win={s['win_rate']:>5.1f}% (CI {s['win_rate_ci95']})  avg_return={s['avg_return']:>+6.2f}%")


if __name__ == "__main__":
    main()

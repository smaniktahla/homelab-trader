#!/usr/bin/env python3
"""Experiment: does data-mining beat the baseline on our own data?

Reproduces the "key idea" methodology from a 2026-07-31 YouTube video
(Jeff Swanson / Build Alpha) on Larry Connors' 2-period RSI strategy, using
our own price_history instead of Build Alpha's ES futures data. See the
2026-07-31 DocMost note on this video for the full writeup.

Baseline (Connors, as published): long when close > SMA(200) and RSI(2) <
20, exit when RSI(2) > 70. No stop loss.

"Optimized" variant from the video: same RSI(2) < 20 entry, but restricted
to Mon/Tue/Wed entries, exit on close < EMA(200) instead of the RSI exit,
plus a 2*ATR(2) stop loss.

This is NOT a replication of Build Alpha's data-mining process -- we don't
have thousands of indicators to search, and doing that kind of broad sweep
on ~10 years of daily bars for one symbol would be a textbook overfitting
exercise (the video's own results are exactly that risk: thousands of
combinations tested against a single in-sample path, best one kept). This
script only checks whether the specific ruleset the video landed on holds
up on our data -- it does not re-run the search.

Not part of the recurring ingest loop. Run manually:
    docker exec invest-ingest python3 research/backtests/backtest_rsi2_key_idea.py
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_utils import save_backtest_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

EXPERIMENT_ID = "006_rsi2_key_idea"
GIT_COMMIT = os.environ.get("BACKTEST_GIT_COMMIT", "unknown")
SYMBOLS = ["SPY", "QQQ"]
STARTING_EQUITY = 100_000.0


def load_series(conn, symbol):
    df = pd.read_sql(
        "SELECT ts, open, high, low, close FROM price_history "
        "WHERE symbol = %s ORDER BY ts",
        conn, params=(symbol,),
    )
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    return df


def rsi(close, period=2):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Wilder smoothing, matches Connors' published RSI(2) definition
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df, period=2):
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def build_indicators(df):
    df = df.copy()
    df["rsi2"] = rsi(df["close"], 2)
    df["sma200"] = df["close"].rolling(200).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["atr2"] = atr(df, 2)
    df["dow"] = df["ts"].dt.dayofweek  # Monday=0
    return df


def run_trades(df, entry_fn, exit_fn, stop_mult=None):
    """Single-position, next-bar-open-style backtest: signals evaluated on
    bar i's close are acted on as if filled at bar i's close (matches
    Build Alpha's same-bar-close convention closely enough for a first-pass
    comparison; we're not modeling slippage/fills precisely here)."""
    trades = []
    in_position = False
    entry_price = entry_idx = stop_price = None

    for i in range(200, len(df)):  # need 200 bars warmed up for SMA/EMA
        row = df.iloc[i]
        if pd.isna(row["rsi2"]) or pd.isna(row["sma200"]):
            continue

        if not in_position:
            if entry_fn(df, i):
                in_position = True
                entry_price = row["close"]
                entry_idx = i
                if stop_mult is not None and not pd.isna(row["atr2"]):
                    stop_price = entry_price - stop_mult * row["atr2"]
                else:
                    stop_price = None
        else:
            stopped_out = stop_price is not None and row["low"] <= stop_price
            if stopped_out:
                exit_price = stop_price
            elif exit_fn(df, i):
                exit_price = row["close"]
            else:
                continue
            trades.append({
                "entry_date": str(df.iloc[entry_idx]["ts"].date()),
                "exit_date": str(row["ts"].date()),
                "entry_price": round(float(entry_price), 2),
                "exit_price": round(float(exit_price), 2),
                "return_pct": float((exit_price - entry_price) / entry_price),
                "bars_held": i - entry_idx,
                "stopped_out": bool(stopped_out),
            })
            in_position = False
    return trades


def equity_curve_stats(trades, starting_equity=STARTING_EQUITY):
    if not trades:
        return None
    equity = starting_equity
    peak = starting_equity
    max_dd = 0.0
    curve = []
    for t in trades:
        equity *= (1 + t["return_pct"])
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)
        curve.append(equity)

    net_profit = equity - starting_equity
    wins = [t for t in trades if t["return_pct"] > 0]
    win_pct = len(wins) / len(trades) * 100
    avg_profit_per_trade = sum(t["return_pct"] * starting_equity for t in trades) / len(trades)

    first_date = datetime.fromisoformat(trades[0]["entry_date"])
    last_date = datetime.fromisoformat(trades[-1]["exit_date"])
    years = max((last_date - first_date).days / 365.25, 0.01)
    cagr = ((equity / starting_equity) ** (1 / years) - 1) * 100

    return {
        "trades": len(trades),
        "net_profit": round(net_profit, 2),
        "max_drawdown": round(max_dd, 2),
        "profit_dd_ratio": round(net_profit / max_dd, 2) if max_dd > 0 else None,
        "win_pct": round(win_pct, 1),
        "avg_profit_per_trade": round(avg_profit_per_trade, 2),
        "cagr_pct": round(cagr, 2),
        "years": round(years, 2),
    }


def baseline_entry(df, i):
    row = df.iloc[i]
    return row["close"] > row["sma200"] and row["rsi2"] < 20


def baseline_exit(df, i):
    return df.iloc[i]["rsi2"] > 70


def optimized_entry(df, i):
    row = df.iloc[i]
    return row["dow"] in (0, 1, 2) and row["rsi2"] < 20  # Mon/Tue/Wed


def optimized_exit(df, i):
    return df.iloc[i]["close"] < df.iloc[i]["ema200"]


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    results = {}
    try:
        for symbol in SYMBOLS:
            df = load_series(conn, symbol)
            if len(df) < 250:
                log.warning("%s: only %d bars, skipping", symbol, len(df))
                continue
            df = build_indicators(df)

            baseline_trades = run_trades(df, baseline_entry, baseline_exit, stop_mult=None)
            optimized_trades = run_trades(df, optimized_entry, optimized_exit, stop_mult=2.0)

            results[symbol] = {
                "bars": len(df),
                "date_range": [str(df["ts"].min().date()), str(df["ts"].max().date())],
                "baseline": equity_curve_stats(baseline_trades),
                "optimized": equity_curve_stats(optimized_trades),
            }
            log.info("%s baseline: %s", symbol, results[symbol]["baseline"])
            log.info("%s optimized: %s", symbol, results[symbol]["optimized"])
    finally:
        conn.close()

    summary = (
        "Reproduced the video's RSI(2) baseline vs. 'key idea' optimized ruleset "
        f"(day-of-week entry filter, EMA200 exit, 2xATR2 stop) on {', '.join(SYMBOLS)} "
        "using our own price_history. Not a data-mining replication -- just checks "
        "whether the video's specific final ruleset holds up out of sample on our data."
    )
    save_backtest_result(EXPERIMENT_ID, GIT_COMMIT, results, summary=summary)
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()

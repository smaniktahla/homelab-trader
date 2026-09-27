"""
Exit mechanics shared by the research backtests: the live mean-reversion
exit rules replayed bar by bar, and a stop-aware exit walker.

"Live MR exits" are defined once here so every backtest that claims to use
them (ingest/research/backtests/backtest_exit_policy_replay.py, the
Phase 5a candidate backtest bridge in shared/candidate_backtest.py) replays
the same rules instead of drifting copies:

  - regime_deterioration: market overall == bear_fear (checked first, as
    the portfolio-level check runs before the per-symbol loop live)
  - thesis_complete: close >= SMA20 (BB midline)
  - overbought: sell-side score_signal() over the same gate a buy uses
  - time_stop: >= TIME_STOP_BARS bars since the signal bar
  - hard stop: stop_loss_pct below the fill, intraday (live OTO leg)

Indicators are computed with the live pure functions from shared/signals.py
over the same trailing windows compute_signals() feeds them (a 252-close
window for fetch_closes("1y"), a 24-bar window for ATR). Close-based exits
fill at the next open. Pure functions over in-memory bars -- nothing here
reads or writes the database.
"""

import bisect
import math

from signals import (
    ATR_PERIOD, RS_LOOKBACK_DAYS,
    compute_atr, compute_bollinger, compute_rsi, detect_regime, score_signal,
)

WINDOW = 252                    # live fetch_closes("1y")
WARMUP = 200                    # regime SMA200 needs this many closes
ATR_WINDOW = ATR_PERIOD + 10    # live _load_recent_ohlc_from_db(conn, sym, ATR_PERIOD + 10)
TIME_STOP_BARS = 20
REGIME_EXIT_OVERALL = "bear_fear"


class MarketContext:
    """Market-wide inputs the live signal reads: market_regime_history
    (overall, score_modifier) and SPY closes for relative strength."""

    def __init__(self, regime_dates, regime_vals, spy_dates, spy_closes):
        self.regime_dates = regime_dates    # ascending dates
        self.regime_vals = regime_vals      # [(overall, score_modifier), ...]
        self.spy_dates = spy_dates
        self.spy_closes = spy_closes

    def regime_asof(self, d):
        i = bisect.bisect_right(self.regime_dates, d) - 1
        return self.regime_vals[i] if i >= 0 else (None, None)

    def spy_close_asof(self, d, back):
        """SPY close `back` rows before the last row dated <= d (live reads
        the last N price_history rows for SPY -- row-aligned, not
        date-aligned)."""
        i = bisect.bisect_right(self.spy_dates, d) - 1 - back
        return self.spy_closes[i] if i >= 0 else None


def live_signal_features(s, t, p, ctx):
    """Everything compute_signals() derives for one symbol on one cycle,
    computed from bars[: t + 1] only. `s` is {"d","o","h","l","c"} lists."""
    c = s["c"]
    closes = c[max(0, t - WINDOW + 1): t + 1]
    rsi = compute_rsi(closes, p["rsi_period"])
    bb_upper, bb_middle, bb_lower, band_std = compute_bollinger(closes, p["bb_period"], p["bb_std"])
    regime = detect_regime(closes, p["regime_sma_fast"], p["regime_sma_slow"], p["regime_band"])
    rs_pct = None
    if t >= RS_LOOKBACK_DAYS:
        spy_now, spy_then = ctx.spy_close_asof(s["d"][t], 0), ctx.spy_close_asof(s["d"][t], RS_LOOKBACK_DAYS)
        if spy_now and spy_then:
            rs_pct = ((c[t] - c[t - RS_LOOKBACK_DAYS]) / c[t - RS_LOOKBACK_DAYS] * 100
                      - (spy_now - spy_then) / spy_then * 100)
    lo = max(0, t - ATR_WINDOW + 1)
    atr = compute_atr(list(zip(s["h"][lo:t + 1], s["l"][lo:t + 1], c[lo:t + 1])))
    return rsi, bb_upper, bb_middle, bb_lower, band_std, regime, rs_pct, atr


def live_cycle(s, t, p, ctx):
    """One live cycle for bar t: (buy_ok, close_exit_reason, atr).
    buy_ok/close_exit are False/None when the market regime is unknown for
    that date (no market_regime_history row at or before it)."""
    rsi, bbu, bbm, bbl, bstd, regime, rs_pct, atr = live_signal_features(s, t, p, ctx)
    overall, score_mod = ctx.regime_asof(s["d"][t])
    if overall is None:
        return False, None, atr
    gate = p["score_proposal_min"] + score_mod
    price = s["c"][t]
    buy, _ = score_signal(rsi, price, bbu, bbl, bstd, bbm, regime, "buy", p, rs_pct=rs_pct, atr=atr)
    sell, _ = score_signal(rsi, price, bbu, bbl, bstd, bbm, regime, "sell", p, rs_pct=rs_pct, atr=atr)
    buy_ok = buy >= p["score_log_min"] and buy >= gate
    if overall == REGIME_EXIT_OVERALL:
        close_exit = "regime_deterioration"
    elif bbm is not None and price >= bbm:
        close_exit = "thesis_complete"
    elif sell >= p["score_log_min"] and sell >= gate:
        close_exit = "overbought"
    else:
        close_exit = None
    return buy_ok, close_exit, atr


def _step(steps, mfe):
    val = None
    for frm, v in steps:
        if mfe >= frm:
            val = v
    return val


def simulate_exit(s, close_exit_at, atr_at, e, fill, policy, stop_loss_pct):
    """Walk from fill bar e until exit; None if still open at end of data.

    close_exit_at(t) -> reason | None is evaluated at bar t's close and
    fills at the next open. atr_at(t) is only read by an ATR-trail policy.

    Policy: {"arm_mfe": frac, "floor": [[mfe_from, lock]...] | None,
    "trail_atr": [[mfe_from, mult]...] | None}; an empty policy is the
    hard stop + close-based exits only. MFE is measured from the fill on
    bar highs. A candidate stop is recomputed after each close and is only
    active from the next bar, so a level raised by bar t's high can never
    fill on bar t. Stops fill at their level, or at the open on a gap
    through it. The effective stop is max(hard stop, candidate stop)."""
    o, h, l = s["o"], s["h"], s["l"]
    n = len(o)
    hard = fill * (1 - stop_loss_pct)
    cand = -math.inf
    hh = -math.inf
    pending = None
    arm = policy.get("arm_mfe")
    for t in range(e, n):
        if t > e and pending:
            return _exit(t, o[t], pending, hh, fill)
        stop = max(hard, cand)
        reason = "hard_stop" if stop == hard else "candidate_stop"
        if t > e and o[t] <= stop:
            return _exit(t, o[t], reason, hh, fill)
        if l[t] <= stop:
            return _exit(t, stop, reason, hh, fill)
        hh = max(hh, h[t])
        mfe = hh / fill - 1
        if arm is not None and mfe >= arm:
            if policy.get("floor"):
                lock = _step(policy["floor"], mfe)
                if lock is not None:
                    cand = max(cand, fill * (1 + lock * mfe))
            if policy.get("trail_atr"):
                atr = atr_at(t)
                mult = _step(policy["trail_atr"], mfe)
                if atr and mult is not None:
                    cand = max(cand, hh - mult * atr)
        reason_at_close = close_exit_at(t)
        if reason_at_close:
            pending = reason_at_close
        elif t - (e - 1) >= TIME_STOP_BARS:
            pending = "time_stop"
    return None


def _exit(t, price, reason, hh, fill):
    mfe_price = max(hh, price) if hh != -math.inf else price
    return {"x": t, "exit_price": price, "reason": reason, "mfe": mfe_price / fill - 1}

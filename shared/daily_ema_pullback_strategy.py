"""
Daily EMA Pullback + Momentum Swing Trading epic, H1 (8 EMA retest) --
see docs/daily-ema-pullback-investigation-reconciliation.md for the full
investigation this PR follows. First bounded PR out of that doc's H1-H10
progression: the baseline variant only, no trend/slope/structure filter --
the spec's own incremental language ("baseline -> trend filter -> slope
filter -> structure filter") means those layers are later, separate PRs,
not part of H1.

Hypothesis: after an established move, a daily close that pulls back to
within a volatility-relative distance of its own 8-period EMA and closes
bullish (close > open) on that same bar tends to continue -- confirmation-
entry variant B ("close-confirm": the pullback bar's own close is the
confirmation, not a separate future bar) per the epic spec's four
variants. Invalidation is the symmetric case: a close that breaks
meaningfully below the EMA (same distance threshold) invalidates the
pullback rather than confirming it.

"Meaningfully" is defined relative to shared/signals.py::compute_atr()
(Wilder's ATR, the same volatility-relative-threshold convention
structural_support_bounce_strategy.py already uses for its own "near a
zone" definition) rather than a fixed % or exact EMA touch -- the spec
explicitly warns against assuming EMA touches are exact-equality events.

Corporate-action adjustment (the investigation doc's decision #1): this
module's own load_adjusted_bars() scales raw OHLC by each bar's own
adjclose/close ratio (falling back to an unadjusted 1.0 factor when
adjclose is NULL, i.e. pre-migration rows) before handing bars to the
strategy -- shared/backtest_engine.py::load_bars() is deliberately left
untouched, since changing its behavior would silently alter every other
strategy's existing backtest results. Volume is not scaled -- this epic's
strategy is price-level only and does not consume volume; a future PR
touching volume-relative context (H6, RVOL) would need its own decision
here, not inherited from this one.

Per the investigation doc's decision #3 (hypothesis_types vs. Strategy
Incubator): this hypothesis registers through hypothesis_types/
hypothesis_candidates like every other strategy in this repo (needed for
strategy_registry.py/backtest visualization to dispatch it at all) AND is
intended to be registered as a strategy_version via
shared/strategy_lifecycle.py::register_candidate_as_strategy_version()
once a candidate is generated -- the two mechanisms answer different
questions (what the hypothesis IS vs. what stage this attempt is at), not
competing ones.
"""

from backtest_engine import Bar
from market_structure import ema
from signals import ATR_PERIOD, compute_atr

DEFAULT_EMA_PERIOD = 8
DEFAULT_ATR_PERIOD = ATR_PERIOD
DEFAULT_PROXIMITY_ATR_MULT = 0.5


def load_adjusted_bars(conn, symbol, start, end):
    """Split/dividend-adjusted OHLC bars for `symbol` in [start, end], via
    each row's own adjclose/close ratio -- rows with a NULL adjclose
    (pre-migration, or a symbol adjclose hasn't been backfilled for yet)
    fall back to an unadjusted factor of 1.0 rather than being dropped, so
    a partially-backfilled symbol degrades to "not adjusted for its older
    rows" instead of losing history outright. Volume is passed through
    unscaled -- see module docstring."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, ts, open, high, low, close, volume, adjclose
            FROM price_history
            WHERE symbol=%s AND ts >= %s AND ts <= %s
            ORDER BY ts ASC
        """, (symbol, start, end))
        rows = cur.fetchall()
    bars = []
    for r in rows:
        sym, ts, o, h, l, c, v, adjclose = r
        close = float(c)
        factor = float(adjclose) / close if (adjclose is not None and close != 0) else 1.0
        bars.append(Bar(
            symbol=sym, ts=ts,
            open=float(o) * factor, high=float(h) * factor, low=float(l) * factor, close=close * factor,
            volume=float(v) if v is not None else 0.0,
        ))
    return bars


def make_daily_8ema_pullback_strategy(ema_period=DEFAULT_EMA_PERIOD, atr_period=DEFAULT_ATR_PERIOD,
                                       proximity_atr_mult=DEFAULT_PROXIMITY_ATR_MULT):
    """Returns a Strategy callable matching shared/backtest_engine.py's
    Callable[[list[Bar]], str | None] interface -- pure function of
    bars_seen, same as every other strategy module (no DB access; the
    adjustment decision above happens at bar-loading time, not here).

    Entry ("buy"): today's close is within proximity_atr_mult*ATR of the
    EMA and today's own close > open (bullish pullback-and-bounce bar).
    Exit ("sell"): today's close is more than proximity_atr_mult*ATR BELOW
    the EMA (the pullback broke down instead of holding). Both directions
    use the same distance threshold -- a symmetric definition, not two
    independently-tuned numbers."""
    def strategy(bars_seen):
        if len(bars_seen) < max(ema_period, atr_period) + 2:
            return None
        closes = [b.close for b in bars_seen]
        ema_now = ema(closes, ema_period)
        if ema_now is None:
            return None
        ohlc = [(b.high, b.low, b.close) for b in bars_seen]
        atr = compute_atr(ohlc, atr_period)
        if atr is None or atr == 0:
            return None
        tolerance = proximity_atr_mult * atr
        close = bars_seen[-1].close
        open_ = bars_seen[-1].open
        if abs(close - ema_now) <= tolerance and close > open_:
            return "buy"
        if close < ema_now - tolerance:
            return "sell"
        return None
    return strategy

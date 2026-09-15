"""
SuperTrend strategy, PR 18 of the Hypothesis-Driven Trading Architecture
epic -- the one strategy left over from PR 16/17's own decomposition (see
shared/strategy_registry.py's docstring).

Hypothesis: an ATR-banded trend-following overlay (Wilder's ATR, the same
smoothing already used for stop-distance sizing in
shared/trade_thesis_stop_resolver.py and structural_support_bounce_strategy.py)
that only flips direction when price closes decisively through the
opposite band is a persistent trend signal -- the same family as
ema_crossover_trend (PR 16): a state-carrying two-series comparison the
current trade-thesis condition-tree grammar (shared/trade_thesis.py)
cannot express as "one feature vs a scalar." Registered in the Hypothesis
Library with default_entry_conditions/default_invalidation_spec = NULL,
same precedent ema_crossover_trend set -- real semantics live in the
catalog description prose, this module is the actual executable
implementation.

Unlike Bollinger/EMA (pure functions of a trailing window), SuperTrend's
final bands are recursive -- bar t's final band depends on bar t-1's final
band and close. This module recomputes the entire trend series from
bars_seen on every call rather than carrying state between calls,
preserving the same as-of-bar-t-safe guarantee (bars_seen is always
exactly what backtest_engine.py's Callable[[list[Bar]], str | None]
contract passed) at the cost of O(n) work per call instead of O(1). Same
acceptance already noted in strategy_registry.py for its own per-bar
overlay recomputation.
"""

DEFAULT_PERIOD = 10
DEFAULT_MULTIPLIER = 3.0


def _wilder_atr_series(highs, lows, closes, period):
    """Wilder's ATR at every bar from index `period` onward (None before
    that), aligned to highs/lows/closes. Same smoothing formula as
    signals.py::compute_atr, but returns the whole series -- SuperTrend's
    band recursion needs ATR at every bar, not just the latest."""
    n = len(closes)
    atr = [None] * n
    if n < period + 1:
        return atr
    trs = [None] * n
    for i in range(1, n):
        trs[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    atr[period] = sum(trs[1:period + 1]) / period
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + trs[i]) / period
    return atr


def _supertrend_bands(highs, lows, closes, period, multiplier):
    """Returns (trend, final_upper, final_lower), each a list aligned to
    closes' index (None before the first defined ATR). trend is 1
    (uptrend) / -1 (downtrend) / None (not yet determined), per the
    standard SuperTrend final-band recursion:

    final_upper[i] = basic_upper[i] if (basic_upper[i] < final_upper[i-1]
                     or close[i-1] > final_upper[i-1]) else final_upper[i-1]
    final_lower[i] = basic_lower[i] if (basic_lower[i] > final_lower[i-1]
                     or close[i-1] < final_lower[i-1]) else final_lower[i-1]
    trend[i] = 1 if close[i] > final_upper[i-1]
               else -1 if close[i] < final_lower[i-1]
               else trend[i-1]
    """
    n = len(closes)
    atr = _wilder_atr_series(highs, lows, closes, period)
    trend = [None] * n
    final_upper = [None] * n
    final_lower = [None] * n
    for i in range(n):
        if atr[i] is None:
            continue
        mid = (highs[i] + lows[i]) / 2
        basic_upper = mid + multiplier * atr[i]
        basic_lower = mid - multiplier * atr[i]
        prev = i - 1
        if prev < 0 or final_upper[prev] is None:
            # First bar with a defined ATR -- no prior final band to
            # recurse against yet, so seed directly from the basic bands.
            final_upper[i] = basic_upper
            final_lower[i] = basic_lower
            if closes[i] > basic_upper:
                trend[i] = 1
            elif closes[i] < basic_lower:
                trend[i] = -1
            continue
        final_upper[i] = (
            basic_upper if (basic_upper < final_upper[prev] or closes[prev] > final_upper[prev])
            else final_upper[prev]
        )
        final_lower[i] = (
            basic_lower if (basic_lower > final_lower[prev] or closes[prev] < final_lower[prev])
            else final_lower[prev]
        )
        if closes[i] > final_upper[prev]:
            trend[i] = 1
        elif closes[i] < final_lower[prev]:
            trend[i] = -1
        else:
            trend[i] = trend[prev]
    return trend, final_upper, final_lower


def _supertrend_series(highs, lows, closes, period, multiplier):
    """Trend-only view of _supertrend_bands(), for callers (the strategy
    below) that don't need the band values themselves."""
    trend, _, _ = _supertrend_bands(highs, lows, closes, period, multiplier)
    return trend


def make_supertrend_strategy(period=DEFAULT_PERIOD, multiplier=DEFAULT_MULTIPLIER):
    """Returns a Strategy callable matching shared/backtest_engine.py's
    Callable[[list[Bar]], str | None] interface. Entry ("buy") when the
    trend flips to up (from down or not-yet-determined) at the latest bar;
    exit ("sell") on the mirrored flip to down. Same non-refire property as
    ema_crossover_trend: a persistent trend has trend[-1] == trend[-2], so
    only the actual transition bar emits a signal."""
    def strategy(bars_seen):
        if len(bars_seen) < period + 2:
            return None
        highs = [b.high for b in bars_seen]
        lows = [b.low for b in bars_seen]
        closes = [b.close for b in bars_seen]
        trend = _supertrend_series(highs, lows, closes, period, multiplier)
        current, previous = trend[-1], trend[-2]
        if current is None or current == previous:
            return None
        return "buy" if current == 1 else "sell"
    return strategy

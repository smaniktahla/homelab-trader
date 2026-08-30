"""
EMA Crossover Trend strategy, PR 16 of the Hypothesis-Driven Trading
Architecture epic (built on PR 15's shared/backtest_engine.py).

Hypothesis: a faster moving average crossing above a slower moving average
indicates positive trend persistence. Registered in the Hypothesis Library
(PR 13) as 'ema_crossover_trend' -- but with default_entry_conditions =
NULL, since the current trade-thesis condition-tree grammar
(shared/trade_thesis.py) only expresses "one feature vs. a scalar," with
no primitive for "feature A vs. feature B" or "value at t vs. value at
t-1". A crossover cannot be honestly expressed in that grammar; the
catalog entry's `description` carries the real semantics in prose instead
of a misleading approximate condition tree. This module is the actual
executable implementation.

Reuses shared/market_structure.py::ema() as-is -- it is a pure function of
whatever `closes` list is passed (no hidden state), so "EMA as of bar t-1"
is simply ema(closes[:-1], period) and "EMA as of bar t" is
ema(closes, period). No new indicator primitive was needed.
"""

from market_structure import ema

DEFAULT_FAST_PERIOD = 20
DEFAULT_SLOW_PERIOD = 21


def make_ema_crossover_strategy(fast_period=DEFAULT_FAST_PERIOD, slow_period=DEFAULT_SLOW_PERIOD):
    """Returns a Strategy callable matching shared/backtest_engine.py's
    Callable[[list[Bar]], str | None] interface. Entry when the fast EMA
    crosses above the slow EMA (fast_prev <= slow_prev AND fast_now >
    slow_now); exit on the mirrored crossunder. The <=/>= vs. >/< asymmetry
    is deliberate: a persistent "fast stays above slow" state has
    fast_prev > slow_prev (strict), so the entry condition is false and it
    does not refire -- only the actual transition bar fires a signal."""
    def strategy(bars_seen):
        closes = [b.close for b in bars_seen]
        if len(closes) < slow_period + 1:
            return None
        fast_now, slow_now = ema(closes, fast_period), ema(closes, slow_period)
        fast_prev, slow_prev = ema(closes[:-1], fast_period), ema(closes[:-1], slow_period)
        if None in (fast_now, slow_now, fast_prev, slow_prev):
            return None
        if fast_prev <= slow_prev and fast_now > slow_now:
            return "buy"
        if fast_prev >= slow_prev and fast_now < slow_now:
            return "sell"
        return None
    return strategy

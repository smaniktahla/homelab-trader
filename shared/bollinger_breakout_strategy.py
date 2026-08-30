"""
Bollinger Breakout Continuation strategy, PR 16 of the Hypothesis-Driven
Trading Architecture epic (built on PR 15's shared/backtest_engine.py).

Hypothesis: a close above the upper Bollinger Band indicates volatility
expansion and positive momentum more likely to continue than immediately
revert. Registered in the Hypothesis Library (PR 13) as
'bollinger_breakout_continuation' -- see ingest/schema.sql's seed data for
the metadata/catalog entry; this module is the actual executable
implementation, kept separate per that PR's own design (hypothesis_types
is documentation/metadata, not wired into the backtest engine).

Reuses shared/signals.py::compute_bollinger() as-is -- it is a pure
function of whatever `closes` list is passed (only the trailing `period`
elements ending at the list's last entry matter), so calling it on
bars_seen's closes each call is naturally as-of-bar-t-safe. No new
indicator primitive was needed for this strategy.
"""

from signals import compute_bollinger

DEFAULT_PERIOD = 20
DEFAULT_NUM_STD = 2.0


def make_bollinger_breakout_strategy(period=DEFAULT_PERIOD, num_std=DEFAULT_NUM_STD):
    """Returns a Strategy callable matching shared/backtest_engine.py's
    Callable[[list[Bar]], str | None] interface: entry when close[t] >
    upper_band[t], exit when close[t] < lower_band[t]."""
    def strategy(bars_seen):
        closes = [b.close for b in bars_seen]
        upper, sma, lower, std = compute_bollinger(closes, period, num_std)
        if upper is None:
            return None
        close = closes[-1]
        if close > upper:
            return "buy"
        if close < lower:
            return "sell"
        return None
    return strategy

"""
Volume Metrics, PR A of the Volume & Volume Profile epic. Pure, stateless
calculation functions -- same shape as shared/signals.py's
compute_bollinger()/compute_rsi(): plain list-in, value-out, a
self-inclusive trailing window ending at the list's last element, None on
insufficient data. No signal_params coupling at this layer, same as those
functions -- any live-config wiring is a caller-side concern for a later
PR, not this one. Nothing here is persisted; values are computed on
demand, matching compute_bollinger()'s existing pattern.

Workstream A2 of the epic. Deliberately does not implement Volume Profile
(POC/VAH/VAL, PR C) or any hypothesis-research logic (PR E/F) -- this
module is raw, reusable feature calculations only.
"""


def dollar_volume(close, volume):
    """close * volume, or None if either input is None."""
    if close is None or volume is None:
        return None
    return close * volume


def volume_sma(volumes, period):
    """Trailing simple moving average of the last `period` volumes,
    ending at volumes[-1]. None if len(volumes) < period."""
    period = int(period)
    if len(volumes) < period:
        return None
    window = volumes[-period:]
    return sum(window) / period


def relative_volume(volumes, period):
    """volumes[-1] / volume_sma(volumes, period) -- self-inclusive
    convention (matches compute_bollinger()'s own window), so rvol == 1.0
    exactly when volume sits at its own trailing average. None if the SMA
    is None or zero."""
    sma = volume_sma(volumes, period)
    if sma is None or sma == 0:
        return None
    return volumes[-1] / sma


def volume_zscore(volumes, period):
    """(volumes[-1] - mean) / std over the trailing `period`-window ending
    at volumes[-1]. None if insufficient data or std == 0 (no division by
    zero)."""
    period = int(period)
    if len(volumes) < period:
        return None
    window = volumes[-period:]
    mean = sum(window) / period
    variance = sum((v - mean) ** 2 for v in window) / period
    std = variance ** 0.5
    if std == 0:
        return None
    return (volumes[-1] - mean) / std


def volume_percentile(volumes, period):
    """Fractional rank (0.0-1.0) of volumes[-1] within the trailing
    `period`-window ending at volumes[-1] -- the fraction of window values
    volumes[-1] is >= to. Matches this repo's existing 0-1 fractional
    convention (e.g. backtest_engine.BacktestResult.win_rate), not a
    0-100 scale. None if len(volumes) < period."""
    period = int(period)
    if len(volumes) < period:
        return None
    window = volumes[-period:]
    latest = window[-1]
    at_or_below = sum(1 for v in window if v <= latest)
    return at_or_below / period

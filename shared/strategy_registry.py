"""
Strategy Registry, PR 17 of the Hypothesis-Driven Trading Architecture
epic. Maps a strategy key to its shared/backtest_engine.py-compatible
factory function and its indicator-overlay computation function, so
api/main.py's visualization endpoint stays a thin dispatcher rather than
hardcoding per-strategy branches -- and so a later PR (SuperTrend, PR 18)
registers a new entry here without touching the API layer's routing logic.

Overlay computation reuses the exact per-bar-loop pattern api/main.py's
existing GET /api/prices/{symbol}?include_bb=true endpoint already uses in
production for Bollinger bands (main.py:176-181) -- compute the indicator
fresh against bars[:i+1] for every bar i, same as the backtest engine
itself does for strategy decisions, so the chart's overlay lines are
computed the same as-of-bar-t-safe way the strategy sees them. Not
optimized for large bar counts -- acceptable at the scale a single
on-demand visualization request runs at.
"""

from bollinger_breakout_strategy import DEFAULT_NUM_STD, DEFAULT_PERIOD, make_bollinger_breakout_strategy
from ema_crossover_strategy import DEFAULT_FAST_PERIOD, DEFAULT_SLOW_PERIOD, make_ema_crossover_strategy
from market_structure import ema
from signals import compute_bollinger


def _line_series(name, bars, values):
    return {
        "name": name,
        "kind": "line",
        "values": [{"ts": b.ts.isoformat(), "value": v} for b, v in zip(bars, values)],
    }


def _bollinger_overlays(bars, period=DEFAULT_PERIOD, num_std=DEFAULT_NUM_STD):
    closes = [b.close for b in bars]
    upper, middle, lower = [], [], []
    for i in range(len(closes)):
        u, m, l, _ = compute_bollinger(closes[: i + 1], period, num_std)
        upper.append(u)
        middle.append(m)
        lower.append(l)
    return [
        _line_series("bb_upper", bars, upper),
        _line_series("bb_middle", bars, middle),
        _line_series("bb_lower", bars, lower),
    ]


def _ema_crossover_overlays(bars, fast_period=DEFAULT_FAST_PERIOD, slow_period=DEFAULT_SLOW_PERIOD):
    closes = [b.close for b in bars]
    fast, slow = [], []
    for i in range(len(closes)):
        fast.append(ema(closes[: i + 1], fast_period))
        slow.append(ema(closes[: i + 1], slow_period))
    return [
        _line_series(f"ema_{fast_period}", bars, fast),
        _line_series(f"ema_{slow_period}", bars, slow),
    ]


STRATEGIES = {
    "bollinger_breakout_continuation": {
        "display_name": "Bollinger Breakout Continuation",
        "make_strategy": make_bollinger_breakout_strategy,
        "compute_overlays": _bollinger_overlays,
    },
    "ema_crossover_trend": {
        "display_name": "EMA Crossover Trend",
        "make_strategy": make_ema_crossover_strategy,
        "compute_overlays": _ema_crossover_overlays,
    },
}

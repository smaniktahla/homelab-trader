"""
Series form of shared/feature_registry.py's features, for backtesting
condition trees (Hypothesis-Driven Phase 5a-1, see
docs/hypothesis-driven-phase5a-scoping.md).

feature_registry's eval_fns answer "what is feature F for symbol S as of
date D" with one database read per call -- correct for live checks, far
too slow for evaluating a tree on every bar of every symbol. Each builder
here takes a symbol's bars (and, where needed, its structural events or the
market regime history) once and returns one value per bar, where value[t]
is exactly what the live eval_fn returns for as_of = dates[t]. Same math:
the live eval_fns wrap compute_rsi/compute_bollinger over closes[: t + 1],
and so does this module -- RSI via the same Wilder recursion carried
forward bar by bar instead of recomputed from scratch (identical float
operations in identical order), Bollinger via compute_bollinger itself.
tests/test_feature_series.py checks parity against evaluate_feature() on a
database fixture, so the two cannot drift silently.

Only features with an as-of-safe historical source are supported; anything
else raises UnsupportedFeature rather than evaluating to None/False:
  - structural_zones.*: zones must be re-clustered from swings as of every
    date (feature_registry's lookahead guard) -- deferred, too expensive
  - market_structure.trend_state: market_structure_history only starts
    2026-08, so it has no backtestable history

Callers pass split/dividend-adjusted closes for backtests; live
feature_registry reads raw closes. That discrepancy is pre-existing and
deliberate here (a split would otherwise fire false oversold readings).
"""

import bisect

from signals import compute_bollinger
from feature_registry import BB_PERIOD, BB_STD, RSI_PERIOD

SUPPORTED_FEATURES = frozenset({
    "technical.close",
    "technical.rsi_14",
    "technical.bb_pct_b",
    "market_regime.overall",
    "structural_events.recent_event_type",
    "structural_events.bars_since_last_breakout",
})

BREAKOUT_EVENT_TYPES = ("breakout", "breakdown")


class UnsupportedFeature(ValueError):
    pass


def rsi_series(closes, period=RSI_PERIOD):
    """value[t] == signals.compute_rsi(closes[: t + 1], period)."""
    period = int(period)
    n = len(closes)
    out = [None] * n
    gains, losses = [], []
    avg_gain = avg_loss = None
    for t in range(1, n):
        delta = closes[t] - closes[t - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
        k = len(gains)  # == t
        if k == period:
            avg_gain = sum(gains[:period]) / period
            avg_loss = sum(losses[:period]) / period
        elif k > period:
            avg_gain = (avg_gain * (period - 1) + gains[-1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[-1]) / period
        # compute_rsi returns None until len(closes) >= period + 2
        if t + 1 >= period + 2:
            if avg_loss == 0:
                out[t] = 100.0
            else:
                rs = avg_gain / avg_loss
                out[t] = 100.0 - (100.0 / (1.0 + rs))
    return out


def bb_pct_b_series(closes, period=BB_PERIOD, num_std=BB_STD):
    """value[t] == feature_registry._eval_bb_pct_b at dates[t]."""
    period = int(period)
    out = [None] * len(closes)
    for t in range(period - 1, len(closes)):
        window = closes[t - period + 1: t + 1]
        upper, _, lower, _ = compute_bollinger(window, period, num_std)
        if upper is None or upper == lower:
            continue
        out[t] = (window[-1] - lower) / (upper - lower)
    return out


def market_regime_series(dates, regime_dates, regime_overall):
    """value[t] = market_regime_history.overall for the last trading_date <= dates[t]."""
    out = []
    for d in dates:
        i = bisect.bisect_right(regime_dates, d) - 1
        out.append(regime_overall[i] if i >= 0 else None)
    return out


def recent_event_type_series(dates, events):
    """events: [(confirmation_date, id, event_type), ...] for one symbol,
    timeframe 'daily'. value[t] = event_type of the latest event by
    (confirmation_time, id) with confirmation_time <= dates[t]."""
    ordered = sorted(events, key=lambda e: (e[0], e[1]))
    out = []
    j = -1
    for d in dates:
        while j + 1 < len(ordered) and ordered[j + 1][0] <= d:
            j += 1
        out.append(ordered[j][2] if j >= 0 else None)
    return out


def bars_since_last_breakout_series(dates, events):
    """value[t] = t - (index of the last bar dated <= the latest
    breakout/breakdown confirmation_time <= dates[t])."""
    confs = sorted(e[0] for e in events if e[2] in BREAKOUT_EVENT_TYPES)
    out = []
    j = -1
    for t, d in enumerate(dates):
        while j + 1 < len(confs) and confs[j + 1] <= d:
            j += 1
        if j < 0:
            out.append(None)
            continue
        event_idx = bisect.bisect_right(dates, confs[j]) - 1
        out.append(t - event_idx if event_idx >= 0 else None)
    return out


def check_supported(features):
    unsupported = sorted(set(features) - SUPPORTED_FEATURES)
    if unsupported:
        raise UnsupportedFeature(f"no backtestable series for {unsupported}")


def build_feature_series(features, dates, closes, *, regime=None, events=None):
    """{feature_id: [value per bar]} for the requested features.
    regime: (regime_dates, regime_overall); events: see recent_event_type_series."""
    check_supported(features)
    out = {}
    for f in features:
        if f == "technical.close":
            out[f] = list(closes)
        elif f == "technical.rsi_14":
            out[f] = rsi_series(closes)
        elif f == "technical.bb_pct_b":
            out[f] = bb_pct_b_series(closes)
        elif f == "market_regime.overall":
            out[f] = market_regime_series(dates, *regime)
        elif f == "structural_events.recent_event_type":
            out[f] = recent_event_type_series(dates, events or [])
        elif f == "structural_events.bars_since_last_breakout":
            out[f] = bars_since_last_breakout_series(dates, events or [])
    return out

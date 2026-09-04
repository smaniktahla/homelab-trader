"""
Structural Support Bounce strategy, Price Structure epic PR G (built on
PR 15's shared/backtest_engine.py and PR C's feature_registry
structural_zones provider).

Hypothesis: price trading within a tight distance of a confirmed support
zone (structural_zones.nearest_support_distance_atr, an as-of-safe
recompute from confirmed swings -- see PR C's module docstring for why
this is NOT a read of the mutable structural_zones table) is more likely
to find support and bounce than continue falling. Registered in the
Hypothesis Library (PR 13) as 'structural_support_bounce' -- see
ingest/schema.sql's seed data for the metadata/catalog entry; this module
is the actual executable implementation, same "catalog is documentation,
this module is the engine" split PR 16 established for
bollinger_breakout_strategy.py.

Unlike PR 16's Bollinger/EMA strategies (pure functions of bars_seen
alone), this hypothesis's signal is DB-backed -- feature_registry's
structural_zones features need a live connection to recompute zone
clustering from structural_swings. make_structural_support_bounce_strategy
closes over that connection at construction time, same factory-function
shape PR 16 used, just parameterized by conn instead of a lookback/std.

Two other hypotheses from this same PR --
'structural_breakout_momentum' (structural_events.recent_event_type ==
'breakout') and 'fvg_reaction_momentum' (recent_event_type ==
'fvg_midpoint_reached', reusing the same feature since FVG lifecycle
milestones flow through structural_events too, PR B2) -- are registered
in the catalog but do not yet have an executable strategy module of
their own, same "template registered, implementation trails" precedent
PR 16 set for ema_crossover_trend. Building those out, and actually
running any of these three against real historical data to assess
whether an edge exists, is deliberately left for a follow-up rather than
attempted here -- see this PR's description.
"""

import feature_registry

DEFAULT_ENTRY_THRESHOLD_ATR = 0.5
DEFAULT_INVALIDATION_THRESHOLD_ATR = 2.0


def make_structural_support_bounce_strategy(conn, entry_threshold_atr=DEFAULT_ENTRY_THRESHOLD_ATR,
                                             invalidation_threshold_atr=DEFAULT_INVALIDATION_THRESHOLD_ATR):
    """Returns a Strategy callable matching shared/backtest_engine.py's
    Callable[[list[Bar]], str | None] interface: entry ("buy") when the
    latest bar's close is within entry_threshold_atr of a confirmed
    support zone below it; exit ("sell") once price has moved back out to
    invalidation_threshold_atr or beyond (the bounce setup no longer
    applies). as_of is always the strategy's own last-seen bar's own
    date -- never "today" -- so this stays correctly as-of-safe when
    replayed historically, the same guarantee feature_registry's
    structural_zones provider itself already gives (PR C)."""
    def strategy(bars_seen):
        last = bars_seen[-1]
        symbol, as_of = last.symbol, last.ts.date()
        dist = feature_registry.evaluate_feature(
            conn, "structural_zones.nearest_support_distance_atr", symbol, as_of)
        if dist is None:
            return None
        if dist < entry_threshold_atr:
            return "buy"
        if dist > invalidation_threshold_atr:
            return "sell"
        return None
    return strategy

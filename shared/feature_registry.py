"""
Feature Registry, PR 2 of the Hypothesis-Driven Trading Architecture epic
-- see docs/trade-thesis-architecture-reconciliation.md §5's PR 2 bullet.
A metadata/code layer describing the evidence a trade thesis (PR 1,
shared/trade_thesis.py) can reference -- it does not modify symbol_features
or any other provider table, which all remain pure observation storage.

Two separate vocabularies, both scoped by provider:

- PROVIDERS answers "which evidence_context.providers key is legal, and
  what table/version field does it point at" (§1b). One entry per
  provider table trade_theses.evidence_context can cite:
  symbol_features, market_structure_history, market_regime_history,
  sector_regime_history, security_regime_history, structural_swings
  (behind the "structural_zones" provider id -- see its ProviderSpec),
  structural_events (Price Structure epic PR C, added alongside PR A/B's
  structural_swings/structural_zones/structural_events tables).
- FEATURES answers "which 'feature' identifier is legal inside an
  entry_conditions/invalidation_spec/success_spec leaf" (deferred by PR 1
  per its own §6/§7 -- grammar shape only, no vocabulary). Namespaced
  "<provider>.<name>" (a naming scheme this PR introduces, since PR 1
  deliberately left it open) so a feature's provider -- and therefore
  which evidence_context entry justifies it -- is never ambiguous.

Feature vocabulary in this PR is deliberately minimal: only what's needed
to port mean_reversion's actual entry logic (RSI, %B, close, market
regime), same "minimal placeholder set" precedent PR 1 used for
hypothesis_type -- not an attempt to enumerate every future strategy's
inputs now.

Every eval_fn is (conn, symbol, as_of: date) -> value | None, as-of-safe by
construction: it reuses shared/regime_common.py's load_daily_series() +
asof_index() (the same "last date <= target_date" convention every other
regime module already uses) or an equivalent bounded query, so a feature
can never see a bar/row dated after `as_of`. eval_fn wraps existing pure
math (shared/signals.py's compute_rsi/compute_bollinger) rather than
reimplementing indicator calculation a second time.

One deliberate exception to "just query the table with a date filter":
the structural_zones provider's features never read shared/
market_structure.py's structural_zones table directly, because that
table is mutable current-state (each cron cycle recomputes a zone's
center/bounds from ALL swings confirmed by today, not by any particular
historical as_of). Reading it for an arbitrary past as_of would leak
later zone refinement backward -- exactly the lookahead risk PR A fixed
for individual swings. Those eval_fns instead recompute zone clustering
fresh from structural_swings (which does carry a real confirmation_time
per row) filtered to as_of, reusing market_structure.py's own
_cluster_zones/_atr_series primitives. structural_events' features don't
have this problem -- that table is genuinely append-only with its own
confirmation_time, so a direct filtered read is as-of-safe as-is.

Nothing in this module is called from any live path yet -- registering and
evaluating a feature is possible and tested, but no signal-generation or
proposal code calls evaluate_feature() in this PR. That wiring, plus the
semantic checks (does a trade_theses row's condition tree only reference
registered features, is data actually available as-of its date) is PR 3's
job. This module only describes what exists; it does not enforce anything
against a trade_theses row.
"""

import logging
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime
from typing import Callable, Optional

from regime_common import asof_index, load_daily_series, load_daily_ohlc
from signals import compute_bollinger, compute_rsi
from market_structure import _cluster_zones, _atr_series, SR_ATR_MULT

log = logging.getLogger(__name__)

RSI_PERIOD = 14
BB_PERIOD = 20
BB_STD = 2.0


@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    source_table: str
    version_field: Optional[str]  # None if the table has no per-row version lineage (e.g. market_regime_history)
    description: str


@dataclass(frozen=True)
class FeatureSpec:
    feature_id: str  # "<provider_id>.<name>"
    provider_id: str
    output_type: str  # "numeric" | "text"
    description: str
    eval_fn: Callable[[object, str, date_cls], object]


def _as_date(as_of):
    """Accepts date or datetime (evidence_context.as_of round-trips as an
    ISO string elsewhere, so callers may hand either); truncates to a date
    since every provider table here is keyed at day granularity."""
    if isinstance(as_of, datetime):
        return as_of.date()
    return as_of


def _eval_close(conn, symbol, as_of):
    dates, closes = load_daily_series(conn, symbol)
    idx = asof_index(dates, _as_date(as_of))
    return closes[idx] if idx is not None else None


def _eval_rsi_14(conn, symbol, as_of):
    dates, closes = load_daily_series(conn, symbol)
    idx = asof_index(dates, _as_date(as_of))
    if idx is None:
        return None
    return compute_rsi(closes[: idx + 1], RSI_PERIOD)


def _eval_bb_pct_b(conn, symbol, as_of):
    """%B = (close - lower_band) / (upper_band - lower_band). Standard
    Bollinger-Band position indicator: 0 = at the lower band, 1 = at the
    upper band, matches the "bb_dist"-style comparisons score_signal()
    already uses internally in shared/signals.py."""
    dates, closes = load_daily_series(conn, symbol)
    idx = asof_index(dates, _as_date(as_of))
    if idx is None:
        return None
    window = closes[: idx + 1]
    upper, _, lower, _ = compute_bollinger(window, BB_PERIOD, BB_STD)
    if upper is None or upper == lower:
        return None
    return (window[-1] - lower) / (upper - lower)


def _eval_market_regime_overall(conn, symbol, as_of):
    """market_regime_history is market-wide, not per-symbol -- `symbol` is
    accepted (and ignored) only so every eval_fn shares one call signature."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT overall FROM market_regime_history
            WHERE trading_date <= %s
            ORDER BY trading_date DESC LIMIT 1
            """,
            (_as_date(as_of),),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _eval_market_structure_trend_state(conn, symbol, as_of):
    """market_structure_history is already a proper daily snapshot table
    (one row per symbol per trading_date, never mutated after the day it
    was written) -- a direct trading_date <= as_of read is as-of-safe on
    its own, unlike structural_zones below."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trend FROM market_structure_history
            WHERE symbol=%s AND trading_date <= %s
            ORDER BY trading_date DESC LIMIT 1
            """,
            (symbol, _as_date(as_of)),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _load_confirmed_swings_asof(conn, symbol, timeframe, swing_type, as_of):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_time, price FROM structural_swings
            WHERE symbol=%s AND timeframe=%s AND swing_type=%s AND confirmation_time <= %s
            ORDER BY event_time ASC
            """,
            (symbol, timeframe, swing_type, _as_date(as_of)),
        )
        rows = cur.fetchall()
    return [{"date": r[0], "price": float(r[1])} for r in rows]


def _nearest_zone_distance_atr(conn, symbol, as_of, direction):
    """Distance (in ATR units) from as-of price to the nearest support
    ("below") or resistance ("above") zone, recomputed fresh from
    structural_swings confirmed as of as_of -- deliberately NOT a read of
    the structural_zones table, which only holds today's final, mutable
    zone state (see PR C's design note: a zone's center/bounds get
    refined by touches confirmed after as_of, so reading that table
    directly for a historical as_of would leak future refinement
    backward, the same lookahead risk PR A fixed for swings themselves).
    timeframe is fixed to "daily" -- the only timeframe with enough real
    depth for this to be meaningful today; weekly/monthly can be added
    once there's history to back them."""
    dates, closes = load_daily_series(conn, symbol)
    idx = asof_index(dates, _as_date(as_of))
    if idx is None:
        return None
    price = closes[idx]

    try:
        daily_ohlc = load_daily_ohlc(conn, symbol)
    except Exception:
        daily_ohlc = []
    ohlc_dates = [b[0] for b in daily_ohlc]
    ohlc_idx = asof_index(ohlc_dates, _as_date(as_of))
    ohlc_asof = daily_ohlc[: ohlc_idx + 1] if ohlc_idx is not None else []
    atr_series = _atr_series(ohlc_asof)
    atr = atr_series[-1] if atr_series else None
    tolerance = (atr or 0) * SR_ATR_MULT
    if tolerance <= 0:
        tolerance = price * 0.005

    swing_type = "low" if direction == "below" else "high"
    points = _load_confirmed_swings_asof(conn, symbol, "daily", swing_type, as_of)
    zones = _cluster_zones(points, tolerance)
    candidates = [z for z in zones if (z["price"] < price if direction == "below" else z["price"] > price)]
    if not candidates or not atr:
        return None
    nearest = max(candidates, key=lambda z: z["price"]) if direction == "below" else min(candidates, key=lambda z: z["price"])
    return abs(price - nearest["price"]) / atr


def _eval_nearest_support_distance_atr(conn, symbol, as_of):
    return _nearest_zone_distance_atr(conn, symbol, as_of, "below")


def _eval_nearest_resistance_distance_atr(conn, symbol, as_of):
    return _nearest_zone_distance_atr(conn, symbol, as_of, "above")


def _eval_recent_event_type(conn, symbol, as_of):
    """structural_events carries its own confirmation_time -- a direct
    filtered read is as-of-safe as-is, unlike zones above."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_type FROM structural_events
            WHERE symbol=%s AND timeframe='daily' AND confirmation_time <= %s
            ORDER BY confirmation_time DESC, id DESC LIMIT 1
            """,
            (symbol, _as_date(as_of)),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _eval_bars_since_last_breakout(conn, symbol, as_of):
    dates, _closes = load_daily_series(conn, symbol)
    idx = asof_index(dates, _as_date(as_of))
    if idx is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT confirmation_time FROM structural_events
            WHERE symbol=%s AND timeframe='daily' AND event_type IN ('breakout', 'breakdown')
              AND confirmation_time <= %s
            ORDER BY confirmation_time DESC LIMIT 1
            """,
            (symbol, _as_date(as_of)),
        )
        row = cur.fetchone()
    if row is None:
        return None
    event_idx = asof_index(dates, row[0])
    if event_idx is None:
        return None
    return idx - event_idx


PROVIDERS = {
    "technical": ProviderSpec(
        provider_id="technical",
        source_table="symbol_features",
        version_field="feature_version",
        description="Technical/composite scoring snapshots (shared/feature_store.py).",
    ),
    "market_structure": ProviderSpec(
        provider_id="market_structure",
        source_table="market_structure_history",
        version_field="calculation_version",
        description="Top-down swing/trend/BOS-CHoCH structure (shared/market_structure.py).",
    ),
    "market_regime": ProviderSpec(
        provider_id="market_regime",
        source_table="market_regime_history",
        version_field=None,
        description="Market-wide trend/VIX regime, one row per trading day (shared/market_regime_history.py).",
    ),
    "sector_regime": ProviderSpec(
        provider_id="sector_regime",
        source_table="sector_regime_history",
        version_field="calculation_version",
        description="Sector-level trend/relative-strength/breadth regime (shared/sector_regime.py).",
    ),
    "security_regime": ProviderSpec(
        provider_id="security_regime",
        source_table="security_regime_history",
        version_field="calculation_version",
        description="Stock-level trend/relative-strength regime (shared/security_regime.py).",
    ),
    "structural_zones": ProviderSpec(
        provider_id="structural_zones",
        source_table="structural_swings",
        version_field=None,
        # source_table intentionally points at structural_swings, not
        # structural_zones -- every eval_fn under this provider recomputes
        # zone clustering fresh from confirmed swings as of the query
        # date rather than reading structural_zones' mutable current-state
        # rows (see _nearest_zone_distance_atr's docstring).
        description="Support/resistance zone proximity, recomputed as-of-safe from confirmed swings (Price Structure epic PR A/C).",
    ),
    "structural_events": ProviderSpec(
        provider_id="structural_events",
        source_table="structural_events",
        version_field="calculation_version",
        description="Append-only structural event log: breakout/breakdown/acceptance/rejection/sweep/structure-failure (Price Structure epic PR B).",
    ),
}

FEATURES = {
    "technical.close": FeatureSpec(
        feature_id="technical.close",
        provider_id="technical",
        output_type="numeric",
        description="Daily close price as-of the given date.",
        eval_fn=_eval_close,
    ),
    "technical.rsi_14": FeatureSpec(
        feature_id="technical.rsi_14",
        provider_id="technical",
        output_type="numeric",
        description="14-period RSI as-of the given date (shared/signals.py::compute_rsi).",
        eval_fn=_eval_rsi_14,
    ),
    "technical.bb_pct_b": FeatureSpec(
        feature_id="technical.bb_pct_b",
        provider_id="technical",
        output_type="numeric",
        description="Bollinger %B (20-period, 2 std) as-of the given date.",
        eval_fn=_eval_bb_pct_b,
    ),
    "market_regime.overall": FeatureSpec(
        feature_id="market_regime.overall",
        provider_id="market_regime",
        output_type="text",
        description="market_regime_history.overall as-of the given date (last trading_date <= as_of).",
        eval_fn=_eval_market_regime_overall,
    ),
    "market_structure.trend_state": FeatureSpec(
        feature_id="market_structure.trend_state",
        provider_id="market_structure",
        output_type="text",
        description="market_structure_history.trend as-of the given date (last trading_date <= as_of).",
        eval_fn=_eval_market_structure_trend_state,
    ),
    "structural_zones.nearest_support_distance_atr": FeatureSpec(
        feature_id="structural_zones.nearest_support_distance_atr",
        provider_id="structural_zones",
        output_type="numeric",
        description="Distance from as-of close to the nearest support zone below it, in ATR units. "
                     "Recomputed from confirmed daily swings as of the given date, not the live zone table.",
        eval_fn=_eval_nearest_support_distance_atr,
    ),
    "structural_zones.nearest_resistance_distance_atr": FeatureSpec(
        feature_id="structural_zones.nearest_resistance_distance_atr",
        provider_id="structural_zones",
        output_type="numeric",
        description="Distance from as-of close to the nearest resistance zone above it, in ATR units. "
                     "Recomputed from confirmed daily swings as of the given date, not the live zone table.",
        eval_fn=_eval_nearest_resistance_distance_atr,
    ),
    "structural_events.recent_event_type": FeatureSpec(
        feature_id="structural_events.recent_event_type",
        provider_id="structural_events",
        output_type="text",
        description="event_type of the most recently confirmed daily structural event as-of the given date.",
        eval_fn=_eval_recent_event_type,
    ),
    "structural_events.bars_since_last_breakout": FeatureSpec(
        feature_id="structural_events.bars_since_last_breakout",
        provider_id="structural_events",
        output_type="numeric",
        description="Trading days elapsed since the last confirmed daily breakout or breakdown event, as-of the given date.",
        eval_fn=_eval_bars_since_last_breakout,
    ),
}


def is_legal_provider(provider_id):
    return provider_id in PROVIDERS


def is_legal_feature(feature_id):
    return feature_id in FEATURES


def get_provider(provider_id):
    return PROVIDERS.get(provider_id)


def get_feature(feature_id):
    return FEATURES.get(feature_id)


def evaluate_feature(conn, feature_id, symbol, as_of):
    """Best-effort, fail-open evaluation of one registered feature,
    matching shared/feature_store.py's contract: returns None (never
    raises) if the feature isn't registered or its eval_fn errors. Not
    called from any live path in this PR -- see module docstring."""
    spec = FEATURES.get(feature_id)
    if spec is None:
        return None
    try:
        return spec.eval_fn(conn, symbol, as_of)
    except Exception as e:
        log.warning(f"feature_registry: eval failed for {feature_id} ({symbol}, as_of={as_of}): {e}")
        return None

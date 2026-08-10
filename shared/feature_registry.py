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
  sector_regime_history, security_regime_history.
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

from regime_common import asof_index, load_daily_series
from signals import compute_bollinger, compute_rsi

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

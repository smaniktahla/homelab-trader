"""
Trade Thesis Validator, PR 3 of the Hypothesis-Driven Trading Architecture
epic -- see docs/trade-thesis-architecture-reconciliation.md §1a. Makes a
TradeThesis (PR 1, shared/trade_thesis.py) semantically real: a
PR-1-grammar-conformant thesis can be constructed today referencing a
feature that will never validate -- this module is what actually checks
that against the Feature Registry (PR 2, shared/feature_registry.py).

Four checks against a single TradeThesis + a live DB connection (features
need real price/regime history to confirm data availability):

0. Hypothesis type -- thesis.hypothesis_type must be registered and
   'active' in the Hypothesis Library (shared/hypothesis_library.py,
   PR 13). shared/trade_thesis.py's grammar layer only checks it's a
   non-empty string (PR 13 moved membership enforcement here), same
   grammar-vs-vocabulary split as check 1 below for 'feature' identifiers.
1. Vocabulary -- every 'feature' identifier referenced anywhere in
   entry_conditions/invalidation_spec/success_spec must be registered in
   feature_registry.FEATURES, and every evidence_context.providers key
   must be registered in feature_registry.PROVIDERS. A provider entry's
   declared 'source' must also match that provider's registered
   source_table -- ties §1b's grammar to §5's registry rather than trusting
   the caller to have spelled the table name right.
2. Data availability -- every referenced feature must actually evaluate to
   a non-None value as-of the thesis's own as_of date, for the thesis's
   symbol. A well-formed condition can name a real, registered feature
   that simply has no data yet (e.g. a symbol with fewer than 20 days of
   price_history) -- that thesis is not semantically valid regardless of
   grammar.
3. No lookahead -- evidence_context.as_of, and every per-provider 'as_of'
   present in evidence_context.providers, must be on or before the
   thesis's own as_of. feature_registry's eval_fns are already as-of-safe
   internally against price/regime history; this is the higher-level check
   that the evidence a thesis CLAIMS to have used wasn't dated after the
   thesis itself.

Runs standalone against hand-authored or backfilled TradeThesis instances
-- it does not require PR 4's live signal-generation path to produce them,
and nothing here is called from any live path yet.
"""

from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime

import feature_registry
import hypothesis_library
from trade_thesis import COMBINATORS, CONDITION_TREE_FIELDS


@dataclass
class ValidationResult:
    errors: list = field(default_factory=list)

    @property
    def is_valid(self):
        return not self.errors


def extract_referenced_features(thesis):
    """Set of every distinct 'feature' identifier used across
    entry_conditions/invalidation_spec/success_spec. Walks the same
    combinator/leaf shape shared/trade_thesis.py's grammar already
    guarantees (a TradeThesis instance has always passed that validation
    by construction), so this walker doesn't re-validate shape -- it only
    extracts."""
    features = set()
    for field_name in CONDITION_TREE_FIELDS:
        _walk(getattr(thesis, field_name), features)
    return features


def _walk(node, features):
    combinator_keys = COMBINATORS & node.keys()
    if combinator_keys:
        combinator = next(iter(combinator_keys))
        children = node[combinator]
        if combinator == "not":
            _walk(children, features)
        else:
            for child in children:
                _walk(child, features)
        return
    features.add(node["feature"])


def _to_date(value):
    return value.date() if isinstance(value, datetime) else value


def _parse_iso_date(value):
    """None if `value` is missing or not a parseable date -- lenient by
    design, since PR 1's grammar only guarantees evidence_context.as_of is
    a non-empty string, not that it's a valid ISO date. An unparseable
    as_of simply can't be checked for lookahead here; it already isn't a
    grammar error and adding a new failure mode for it is out of scope."""
    if not isinstance(value, str):
        return None
    try:
        return date_cls.fromisoformat(value[:10])
    except ValueError:
        return None


def validate_trade_thesis(conn, thesis):
    """Run all three semantic checks against a single TradeThesis. Returns
    a ValidationResult listing every failure found (not just the first),
    so a caller building/backfilling many theses can see everything wrong
    with one in a single pass."""
    errors = []

    if not hypothesis_library.is_legal_hypothesis_type(conn, thesis.hypothesis_type):
        errors.append(f"unregistered or inactive hypothesis_type '{thesis.hypothesis_type}'")

    referenced_features = extract_referenced_features(thesis)

    for feature_id in sorted(referenced_features):
        if not feature_registry.is_legal_feature(feature_id):
            errors.append(f"unregistered feature '{feature_id}'")

    providers = thesis.evidence_context.get("providers", {})
    for provider_id, provider_entry in providers.items():
        spec = feature_registry.get_provider(provider_id)
        if spec is None:
            errors.append(f"unregistered evidence provider '{provider_id}'")
            continue
        declared_source = provider_entry.get("source")
        if declared_source != spec.source_table:
            errors.append(
                f"provider '{provider_id}' declares source '{declared_source}', "
                f"registry expects '{spec.source_table}'"
            )

    thesis_as_of_date = _to_date(thesis.as_of)

    context_as_of = _parse_iso_date(thesis.evidence_context.get("as_of"))
    if context_as_of is not None and context_as_of > thesis_as_of_date:
        errors.append(
            f"evidence_context.as_of {context_as_of} is after thesis.as_of {thesis_as_of_date} (lookahead)"
        )

    for provider_id, provider_entry in providers.items():
        provider_as_of = _parse_iso_date(provider_entry.get("as_of"))
        if provider_as_of is not None and provider_as_of > thesis_as_of_date:
            errors.append(
                f"provider '{provider_id}'.as_of {provider_as_of} is after "
                f"thesis.as_of {thesis_as_of_date} (lookahead)"
            )

    # Data availability -- only meaningful for features that passed the
    # vocabulary check above; an unregistered feature already has its one
    # error and evaluate_feature() would just return None for it too.
    for feature_id in sorted(referenced_features):
        if not feature_registry.is_legal_feature(feature_id):
            continue
        value = feature_registry.evaluate_feature(conn, feature_id, thesis.symbol, thesis.as_of)
        if value is None:
            errors.append(
                f"feature '{feature_id}' has no data available as-of {thesis_as_of_date} for symbol {thesis.symbol}"
            )

    return ValidationResult(errors=errors)

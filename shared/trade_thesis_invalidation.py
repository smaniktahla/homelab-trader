"""
Trade Thesis Invalidation Model, PR 5 of the Hypothesis-Driven Trading
Architecture epic -- per docs/trade-thesis-architecture-reconciliation.md
§5's PR 5 bullet: "Extend the existing (currently inert) shared/
market_structure.py + regime infra ... into formal invalidation evaluation
-- do not reimplement structure/regime detection."

evaluate_thesis_invalidation() combines three independent signal sources,
any one of which alone is enough to flag a thesis invalidated:

1. invalidation_spec itself -- the PR 1 condition tree, evaluated live via
   evaluate_condition_tree() (new in this PR) against the Feature Registry
   (PR 2). Nobody built an actual evaluator for a condition tree before
   this PR -- PR 1 only validates shape, PR 3 only validates vocabulary/
   availability/lookahead against a fixed as_of.
2. Market Structure Engine deterioration -- reads (never recomputes)
   shared/market_structure.py's latest persisted CHoCH (change-of-
   character) flag via its existing snapshot_market_structure_for_symbol().
3. Regime deterioration -- reads (never recomputes) shared/
   security_regime.py's latest persisted stock-level classification and
   the already-registered market_regime.overall feature (PR 2), both via
   their existing loaders/eval_fns.

All reads use whatever was already computed by the ingest cycle's own
update_market_structure()/update_hierarchy_regime() calls, same "compute
once elsewhere, read back here" split every other consumer of this infra
already follows -- this module adds no new computation of structure or
regime, only a formal reading of it for invalidation purposes.

Three-valued (Kleene) logic throughout: a feature/signal that can't be
evaluated (no data yet, unregistered) is None ("unknown"), never coerced
to True or False -- same "never coerced to a guess" convention shared/
regime_common.py's own docstring establishes. A thesis is only ever
flagged invalidated on a definite, positively-observed signal; missing
data never triggers invalidation on its own.

Nothing here writes thesis.status or creates any row -- that's PR 10 (Live
Thesis Re-Evaluation)'s append-only trade_thesis_evaluations table, per
§4. This module only answers "does this thesis's premise look broken
right now," as a pure read; it does not act on the answer.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from feature_registry import evaluate_feature
from market_structure import snapshot_market_structure_for_symbol
from security_regime import load_latest_security_regime
from trade_thesis import COMBINATORS

log = logging.getLogger(__name__)

_BEARISH_SECURITY_CLASSIFICATIONS = frozenset({"bearish", "strong_bearish"})


@dataclass
class InvalidationResult:
    invalidated: bool
    reasons: list = field(default_factory=list)  # every signal that fired, not just the first
    unknown: bool = False  # True only if every one of the three signal sources was unevaluable


def _apply_operator(op, value, threshold):
    if op == "gt":
        return value > threshold
    if op == "lt":
        return value < threshold
    if op == "gte":
        return value >= threshold
    if op == "lte":
        return value <= threshold
    if op == "eq":
        return value == threshold
    if op == "between":
        low, high = threshold
        return low <= value <= high
    raise ValueError(f"unreachable: illegal operator '{op}' (PR 1 grammar should have already rejected this)")


def evaluate_condition_tree(conn, node, symbol, as_of):
    """Evaluate a PR-1-grammar-conformant condition tree against live data
    via the Feature Registry. Three-valued: True, False, or None ("can't
    be determined" -- an unregistered feature, or one with no data
    available as-of `as_of`).

    Kleene semantics for and/or/not: None propagates unless a definite
    short-circuit already settles the result -- an 'and' with one False
    child is False even if another child is unknown; an 'or' with one True
    child is True even if another child is unknown."""
    combinator_keys = COMBINATORS & node.keys()
    if combinator_keys:
        combinator = next(iter(combinator_keys))
        if combinator == "not":
            child = evaluate_condition_tree(conn, node["not"], symbol, as_of)
            return None if child is None else (not child)

        children = [evaluate_condition_tree(conn, child, symbol, as_of) for child in node[combinator]]
        if combinator == "and":
            if any(c is False for c in children):
                return False
            if any(c is None for c in children):
                return None
            return True
        else:  # "or"
            if any(c is True for c in children):
                return True
            if any(c is None for c in children):
                return None
            return False

    value = evaluate_feature(conn, node["feature"], symbol, as_of)
    if value is None:
        return None
    return _apply_operator(node["op"], value, node["value"])


def evaluate_thesis_invalidation(conn, thesis, as_of=None):
    """Read-only check of whether `thesis` looks invalidated right now.
    Never writes anything -- see module docstring. `as_of` defaults to
    now: unlike PR 1-4's as-of-safe historical evaluation, this is meant
    to run against current data, though it reuses the exact same as-of-safe
    eval_fns since 'now' is just another as_of value to them."""
    as_of = as_of or datetime.now(timezone.utc)
    reasons = []
    any_evaluated = False

    spec_result = evaluate_condition_tree(conn, thesis.invalidation_spec, thesis.symbol, as_of)
    if spec_result is not None:
        any_evaluated = True
        if spec_result:
            reasons.append("invalidation_spec_triggered")

    structure = snapshot_market_structure_for_symbol(conn, thesis.symbol)
    if structure.get("trend") != "insufficient_data":
        any_evaluated = True
        if structure.get("choch"):
            reasons.append("structure_choch")

    security_row = load_latest_security_regime(conn, thesis.symbol)
    if security_row is not None:
        any_evaluated = True
        classification = security_row.get("classification")
        if classification in _BEARISH_SECURITY_CLASSIFICATIONS:
            reasons.append(f"security_regime_{classification}")

    market_overall = evaluate_feature(conn, "market_regime.overall", thesis.symbol, as_of)
    if market_overall is not None:
        any_evaluated = True
        if market_overall.startswith("bear"):
            reasons.append(f"market_regime_{market_overall}")

    return InvalidationResult(
        invalidated=bool(reasons),
        reasons=reasons,
        unknown=not any_evaluated,
    )

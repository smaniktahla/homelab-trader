"""
Signal component model: the typed structure each scoring family (technical,
fundamental, earnings, news, options, macro_fit) fills in, and the pure
combination function that turns a partial set of components into one
composite score. No DB/IO here — see shared/feature_store.py for
persistence. Pull composite scores through weighted_component_score() even
when only one component is weighted (as in PR #1) so the API and any future
scoring.py exercise the real abstraction instead of a second, hand-written
"score = technical_score" shortcut that could drift from this one.
"""

from dataclasses import dataclass, field


@dataclass
class SignalComponents:
    technical: float | None = None
    fundamental: float | None = None
    earnings: float | None = None
    news: float | None = None
    options: float | None = None
    macro_fit: float | None = None
    data_confidence: float = 0.0
    vetoes: list[str] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)


def weighted_component_score(scores, weights):
    """Weight-renormalized average over only the non-None entries in `scores`.

    Returns None if every component is missing, or if the components that
    *are* available happen to carry zero total weight (avoids a ZeroDivisionError
    and avoids the more subtle bug of silently returning 0 for "no information",
    which would read as maximally bearish rather than as "unknown").

    Raises ValueError if any weight is negative — a negative weight isn't a
    modeling choice this function is meant to support silently; a caller
    that means to penalize a component should transform the score itself,
    not sign-flip its weight.
    """
    if any(w < 0 for w in weights.values()):
        raise ValueError("Component weights must be non-negative")

    available = {name: score for name, score in scores.items() if score is not None}
    if not available:
        return None

    denominator = sum(weights.get(name, 0.0) for name in available)
    if denominator == 0:
        return None

    numerator = sum(available[name] * weights.get(name, 0.0) for name in available)
    return numerator / denominator

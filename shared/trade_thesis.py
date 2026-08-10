"""
Trade Thesis Schema, PR 1 of the Hypothesis-Driven Trading Architecture
epic -- see docs/trade-thesis-architecture-reconciliation.md (§1/§1a/§1b/§7)
for the full design. `trade_theses` is a new object one level below the
existing `theses` (strategy family) registry: one falsifiable hypothesis
for one specific opportunity, not a strategy-wide concept.

This module defines the **grammar** only -- the JSON shape and the set of
syntactically legal operators/combinators for entry_conditions/
invalidation_spec/success_spec, plus the evidence_context provider
envelope (§1b). It does NOT check that a referenced feature identifier
exists, is computable as-of a date, or resolves without lookahead -- that
is PR 3 (trade thesis validator), which runs against PR 2's Feature
Registry. A TradeThesis can be well-formed per this module's grammar and
still reference a feature that will never validate.

No code path here is called from any live signal-generation path yet --
that is PR 4 (evidence evaluation engine). record_trade_thesis() exists
so PR 1's own tests, and any future hand-authored/backfilled row, have a
real persistence path to exercise -- same "computed, tested, persisted,
not yet load-bearing" staging as shared/market_structure.py.
"""

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime

log = logging.getLogger(__name__)

TRADE_THESIS_SCHEMA_VERSION = "v1"  # versions this module's grammar (condition shape,
                                     # legal operators/combinators, evidence_context envelope).
                                     # Bump only when the grammar itself changes.

# Minimal placeholder vocabulary sufficient to port mean_reversion's existing
# entry logic. Full vocabulary across future strategy families is explicitly
# deferred (see docs/trade-thesis-architecture-reconciliation.md §6) --
# grammar-adjacent enum work, not PR 2's Feature Registry job.
HYPOTHESIS_TYPES = frozenset({
    "mean_reversion_oversold",
    "mean_reversion_overbought",
})

STATUSES = frozenset({
    "proposed", "active", "weakening", "invalidated", "completed", "superseded",
})

# Legal comparators for a single condition leaf.
OPERATORS = frozenset({"gt", "lt", "gte", "lte", "eq", "between"})

# Legal logical combinators for a condition tree's internal nodes.
COMBINATORS = frozenset({"and", "or", "not"})

# Fields whose grammar is a recursive condition tree, validated identically.
_CONDITION_TREE_FIELDS = ("entry_conditions", "invalidation_spec", "success_spec")

# Fields frozen at construction -- never legal to mutate after a TradeThesis
# is created (see docs/trade-thesis-architecture-reconciliation.md §4).
# `status` is deliberately excluded: it is the one field allowed to change
# over the thesis's life (PR 10), and even then via an append-only
# trade_thesis_evaluations row, not a blind UPDATE -- not this module's job.
_IMMUTABLE_FIELDS = (
    "entry_conditions", "evidence_snapshot", "invalidation_spec",
    "success_spec", "confidence", "provenance",
)


class GrammarError(ValueError):
    """Raised when a condition tree, evidence_context, or other grammar-
    constrained field doesn't conform to the Trade Thesis Schema grammar.
    A GrammarError means the JSON is syntactically illegal -- it says
    nothing about whether a referenced feature identifier actually exists
    (PR 3's job)."""


def validate_condition_tree(node, field_name):
    """Recursively validate a condition tree's syntactic shape. Raises
    GrammarError on any violation; returns None on success (called for its
    side effect, same convention as the stdlib's own validation helpers).

    A leaf condition is `{"feature": <str>, "op": <OPERATORS>, "value": ...}`
    ("value" is a 2-element [low, high] list when op == "between", otherwise
    a scalar). An internal node is `{"and"/"or": [<node>, ...]}` or
    `{"not": <node>}`. This function checks shape and legal
    operator/combinator membership only -- it never inspects whether
    "feature" names something real (PR 3's job, per module docstring)."""
    if not isinstance(node, dict):
        raise GrammarError(f"{field_name}: condition node must be an object, got {type(node).__name__}")

    combinator_keys = COMBINATORS & node.keys()
    if combinator_keys:
        if len(node) != 1:
            raise GrammarError(f"{field_name}: combinator node must have exactly one key, got {sorted(node.keys())}")
        combinator = next(iter(combinator_keys))
        children = node[combinator]
        if combinator == "not":
            validate_condition_tree(children, field_name)
        else:
            if not isinstance(children, list) or len(children) < 2:
                raise GrammarError(f"{field_name}: '{combinator}' requires a list of at least 2 child conditions")
            for child in children:
                validate_condition_tree(child, field_name)
        return

    required = {"feature", "op", "value"}
    missing = required - node.keys()
    if missing:
        raise GrammarError(f"{field_name}: condition leaf missing required key(s) {sorted(missing)}")
    extra = node.keys() - required
    if extra:
        raise GrammarError(f"{field_name}: condition leaf has unexpected key(s) {sorted(extra)}")

    if not isinstance(node["feature"], str) or not node["feature"]:
        raise GrammarError(f"{field_name}: 'feature' must be a non-empty string")

    op = node["op"]
    if op not in OPERATORS:
        raise GrammarError(f"{field_name}: illegal operator '{op}', must be one of {sorted(OPERATORS)}")

    value = node["value"]
    if op == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise GrammarError(f"{field_name}: 'between' requires value to be a 2-element [low, high] list")
        if not all(isinstance(v, (int, float)) for v in value):
            raise GrammarError(f"{field_name}: 'between' bounds must be numeric")
    else:
        if not isinstance(value, (int, float, str, bool)):
            raise GrammarError(f"{field_name}: 'value' must be a scalar for op '{op}', got {type(value).__name__}")


def validate_evidence_context(evidence_context):
    """Validate the {as_of, providers: {...}} envelope shape (§1b). Checks
    structure only -- which provider keys are legal and what each one's
    version field means is PR 2/3's job, per module docstring."""
    if not isinstance(evidence_context, dict):
        raise GrammarError(f"evidence_context must be an object, got {type(evidence_context).__name__}")

    required = {"as_of", "providers"}
    missing = required - evidence_context.keys()
    if missing:
        raise GrammarError(f"evidence_context missing required key(s) {sorted(missing)}")

    if not isinstance(evidence_context["as_of"], str) or not evidence_context["as_of"]:
        raise GrammarError("evidence_context.as_of must be a non-empty string")

    providers = evidence_context["providers"]
    if not isinstance(providers, dict) or not providers:
        raise GrammarError("evidence_context.providers must be a non-empty object")

    for key, provider in providers.items():
        if not isinstance(provider, dict):
            raise GrammarError(f"evidence_context.providers['{key}'] must be an object")
        if "source" not in provider or not isinstance(provider["source"], str) or not provider["source"]:
            raise GrammarError(f"evidence_context.providers['{key}'] missing non-empty 'source'")


def _evidence_snapshot_keys_invalid(snapshot):
    required = {"supporting", "contradictory", "missing"}
    if not isinstance(snapshot, dict) or snapshot.keys() != required:
        return True
    return any(not isinstance(snapshot[k], list) for k in required)


@dataclass(frozen=True)
class TradeThesis:
    """Object model mirroring the trade_theses table columns (same shape
    convention as shared/signal_components.py -- typed dataclass, no DB/IO).
    frozen=True makes the §4 immutability contract structurally hard to
    violate for the fields that matter: there is no attribute-assignment
    path at all, immutable or not. `status` progression (PR 10) is
    expected to construct a new TradeThesis via evolve(), not mutate one in
    place -- this dataclass has no concept of "the same row, updated,"
    only "a new value."""

    thesis_id: int
    symbol: str
    hypothesis_type: str
    hypothesis_text: str
    entry_conditions: dict
    invalidation_spec: dict
    success_spec: dict
    evidence_context: dict
    provenance: dict
    as_of: datetime
    schema_version: str = TRADE_THESIS_SCHEMA_VERSION
    evidence_snapshot: dict = field(default_factory=lambda: {"supporting": [], "contradictory": [], "missing": []})
    confidence: float | None = None
    status: str = "proposed"

    def __post_init__(self):
        if self.hypothesis_type not in HYPOTHESIS_TYPES:
            raise GrammarError(f"illegal hypothesis_type '{self.hypothesis_type}', must be one of {sorted(HYPOTHESIS_TYPES)}")
        if self.status not in STATUSES:
            raise GrammarError(f"illegal status '{self.status}', must be one of {sorted(STATUSES)}")
        if self.confidence is not None and not (0 <= self.confidence <= 1):
            raise GrammarError(f"confidence must be between 0 and 1, got {self.confidence}")

        for field_name in _CONDITION_TREE_FIELDS:
            validate_condition_tree(getattr(self, field_name), field_name)
        validate_evidence_context(self.evidence_context)

        if _evidence_snapshot_keys_invalid(self.evidence_snapshot):
            raise GrammarError("evidence_snapshot must be an object with 'supporting'/'contradictory'/'missing' list fields")

    def evolve(self, **changes):
        """Construct a new TradeThesis with `changes` applied, re-running
        all grammar validation via __post_init__. The only sanctioned way
        to get a "changed" TradeThesis -- direct field mutation isn't
        possible on a frozen dataclass. Callers that touch an
        _IMMUTABLE_FIELDS name should not use this for anything but
        constructing the very first version of a thesis; nothing in this
        PR calls evolve() to rewrite a persisted row's frozen fields."""
        return replace(self, **changes)

    def to_json(self):
        """Serialize to the same dict shape the trade_theses table's JSONB
        columns expect. Round-trips with from_json()."""
        return {
            "thesis_id": self.thesis_id,
            "symbol": self.symbol,
            "schema_version": self.schema_version,
            "evidence_context": self.evidence_context,
            "hypothesis_type": self.hypothesis_type,
            "hypothesis_text": self.hypothesis_text,
            "entry_conditions": self.entry_conditions,
            "evidence_snapshot": self.evidence_snapshot,
            "invalidation_spec": self.invalidation_spec,
            "success_spec": self.success_spec,
            "confidence": self.confidence,
            "provenance": self.provenance,
            "status": self.status,
            "as_of": self.as_of.isoformat(),
        }

    @classmethod
    def from_json(cls, data):
        """Deserialize from to_json()'s shape. Re-validates via
        __post_init__ -- a hand-edited or corrupted JSON blob fails loudly
        here rather than silently constructing an invalid TradeThesis."""
        kwargs = dict(data)
        kwargs["as_of"] = datetime.fromisoformat(kwargs["as_of"])
        return cls(**kwargs)


def record_trade_thesis(conn, thesis):
    """Best-effort insert of a TradeThesis into trade_theses. Returns the
    new row id, or None if the write failed for any reason. Fail-open,
    same convention as shared/feature_store.py -- rolls back only its own
    statement and never raises past its own try/except.

    Not called from any live path in this PR (see module docstring) --
    exists so PR 1's persistence contract has a real, tested implementation
    ready for PR 4 to call, rather than being designed blind."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trade_theses
                    (thesis_id, symbol, schema_version, evidence_context, hypothesis_type,
                     hypothesis_text, entry_conditions, evidence_snapshot, invalidation_spec,
                     success_spec, confidence, provenance, status, as_of)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                thesis.thesis_id, thesis.symbol, thesis.schema_version,
                json.dumps(thesis.evidence_context), thesis.hypothesis_type, thesis.hypothesis_text,
                json.dumps(thesis.entry_conditions), json.dumps(thesis.evidence_snapshot),
                json.dumps(thesis.invalidation_spec), json.dumps(thesis.success_spec),
                thesis.confidence, json.dumps(thesis.provenance), thesis.status, thesis.as_of,
            ))
            row = cur.fetchone()
        conn.commit()
        return row[0] if row else None
    except Exception as e:
        log.warning(f"trade_thesis: record failed for {thesis.symbol}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None

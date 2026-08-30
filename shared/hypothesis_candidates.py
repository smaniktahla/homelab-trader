"""
Hypothesis Candidate Generation, PR 14 of the Hypothesis-Driven Trading
Architecture epic. Sits between shared/hypothesis_library.py (PR 13's
catalog of hypothesis_type templates) and a later, separately-scoped
"Strategy Incubator Integration" PR that will register candidates into a
much larger, not-yet-built Strategy Incubator epic's own ResearchExperiment/
StrategyVersion concepts.

Given a hypothesis_type and a caller-supplied parameter-variation spec
(a dict mapping a condition-tree 'feature' identifier to a list of scalar
values to substitute), this module generates one concrete candidate --
a fully-substituted entry_conditions/invalidation_spec/success_spec set --
per combination in the Cartesian product across all supplied features.

This is generation + persistence + provenance ONLY. There is no callable
backtest-invocation entry point anywhere in this repo -- backtest_results
(ingest/schema.sql) is only ever written by one-off standalone scripts
under ingest/research/backtests/*.py, each with its own hardcoded params --
so this module does not attempt to wire candidates into backtest execution;
that is a separately-scoped, much larger effort. Nothing here executes a
candidate, scores it, promotes it, or rejects it -- candidate_batches/
candidates are deliberately NOT named research_experiments/strategy_versions
so they don't squat on the Strategy Incubator epic's future schema.

candidate_batches.hypothesis_type_version is copied from
hypothesis_types.version AT GENERATION TIME and never re-derived -- if the
catalog entry is edited afterward, already-generated batches keep pointing
at the version they were actually generated against. Generation always
requires the hypothesis_type to be instantiable with allow_experimental=True
(shared/hypothesis_library.py::is_instantiable_hypothesis_type) -- this is a
research-only path, exactly the case that flag exists for.
"""

import copy
import itertools
import json
import logging
from dataclasses import dataclass

import hypothesis_library
from trade_thesis import GrammarError, validate_condition_tree

log = logging.getLogger(__name__)

# Maps a HypothesisTypeSpec template field to the destination candidates
# column name -- the "default_" prefix genuinely differs from the generated
# artifact's field name, so this mapping is spelled out rather than reusing
# hypothesis_library's private _SPEC_CONDITION_TREE_FIELDS tuple.
_TEMPLATE_TO_CANDIDATE_FIELDS = (
    ("default_entry_conditions", "entry_conditions"),
    ("default_invalidation_spec", "invalidation_spec"),
    ("default_success_spec", "success_spec"),
)


@dataclass(frozen=True)
class CandidateBatch:
    id: int
    hypothesis_type: str
    hypothesis_type_version: int
    schema_version: str
    parameter_spec: dict
    generated_by: str | None


@dataclass(frozen=True)
class Candidate:
    id: int
    batch_id: int
    parameter_values: dict
    entry_conditions: dict
    invalidation_spec: dict | None
    success_spec: dict | None


def _collect_referenced_features(tree):
    """Every leaf 'feature' string in a condition tree. Same leaf/combinator
    shape validate_condition_tree() already knows, but collecting instead of
    validating -- tree is assumed already grammar-valid (it came from a
    hypothesis_types row, which is validated at write time)."""
    features = set()

    def _walk(node):
        combinator_keys = {"and", "or", "not"} & node.keys()
        if combinator_keys:
            combinator = next(iter(combinator_keys))
            children = node[combinator]
            if combinator == "not":
                _walk(children)
            else:
                for child in children:
                    _walk(child)
            return
        features.add(node["feature"])

    _walk(tree)
    return features


def _substitute_leaf_values(tree, feature, value):
    """Deep-copies nothing itself (caller deep-copies the whole tree first);
    mutates `tree` in place, replacing every leaf whose 'feature' matches
    with the supplied scalar `value`. 'between' leaves on a matching feature
    are left untouched (a single scalar can't redefine a [low, high] pair)
    -- logged by the caller, not an error here."""
    combinator_keys = {"and", "or", "not"} & tree.keys()
    if combinator_keys:
        combinator = next(iter(combinator_keys))
        children = tree[combinator]
        if combinator == "not":
            _substitute_leaf_values(children, feature, value)
        else:
            for child in children:
                _substitute_leaf_values(child, feature, value)
        return
    if tree["feature"] == feature and tree["op"] != "between":
        tree["value"] = value


def _leaf_has_unsubstitutable_between(tree, features):
    """True if any leaf referencing one of `features` uses op=='between' --
    used only to decide whether to log a warning, not to block generation."""
    combinator_keys = {"and", "or", "not"} & tree.keys()
    if combinator_keys:
        combinator = next(iter(combinator_keys))
        children = tree[combinator]
        if combinator == "not":
            return _leaf_has_unsubstitutable_between(children, features)
        return any(_leaf_has_unsubstitutable_between(c, features) for c in children)
    return tree["feature"] in features and tree["op"] == "between"


def generate_candidates(conn, type_key, parameter_spec, *, generated_by=None):
    """Generate and persist one candidate per combination in the Cartesian
    product of parameter_spec's values, substituted into type_key's
    default_entry_conditions/default_invalidation_spec/default_success_spec
    templates. Returns (batch_id, [candidate_id, ...]) on success, or None
    on any failure (fail-open, matching hypothesis_library's contract) --
    logs the reason via log.warning. A partially-generated batch is never
    left visible: everything happens in one transaction."""
    spec = hypothesis_library.get_hypothesis_type(conn, type_key)
    if spec is None:
        log.warning(f"hypothesis_candidates: unknown hypothesis_type '{type_key}'")
        return None

    if not hypothesis_library.is_instantiable_hypothesis_type(conn, type_key, allow_experimental=True):
        log.warning(f"hypothesis_candidates: '{type_key}' is not instantiable (inactive/deprecated or stale schema_version)")
        return None

    if spec.default_entry_conditions is None:
        log.warning(f"hypothesis_candidates: '{type_key}' has no default_entry_conditions to generate from")
        return None

    templates = {}
    referenced_features = set()
    for template_field, candidate_field in _TEMPLATE_TO_CANDIDATE_FIELDS:
        tree = getattr(spec, template_field)
        templates[candidate_field] = tree
        if tree is not None:
            referenced_features |= _collect_referenced_features(tree)

    unmatched = set(parameter_spec.keys()) - referenced_features
    if unmatched:
        log.warning(f"hypothesis_candidates: swept feature(s) {sorted(unmatched)} not referenced in '{type_key}' templates")
        return None

    for feature in parameter_spec:
        for template_field, candidate_field in _TEMPLATE_TO_CANDIDATE_FIELDS:
            tree = templates[candidate_field]
            if tree is not None and _leaf_has_unsubstitutable_between(tree, {feature}):
                log.warning(f"hypothesis_candidates: '{feature}' has a 'between' leaf in {candidate_field}, left unsubstituted")

    feature_names = list(parameter_spec.keys())
    combinations = list(itertools.product(*(parameter_spec[f] for f in feature_names))) if feature_names else [()]

    generated = []
    for combo in combinations:
        parameter_values = dict(zip(feature_names, combo))
        candidate_trees = {}
        try:
            for candidate_field, tree in templates.items():
                if tree is None:
                    candidate_trees[candidate_field] = None
                    continue
                substituted = copy.deepcopy(tree)
                for feature, value in parameter_values.items():
                    _substitute_leaf_values(substituted, feature, value)
                validate_condition_tree(substituted, candidate_field)
                candidate_trees[candidate_field] = substituted
        except GrammarError as e:
            log.warning(f"hypothesis_candidates: generated tree failed validation for '{type_key}', combo {parameter_values}: {e}")
            return None
        generated.append((parameter_values, candidate_trees))

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO candidate_batches
                    (hypothesis_type, hypothesis_type_version, schema_version, parameter_spec, generated_by)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (type_key, spec.version, spec.schema_version, json.dumps(parameter_spec), generated_by))
            batch_id = cur.fetchone()[0]

            candidate_ids = []
            for parameter_values, candidate_trees in generated:
                cur.execute("""
                    INSERT INTO candidates
                        (batch_id, parameter_values, entry_conditions, invalidation_spec, success_spec)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    batch_id, json.dumps(parameter_values), json.dumps(candidate_trees["entry_conditions"]),
                    json.dumps(candidate_trees["invalidation_spec"]) if candidate_trees["invalidation_spec"] is not None else None,
                    json.dumps(candidate_trees["success_spec"]) if candidate_trees["success_spec"] is not None else None,
                ))
                candidate_ids.append(cur.fetchone()[0])
        conn.commit()
        return batch_id, candidate_ids
    except Exception as e:
        log.warning(f"hypothesis_candidates: persistence failed for '{type_key}': {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def get_candidate_batch(conn, batch_id):
    """None if batch_id doesn't exist, or on any failure -- fail-open,
    matching hypothesis_library.get_hypothesis_type()'s contract."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, hypothesis_type, hypothesis_type_version, schema_version, parameter_spec, generated_by
                FROM candidate_batches WHERE id=%s
            """, (batch_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return CandidateBatch(
            id=row[0], hypothesis_type=row[1], hypothesis_type_version=row[2],
            schema_version=row[3], parameter_spec=row[4], generated_by=row[5],
        )
    except Exception as e:
        log.warning(f"hypothesis_candidates: get_candidate_batch failed for id={batch_id}: {e}")
        return None


def list_candidates(conn, batch_id):
    """All candidates in a batch. Empty list (not None) if the batch has no
    candidates or the query fails -- same fail-open list contract as
    hypothesis_library.list_hypothesis_types()."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, batch_id, parameter_values, entry_conditions, invalidation_spec, success_spec
                FROM candidates WHERE batch_id=%s ORDER BY id
            """, (batch_id,))
            rows = cur.fetchall()
        return [
            Candidate(id=r[0], batch_id=r[1], parameter_values=r[2], entry_conditions=r[3],
                      invalidation_spec=r[4], success_spec=r[5])
            for r in rows
        ]
    except Exception as e:
        log.warning(f"hypothesis_candidates: list_candidates failed for batch_id={batch_id}: {e}")
        return []


def list_candidate_batches(conn, hypothesis_type=None):
    """All candidate batches, optionally filtered by hypothesis_type.
    Empty list (not None) on failure."""
    try:
        with conn.cursor() as cur:
            if hypothesis_type is not None:
                cur.execute("""
                    SELECT id, hypothesis_type, hypothesis_type_version, schema_version, parameter_spec, generated_by
                    FROM candidate_batches WHERE hypothesis_type=%s ORDER BY id
                """, (hypothesis_type,))
            else:
                cur.execute("""
                    SELECT id, hypothesis_type, hypothesis_type_version, schema_version, parameter_spec, generated_by
                    FROM candidate_batches ORDER BY id
                """)
            rows = cur.fetchall()
        return [
            CandidateBatch(id=r[0], hypothesis_type=r[1], hypothesis_type_version=r[2],
                           schema_version=r[3], parameter_spec=r[4], generated_by=r[5])
            for r in rows
        ]
    except Exception as e:
        log.warning(f"hypothesis_candidates: list_candidate_batches failed: {e}")
        return []

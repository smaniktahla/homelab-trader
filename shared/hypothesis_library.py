"""
Hypothesis Library, PR 13 of the Hypothesis-Driven Trading Architecture
epic -- see docs/trade-thesis-architecture-reconciliation.md §5 (Phase 4-6
disposition: "genuinely new, no existing infra to reconcile against").

A catalog of hypothesis_type *templates*, DB-backed so a new type can be
added by INSERT instead of a code deploy. Before this PR,
shared/trade_thesis.py::HYPOTHESIS_TYPES was a hardcoded frozenset checked
at TradeThesis construction time -- adding a type meant editing that module.
This module replaces that with hypothesis_types (ingest/schema.sql), and
membership enforcement moves to shared/trade_thesis_validator.py via
is_legal_hypothesis_type(), matching the same grammar/vocabulary split PR 3
already applies to 'feature' identifiers against shared/feature_registry.py.

Shape mirrors feature_registry.py: a frozen spec dataclass plus DB-backed
lookup/write functions, not trade_thesis.py's per-instance grammar-object
shape (this isn't a per-instance concept -- one row can back many
TradeThesis instances).

Conceptual hierarchy this module sits at the bottom of: HypothesisType ->
ResearchExperiment -> StrategyVersion -> TradeThesis -> TradeProposal ->
Order/Position. This module implements only the HypothesisType layer.
hypothesis_types.status (active/experimental/deprecated) is catalog-entry
lifecycle only -- "is this template available for use," never "has an
implementation of this idea demonstrated a tradable edge." That question,
and any strategy lifecycle/versioning/scoring/promotion concept, belongs to
the separately-scoped Strategy Incubator epic, one or more layers up. Do
not grow those concepts here.

Two distinct questions, two functions -- do not collapse them:
- hypothesis_type_exists(): is this key in the catalog at all (any status)?
  For research tooling that wants to browse/reference experimental or even
  deprecated templates.
- is_instantiable_hypothesis_type(): is this key legal for constructing a
  NEW real TradeThesis right now? Requires status='active' (optionally also
  'experimental' for research-only callers via allow_experimental=True --
  the live signal-generation path must never pass that) AND
  schema_version == trade_thesis.TRADE_THESIS_SCHEMA_VERSION. A catalog
  entry written against an older grammar version stops being instantiable
  the moment the grammar bumps, without any row needing to be touched --
  it's still catalog-visible via list/get, it just fails this check.
  is_legal_hypothesis_type() is a thin active-only wrapper around this,
  kept for callers (the PR 3 validator) that don't need the
  allow_experimental knob.

Future StrategyVersion/TradeThesis-generating code (PR 14) must record
which hypothesis_type_version it was generated against and keep that
association immutable even if the catalog entry is later edited -- a
mutable template must never retroactively change the historical meaning of
an already-created research object. This module does not itself add a
hypothesis_type_version column to trade_theses (out of scope for PR 13),
but hypothesis_type_changes below exists specifically so that provenance
question stays answerable once PR 14 needs it.
"""

import json
import logging
from dataclasses import asdict, dataclass

import feature_registry
from trade_thesis import GrammarError, TRADE_THESIS_SCHEMA_VERSION, validate_condition_tree

log = logging.getLogger(__name__)

STATUSES = frozenset({"active", "experimental", "deprecated"})

# Fields on HypothesisTypeSpec that carry a condition-tree template, same
# grammar as trade_theses' own CONDITION_TREE_FIELDS.
_SPEC_CONDITION_TREE_FIELDS = ("default_entry_conditions", "default_invalidation_spec", "default_success_spec")

# Fields a caller may set via register/update; schema_version and version
# are deliberately excluded -- schema_version is always stamped from
# TRADE_THESIS_SCHEMA_VERSION, version is always self-incrementing.
_WRITABLE_FIELDS = frozenset({
    "type_key", "display_name", "description", "category", "required_providers",
    "default_entry_conditions", "default_invalidation_spec", "default_success_spec", "status",
})


@dataclass(frozen=True)
class HypothesisTypeSpec:
    type_key: str
    display_name: str
    description: str
    category: str | None
    schema_version: str
    required_providers: list
    default_entry_conditions: dict | None
    default_invalidation_spec: dict | None
    default_success_spec: dict | None
    status: str
    version: int


def _row_to_spec(row):
    (type_key, display_name, description, category, schema_version, required_providers,
     default_entry_conditions, default_invalidation_spec, default_success_spec, status, version) = row
    return HypothesisTypeSpec(
        type_key=type_key, display_name=display_name, description=description, category=category,
        schema_version=schema_version, required_providers=list(required_providers or []),
        default_entry_conditions=default_entry_conditions, default_invalidation_spec=default_invalidation_spec,
        default_success_spec=default_success_spec, status=status, version=version,
    )


_SELECT_COLUMNS = """
    type_key, display_name, description, category, schema_version, required_providers,
    default_entry_conditions, default_invalidation_spec, default_success_spec, status, version
"""


def list_hypothesis_types(conn, status=None):
    """All catalog entries, optionally filtered by status. Empty list (not
    None) if the table is empty or the query fails -- callers enumerating
    for a UI or PR 14's strategy generation shouldn't need a None check."""
    try:
        with conn.cursor() as cur:
            if status is not None:
                cur.execute(f"SELECT {_SELECT_COLUMNS} FROM hypothesis_types WHERE status=%s ORDER BY type_key", (status,))
            else:
                cur.execute(f"SELECT {_SELECT_COLUMNS} FROM hypothesis_types ORDER BY type_key")
            rows = cur.fetchall()
        return [_row_to_spec(row) for row in rows]
    except Exception as e:
        log.warning(f"hypothesis_library: list failed: {e}")
        return []


def get_hypothesis_type(conn, type_key):
    """None if type_key doesn't exist, or on any failure -- fail-open,
    matching feature_registry.get_feature()'s dict.get() contract."""
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_SELECT_COLUMNS} FROM hypothesis_types WHERE type_key=%s", (type_key,))
            row = cur.fetchone()
        return _row_to_spec(row) if row else None
    except Exception as e:
        log.warning(f"hypothesis_library: get failed for '{type_key}': {e}")
        return None


def hypothesis_type_exists(conn, type_key):
    """True iff type_key is in the catalog at all, regardless of status.
    For research tooling that wants to reference/browse experimental or
    deprecated templates without asking "is this usable right now" -- see
    module docstring for why this is a separate question from
    is_instantiable_hypothesis_type()."""
    return get_hypothesis_type(conn, type_key) is not None


def is_instantiable_hypothesis_type(conn, type_key, *, allow_experimental=False):
    """True iff type_key is legal for constructing a NEW real TradeThesis
    right now: status='active' (or 'active'/'experimental' when
    allow_experimental=True, for research-only callers), AND
    schema_version matches the current trade_thesis.TRADE_THESIS_SCHEMA_
    VERSION. The live signal-generation path must always call this (or
    is_legal_hypothesis_type()) with allow_experimental=False -- see module
    docstring."""
    spec = get_hypothesis_type(conn, type_key)
    if spec is None:
        return False
    legal_statuses = {"active", "experimental"} if allow_experimental else {"active"}
    if spec.status not in legal_statuses:
        return False
    return spec.schema_version == TRADE_THESIS_SCHEMA_VERSION


def is_legal_hypothesis_type(conn, type_key):
    """Thin active-only wrapper around is_instantiable_hypothesis_type().
    This is the exact check shared/trade_thesis_validator.py::
    validate_trade_thesis() calls for the live path -- 'experimental'/
    'deprecated' entries, and entries written against a stale
    schema_version, are catalog-visible (list/get still return them) but
    not legal for new trade_theses construction."""
    return is_instantiable_hypothesis_type(conn, type_key, allow_experimental=False)


def _validate_spec_fields(conn, spec):
    """Shared write-time validation for register/update: required_providers
    must all be registered feature_registry providers, and any condition-
    tree template field must be grammar-legal per trade_thesis's own
    validator. Returns a list of error strings (empty if valid) -- catalog
    templates must themselves be well-formed, they are seed material for
    real TradeThesis instances (PR 14)."""
    errors = []
    for provider_id in spec.required_providers:
        if not feature_registry.is_legal_provider(provider_id):
            errors.append(f"unregistered provider '{provider_id}' in required_providers")

    for field_name in _SPEC_CONDITION_TREE_FIELDS:
        tree = getattr(spec, field_name)
        if tree is None:
            continue
        # default_*_spec fields share the same leaf/combinator grammar as
        # trade_theses' own entry_conditions/invalidation_spec/success_spec
        # (CONDITION_TREE_FIELDS), so reuse validate_condition_tree() as-is.
        try:
            validate_condition_tree(tree, field_name)
        except GrammarError as e:
            errors.append(str(e))

    if spec.status not in STATUSES:
        errors.append(f"illegal status '{spec.status}', must be one of {sorted(STATUSES)}")

    return errors


def register_hypothesis_type(conn, spec):
    """Insert a new catalog entry. Fail-open (returns None on error or on
    failed validation), same contract as trade_thesis.record_trade_thesis().
    schema_version is always stamped from TRADE_THESIS_SCHEMA_VERSION --
    any value the caller set on `spec` is ignored, so a catalog entry can
    never claim conformance to a grammar version it wasn't actually written
    against. Validates required_providers and any default_*_spec template
    before writing -- see _validate_spec_fields()."""
    spec = HypothesisTypeSpec(**{**asdict(spec), "schema_version": TRADE_THESIS_SCHEMA_VERSION})
    errors = _validate_spec_fields(conn, spec)
    if errors:
        log.warning(f"hypothesis_library: register rejected for '{spec.type_key}': {errors}")
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO hypothesis_types
                    (type_key, display_name, description, category, schema_version, required_providers,
                     default_entry_conditions, default_invalidation_spec, default_success_spec, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                spec.type_key, spec.display_name, spec.description, spec.category, spec.schema_version,
                json.dumps(spec.required_providers),
                json.dumps(spec.default_entry_conditions) if spec.default_entry_conditions is not None else None,
                json.dumps(spec.default_invalidation_spec) if spec.default_invalidation_spec is not None else None,
                json.dumps(spec.default_success_spec) if spec.default_success_spec is not None else None,
                spec.status,
            ))
            row = cur.fetchone()
        conn.commit()
        return row[0] if row else None
    except Exception as e:
        log.warning(f"hypothesis_library: register failed for '{spec.type_key}': {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def _record_change(cur, type_key, new_version, changed_by, previous_spec, new_spec):
    cur.execute("""
        INSERT INTO hypothesis_type_changes (type_key, version, changed_by, previous_value, new_value)
        VALUES (%s, %s, %s, %s, %s)
    """, (type_key, new_version, changed_by, json.dumps(asdict(previous_spec)), json.dumps(asdict(new_spec))))


def update_hypothesis_type(conn, type_key, changed_by=None, **changes):
    """Partial update of an existing catalog entry: only fields present in
    `changes` are touched (schema_version/version can't be set directly --
    see _WRITABLE_FIELDS), version is always incremented, updated_at always
    refreshed, and a hypothesis_type_changes row records the full before/
    after snapshot -- see module docstring on why mutated templates need
    minimal change history. Re-validates the merged result (existing spec
    fields overridden by `changes`) before writing -- a partial update must
    not be able to leave the row in an invalid state. Returns True on
    success, False if the entry doesn't exist, no writable field changed,
    or validation/write fails."""
    current = get_hypothesis_type(conn, type_key)
    if current is None:
        return False

    set_fields = {k: v for k, v in changes.items() if k in _WRITABLE_FIELDS}
    if not set_fields:
        return False

    merged = HypothesisTypeSpec(**{**asdict(current), **set_fields})
    errors = _validate_spec_fields(conn, merged)
    if errors:
        log.warning(f"hypothesis_library: update rejected for '{type_key}': {errors}")
        return False

    json_fields = {"required_providers", "default_entry_conditions", "default_invalidation_spec", "default_success_spec"}
    set_clause = ", ".join(f"{k}=%s" for k in set_fields) + ", version = version + 1, updated_at = NOW()"
    values = [json.dumps(v) if k in json_fields and v is not None else v for k, v in set_fields.items()]

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE hypothesis_types SET {set_clause} WHERE type_key=%s RETURNING id",
                (*values, type_key),
            )
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return False
            new_version = current.version + 1
            _record_change(cur, type_key, new_version, changed_by, current, merged)
        conn.commit()
        return True
    except Exception as e:
        log.warning(f"hypothesis_library: update failed for '{type_key}': {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False

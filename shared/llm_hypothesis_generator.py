"""
LLM hypothesis candidate generation -- Hypothesis-Driven Phase 5a-2, see
docs/hypothesis-driven-phase5a-scoping.md.

Asks the local model on AI2 (llama-server, OpenAI-compatible) to propose
threshold values for the numeric leaves of an existing hypothesis type's
template trees, then hands them to the existing
hypothesis_candidates.generate_candidates() -- which substitutes them and
re-validates every tree exactly as it does for a human-authored sweep.
The LLM never writes a tree, only values for leaves that already exist.

Inert by construction: the output is a candidate batch and nothing else.
Nothing here registers a strategy version, transitions a lifecycle state,
or touches trade_theses, proposals, orders or live signal parameters. The
model is never shown backtest results (that feedback loop is Phase 6).

Guardrails:
  - a proposal whose Cartesian product exceeds llm_candidate_batch_cap is
    rejected whole, never truncated
  - at most llm_candidate_batches_per_day LLM batches per rolling 24 hours
  - only numeric features are offered (text features such as event types
    have no declared vocabulary the model could be held to)
  - every batch records its provenance (candidate_batches.llm_provenance):
    the model as reported by the server (its alias is cosmetic), endpoint,
    prompt template version, prompt hash, sampling parameters, the raw
    response and the model's rationale
Fail-open: an unreachable server or malformed response creates nothing and
returns the reason to the caller; nothing retries in a loop.
"""

import hashlib
import json
import logging
import math
import os
from datetime import datetime, timezone

import requests

import hypothesis_candidates
import hypothesis_library
from feature_registry import FEATURES
from hypothesis_candidates import _collect_referenced_features
from signals import load_params

log = logging.getLogger(__name__)

LLM_BASE_URL = os.environ.get("LLM_CANDIDATE_BASE_URL", "http://10.10.10.226:8080")
PROMPT_TEMPLATE_VERSION = 1
TEMPERATURE = 0.7
MAX_TOKENS = 800
TIMEOUT_S = 180
DEFAULT_BATCH_CAP = 12
DEFAULT_BATCHES_PER_DAY = 5
MAX_VALUES_PER_FEATURE = 6

_TREE_FIELDS = ("default_entry_conditions", "default_invalidation_spec", "default_success_spec")


class GenerationRejected(Exception):
    """A proposal (or the request itself) was refused; message says why."""


# ── template inspection ────────────────────────────────────────────────────

def _leaves(node):
    for comb in ("and", "or"):
        if comb in node:
            for child in node[comb]:
                yield from _leaves(child)
            return
    if "not" in node:
        yield from _leaves(node["not"])
        return
    yield node


def substitutable_numeric_features(spec):
    """{feature_id: template value} for numeric features the model may set.

    generate_candidates() substitutes a value into EVERY leaf using the
    feature, across entry/invalidation/success. A feature whose leaves carry
    different template values (e.g. bollinger_breakout_continuation's
    bb_pct_b: entry > 1.0, invalidation < 0.0) would have both thresholds
    overwritten with one number, silently changing the hypothesis -- so it
    is not offered. Neither are text features or 'between' leaves."""
    values = {}
    blocked = set()
    for field in _TREE_FIELDS:
        tree = getattr(spec, field)
        if tree is None:
            continue
        for leaf in _leaves(tree):
            fid = leaf["feature"]
            f = FEATURES.get(fid)
            if f is None or f.output_type != "numeric" or leaf["op"] == "between":
                blocked.add(fid)
                continue
            values.setdefault(fid, set()).add(leaf["value"])
    return {fid: next(iter(v)) for fid, v in values.items() if len(v) == 1 and fid not in blocked}


def build_prompt(spec, features, batch_cap):
    lines = [
        "You propose parameter values for a trading research hypothesis. Output JSON only.",
        "",
        f"Hypothesis type: {spec.type_key} -- {spec.description}",
    ]
    for field in _TREE_FIELDS:
        tree = getattr(spec, field)
        if tree is not None:
            lines.append(f"Template {field.replace('default_', '')}: {json.dumps(tree, sort_keys=True)}")
    lines += ["", "Features you may set values for (each value is substituted into every leaf that uses the feature):"]
    for fid in sorted(features):
        lines.append(f"- {fid} (numeric): {FEATURES[fid].description} Template value: {features[fid]}")
    lines += [
        "",
        "Rules:",
        '- Return {"parameter_spec": {feature_id: [values...]}, "rationale": "<2-4 sentences>"}.',
        "- Use only the feature ids listed above. Every value must be a number.",
        f"- The number of combinations (product of the list lengths) must be <= {batch_cap}.",
        "- Propose values a careful researcher would want to test, not just a uniform grid.",
    ]
    return "\n".join(lines)


# ── validation ─────────────────────────────────────────────────────────────

def validate_proposal(proposal, features, batch_cap):
    """Return (parameter_spec, rationale) or raise GenerationRejected."""
    if not isinstance(proposal, dict) or not isinstance(proposal.get("parameter_spec"), dict):
        raise GenerationRejected("response has no parameter_spec object")
    spec_in = proposal["parameter_spec"]
    if not spec_in:
        raise GenerationRejected("parameter_spec is empty")
    unknown = sorted(set(spec_in) - set(features))
    if unknown:
        raise GenerationRejected(f"parameter_spec names features not offered: {unknown}")
    parameter_spec = {}
    for fid, values in spec_in.items():
        if not isinstance(values, list) or not values:
            raise GenerationRejected(f"{fid}: values must be a non-empty list")
        clean = []
        for v in values:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise GenerationRejected(f"{fid}: non-numeric value {v!r}")
            if v not in clean:
                clean.append(v)
        if len(clean) > MAX_VALUES_PER_FEATURE:
            raise GenerationRejected(f"{fid}: {len(clean)} values exceeds {MAX_VALUES_PER_FEATURE}")
        parameter_spec[fid] = clean
    combos = math.prod(len(v) for v in parameter_spec.values())
    if combos > batch_cap:
        raise GenerationRejected(f"{combos} combinations exceeds the batch cap of {batch_cap}")
    rationale = proposal.get("rationale")
    return parameter_spec, rationale if isinstance(rationale, str) else None


def _batches_last_24h(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM candidate_batches
            WHERE generated_by LIKE 'llm:%' AND generated_at > NOW() - INTERVAL '24 hours'
        """)
        return cur.fetchone()[0]


# ── model call ─────────────────────────────────────────────────────────────

def _server_model(base_url):
    """The model actually loaded (llama-server's alias is cosmetic)."""
    try:
        props = requests.get(f"{base_url}/props", timeout=10).json()
        path = props.get("model_path")
        return os.path.basename(path) if path else props.get("model_alias")
    except Exception:
        return None


def call_llm(prompt, base_url=LLM_BASE_URL):
    """Returns (raw_content, model_id). Raises GenerationRejected on any
    transport/format failure."""
    body = {
        "model": "local",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        r = requests.post(f"{base_url}/v1/chat/completions", json=body, timeout=TIMEOUT_S)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        raise GenerationRejected(f"LLM call failed: {e}") from e
    return content, _server_model(base_url)


# ── entry point ────────────────────────────────────────────────────────────

def generate_llm_candidates(conn, type_key, *, call=None, base_url=LLM_BASE_URL):
    """Ask the model for parameter values for `type_key` and persist them as
    a candidate batch. Returns {"batch_id", "candidate_ids", "parameter_spec",
    "rationale", "model"}; raises GenerationRejected with the reason when
    nothing was created."""
    spec = hypothesis_library.get_hypothesis_type(conn, type_key)
    if spec is None:
        raise GenerationRejected(f"unknown hypothesis_type '{type_key}'")
    features = substitutable_numeric_features(spec)
    if not features:
        raise GenerationRejected(f"'{type_key}' has no numeric template leaves an LLM could set")

    p = load_params(conn)
    batch_cap = int(p.get("llm_candidate_batch_cap", DEFAULT_BATCH_CAP))
    per_day = int(p.get("llm_candidate_batches_per_day", DEFAULT_BATCHES_PER_DAY))
    if _batches_last_24h(conn) >= per_day:
        raise GenerationRejected(f"daily limit reached ({per_day} LLM batches per 24h)")

    prompt = build_prompt(spec, features, batch_cap)
    raw, model = (call or call_llm)(prompt, base_url)
    try:
        proposal = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise GenerationRejected(f"response is not JSON: {e}") from e
    parameter_spec, rationale = validate_proposal(proposal, features, batch_cap)

    provenance = {
        "model": model,
        "endpoint": base_url,
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "sampling": {"temperature": TEMPERATURE, "max_tokens": MAX_TOKENS, "thinking": False},
        "hypothesis_type_version": spec.version,
        "raw_response": raw,
        "rationale": rationale,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    generated_by = f"llm:{model or 'unknown'}"
    result = hypothesis_candidates.generate_candidates(
        conn, type_key, parameter_spec, generated_by=generated_by, llm_provenance=provenance)
    if result is None:
        raise GenerationRejected("generate_candidates rejected the proposal (see service log)")
    batch_id, candidate_ids = result
    log.info(f"llm_hypothesis_generator: batch {batch_id} for {type_key}, {len(candidate_ids)} candidates, {generated_by}")
    return {"batch_id": batch_id, "candidate_ids": candidate_ids, "parameter_spec": parameter_spec,
            "rationale": rationale, "model": model}

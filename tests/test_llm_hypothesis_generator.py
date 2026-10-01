"""
Phase 5a-2 LLM candidate generation. The model call is replaced by a fake,
so these check validation, caps, provenance and inertness -- never a real
model's output.
"""

import json

import pytest

import hypothesis_library
from hypothesis_candidates import get_candidate_batch, list_candidates
from llm_hypothesis_generator import (
    GenerationRejected, generate_llm_candidates, substitutable_numeric_features, validate_proposal,
)

MR = "mean_reversion_oversold"
FEATURES = {"technical.rsi_14": 30, "technical.bb_pct_b": 0.1}


def _fake(response, model="fake-model-Q4.gguf"):
    calls = []

    def call(prompt, base_url):
        calls.append(prompt)
        return (json.dumps(response) if not isinstance(response, str) else response), model
    call.calls = calls
    return call


@pytest.fixture
def restore_params(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM signal_params WHERE key LIKE 'llm_candidate_%'")
        saved = cur.fetchall()
    yield
    with conn.cursor() as cur:
        for k, v in saved:
            cur.execute("UPDATE signal_params SET value=%s WHERE key=%s", (v, k))
    conn.commit()


def test_only_numeric_template_leaves_are_offered(conn):
    assert substitutable_numeric_features(hypothesis_library.get_hypothesis_type(conn, MR)) == FEATURES
    # structural_breakout_momentum's leaves are text (event types): nothing to offer
    assert substitutable_numeric_features(
        hypothesis_library.get_hypothesis_type(conn, "structural_breakout_momentum")) == {}
    with pytest.raises(GenerationRejected, match="no numeric template leaves"):
        generate_llm_candidates(conn, "structural_breakout_momentum", call=_fake({}))
    # bollinger_breakout_continuation uses bb_pct_b at two different thresholds
    # (entry > 1.0, invalidation < 0.0); one substituted value would overwrite
    # both, so the feature is not offered
    assert substitutable_numeric_features(
        hypothesis_library.get_hypothesis_type(conn, "bollinger_breakout_continuation")) == {}


def test_happy_path_creates_an_inert_batch_with_provenance(conn):
    fake = _fake({"parameter_spec": {"technical.rsi_14": [25, 35, 25], "technical.bb_pct_b": [0.05, 0.15]},
                  "rationale": "because"})
    out = generate_llm_candidates(conn, MR, call=fake)
    assert out["parameter_spec"] == {"technical.rsi_14": [25, 35], "technical.bb_pct_b": [0.05, 0.15]}  # deduped
    assert len(out["candidate_ids"]) == 4

    batch = get_candidate_batch(conn, out["batch_id"])
    assert batch.generated_by == "llm:fake-model-Q4.gguf"
    prov = batch.llm_provenance
    assert prov["model"] == "fake-model-Q4.gguf" and prov["rationale"] == "because"
    assert json.loads(prov["raw_response"])["rationale"] == "because"
    assert len(prov["prompt_sha256"]) == 64 and prov["prompt_template_version"] == 1

    # substituted into the real template, validated like any sweep
    trees = [c.entry_conditions for c in list_candidates(conn, out["batch_id"])]
    assert {leaf["value"] for t in trees for leaf in t["or"] if leaf["feature"] == "technical.rsi_14"} == {25, 35}

    # inert: nothing registered, nothing backtested, nothing traded
    with conn.cursor() as cur:
        for table in ("strategy_versions", "candidate_backtests", "trade_theses", "trade_proposals"):
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            assert cur.fetchone()[0] == 0, table

    # the prompt carries the template and offered features, and no results
    assert "technical.rsi_14" in fake.calls[0] and "<= 12" in fake.calls[0]
    assert "backtest" not in fake.calls[0].lower()


@pytest.mark.parametrize("response, reason", [
    ({"parameter_spec": {"technical.rsi_14": [20, 25, 30, 35], "technical.bb_pct_b": [0.0, 0.05, 0.1, 0.2]}},
     "exceeds the batch cap"),
    ({"parameter_spec": {"technical.close": [100]}}, "not offered"),
    ({"parameter_spec": {"technical.rsi_14": ["low"]}}, "non-numeric"),
    ({"parameter_spec": {"technical.rsi_14": [True]}}, "non-numeric"),
    ({"parameter_spec": {"technical.rsi_14": []}}, "non-empty list"),
    ({"parameter_spec": {}}, "empty"),
    ({"rationale": "no spec"}, "no parameter_spec"),
    ("not json at all", "not JSON"),
])
def test_bad_proposals_are_rejected_whole_and_create_nothing(conn, response, reason):
    with pytest.raises(GenerationRejected, match=reason):
        generate_llm_candidates(conn, MR, call=_fake(response))
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM candidate_batches")
        assert cur.fetchone()[0] == 0


def test_transport_failure_creates_nothing(conn):
    def broken(prompt, base_url):
        raise GenerationRejected("LLM call failed: connection refused")
    with pytest.raises(GenerationRejected, match="connection refused"):
        generate_llm_candidates(conn, MR, call=broken)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM candidate_batches")
        assert cur.fetchone()[0] == 0


def test_daily_limit_and_batch_cap_come_from_signal_params(conn, restore_params):
    with conn.cursor() as cur:
        cur.execute("UPDATE signal_params SET value=1 WHERE key='llm_candidate_batches_per_day'")
        cur.execute("UPDATE signal_params SET value=2 WHERE key='llm_candidate_batch_cap'")
    conn.commit()
    ok = _fake({"parameter_spec": {"technical.rsi_14": [25, 35]}})
    generate_llm_candidates(conn, MR, call=ok)
    with pytest.raises(GenerationRejected, match="daily limit"):
        generate_llm_candidates(conn, MR, call=ok)
    assert "<= 2" in ok.calls[0]


def test_human_sweeps_do_not_count_toward_the_llm_daily_limit(conn, restore_params):
    from hypothesis_candidates import generate_candidates
    with conn.cursor() as cur:
        cur.execute("UPDATE signal_params SET value=1 WHERE key='llm_candidate_batches_per_day'")
    conn.commit()
    generate_candidates(conn, MR, {"technical.rsi_14": [20]}, generated_by="salil")
    generate_llm_candidates(conn, MR, call=_fake({"parameter_spec": {"technical.rsi_14": [25]}}))


def test_validate_proposal_caps_values_per_feature():
    with pytest.raises(GenerationRejected, match="exceeds 6"):
        validate_proposal({"parameter_spec": {"technical.rsi_14": [10, 15, 20, 25, 30, 35, 40]}},
                          FEATURES, batch_cap=100)

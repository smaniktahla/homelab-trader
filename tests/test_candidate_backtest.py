"""
Candidate backtest bridge (Hypothesis-Driven Phase 5a-1). Synthetic data,
arbitrary parameters -- these check mechanics and guardrails, not any
strategy's performance.
"""

import random
from datetime import date, timedelta

from candidate_backtest import (
    HOLDOUT_START, compile_tree, list_candidate_backtests, run_candidate_backtests,
)
from hypothesis_candidates import generate_candidates
from signals import load_params
from trade_thesis_invalidation import evaluate_condition_tree  # noqa: F401  (semantics reference)

START = date(2023, 6, 1)


# ── Kleene tree semantics ─────────────────────────────────────────────────

def test_compile_tree_kleene_semantics():
    series = {"a": [None, 1, 5], "b": [3, None, 3]}
    lt2 = {"feature": "a", "op": "lt", "value": 2}
    gt2 = {"feature": "b", "op": "gt", "value": 2}
    and_ = compile_tree({"and": [lt2, gt2]}, series)
    or_ = compile_tree({"or": [lt2, gt2]}, series)
    not_ = compile_tree({"not": lt2}, series)
    assert [and_(t) for t in range(3)] == [None, None, False]
    assert [or_(t) for t in range(3)] == [True, True, True]
    assert [not_(t) for t in range(3)] == [None, False, True]
    assert compile_tree(None, series) is None


# ── end-to-end fixture ────────────────────────────────────────────────────

def _walk(n, seed, drift=0.0):
    rng = random.Random(seed)
    out, c = [], 100.0
    for i in range(n):
        shock = -0.06 if i % 37 == 0 else rng.gauss(drift, 0.02)   # periodic dips -> oversold entries
        c = max(1.0, c * (1 + shock))
        out.append(round(c, 4))
    return out


def _seed_prices(conn, symbol, closes, start=START):
    with conn.cursor() as cur:
        for i, c in enumerate(closes):
            d = start + timedelta(days=i)
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, 1000)
            """, (symbol, d, c, c * 1.01, c * 0.99, c))
    conn.commit()


def _seed_regime(conn, days):
    with conn.cursor() as cur:
        for i in range(days):
            cur.execute("""
                INSERT INTO market_regime_history (trading_date, overall, score_modifier)
                VALUES (%s, 'neutral', 0)
            """, (START + timedelta(days=i),))
    conn.commit()


def _seed_universe(conn, days):
    for k, sym in enumerate(("AAA", "BBB", "CCC")):
        _seed_prices(conn, sym, _walk(days, seed=k + 1))
    _seed_prices(conn, "SPY", _walk(days, seed=99, drift=0.0005))
    _seed_regime(conn, days)


def _days_to(d):
    return (d - START).days


def test_results_are_appended_counted_and_bounded_by_holdout(conn):
    _seed_universe(conn, _days_to(HOLDOUT_START) + 120)   # ~4 months of data past the holdout boundary
    batch_id, cand_ids = generate_candidates(
        conn, "mean_reversion_oversold", {"technical.rsi_14": [35, 45]}, generated_by="test")
    p = load_params(conn)

    first = run_candidate_backtests(conn, cand_ids, params=p)
    assert [r["trial_number"] for r in first] == [1, 2]
    assert {r["status"] for r in first} <= {"complete", "insufficient_trades"}

    rows = list_candidate_backtests(conn, cand_ids[0])
    assert len(rows) == 1
    cfg, m = rows[0]["run_config"], rows[0]["metrics"]
    assert cfg["holdout_start"] == HOLDOUT_START.isoformat()
    assert date.fromisoformat(cfg["data_end"]) < HOLDOUT_START
    assert cfg["exit_family"] == "live_mean_reversion"
    assert m["trades"] > 0
    assert m["reference_default_template"]["trades"] > 0
    assert rows[0]["trials_to_date_for_type"] == 2

    # re-run appends, never updates; trial numbering keeps counting
    second = run_candidate_backtests(conn, cand_ids, params=p)
    assert [r["trial_number"] for r in second] == [3, 4]
    assert len(list_candidate_backtests(conn, cand_ids[0])) == 2

    # data past the holdout must not change anything: poison it, re-run, compare
    with conn.cursor() as cur:
        cur.execute("UPDATE price_history SET close = close * 0.2, low = low * 0.2 WHERE ts >= %s", (HOLDOUT_START,))
    conn.commit()
    third = run_candidate_backtests(conn, [cand_ids[0]], params=p)
    poisoned = list_candidate_backtests(conn, cand_ids[0])[0]
    assert poisoned["id"] == third[0]["backtest_id"]
    for key in ("trades", "mean_ret", "median_ret", "exit_reasons", "open_at_cutoff"):
        assert poisoned["metrics"][key] == m[key]


def test_insufficient_evidence_is_flagged(conn):
    _seed_universe(conn, _days_to(HOLDOUT_START))
    _, cand_ids = generate_candidates(conn, "mean_reversion_oversold",
                                      {"technical.rsi_14": [3], "technical.bb_pct_b": [-5.0]})
    res = run_candidate_backtests(conn, cand_ids, params=load_params(conn))
    assert res[0]["status"] == "insufficient_trades"   # extreme thresholds: almost no entries
    assert res[0]["trial_number"] == 1                  # still counted as a trial


def test_unsupported_feature_is_recorded_not_evaluated(conn):
    _seed_universe(conn, 260)
    _, cand_ids = generate_candidates(
        conn, "structural_support_bounce", {"structural_zones.nearest_support_distance_atr": [0.5]})
    res = run_candidate_backtests(conn, cand_ids, params=load_params(conn))
    assert res[0]["status"] == "unsupported_feature" and res[0]["trial_number"] is None
    row = list_candidate_backtests(conn, cand_ids[0])[0]
    assert "structural_zones" in row["detail"] and row["metrics"] is None


def test_breakout_family_uses_structural_events_and_invalidation(conn):
    _seed_universe(conn, _days_to(HOLDOUT_START))
    with conn.cursor() as cur:
        for i, sym in enumerate(("AAA", "BBB", "CCC")):
            for k, day in enumerate(range(230, _days_to(HOLDOUT_START) - 30, 25)):
                etype = "breakout" if k % 2 == 0 else "failed_breakout"
                d = START + timedelta(days=day + i)
                cur.execute("""
                    INSERT INTO structural_events
                        (symbol, timeframe, event_type, reference_type, reference_id, event_time, confirmation_time, metadata)
                    VALUES (%s, 'daily', %s, 'zone', %s, %s, %s, '{}')
                """, (sym, etype, k, d, d))
    conn.commit()
    _, cand_ids = generate_candidates(conn, "structural_breakout_momentum", {})
    res = run_candidate_backtests(conn, cand_ids, params=load_params(conn))
    row = list_candidate_backtests(conn, cand_ids[0])[0]
    assert res[0]["status"] in ("complete", "insufficient_trades")
    assert row["run_config"]["exit_family"] == "breakout"
    assert row["metrics"]["trades"] > 0
    assert "invalidation" in row["metrics"]["exit_reasons"]


def test_unknown_candidate_is_not_found_and_writes_nothing(conn):
    assert run_candidate_backtests(conn, [424242], params=load_params(conn)) == [
        {"candidate_id": 424242, "backtest_id": None, "status": "not_found", "trial_number": None}]
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM candidate_backtests")
        assert cur.fetchone()[0] == 0

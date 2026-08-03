"""
End-to-end test of Platform Improvements PR B's lifecycle-based expectancy
segments, against a real Postgres connection -- ingest/postmortem.py had
zero test coverage before this PR. Exercises the real pipeline: insert
trades (linked to trade_proposals with exit_reason, and a signal_outcomes
row for market_regime) -> build_position_lifecycles(conn) materializes
position_lifecycles/position_trades -> run_postmortem_review(conn) joins
them back and persists metric_summary to strategy_review_proposals.

Distinct from tests/test_postmortem.py's ambit if one is ever added for the
signal-level buckets -- this file is scoped specifically to the new
lifecycle_*-prefixed segments this PR adds.
"""

import sys
from datetime import datetime, timedelta, timezone

import pytest


def _import_ingest_modules():
    import pathlib
    ingest_dir = str(pathlib.Path(__file__).resolve().parent.parent / "ingest")
    if ingest_dir not in sys.path:
        sys.path.insert(0, ingest_dir)
    sys.modules.pop("build_position_lifecycles", None)
    sys.modules.pop("postmortem", None)
    from build_position_lifecycles import build_position_lifecycles
    from postmortem import run_postmortem_review
    return build_position_lifecycles, run_postmortem_review


def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug='mean_reversion'")
        return cur.fetchone()[0]


def _insert_proposal(conn, symbol, side, qty, exit_reason=None):
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, exit_reason, thesis_id)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, (symbol, side, qty, exit_reason, thesis_id))
        proposal_id = cur.fetchone()[0]
    conn.commit()
    return proposal_id


def _insert_signal_outcome(conn, symbol, proposal_id, market_regime):
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO signal_outcomes (symbol, side, proposal_id, market_regime, thesis_id)
            VALUES (%s, 'buy', %s, %s, %s)
        """, (symbol, proposal_id, market_regime, thesis_id))
    conn.commit()


def _insert_universe(conn, symbol, sector):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO universe (symbol, sector) VALUES (%s, %s)
            ON CONFLICT (symbol) DO UPDATE SET sector = EXCLUDED.sector
        """, (symbol, sector))
    conn.commit()


def _insert_trade(conn, symbol, side, qty, price, cost, traded_at, proposal_id=None, initial_stop_price=None):
    thesis_id = _mean_reversion_thesis_id(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trades (symbol, side, qty, price, notional, traded_at, cost,
                                 status, thesis_id, proposal_id, initial_stop_price)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'filled', %s, %s, %s)
        """, (symbol, side, qty, price, qty * price, traded_at, cost, thesis_id, proposal_id, initial_stop_price))
    conn.commit()


def test_lifecycle_expectancy_segments_land_in_metric_summary(conn):
    build_position_lifecycles, run_postmortem_review = _import_ingest_modules()

    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _insert_universe(conn, "AAPL", "Technology")

    buy_proposal = _insert_proposal(conn, "AAPL", "buy", 10)
    _insert_signal_outcome(conn, "AAPL", buy_proposal, "bullish")
    sell_proposal = _insert_proposal(conn, "AAPL", "sell", 10, exit_reason="thesis_complete")

    # initial_stop_price=90 -> risk_per_share=10 -> realized_r = net_pnl / (10*10) = net_pnl/100
    _insert_trade(conn, "AAPL", "buy", 10, 100.0, 0.0, base, proposal_id=buy_proposal, initial_stop_price=90.0)
    _insert_trade(conn, "AAPL", "sell", 10, 120.0, 0.0, base + timedelta(days=3), proposal_id=sell_proposal)

    build_position_lifecycles(conn)
    result = run_postmortem_review(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT metric_summary FROM strategy_review_proposals ORDER BY created_at DESC LIMIT 1")
        metric_summary = cur.fetchone()[0]

    assert "Trade-level expectancy: 1 closed position(s)" in result["finding"]

    thesis_stats = metric_summary["lifecycle_thesis"]["mean_reversion"]
    assert thesis_stats["n"] == 1
    assert thesis_stats["expectancy_dollars"] == 200.0   # 10 * (120 - 100)
    assert thesis_stats["n_with_r"] == 1
    assert thesis_stats["expectancy_r"] == 2.0            # 200 / (10 * 10)
    assert thesis_stats["sample_quality"] == "insufficient"   # n=1

    assert metric_summary["lifecycle_symbol"]["AAPL"]["n"] == 1
    assert metric_summary["lifecycle_exit_reason"]["thesis_complete"]["n"] == 1
    assert metric_summary["lifecycle_market_regime"]["bullish"]["n"] == 1
    assert metric_summary["lifecycle_sector"]["Technology"]["n"] == 1
    assert metric_summary["lifecycle_holding_period"]["1-5d"]["n"] == 1   # 3-day hold
    assert metric_summary["lifecycle_calendar_period"]["2026-06"]["n"] == 1


def test_lifecycle_segments_present_even_when_signal_side_is_insufficient(conn):
    """The two data sources are independent -- a quiet week for new signals
    must not suppress the trade-level expectancy segments, which come from
    a completely different table (position_lifecycles, not signal_outcomes)."""
    build_position_lifecycles, run_postmortem_review = _import_ingest_modules()
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)

    # No signal_outcomes rows inserted at all -> n_resolved=0, well below
    # postmortem.py's own MIN_BUCKET_N -- hits the "Insufficient data" branch.
    _insert_trade(conn, "MSFT", "buy", 5, 200.0, 0.0, base)
    _insert_trade(conn, "MSFT", "sell", 5, 210.0, 0.0, base + timedelta(days=1))
    build_position_lifecycles(conn)

    result = run_postmortem_review(conn)
    assert result["n_resolved"] == 0
    assert "Insufficient data" in result["finding"]
    assert "Trade-level expectancy: 1 closed position(s)" in result["finding"]

    with conn.cursor() as cur:
        cur.execute("SELECT metric_summary FROM strategy_review_proposals ORDER BY created_at DESC LIMIT 1")
        metric_summary = cur.fetchone()[0]
    assert metric_summary["lifecycle_symbol"]["MSFT"]["n"] == 1


def test_manual_trade_with_no_linked_signal_skips_market_regime_not_other_dimensions(conn):
    build_position_lifecycles, run_postmortem_review = _import_ingest_modules()
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)

    # No proposal_id at all -- a manual trade, same as _resolve_thesis_id's
    # own fallback path in api/main.py.
    _insert_trade(conn, "TSLA", "buy", 2, 300.0, 0.0, base)
    _insert_trade(conn, "TSLA", "sell", 2, 310.0, 0.0, base + timedelta(days=1))
    build_position_lifecycles(conn)

    run_postmortem_review(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT metric_summary FROM strategy_review_proposals ORDER BY created_at DESC LIMIT 1")
        metric_summary = cur.fetchone()[0]

    assert metric_summary["lifecycle_symbol"]["TSLA"]["n"] == 1
    assert "TSLA" not in metric_summary.get("lifecycle_market_regime", {})

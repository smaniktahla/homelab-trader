#!/usr/bin/env python3
"""
Phase 1 Reconnaissance: Data Coverage & Strategy Tagging

Goals:
- Determine usable date range of signal_outcomes
- Verify strategy tagging via thesis_id
- Assess data quality/completeness
- Identify gaps before macro data integration

Run this before acquiring macro data. Report findings back before proceeding.
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta


def get_db_connection():
    """Connect to invest database."""
    db_url = os.environ.get("DATABASE_URL", "postgresql://invest@10.10.10.201:5432/invest")
    conn = psycopg2.connect(db_url)
    return conn


def get_signal_outcomes_date_range(conn):
    """Find min/max dates in signal_outcomes."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT
                MIN(generated_at) as min_date,
                MAX(generated_at) as max_date,
                COUNT(*) as total_signals
            FROM signal_outcomes
        """)
        result = cur.fetchone()
    return result


def get_strategy_breakdown(conn):
    """Count signals per strategy (thesis)."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT
                t.slug,
                t.display_name,
                COUNT(*) as signal_count,
                COUNT(CASE WHEN proposal_status = 'proposed' THEN 1 END) as proposed_count,
                COUNT(CASE WHEN forward_return_20d IS NOT NULL THEN 1 END) as with_outcomes
            FROM signal_outcomes so
            LEFT JOIN theses t ON so.thesis_id = t.id
            GROUP BY t.id, t.slug, t.display_name
            ORDER BY signal_count DESC
        """)
        results = cur.fetchall()
    return results


def get_market_regime_history_coverage(conn):
    """Check market_regime_history date range."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT
                MIN(trading_date) as min_date,
                MAX(trading_date) as max_date,
                COUNT(*) as total_days
            FROM market_regime_history
        """)
        result = cur.fetchone()
    return result


def get_overlap_analysis(conn):
    """How much signal_outcomes overlap with market_regime_history?"""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT
                COUNT(*) as total_signals,
                COUNT(CASE WHEN DATE(so.generated_at) IN (SELECT trading_date FROM market_regime_history)
                      THEN 1 END) as signals_with_market_regime,
                COUNT(CASE WHEN DATE(so.generated_at) IN (SELECT trading_date FROM market_regime_history)
                      AND so.forward_return_20d IS NOT NULL THEN 1 END) as signals_with_outcomes_and_market_regime
            FROM signal_outcomes so
        """)
        result = cur.fetchone()
    return result


def main():
    conn = get_db_connection()

    print("=" * 80)
    print("MACRO REGIME OVERLAY — RECONNAISSANCE REPORT")
    print("=" * 80)

    print("\n1. signal_outcomes Date Range")
    print("-" * 80)
    date_range = get_signal_outcomes_date_range(conn)
    if date_range['total_signals'] > 0:
        print(f"  Min date:        {date_range['min_date']}")
        print(f"  Max date:        {date_range['max_date']}")
        print(f"  Total signals:   {date_range['total_signals']}")
        span = (date_range['max_date'] - date_range['min_date']).days
        print(f"  Span:            {span} days")
    else:
        print("  ⚠ No signal_outcomes data found")

    print("\n2. Strategy Breakdown (via thesis_id)")
    print("-" * 80)
    strategies = get_strategy_breakdown(conn)
    if strategies:
        for row in strategies:
            print(f"\n  {row['display_name']} ({row['slug']}):")
            print(f"    Total signals:        {row['signal_count']}")
            print(f"    Proposed trades:      {row['proposed_count']}")
            print(f"    With 20d outcomes:    {row['with_outcomes']}")
    else:
        print("  ⚠ No strategies found or thesis_id not populated")

    print("\n3. market_regime_history Coverage")
    print("-" * 80)
    mrh_range = get_market_regime_history_coverage(conn)
    if mrh_range['total_days'] > 0:
        print(f"  Min date:        {mrh_range['min_date']}")
        print(f"  Max date:        {mrh_range['max_date']}")
        print(f"  Total days:      {mrh_range['total_days']}")
    else:
        print("  ⚠ No market_regime_history data found")

    print("\n4. Overlap: signal_outcomes ∩ market_regime_history")
    print("-" * 80)
    overlap = get_overlap_analysis(conn)
    print(f"  Total signals:                      {overlap['total_signals']}")
    print(f"  With market_regime available:       {overlap['signals_with_market_regime']}")
    print(f"  With outcomes + market_regime:      {overlap['signals_with_outcomes_and_market_regime']}")
    if overlap['total_signals'] > 0:
        pct = 100.0 * overlap['signals_with_market_regime'] / overlap['total_signals']
        print(f"  Coverage %:                         {pct:.1f}%")

    print("\n" + "=" * 80)
    print("END RECONNAISSANCE")
    print("=" * 80)

    conn.close()


if __name__ == "__main__":
    main()

"""Shared helper for backtest_*.py research scripts: persist a run's
results to backtest_results so they survive container restarts and have
somewhere for a future dashboard tab to read from -- previously every
script only wrote JSON to /tmp, wiped on every restart, with zero UI
surface. See 2026-07-22 DocMost session notes.

Opens its own short-lived connection rather than taking one from the
caller, since each script's main `conn` is typically already closed by the
time the report is assembled (data loading finishes well before the report
dict is built)."""

import json
import os

import psycopg2


def save_backtest_result(experiment_id, git_commit, results, summary=None):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO backtest_results (experiment_id, git_commit, summary, results)
                VALUES (%s, %s, %s, %s)
            """, (experiment_id, git_commit, summary, json.dumps(results, default=str)))
        conn.commit()
    finally:
        conn.close()

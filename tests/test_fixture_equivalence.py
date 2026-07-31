"""
The PR #1 "no behavior change" acceptance test. Runs compute_signals()
against the pre-PR#1 code (pulled straight from `git show main:shared/signals.py`,
executed as an isolated module) and the post-PR#1 code (the current
shared/signals.py on disk), against an identical frozen fixture — same DB
rows, same signal_params, same mocked Yahoo/Alpaca HTTP responses — and
diffs every existing column of signals/trade_proposals/signal_outcomes.

Only the new PR #1 columns/table (symbol_features, and signal_outcomes'
technical_score/data_confidence/feature_version/model_version/
component_weights/feature_snapshot_id/vetoes) are allowed to differ.
Everything that existed before this PR — score, rationale, qty, decision,
block_reason — must be byte-identical.
"""

import subprocess
import sys
import types
from datetime import datetime, timezone, timedelta

import requests_mock

REPO_ROOT = None  # set in conftest via sys.path; recomputed here for git show


def _repo_root():
    global REPO_ROOT
    if REPO_ROOT is None:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                              cwd=__file__.rsplit("/tests/", 1)[0],
                              capture_output=True, text=True, check=True)
        REPO_ROOT = out.stdout.strip()
    return REPO_ROOT


def _load_pre_pr1_signals_module():
    """Exec `main`'s shared/signals.py (before this PR) as an isolated
    module, distinct from the `signals` module already on sys.path (which
    is the current, post-PR#1 file on disk). Both resolve their `from
    earnings import ...` / `from circuit_breaker import ...` against the
    same already-imported modules on sys.path, since those files are
    unchanged by this PR — only signals.py itself differs."""
    src = subprocess.run(
        ["git", "show", "main:shared/signals.py"],
        cwd=_repo_root(), capture_output=True, text=True, check=True,
    ).stdout
    module = types.ModuleType("signals_pre_pr1")
    sys.modules["signals_pre_pr1"] = module
    exec(compile(src, "signals_pre_pr1.py", "exec"), module.__dict__)
    return module


def _make_yahoo_chart_json(closes):
    n = len(closes)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamps = [int((start + timedelta(days=i)).timestamp()) for i in range(n)]
    return {
        "chart": {
            "result": [{
                "timestamp": timestamps,
                "indicators": {
                    "quote": [{
                        "open": closes, "high": [c * 1.01 for c in closes],
                        "low": [c * 0.99 for c in closes], "close": closes,
                        "volume": [1_000_000] * n,
                    }],
                    "adjclose": [{"adjclose": closes}],
                },
            }]
        }
    }


def _aapl_closes():
    # 50 days flat-ish (keeps the 200/50-day regime read as "ranging",
    # which score_signal() boosts by 1.15x), then a sharp multi-day drop
    # into deeply-oversold RSI + well below the lower Bollinger Band. This
    # is tuned to clear score_proposal_min (65) so the fixture exercises
    # the full gating cascade, including calc_buy_qty position sizing —
    # not just the "blocked below threshold" branch.
    closes = [180 + 0.05 * ((-1) ** i) for i in range(45)]
    closes += [closes[-1] * f for f in (0.95, 0.90, 0.85, 0.80, 0.76)]
    return closes


def _spy_closes_db_rows():
    # Flat-ish SPY for relative-strength — 30 daily rows.
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return [(base + timedelta(days=i), 450 + (i % 3) * 0.5) for i in range(30)]


def _seed_fixture(conn, aapl_closes):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO watchlist (symbol, name) VALUES ('AAPL','Apple Inc') "
                     "ON CONFLICT DO NOTHING")
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        for i, close in enumerate(aapl_closes[-30:]):
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume, adjclose)
                VALUES ('AAPL', %s, %s, %s, %s, %s, 1000000, %s)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, (base + timedelta(days=i), close, close * 1.01, close * 0.99, close, close))
        for ts, close in _spy_closes_db_rows():
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume, adjclose)
                VALUES ('SPY', %s, %s, %s, %s, %s, 5000000, %s)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, (ts, close, close * 1.01, close * 0.99, close, close))
    conn.commit()


def _reset_dynamic_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""TRUNCATE symbol_features, signal_outcomes, signals, trade_proposals,
                        trades, portfolio_snapshots, price_history, watchlist
                        RESTART IDENTITY CASCADE""")
    conn.commit()


def _mock_http(m, alpaca_base, aapl_closes):
    m.get(f"https://query2.finance.yahoo.com/v8/finance/chart/AAPL",
          json=_make_yahoo_chart_json(aapl_closes))
    m.get(f"{alpaca_base}/v2/account",
          json={"cash": "10000", "portfolio_value": "10000"})
    m.get(f"{alpaca_base}/v2/positions", json=[])


def _snapshot_state(conn):
    """Existing (pre-PR#1) columns only — the new PR #1 columns are
    deliberately excluded since they're allowed to differ."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, signal_type, score, rationale FROM signals
            ORDER BY symbol, signal_type
        """)
        signals_rows = cur.fetchall()

        cur.execute("""
            SELECT symbol, side, qty, rationale, signal_score, exit_reason, decision
            FROM trade_proposals ORDER BY symbol, side
        """)
        proposal_rows = cur.fetchall()

        cur.execute("""
            SELECT symbol, side, score, rsi, bb_upper, bb_middle, bb_lower, band_std,
                   market_regime, symbol_regime, price_at_signal, proposal_status,
                   block_reason, approval_status
            FROM signal_outcomes ORDER BY symbol, side
        """)
        outcome_rows = cur.fetchall()
    return {"signals": signals_rows, "proposals": proposal_rows, "outcomes": outcome_rows}


def _run_compute_signals(module, conn, alpaca_base, aapl_closes):
    with requests_mock.Mocker() as m:
        _mock_http(m, alpaca_base, aapl_closes)
        module.compute_signals(conn, ["AAPL"])


def test_proposal_behavior_unchanged_by_pr1(conn, monkeypatch):
    alpaca_base = "https://fake-alpaca.test"
    monkeypatch.setenv("ALPACA_BASE_URL", alpaca_base)

    aapl_closes = _aapl_closes()

    pre = _load_pre_pr1_signals_module()
    import signals as post  # the current, post-PR#1 module already on sys.path

    # Both modules read ALPACA_BASE at *import* time into a module-level
    # constant; the pre module was just exec'd after the env var was set
    # above, so it already captured the right value. The post module may
    # have been imported earlier in the test session (before this env var
    # was set) by another test module, so pin its constant explicitly to
    # keep the comparison honest without relying on import order.
    pre.ALPACA_BASE = alpaca_base
    post.ALPACA_BASE = alpaca_base
    pre.ALPACA_HEADERS = {"APCA-API-KEY-ID": "", "APCA-API-SECRET-KEY": ""}
    post.ALPACA_HEADERS = {"APCA-API-KEY-ID": "", "APCA-API-SECRET-KEY": ""}

    _reset_dynamic_tables(conn)
    _seed_fixture(conn, aapl_closes)
    _run_compute_signals(pre, conn, alpaca_base, aapl_closes)
    pre_state = _snapshot_state(conn)

    _reset_dynamic_tables(conn)
    _seed_fixture(conn, aapl_closes)
    _run_compute_signals(post, conn, alpaca_base, aapl_closes)
    post_state = _snapshot_state(conn)

    assert pre_state["signals"], "fixture must actually produce at least one signal to be meaningful"
    assert pre_state == post_state

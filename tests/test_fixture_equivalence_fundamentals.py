"""
PR #2 (fundamentals shadow mode)'s "no behavior change" acceptance test --
same discipline as PR #1's test_fixture_equivalence.py, but diffed against
THIS branch's actual parent (feat/symbol-features-shadow-mode), not main.
PR #1 already changed compute_signals() vs main; this test only needs to
prove PR #2 doesn't change it further on top of that baseline.

Also proves the new code path actually runs (not just that it's harmless):
fundamental_facts is seeded so compute_fundamental_score() returns a real
value, and the post-PR#2 run's signal_outcomes.fundamental_score is
asserted non-NULL while every pre-existing decision-relevant column stays
byte-identical to the pre-PR#2 run.
"""

import subprocess
import sys
import types
from datetime import datetime, timezone, timedelta

import requests_mock

REPO_ROOT = None


def _repo_root():
    global REPO_ROOT
    if REPO_ROOT is None:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                              cwd=__file__.rsplit("/tests/", 1)[0],
                              capture_output=True, text=True, check=True)
        REPO_ROOT = out.stdout.strip()
    return REPO_ROOT


def _resolve_pre_pr2_ref():
    """feat/symbol-features-shadow-mode (PR #1's branch, which PR #2 was
    originally stacked on) was deleted from origin after PR #1 squash-merged
    into main as 6ed6abab99e7a2e0d89ce75a5c9beb4c89c874ad on 2026-08-01 --
    so neither the local branch nor its origin ref resolves in any fresh
    clone anymore. That squash-merge commit's tree is identical to the old
    branch tip's, so it stands in as the pre-PR#2 baseline for
    shared/signals.py. Keep the deleted branch name as a first try in case
    a long-lived local checkout still has it, then fall back to the pinned
    SHA that stands in for it."""
    for ref in (
        "feat/symbol-features-shadow-mode",
        "origin/feat/symbol-features-shadow-mode",
        "6ed6abab99e7a2e0d89ce75a5c9beb4c89c874ad",
    ):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=_repo_root(), capture_output=True, text=True,
        )
        if result.returncode == 0:
            return ref
    raise RuntimeError(
        "Neither 'feat/symbol-features-shadow-mode', its origin ref, nor the pinned "
        "pre-PR#2 SHA (6ed6abab99e7a2e0d89ce75a5c9beb4c89c874ad, PR #1's squash-merge "
        "commit on main, standing in for the deleted branch) resolves in this "
        "checkout -- can't locate the pre-PR#2 baseline for shared/signals.py."
    )


def _load_pre_pr2_signals_module():
    ref = _resolve_pre_pr2_ref()
    src = subprocess.run(
        ["git", "show", f"{ref}:shared/signals.py"],
        cwd=_repo_root(), capture_output=True, text=True, check=True,
    ).stdout
    module = types.ModuleType("signals_pre_pr2")
    sys.modules["signals_pre_pr2"] = module
    exec(compile(src, "signals_pre_pr2.py", "exec"), module.__dict__)
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
    # Same deterministic shape as PR #1's fixture: mild oscillation, then
    # a sharp multi-day drop -> deeply oversold RSI + below the lower BB.
    closes = [180 + 0.05 * ((-1) ** i) for i in range(45)]
    closes += [closes[-1] * f for f in (0.95, 0.90, 0.85, 0.80, 0.76)]
    return closes


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
        base_spy = datetime(2026, 6, 1, tzinfo=timezone.utc)
        for i in range(30):
            close = 450 + (i % 3) * 0.5
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume, adjclose)
                VALUES ('SPY', %s, %s, %s, %s, %s, 5000000, %s)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, (base_spy + timedelta(days=i), close, close * 1.01, close * 0.99, close, close))

        # Fundamentals data -- present so compute_fundamental_score() has
        # something real to return in the post-PR#2 run. The pre-PR#2 run
        # never reads this table at all (it doesn't import shared/fundamentals.py),
        # so its presence can't affect that run's results.
        cur.execute("""
            INSERT INTO fundamental_facts
                (symbol, metric, value, unit, fiscal_period, period_start, period_end,
                 filed_at, accepted_at, form_type, accession_number, source)
            VALUES
                ('AAPL', 'Revenues', 900, 'USD', 'Q1-2025', '2025-01-01', '2025-03-31',
                 '2025-04-15', '2025-04-15', '10-Q', 'acc-prior', 'sec_edgar'),
                ('AAPL', 'Revenues', 1100, 'USD', 'Q1-2026', '2026-01-01', '2026-03-31',
                 '2026-04-15', '2026-04-15', '10-Q', 'acc-current', 'sec_edgar'),
                ('AAPL', 'GrossProfit', 500, 'USD', 'Q1-2026', '2026-01-01', '2026-03-31',
                 '2026-04-15', '2026-04-15', '10-Q', 'acc-gp', 'sec_edgar')
            ON CONFLICT DO NOTHING
        """)
    conn.commit()


def _reset_dynamic_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""TRUNCATE symbol_features, signal_outcomes, signals, trade_proposals,
                        trades, portfolio_snapshots, price_history, watchlist, fundamental_facts
                        RESTART IDENTITY CASCADE""")
    conn.commit()


def _set_signal_param(conn, key, value, description=""):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO signal_params (key, value, description) VALUES (%s, %s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, value, description))
    conn.commit()


def _mock_http(m, alpaca_base, aapl_closes):
    m.get("https://query2.finance.yahoo.com/v8/finance/chart/AAPL",
          json=_make_yahoo_chart_json(aapl_closes))
    m.get(f"{alpaca_base}/v2/account", json={"cash": "10000", "portfolio_value": "10000"})
    m.get(f"{alpaca_base}/v2/positions", json=[])


def _snapshot_state(conn):
    """Existing (pre-PR#2) columns only, exactly as PR #1's own equivalence
    test does -- fundamental_score is deliberately excluded here (checked
    separately below) since it's allowed, expected, to differ."""
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


def _fundamental_scores(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol, side, fundamental_score FROM signal_outcomes ORDER BY symbol, side")
        return cur.fetchall()


def _run_compute_signals(module, conn, alpaca_base, aapl_closes):
    with requests_mock.Mocker() as m:
        _mock_http(m, alpaca_base, aapl_closes)
        module.compute_signals(conn, ["AAPL"])


def _prepare_modules(monkeypatch, alpaca_base):
    monkeypatch.setenv("ALPACA_BASE_URL", alpaca_base)
    pre = _load_pre_pr2_signals_module()
    import signals as post  # current, post-PR#2 module already on sys.path
    for mod in (pre, post):
        mod.ALPACA_BASE = alpaca_base
        mod.ALPACA_HEADERS = {"APCA-API-KEY-ID": "", "APCA-API-SECRET-KEY": ""}
    return pre, post


def test_fundamentals_attachment_does_not_change_existing_columns(conn, monkeypatch):
    alpaca_base = "https://fake-alpaca.test"
    aapl_closes = _aapl_closes()
    pre, post = _prepare_modules(monkeypatch, alpaca_base)

    _reset_dynamic_tables(conn)
    _set_signal_param(conn, "score_proposal_min", 65, "default -- this scenario must stay blocked")
    _seed_fixture(conn, aapl_closes)
    _run_compute_signals(pre, conn, alpaca_base, aapl_closes)
    pre_state = _snapshot_state(conn)
    pre_fundamental_scores = _fundamental_scores(conn)

    _reset_dynamic_tables(conn)
    _set_signal_param(conn, "score_proposal_min", 65, "default -- this scenario must stay blocked")
    _seed_fixture(conn, aapl_closes)
    _run_compute_signals(post, conn, alpaca_base, aapl_closes)
    post_state = _snapshot_state(conn)
    post_fundamental_scores = _fundamental_scores(conn)

    assert pre_state["signals"], "fixture must actually produce at least one signal to be meaningful"
    assert pre_state == post_state

    # The new code path must actually run (not just be harmless): pre-PR#2
    # code never touches fundamental_score at all; post-PR#2 must populate
    # it with a real value given the seeded fundamental_facts above.
    assert all(row[2] is None for row in pre_fundamental_scores)
    assert any(row[2] is not None for row in post_fundamental_scores)

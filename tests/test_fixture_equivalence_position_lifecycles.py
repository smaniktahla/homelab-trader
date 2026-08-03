"""
Platform Improvements PR A's "no behavior change" acceptance test for
shared/signals.py. Runs compute_signals() against the pre-PR code (pulled
straight from `git show main:shared/signals.py`, executed as an isolated
module) and the post-PR code (the current shared/signals.py on disk),
against an identical frozen fixture, and diffs every existing column of
trade_proposals. Only the four new planned_* columns are allowed to
differ -- everything that existed before this PR (symbol, side, qty,
rationale, signal_score, exit_reason, decision) must be byte-identical.

Mirrors tests/test_fixture_equivalence.py's structure and helpers exactly
(separate, self-contained copies per that file's own established
convention -- see test_fixture_equivalence_fundamentals.py for the same
pattern applied to PR #3).
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


def _resolve_pre_pr_a_ref():
    """This branch (feat/position-lifecycles) was cut directly from main,
    not stacked on another PR's branch -- unlike the fundamentals PR
    earlier this session, there's no deleted-branch risk here. Still try
    both local and origin-tracking refs, matching the established pattern,
    since a plain `git clone` only ever gets a local branch for whatever
    ref was checked out."""
    for ref in ("main", "origin/main"):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=_repo_root(), capture_output=True, text=True,
        )
        if result.returncode == 0:
            return ref
    raise RuntimeError(
        "Neither 'main' nor 'origin/main' resolves in this checkout -- "
        "can't locate the pre-PR-A baseline for shared/signals.py."
    )


def _load_pre_pr_a_signals_module():
    ref = _resolve_pre_pr_a_ref()
    src = subprocess.run(
        ["git", "show", f"{ref}:shared/signals.py"],
        cwd=_repo_root(), capture_output=True, text=True, check=True,
    ).stdout
    module = types.ModuleType("signals_pre_pr_a")
    sys.modules["signals_pre_pr_a"] = module
    exec(compile(src, "signals_pre_pr_a.py", "exec"), module.__dict__)
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
    closes = [180 + 0.05 * ((-1) ** i) for i in range(45)]
    closes += [closes[-1] * f for f in (0.95, 0.90, 0.85, 0.80, 0.76)]
    return closes


def _spy_closes_db_rows():
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


def _set_signal_param(conn, key, value, description=""):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO signal_params (key, value, description) VALUES (%s, %s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, value, description))
    conn.commit()


def _prepare_modules(monkeypatch, alpaca_base):
    monkeypatch.setenv("ALPACA_BASE_URL", alpaca_base)
    pre = _load_pre_pr_a_signals_module()
    import signals as post

    for mod in (pre, post):
        mod.ALPACA_BASE = alpaca_base
        mod.ALPACA_HEADERS = {"APCA-API-KEY-ID": "", "APCA-API-SECRET-KEY": ""}
    return pre, post


def _mock_http(m, alpaca_base, aapl_closes):
    m.get(f"https://query2.finance.yahoo.com/v8/finance/chart/AAPL",
          json=_make_yahoo_chart_json(aapl_closes))
    m.get(f"{alpaca_base}/v2/account",
          json={"cash": "10000", "portfolio_value": "10000"})
    m.get(f"{alpaca_base}/v2/positions", json=[])


def _snapshot_pre_existing_columns(conn):
    """Only the trade_proposals columns that existed before this PR --
    the four new planned_* columns are deliberately excluded, same
    exclusion pattern test_fixture_equivalence.py established for PR #1's
    new columns."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, side, qty, rationale, signal_score, exit_reason, decision
            FROM trade_proposals ORDER BY symbol, side
        """)
        return cur.fetchall()


def _run_compute_signals(module, conn, alpaca_base, aapl_closes):
    with requests_mock.Mocker() as m:
        _mock_http(m, alpaca_base, aapl_closes)
        module.compute_signals(conn, ["AAPL"])


def test_proposal_path_unchanged_by_position_lifecycles_pr(conn, monkeypatch):
    """Same low-threshold fixture as test_fixture_equivalence.py's proposal
    test -- proves this PR doesn't touch qty/side/timing/decision logic,
    only adds new informational columns."""
    alpaca_base = "https://fake-alpaca.test"
    aapl_closes = _aapl_closes()
    pre, post = _prepare_modules(monkeypatch, alpaca_base)

    def _seed_low_threshold(conn):
        _seed_fixture(conn, aapl_closes)
        _set_signal_param(conn, "score_proposal_min", 10, "lowered for this fixture only")
        _set_signal_param(conn, "max_open_positions", 5)
        _set_signal_param(conn, "trade_allocation_pct", 0.05)
        _set_signal_param(conn, "max_position_pct", 0.20)
        _set_signal_param(conn, "buy_cooldown_days", 2)
        _set_signal_param(conn, "earnings_blackout_days", 3)
        _set_signal_param(conn, "circuit_breaker_drawdown_pct", 0.15)
        _set_signal_param(conn, "stop_loss_pct", 0.08)

    _reset_dynamic_tables(conn)
    _seed_low_threshold(conn)
    _run_compute_signals(pre, conn, alpaca_base, aapl_closes)
    pre_state = _snapshot_pre_existing_columns(conn)

    _reset_dynamic_tables(conn)
    _seed_low_threshold(conn)
    _run_compute_signals(post, conn, alpaca_base, aapl_closes)
    post_state = _snapshot_pre_existing_columns(conn)

    assert pre_state, "fixture must actually produce a proposal to be meaningful"
    assert pre_state == post_state

    # New fields ARE populated on the post-PR run, matching the resolved
    # design decision: derived from the same stop_loss_pct ratio, zero new
    # risk-sizing logic.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT price, qty, planned_entry_price, planned_initial_stop_price,
                   planned_risk_per_share, planned_risk_dollars
            FROM trade_proposals tp
            JOIN LATERAL (SELECT close AS price FROM price_history
                           WHERE symbol=tp.symbol ORDER BY ts DESC LIMIT 1) ph ON true
            WHERE tp.symbol='AAPL' AND tp.side='buy'
        """)
        row = cur.fetchone()
    assert row is not None
    price, qty, planned_entry, planned_stop, risk_per_share, risk_dollars = row
    assert planned_entry == price
    assert round(float(planned_stop), 6) == round(float(price) * (1 - 0.08), 6)
    assert round(float(risk_per_share), 6) == round(float(price) * 0.08, 6)
    assert round(float(risk_dollars), 4) == round(float(risk_per_share) * float(qty), 4)


def test_sell_proposals_leave_planned_fields_null(conn):
    """Sell/exit proposals are not position-opening decisions -- the four
    planned_* fields must stay NULL, not be coerced to some derived value
    that implies a fictional new entry."""
    import signals

    _reset_dynamic_tables(conn)
    _set_signal_param(conn, "stop_loss_pct", 0.08)
    p = signals.load_params(conn)
    thesis_id = signals.mean_reversion_thesis_id(conn)

    positions = {
        "AAPL": {"qty": 10, "avg_entry": 100.0, "current_price": 90.0, "unrealized_plpc": -0.10},
    }
    signals.check_stop_losses(conn, positions, p, thesis_id)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT side, planned_entry_price, planned_initial_stop_price,
                   planned_risk_per_share, planned_risk_dollars
            FROM trade_proposals WHERE symbol='AAPL'
        """)
        row = cur.fetchone()
    assert row is not None
    side, planned_entry, planned_stop, risk_per_share, risk_dollars = row
    assert side == "sell"
    assert planned_entry is None
    assert planned_stop is None
    assert risk_per_share is None
    assert risk_dollars is None

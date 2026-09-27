"""
End-to-end tests of shared/rule_adherence.py's check_gates() against a real
Postgres connection, with Alpaca's account/positions endpoints mocked --
same reasoning as tests/test_api_symbol_performance.py: catches real
RealDictCursor/SQL issues pure unit tests over plain dicts can't.

Each test seeds just enough state to make exactly one gate fail (or none),
verifying check_gates() evaluates every gate unconditionally rather than
short-circuiting at the first failure like compute_signals() itself does.
"""

import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest
import requests_mock


@pytest.fixture
def ra(_schema_ready, monkeypatch):
    """Imports shared/rule_adherence.py (and its signals.py dependency)
    fresh, with ALPACA_BASE_URL pointed at a fake host -- signals.py reads
    ALPACA_BASE_URL once at import time into a module-level constant, so
    the env var must be set before the (re-)import, same reasoning
    api/main.py's own api_client test fixture already established."""
    monkeypatch.setenv("ALPACA_BASE_URL", "https://fake-alpaca.test")
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")

    import pathlib
    shared_dir = str(pathlib.Path(__file__).resolve().parent.parent / "shared")
    if shared_dir not in sys.path:
        sys.path.insert(0, shared_dir)

    for mod in ("rule_adherence", "signals", "circuit_breaker", "earnings"):
        sys.modules.pop(mod, None)
    import rule_adherence
    return rule_adherence


def _mock_account(m, cash=50000.0, portfolio_value=100000.0):
    m.get("https://fake-alpaca.test/v2/account", json={"cash": str(cash), "portfolio_value": str(portfolio_value)})


def _mock_positions(m, positions=None):
    positions = positions or []
    m.get("https://fake-alpaca.test/v2/positions", json=positions)


def _position(symbol, qty=10, avg_entry=100.0, current_price=100.0, market_value=1000.0):
    return {
        "symbol": symbol, "qty": str(qty), "avg_entry_price": str(avg_entry),
        "current_price": str(current_price), "market_value": str(market_value),
        "unrealized_plpc": "0.0",
    }


def _rule(results, name):
    return next(r for r in results if r["rule"] == name)


def test_sell_side_only_checks_position_held(ra, conn):
    with requests_mock.Mocker() as m:
        _mock_account(m)
        _mock_positions(m, [_position("AAPL", qty=10)])
        results = ra.check_gates(conn, "AAPL", "sell", 10, 150.0)
    assert len(results) == 1
    assert results[0]["rule"] == "position_held"
    assert results[0]["passed"] is True


def test_sell_side_fails_when_not_held(ra, conn):
    with requests_mock.Mocker() as m:
        _mock_account(m)
        _mock_positions(m, [])
        results = ra.check_gates(conn, "AAPL", "sell", 10, 150.0)
    assert results[0]["passed"] is False
    assert not ra.any_violation([{"rule": "x", "passed": True, "detail": None}])
    assert ra.any_violation(results)


def test_buy_all_clear_no_violations(ra, conn):
    with requests_mock.Mocker() as m:
        _mock_account(m, cash=50000.0, portfolio_value=100000.0)
        _mock_positions(m, [])
        results = ra.check_gates(conn, "AAPL", "buy", 5, 100.0)
    assert [r["rule"] for r in results] == [
        "trading_permission", "max_open_positions", "earnings_blackout",
        "buy_cooldown", "position_sizing", "sector_cap", "cluster_cap",
    ]
    assert not ra.any_violation(results)
    assert all(r["detail"] is None for r in results)


def test_evaluates_every_gate_even_when_one_fails(ra, conn):
    """The whole point of this module vs. compute_signals()'s own
    short-circuiting gate order: a failure on gate 1 must not prevent
    the remaining gates from being evaluated and reported too."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trades (symbol, side, qty, price, notional, traded_at, cost, status, thesis_id)
            SELECT 'AAPL', 'buy', 5, 100.0, 500.0, NOW() - INTERVAL '1 hour', 0, 'filled', id
            FROM theses WHERE slug='mean_reversion'
        """)
    conn.commit()
    with requests_mock.Mocker() as m:
        _mock_account(m, cash=50000.0, portfolio_value=100000.0)
        _mock_positions(m, [])
        results = ra.check_gates(conn, "AAPL", "buy", 5, 100.0)
    assert len(results) == 7
    assert _rule(results, "buy_cooldown")["passed"] is False
    # every OTHER gate was still evaluated, not skipped
    assert _rule(results, "trading_permission")["passed"] is True
    assert _rule(results, "sector_cap")["passed"] is True
    assert _rule(results, "cluster_cap")["passed"] is True


def test_circuit_breaker_active(ra, conn):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO portfolio_snapshots (portfolio_value, high_water_mark, drawdown_pct)
            VALUES (200000, 200000, 0.0)
        """)
    conn.commit()
    with requests_mock.Mocker() as m:
        # 100k vs. a 200k high-water-mark -> 50% drawdown, well over the
        # default circuit_breaker_drawdown_pct threshold (0.15)
        _mock_account(m, cash=50000.0, portfolio_value=100000.0)
        _mock_positions(m, [])
        results = ra.check_gates(conn, "AAPL", "buy", 5, 100.0)
    cb = _rule(results, "trading_permission")
    assert cb["passed"] is False
    assert "portfolio_drawdown_limit" in cb["detail"]
    # Deliberately does NOT insert another portfolio_snapshots row --
    # current_high_water_mark() is read-only, unlike
    # record_snapshot_and_check().
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM portfolio_snapshots")
        assert cur.fetchone()[0] == 1


def test_max_open_positions_exceeded(ra, conn):
    import signals   # already imported by the `ra` fixture; DEFAULTS needs no DB row to exist
    max_open = int(signals.DEFAULTS["max_open_positions"])
    positions = [_position(f"SYM{i}", qty=1, market_value=100.0) for i in range(max_open)]
    with requests_mock.Mocker() as m:
        _mock_account(m, cash=50000.0, portfolio_value=100000.0)
        _mock_positions(m, positions)
        results = ra.check_gates(conn, "AAPL", "buy", 5, 100.0)
    assert _rule(results, "max_open_positions")["passed"] is False


def test_earnings_blackout(ra, conn):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO earnings_events (symbol, earnings_date) VALUES ('AAPL', %s)
        """, (date.today(),))
    conn.commit()
    with requests_mock.Mocker() as m:
        _mock_account(m)
        _mock_positions(m, [])
        results = ra.check_gates(conn, "AAPL", "buy", 5, 100.0)
    eb = _rule(results, "earnings_blackout")
    assert eb["passed"] is False
    assert "earnings_blackout" in eb["detail"]


def test_position_sizing_violation(ra, conn):
    with requests_mock.Mocker() as m:
        _mock_account(m, cash=50000.0, portfolio_value=100000.0)
        _mock_positions(m, [])
        # max_position_pct defaults well under 100% -- a $90k buy on a
        # $100k portfolio should blow the cap regardless of the exact
        # configured percentage.
        results = ra.check_gates(conn, "AAPL", "buy", 900, 100.0)
    sizing = _rule(results, "position_sizing")
    assert sizing["passed"] is False
    assert "position_sizing_exceeded" in sizing["detail"]


def test_sector_cap_exceeded(ra, conn):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO universe (symbol, sector) VALUES ('AAPL', 'Technology'), ('MSFT', 'Technology')
            ON CONFLICT (symbol) DO UPDATE SET sector = EXCLUDED.sector
        """)
    conn.commit()
    with requests_mock.Mocker() as m:
        _mock_account(m, cash=50000.0, portfolio_value=100000.0)
        # Already 29% of portfolio in Technology (MSFT) -- adding AAPL
        # should push the sector over its default 30% cap.
        _mock_positions(m, [_position("MSFT", qty=100, market_value=29000.0)])
        results = ra.check_gates(conn, "AAPL", "buy", 50, 100.0)
    sector = _rule(results, "sector_cap")
    assert sector["passed"] is False
    assert "sector_cap_exceeded" in sector["detail"]


def _seed_sectors(conn, sectors):
    with conn.cursor() as cur:
        for symbol, sector in sectors.items():
            cur.execute("""
                INSERT INTO universe (symbol, sector) VALUES (%s, %s)
                ON CONFLICT (symbol) DO UPDATE SET sector = EXCLUDED.sector
            """, (symbol, sector))
    conn.commit()


def test_cluster_cap_exceeded_across_sectors(ra, conn):
    """Utilities and Real Estate are separate GICS sectors, so neither
    trips the 30% per-sector cap on its own -- but together they breach
    the 25% rate-sensitivity cluster cap (cluster_cap_pct default)."""
    _seed_sectors(conn, {"DTE": "Utilities", "CPT": "Real Estate"})
    with requests_mock.Mocker() as m:
        _mock_account(m, cash=50000.0, portfolio_value=100000.0)
        # 20% already in Utilities; buying 10% of Real Estate -> 30% cluster
        _mock_positions(m, [_position("DTE", qty=200, market_value=20000.0)])
        results = ra.check_gates(conn, "CPT", "buy", 100, 100.0)
    assert _rule(results, "sector_cap")["passed"] is True
    cluster = _rule(results, "cluster_cap")
    assert cluster["passed"] is False
    assert "cluster_cap_exceeded:rate_sensitivity" in cluster["detail"]


def test_cluster_cap_passes_under_cap(ra, conn):
    _seed_sectors(conn, {"DTE": "Utilities", "CPT": "Real Estate"})
    with requests_mock.Mocker() as m:
        _mock_account(m, cash=50000.0, portfolio_value=100000.0)
        # 20% + 4% = 24%, just under the 25% cluster cap
        _mock_positions(m, [_position("DTE", qty=200, market_value=20000.0)])
        results = ra.check_gates(conn, "CPT", "buy", 40, 100.0)
    cluster = _rule(results, "cluster_cap")
    assert cluster["passed"] is True
    assert cluster["detail"] is None


def test_cluster_cap_ignores_non_cluster_symbol(ra, conn):
    """A Technology buy isn't in the rate-sensitivity cluster, so heavy
    existing cluster exposure must not block it."""
    _seed_sectors(conn, {"DTE": "Utilities", "CPT": "Real Estate", "AAPL": "Technology"})
    with requests_mock.Mocker() as m:
        _mock_account(m, cash=50000.0, portfolio_value=100000.0)
        _mock_positions(m, [
            _position("DTE", qty=200, market_value=20000.0),
            _position("CPT", qty=100, market_value=10000.0),
        ])
        results = ra.check_gates(conn, "AAPL", "buy", 50, 100.0)
    assert _rule(results, "cluster_cap")["passed"] is True


def test_cluster_cap_disabled_when_param_zero(ra, conn):
    """cluster_cap_pct = 0 disables the gate (per its schema.sql seed
    comment), even for a buy that would otherwise breach it."""
    _seed_sectors(conn, {"DTE": "Utilities", "CPT": "Real Estate"})
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM signal_params WHERE key = 'cluster_cap_pct'")
        original = cur.fetchone()[0]
        cur.execute("UPDATE signal_params SET value = 0 WHERE key = 'cluster_cap_pct'")
    conn.commit()
    try:
        with requests_mock.Mocker() as m:
            _mock_account(m, cash=50000.0, portfolio_value=100000.0)
            _mock_positions(m, [_position("DTE", qty=200, market_value=20000.0)])
            results = ra.check_gates(conn, "CPT", "buy", 100, 100.0)
        assert _rule(results, "cluster_cap")["passed"] is True
    finally:
        # signal_params is config data the conn fixture doesn't reset
        with conn.cursor() as cur:
            cur.execute("UPDATE signal_params SET value = %s WHERE key = 'cluster_cap_pct'", (original,))
        conn.commit()

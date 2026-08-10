"""
PR 7 of the Hypothesis-Driven Trading Architecture epic -- "Risk sizing
from thesis invalidation." Per docs/trade-thesis-architecture-reconciliation.md
§5's PR 7 bullet: shared/risk_engine.py::evaluate_proposal() already sizes
purely from planned_initial_stop_price + risk_per_trade_pct + open-risk cap
+ sector caps -- no new sizing engine, this PR only has to prove it
consumes a thesis-derived (PR 6, structure-aware) stop correctly. No
production code changes accompany this file.

evaluate_proposal() is already wired into compute_signals() (the
risk_decisions audit record at proposal time) and already receives
whatever `planned_initial_stop_price` ended up being computed that cycle
-- the exact same local variable PR 6 now conditionally sets from either
the flat percentage or the structure-derived stop. There is nothing new to
wire; this file cross-checks that the live path's persisted risk_decisions
row exactly matches an independent, direct evaluate_proposal() call built
from the same DB-read inputs, for both stop sources, and shows
approved_quantity actually responds to which stop was used (proving real
consumption, not a coincidental match).
"""

import sys
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import requests_mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _dir in (ROOT / "shared", ROOT / "ingest"):
    p = str(_dir)
    if p not in sys.path:
        sys.path.insert(0, p)

for _mod in ("risk_engine", "trade_thesis_stop_resolver", "market_structure", "signals"):
    sys.modules.pop(_mod, None)
import signals
import market_structure as ms
from risk_engine import evaluate_proposal, load_open_risk_dollars

_STRUCTURE_CTX = {
    "trend": "bullish", "confidence": 80, "trend_strength": "strong", "volatility": "normal",
    "bos": False, "choch": False, "risk": "low", "summary": "test",
    "monthly": {"trend_direction": "higher_highs_higher_lows"},
    "weekly": {"trend_direction": "higher_highs_higher_lows"},
    "daily": {"trend_direction": "higher_highs_higher_lows"},
}


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


def _bullish_buy_closes():
    closes = [180 + 0.05 * ((-1) ** i) for i in range(45)]
    closes += [closes[-1] * f for f in (0.95, 0.90, 0.85, 0.80, 0.76)]
    return closes


def _seed_signal_fixture(conn, symbol, closes):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO watchlist (symbol, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (symbol, symbol))
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        for i, close in enumerate(closes[-30:]):
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, 1000000)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, (symbol, base + timedelta(days=i), close, close * 1.01, close * 0.99, close))
        for i in range(30):
            spy_close = 450 + (i % 3) * 0.5
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES ('SPY', %s, %s, %s, %s, %s, 5000000)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, (base + timedelta(days=i), spy_close, spy_close * 1.01, spy_close * 0.99, spy_close))
    conn.commit()


def _set_signal_param(conn, key, value):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO signal_params (key, value, description) VALUES (%s, %s, '')
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, value))
    conn.commit()


def _set_gate_params(conn):
    # trade_allocation_pct/max_position_pct deliberately generous so the
    # strategy's own requested qty and the position/buying-power caps never
    # bind -- this test needs risk_budget_dollars/risk_per_share to be the
    # constraint that actually determines approved_quantity, so it can
    # demonstrate that quantity moving with the stop price.
    _set_signal_param(conn, "score_proposal_min", 10)
    _set_signal_param(conn, "max_open_positions", 5)
    _set_signal_param(conn, "trade_allocation_pct", 0.5)
    _set_signal_param(conn, "max_position_pct", 0.9)
    _set_signal_param(conn, "buy_cooldown_days", 2)
    _set_signal_param(conn, "earnings_blackout_days", 3)
    _set_signal_param(conn, "circuit_breaker_drawdown_pct", 0.15)
    _set_signal_param(conn, "stop_loss_pct", 0.08)


@pytest.fixture(autouse=True)
def _reset_signal_params_after(conn):
    """signal_params is deliberately NOT in conftest.py's RESET_TABLES (it's
    treated as seeded-once config, shared across the whole test session) --
    every test in this file mutates trade_allocation_pct/max_position_pct/
    stop_loss_pct/structure_aware_stop_enabled away from their defaults, so
    without this teardown those values would leak into whichever test file
    happens to run next and silently break its own default-value
    assumptions (e.g. test_rule_adherence.py's position-sizing test)."""
    yield
    _set_signal_param(conn, "trade_allocation_pct", 0.05)
    _set_signal_param(conn, "max_position_pct", 0.20)
    _set_signal_param(conn, "stop_loss_pct", 0.08)
    _set_signal_param(conn, "structure_aware_stop_enabled", 0)


@pytest.fixture
def alpaca_base(monkeypatch):
    base = "https://fake-alpaca.test"
    monkeypatch.setenv("ALPACA_BASE_URL", base)
    signals.ALPACA_BASE = base
    signals.ALPACA_HEADERS = {"APCA-API-KEY-ID": "", "APCA-API-SECRET-KEY": ""}
    return base


def _run_compute_signals(conn, alpaca_base, closes, symbol="AAPL"):
    with requests_mock.Mocker() as m:
        m.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
              json=_make_yahoo_chart_json(closes))
        m.get(f"{alpaca_base}/v2/account", json={"cash": "10000", "portfolio_value": "10000"})
        m.get(f"{alpaca_base}/v2/positions", json=[])
        signals.compute_signals(conn, [symbol])


def _fetch_proposal_and_decision(conn, symbol="AAPL"):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, qty, planned_initial_stop_price FROM trade_proposals
            WHERE symbol=%s AND side='buy'
        """, (symbol,))
        proposal_id, requested_qty, stop_price = cur.fetchone()
        cur.execute("""
            SELECT approved_quantity, outcome, risk_budget_dollars, binding_constraint
            FROM risk_decisions WHERE proposal_id=%s
        """, (proposal_id,))
        decision_row = cur.fetchone()
    assert decision_row is not None, "expected a risk_decisions row to have been recorded for this proposal"
    return {
        "proposal_id": proposal_id,
        "requested_qty": float(requested_qty),
        "stop_price": float(stop_price),
        "approved_quantity": decision_row[0],
        "outcome": decision_row[1],
        "risk_budget_dollars": float(decision_row[2]) if decision_row[2] is not None else None,
        "binding_constraint": decision_row[3],
    }


def _independent_recomputation(conn, symbol, requested_qty, stop_price):
    """Directly call evaluate_proposal() with the exact same DB-read inputs
    compute_signals() itself would have used this cycle, to cross-check
    against what got persisted -- not a mock, the real function."""
    p = signals.load_params(conn)
    sector_map = signals.load_sector_map(conn, {symbol})
    open_risk_dollars = load_open_risk_dollars(conn)
    return evaluate_proposal(
        symbol, _last_price(conn, symbol), requested_qty, stop_price,
        cash=10000.0, portfolio_value=10000.0, positions={}, sector_map=sector_map,
        open_risk_dollars=open_risk_dollars, p=p, drawdown_multiplier=1.0,
    )


def _last_price(conn, symbol):
    with conn.cursor() as cur:
        cur.execute("SELECT close FROM price_history WHERE symbol=%s ORDER BY ts DESC LIMIT 1", (symbol,))
        return float(cur.fetchone()[0])


def test_percentage_stop_risk_decision_matches_independent_recomputation(conn, alpaca_base):
    closes = _bullish_buy_closes()
    _set_gate_params(conn)
    _set_signal_param(conn, "structure_aware_stop_enabled", 0)
    _seed_signal_fixture(conn, "AAPL", closes)

    _run_compute_signals(conn, alpaca_base, closes)

    persisted = _fetch_proposal_and_decision(conn)
    recomputed = _independent_recomputation(
        conn, "AAPL", persisted["requested_qty"], persisted["stop_price"])

    assert persisted["approved_quantity"] == recomputed["approved_quantity"]
    assert persisted["outcome"] == recomputed["outcome"]
    assert persisted["risk_budget_dollars"] == pytest.approx(recomputed["risk_budget_dollars"])
    assert persisted["binding_constraint"] == recomputed["binding_constraint"]


def test_structure_stop_risk_decision_matches_independent_recomputation(conn, alpaca_base):
    closes = _bullish_buy_closes()
    _set_gate_params(conn)
    _set_signal_param(conn, "structure_aware_stop_enabled", 1)
    _seed_signal_fixture(conn, "AAPL", closes)

    price = closes[-1]
    percentage_stop = price * (1 - 0.08)
    # Tighter than the percentage stop, well within the sanity cap.
    support_price = price - (price - percentage_stop) * 0.5
    ms.store_market_structure_day(conn, date.today(), "AAPL", dict(_STRUCTURE_CTX, nearest_support={"price": support_price}))

    _run_compute_signals(conn, alpaca_base, closes)

    persisted = _fetch_proposal_and_decision(conn)
    assert persisted["stop_price"] == pytest.approx(support_price, rel=1e-6)  # sanity: PR 6 actually used it

    recomputed = _independent_recomputation(
        conn, "AAPL", persisted["requested_qty"], persisted["stop_price"])

    assert persisted["approved_quantity"] == recomputed["approved_quantity"]
    assert persisted["outcome"] == recomputed["outcome"]
    assert persisted["risk_budget_dollars"] == pytest.approx(recomputed["risk_budget_dollars"])
    assert persisted["binding_constraint"] == recomputed["binding_constraint"]


def test_tighter_structure_stop_yields_more_approved_shares_than_percentage_stop(conn, alpaca_base):
    """The real proof of consumption: risk_budget_dollars is a fixed dollar
    amount (portfolio_value * risk_per_trade_pct), so a tighter stop (less
    risk per share) must approve MORE shares for the exact same dollar
    risk, and a wider stop must approve FEWER -- if evaluate_proposal()
    weren't actually using the stop it was handed, this relationship
    wouldn't hold."""
    closes = _bullish_buy_closes()
    price = closes[-1]
    percentage_stop = price * (1 - 0.08)

    # -- Scenario A: percentage stop --
    _set_gate_params(conn)
    _set_signal_param(conn, "structure_aware_stop_enabled", 0)
    _seed_signal_fixture(conn, "AAPL", closes)
    _run_compute_signals(conn, alpaca_base, closes)
    percentage_result = _fetch_proposal_and_decision(conn)
    assert percentage_result["binding_constraint"] == "risk_budget", (
        "test fixture must be tuned so risk_budget is binding, not buying_power/"
        f"position_allocation/etc -- got {percentage_result['binding_constraint']}"
    )

    # -- Scenario B: structure stop, meaningfully tighter --
    with conn.cursor() as cur:
        cur.execute("DELETE FROM risk_decisions WHERE symbol='AAPL'")
        cur.execute("DELETE FROM signal_outcomes WHERE symbol='AAPL'")
        cur.execute("DELETE FROM trade_proposals WHERE symbol='AAPL'")
        cur.execute("DELETE FROM signals WHERE symbol='AAPL'")
    conn.commit()
    _set_signal_param(conn, "structure_aware_stop_enabled", 1)
    tight_support_price = price - (price - percentage_stop) * 0.3  # tighter than percentage
    ms.store_market_structure_day(conn, date.today(), "AAPL", dict(_STRUCTURE_CTX, nearest_support={"price": tight_support_price}))
    _run_compute_signals(conn, alpaca_base, closes)
    structure_result = _fetch_proposal_and_decision(conn)
    assert structure_result["binding_constraint"] == "risk_budget"

    assert structure_result["stop_price"] > percentage_result["stop_price"]  # tighter = closer to price
    assert structure_result["approved_quantity"] > percentage_result["approved_quantity"]
    # Dollar risk budget itself must be identical -- only risk_per_share changed.
    assert structure_result["risk_budget_dollars"] == pytest.approx(percentage_result["risk_budget_dollars"])

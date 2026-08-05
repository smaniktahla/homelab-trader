"""
Tests for shared/risk_engine.py -- the single authoritative source for
approved_quantity (see docs/risk-engine-architecture-reconciliation.md).

evaluate_proposal() is pure and gets a table covering every constraint it
can bind on, one at a time, plus the "requested is already the tightest
constraint" (fully approved) case and the "no planned stop" fallback
(risk_budget/portfolio_open_risk both skipped). load_open_risk_dollars()
is tested against a real Postgres connection, same reasoning as every
other DB-touching function in this codebase's test suite.
"""

import sys
import pathlib
from datetime import datetime, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _dir in (ROOT / "shared",):
    p = str(_dir)
    if p not in sys.path:
        sys.path.insert(0, p)

sys.modules.pop("risk_engine", None)
import risk_engine as re_mod

P = {
    "max_position_pct": 0.20,
    "sector_max_pct": 0.30,
    "risk_per_trade_pct": 0.01,
    "max_portfolio_open_risk_pct": 0.06,
}


def _base_kwargs(**overrides):
    kwargs = dict(
        symbol="AAPL", price=100.0, requested_qty=10, planned_initial_stop_price=92.0,
        cash=100_000.0, portfolio_value=100_000.0, positions={}, sector_map={},
        open_risk_dollars=0.0, p=P,
    )
    kwargs.update(overrides)
    return kwargs


def test_fully_approved_when_requested_is_tightest():
    d = re_mod.evaluate_proposal(**_base_kwargs(requested_qty=5))
    assert d["outcome"] == "approved"
    assert d["approved_quantity"] == 5
    assert d["binding_constraint"] is None


def test_reduced_by_buying_power():
    d = re_mod.evaluate_proposal(**_base_kwargs(requested_qty=10_000, cash=500.0))
    assert d["outcome"] == "reduced"
    assert d["binding_constraint"] == "buying_power"
    assert d["approved_quantity"] == 5  # floor(500/100)


def test_reduced_by_position_allocation():
    # No planned stop -> risk_budget/portfolio_open_risk are skipped, so
    # this isolates position_allocation as the only other constraint below
    # buying_power. max_position_pct 20% of 100k = 20k room; price 100 ->
    # 200 shares cap.
    d = re_mod.evaluate_proposal(**_base_kwargs(requested_qty=10_000, planned_initial_stop_price=None))
    assert d["outcome"] == "reduced"
    assert d["binding_constraint"] == "position_allocation"
    assert d["approved_quantity"] == 200


def test_position_allocation_accounts_for_existing_market_value():
    positions = {"AAPL": {"market_value": 15_000.0}}
    d = re_mod.evaluate_proposal(**_base_kwargs(
        requested_qty=10_000, positions=positions, planned_initial_stop_price=None))
    # 20k cap - 15k existing = 5k room / $100 = 50 shares
    assert d["approved_quantity"] == 50
    assert d["binding_constraint"] == "position_allocation"


def test_reduced_by_risk_budget():
    # risk_per_trade_pct 1% of 100k = $1000 budget; risk_per_share = 100-92 = 8
    # -> 125 shares. Tighter than position_allocation (200) and buying_power.
    d = re_mod.evaluate_proposal(**_base_kwargs(requested_qty=10_000))
    assert d["binding_constraint"] == "risk_budget"
    assert d["approved_quantity"] == 125


def test_drawdown_multiplier_tapers_risk_budget():
    # Same setup as test_reduced_by_risk_budget, but a 0.5 drawdown_multiplier
    # halves the $1000 budget to $500 -> 500/8 = 62 shares (Risk Engine PR 3).
    d = re_mod.evaluate_proposal(**_base_kwargs(requested_qty=10_000), drawdown_multiplier=0.5)
    assert d["binding_constraint"] == "risk_budget"
    assert d["approved_quantity"] == 62
    assert d["constraint_detail"]["risk_budget"]["drawdown_multiplier"] == 0.5


def test_drawdown_multiplier_defaults_to_full_size():
    """Existing callers/tests that don't pass drawdown_multiplier at all
    must see identical behavior to before Risk Engine PR 3."""
    with_default = re_mod.evaluate_proposal(**_base_kwargs(requested_qty=10_000))
    explicit_full = re_mod.evaluate_proposal(**_base_kwargs(requested_qty=10_000), drawdown_multiplier=1.0)
    assert with_default == explicit_full


def test_drawdown_multiplier_does_not_affect_position_allocation_or_sector():
    """Only the risk-taking knob (risk_budget) tapers under drawdown --
    position_allocation/sector_exposure are pure exposure caps, unrelated
    to recent performance (see shared/risk_engine.py's own docstring)."""
    d = re_mod.evaluate_proposal(**_base_kwargs(
        requested_qty=10_000, planned_initial_stop_price=None), drawdown_multiplier=0.5)
    assert d["constraint_detail"]["position_allocation"]["qty"] == 200
    assert d["binding_constraint"] == "position_allocation"


def test_reduced_by_portfolio_open_risk():
    # max_portfolio_open_risk_pct 6% of 100k = $6000 cap; already $5900 open
    # -> only $100 of room left / $8 risk-per-share = 12 shares.
    d = re_mod.evaluate_proposal(**_base_kwargs(requested_qty=10_000, open_risk_dollars=5900.0))
    assert d["binding_constraint"] == "portfolio_open_risk"
    assert d["approved_quantity"] == 12


def test_portfolio_open_risk_already_exhausted_rejects():
    d = re_mod.evaluate_proposal(**_base_kwargs(requested_qty=10, open_risk_dollars=6000.0))
    assert d["outcome"] == "rejected"
    assert d["binding_constraint"] == "portfolio_open_risk"
    assert d["approved_quantity"] == 0


def test_reduced_by_sector_exposure():
    sector_map = {"AAPL": "Technology", "MSFT": "Technology"}
    positions = {"MSFT": {"market_value": 29_000.0}}
    # sector_max_pct 30% of 100k = 30k cap - 29k existing = 1k room / $100 = 10 shares
    d = re_mod.evaluate_proposal(**_base_kwargs(requested_qty=10_000, sector_map=sector_map, positions=positions))
    assert d["binding_constraint"] == "sector_exposure"
    assert d["approved_quantity"] == 10


def test_no_planned_stop_skips_risk_and_open_risk_constraints():
    d = re_mod.evaluate_proposal(**_base_kwargs(requested_qty=10_000, planned_initial_stop_price=None))
    assert d["constraint_detail"]["risk_budget"] == {"skipped": "no_planned_stop_price"}
    assert d["constraint_detail"]["portfolio_open_risk"] == {"skipped": "no_planned_stop_price"}
    # Falls back to position_allocation (200) as the tightest remaining constraint.
    assert d["binding_constraint"] == "position_allocation"
    assert d["approved_quantity"] == 200


def test_stop_at_or_above_price_treated_as_no_stop():
    """A malformed/stale stop (>= entry price) must not produce a negative
    or zero risk_per_share divide -- treated the same as no stop at all."""
    d = re_mod.evaluate_proposal(**_base_kwargs(requested_qty=10_000, planned_initial_stop_price=105.0))
    assert d["constraint_detail"]["risk_budget"] == {"skipped": "no_planned_stop_price"}


def test_no_sector_mapped_skips_sector_constraint():
    d = re_mod.evaluate_proposal(**_base_kwargs(requested_qty=5, sector_map={}))
    assert d["constraint_detail"]["sector_exposure"] == {"skipped": "no_sector_mapped"}


def test_zero_cash_rejects_immediately():
    d = re_mod.evaluate_proposal(**_base_kwargs(cash=0.0))
    assert d["outcome"] == "rejected"
    assert d["binding_constraint"] == "no_portfolio_data"
    assert d["constraint_detail"] == {}


def test_sub_one_share_buying_power_rejects():
    d = re_mod.evaluate_proposal(**_base_kwargs(cash=50.0, price=100.0))
    assert d["outcome"] == "rejected"
    assert d["binding_constraint"] == "insufficient_buying_power"


def test_never_approves_more_than_requested():
    """Even with abundant room on every constraint, approved_quantity never
    exceeds the strategy's own requested_qty -- the risk engine can shrink,
    never grow, a proposal."""
    d = re_mod.evaluate_proposal(**_base_kwargs(requested_qty=3, cash=10_000_000.0, portfolio_value=10_000_000.0))
    assert d["approved_quantity"] == 3
    assert d["outcome"] == "approved"


# ─────────────────────────────────────────────────────────────────────────
# load_open_risk_dollars -- real DB
# ─────────────────────────────────────────────────────────────────────────

def _insert_lifecycle(conn, symbol, status, risk_dollars):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO position_lifecycles (symbol, status, opened_at, qty, actual_initial_risk_dollars)
            VALUES (%s, %s, %s, 10, %s)
        """, (symbol, status, datetime(2026, 1, 1, tzinfo=timezone.utc), risk_dollars))
    conn.commit()


def test_load_open_risk_dollars_sums_only_open_lifecycles(conn):
    _insert_lifecycle(conn, "AAPL", "open", 200.0)
    _insert_lifecycle(conn, "MSFT", "open", 150.0)
    _insert_lifecycle(conn, "NVDA", "closed", 999.0)  # must not count
    assert re_mod.load_open_risk_dollars(conn) == pytest.approx(350.0)


def test_load_open_risk_dollars_null_risk_contributes_zero(conn):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO position_lifecycles (symbol, status, opened_at, qty, actual_initial_risk_dollars)
            VALUES ('AAPL', 'open', %s, 10, NULL)
        """, (datetime(2026, 1, 1, tzinfo=timezone.utc),))
    conn.commit()
    _insert_lifecycle(conn, "MSFT", "open", 100.0)
    assert re_mod.load_open_risk_dollars(conn) == pytest.approx(100.0)


def test_load_open_risk_dollars_zero_when_nothing_open(conn):
    assert re_mod.load_open_risk_dollars(conn) == 0.0

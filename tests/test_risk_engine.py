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
from decimal import Decimal

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


def test_decimal_inputs_do_not_crash():
    """Regression test: api/main.py passes trade_proposals.planned_initial_stop_price
    straight from a DB row -- psycopg2 returns NUMERIC columns as
    decimal.Decimal, not float. Mixing that with a float price used to
    raise "unsupported operand type(s) for -: 'float' and
    'decimal.Decimal'" inside the risk_per_share calculation. Caught live
    in production; every numeric input gets the Decimal treatment here to
    make sure the boundary coercion in evaluate_proposal() covers all of
    them, not just the one that happened to crash first."""
    d = re_mod.evaluate_proposal(
        symbol="AAPL", price=Decimal("100.0"), requested_qty=10,
        planned_initial_stop_price=Decimal("92.0"),
        cash=Decimal("100000.0"), portfolio_value=Decimal("100000.0"),
        positions={}, sector_map={}, open_risk_dollars=Decimal("0.0"), p=P,
    )
    assert d["outcome"] == "approved"
    assert d["approved_quantity"] == 10


# ─────────────────────────────────────────────────────────────────────────
# VR-2: volatility_budget candidate (see
# docs/volatility-sizing-vr0-reconciliation.md §6.1/§6.5). P (above) has no
# volatility_* keys at all -- p.get(...) returns None/falsy for every one
# of them, which is exactly the "disabled" case these tests lean on.
# ─────────────────────────────────────────────────────────────────────────

def _enabled_p(**overrides):
    p = dict(P)
    p.update({
        "volatility_sizing_enabled": 1,
        "volatility_reference_vol": 0.25,
        "volatility_vol_floor": 0.05,
        "volatility_max_multiplier": 1.0,
    })
    p.update(overrides)
    return p


def test_disabled_volatility_overlay_is_bit_for_bit_identical_to_pre_vr2():
    """The VR-2 acceptance criterion: with the feature flag off (P has no
    volatility_sizing_enabled key at all), passing a volatility_forecast --
    even a fully valid, would-otherwise-bind one -- must not change
    anything. constraint_detail gets no 'volatility_budget' key whatsoever,
    not even an inert one; the disabled path never even looks at
    volatility_forecast."""
    forecast = {"status": "ok", "annualized_vol": 0.60, "estimator": "realized_vol"}
    without_forecast = re_mod.evaluate_proposal(**_base_kwargs(requested_qty=10_000))
    with_forecast = re_mod.evaluate_proposal(
        **_base_kwargs(requested_qty=10_000), volatility_forecast=forecast
    )
    assert without_forecast == with_forecast
    assert "volatility_budget" not in with_forecast["constraint_detail"]


def test_enabled_but_no_forecast_falls_back_to_pre_vr2_sizing():
    """Enabled, but the caller has no forecast to offer (volatility_forecast
    left at its default None) -- approved_quantity/outcome/binding_constraint
    must exactly match the disabled case; constraint_detail differs only by
    one added, inert 'volatility_budget' key (per the reconciliation doc's
    own acceptance-criterion wording: 'apart from the added inert fields')."""
    baseline = re_mod.evaluate_proposal(**_base_kwargs(requested_qty=10_000))
    d = re_mod.evaluate_proposal(**_base_kwargs(requested_qty=10_000, p=_enabled_p()))
    assert d["approved_quantity"] == baseline["approved_quantity"]
    assert d["outcome"] == baseline["outcome"]
    assert d["binding_constraint"] == baseline["binding_constraint"]
    assert d["constraint_detail"]["volatility_budget"] == {
        "skipped": "no_valid_forecast", "forecast_status": None,
    }
    del d["constraint_detail"]["volatility_budget"]
    assert d["constraint_detail"] == baseline["constraint_detail"]


def test_enabled_with_stale_forecast_falls_back():
    """Same fallback behavior for a forecast that exists but isn't status
    'ok' -- a stale/insufficient-history/failed forecast must never be
    treated as if it were a valid reading."""
    forecast = {"status": "stale", "annualized_vol": 0.40, "estimator": "realized_vol"}
    d = re_mod.evaluate_proposal(
        **_base_kwargs(requested_qty=10_000, p=_enabled_p()), volatility_forecast=forecast
    )
    assert d["constraint_detail"]["volatility_budget"] == {
        "skipped": "no_valid_forecast", "forecast_status": "stale",
    }
    assert d["binding_constraint"] != "volatility_budget"


def test_volatility_budget_reduces_size_when_forecast_above_reference():
    """forecast_vol=0.50 vs reference_vol=0.25 -> multiplier=0.5.
    base_notional = requested_qty(10) * price(100) = 1000 -> volatility_qty
    = floor(1000 * 0.5 / 100) = 5, tighter than every other constraint
    (risk_budget alone would allow 125)."""
    forecast = {"status": "ok", "annualized_vol": 0.50, "estimator": "realized_vol"}
    d = re_mod.evaluate_proposal(
        **_base_kwargs(requested_qty=10, p=_enabled_p()), volatility_forecast=forecast
    )
    assert d["binding_constraint"] == "volatility_budget"
    assert d["approved_quantity"] == 5
    assert d["constraint_detail"]["volatility_budget"]["multiplier"] == pytest.approx(0.5)
    assert d["constraint_detail"]["volatility_budget"]["estimator"] == "realized_vol"


def test_volatility_multiplier_capped_at_max_multiplier_never_increases_size():
    """forecast_vol=0.05 (== the floor) vs reference_vol=0.25 would imply a
    5x multiplier -- capped at volatility_max_multiplier=1.0, so
    volatility_budget never binds tighter than 'requested' and never scales
    a position up. This is the roadmap's own non-negotiable: reductions
    only, no leverage increase."""
    forecast = {"status": "ok", "annualized_vol": 0.05, "estimator": "realized_vol"}
    d = re_mod.evaluate_proposal(
        **_base_kwargs(requested_qty=10, p=_enabled_p()), volatility_forecast=forecast
    )
    assert d["constraint_detail"]["volatility_budget"]["multiplier"] == pytest.approx(1.0)
    assert d["outcome"] == "approved"
    assert d["approved_quantity"] == 10


def test_volatility_floor_prevents_extreme_multiplier_from_near_zero_forecast():
    """A near-zero forecast_vol (0.001) would imply an enormous multiplier
    (0.25/0.001 = 250x) without a floor -- vol_floor=0.05 caps the
    denominator, giving 0.25/0.05 = 5.0 (still further capped by
    max_multiplier, set generously high here to isolate the floor's own
    effect from the multiplier cap)."""
    forecast = {"status": "ok", "annualized_vol": 0.001, "estimator": "realized_vol"}
    d = re_mod.evaluate_proposal(
        **_base_kwargs(requested_qty=10, p=_enabled_p(volatility_max_multiplier=10.0)),
        volatility_forecast=forecast,
    )
    assert d["constraint_detail"]["volatility_budget"]["multiplier"] == pytest.approx(5.0)


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

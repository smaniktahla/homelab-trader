"""
Tests for shared/circuit_breaker.py's pure helpers -- drawdown_pct_of(),
is_breached(), and drawdown_size_multiplier() (Risk Engine PR 3). No prior
test coverage existed for this module at all; these three functions
replace what used to be three independently hand-copied drawdown formulas
across the codebase (shared/rule_adherence.py, shared/signals.py's own
record_snapshot_and_check(), and backtest_portfolio_montecarlo.py) -- see
docs/risk-engine-architecture-reconciliation.md section C.4.
"""

import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
p = str(ROOT / "shared")
if p not in sys.path:
    sys.path.insert(0, p)

sys.modules.pop("circuit_breaker", None)
import circuit_breaker as cb


def test_drawdown_pct_of_no_drawdown():
    assert cb.drawdown_pct_of(100_000, 100_000) == 0.0


def test_drawdown_pct_of_real_drawdown():
    assert cb.drawdown_pct_of(85_000, 100_000) == pytest.approx(0.15)


def test_drawdown_pct_of_above_hwm_is_negative():
    """A new all-time high (portfolio_value > hwm passed in) produces a
    negative drawdown_pct -- callers are expected to take max(hwm, value)
    before calling this, same as record_snapshot_and_check() already
    does; this function itself doesn't clamp."""
    assert cb.drawdown_pct_of(110_000, 100_000) == pytest.approx(-0.10)


def test_drawdown_pct_of_zero_hwm():
    assert cb.drawdown_pct_of(50_000, 0) == 0.0


def test_is_breached_exact_threshold_counts_as_breached():
    assert cb.is_breached(0.15, 0.15) is True


def test_is_breached_below_threshold():
    assert cb.is_breached(0.10, 0.15) is False


def test_drawdown_size_multiplier_no_drawdown_is_full_size():
    assert cb.drawdown_size_multiplier(0.0, 0.15) == 1.0


def test_drawdown_size_multiplier_at_threshold_is_floor():
    assert cb.drawdown_size_multiplier(0.15, 0.15) == pytest.approx(0.5)


def test_drawdown_size_multiplier_halfway_is_between():
    # halfway to the threshold -> halfway between 1.0 and the 0.5 floor
    assert cb.drawdown_size_multiplier(0.075, 0.15) == pytest.approx(0.75)


def test_drawdown_size_multiplier_custom_floor():
    assert cb.drawdown_size_multiplier(0.15, 0.15, floor=0.25) == pytest.approx(0.25)


def test_drawdown_size_multiplier_beyond_threshold_clamps_at_floor():
    """Should never go below floor even if drawdown_pct somehow exceeds
    the threshold for one cycle before is_breached() is observed
    elsewhere (this function never itself blocks anything -- see its
    docstring)."""
    assert cb.drawdown_size_multiplier(0.30, 0.15) == pytest.approx(0.5)


def test_drawdown_size_multiplier_zero_threshold_is_full_size():
    assert cb.drawdown_size_multiplier(0.05, 0.0) == 1.0


def test_drawdown_size_multiplier_negative_drawdown_is_full_size():
    """A new all-time high produces a negative drawdown_pct -- must not
    produce a multiplier above 1.0."""
    assert cb.drawdown_size_multiplier(-0.05, 0.15) == 1.0

"""
VR-3a of the Volatility Forecasting & Risk-Targeted Position Sizing epic:
paired-opportunity / trade-level sizing comparison. See
docs/volatility-sizing-vr0-reconciliation.md §4.1 for why this runs on
shared/backtest_engine.py directly (single-symbol, no account ledger)
rather than waiting on VR-0b's portfolio-level replay extension --
VR-3b (constrained-account replay, requiring VR-0b) is a separate,
later comparison.

The property this module exists to prove or disprove: with entry/exit
timestamps held IDENTICAL (same strategy, same bars, same
execution_timing -- run_backtest()'s determinism guarantees this
structurally, not by extra bookkeeping here), does volatility-scaled
sizing produce a better size-weighted, risk-adjusted outcome than the
current fixed-notional policy on the same set of opportunities?

Sizing math reuses shared/volatility_forecast.py's ESTIMATORS and
volatility_size_multiplier() -- the exact same formula
shared/risk_engine.py's VR-2 volatility_budget candidate uses -- so a
backtest finding here reflects the same sizing rule that would run live,
not a reimplementation that could quietly drift from it.

Fallback semantics mirror VR-2's own (see
docs/volatility-sizing-vr0-reconciliation.md §6.4): when the estimator
can't produce a valid ("ok") reading from the bars visible so far, the
volatility-scaled policy's qty_fn falls back to the SAME fixed-notional
qty the baseline policy would have used for that entry -- never a
rejection, never a substituted estimator.

This module runs the comparison; it does not draw conclusions from real
market data (no live/paper price_history run ships here) -- that
empirical step, with a registered universe/date range and out-of-sample
split per docs/volatility-sizing-vr0-reconciliation.md's VR-0 experiment
contract, is separate follow-up work once real historical data is
available to run against.
"""

import math
from dataclasses import dataclass

from backtest_engine import run_backtest
from volatility_forecast import ESTIMATORS, STATUS_OK, DEFAULTS as VOLATILITY_DEFAULTS, volatility_size_multiplier


def fixed_notional_qty_fn(base_notional):
    """The 'current policy' baseline for this single-symbol comparison:
    floor(base_notional / price) at entry, same fixed-fractional shape as
    shared/signals.py::calc_buy_qty() without the portfolio-level caps
    (buying power, position/sector limits) that don't apply to a
    single-symbol paired-opportunity comparison -- see module docstring
    and the reconciliation doc's own note that this analysis "is not a
    feasible account simulation when capital overlaps." Returns a
    qty_fn suitable for backtest_engine.run_backtest()'s callable qty."""
    def qty_fn(bars_seen, signal):
        price = bars_seen[-1].close
        return math.floor(base_notional / price) if price else 0.0
    return qty_fn


def volatility_scaled_qty_fn(base_notional, estimator="realized_vol", reference_vol=0.25,
                              vol_floor=0.05, max_multiplier=1.0, params=None):
    """Inverse-volatility-scaled qty_fn for backtest_engine.run_backtest().
    At each BUY signal, estimates daily_vol from the closes already visible
    in bars_seen (no DB access -- this stays a pure, in-memory computation
    over exactly what the strategy itself saw, preserving run_backtest()'s
    no-lookahead guarantee), annualizes it, and applies
    volatility_size_multiplier() -- the identical formula VR-2's
    risk_engine.py candidate uses. Falls back to the fixed-notional qty
    (via fixed_notional_qty_fn) when the estimator can't produce a valid
    reading, exactly mirroring VR-2's §6.4 fallback -- this policy is never
    tighter *or* looser than the fixed baseline on a bar it can't size."""
    merged_params = dict(VOLATILITY_DEFAULTS)
    if params:
        merged_params.update(params)
    params = merged_params
    if estimator not in ESTIMATORS:
        raise ValueError(f"unknown estimator {estimator!r} -- must be one of {sorted(ESTIMATORS)}")
    estimator_fn = ESTIMATORS[estimator]
    fallback = fixed_notional_qty_fn(base_notional)

    def qty_fn(bars_seen, signal):
        closes = [b.close for b in bars_seen]
        daily_vol, _n, status = estimator_fn(closes, params)
        price = bars_seen[-1].close
        if status != STATUS_OK or daily_vol is None or not price:
            return fallback(bars_seen, signal)
        annualized_vol = daily_vol * math.sqrt(params["volatility_sessions_per_year"])
        multiplier = volatility_size_multiplier(annualized_vol, reference_vol, vol_floor, max_multiplier)
        return math.floor((base_notional * multiplier) / price)

    return qty_fn


@dataclass(frozen=True)
class PairedTrade:
    """One entry/exit opportunity, sized two ways. entry_ts/exit_ts are
    identical across both policies by construction (same strategy, same
    bars) -- this is the 'paired' guarantee, not something computed or
    checked after the fact."""
    entry_ts: object
    exit_ts: object
    entry_price: float
    exit_price: float
    fixed_qty: float
    fixed_pnl: float
    volatility_qty: float
    volatility_pnl: float

    @property
    def fixed_return_pct(self):
        notional = self.fixed_qty * self.entry_price
        return (self.fixed_pnl / notional) if notional else None

    @property
    def volatility_return_pct(self):
        notional = self.volatility_qty * self.entry_price
        return (self.volatility_pnl / notional) if notional else None


@dataclass(frozen=True)
class SizingComparisonReport:
    symbol: str
    estimator: str
    fixed_result: object          # BacktestResult
    volatility_result: object     # BacktestResult
    paired_trades: list

    @property
    def trade_count(self):
        return len(self.paired_trades)

    @property
    def fixed_total_pnl(self):
        return sum(t.fixed_pnl for t in self.paired_trades)

    @property
    def volatility_total_pnl(self):
        return sum(t.volatility_pnl for t in self.paired_trades)

    @property
    def mean_fixed_return_pct(self):
        returns = [t.fixed_return_pct for t in self.paired_trades if t.fixed_return_pct is not None]
        return sum(returns) / len(returns) if returns else None

    @property
    def mean_volatility_return_pct(self):
        returns = [t.volatility_return_pct for t in self.paired_trades if t.volatility_return_pct is not None]
        return sum(returns) / len(returns) if returns else None


def compare_sizing_policies(bars, strategy, *, base_notional, estimator="realized_vol",
                             reference_vol=0.25, vol_floor=0.05, max_multiplier=1.0,
                             execution_timing="next_bar_open", params=None):
    """Run the SAME strategy over the SAME bars twice -- once under the
    fixed-notional baseline, once under volatility-scaled sizing -- and
    pair up the resulting closed trades by position (both runs see
    identical signals in identical order, since bars/strategy/
    execution_timing are all identical; only qty differs, which cannot
    change which bars are actionable). Only CLOSED trades are paired; an
    end-of-data still-open position (if any) is excluded from
    paired_trades since it has no realized pnl to compare yet -- present
    in fixed_result.trades/volatility_result.trades if a caller wants it."""
    fixed_result = run_backtest(
        bars, strategy, execution_timing=execution_timing, qty=fixed_notional_qty_fn(base_notional)
    )
    volatility_result = run_backtest(
        bars, strategy, execution_timing=execution_timing,
        qty=volatility_scaled_qty_fn(
            base_notional, estimator=estimator, reference_vol=reference_vol,
            vol_floor=vol_floor, max_multiplier=max_multiplier, params=params,
        ),
    )

    fixed_closed = [t for t in fixed_result.trades if t.status == "closed"]
    vol_closed = [t for t in volatility_result.trades if t.status == "closed"]
    if len(fixed_closed) != len(vol_closed):
        raise AssertionError(
            "paired-opportunity invariant violated: fixed and volatility-scaled runs produced a "
            f"different number of closed trades ({len(fixed_closed)} vs {len(vol_closed)}) despite "
            "identical bars/strategy/execution_timing -- qty must never affect which signals are "
            "actionable, only their size."
        )

    paired = []
    for f, v in zip(fixed_closed, vol_closed):
        if f.entry_execution_ts != v.entry_execution_ts or f.exit_execution_ts != v.exit_execution_ts:
            raise AssertionError(
                "paired-opportunity invariant violated: entry/exit timestamps diverged between "
                f"the fixed and volatility-scaled runs ({f.entry_execution_ts}/{f.exit_execution_ts} "
                f"vs {v.entry_execution_ts}/{v.exit_execution_ts})."
            )
        paired.append(PairedTrade(
            entry_ts=f.entry_execution_ts, exit_ts=f.exit_execution_ts,
            entry_price=f.entry_price, exit_price=f.exit_price,
            fixed_qty=f.qty, fixed_pnl=f.net_pnl,
            volatility_qty=v.qty, volatility_pnl=v.net_pnl,
        ))

    return SizingComparisonReport(
        symbol=bars[0].symbol if bars else "",
        estimator=estimator,
        fixed_result=fixed_result,
        volatility_result=volatility_result,
        paired_trades=paired,
    )

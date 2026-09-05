"""
VR-3b of the Volatility Forecasting & Risk-Targeted Position Sizing epic:
constrained-account replay. See docs/volatility-sizing-vr0-reconciliation.md
§4.1/§5 -- unlike VR-3a's paired-opportunity analysis (identical
entry/exit timestamps by construction, single-symbol, no ledger), this
module runs shared/portfolio_backtest_engine.py's (VR-0b) real multi-
symbol, cash-constrained ledger and explicitly expects the two policies
to admit DIFFERENT trades, not the same trades sized differently -- cash
spent on one symbol's candidate is cash unavailable for the next, so a
sizing change can ripple into which candidates even get filled at all.
Divergence is reported, never asserted away or treated as a bug.

Both policies here are literally the SAME function --
shared/risk_engine.py::evaluate_proposal(), the one authoritative sizing
decision point -- called with different `p` params (volatility_sizing_enabled
0 vs 1) and, for the volatility policy, an actual forecast computed from
whatever bars are visible so far via shared/volatility_forecast.py's
estimators. This is deliberate: VR-3b is not a second, parallel sizing
implementation for backtesting purposes, it is the real risk engine
running against synthetic/historical bars instead of live Alpaca state.
If evaluate_proposal() changes, both policies here change with it --
they cannot silently drift from what would run live.

Portfolio-level risk state the live risk engine normally gets from a DB
(open_risk_dollars via shared/risk_engine.py::load_open_risk_dollars(),
sourced from position_lifecycles) has no DB equivalent inside a backtest.
Each qty_fn factory below tracks its own risk-dollars-per-open-position
ledger instead, pruned in sync with shared/portfolio_backtest_engine.py's
own position closes (a symbol no longer in portfolio_state["positions"]
had its position closed by the engine, so its tracked risk is dropped) --
same reasoning as load_open_risk_dollars() summing
actual_initial_risk_dollars, just backed by an in-memory dict instead of
a table.

planned_initial_stop_price is estimated the same way the GET /api/advisor
bugfix does (api/main.py::_build_advisor_candidates()) -- price * (1 -
stop_loss_pct) -- since neither this module nor a backtest strategy has a
persisted trade_thesis-derived stop to read back. This is a real
approximation, not the actual stop logic compute_signals() would use in
production; it's what makes risk_per_share non-None so the risk engine's
stop-distance/portfolio-open-risk constraints actually get evaluated
instead of silently skipping, same rationale as the advisor fix.
"""

import math
from dataclasses import dataclass

from risk_engine import evaluate_proposal
from portfolio_backtest_engine import run_portfolio_backtest
from volatility_forecast import (
    DEFAULTS as VOLATILITY_DEFAULTS,
    ESTIMATORS,
    STATUS_OK,
)


def _requested_qty(price, portfolio_state, params):
    """The strategy's own pre-risk-engine sized qty, matching
    shared/signals.py::calc_buy_qty()'s fixed-fractional-of-portfolio-value
    convention -- evaluate_proposal()'s requested_qty is an
    upper bound it can only shrink, never grow, so this must be a
    reasonable starting size, not the final answer."""
    if price <= 0:
        return 0
    notional = portfolio_state["total_equity"] * params.get("trade_allocation_pct", 0.05)
    return math.floor(notional / price)


def make_current_policy_qty_fn(sector_map, params, stop_loss_pct=0.08, drawdown_multiplier=1.0):
    """The 'current policy' baseline: shared/risk_engine.py::evaluate_proposal()
    with volatility_sizing_enabled left at whatever `params` says (expected
    off, i.e. pre-VR-2 sizing) -- every other constraint (buying power,
    position/sector allocation, stop-distance risk budget, portfolio open
    risk) still applies exactly as it would live."""
    risk_dollars_by_symbol = {}

    def qty_fn(symbol, price, portfolio_state, bars_seen, side):
        for s in list(risk_dollars_by_symbol):
            if s not in portfolio_state["positions"]:
                del risk_dollars_by_symbol[s]

        requested_qty = _requested_qty(price, portfolio_state, params)
        if requested_qty <= 0:
            return 0
        planned_stop_price = price * (1 - stop_loss_pct)
        positions_by_symbol = {s: {"market_value": p["market_value"]} for s, p in portfolio_state["positions"].items()}
        open_risk_dollars = sum(risk_dollars_by_symbol.values())

        decision = evaluate_proposal(
            symbol, price, requested_qty, planned_stop_price,
            portfolio_state["cash"], portfolio_state["total_equity"],
            positions_by_symbol, sector_map, open_risk_dollars, params,
            drawdown_multiplier=drawdown_multiplier,
        )
        qty = decision["approved_quantity"]
        if qty > 0:
            risk_dollars_by_symbol[symbol] = (price - planned_stop_price) * qty
        return qty

    return qty_fn


def make_volatility_policy_qty_fn(sector_map, params, stop_loss_pct=0.08, drawdown_multiplier=1.0,
                                   estimator="realized_vol", volatility_params=None):
    """The volatility-scaled policy: the SAME evaluate_proposal() call,
    but with a real forecast computed from bars_seen (via
    shared/volatility_forecast.py's estimators, same causal, in-memory
    computation shared/sizing_policy_comparison.py's VR-3a qty_fn uses)
    passed in as volatility_forecast -- activating VR-2's volatility_budget
    candidate. `params` must have volatility_sizing_enabled=1 and the
    volatility_reference_vol/volatility_vol_floor/volatility_max_multiplier
    keys set for this to actually do anything (see risk_engine.py) -- this
    function does not set them itself, so a caller can't accidentally run
    the 'volatility policy' with the overlay silently inert."""
    if estimator not in ESTIMATORS:
        raise ValueError(f"unknown estimator {estimator!r} -- must be one of {sorted(ESTIMATORS)}")
    vol_params = dict(VOLATILITY_DEFAULTS)
    if volatility_params:
        vol_params.update(volatility_params)
    estimator_fn = ESTIMATORS[estimator]
    risk_dollars_by_symbol = {}

    def qty_fn(symbol, price, portfolio_state, bars_seen, side):
        for s in list(risk_dollars_by_symbol):
            if s not in portfolio_state["positions"]:
                del risk_dollars_by_symbol[s]

        requested_qty = _requested_qty(price, portfolio_state, params)
        if requested_qty <= 0:
            return 0
        planned_stop_price = price * (1 - stop_loss_pct)
        positions_by_symbol = {s: {"market_value": p["market_value"]} for s, p in portfolio_state["positions"].items()}
        open_risk_dollars = sum(risk_dollars_by_symbol.values())

        closes = [b.close for b in bars_seen]
        daily_vol, _n, status = estimator_fn(closes, vol_params)
        volatility_forecast = None
        if status == STATUS_OK and daily_vol is not None:
            annualized_vol = daily_vol * math.sqrt(vol_params["volatility_sessions_per_year"])
            volatility_forecast = {"status": STATUS_OK, "annualized_vol": annualized_vol, "estimator": estimator}

        decision = evaluate_proposal(
            symbol, price, requested_qty, planned_stop_price,
            portfolio_state["cash"], portfolio_state["total_equity"],
            positions_by_symbol, sector_map, open_risk_dollars, params,
            drawdown_multiplier=drawdown_multiplier, volatility_forecast=volatility_forecast,
        )
        qty = decision["approved_quantity"]
        if qty > 0:
            risk_dollars_by_symbol[symbol] = (price - planned_stop_price) * qty
        return qty

    return qty_fn


@dataclass(frozen=True)
class AccountComparisonReport:
    current_result: object       # PortfolioBacktestResult
    volatility_result: object    # PortfolioBacktestResult

    @property
    def current_trade_symbols(self):
        return {t.symbol for t in self.current_result.trades}

    @property
    def volatility_trade_symbols(self):
        return {t.symbol for t in self.volatility_result.trades}

    @property
    def symbols_only_in_current(self):
        """Symbols the current policy traded that the volatility policy
        never did -- e.g. cash spent elsewhere under one policy but not
        the other. Divergence, not a pairing failure -- expected and
        reported, per this module's own docstring."""
        return self.current_trade_symbols - self.volatility_trade_symbols

    @property
    def symbols_only_in_volatility(self):
        return self.volatility_trade_symbols - self.current_trade_symbols

    @property
    def common_symbols(self):
        return self.current_trade_symbols & self.volatility_trade_symbols

    @property
    def summary(self):
        """A single dict of the account-level metrics the roadmap's VR-3
        report section asks for -- return/drawdown/trade-count/rejected-
        candidate-count per policy, plus the trade-set divergence, all in
        one place for a caller to log/persist without reaching into both
        PortfolioBacktestResult objects by hand."""
        return {
            "current": {
                "total_return_pct": self.current_result.total_return_pct,
                "max_drawdown_pct": self.current_result.max_drawdown_pct,
                "trade_count": len(self.current_result.trades),
                "rejected_fill_count": len(self.current_result.rejected_fills),
                "final_equity": self.current_result.final_equity,
            },
            "volatility": {
                "total_return_pct": self.volatility_result.total_return_pct,
                "max_drawdown_pct": self.volatility_result.max_drawdown_pct,
                "trade_count": len(self.volatility_result.trades),
                "rejected_fill_count": len(self.volatility_result.rejected_fills),
                "final_equity": self.volatility_result.final_equity,
            },
            "trade_set_divergence": {
                "common_symbols": sorted(self.common_symbols),
                "only_in_current": sorted(self.symbols_only_in_current),
                "only_in_volatility": sorted(self.symbols_only_in_volatility),
            },
        }


def compare_account_policies(bars_by_symbol, strategy, *, starting_cash, sector_map,
                              current_params, volatility_params, stop_loss_pct=0.08,
                              execution_timing="next_bar_open", cost_model=None,
                              estimator="realized_vol", volatility_estimator_params=None):
    """Runs the SAME strategy/bars/starting_cash through
    shared/portfolio_backtest_engine.py::run_portfolio_backtest() twice --
    once under make_current_policy_qty_fn(current_params), once under
    make_volatility_policy_qty_fn(volatility_params) -- and returns an
    AccountComparisonReport. Unlike VR-3a's compare_sizing_policies(),
    this does NOT assert the two runs admit the same trades -- they are
    expected to diverge once cash competition is real, and the report's
    job is to characterize that divergence, not eliminate it.

    current_params/volatility_params are independent `p` dicts passed to
    evaluate_proposal() -- typically the same dict with only
    volatility_sizing_enabled (and its reference_vol/vol_floor/
    max_multiplier) differing between them; kept as two separate
    parameters rather than one dict + a flag so a caller can't
    accidentally run both sides with identical params and mistake "no
    divergence" for a finding."""
    current_qty_fn = make_current_policy_qty_fn(sector_map, current_params, stop_loss_pct=stop_loss_pct)
    volatility_qty_fn = make_volatility_policy_qty_fn(
        sector_map, volatility_params, stop_loss_pct=stop_loss_pct,
        estimator=estimator, volatility_params=volatility_estimator_params,
    )

    current_result = run_portfolio_backtest(
        bars_by_symbol, strategy, starting_cash=starting_cash,
        execution_timing=execution_timing, qty_fn=current_qty_fn, cost_model=cost_model,
    )
    volatility_result = run_portfolio_backtest(
        bars_by_symbol, strategy, starting_cash=starting_cash,
        execution_timing=execution_timing, qty_fn=volatility_qty_fn, cost_model=cost_model,
    )

    return AccountComparisonReport(current_result=current_result, volatility_result=volatility_result)

"""
VR-0b of the Volatility Forecasting & Risk-Targeted Position Sizing epic:
portfolio-level replay extension. See
docs/volatility-sizing-vr0-reconciliation.md §4.1/§4 -- shared/backtest_engine.py
(PR15) is deliberately single-symbol/unit-qty with no cash ledger; this
module extends the SAME primitives (Bar, Signal-equivalent, Fill, Trade --
Trade is reused verbatim, it already carries a `symbol` field for exactly
this) to a multi-symbol, dollar-denominated, cost-aware ledger, rather
than inventing a second, unrelated backtest engine. It exists specifically
to unblock VR-3b (constrained-account replay: cash competition,
concurrent positions, costs) -- VR-3a's paired-opportunity analysis does
NOT need this and runs on backtest_engine.py directly (see the
reconciliation doc's §4.1 correction against treating that "as-is" claim
as dogma).

Same no-lookahead discipline as backtest_engine.py: the strategy callable
is only ever given, for calendar date D, each symbol's bars truncated to
`bars[: i + 1]` where bar i is D's own bar for that symbol -- never a
later date. Execution timing mirrors backtest_engine.py's two modes
("next_bar_open" default, "same_bar_close" alternative), scheduled
per-symbol against that symbol's OWN next bar (not a globally shared
calendar index), since different symbols can have gaps (holidays, late
listings) on different dates.

Deterministic same-day candidate priority (a VR-3b requirement): the
strategy callable returns an ORDERED list of (symbol, side) candidates for
a given date; the engine processes them in that exact order, debiting
cash immediately after each fill so a later candidate in the same batch
sees the already-reduced balance. The strategy decides priority; the
engine never reorders or randomizes it.

Position sizing is pluggable (qty_fn), not baked in -- the whole point of
this module existing is to let VR-3b experiments swap in different sizing
policies (fixed-fractional, volatility-scaled via
shared/volatility_forecast.py, or eventually shared/risk_engine.py itself)
against the SAME account mechanics, exactly mirroring how
shared/sizing_policy_comparison.py (VR-3a) swaps qty_fn against the same
single-symbol mechanics. The default qty_fn here is a simple
fixed-fractional-of-equity policy, matching shared/signals.py::calc_buy_qty()'s
convention, not a claim about what VR-3b should actually test.

No dividend/split adjustment beyond whatever the input Bars already
contain (see docs/volatility-sizing-vr0-reconciliation.md §3.1's decision
-- dividend adjustment is a documented, out-of-scope gap, not silently
assumed solved here either).
"""

import math
from dataclasses import dataclass, field
from datetime import datetime


EXECUTION_TIMINGS = frozenset({"same_bar_close", "next_bar_open"})
SIDES = frozenset({"buy", "sell"})


@dataclass(frozen=True)
class PortfolioSignal:
    """Per-symbol decision, always recorded -- same "never silently
    dropped" convention as backtest_engine.Signal, extended with which
    symbol it's about."""
    symbol: str
    bar_ts: datetime
    side: str
    reason: str
    actionable: bool


@dataclass(frozen=True)
class PortfolioFill:
    symbol: str
    signal_ts: datetime
    execution_ts: datetime
    side: str
    price: float
    qty: float
    commission: float


@dataclass(frozen=True)
class RejectedFill:
    """An actionable signal that reached its scheduled execution date but
    could not actually be filled -- specifically, insufficient cash
    remaining (e.g. a higher-priority same-day candidate already spent
    it). Distinct from a non-actionable Signal (which was never going to
    be attempted at all): this is a candidate the strategy wanted, that
    the account genuinely could not afford. VR-3b's account-level
    reporting explicitly wants rejected-candidate visibility, not just
    successful fills."""
    symbol: str
    signal_ts: datetime
    execution_ts: datetime
    side: str
    reason: str


@dataclass(frozen=True)
class Trade:
    """Deliberately the same shape as backtest_engine.Trade (duplicated
    here rather than imported, since this module has no other dependency
    on backtest_engine.py and importing one dataclass alone isn't worth a
    cross-module coupling) -- symbol/qty/entry/exit/gross_pnl/net_pnl,
    same field names, same meaning. net_pnl includes both entry and exit
    commissions; gross_pnl does not."""
    symbol: str
    status: str
    entry_signal_ts: datetime
    entry_execution_ts: datetime
    entry_price: float
    qty: float
    exit_signal_ts: datetime | None
    exit_execution_ts: datetime | None
    exit_price: float | None
    gross_pnl: float
    net_pnl: float


@dataclass(frozen=True)
class EquityPoint:
    date: datetime
    cash: float
    positions_value: float
    total_equity: float


@dataclass(frozen=True)
class PortfolioBacktestResult:
    execution_timing: str
    starting_cash: float
    final_cash: float
    signals: list = field(default_factory=list)
    fills: list = field(default_factory=list)
    rejected_fills: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)

    @property
    def final_equity(self):
        return self.equity_curve[-1].total_equity if self.equity_curve else self.starting_cash

    @property
    def total_return_pct(self):
        if not self.starting_cash:
            return None
        return (self.final_equity - self.starting_cash) / self.starting_cash * 100.0

    @property
    def max_drawdown_pct(self):
        """Peak-to-trough decline of total_equity over the whole curve,
        as a positive percentage (0 if the curve never declined)."""
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0].total_equity
        worst = 0.0
        for point in self.equity_curve:
            peak = max(peak, point.total_equity)
            if peak > 0:
                drawdown = (peak - point.total_equity) / peak * 100.0
                worst = max(worst, drawdown)
        return worst


def fixed_fractional_qty_fn(trade_allocation_pct=0.05):
    """Default qty_fn factory: floor(total_equity * trade_allocation_pct / price),
    capped by actually-available cash -- same fixed-fractional-of-portfolio-value
    convention as shared/signals.py::calc_buy_qty(), not a claim about what
    VR-3b should test. Ignores `side`/`bars_seen` entirely (a real policy,
    e.g. a volatility-scaled one, would use them)."""
    def qty_fn(symbol, price, portfolio_state, bars_seen, side):
        if price <= 0:
            return 0.0
        notional = portfolio_state["total_equity"] * trade_allocation_pct
        qty = math.floor(notional / price)
        max_affordable = math.floor(portfolio_state["cash"] / price)
        return max(0.0, min(qty, max_affordable))
    return qty_fn


def _zero_cost_model(price, qty):
    return 0.0


def run_portfolio_backtest(bars_by_symbol, strategy, *, starting_cash,
                            execution_timing="next_bar_open", qty_fn=None, cost_model=None):
    """Multi-symbol, cash-constrained replay.

    bars_by_symbol: dict[symbol -> list[Bar]] (backtest_engine.Bar or any
    object with the same symbol/ts/open/high/low/close/volume fields),
    each list oldest->newest, one entry per that symbol's own trading
    calendar (gaps across symbols are fine -- handled per-symbol, not via
    a shared global index).

    strategy: Callable[[date, dict[symbol, list[Bar]], portfolio_state], list[(symbol, side)]].
    Called once per calendar date that at least one symbol has a bar for,
    with `bars_by_symbol_asof` containing ONLY the symbols that have a bar
    exactly on that date, each truncated to bars up to and including that
    date -- never later. `portfolio_state` is a read-only snapshot:
    {"cash": float, "total_equity": float, "positions": {symbol: {"qty", "entry_price"}}}.
    Returns an ORDERED list of (symbol, side) candidates for that date --
    the engine processes them in that exact order, updating cash after
    each one, so this order IS the deterministic same-day priority.

    qty_fn: Callable[[symbol, price, portfolio_state, bars_seen, side], float].
    Defaults to fixed_fractional_qty_fn() (5% of total_equity). Called
    only for "buy" candidates -- a "sell" always closes the full existing
    position (same "exit uses what was bought" semantics as
    backtest_engine.run_backtest()'s qty_fn extension).

    cost_model: Callable[[price, qty], float] -> commission dollars.
    Defaults to zero (no costs) -- explicit opt-in, not a silent
    assumption that live trading is free.
    """
    if execution_timing not in EXECUTION_TIMINGS:
        raise ValueError(f"execution_timing must be one of {sorted(EXECUTION_TIMINGS)}, got {execution_timing!r}")
    qty_fn = qty_fn or fixed_fractional_qty_fn()
    cost_model = cost_model or _zero_cost_model

    symbols = sorted(bars_by_symbol)
    index_by_symbol_date = {s: {b.ts: i for i, b in enumerate(bars_by_symbol[s])} for s in symbols}
    all_dates = sorted({b.ts for bars in bars_by_symbol.values() for b in bars})

    cash = float(starting_cash)
    positions = {}          # symbol -> {"qty", "entry_price", "entry_signal_ts", "entry_execution_ts", "entry_commission"}
    last_price = {}         # symbol -> most recent close seen, for marking equity on days a symbol has no bar
    pending = {}            # symbol -> (PortfolioSignal, target_date)

    signals = []
    fills = []
    rejected_fills = []
    trades = []
    equity_curve = []

    def _bars_upto(symbol, date):
        idx = index_by_symbol_date[symbol][date]
        return bars_by_symbol[symbol][: idx + 1]

    def _apply_fill(symbol, signal, bar):
        nonlocal cash
        price = bar.open if execution_timing == "next_bar_open" else bar.close
        if signal.side == "buy":
            portfolio_state = {
                "cash": cash,
                "total_equity": _mark_equity(),
                "positions": {s: dict(p) for s, p in positions.items()},
            }
            qty = qty_fn(symbol, price, portfolio_state, _bars_upto(symbol, bar.ts), "buy")
            qty = max(0.0, float(qty))
            if qty <= 0:
                rejected_fills.append(RejectedFill(
                    symbol=symbol, signal_ts=signal.bar_ts, execution_ts=bar.ts,
                    side="buy", reason="qty_fn_returned_zero",
                ))
                return
            commission = cost_model(price, qty)
            total_cost = price * qty + commission
            if total_cost > cash:
                rejected_fills.append(RejectedFill(
                    symbol=symbol, signal_ts=signal.bar_ts, execution_ts=bar.ts,
                    side="buy", reason="insufficient_cash",
                ))
                return
            cash -= total_cost
            positions[symbol] = {
                "qty": qty, "entry_price": price,
                "entry_signal_ts": signal.bar_ts, "entry_execution_ts": bar.ts,
                "entry_commission": commission,
            }
            fills.append(PortfolioFill(
                symbol=symbol, signal_ts=signal.bar_ts, execution_ts=bar.ts,
                side="buy", price=price, qty=qty, commission=commission,
            ))
        else:  # sell -- always closes the full existing position
            pos = positions.pop(symbol, None)
            if pos is None:
                return  # already closed by something else this cycle -- no-op, not an error
            commission = cost_model(price, pos["qty"])
            proceeds = price * pos["qty"] - commission
            cash += proceeds
            gross_pnl = (price - pos["entry_price"]) * pos["qty"]
            net_pnl = gross_pnl - pos["entry_commission"] - commission
            fills.append(PortfolioFill(
                symbol=symbol, signal_ts=signal.bar_ts, execution_ts=bar.ts,
                side="sell", price=price, qty=pos["qty"], commission=commission,
            ))
            trades.append(Trade(
                symbol=symbol, status="closed",
                entry_signal_ts=pos["entry_signal_ts"], entry_execution_ts=pos["entry_execution_ts"],
                entry_price=pos["entry_price"], qty=pos["qty"],
                exit_signal_ts=signal.bar_ts, exit_execution_ts=bar.ts, exit_price=price,
                gross_pnl=gross_pnl, net_pnl=net_pnl,
            ))

    def _mark_equity():
        positions_value = sum(p["qty"] * last_price.get(s, p["entry_price"]) for s, p in positions.items())
        return cash + positions_value

    for date in all_dates:
        # 1. Apply any fills scheduled for today (next_bar_open only --
        #    same_bar_close never schedules a pending fill in the first
        #    place, see below).
        for symbol in list(pending):
            signal, target_date = pending[symbol]
            if target_date == date and date in index_by_symbol_date[symbol]:
                idx = index_by_symbol_date[symbol][date]
                _apply_fill(symbol, signal, bars_by_symbol[symbol][idx])
                del pending[symbol]

        # 2. Update last_price for every symbol with a bar today (used for
        #    equity marking on days OTHER symbols go quiet).
        bars_by_symbol_asof = {}
        for symbol in symbols:
            idx = index_by_symbol_date[symbol].get(date)
            if idx is None:
                continue
            last_price[symbol] = bars_by_symbol[symbol][idx].close
            bars_by_symbol_asof[symbol] = _bars_upto(symbol, date)

        # 3. Ask the strategy for today's ordered candidates, over exactly
        #    the symbols that have fresh data today -- never a symbol with
        #    only stale/no history.
        portfolio_state = {
            "cash": cash, "total_equity": _mark_equity(),
            "positions": {s: dict(p) for s, p in positions.items()},
        }
        candidates = strategy(date, bars_by_symbol_asof, portfolio_state) or []

        for symbol, side in candidates:
            if side not in SIDES:
                raise ValueError(f"strategy returned illegal side {side!r} for {symbol!r}, must be one of {sorted(SIDES)}")
            if symbol not in bars_by_symbol_asof:
                continue  # strategy named a symbol with no data today -- ignore, not an error
            bar = bars_by_symbol_asof[symbol][-1]
            actionable = (side == "buy" and symbol not in positions) or (side == "sell" and symbol in positions)
            signal = PortfolioSignal(
                symbol=symbol, bar_ts=bar.ts, side=side,
                reason=f"strategy_signal:{side}", actionable=actionable,
            )
            signals.append(signal)
            if not actionable:
                continue
            if execution_timing == "same_bar_close":
                _apply_fill(symbol, signal, bar)
            else:
                idx = index_by_symbol_date[symbol][date]
                if idx + 1 < len(bars_by_symbol[symbol]):
                    pending[symbol] = (signal, bars_by_symbol[symbol][idx + 1].ts)
                # else: no next bar for this symbol -- signal recorded, never filled.

        # 4. Mark equity for today, after all of today's activity.
        positions_value = sum(p["qty"] * last_price.get(s, p["entry_price"]) for s, p in positions.items())
        equity_curve.append(EquityPoint(
            date=date, cash=cash, positions_value=positions_value, total_equity=cash + positions_value,
        ))

    # Any still-open positions at the end of the data remain open, zero
    # realized pnl -- same convention as backtest_engine.run_backtest().
    for symbol, pos in positions.items():
        trades.append(Trade(
            symbol=symbol, status="open",
            entry_signal_ts=pos["entry_signal_ts"], entry_execution_ts=pos["entry_execution_ts"],
            entry_price=pos["entry_price"], qty=pos["qty"],
            exit_signal_ts=None, exit_execution_ts=None, exit_price=None,
            gross_pnl=0.0, net_pnl=0.0,
        ))

    return PortfolioBacktestResult(
        execution_timing=execution_timing, starting_cash=float(starting_cash), final_cash=cash,
        signals=signals, fills=fills, rejected_fills=rejected_fills, trades=trades,
        equity_curve=equity_curve,
    )

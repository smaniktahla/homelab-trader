"""
Tests for shared/portfolio_backtest_engine.py -- VR-0b of the Volatility
Forecasting & Risk-Targeted Position Sizing epic. All synthetic in-memory
data, same style as tests/test_backtest_engine.py -- no DB needed, since
this module has none of its own.
"""

import math
import sys
import pathlib
from datetime import date, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
p = str(ROOT / "shared")
if p not in sys.path:
    sys.path.insert(0, p)

for _mod in ("portfolio_backtest_engine",):
    sys.modules.pop(_mod, None)
import portfolio_backtest_engine as pbe

START = date(2026, 1, 1)


class SimpleBar:
    """A plain namespace matching backtest_engine.Bar's field shape
    (symbol/ts/open/high/low/close/volume) -- this module only ever reads
    those attributes, so a lightweight stand-in avoids importing
    backtest_engine.py for a test file that otherwise has zero dependency
    on it."""
    def __init__(self, symbol, ts, open, high, low, close, volume=1000):
        self.symbol = symbol
        self.ts = ts
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


def _bars(symbol, closes, opens=None, start=START, skip_dates=frozenset()):
    """closes over consecutive calendar dates starting at `start`, skipping
    any date whose 0-based offset is in skip_dates (simulates a symbol
    missing a session other symbols have)."""
    opens = opens or closes
    bars = []
    offset = 0
    i = 0
    while i < len(closes):
        if offset in skip_dates:
            offset += 1
            continue
        d = start + timedelta(days=offset)
        o, c = opens[i], closes[i]
        bars.append(SimpleBar(symbol, d, o, max(o, c), min(o, c), c))
        offset += 1
        i += 1
    return bars


def _buy_once_strategy(symbol, buy_date, sell_date=None):
    def strategy(today, bars_by_symbol_asof, portfolio_state):
        candidates = []
        if today == buy_date and symbol in bars_by_symbol_asof:
            candidates.append((symbol, "buy"))
        if sell_date and today == sell_date and symbol in bars_by_symbol_asof:
            candidates.append((symbol, "sell"))
        return candidates
    return strategy


# ─────────────────────────────────────────────────────────────────────────
# no-lookahead
# ─────────────────────────────────────────────────────────────────────────

def test_strategy_never_receives_future_bars():
    bars = {"AAA": _bars("AAA", [100, 101, 102, 103, 104])}
    seen_lengths = []

    def strategy(today, bars_by_symbol_asof, portfolio_state):
        seen_lengths.append(len(bars_by_symbol_asof["AAA"]))
        return []

    pbe.run_portfolio_backtest(bars, strategy, starting_cash=10_000.0)
    assert seen_lengths == [1, 2, 3, 4, 5]


def test_symbol_absent_until_its_first_bar_date():
    bars = {
        "AAA": _bars("AAA", [100, 101, 102]),
        "BBB": _bars("BBB", [50, 51], start=START + timedelta(days=1)),
    }
    seen_symbols_by_date = {}

    def strategy(today, bars_by_symbol_asof, portfolio_state):
        seen_symbols_by_date[today] = set(bars_by_symbol_asof)
        return []

    pbe.run_portfolio_backtest(bars, strategy, starting_cash=10_000.0)
    assert seen_symbols_by_date[START] == {"AAA"}
    assert seen_symbols_by_date[START + timedelta(days=1)] == {"AAA", "BBB"}


# ─────────────────────────────────────────────────────────────────────────
# cash ledger / trade lifecycle
# ─────────────────────────────────────────────────────────────────────────

def test_buy_then_sell_roundtrip_cash_and_pnl():
    bars = {"AAA": _bars("AAA", [100, 110, 120, 130], opens=[95, 105, 115, 125])}
    strategy = _buy_once_strategy("AAA", buy_date=START, sell_date=START + timedelta(days=2))
    qty_fn = lambda symbol, price, portfolio_state, bars_seen, side: 10.0  # noqa: E731

    result = pbe.run_portfolio_backtest(bars, strategy, starting_cash=10_000.0, qty_fn=qty_fn)

    assert len(result.fills) == 2
    buy_fill, sell_fill = result.fills
    assert buy_fill.side == "buy" and buy_fill.price == 105.0  # next_bar_open: bar1's open
    assert sell_fill.side == "sell" and sell_fill.price == 125.0  # bar3's open

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.status == "closed"
    assert trade.qty == 10.0
    assert trade.gross_pnl == (125.0 - 105.0) * 10.0
    assert trade.net_pnl == trade.gross_pnl  # zero-cost default model

    expected_cash = 10_000.0 - 105.0 * 10.0 + 125.0 * 10.0
    assert result.final_cash == expected_cash


def test_costs_reduce_cash_and_net_pnl():
    bars = {"AAA": _bars("AAA", [100, 110, 120, 130], opens=[95, 105, 115, 125])}
    strategy = _buy_once_strategy("AAA", buy_date=START, sell_date=START + timedelta(days=2))
    qty_fn = lambda symbol, price, portfolio_state, bars_seen, side: 10.0  # noqa: E731
    cost_model = lambda price, qty: 1.0 + 0.001 * price * qty  # noqa: E731

    result = pbe.run_portfolio_backtest(
        bars, strategy, starting_cash=10_000.0, qty_fn=qty_fn, cost_model=cost_model,
    )
    trade = result.trades[0]
    entry_commission = 1.0 + 0.001 * 105.0 * 10.0
    exit_commission = 1.0 + 0.001 * 125.0 * 10.0
    expected_net = trade.gross_pnl - entry_commission - exit_commission
    assert trade.net_pnl == expected_net
    assert trade.net_pnl < trade.gross_pnl


def test_open_trade_at_end_of_data_remains_open():
    bars = {"AAA": _bars("AAA", [100, 101, 102])}
    strategy = _buy_once_strategy("AAA", buy_date=START)
    result = pbe.run_portfolio_backtest(bars, strategy, starting_cash=10_000.0)
    assert len(result.trades) == 1
    assert result.trades[0].status == "open"
    assert result.trades[0].net_pnl == 0.0


def test_no_next_bar_signal_recorded_never_filled():
    bars = {"AAA": _bars("AAA", [100, 101, 102])}
    strategy = _buy_once_strategy("AAA", buy_date=START + timedelta(days=2))  # last bar
    result = pbe.run_portfolio_backtest(bars, strategy, starting_cash=10_000.0)
    assert len(result.signals) == 1
    assert result.signals[0].actionable is True
    assert result.fills == []
    assert result.trades == []


# ─────────────────────────────────────────────────────────────────────────
# deterministic same-day priority / cash competition
# ─────────────────────────────────────────────────────────────────────────

def test_same_day_candidates_processed_in_strategy_priority_order():
    """Two symbols both signal 'buy' on the same day; only enough cash for
    one full-sized position. The strategy returns [B, A] priority (B
    first) -- B must get filled and A must be rejected, proving the
    engine honors the strategy's own order rather than e.g. alphabetical
    or insertion order of the input dict."""
    bars = {
        "AAA": _bars("AAA", [100, 100, 100]),
        "BBB": _bars("BBB", [100, 100, 100]),
    }

    def strategy(today, bars_by_symbol_asof, portfolio_state):
        if today == START:
            return [("BBB", "buy"), ("AAA", "buy")]
        return []

    # Fixed qty_fn: both candidates ask for 80 shares at $100 = $8000 each;
    # only $10,000 total cash -- the second one processed cannot be
    # afforded once the first has spent its share.
    qty_fn = lambda symbol, price, portfolio_state, bars_seen, side: 80.0  # noqa: E731
    result = pbe.run_portfolio_backtest(bars, strategy, starting_cash=10_000.0, qty_fn=qty_fn)

    assert len(result.fills) == 1
    assert result.fills[0].symbol == "BBB"
    assert len(result.rejected_fills) == 1
    assert result.rejected_fills[0].symbol == "AAA"
    assert result.rejected_fills[0].reason == "insufficient_cash"


def test_qty_fn_returning_zero_is_rejected_not_a_crash():
    bars = {"AAA": _bars("AAA", [100, 100])}
    strategy = _buy_once_strategy("AAA", buy_date=START)
    qty_fn = lambda symbol, price, portfolio_state, bars_seen, side: 0.0  # noqa: E731
    result = pbe.run_portfolio_backtest(bars, strategy, starting_cash=10_000.0, qty_fn=qty_fn)
    assert result.fills == []
    assert len(result.rejected_fills) == 1
    assert result.rejected_fills[0].reason == "qty_fn_returned_zero"


def test_illegal_side_raises():
    bars = {"AAA": _bars("AAA", [100, 100])}

    def strategy(today, bars_by_symbol_asof, portfolio_state):
        return [("AAA", "hold")]

    try:
        pbe.run_portfolio_backtest(bars, strategy, starting_cash=10_000.0)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_unlisted_symbol_from_strategy_is_ignored_not_a_crash():
    bars = {"AAA": _bars("AAA", [100, 100])}

    def strategy(today, bars_by_symbol_asof, portfolio_state):
        return [("NOT_A_REAL_SYMBOL", "buy")]

    result = pbe.run_portfolio_backtest(bars, strategy, starting_cash=10_000.0)
    assert result.fills == []
    assert result.signals == []


# ─────────────────────────────────────────────────────────────────────────
# equity curve / drawdown
# ─────────────────────────────────────────────────────────────────────────

def test_equity_curve_marks_every_date_with_no_activity():
    bars = {"AAA": _bars("AAA", [100, 101, 102, 103])}

    def strategy(today, bars_by_symbol_asof, portfolio_state):
        return []

    result = pbe.run_portfolio_backtest(bars, strategy, starting_cash=10_000.0)
    assert len(result.equity_curve) == 4
    assert all(pt.total_equity == 10_000.0 for pt in result.equity_curve)
    assert result.total_return_pct == 0.0
    assert result.max_drawdown_pct == 0.0


def test_gap_symbol_carries_forward_last_known_price_for_equity():
    """BBB has no bar on day 1 (skipped) -- equity marking for BBB's
    position that day must use its last known close (from day 0), not
    crash or silently drop it from the total."""
    bars = {
        "AAA": _bars("AAA", [100, 100, 100]),
        "BBB": _bars("BBB", [50, 60], skip_dates={1}),  # dates 0 and 2, skips date-offset 1
    }
    strategy = _buy_once_strategy("BBB", buy_date=START)
    qty_fn = lambda symbol, price, portfolio_state, bars_seen, side: 10.0  # noqa: E731
    result = pbe.run_portfolio_backtest(bars, strategy, starting_cash=10_000.0, qty_fn=qty_fn)

    # BBB fills at next_bar_open -- its own next bar is date-offset 2 (50 -> 60, opens default to closes)
    assert len(result.fills) == 1
    fill_date = result.fills[0].execution_ts
    assert fill_date == START + timedelta(days=2)

    # On date-offset 1 (AAA has a bar, BBB doesn't), BBB isn't held yet
    # (fill hasn't happened), so this doesn't exercise the carry-forward
    # for a HELD position -- rerun with an earlier buy to actually hold
    # through the gap.
    bars2 = {
        "AAA": _bars("AAA", [100, 100, 100]),
        "BBB": _bars("BBB", [50, 55, 60], skip_dates=set()),
    }

    def strategy2(today, bars_by_symbol_asof, portfolio_state):
        return [("BBB", "buy")] if today == START else []

    result2 = pbe.run_portfolio_backtest(bars2, strategy2, starting_cash=10_000.0, qty_fn=qty_fn)
    # Position opens at bar1's open (day offset 1, price 55) -- held through day 2.
    assert result2.equity_curve[-1].positions_value == 10.0 * 60.0


def test_max_drawdown_pct_computed_correctly():
    bars = {"AAA": _bars("AAA", [100, 100, 100, 100])}
    strategy = _buy_once_strategy("AAA", buy_date=START)
    qty_fn = lambda symbol, price, portfolio_state, bars_seen, side: 50.0  # noqa: E731
    result = pbe.run_portfolio_backtest(bars, strategy, starting_cash=10_000.0, qty_fn=qty_fn)
    # No price movement in this fixture -- drawdown should stay at 0.
    assert result.max_drawdown_pct == 0.0


# ─────────────────────────────────────────────────────────────────────────
# portfolio_state's market_value (VR-3b needs this to reuse
# risk_engine.evaluate_proposal()'s position-allocation/sector-exposure math)
# ─────────────────────────────────────────────────────────────────────────

def test_portfolio_state_positions_include_market_value():
    bars = {"AAA": _bars("AAA", [100, 100, 150, 150])}
    strategy = _buy_once_strategy("AAA", buy_date=START)
    qty_fn = lambda symbol, price, portfolio_state, bars_seen, side: 10.0  # noqa: E731
    seen_states = []

    def capturing_strategy(today, bars_by_symbol_asof, portfolio_state):
        seen_states.append(portfolio_state)
        return strategy(today, bars_by_symbol_asof, portfolio_state)

    pbe.run_portfolio_backtest(bars, capturing_strategy, starting_cash=10_000.0, qty_fn=qty_fn)
    # By the last date (offset 3), AAA was bought at bar1's open (100) and
    # last_price has since moved to 150 (bar index 2 -> today's close) --
    # market_value must reflect the CURRENT mark, not the stale entry price.
    last_state = seen_states[-1]
    assert last_state["positions"]["AAA"]["qty"] == 10.0
    assert last_state["positions"]["AAA"]["market_value"] == 10.0 * 150.0


def test_market_value_absent_before_any_position_is_opened():
    bars = {"AAA": _bars("AAA", [100, 100])}
    seen_states = []

    def strategy(today, bars_by_symbol_asof, portfolio_state):
        seen_states.append(portfolio_state)
        return []

    pbe.run_portfolio_backtest(bars, strategy, starting_cash=10_000.0)
    assert seen_states[0]["positions"] == {}


# ─────────────────────────────────────────────────────────────────────────
# fixed_fractional_qty_fn (the default policy)
# ─────────────────────────────────────────────────────────────────────────

def test_fixed_fractional_qty_fn_sizes_against_total_equity():
    qty_fn = pbe.fixed_fractional_qty_fn(trade_allocation_pct=0.10)
    portfolio_state = {"cash": 10_000.0, "total_equity": 10_000.0, "positions": {}}
    qty = qty_fn("AAA", 50.0, portfolio_state, [], "buy")
    assert qty == math.floor(1000.0 / 50.0)


def test_fixed_fractional_qty_fn_capped_by_available_cash():
    qty_fn = pbe.fixed_fractional_qty_fn(trade_allocation_pct=0.50)
    portfolio_state = {"cash": 100.0, "total_equity": 10_000.0, "positions": {}}
    qty = qty_fn("AAA", 50.0, portfolio_state, [], "buy")
    assert qty == math.floor(100.0 / 50.0)  # cash-capped, not equity-fraction (which would be 100 shares)

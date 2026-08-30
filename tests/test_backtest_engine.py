"""
PR 15, Hypothesis-Driven Trading Architecture epic. Tests the backtest
engine's MECHANICS -- timing, no-lookahead, position-state gating, trade
lifecycle, P&L -- using trivial synthetic strategies, not real indicators.
Strategy correctness (Bollinger breakout, EMA crossover, etc.) is a later
PR's job.
"""

from datetime import datetime, timedelta, timezone

from backtest_engine import Bar, BacktestResult, Signal, load_bars, run_backtest

SYMBOL = "TEST"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bars(closes, opens=None):
    """closes: list of close prices. opens defaults to closes (irrelevant
    for same_bar_close tests, matters for next_bar_open tests where opens
    should differ so a test can prove which price was actually used)."""
    opens = opens or closes
    return [
        Bar(symbol=SYMBOL, ts=START + timedelta(days=i), open=o, high=max(o, c), low=min(o, c), close=c, volume=1000)
        for i, (o, c) in enumerate(zip(opens, closes))
    ]


def _buy_on_bar_zero(bars_seen):
    return "buy" if len(bars_seen) == 1 else None


def _buy_every_bar(bars_seen):
    return "buy"


def _sell_on_bar_zero(bars_seen):
    return "sell" if len(bars_seen) == 1 else None


# --- execution timing ---------------------------------------------------------

def test_next_bar_open_fill_uses_next_bars_open_not_current_bars_close():
    bars = _bars(closes=[100, 200], opens=[90, 150])  # bar0 close=100, bar1 open=150
    result = run_backtest(bars, _buy_on_bar_zero, execution_timing="next_bar_open")
    assert len(result.fills) == 1
    fill = result.fills[0]
    assert fill.execution_ts == bars[1].ts
    assert fill.price == 150.0  # bar1's open, NOT bar0's close (100)


def test_same_bar_close_fill_uses_triggering_bars_own_close():
    bars = _bars(closes=[100, 200], opens=[90, 150])
    result = run_backtest(bars, _buy_on_bar_zero, execution_timing="same_bar_close")
    assert len(result.fills) == 1
    fill = result.fills[0]
    assert fill.execution_ts == bars[0].ts
    assert fill.price == 100.0  # bar0's own close


def test_signal_on_last_bar_with_next_bar_open_is_recorded_but_never_filled():
    def _buy_on_last_bar(bars_seen):
        return "buy" if len(bars_seen) == 3 else None

    bars = _bars(closes=[100, 101, 102])
    result = run_backtest(bars, _buy_on_last_bar, execution_timing="next_bar_open")
    assert len(result.signals) == 1
    assert result.signals[0].actionable is True
    assert len(result.fills) == 0
    # Trade opened conceptually never happens since fill never occurs.
    assert result.trades == []


# --- structural no-lookahead ---------------------------------------------------

def test_strategy_never_receives_future_bars():
    seen_lengths = []

    def _recording_strategy(bars_seen):
        seen_lengths.append(len(bars_seen))
        return None

    bars = _bars(closes=[100, 101, 102, 103, 104])
    run_backtest(bars, _recording_strategy, execution_timing="next_bar_open")
    assert seen_lengths == [1, 2, 3, 4, 5]


def test_strategy_receives_immutable_bar_objects_up_to_current_only():
    captured = []

    def _capture(bars_seen):
        captured.append(bars_seen)
        return None

    bars = _bars(closes=[100, 101, 102])
    run_backtest(bars, _capture, execution_timing="next_bar_open")
    # Each call's list is a strict prefix; the last close visible each time
    # must match bars_seen[-1].close and never look ahead of it.
    for i, seen in enumerate(captured):
        assert seen == bars[: i + 1]


# --- position-state gating ------------------------------------------------------

def test_duplicate_buy_signal_while_already_long_is_recorded_not_actionable():
    bars = _bars(closes=[100, 101, 102, 103])
    result = run_backtest(bars, _buy_every_bar, execution_timing="next_bar_open")
    assert len(result.signals) == 4
    assert result.signals[0].actionable is True
    assert all(s.actionable is False for s in result.signals[1:])
    # Only one fill (the first buy), no duplicate trades.
    assert len(result.fills) == 1
    assert len(result.trades) == 1


def test_sell_signal_while_flat_is_ignored_not_actionable():
    bars = _bars(closes=[100, 101])
    result = run_backtest(bars, _sell_on_bar_zero, execution_timing="next_bar_open")
    assert len(result.signals) == 1
    assert result.signals[0].actionable is False
    assert result.fills == []
    assert result.trades == []


# --- trade lifecycle and P&L ----------------------------------------------------

def test_full_round_trip_trade_lifecycle_and_pnl():
    def _buy_then_sell(bars_seen):
        n = len(bars_seen)
        if n == 1:
            return "buy"
        if n == 3:
            return "sell"
        return None

    bars = _bars(closes=[100, 110, 120, 130], opens=[95, 105, 115, 125])
    result = run_backtest(bars, _buy_then_sell, execution_timing="next_bar_open")

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.status == "closed"
    assert trade.entry_price == 105.0   # bar1's open (fill for bar0's buy signal)
    assert trade.exit_price == 125.0    # bar3's open (fill for bar2's sell signal)
    assert trade.gross_pnl == 20.0
    assert trade.net_pnl == 20.0
    assert result.total_pnl == 20.0
    assert result.trade_count == 1
    assert result.win_rate == 1.0


def test_open_trade_at_end_of_data_remains_open_with_zero_realized_pnl():
    bars = _bars(closes=[100, 101, 102])
    result = run_backtest(bars, _buy_on_bar_zero, execution_timing="next_bar_open")
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.status == "open"
    assert trade.exit_price is None
    assert trade.gross_pnl == 0.0
    assert trade.net_pnl == 0.0
    assert result.total_pnl == 0.0


def test_win_rate_and_trade_count_and_total_pnl_across_multiple_trades():
    # Trade 1: buy@bar0 fill(bar1 open=110), sell@bar2 fill(bar3 open=140) -> +30 win
    # Trade 2: buy@bar4 fill(bar5 open=90), sell@bar6 fill(bar7 open=70) -> -20 loss
    def _two_round_trips(bars_seen):
        n = len(bars_seen)
        if n in (1, 5):
            return "buy"
        if n in (3, 7):
            return "sell"
        return None

    bars = _bars(
        closes=[100, 110, 120, 140, 100, 90, 80, 70],
        opens=[95, 110, 115, 140, 95, 90, 85, 70],
    )
    result = run_backtest(bars, _two_round_trips, execution_timing="next_bar_open")
    closed = [t for t in result.trades if t.status == "closed"]
    assert len(closed) == 2
    assert result.trade_count == 2
    assert result.win_rate == 0.5
    assert result.total_pnl == 30.0 + (-20.0)


# --- serialization ---------------------------------------------------------------

def test_backtest_result_to_json_from_json_round_trip():
    def _buy_then_sell(bars_seen):
        n = len(bars_seen)
        if n == 1:
            return "buy"
        if n == 3:
            return "sell"
        return None

    bars = _bars(closes=[100, 110, 120, 130])
    result = run_backtest(bars, _buy_then_sell, execution_timing="next_bar_open")
    restored = BacktestResult.from_json(result.to_json())
    assert restored == result


def test_empty_bars_returns_empty_result():
    result = run_backtest([], _buy_on_bar_zero, execution_timing="next_bar_open")
    assert result.trades == []
    assert result.signals == []
    assert result.fills == []
    assert result.trade_count == 0
    assert result.win_rate is None
    assert result.total_pnl == 0.0


# --- load_bars (DB-backed) --------------------------------------------------------

def test_load_bars_returns_ordered_ohlcv(conn):
    rows = [
        (START, 100, 105, 99, 103, 1000),
        (START + timedelta(days=1), 103, 108, 101, 107, 1500),
        (START + timedelta(days=2), 107, 110, 105, 109, 1200),
    ]
    with conn.cursor() as cur:
        for ts, o, h, l, c, v in rows:
            cur.execute(
                "INSERT INTO price_history (symbol, ts, open, high, low, close, volume) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (symbol, ts) DO NOTHING",
                (SYMBOL, ts, o, h, l, c, v),
            )
    conn.commit()

    bars = load_bars(conn, SYMBOL, START, START + timedelta(days=2))
    assert len(bars) == 3
    assert [b.ts for b in bars] == [r[0] for r in rows]
    assert bars[0].open == 100.0
    assert bars[0].close == 103.0
    assert bars[-1].volume == 1200.0

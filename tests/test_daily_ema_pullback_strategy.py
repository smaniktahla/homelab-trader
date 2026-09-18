"""
Daily EMA Pullback epic, H1 (8 EMA retest, baseline variant). Fixture
values were independently computed by calling market_structure.ema() and
signals.compute_atr() directly (both already-tested primitives elsewhere
in this repo) before writing these assertions -- see
shared/daily_ema_pullback_strategy.py's module docstring for the exact
entry/exit definition being tested.
"""

from datetime import datetime, timedelta, timezone

from backtest_engine import Bar
from daily_ema_pullback_strategy import load_adjusted_bars, make_daily_8ema_pullback_strategy

SYMBOL = "TEST"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
EMA_PERIOD, ATR_PERIOD, PROXIMITY_MULT = 3, 3, 1.0

# h == l == close throughout (true range collapses to |close[i] - close[i-1]|,
# same trick shared/test_supertrend_strategy.py uses); opens are set
# per-bar to deliberately control the close > open ("bullish pullback bar")
# condition independent of the close/EMA/ATR relationship at each index.
_CLOSES = [100.0] * 6 + [112.0, 106.0, 108.5, 107.0, 106.0, 90.0, 88.0]
_OPENS = [100.0] * 6 + [110.0, 107.0, 106.0, 107.0, 106.0, 90.0, 88.0]
_BUY_INDEX = 8
_SELL_INDEX = 11
# One trailing bar (index 12) beyond _SELL_INDEX purely so the sell
# signal's next_bar_open fill has somewhere to execute in the full-
# round-trip test below -- its own value is otherwise inert.


def _bars(closes, opens):
    return [
        Bar(symbol=SYMBOL, ts=START + timedelta(days=i), open=o, high=c, low=c, close=c, volume=1000)
        for i, (o, c) in enumerate(zip(opens, closes))
    ]


_BARS = _bars(_CLOSES, _OPENS)


def test_defaults_require_enough_bars_before_any_decision():
    strategy = make_daily_8ema_pullback_strategy()  # ema_period=8, atr_period=14
    bars = _bars([100.0] * 21, [100.0] * 21)  # one short of max(8,14)+2 = 16... plenty short either way
    assert strategy(bars[:9]) is None  # far short of max(8,14)+2=16


def test_far_from_ema_produces_no_signal():
    # index 6: close=112 is 6 away from ema=106 with atr=4 -- outside the
    # proximity band and not a breakdown either.
    strategy = make_daily_8ema_pullback_strategy(EMA_PERIOD, ATR_PERIOD, PROXIMITY_MULT)
    assert strategy(_BARS[:7]) is None


def test_at_ema_but_bearish_close_produces_no_signal():
    # index 7: close==ema exactly (dist=0, well within the proximity band)
    # but the bar itself closed bearish (close < open) -- not a confirmed
    # bounce, so no buy.
    strategy = make_daily_8ema_pullback_strategy(EMA_PERIOD, ATR_PERIOD, PROXIMITY_MULT)
    assert strategy(_BARS[:8]) is None


def test_pullback_with_bullish_close_fires_buy():
    strategy = make_daily_8ema_pullback_strategy(EMA_PERIOD, ATR_PERIOD, PROXIMITY_MULT)
    assert strategy(_BARS[:_BUY_INDEX + 1]) == "buy"


def test_flat_closes_near_ema_produce_no_refire():
    # indices 9-10: still within the proximity band but close == open each
    # time (no bullish confirmation), and not a breakdown -- no signal.
    strategy = make_daily_8ema_pullback_strategy(EMA_PERIOD, ATR_PERIOD, PROXIMITY_MULT)
    assert strategy(_BARS[:10]) is None
    assert strategy(_BARS[:11]) is None


def test_breakdown_below_ema_minus_atr_fires_sell():
    strategy = make_daily_8ema_pullback_strategy(EMA_PERIOD, ATR_PERIOD, PROXIMITY_MULT)
    assert strategy(_BARS[:_SELL_INDEX + 1]) == "sell"


def test_full_round_trip_via_backtest_engine():
    from backtest_engine import run_backtest
    strategy = make_daily_8ema_pullback_strategy(EMA_PERIOD, ATR_PERIOD, PROXIMITY_MULT)
    result = run_backtest(_BARS, strategy, execution_timing="next_bar_open")

    signal_sides = [s.side for s in result.signals]
    assert signal_sides == ["buy", "sell"]  # exactly the two engineered transitions, no refires

    closed = [t for t in result.trades if t.status == "closed"]
    assert len(closed) == 1
    trade = closed[0]
    assert trade.entry_execution_ts == _BARS[_BUY_INDEX + 1].ts
    assert trade.exit_execution_ts == _BARS[_SELL_INDEX + 1].ts


# --- load_adjusted_bars() ------------------------------------------------------

def test_load_adjusted_bars_scales_ohlc_by_adjclose_ratio(conn):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO price_history (symbol, ts, open, high, low, close, volume, adjclose)
            VALUES ('ADJTEST', %s, 100.0, 105.0, 95.0, 100.0, 1000, 50.0)
        """, (start,))
    conn.commit()
    try:
        bars = load_adjusted_bars(conn, "ADJTEST", start, start + timedelta(days=1))
        assert len(bars) == 1
        b = bars[0]
        # adjclose=50, close=100 -> factor 0.5, applied uniformly to O/H/L/C.
        assert b.close == 50.0
        assert b.open == 50.0
        assert b.high == 52.5
        assert b.low == 47.5
        assert b.volume == 1000.0  # unscaled, see module docstring
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM price_history WHERE symbol='ADJTEST'")
        conn.commit()


def test_load_adjusted_bars_falls_back_to_unadjusted_when_adjclose_null(conn):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO price_history (symbol, ts, open, high, low, close, volume, adjclose)
            VALUES ('ADJTEST2', %s, 100.0, 105.0, 95.0, 100.0, 1000, NULL)
        """, (start,))
    conn.commit()
    try:
        bars = load_adjusted_bars(conn, "ADJTEST2", start, start + timedelta(days=1))
        assert len(bars) == 1
        b = bars[0]
        assert (b.open, b.high, b.low, b.close) == (100.0, 105.0, 95.0, 100.0)
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM price_history WHERE symbol='ADJTEST2'")
        conn.commit()

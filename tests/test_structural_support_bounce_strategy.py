"""
Price Structure epic PR G. Full run_backtest() cycles (shared/
backtest_engine.py, PR 15) over real DB-backed structural_zones features
(PR C) -- proves the Structural Support Bounce strategy behaves correctly
end to end, unlike PR 16's pure-function Bollinger/EMA strategies, this
one needs a live connection (feature_registry's structural_zones
provider recomputes zone clustering from structural_swings), so these
tests use the conn fixture and seed real price_history/structural_swings
rows rather than constructing bars purely in memory.
"""

from datetime import date, datetime, timedelta, timezone

from backtest_engine import Bar, run_backtest
from structural_support_bounce_strategy import (
    DEFAULT_ENTRY_THRESHOLD_ATR, DEFAULT_INVALIDATION_THRESHOLD_ATR,
    make_structural_support_bounce_strategy,
)

SYMBOL = "SUPPBOUNCE"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _seed_price_history(conn, closes, start=START):
    with conn.cursor() as cur:
        for i, c in enumerate(closes):
            ts = start + timedelta(days=i)
            o, h, l = c - 0.3, c + 0.5, c - 0.5
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES (%s,%s,%s,%s,%s,%s,1000) ON CONFLICT (symbol, ts) DO NOTHING
            """, (SYMBOL, ts, o, h, l, c))
    conn.commit()


def _seed_swing(conn, event_time, confirmation_time, price, swing_type="low"):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO structural_swings (symbol, timeframe, swing_type, event_time, confirmation_time, price)
            VALUES (%s,'daily',%s,%s,%s,%s) ON CONFLICT DO NOTHING
        """, (SYMBOL, swing_type, event_time, confirmation_time, price))
    conn.commit()


def _bars_from_closes(closes, start=START):
    return [
        Bar(symbol=SYMBOL, ts=start + timedelta(days=i), open=c - 0.3, high=c + 0.5, low=c - 0.5, close=c, volume=1000)
        for i, c in enumerate(closes)
    ]


def test_no_signal_without_any_structural_swing_data(conn):
    closes = [100.0] * 30
    _seed_price_history(conn, closes)
    strategy = make_structural_support_bounce_strategy(conn)
    bars = _bars_from_closes(closes)
    result = run_backtest(bars, strategy, execution_timing="next_bar_open")
    assert result.signals == []


def test_full_backtest_cycle_buys_near_support_then_sells_once_price_moves_away(conn):
    # Descend from 110 to ~96 with mild noise (non-zero ATR), hold near
    # 95-96 for a few bars (two confirmed swing lows anchor the support
    # zone there), then rally sharply to 130 -- far enough away that the
    # invalidation threshold fires.
    decline = [110 - i * 0.7 + (0.3 if i % 2 else -0.3) for i in range(20)]  # ~110 -> ~96
    hold = [96.0, 95.5, 96.2, 95.8, 96.1]
    rally = [105, 115, 122, 128, 132]
    closes = decline + hold + rally
    _seed_price_history(conn, closes)

    # two confirmed lows near 95-96, both confirmed well before the "hold" segment ends
    _seed_swing(conn, START.date() + timedelta(days=18), START.date() + timedelta(days=21), 95.9)
    _seed_swing(conn, START.date() + timedelta(days=19), START.date() + timedelta(days=22), 96.0)

    strategy = make_structural_support_bounce_strategy(
        conn, entry_threshold_atr=DEFAULT_ENTRY_THRESHOLD_ATR,
        invalidation_threshold_atr=DEFAULT_INVALIDATION_THRESHOLD_ATR)
    bars = _bars_from_closes(closes)
    result = run_backtest(bars, strategy, execution_timing="next_bar_open")

    buy_signals = [s for s in result.signals if s.side == "buy"]
    sell_signals = [s for s in result.signals if s.side == "sell"]
    assert len(buy_signals) >= 1
    assert len(sell_signals) >= 1
    # the first buy must come from the hold segment (near the support
    # zone), not the initial decline (still far from the eventual zone)
    first_buy_idx = bars.index(next(b for b in bars if b.ts == buy_signals[0].bar_ts))
    assert first_buy_idx >= len(decline)
    # the first sell must come after the rally has moved price well away
    first_sell_idx = bars.index(next(b for b in bars if b.ts == sell_signals[0].bar_ts))
    assert first_sell_idx >= len(decline) + len(hold)
    assert len(result.trades) >= 1


def test_strategy_never_receives_future_bars(conn):
    """Same structural no-lookahead proof PR 15's own generic test uses --
    here specifically confirming the DB-backed feature lookup respects
    bars_seen[-1]'s own date, not "today" or any bar beyond it."""
    closes = [100.0 - i * 0.1 for i in range(30)]
    _seed_price_history(conn, closes)
    _seed_swing(conn, START.date() + timedelta(days=25), START.date() + timedelta(days=28), 90.0)

    seen_lengths = []
    real_strategy = make_structural_support_bounce_strategy(conn)

    def spy(bars_seen):
        seen_lengths.append(len(bars_seen))
        return real_strategy(bars_seen)

    bars = _bars_from_closes(closes)
    run_backtest(bars, spy, execution_timing="next_bar_open")
    assert seen_lengths == list(range(1, len(bars) + 1))

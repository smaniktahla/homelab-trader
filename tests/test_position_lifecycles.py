from datetime import datetime, timezone, timedelta

import position_lifecycles as pl


def _day(n):
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=n)


def _t(id, symbol, side, qty, price, cost=0.0, traded_at=None, thesis_id=1,
       trade_thesis_id=None, initial_stop_price=None, planned_entry_price=None,
       planned_initial_stop_price=None, planned_risk_per_share=None,
       planned_risk_dollars=None):
    return {
        "id": id, "symbol": symbol, "side": side, "qty": qty, "price": price,
        "cost": cost, "traded_at": traded_at or _day(0), "thesis_id": thesis_id,
        "trade_thesis_id": trade_thesis_id,
        "initial_stop_price": initial_stop_price,
        "planned_entry_price": planned_entry_price,
        "planned_initial_stop_price": planned_initial_stop_price,
        "planned_risk_per_share": planned_risk_per_share,
        "planned_risk_dollars": planned_risk_dollars,
    }


def test_simple_closed_lifecycle_with_risk_data():
    trades = [
        _t(1, "AAPL", "buy", 10, 100.0, cost=1.0, traded_at=_day(0),
           initial_stop_price=95.0, planned_entry_price=100.0,
           planned_initial_stop_price=95.0, planned_risk_per_share=5.0,
           planned_risk_dollars=50.0),
        _t(2, "AAPL", "sell", 10, 110.0, cost=1.0, traded_at=_day(5)),
    ]
    result = pl.match_lifecycles(trades)
    lcs = result["AAPL"]["lifecycles"]
    assert len(lcs) == 1
    lc = lcs[0]
    assert lc.status == "closed"
    assert lc.qty == 10
    assert lc.entry_notional == 1000.0
    assert lc.exit_notional == 1100.0
    assert lc.gross_pnl == 100.0
    assert lc.total_cost == 2.0
    assert lc.net_pnl == 98.0
    assert lc.actual_initial_risk_per_share == 5.0
    assert lc.actual_initial_risk_dollars == 50.0
    assert lc.realized_r == round(98.0 / 50.0, 3)
    assert lc.thesis_id == 1
    assert lc.data_quality_flags == []
    assert result["AAPL"]["unmatched_sell_qty"] == 0.0


def test_open_lifecycle_with_no_exits():
    trades = [_t(1, "AAPL", "buy", 10, 100.0, cost=1.0, traded_at=_day(0))]
    result = pl.match_lifecycles(trades)
    lcs = result["AAPL"]["lifecycles"]
    assert len(lcs) == 1
    lc = lcs[0]
    assert lc.status == "open"
    assert lc.closed_at is None
    assert lc.qty == 10
    # 0.0, not None -- "nothing sold yet" is a known fact, not missing data.
    assert lc.gross_pnl == 0.0
    assert lc.net_pnl == 0.0
    assert lc.exit_notional == 0.0


def test_pyramiding_consumes_fifo_oldest_lot_first():
    """Two buys at different prices; a sell smaller than the total should
    consume the OLDEST lot first (true FIFO), not blend into an average
    like round_trips.py's stand-in does. The realized P&L on the sell
    proves which lot was actually consumed."""
    trades = [
        _t(1, "MSFT", "buy", 10, 100.0, traded_at=_day(0)),
        _t(2, "MSFT", "buy", 10, 120.0, traded_at=_day(1)),
        _t(3, "MSFT", "sell", 10, 130.0, traded_at=_day(2)),  # should consume the $100 lot, not $120
    ]
    result = pl.match_lifecycles(trades)
    lc = result["MSFT"]["lifecycles"][0]
    assert lc.status == "open"
    assert lc.qty == 10  # the $120 lot remains
    # FIFO: realized on the 10@100 lot = 10*(130-100) = 300
    assert lc.gross_pnl == 300.0


def test_pyramiding_and_partial_exit_spanning_two_lots():
    trades = [
        _t(1, "MSFT", "buy", 10, 100.0, traded_at=_day(0)),
        _t(2, "MSFT", "buy", 10, 120.0, traded_at=_day(1)),
        _t(3, "MSFT", "sell", 15, 130.0, traded_at=_day(2)),  # consumes all 10@100 + 5@120
    ]
    result = pl.match_lifecycles(trades)
    lc = result["MSFT"]["lifecycles"][0]
    assert lc.status == "open"
    assert lc.qty == 5  # 5 shares of the $120 lot remain
    expected_gross = 10 * (130 - 100) + 5 * (130 - 120)
    assert lc.gross_pnl == expected_gross
    # position_trades should show the sell split across both lots
    exit_allocations = [p["qty_allocated"] for p in lc.position_trades if p["role"] == "exit"]
    assert sorted(exit_allocations) == [5.0, 10.0] or sum(exit_allocations) == 15.0


def test_oversell_with_nothing_open_is_tallied_not_fabricated():
    trades = [_t(1, "TSLA", "sell", 5, 200.0, traded_at=_day(0))]
    result = pl.match_lifecycles(trades)
    assert result["TSLA"]["lifecycles"] == []
    assert result["TSLA"]["unmatched_sell_qty"] == 5.0


def test_oversell_exceeding_open_lot_tallies_the_excess():
    trades = [
        _t(1, "TSLA", "buy", 5, 200.0, traded_at=_day(0)),
        _t(2, "TSLA", "sell", 8, 210.0, traded_at=_day(1)),  # only 5 are locally known
    ]
    result = pl.match_lifecycles(trades)
    data = result["TSLA"]
    assert data["unmatched_sell_qty"] == 3.0
    lc = data["lifecycles"][0]
    assert lc.status == "closed"
    assert lc.qty == 5


def test_cross_thesis_concurrent_symbol_flagged_not_resolved():
    trades = [
        _t(1, "NVDA", "buy", 10, 100.0, traded_at=_day(0), thesis_id=1),
        _t(2, "NVDA", "buy", 10, 105.0, traded_at=_day(1), thesis_id=2),
    ]
    result = pl.match_lifecycles(trades)
    lc = result["NVDA"]["lifecycles"][0]
    assert lc.status == "open"
    assert lc.thesis_id is None
    assert "concurrent_multi_thesis_symbol" in lc.data_quality_flags


# --- trade_thesis_id (PR 9) ----------------------------------------------

def test_single_trade_thesis_id_resolves_unambiguously():
    trades = [_t(1, "AAPL", "buy", 10, 100.0, traded_at=_day(0), trade_thesis_id=42)]
    result = pl.match_lifecycles(trades)
    lc = result["AAPL"]["lifecycles"][0]
    assert lc.trade_thesis_id == 42
    assert "concurrent_multi_trade_thesis_position" not in lc.data_quality_flags


def test_no_trade_thesis_id_stays_none_without_flag():
    # No fill ever carried a trade_thesis_id (instantiation was off, or
    # predates PR 4) -- must read as "no thesis," not as ambiguity.
    trades = [_t(1, "AAPL", "buy", 10, 100.0, traded_at=_day(0))]
    result = pl.match_lifecycles(trades)
    lc = result["AAPL"]["lifecycles"][0]
    assert lc.trade_thesis_id is None
    assert lc.data_quality_flags == []


def test_one_real_trade_thesis_id_among_untracked_fills_resolves_unambiguously():
    # Pyramided position: first entry carried a trade_thesis_id, a later
    # add-on didn't (e.g. instantiation was toggled off in between). This
    # must resolve to the one real value, not read as ambiguous just
    # because the fills disagree on presence.
    trades = [
        _t(1, "AAPL", "buy", 10, 100.0, traded_at=_day(0), trade_thesis_id=42),
        _t(2, "AAPL", "buy", 5, 102.0, traded_at=_day(1), trade_thesis_id=None),
    ]
    result = pl.match_lifecycles(trades)
    lc = result["AAPL"]["lifecycles"][0]
    assert lc.trade_thesis_id == 42
    assert "concurrent_multi_trade_thesis_position" not in lc.data_quality_flags


def test_distinct_trade_thesis_ids_flagged_and_unresolved():
    # A genuine pyramid across two separate opportunities -- real
    # ambiguity, distinct from the "some fills just have no data" case above.
    trades = [
        _t(1, "AAPL", "buy", 10, 100.0, traded_at=_day(0), trade_thesis_id=42),
        _t(2, "AAPL", "buy", 5, 102.0, traded_at=_day(1), trade_thesis_id=43),
    ]
    result = pl.match_lifecycles(trades)
    lc = result["AAPL"]["lifecycles"][0]
    assert lc.trade_thesis_id is None
    assert "concurrent_multi_trade_thesis_position" in lc.data_quality_flags


def test_trade_thesis_id_independent_of_strategy_thesis_id_ambiguity():
    # Same strategy family (thesis_id=1 both times, unambiguous) but two
    # distinct trade_thesis_id values -- the two flags are independent;
    # only the trade_thesis one should fire here.
    trades = [
        _t(1, "AAPL", "buy", 10, 100.0, traded_at=_day(0), thesis_id=1, trade_thesis_id=42),
        _t(2, "AAPL", "buy", 5, 102.0, traded_at=_day(1), thesis_id=1, trade_thesis_id=43),
    ]
    result = pl.match_lifecycles(trades)
    lc = result["AAPL"]["lifecycles"][0]
    assert lc.thesis_id == 1
    assert "concurrent_multi_thesis_symbol" not in lc.data_quality_flags
    assert lc.trade_thesis_id is None
    assert "concurrent_multi_trade_thesis_position" in lc.data_quality_flags


def test_missing_risk_data_stays_none_never_zero():
    """A manual trade with no linked proposal has no initial_stop_price at
    all -- every risk/R field must be None, never coerced to 0."""
    trades = [
        _t(1, "GOOG", "buy", 5, 100.0, traded_at=_day(0)),  # no initial_stop_price
        _t(2, "GOOG", "sell", 5, 110.0, traded_at=_day(1)),
    ]
    result = pl.match_lifecycles(trades)
    lc = result["GOOG"]["lifecycles"][0]
    assert lc.actual_initial_risk_per_share is None
    assert lc.actual_initial_risk_dollars is None
    assert lc.realized_r is None
    assert lc.mae_r is None
    assert lc.mfe_r is None


def test_pathwise_mae_mfe_against_price_bars():
    bars = {
        "AMD": [
            {"ts": _day(0), "high": 101.0, "low": 99.0, "resolution": "daily"},
            {"ts": _day(1), "high": 108.0, "low": 90.0, "resolution": "daily"},
            {"ts": _day(2), "high": 115.0, "low": 100.0, "resolution": "daily"},
        ]
    }
    trades = [
        _t(1, "AMD", "buy", 10, 100.0, traded_at=_day(0), initial_stop_price=90.0),
        _t(2, "AMD", "sell", 10, 105.0, traded_at=_day(2)),
    ]
    result = pl.match_lifecycles(trades, price_bars_by_symbol=bars)
    lc = result["AMD"]["lifecycles"][0]
    assert lc.mae_price == 90.0
    assert lc.mfe_price == 115.0
    assert lc.excursion_resolution == "daily_approximation"
    # risk_per_share = |100 - 90| = 10; worst excursion = 90 - 100 = -10 -> mae_r = -1.0
    assert lc.mae_r == -1.0
    # best excursion = 115 - 100 = 15 -> mfe_r = 1.5
    assert lc.mfe_r == 1.5


def test_mae_mfe_uses_time_varying_weighted_cost_basis():
    """A pyramid add shifts the cost basis mid-holding -- excursion after
    the add must be measured against the NEW blended basis, not the
    original entry price alone."""
    bars = {
        "IBM": [
            {"ts": _day(0), "high": 101.0, "low": 99.0, "resolution": "daily"},
            {"ts": _day(1), "high": 102.0, "low": 98.0, "resolution": "daily"},   # before the add: basis=100
            {"ts": _day(2), "high": 106.0, "low": 94.0, "resolution": "daily"},   # after the add: basis=105 (10@100 + 10@110)/20
        ]
    }
    trades = [
        _t(1, "IBM", "buy", 10, 100.0, traded_at=_day(0), initial_stop_price=90.0),
        _t(2, "IBM", "buy", 10, 110.0, traded_at=_day(2)),
        _t(3, "IBM", "sell", 20, 108.0, traded_at=_day(3)),
    ]
    result = pl.match_lifecycles(trades, price_bars_by_symbol=bars)
    lc = result["IBM"]["lifecycles"][0]
    # Day 2's bar (low=94) is evaluated against the POST-add basis of 105,
    # not the original 100 -- excursion = 94-105 = -11, worse than day 1's
    # 98-100=-2, so day 2 should win as the MAE bar.
    assert lc.mae_price == 94.0
    # MFE is the best EXCURSION vs basis-at-that-time, not the highest raw
    # price: day 2's high (106) against the new higher basis (105) is only
    # +1, less favorable than day 1's high (102) against the still-lower
    # basis (100), which is +2 -- so day 1 wins, even though 106 > 102.
    # This is the whole point of a time-varying reference: raw price alone
    # isn't what matters once the cost basis has moved.
    assert lc.mfe_price == 102.0


def test_no_price_bars_leaves_excursion_fields_none():
    trades = [
        _t(1, "F", "buy", 10, 12.0, traded_at=_day(0), initial_stop_price=11.0),
        _t(2, "F", "sell", 10, 13.0, traded_at=_day(1)),
    ]
    result = pl.match_lifecycles(trades)  # no price_bars_by_symbol at all
    lc = result["F"]["lifecycles"][0]
    assert lc.mae_price is None
    assert lc.mfe_price is None
    assert lc.excursion_resolution is None


def test_full_close_then_reopen_produces_two_separate_lifecycles():
    trades = [
        _t(1, "AAPL", "buy", 10, 100.0, traded_at=_day(0)),
        _t(2, "AAPL", "sell", 10, 110.0, traded_at=_day(1)),
        _t(3, "AAPL", "buy", 5, 120.0, traded_at=_day(5)),
    ]
    result = pl.match_lifecycles(trades)
    lcs = result["AAPL"]["lifecycles"]
    assert len(lcs) == 2
    assert lcs[0].status == "closed"
    assert lcs[1].status == "open"
    assert lcs[1].entry_trade_ids == [3]

from datetime import datetime, timezone, timedelta

import round_trips as rt


def _t(id, symbol, side, qty, price, cost=0.0, traded_at=None):
    return {"id": id, "symbol": symbol, "side": side, "qty": qty, "price": price,
            "cost": cost, "traded_at": traded_at or datetime(2026, 1, 1, tzinfo=timezone.utc)}


def _day(n):
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=n)


def test_simple_completed_round_trip():
    trades = [
        _t(1, "AAPL", "buy", 10, 100.0, cost=1.0, traded_at=_day(0)),
        _t(2, "AAPL", "sell", 10, 110.0, cost=1.0, traded_at=_day(5)),
    ]
    result = rt.reconstruct(trades)
    trips = result["AAPL"]["round_trips"]
    assert len(trips) == 1
    trip = trips[0]
    assert trip.entry_trade_ids == [1]
    assert trip.exit_trade_ids == [2]
    assert trip.qty == 10
    assert trip.entry_notional == 1000.0
    assert trip.pnl_before_costs == 100.0   # (110-100)*10
    assert trip.total_cost == 2.0            # entry cost + exit cost
    assert trip.net_pnl == 98.0
    assert trip.return_pct == round(98.0 / 1000.0 * 100, 2)
    assert trip.holding_days == 5.0
    assert result["AAPL"]["open_episode"] is None


def test_open_episode_with_no_exits():
    trades = [_t(1, "AAPL", "buy", 10, 100.0, cost=1.0, traded_at=_day(0))]
    result = rt.reconstruct(trades)
    assert result["AAPL"]["round_trips"] == []
    ep = result["AAPL"]["open_episode"]
    assert ep.reconstructed_qty == 10
    assert ep.entry_notional == 1000.0
    # Nothing has been sold yet, so nothing is realized -- the entry's
    # commission belongs to the still-open shares' cost basis, not to a
    # "realized" figure. It must NOT be charged the instant a position
    # opens, before any shares are sold.
    assert ep.partial_realized_net_pnl == 0.0


def test_open_episode_entry_cost_prorated_by_shares_actually_sold():
    trades = [
        _t(1, "AAPL", "buy", 10, 100.0, cost=2.0, traded_at=_day(0)),
        _t(2, "AAPL", "sell", 4, 110.0, cost=1.0, traded_at=_day(3)),  # partial exit, position stays open
    ]
    result = rt.reconstruct(trades)
    assert result["AAPL"]["round_trips"] == []
    ep = result["AAPL"]["open_episode"]
    assert ep.reconstructed_qty == 6
    # Only 4 of the 10 entry shares have been sold -> only 40% of the $2
    # entry commission is attributable to what's actually been realized.
    prorated_entry_cost = 2.0 * (4 / 10)
    expected_net = (4 * (110 - 100)) - prorated_entry_cost - 1.0
    assert ep.partial_realized_net_pnl == round(expected_net, 2)


def test_pyramiding_multiple_buys_before_full_exit():
    trades = [
        _t(1, "AAPL", "buy", 10, 100.0, traded_at=_day(0)),
        _t(2, "AAPL", "buy", 10, 120.0, traded_at=_day(2)),   # avg cost now 110
        _t(3, "AAPL", "sell", 20, 130.0, traded_at=_day(10)),
    ]
    result = rt.reconstruct(trades)
    trips = result["AAPL"]["round_trips"]
    assert len(trips) == 1
    trip = trips[0]
    assert trip.entry_trade_ids == [1, 2]
    assert trip.qty == 20
    assert trip.entry_notional == 1000.0 + 1200.0
    assert trip.pnl_before_costs == (130 - 110) * 20  # avg cost 110
    assert trip.holding_days == 10.0  # from first entry, not the second


def test_partial_exit_then_full_exit_produces_one_round_trip():
    trades = [
        _t(1, "AAPL", "buy", 10, 100.0, traded_at=_day(0)),
        _t(2, "AAPL", "sell", 4, 110.0, traded_at=_day(3)),   # partial
        _t(3, "AAPL", "sell", 6, 120.0, traded_at=_day(8)),   # closes it out
    ]
    result = rt.reconstruct(trades)
    trips = result["AAPL"]["round_trips"]
    assert len(trips) == 1
    trip = trips[0]
    assert trip.exit_trade_ids == [2, 3]
    assert trip.qty == 10
    expected_pnl = 4 * (110 - 100) + 6 * (120 - 100)
    assert trip.pnl_before_costs == expected_pnl
    assert trip.closed_at == _day(8)


def test_two_separate_round_trips_for_same_symbol():
    trades = [
        _t(1, "AAPL", "buy", 10, 100.0, traded_at=_day(0)),
        _t(2, "AAPL", "sell", 10, 90.0, traded_at=_day(3)),    # loser, closes flat
        _t(3, "AAPL", "buy", 5, 95.0, traded_at=_day(10)),
        _t(4, "AAPL", "sell", 5, 105.0, traded_at=_day(15)),   # winner
    ]
    result = rt.reconstruct(trades)
    trips = result["AAPL"]["round_trips"]
    assert len(trips) == 2
    assert trips[0].net_pnl == -100.0
    assert trips[1].net_pnl == 50.0
    assert result["AAPL"]["open_episode"] is None


def test_oversell_is_capped_not_fabricated():
    trades = [
        _t(1, "AAPL", "buy", 5, 100.0, traded_at=_day(0)),
        _t(2, "AAPL", "sell", 20, 110.0, traded_at=_day(3)),  # sells more than locally known
    ]
    result = rt.reconstruct(trades)
    trips = result["AAPL"]["round_trips"]
    assert len(trips) == 1
    assert trips[0].qty == 5  # capped to what was actually bought locally, not 20
    assert result["AAPL"]["open_episode"] is None


def test_sell_with_nothing_open_is_ignored():
    trades = [_t(1, "AAPL", "sell", 10, 100.0, traded_at=_day(0))]
    result = rt.reconstruct(trades)
    assert result.get("AAPL", {"round_trips": []})["round_trips"] == []


def test_portfolio_totals_only_uses_completed_round_trips():
    trades = [
        _t(1, "AAPL", "buy", 10, 100.0, traded_at=_day(0)),
        _t(2, "AAPL", "sell", 10, 110.0, traded_at=_day(5)),   # +100 completed winner
        _t(3, "MSFT", "buy", 10, 200.0, traded_at=_day(0)),
        _t(4, "MSFT", "sell", 10, 180.0, traded_at=_day(5)),   # -200 completed loser
        _t(5, "NVDA", "buy", 10, 50.0, traded_at=_day(0)),      # still open — must NOT count
    ]
    result = rt.reconstruct(trades)
    totals = rt.portfolio_totals(result)
    assert totals["gross_profit_total"] == 100.0
    assert totals["gross_loss_total"] == -200.0
    assert totals["net_pnl_total"] == -100.0


def test_symbol_contribution_can_exceed_100_percent():
    trades = [
        _t(1, "AAPL", "buy", 10, 100.0, traded_at=_day(0)),
        _t(2, "AAPL", "sell", 10, 110.0, traded_at=_day(5)),   # +100
        _t(3, "MSFT", "buy", 10, 200.0, traded_at=_day(0)),
        _t(4, "MSFT", "sell", 10, 180.0, traded_at=_day(5)),   # -200
    ]
    result = rt.reconstruct(trades)
    totals = rt.portfolio_totals(result)  # net_pnl_total = -100
    contrib = rt.symbol_contribution("AAPL", result, totals)
    # AAPL made +100 while the portfolio as a whole is -100 -> AAPL's
    # share of net P&L is negative-of-100%, not bounded to [0,100].
    assert contrib["contribution_to_net_pnl_pct"] == -100.0
    assert contrib["contribution_to_gross_gains_pct"] == 100.0  # AAPL is the only winner


def test_symbol_summary_excludes_open_position_from_completed_stats():
    trades = [
        _t(1, "AAPL", "buy", 10, 100.0, traded_at=_day(0)),
        _t(2, "AAPL", "sell", 10, 90.0, traded_at=_day(3)),     # completed loser
        _t(3, "AAPL", "buy", 10, 95.0, traded_at=_day(10)),     # still open
    ]
    result = rt.reconstruct(trades)
    totals = rt.portfolio_totals(result)
    live_position = {"qty": "10", "avg_entry_price": "95.0", "current_price": "150.0",
                      "unrealized_pl": "550.0", "market_value": "1500.0"}
    summary = rt.symbol_summary("AAPL", result, totals, live_position=live_position)

    # Completed-trade stats reflect ONLY the one closed round trip -- the
    # open position's large unrealized gain must not leak into win_rate,
    # avg_return_pct, best/worst, or avg_holding_days.
    assert summary["completed_round_trips"] == 1
    assert summary["wins"] == 0
    assert summary["losses"] == 1
    assert summary["win_rate_pct"] == 0.0
    assert summary["best_trip"]["net_pnl"] == -100.0
    assert summary["worst_trip"]["net_pnl"] == -100.0

    # But unrealized/total P&L DO reflect the open position.
    assert summary["unrealized_pnl"] == 550.0
    assert summary["realized_pnl"] == -100.0
    assert summary["total_pnl"] == 450.0
    assert summary["methodology"] == "average_cost_reconstruction"
    assert summary["open_position"]["qty"] == 10.0


def test_symbol_summary_with_no_trades_at_all():
    result = rt.reconstruct([])
    totals = rt.portfolio_totals(result)
    summary = rt.symbol_summary("GOOG", result, totals, live_position=None)
    assert summary["completed_round_trips"] == 0
    assert summary["win_rate_pct"] is None
    assert summary["avg_return_pct"] is None
    assert summary["best_trip"] is None
    assert summary["open_position"] is None
    assert summary["realized_pnl"] == 0.0
    assert summary["total_pnl"] == 0.0


def test_symbol_summary_flags_qty_mismatch_between_ledger_and_broker():
    trades = [_t(1, "AAPL", "buy", 10, 100.0, traded_at=_day(0))]
    result = rt.reconstruct(trades)
    totals = rt.portfolio_totals(result)
    live_position = {"qty": "15", "avg_entry_price": "100.0", "current_price": "100.0",
                      "unrealized_pl": "0.0", "market_value": "1500.0"}
    summary = rt.symbol_summary("AAPL", result, totals, live_position=live_position)
    assert summary["data_quality_note"] is not None
    assert "15" in summary["data_quality_note"]

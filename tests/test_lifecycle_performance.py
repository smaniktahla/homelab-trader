"""
Pure unit tests for shared/lifecycle_performance.py -- no DB, synthetic
dict rows shaped like what api/main.py's _all_closed_lifecycles/
_open_lifecycle helpers actually return (RealDictCursor rows joined
against position_trades for entry_trade_ids/exit_trade_ids).

Mirrors the style of shared/position_lifecycles.py's own test suite
(tests/test_position_lifecycles.py): plain dicts, direct float ==
comparisons, no DB fixtures needed.
"""

from datetime import datetime, timedelta, timezone

import lifecycle_performance as lp


def _day(n):
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=n)


def _lc(symbol="AAPL", opened=0, closed=5, qty=10.0, entry_notional=1000.0,
        net_pnl=100.0, entry_trade_ids=None, exit_trade_ids=None, status="closed"):
    return {
        "symbol": symbol,
        "status": status,
        "opened_at": _day(opened),
        "closed_at": _day(closed) if status == "closed" else None,
        "qty": qty,
        "entry_notional": entry_notional,
        "net_pnl": net_pnl,
        "entry_trade_ids": entry_trade_ids if entry_trade_ids is not None else [1],
        "exit_trade_ids": exit_trade_ids if exit_trade_ids is not None else [2],
    }


def test_symbol_summary_no_lifecycles():
    s = lp.symbol_summary("AAPL", {}, lp.portfolio_totals({}), unmatched_sell_qty=0.0)
    assert s["methodology"] == "position_lifecycle_fifo"
    assert s["methodology_status"] == "complete"
    assert s["completed_round_trips"] == 0
    assert s["win_rate_pct"] is None
    assert s["avg_return_pct"] is None
    assert s["best_trip"] is None
    assert s["worst_trip"] is None
    assert s["open_position"] is None
    assert s["reconciliation"]["status"] == "match"


def test_win_loss_breakeven_classification():
    rows = {"AAPL": [
        _lc(net_pnl=100.0, entry_trade_ids=[1], exit_trade_ids=[2]),
        _lc(net_pnl=-50.0, entry_trade_ids=[3], exit_trade_ids=[4]),
        _lc(net_pnl=0.0, entry_trade_ids=[5], exit_trade_ids=[6]),
    ]}
    totals = lp.portfolio_totals(rows)
    s = lp.symbol_summary("AAPL", rows, totals, unmatched_sell_qty=0.0)
    assert s["completed_round_trips"] == 3
    assert s["wins"] == 1
    assert s["losses"] == 1
    assert s["breakeven"] == 1
    assert s["win_rate_pct"] == round(1 / 3 * 100, 1)


def test_return_pct_and_holding_days():
    rows = {"AAPL": [_lc(opened=0, closed=4, entry_notional=1000.0, net_pnl=250.0)]}
    totals = lp.portfolio_totals(rows)
    s = lp.symbol_summary("AAPL", rows, totals, unmatched_sell_qty=0.0)
    assert s["avg_return_pct"] == 25.0
    assert s["median_return_pct"] == 25.0
    assert s["avg_holding_days"] == 4.0
    assert s["best_trip"]["net_pnl"] == 250.0
    assert s["best_trip"]["return_pct"] == 25.0


def test_best_and_worst_trip():
    rows = {"AAPL": [
        _lc(entry_trade_ids=[1], exit_trade_ids=[2], net_pnl=300.0),
        _lc(entry_trade_ids=[3], exit_trade_ids=[4], net_pnl=-120.0),
        _lc(entry_trade_ids=[5], exit_trade_ids=[6], net_pnl=50.0),
    ]}
    totals = lp.portfolio_totals(rows)
    s = lp.symbol_summary("AAPL", rows, totals, unmatched_sell_qty=0.0)
    assert s["best_trip"]["net_pnl"] == 300.0
    assert s["worst_trip"]["net_pnl"] == -120.0


def test_capital_deployed_sums_closed_and_open_entry_notional():
    rows = {"AAPL": [_lc(entry_notional=1000.0, net_pnl=100.0)]}
    open_lc = _lc(status="open", entry_notional=500.0, net_pnl=0.0, closed=None)
    totals = lp.portfolio_totals(rows)
    s = lp.symbol_summary("AAPL", rows, totals, unmatched_sell_qty=0.0, open_lifecycle=open_lc)
    assert s["capital_deployed"] == 1500.0
    assert s["capital_deployed_methodology"] == "sum_of_entry_notionals"


def test_open_lifecycle_net_pnl_is_realized_to_date_no_extra_proration():
    """position_lifecycles.gross_pnl/net_pnl are already realized-to-date
    (0.0 baseline while nothing's sold, partial otherwise) per PR A's own
    dataclass docstring -- symbol_summary must not re-derive or double-count
    this, just add it straight into realized_pnl_total."""
    rows = {"AAPL": [_lc(net_pnl=100.0)]}
    open_lc = _lc(status="open", entry_notional=500.0, net_pnl=30.0, closed=None)
    totals = lp.portfolio_totals(rows)
    live_position = {
        "qty": "5", "avg_entry_price": "95.0", "current_price": "110.0",
        "unrealized_pl": "75.0", "market_value": "550.0",
    }
    s = lp.symbol_summary("AAPL", rows, totals, unmatched_sell_qty=0.0,
                           open_lifecycle=open_lc, live_position=live_position)
    assert s["realized_pnl"] == 130.0   # 100 (closed) + 30 (open, realized-to-date)
    assert s["unrealized_pnl"] == 75.0
    assert s["total_pnl"] == 205.0
    assert s["open_position"]["partial_realized_pnl"] == 30.0
    assert s["open_position"]["qty"] == 5.0   # prefers live broker qty over ledger qty


def test_portfolio_totals_and_contribution_can_exceed_100_or_go_negative():
    rows = {
        "AAPL": [_lc(symbol="AAPL", entry_trade_ids=[1], exit_trade_ids=[2], net_pnl=200.0)],
        "MSFT": [_lc(symbol="MSFT", entry_trade_ids=[3], exit_trade_ids=[4], net_pnl=-350.0)],
    }
    totals = lp.portfolio_totals(rows)
    assert totals["gross_profit_total"] == 200.0
    assert totals["gross_loss_total"] == -350.0
    assert totals["net_pnl_total"] == -150.0

    aapl_contribution = lp.symbol_contribution("AAPL", rows, totals)
    assert aapl_contribution["contribution_to_gross_gains_pct"] == 100.0
    # AAPL's own +200 net pnl against a portfolio net pnl of -150 -> exceeds
    # 100% in magnitude and flips sign relative to AAPL's own result.
    assert aapl_contribution["contribution_to_net_pnl_pct"] == round(200.0 / -150.0 * 100, 2)

    # MSFT has zero winning trips of its own, but the portfolio DOES have
    # gross profit (from AAPL) -- so this is a real, computable 0.0% share,
    # not an undefined "None" (that only happens when the portfolio-wide
    # denominator itself is zero).
    msft_contribution = lp.symbol_contribution("MSFT", rows, totals)
    assert msft_contribution["contribution_to_gross_gains_pct"] == 0.0


def test_methodology_status_partial_when_unmatched_sell_qty_positive():
    s = lp.symbol_summary("AAPL", {}, lp.portfolio_totals({}), unmatched_sell_qty=12.5)
    assert s["methodology_status"] == "partial"
    assert s["unmatched_sell_qty"] == 12.5


def test_reconciliation_match_when_both_flat():
    s = lp.symbol_summary("AAPL", {}, lp.portfolio_totals({}), unmatched_sell_qty=0.0)
    assert s["reconciliation"] == {
        "ledger_status": "flat", "broker_status": "flat", "status": "match", "detail": None,
    }


def test_reconciliation_qty_mismatch():
    open_lc = _lc(status="open", qty=5.0, entry_notional=500.0, net_pnl=0.0, closed=None)
    live_position = {"qty": "20", "avg_entry_price": "100.0", "current_price": "100.0",
                      "unrealized_pl": "0.0", "market_value": "2000.0"}
    s = lp.symbol_summary("AAPL", {}, lp.portfolio_totals({}), unmatched_sell_qty=0.0,
                           open_lifecycle=open_lc, live_position=live_position)
    assert s["reconciliation"]["status"] == "qty_mismatch"
    assert s["reconciliation"]["ledger_status"] == "open"
    assert s["reconciliation"]["broker_status"] == "open"


def test_reconciliation_ledger_only():
    open_lc = _lc(status="open", qty=5.0, entry_notional=500.0, net_pnl=0.0, closed=None)
    s = lp.symbol_summary("AAPL", {}, lp.portfolio_totals({}), unmatched_sell_qty=0.0,
                           open_lifecycle=open_lc, live_position=None)
    assert s["reconciliation"]["status"] == "ledger_only"


def test_reconciliation_broker_only():
    live_position = {"qty": "5", "avg_entry_price": "100.0", "current_price": "100.0",
                      "unrealized_pl": "0.0", "market_value": "500.0"}
    s = lp.symbol_summary("AAPL", {}, lp.portfolio_totals({}), unmatched_sell_qty=0.0,
                           open_lifecycle=None, live_position=live_position)
    assert s["reconciliation"]["status"] == "broker_only"


def test_round_trips_detail_shape():
    rows = {"MSFT": [_lc(symbol="MSFT", opened=0, closed=4, qty=10.0, entry_notional=2000.0,
                          net_pnl=200.0, entry_trade_ids=[10], exit_trade_ids=[11])]}
    open_lc = _lc(symbol="MSFT", status="open", qty=3.0, entry_notional=630.0, net_pnl=0.0, closed=None)
    live_position = {"qty": "3", "avg_entry_price": "210.0", "current_price": "215.0",
                      "unrealized_pl": "15.0", "market_value": "645.0"}
    detail = lp.round_trips_detail("MSFT", rows, open_lifecycle=open_lc, live_position=live_position)
    assert detail["methodology"] == "position_lifecycle_fifo"
    assert len(detail["round_trips"]) == 1
    trip = detail["round_trips"][0]
    assert trip["status"] == "closed"
    assert trip["entry_trade_ids"] == [10]
    assert trip["exit_trade_ids"] == [11]
    assert trip["net_pnl"] == 200.0
    assert trip["return_pct"] == 10.0
    assert trip["holding_days"] == 4.0
    assert detail["open_position"]["qty"] == 3.0


def test_symbol_with_no_lifecycles_at_all_is_isolated_from_other_symbols():
    rows = {"MSFT": [_lc(symbol="MSFT", net_pnl=100.0)]}
    totals = lp.portfolio_totals(rows)
    s = lp.symbol_summary("AAPL", rows, totals, unmatched_sell_qty=0.0)
    assert s["completed_round_trips"] == 0
    assert s["symbol_net_pnl"] == 0.0
    assert s["portfolio_net_pnl_total"] == 100.0

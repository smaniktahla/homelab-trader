"""
Coverage for shared/signals.py::check_portfolio_loss_sell -- the
account-wide, cost-basis-based stop-loss (independent of
circuit_breaker.py's high-water-mark drawdown, which only pauses new BUYs
and never sells). Same test shape as test_exit_taxonomy_integration.py:
runs the real compute_signals() path with symbols=[] so only the
position-level exit checks fire, against a fake Alpaca account/positions
response.
"""

import sys
import pathlib
from datetime import date

import pytest
import requests_mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _dir in (ROOT / "shared", ROOT / "ingest"):
    p = str(_dir)
    if p not in sys.path:
        sys.path.insert(0, p)

for _mod in ("signals",):
    sys.modules.pop(_mod, None)
import signals


def _mean_reversion_thesis_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM theses WHERE slug = 'mean_reversion'")
        return cur.fetchone()[0]


def _set_signal_param(conn, key, value):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO signal_params (key, value, description) VALUES (%s, %s, '')
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, value))
    conn.commit()


@pytest.fixture(autouse=True)
def _reset_param_after(conn):
    yield
    _set_signal_param(conn, "portfolio_stop_loss_pct", 0.05)


@pytest.fixture
def alpaca_base(monkeypatch):
    base = "https://fake-alpaca.test"
    monkeypatch.setenv("ALPACA_BASE_URL", base)
    signals.ALPACA_BASE = base
    signals.ALPACA_HEADERS = {"APCA-API-KEY-ID": "", "APCA-API-SECRET-KEY": ""}
    return base


def _position(symbol, qty, entry, current):
    market_value = current * qty
    return {
        "symbol": symbol, "qty": str(qty), "avg_entry_price": str(entry),
        "current_price": str(current), "market_value": str(market_value),
        "unrealized_plpc": str((current - entry) / entry),
    }


def _run_with_positions(conn, alpaca_base, cash, portfolio_value, positions):
    with requests_mock.Mocker() as m:
        m.get(f"{alpaca_base}/v2/account", json={"cash": str(cash), "portfolio_value": str(portfolio_value)})
        m.get(f"{alpaca_base}/v2/positions", json=positions)
        signals.compute_signals(conn, [])


def _sell_proposals(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, exit_reason, qty FROM trade_proposals
            WHERE side='sell' ORDER BY symbol
        """)
        return cur.fetchall()


def test_no_sell_proposal_when_book_loss_under_threshold(conn, alpaca_base):
    _mean_reversion_thesis_id(conn)
    _set_signal_param(conn, "portfolio_stop_loss_pct", 0.12)
    # One position down 5% -- well under the 12% threshold
    positions = [_position("AAPL", 10, 100.0, 95.0)]

    _run_with_positions(conn, alpaca_base, cash=9000, portfolio_value=9950, positions=positions)

    assert [r for r in _sell_proposals(conn) if r[1] == "portfolio_stop_loss"] == []


def test_sell_proposals_created_for_losing_positions_only(conn, alpaca_base):
    _mean_reversion_thesis_id(conn)
    # portfolio_stop_loss_pct must stay BELOW stop_loss_pct (default 8%,
    # see check_stop_losses) for this check to ever fire on its own -- per
    # check_portfolio_loss_sell's docstring, a threshold ABOVE stop_loss_pct
    # can (almost) never trigger before check_stop_losses already has, on
    # whichever position is driving the loss, which claims the
    # _open_sell_exists() dedup slot first and starves this check's own
    # insert. Use 0.05, matching the real signal_params default.
    _set_signal_param(conn, "portfolio_stop_loss_pct", 0.05)
    # AAPL is down 7.9% -- under its own 8% stop_loss_pct, so
    # check_stop_losses leaves it alone -- but at a $10,000 cost basis it
    # dominates the book. MSFT is up 5% on a much smaller $200 cost basis,
    # so it can't dilute AAPL's loss enough to pull the combined book back
    # under the 5% portfolio threshold.
    # AAPL: cost basis $10,000, loss = $790. MSFT: cost basis $200, gain = $10.
    # Net: -$780 / $10,200 = ~7.65%, over the 5% portfolio threshold.
    positions = [
        _position("AAPL", 100, 100.0, 92.10),  # down 7.9%, cost basis $10,000
        _position("MSFT", 1, 200.0, 210.0),    # up 5%, cost basis $200
    ]

    _run_with_positions(conn, alpaca_base, cash=1000, portfolio_value=1000 + 9210 + 210, positions=positions)

    rows = [r for r in _sell_proposals(conn) if r[1] == "portfolio_stop_loss"]
    assert len(rows) == 1
    symbol, exit_reason, qty = rows[0]
    assert symbol == "AAPL"  # the losing position, not the profitable MSFT
    assert float(qty) == 100.0


def test_no_duplicate_when_sell_already_open(conn, alpaca_base):
    thesis_id = _mean_reversion_thesis_id(conn)
    _set_signal_param(conn, "portfolio_stop_loss_pct", 0.12)
    positions = [_position("AAPL", 10, 100.0, 70.0)]  # down 30%, well over threshold
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO trade_proposals (symbol, side, qty, rationale, signal_score, thesis_id)
            VALUES ('AAPL', 'sell', 10, 'already open', 50, %s)
        """, (thesis_id,))
    conn.commit()

    _run_with_positions(conn, alpaca_base, cash=9300, portfolio_value=9300 + 700, positions=positions)

    rows = _sell_proposals(conn)
    assert len(rows) == 1  # the pre-existing one, no new portfolio_stop_loss row added

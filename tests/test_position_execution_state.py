"""
Tests for shared/position_execution_state.py -- pure classifier, no DB
fixture needed for classify_position_execution_state() itself, same
reasoning as tests/test_market_structure.py's pure-function section.

Table-driven for the ambiguous combinations (resting stop + open
proposal, resting stop + submitted sell, etc.) -- these are exactly the
cases prone to silently regressing later, per the PR review feedback that
prompted this file.
"""

import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
p = str(ROOT / "shared")
if p not in sys.path:
    sys.path.insert(0, p)

sys.modules.pop("position_execution_state", None)
import position_execution_state as pes


def _order(side="sell", type_="stop"):
    return {"side": side, "type": type_}


_OPEN_PROPOSAL = {"id": 100, "symbol": "APP", "side": "sell", "decision": None}

# (description, pending_orders, open_sell_proposal, expected_state)
CASES = [
    (
        "no orders, no proposal -> owned",
        [],
        None,
        pes.STATE_OWNED,
    ),
    (
        "resting stop only -> protected",
        [_order(type_="stop")],
        None,
        pes.STATE_PROTECTED,
    ),
    (
        "resting stop_limit only -> protected",
        [_order(type_="stop_limit")],
        None,
        pes.STATE_PROTECTED,
    ),
    (
        "open proposal, no orders -> exit_recommended",
        [],
        _OPEN_PROPOSAL,
        pes.STATE_EXIT_RECOMMENDED,
    ),
    (
        "resting stop AND open proposal -> exit_recommended wins, not protected",
        [_order(type_="stop")],
        _OPEN_PROPOSAL,
        pes.STATE_EXIT_RECOMMENDED,
    ),
    (
        "submitted market sell only -> sell_pending",
        [_order(type_="market")],
        None,
        pes.STATE_SELL_PENDING,
    ),
    (
        "submitted market sell AND resting stop -> sell_pending wins",
        [_order(type_="market"), _order(type_="stop")],
        None,
        pes.STATE_SELL_PENDING,
    ),
    (
        "submitted market sell AND open proposal -> sell_pending wins",
        [_order(type_="market")],
        _OPEN_PROPOSAL,
        pes.STATE_SELL_PENDING,
    ),
    (
        "buy-side stop order present (OTO leg on a DIFFERENT still-pending "
        "buy) must not be mistaken for a protective sell stop -> owned",
        [_order(side="buy", type_="stop")],
        None,
        pes.STATE_OWNED,
    ),
    (
        "buy-side market order present -> must not be mistaken for sell_pending",
        [_order(side="buy", type_="market")],
        None,
        pes.STATE_OWNED,
    ),
    (
        "limit sell order (not market, not stop) -> not protected, not "
        "sell_pending -- falls through to owned since it's neither a "
        "protective stop nor the market-sell shape this app itself submits",
        [_order(type_="limit")],
        None,
        pes.STATE_OWNED,
    ),
    (
        "pending_orders is None (not just empty list) -> owned, no crash",
        None,
        None,
        pes.STATE_OWNED,
    ),
]


def test_classify_position_execution_state_table():
    for description, pending_orders, open_sell_proposal, expected in CASES:
        actual = pes.classify_position_execution_state(pending_orders, open_sell_proposal)
        assert actual == expected, f"{description}: expected {expected!r}, got {actual!r}"


def test_all_states_are_in_valid_states():
    for _, pending_orders, open_sell_proposal, expected in CASES:
        assert expected in pes.VALID_STATES

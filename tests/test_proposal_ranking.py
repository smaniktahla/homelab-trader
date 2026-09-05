"""
Tests for shared/proposal_ranking.py -- pure, no DB needed.

Covers the internal cascade/helper functions individually (same
"branch-table" style as test_market_regime_history.py's
classify_overall() coverage) plus rank_proposals() end-to-end on a small
multi-sector fixture. Deliberately does NOT touch score_signal()/
compute_signals() -- this module is read-time only, so
tests/test_fixture_equivalence*.py is unaffected by anything here.
"""

import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _dir in (ROOT / "shared",):
    p = str(_dir)
    if p not in sys.path:
        sys.path.insert(0, p)

for _mod in ("proposal_ranking",):
    sys.modules.pop(_mod, None)
import proposal_ranking as pr


DEFAULT_PARAMS = dict(pr.PROPOSAL_RANKING_DEFAULTS, sector_max_pct=0.30)
DISABLED_PARAMS = dict(DEFAULT_PARAMS, proposal_ranking_enabled=0)


def _proposal(id, symbol, side="buy", qty=10, signal_score=70, final_proposal_score=None, current_price=100.0):
    return {
        "id": id, "symbol": symbol, "side": side, "qty": qty,
        "signal_score": signal_score, "final_proposal_score": final_proposal_score,
        "current_price": current_price, "rationale": f"rationale for {symbol}",
    }


# ─────────────────────────────────────────────────────────────────────────
# _assign_tier -- every cascade branch
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "cluster_rank,cluster_size,saturation_ratio,risk_outcome,affordable,expected_tier",
    [
        # rejected / unaffordable always wins, regardless of rank
        (1, 1, 0.0, "rejected", True, pr.TIER_MINIMAL),
        (1, 1, 0.0, "approved", False, pr.TIER_MINIMAL),
        # rank 1, clean
        (1, 1, 0.0, "approved", True, pr.TIER_HIGHEST),
        (1, 3, 0.2, None, True, pr.TIER_HIGHEST),
        # rank 1, but reduced by the risk engine
        (1, 1, 0.0, "reduced", True, pr.TIER_GOOD),
        # rank 1, but sector already saturated past the watch threshold
        (1, 3, 0.9, "approved", True, pr.TIER_GOOD),
        # not rank 1, sector at/over hard cap
        (2, 3, 1.0, "approved", True, pr.TIER_MINIMAL),
        (3, 3, 1.5, "approved", True, pr.TIER_MINIMAL),
        # not rank 1, saturated past watch threshold but under hard cap
        (2, 3, 0.8, "approved", True, pr.TIER_LOW),
        # not rank 1, reduced by risk engine (even if sector isn't saturated)
        (2, 3, 0.1, "reduced", True, pr.TIER_LOW),
        # not rank 1, clean, within top third of a bigger cluster
        (2, 6, 0.1, "approved", True, pr.TIER_REASONABLE),
        # not rank 1, clean, outside the top third -- falls to LOW
        (5, 6, 0.1, "approved", True, pr.TIER_LOW),
        # small cluster (size 2): rank 2 is NOT within max(2, size//3)=2's
        # "cluster_rank <= 2" bound... actually it IS (rank 2 <= 2) -- REASONABLE
        (2, 2, 0.1, "approved", True, pr.TIER_REASONABLE),
    ],
)
def test_assign_tier_cascade(cluster_rank, cluster_size, saturation_ratio, risk_outcome, affordable, expected_tier):
    tier = pr._assign_tier(cluster_rank, cluster_size, saturation_ratio, risk_outcome, affordable,
                            watch_threshold=0.75)
    assert tier == expected_tier


# ─────────────────────────────────────────────────────────────────────────
# _build_sector_clusters
# ─────────────────────────────────────────────────────────────────────────

def test_build_sector_clusters_groups_by_sector_and_sorts_by_score_desc():
    proposals = [
        _proposal(1, "WEC", signal_score=60),
        _proposal(2, "NI", signal_score=80),
        _proposal(3, "AEP", signal_score=70),
    ]
    sector_map = {"WEC": "Utilities", "NI": "Utilities", "AEP": "Utilities"}
    clusters = pr._build_sector_clusters(proposals, sector_map)
    assert list(clusters.keys()) == ["sector:Utilities"]
    ranked_symbols = [p["symbol"] for p in clusters["sector:Utilities"]]
    assert ranked_symbols == ["NI", "AEP", "WEC"]


def test_build_sector_clusters_unmapped_sector_is_a_singleton():
    proposals = [_proposal(1, "ZZZZ", signal_score=90)]
    clusters = pr._build_sector_clusters(proposals, sector_map={})
    assert list(clusters.keys()) == ["symbol:ZZZZ"]
    assert len(clusters["symbol:ZZZZ"]) == 1


def test_build_sector_clusters_ties_broken_by_symbol():
    proposals = [_proposal(1, "BBB", signal_score=70), _proposal(2, "AAA", signal_score=70)]
    sector_map = {"BBB": "Energy", "AAA": "Energy"}
    clusters = pr._build_sector_clusters(proposals, sector_map)
    ranked_symbols = [p["symbol"] for p in clusters["sector:Energy"]]
    assert ranked_symbols == ["AAA", "BBB"]


# ─────────────────────────────────────────────────────────────────────────
# _effective_sector_dollars
# ─────────────────────────────────────────────────────────────────────────

def test_effective_sector_dollars_position_only():
    positions = {"AEP": {"market_value": 1000.0}, "MSFT": {"market_value": 500.0}}
    sector_map = {"AEP": "Utilities", "MSFT": "Information Technology"}
    total = pr._effective_sector_dollars("Utilities", positions, [], sector_map, {})
    assert total == 1000.0


def test_effective_sector_dollars_pending_buy_not_yet_a_position():
    positions = {}
    pending = [{"symbol": "WEC", "side": "buy", "qty": 5, "status": "open"}]
    sector_map = {"WEC": "Utilities"}
    price_map = {"WEC": 90.0}
    total = pr._effective_sector_dollars("Utilities", positions, pending, sector_map, price_map)
    assert total == 450.0


def test_effective_sector_dollars_pending_sell_reduces_and_floors_at_zero():
    positions = {"AEP": {"market_value": 100.0}}
    pending = [{"symbol": "AEP", "side": "sell", "qty": 5, "status": "open"}]
    sector_map = {"AEP": "Utilities"}
    price_map = {"AEP": 50.0}  # notional 250 > 100 position value
    total = pr._effective_sector_dollars("Utilities", positions, pending, sector_map, price_map)
    assert total == 0.0


def test_effective_sector_dollars_mixed():
    positions = {"AEP": {"market_value": 1000.0}}
    pending = [
        {"symbol": "WEC", "side": "buy", "qty": 10, "status": "open"},   # not held yet -> add
        {"symbol": "AEP", "side": "sell", "qty": 2, "status": "open"},  # held -> subtract
    ]
    sector_map = {"AEP": "Utilities", "WEC": "Utilities"}
    price_map = {"WEC": 90.0, "AEP": 100.0}
    total = pr._effective_sector_dollars("Utilities", positions, pending, sector_map, price_map)
    assert total == 1000.0 + 900.0 - 200.0


def test_effective_sector_dollars_unmapped_sector_is_zero():
    assert pr._effective_sector_dollars(None, {}, [], {}, {}) == 0.0


# ─────────────────────────────────────────────────────────────────────────
# _projected_buying_power
# ─────────────────────────────────────────────────────────────────────────

def test_projected_buying_power_pending_sell_adds_proceeds():
    pending = [{"symbol": "AEP", "side": "sell", "qty": 5, "status": "open"}]
    price_map = {"AEP": 100.0}
    assert pr._projected_buying_power(1000.0, pending, price_map) == 1500.0


def test_projected_buying_power_pending_buy_does_not_double_subtract():
    pending = [{"symbol": "WEC", "side": "buy", "qty": 5, "status": "open"}]
    price_map = {"WEC": 90.0}
    assert pr._projected_buying_power(1000.0, pending, price_map) == 1000.0


def test_projected_buying_power_none_passthrough():
    assert pr._projected_buying_power(None, [], {}) is None


# ─────────────────────────────────────────────────────────────────────────
# rank_proposals -- end to end
# ─────────────────────────────────────────────────────────────────────────

def test_rank_proposals_disabled_returns_input_unchanged():
    proposals = [_proposal(1, "AEP")]
    result = pr.rank_proposals(proposals, {}, [], {}, 100000.0, 100000.0, {"AEP": 100.0}, {}, DISABLED_PARAMS)
    assert result == proposals
    assert "priority_tier" not in result[0]


def test_rank_proposals_empty_input():
    assert pr.rank_proposals([], {}, [], {}, 1000.0, 1000.0, {}, {}, DEFAULT_PARAMS) == []


def test_rank_proposals_sell_always_highest_tier_no_clustering():
    proposals = [_proposal(1, "AEP", side="sell", qty=5)]
    result = pr.rank_proposals(proposals, {}, [], {"AEP": "Utilities"}, 1000.0, 1000.0,
                                {"AEP": 100.0}, {}, DEFAULT_PARAMS)
    row = result[0]
    assert row["priority_tier"] == pr.TIER_HIGHEST
    assert row["recommended_action"] == "Sell"
    assert row["cluster_id"] is None
    assert row["opportunity_cost_note"] is None


def test_rank_proposals_end_to_end_two_sectors_plus_sell_passthrough_fields_unchanged():
    proposals = [
        _proposal(1, "NI", signal_score=85, current_price=50.0, qty=10),   # Utilities best
        _proposal(2, "AEP", signal_score=70, current_price=90.0, qty=10),  # Utilities #2
        _proposal(3, "WEC", signal_score=60, current_price=90.0, qty=10),  # Utilities #3
        _proposal(4, "MSFT", signal_score=90, current_price=300.0, qty=5),  # tech, singleton
        _proposal(5, "XOM", side="sell", signal_score=None, current_price=110.0, qty=8),
    ]
    positions = {}
    pending_orders = []
    sector_map = {"NI": "Utilities", "AEP": "Utilities", "WEC": "Utilities", "MSFT": "Information Technology"}
    price_map = {"NI": 50.0, "AEP": 90.0, "WEC": 90.0, "MSFT": 300.0, "XOM": 110.0}
    buying_power = 1_000_000.0
    portfolio_value = 1_000_000.0
    risk_decisions = {}

    result = pr.rank_proposals(proposals, positions, pending_orders, sector_map,
                                buying_power, portfolio_value, price_map, risk_decisions, DEFAULT_PARAMS)

    by_id = {r["id"]: r for r in result}

    # Non-ranking fields pass through byte-identical.
    assert by_id[1]["rationale"] == "rationale for NI"
    assert by_id[1]["qty"] == 10
    assert by_id[1]["signal_score"] == 85

    # NI is the sector's best-scored candidate -> rank 1, top tier, no opportunity cost note.
    assert by_id[1]["cluster_rank"] == 1
    assert by_id[1]["priority_tier"] == pr.TIER_HIGHEST
    assert by_id[1]["opportunity_cost_note"] is None
    assert by_id[1]["cluster_label"] == "Utilities (3)"

    # AEP/WEC are lower-ranked alternatives in the same cluster with a note.
    assert by_id[2]["cluster_rank"] == 2
    assert "NI" in by_id[2]["competes_with"]
    assert by_id[2]["opportunity_cost_note"] is not None

    assert by_id[3]["cluster_rank"] == 3

    # MSFT has no sector-mates -> singleton, no cluster_id/label, top tier.
    assert by_id[4]["cluster_id"] is None
    assert by_id[4]["cluster_label"] is None
    assert by_id[4]["priority_tier"] == pr.TIER_HIGHEST

    # The sell proposal is untouched by clustering.
    assert by_id[5]["side"] == "sell"
    assert by_id[5]["recommended_action"] == "Sell"


def test_rank_proposals_unaffordable_when_buying_power_exhausted():
    # Two singleton (unmapped-sector) proposals, budget only covers the first.
    proposals = [
        _proposal(1, "AAA", signal_score=90, current_price=1000.0, qty=1),
        _proposal(2, "BBB", signal_score=80, current_price=1000.0, qty=1),
    ]
    price_map = {"AAA": 1000.0, "BBB": 1000.0}
    result = pr.rank_proposals(proposals, {}, [], {}, 1000.0, 100000.0, price_map, {}, DEFAULT_PARAMS)
    by_id = {r["id"]: r for r in result}
    assert by_id[1]["priority_tier"] == pr.TIER_HIGHEST
    assert by_id[2]["priority_tier"] == pr.TIER_MINIMAL


def test_rank_proposals_reflects_risk_engine_reduced_outcome():
    proposals = [_proposal(1, "AAA", signal_score=90, current_price=10.0, qty=1)]
    price_map = {"AAA": 10.0}
    risk_decisions = {1: {"outcome": "reduced", "binding_constraint": "portfolio_open_risk"}}
    result = pr.rank_proposals(proposals, {}, [], {}, 100000.0, 100000.0, price_map, risk_decisions, DEFAULT_PARAMS)
    assert result[0]["priority_tier"] == pr.TIER_GOOD


def test_rank_proposals_reflects_risk_engine_rejected_outcome():
    proposals = [_proposal(1, "AAA", signal_score=90, current_price=10.0, qty=1)]
    price_map = {"AAA": 10.0}
    risk_decisions = {1: {"outcome": "rejected", "binding_constraint": "buying_power"}}
    result = pr.rank_proposals(proposals, {}, [], {}, 100000.0, 100000.0, price_map, risk_decisions, DEFAULT_PARAMS)
    assert result[0]["priority_tier"] == pr.TIER_MINIMAL


def test_rank_proposals_surfaces_risk_decision_fields_on_the_row():
    """Bug fix: a proposal's row must expose what the risk engine actually
    approved (outcome/binding_constraint/approved_quantity), not just use
    that data internally to pick a priority_tier and discard it -- this is
    what lets the dashboard show '17 requested -> 3 approved
    (portfolio_open_risk)' instead of only discovering the mismatch after
    a rejected Approve click."""
    proposals = [_proposal(1, "AAA", signal_score=90, current_price=10.0, qty=17)]
    price_map = {"AAA": 10.0}
    risk_decisions = {1: {"outcome": "reduced", "binding_constraint": "portfolio_open_risk", "approved_quantity": 3}}
    result = pr.rank_proposals(proposals, {}, [], {}, 100000.0, 100000.0, price_map, risk_decisions, DEFAULT_PARAMS)
    assert result[0]["risk_outcome"] == "reduced"
    assert result[0]["risk_binding_constraint"] == "portfolio_open_risk"
    assert result[0]["risk_approved_quantity"] == 3
    assert result[0]["qty"] == 17  # the strategy's own requested qty is never overwritten


def test_rank_proposals_missing_risk_decision_gives_none_fields_not_a_crash():
    proposals = [_proposal(1, "AAA", signal_score=90, current_price=10.0, qty=5)]
    price_map = {"AAA": 10.0}
    result = pr.rank_proposals(proposals, {}, [], {}, 100000.0, 100000.0, price_map, {}, DEFAULT_PARAMS)
    assert result[0]["risk_outcome"] is None
    assert result[0]["risk_binding_constraint"] is None
    assert result[0]["risk_approved_quantity"] is None

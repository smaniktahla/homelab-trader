"""
Tests for shared/regime_scoring.py::compute_regime_adjustment -- pure,
no DB needed. Covers every config-key combination from the PR spec's
example config, the enabled/disabled switch, and unknown-input handling.
"""

import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
p = str(ROOT / "shared")
if p not in sys.path:
    sys.path.insert(0, p)

for _mod in ("regime_scoring",):
    sys.modules.pop(_mod, None)
import regime_scoring as rs


ENABLED_PARAMS = dict(rs.REGIME_SCORING_DEFAULTS, regime_scoring_enabled=1)


def test_disabled_by_default_yields_zero_adjustment():
    params = dict(rs.REGIME_SCORING_DEFAULTS)
    adj = rs.compute_regime_adjustment("bull_calm", "strong_bullish", "outperforming_sector", params)
    assert adj == {
        "market_regime_adjustment": 0, "sector_regime_adjustment": 0,
        "relative_strength_adjustment": 0, "total_regime_adjustment": 0,
    }


def test_market_bull_sector_bull():
    adj = rs.compute_regime_adjustment("bull_calm", "bullish", None, ENABLED_PARAMS)
    assert adj["sector_regime_adjustment"] == 15
    assert adj["total_regime_adjustment"] == 15


def test_market_bull_sector_neutral():
    adj = rs.compute_regime_adjustment("bull_volatile", "neutral", None, ENABLED_PARAMS)
    assert adj["sector_regime_adjustment"] == 5


def test_market_bull_sector_bear():
    adj = rs.compute_regime_adjustment("bull_calm", "strong_bearish", None, ENABLED_PARAMS)
    assert adj["sector_regime_adjustment"] == -10


def test_market_bear_sector_bull():
    adj = rs.compute_regime_adjustment("bear_calm", "strong_bullish", None, ENABLED_PARAMS)
    assert adj["sector_regime_adjustment"] == 0


def test_market_bear_sector_neutral():
    adj = rs.compute_regime_adjustment("bear_fear", "neutral", None, ENABLED_PARAMS)
    assert adj["sector_regime_adjustment"] == -10


def test_market_bear_sector_bear():
    adj = rs.compute_regime_adjustment("bear_volatile", "bearish", None, ENABLED_PARAMS)
    assert adj["sector_regime_adjustment"] == -20


def test_stock_outperform_sector():
    adj = rs.compute_regime_adjustment("neutral", "unknown", "outperforming_sector", ENABLED_PARAMS)
    assert adj["relative_strength_adjustment"] == 5


def test_stock_underperform_sector():
    adj = rs.compute_regime_adjustment("neutral", "unknown", "underperforming_sector", ENABLED_PARAMS)
    assert adj["relative_strength_adjustment"] == -5


def test_combined_market_sector_and_relative_strength():
    adj = rs.compute_regime_adjustment("bull_calm", "bullish", "outperforming_sector", ENABLED_PARAMS)
    assert adj["sector_regime_adjustment"] == 15
    assert adj["relative_strength_adjustment"] == 5
    assert adj["total_regime_adjustment"] == 20


def test_unknown_market_never_guesses():
    """Neutral/unknown market has no configured row -- adjustment must stay
    zero rather than falling back to some other bucket."""
    adj = rs.compute_regime_adjustment("unknown", "bullish", None, ENABLED_PARAMS)
    assert adj["sector_regime_adjustment"] == 0


def test_unknown_sector_never_guesses():
    adj = rs.compute_regime_adjustment("bull_calm", "insufficient_data", None, ENABLED_PARAMS)
    assert adj["sector_regime_adjustment"] == 0


def test_unknown_vs_sector_never_guesses():
    adj = rs.compute_regime_adjustment("bull_calm", "bullish", "unknown", ENABLED_PARAMS)
    assert adj["relative_strength_adjustment"] == 0


def test_custom_config_values_respected():
    params = dict(ENABLED_PARAMS, regime_mkt_bull_sector_bull=42)
    adj = rs.compute_regime_adjustment("bull_calm", "bullish", None, params)
    assert adj["sector_regime_adjustment"] == 42


def test_bucket_market_trend():
    assert rs.bucket_market_trend("bull_calm") == "bull"
    assert rs.bucket_market_trend("bull_volatile") == "bull"
    assert rs.bucket_market_trend("bear_fear") == "bear"
    assert rs.bucket_market_trend("fear") == "bear"
    assert rs.bucket_market_trend("neutral") is None
    assert rs.bucket_market_trend("unknown") is None


def test_bucket_sector_trend():
    assert rs.bucket_sector_trend("strong_bullish") == "bull"
    assert rs.bucket_sector_trend("bullish") == "bull"
    assert rs.bucket_sector_trend("strong_bearish") == "bear"
    assert rs.bucket_sector_trend("bearish") == "bear"
    assert rs.bucket_sector_trend("neutral") == "neutral"
    assert rs.bucket_sector_trend("unknown") is None
    assert rs.bucket_sector_trend("insufficient_data") is None

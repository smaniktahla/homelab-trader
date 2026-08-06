"""
Tests for shared/structure_scoring.py::compute_structure_adjustment --
pure, no DB needed. Same coverage shape as test_regime_scoring.py: the
enabled/disabled switch, every trend/event branch, and mixed/
insufficient_data never guessing a direction.
"""

import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
p = str(ROOT / "shared")
if p not in sys.path:
    sys.path.insert(0, p)

for _mod in ("structure_scoring",):
    sys.modules.pop(_mod, None)
import structure_scoring as ss


ENABLED_PARAMS = dict(ss.STRUCTURE_SCORING_DEFAULTS, structure_scoring_enabled=1)


def _snap(trend="bullish", bos=False, choch=False):
    return {"trend": trend, "bos": bos, "choch": choch}


def test_disabled_by_default_yields_zero_adjustment():
    params = dict(ss.STRUCTURE_SCORING_DEFAULTS)
    adj = ss.compute_structure_adjustment(_snap("bullish", bos=True, choch=True), params)
    assert adj == {
        "structure_trend_adjustment": 0, "structure_event_adjustment": 0,
        "total_structure_adjustment": 0,
    }


def test_bullish_trend_adjustment():
    adj = ss.compute_structure_adjustment(_snap("bullish"), ENABLED_PARAMS)
    assert adj["structure_trend_adjustment"] == 10
    assert adj["total_structure_adjustment"] == 10


def test_bearish_trend_adjustment():
    adj = ss.compute_structure_adjustment(_snap("bearish"), ENABLED_PARAMS)
    assert adj["structure_trend_adjustment"] == -10


def test_mixed_trend_never_guesses():
    adj = ss.compute_structure_adjustment(_snap("mixed"), ENABLED_PARAMS)
    assert adj["structure_trend_adjustment"] == 0


def test_insufficient_data_trend_never_guesses():
    adj = ss.compute_structure_adjustment(_snap("insufficient_data"), ENABLED_PARAMS)
    assert adj["structure_trend_adjustment"] == 0


def test_choch_penalty():
    adj = ss.compute_structure_adjustment(_snap("mixed", choch=True), ENABLED_PARAMS)
    assert adj["structure_event_adjustment"] == -10
    assert adj["total_structure_adjustment"] == -10


def test_bos_bonus():
    adj = ss.compute_structure_adjustment(_snap("mixed", bos=True), ENABLED_PARAMS)
    assert adj["structure_event_adjustment"] == 5


def test_trend_and_event_adjustments_combine():
    adj = ss.compute_structure_adjustment(_snap("bullish", bos=True), ENABLED_PARAMS)
    assert adj["structure_trend_adjustment"] == 10
    assert adj["structure_event_adjustment"] == 5
    assert adj["total_structure_adjustment"] == 15


def test_custom_config_values_respected():
    params = dict(ENABLED_PARAMS, structure_trend_bullish=42)
    adj = ss.compute_structure_adjustment(_snap("bullish"), params)
    assert adj["structure_trend_adjustment"] == 42

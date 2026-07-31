import pytest

from signal_components import weighted_component_score


def test_available_weight_normalization():
    # Only technical + news available; fundamental/earnings/options/macro_fit
    # missing. Result should be the weighted average over the two available
    # components only, renormalized by their own weights (not the full set).
    scores = {"technical": 80, "news": 40, "fundamental": None, "earnings": None}
    weights = {"technical": 0.5, "news": 0.3, "fundamental": 0.1, "earnings": 0.1}
    result = weighted_component_score(scores, weights)
    expected = (80 * 0.5 + 40 * 0.3) / (0.5 + 0.3)
    assert result == pytest.approx(expected)


def test_all_components_missing_returns_none():
    scores = {"technical": None, "fundamental": None, "news": None}
    weights = {"technical": 0.5, "fundamental": 0.3, "news": 0.2}
    assert weighted_component_score(scores, weights) is None


def test_missing_component_not_treated_as_zero():
    # A present-but-zero score must differ from a missing (None) one.
    weights = {"technical": 0.5, "news": 0.5}
    zero_present = weighted_component_score({"technical": 0, "news": 80}, weights)
    missing = weighted_component_score({"technical": None, "news": 80}, weights)
    assert zero_present == pytest.approx((0 * 0.5 + 80 * 0.5) / 1.0)
    assert missing == 80  # renormalized over news alone, not dragged toward 0
    assert zero_present != missing


def test_single_component_weight_one_is_identity():
    # This is PR #1's actual live usage: composite == technical_score exactly.
    assert weighted_component_score({"technical": 62}, {"technical": 1.0}) == 62


def test_zero_available_weight_returns_none():
    # technical is available but carries zero weight, and nothing else is
    # available either -- dividing by zero must not happen, and the result
    # must not silently read as "score of 0" (maximally bearish).
    scores = {"technical": 55, "fundamental": None}
    weights = {"technical": 0.0, "fundamental": 0.4}
    assert weighted_component_score(scores, weights) is None


def test_rejects_negative_weight():
    with pytest.raises(ValueError):
        weighted_component_score({"technical": 50, "news": 10}, {"technical": 1.2, "news": -0.2})

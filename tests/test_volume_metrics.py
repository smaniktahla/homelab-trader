"""
PR A, Volume & Volume Profile epic. Pure unit tests, no DB needed -- these
functions are stateless calculations only.
"""

from volume_metrics import dollar_volume, relative_volume, volume_percentile, volume_sma, volume_zscore


# --- dollar_volume -----------------------------------------------------------

def test_dollar_volume_basic():
    assert dollar_volume(10.0, 1000) == 10000.0


def test_dollar_volume_none_inputs():
    assert dollar_volume(None, 1000) is None
    assert dollar_volume(10.0, None) is None
    assert dollar_volume(None, None) is None


# --- volume_sma ----------------------------------------------------------------

def test_volume_sma_matches_manual_average():
    volumes = [100, 200, 300, 400, 500]
    assert volume_sma(volumes, 5) == 300.0


def test_volume_sma_uses_trailing_window_only():
    volumes = [9999, 100, 200, 300]  # first value out of window
    assert volume_sma(volumes, 3) == 200.0


def test_volume_sma_none_when_too_short():
    assert volume_sma([100, 200], 5) is None


# --- relative_volume -------------------------------------------------------------

def test_relative_volume_is_one_when_at_trailing_average():
    volumes = [200, 200, 200, 200, 200]
    assert relative_volume(volumes, 5) == 1.0


def test_relative_volume_spike_above_one():
    volumes = [100, 100, 100, 100, 500]
    # sma = (100*4 + 500)/5 = 180, rvol = 500/180
    assert relative_volume(volumes, 5) == 500 / 180


def test_relative_volume_none_when_too_short():
    assert relative_volume([100, 200], 5) is None


def test_relative_volume_none_when_sma_is_zero():
    assert relative_volume([0, 0, 0], 3) is None


# --- volume_zscore ---------------------------------------------------------------

def test_volume_zscore_known_values():
    volumes = [100, 100, 100, 100, 200]
    # mean = 120, variance = [4*(100-120)^2 + (200-120)^2]/5 = [1600+6400]/5=1600, std=40
    # z = (200-120)/40 = 2.0
    assert volume_zscore(volumes, 5) == 2.0


def test_volume_zscore_zero_std_returns_none():
    assert volume_zscore([100, 100, 100], 3) is None


def test_volume_zscore_none_when_too_short():
    assert volume_zscore([100], 3) is None


# --- volume_percentile -----------------------------------------------------------

def test_volume_percentile_highest_value_is_one():
    volumes = [100, 200, 300, 400, 500]
    assert volume_percentile(volumes, 5) == 1.0


def test_volume_percentile_lowest_value_is_smallest_fraction():
    volumes = [500, 400, 300, 200, 100]
    assert volume_percentile(volumes, 5) == 1 / 5


def test_volume_percentile_known_middle_rank():
    volumes = [100, 500, 400, 300, 200]  # latest=200, values <= 200: {100, 200} = 2/5
    assert volume_percentile(volumes, 5) == 2 / 5


def test_volume_percentile_none_when_too_short():
    assert volume_percentile([100, 200], 5) is None

"""
Tests for shared/volatility_forecast.py -- VR-1 of the Volatility
Forecasting & Risk-Targeted Position Sizing epic (see
docs/volatility-sizing-vr0-reconciliation.md).

Two tiers, same shape as tests/test_market_structure.py /
tests/test_security_regime.py: pure-function unit tests against
hand-computed fixtures first (no DB), then a handful of
compute_volatility_forecast()/persistence integration tests against a real
Postgres connection for the causality/truncation and store/load guarantees
that only make sense with actual as-of-date slicing.
"""

import math
import sys
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
p = str(ROOT / "shared")
if p not in sys.path:
    sys.path.insert(0, p)

for _mod in ("volatility_forecast", "market_structure", "regime_common"):
    sys.modules.pop(_mod, None)
import volatility_forecast as vf

# tests/conftest.py's `conn` fixture (session-scoped schema + per-test
# truncate) is picked up automatically.


def _closes_from_returns(start, rets):
    """Builds a closes series (len(rets)+1) whose log returns are exactly
    `rets`, via closes[i] = closes[i-1] * exp(rets[i-1]) -- lets a test
    fixture specify returns directly instead of reverse-engineering prices."""
    closes = [start]
    for r in rets:
        closes.append(closes[-1] * math.exp(r))
    return closes


def _insert_prices(conn, symbol, start_date, closes):
    with conn.cursor() as cur:
        for i, c in enumerate(closes):
            d = start_date + timedelta(days=i)
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, (symbol, d, c, c, c, c, 1000))
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────
# realized_vol_daily -- pure, hand-calculated
# ─────────────────────────────────────────────────────────────────────────

def test_realized_vol_hand_calculated():
    """rets = [0.01, -0.02, 0.03, -0.01]; sample stdev (ddof=1) computed by
    hand: mean=0.0025, deviations=[0.0075,-0.0225,0.0275,-0.0125], sum of
    squared deviations=0.001475, /3=0.00049167, sqrt=0.0221736..."""
    rets = [0.01, -0.02, 0.03, -0.01]
    closes = _closes_from_returns(100.0, rets)
    daily_vol, n, status = vf.realized_vol_daily(closes, window=4)
    assert status == vf.STATUS_OK
    assert n == 4
    expected = math.sqrt(0.001475 / 3)
    assert daily_vol == pytest.approx(expected)


def test_realized_vol_insufficient_history():
    closes = _closes_from_returns(100.0, [0.01, -0.01])
    daily_vol, n, status = vf.realized_vol_daily(closes, window=10)
    assert status == vf.STATUS_INSUFFICIENT_HISTORY
    assert daily_vol is None


def test_realized_vol_zero_price_is_nonfinite_not_a_crash():
    closes = [100.0, 0.0, 101.0, 99.0, 102.0, 100.5]
    daily_vol, n, status = vf.realized_vol_daily(closes, window=5)
    assert status == vf.STATUS_ZERO_OR_NONFINITE
    assert daily_vol is None


def test_realized_vol_flat_series_is_a_valid_zero():
    """A perfectly flat price series has zero return dispersion -- this is
    a legitimate (if degenerate) daily_vol of 0.0, not an error status.
    zero_or_nonfinite is reserved for actually-invalid inputs (non-positive
    prices), not for a valid-but-boring answer."""
    closes = [100.0] * 10
    daily_vol, n, status = vf.realized_vol_daily(closes, window=9)
    assert status == vf.STATUS_OK
    assert daily_vol == pytest.approx(0.0)


# ─────────────────────────────────────────────────────────────────────────
# ewma_vol_daily -- pure, hand-calculated recurrence
# ─────────────────────────────────────────────────────────────────────────

def test_ewma_recurrence_hand_calculated():
    """rets = [0.02, -0.01, 0.03, 0.01, -0.02], lam=0.9, min_periods=3.

    Seed (population variance of first 3 returns, demeaned):
      seed_mean = (0.02 - 0.01 + 0.03) / 3 = 0.013333...
      deviations: 0.006667, -0.023333, 0.016667
      sum of squares = 0.00044444 + 0.00054444 + 0.00027778 = 0.00086667
      seed_var = 0.00086667 / 3 = 0.00028889

    Recurse over rets[3:] = [0.01, -0.02]:
      var1 = 0.9*0.00028889 + 0.1*0.01^2   = 0.00027000
      var2 = 0.9*0.00027000 + 0.1*(-0.02)^2 = 0.00028300

    daily_vol = sqrt(0.00028300...)
    """
    rets = [0.02, -0.01, 0.03, 0.01, -0.02]
    closes = _closes_from_returns(100.0, rets)

    seed = rets[:3]
    seed_mean = sum(seed) / 3
    seed_var = sum((r - seed_mean) ** 2 for r in seed) / 3
    var = seed_var
    for r in rets[3:]:
        var = 0.9 * var + 0.1 * r * r
    expected = math.sqrt(var)

    daily_vol, n, status = vf.ewma_vol_daily(closes, lam=0.9, min_periods=3)
    assert status == vf.STATUS_OK
    assert n == 5
    assert daily_vol == pytest.approx(expected)


def test_ewma_insufficient_history():
    closes = _closes_from_returns(100.0, [0.01, -0.01, 0.02])
    daily_vol, n, status = vf.ewma_vol_daily(closes, lam=0.94, min_periods=30)
    assert status == vf.STATUS_INSUFFICIENT_HISTORY
    assert daily_vol is None


def test_ewma_causal_recurrence_is_prefix_stable():
    """The defining property of a causal recurrence: appending more
    trailing data changes only the later steps, never revises earlier
    ones. Concretely, running ewma_vol_daily on a truncated prefix and then
    continuing the same recurrence by hand over the remaining returns must
    reproduce the full-series result -- proves var_t never depends on
    r_{t+1} or later."""
    rets = [0.015, -0.008, 0.022, -0.011, 0.006, 0.031, -0.019, 0.004]
    closes = _closes_from_returns(50.0, rets)
    lam, min_periods = 0.9, 4

    full_vol, _, full_status = vf.ewma_vol_daily(closes, lam, min_periods)
    assert full_status == vf.STATUS_OK

    # Recompute by hand: seed on rets[:4], then recurse over rets[4:] --
    # must match ewma_vol_daily's own internal computation exactly.
    seed = rets[:min_periods]
    seed_mean = sum(seed) / len(seed)
    var = sum((r - seed_mean) ** 2 for r in seed) / len(seed)
    for r in rets[min_periods:]:
        var = lam * var + (1 - lam) * r * r
    assert full_vol == pytest.approx(math.sqrt(var))


# ─────────────────────────────────────────────────────────────────────────
# _finalize_forecast -- unit derivation (the module's central contract)
# ─────────────────────────────────────────────────────────────────────────

def test_unit_derivation_annualized_and_horizon_vol():
    """daily_vol=0.02: annualized = 0.02*sqrt(252), horizon_vol(5d) =
    0.02*sqrt(5) -- distinct numbers, must never be confused for each
    other (this is the exact bug class the reconciliation doc's unit rule
    exists to prevent)."""
    forecast = vf._finalize_forecast(
        symbol="TEST", timeframe="1d", as_of=date(2026, 1, 10), horizon="5d",
        estimator="realized_vol", calculation_version=1, input_cutoff=date(2026, 1, 10),
        price_at_as_of=100.0, daily_vol=0.02, observation_count=20, status=vf.STATUS_OK,
        history_daily_vols=[0.02], sessions_per_year=252, percentile_lookback=100,
        fit_metadata={},
    )
    assert forecast.annualized_vol == pytest.approx(0.02 * math.sqrt(252))
    assert forecast.horizon_vol == pytest.approx(0.02 * math.sqrt(5))
    assert forecast.annualized_vol != pytest.approx(forecast.horizon_vol)
    assert forecast.expected_move_dollars == pytest.approx(100.0 * 0.02 * math.sqrt(5))


def test_invalid_status_produces_no_derived_units():
    """A non-ok status must not leave stale/misleading derived numbers
    lying around -- annualized_vol etc. are None, not computed from a
    None/garbage daily_vol."""
    forecast = vf._finalize_forecast(
        symbol="TEST", timeframe="1d", as_of=date(2026, 1, 10), horizon="1d",
        estimator="realized_vol", calculation_version=1, input_cutoff=None,
        price_at_as_of=None, daily_vol=None, observation_count=0,
        status=vf.STATUS_INSUFFICIENT_HISTORY, history_daily_vols=[],
        sessions_per_year=252, percentile_lookback=100, fit_metadata={},
    )
    assert forecast.annualized_vol is None
    assert forecast.horizon_vol is None
    assert forecast.expected_move_dollars is None
    assert forecast.percentile is None
    assert forecast.regime is None


def test_percentile_and_regime_bucketing():
    """Latest daily_vol is the max of its own trailing history -> 100th
    percentile -> expansion. Mirrors market_structure._volatility_context's
    thresholds exactly (compression <25, expansion >75)."""
    history = [0.01, 0.011, 0.012, 0.013, 0.05]  # latest (0.05) is the max
    forecast = vf._finalize_forecast(
        symbol="TEST", timeframe="1d", as_of=date(2026, 1, 10), horizon="1d",
        estimator="realized_vol", calculation_version=1, input_cutoff=date(2026, 1, 10),
        price_at_as_of=100.0, daily_vol=0.05, observation_count=20, status=vf.STATUS_OK,
        history_daily_vols=history, sessions_per_year=252, percentile_lookback=100,
        fit_metadata={},
    )
    assert forecast.percentile == 100.0
    assert forecast.regime == "expansion"


def test_unknown_horizon_raises():
    try:
        vf._finalize_forecast(
            symbol="TEST", timeframe="1d", as_of=date(2026, 1, 10), horizon="not_a_horizon",
            estimator="realized_vol", calculation_version=1, input_cutoff=date(2026, 1, 10),
            price_at_as_of=100.0, daily_vol=0.02, observation_count=20, status=vf.STATUS_OK,
            history_daily_vols=[0.02], sessions_per_year=252, percentile_lookback=100,
            fit_metadata={},
        )
        assert False, "expected ValueError for an unregistered horizon"
    except ValueError:
        pass


# ─────────────────────────────────────────────────────────────────────────
# VolatilityForecast.to_json / from_json round trip
# ─────────────────────────────────────────────────────────────────────────

def test_to_json_from_json_round_trip():
    forecast = vf.VolatilityForecast(
        symbol="AAPL", timeframe="1d", as_of=date(2026, 1, 10),
        available_at=datetime(2026, 1, 10, 21, 5, tzinfo=timezone.utc),
        horizon="5d", estimator="realized_vol", calculation_version=1,
        input_cutoff=date(2026, 1, 10), daily_vol=0.018, annualized_vol=0.286,
        horizon_vol=0.040, expected_move_dollars=6.5, percentile=62.0,
        regime="normal", observation_count=20, status=vf.STATUS_OK, fit_metadata={"window": 20},
    )
    round_tripped = vf.VolatilityForecast.from_json(forecast.to_json())
    assert round_tripped == forecast


# ─────────────────────────────────────────────────────────────────────────
# compute_volatility_forecast / persistence -- DB-backed
# ─────────────────────────────────────────────────────────────────────────

def test_causal_truncation_matches_shorter_dataset(conn):
    """The roadmap's core causality guarantee: truncating price_history at
    as_of_date and asking for a forecast must give the identical answer to
    asking for as_of_date against a longer dataset that has future rows
    beyond it. Insert 80 days of prices, compute at day 50 with all 80 days
    present, then delete everything after day 50 and recompute -- must
    match exactly."""
    symbol = "TRUNCTEST"
    start = date(2026, 1, 1)
    rets = [0.001 * ((-1) ** i) * (1 + i % 5) for i in range(79)]
    closes = _closes_from_returns(100.0, rets)
    _insert_prices(conn, symbol, start, closes)

    as_of = start + timedelta(days=50)
    forecast_full = vf.compute_volatility_forecast(conn, symbol, "realized_vol", as_of_date=as_of)

    with conn.cursor() as cur:
        cur.execute("DELETE FROM price_history WHERE symbol=%s AND ts > %s", (symbol, as_of))
    conn.commit()

    forecast_truncated = vf.compute_volatility_forecast(conn, symbol, "realized_vol", as_of_date=as_of)

    assert forecast_full.daily_vol == pytest.approx(forecast_truncated.daily_vol)
    assert forecast_full.annualized_vol == pytest.approx(forecast_truncated.annualized_vol)
    assert forecast_full.status == forecast_truncated.status == vf.STATUS_OK


def test_repeat_runs_are_identical(conn):
    symbol = "DETERM"
    start = date(2026, 1, 1)
    rets = [0.002 * math.sin(i / 3.0) for i in range(60)]
    closes = _closes_from_returns(50.0, rets)
    _insert_prices(conn, symbol, start, closes)
    as_of = start + timedelta(days=55)

    f1 = vf.compute_volatility_forecast(conn, symbol, "ewma", as_of_date=as_of)
    f2 = vf.compute_volatility_forecast(conn, symbol, "ewma", as_of_date=as_of)
    assert f1.daily_vol == f2.daily_vol
    assert f1.annualized_vol == f2.annualized_vol
    assert f1.percentile == f2.percentile
    assert f1.status == f2.status


def test_stale_status_when_input_cutoff_far_behind_as_of(conn):
    symbol = "STALESYM"
    start = date(2026, 1, 1)
    closes = _closes_from_returns(100.0, [0.001] * 40)
    _insert_prices(conn, symbol, start, closes)

    as_of = start + timedelta(days=39) + timedelta(days=30)  # 30 days after the last actual bar
    forecast = vf.compute_volatility_forecast(conn, symbol, "realized_vol", as_of_date=as_of)
    assert forecast.status == vf.STATUS_STALE
    assert forecast.annualized_vol is None


def test_no_data_is_insufficient_history_not_a_crash(conn):
    forecast = vf.compute_volatility_forecast(conn, "NOSUCHSYMBOL", "realized_vol", as_of_date=date(2026, 1, 10))
    assert forecast.status == vf.STATUS_INSUFFICIENT_HISTORY
    assert forecast.daily_vol is None


def test_store_and_load_latest_round_trip(conn):
    symbol = "STORETEST"
    start = date(2026, 1, 1)
    rets = [0.001 * (i % 7 - 3) for i in range(40)]
    closes = _closes_from_returns(80.0, rets)
    _insert_prices(conn, symbol, start, closes)
    as_of = start + timedelta(days=35)

    forecast = vf.compute_volatility_forecast(conn, symbol, "realized_vol", horizon="5d", as_of_date=as_of)
    assert forecast.status == vf.STATUS_OK
    vf.store_volatility_forecast(conn, forecast)

    loaded = vf.load_latest_volatility_forecast(conn, symbol, "realized_vol", "5d")
    assert loaded is not None
    assert loaded["symbol"] == symbol
    assert loaded["estimator"] == "realized_vol"
    assert loaded["horizon"] == "5d"
    assert float(loaded["daily_vol"]) == pytest.approx(forecast.daily_vol)
    assert float(loaded["annualized_vol"]) == pytest.approx(forecast.annualized_vol)
    assert loaded["status"] == vf.STATUS_OK

    # Re-storing the same (symbol, as_of, horizon, estimator) is an update,
    # not a second row -- idempotency per store_volatility_forecast's
    # ON CONFLICT DO UPDATE.
    vf.store_volatility_forecast(conn, forecast)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM volatility_forecast_history WHERE symbol=%s", (symbol,)
        )
        count = cur.fetchone()[0]
    assert count == 1


def test_update_volatility_forecasts_batch(conn):
    symbol = "BATCHTEST"
    start = date(2026, 1, 1)
    closes = _closes_from_returns(60.0, [0.001 * (i % 4 - 1.5) for i in range(50)])
    _insert_prices(conn, symbol, start, closes)
    as_of = start + timedelta(days=45)

    vf.update_volatility_forecasts(conn, [symbol], as_of_date=as_of)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT estimator FROM volatility_forecast_history WHERE symbol=%s ORDER BY estimator", (symbol,)
        )
        estimators = [r[0] for r in cur.fetchall()]
    assert estimators == ["ewma", "realized_vol"]

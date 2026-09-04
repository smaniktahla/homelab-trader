"""
Volatility Forecast contract -- VR-1 of the Volatility Forecasting &
Risk-Targeted Position Sizing epic. See
docs/volatility-sizing-vr0-reconciliation.md §6.3 for the field-by-field
contract this implements, and its unit rule: every estimator normalizes
into daily_vol first; annualized_vol/horizon_vol/expected_move_dollars are
always DERIVED here, centrally, in _finalize_forecast(), never computed ad
hoc inside an estimator. This is the mechanism that prevents the
annualized-vs-horizon-vs-dollar-move confusion the roadmap calls out --
there is exactly one place the conversion happens.

Two initial estimators, both pure functions of a trailing daily-close
series: realized_vol (trailing sample stdev of daily log returns) and ewma
(RiskMetrics-style exponentially-weighted variance). A future estimator
(garch_1_1, VR-4; implied_vol, a downstream phase) plugs into the same
ESTIMATORS dict and _finalize_forecast() boundary without changing the
VolatilityForecast shape -- implied_vol in particular converts an
annualized IV down to daily_vol at ingestion (options give annualized
directly) rather than the reverse, per the reconciliation doc.

Not called from any live path yet -- this module ships the contract,
estimators, and persistence only, same staged-rollout discipline as
market_structure.py Phase 1 and feature_registry.py before it. Wiring a
`volatility_budget_qty` candidate into shared/risk_engine.py is VR-2's job.

Persistence follows the same "frozen dataclass spec -> versioned DB table
-> compute once, snapshot, read back at decision time" pattern as
market_structure.py/market_regime_history.py, not a new generic feature
store -- see feature_registry.py's PROVIDERS entry for this module.
"""

import logging
import math
import sys
import pathlib
from dataclasses import dataclass, asdict
from datetime import date as date_cls
from datetime import datetime, timezone
from typing import Optional

from psycopg2.extras import Json, RealDictCursor

_here = pathlib.Path(__file__).resolve().parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from regime_common import load_daily_series, asof_index  # noqa: E402
from market_structure import percentile_rank  # noqa: E402

log = logging.getLogger(__name__)

CALCULATION_VERSION = 1

# Canonical annualization convention for equities (see reconciliation doc
# §6.3) -- a future crypto adapter documents and uses its own calendar
# constant instead of silently reusing this one.
DEFAULT_SESSIONS_PER_YEAR = 252

STATUS_OK = "ok"
STATUS_INSUFFICIENT_HISTORY = "insufficient_history"
STATUS_STALE = "stale"
STATUS_ZERO_OR_NONFINITE = "zero_or_nonfinite_input"
STATUS_ESTIMATOR_FAILED = "estimator_failed"

VALID_STATUSES = {
    STATUS_OK, STATUS_INSUFFICIENT_HISTORY, STATUS_STALE,
    STATUS_ZERO_OR_NONFINITE, STATUS_ESTIMATOR_FAILED,
}

# Forecast target windows this module knows how to scale daily_vol into.
# Adding a new horizon is a one-line addition here, not a schema change --
# horizon is stored as free text on the forecast row.
HORIZON_SESSIONS = {
    "1d": 1,
    "5d": 5,
    "10d": 10,
    "21d": 21,
}

# Which persisted forecast product shared/risk_engine.py's VR-2
# volatility_budget candidate reads by default -- an execution-layer
# choice (not a signal_params row, since signal_params values are NUMERIC-
# only and this is a pair of strings) kept here so it's defined once, not
# duplicated across shared/signals.py and api/main.py's call sites.
SIZING_ESTIMATOR = "realized_vol"
SIZING_HORIZON = "1d"

DEFAULTS = {
    "volatility_realized_vol_window": 20,
    "volatility_ewma_lambda": 0.94,
    "volatility_ewma_min_periods": 30,
    "volatility_percentile_lookback": 100,
    "volatility_sessions_per_year": DEFAULT_SESSIONS_PER_YEAR,
    "volatility_stale_after_days": 5,
}


def load_params(conn):
    """Same load-then-override-from-signal_params, fall-back-to-DEFAULTS-
    wholesale-on-any-error convention as shared/signals.py::load_params."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key, value FROM signal_params WHERE key = ANY(%s)",
                (list(DEFAULTS.keys()),),
            )
            rows = cur.fetchall()
        params = dict(DEFAULTS)
        for row in rows:
            k = row[0] if isinstance(row, (list, tuple)) else row["key"]
            v = row[1] if isinstance(row, (list, tuple)) else row["value"]
            if v is not None:
                params[k] = float(v)
        return params
    except Exception as e:
        log.warning(f"Could not load volatility_forecast params, using defaults: {e}")
        return dict(DEFAULTS)


@dataclass(frozen=True)
class VolatilityForecast:
    symbol: str
    timeframe: str                          # bar timeframe the estimate is computed over, e.g. "1d"
    as_of: date_cls                         # observation date the estimate is valid as-of (no lookahead past this)
    available_at: datetime                  # when this became usable by a downstream consumer
    horizon: str                            # forecast target window, e.g. "5d"
    estimator: str                          # "realized_vol" | "ewma" | "garch_1_1" (VR-4) | "implied_vol" (future)
    calculation_version: int
    input_cutoff: Optional[date_cls]        # last input observation actually used
    daily_vol: Optional[float]              # canonical unit #1: stdev of daily log returns, decimal
    annualized_vol: Optional[float]         # canonical unit #2: daily_vol * sqrt(sessions_per_year)
    horizon_vol: Optional[float]            # canonical unit #3: daily_vol * sqrt(horizon_sessions)
    expected_move_dollars: Optional[float]  # price_at_as_of * horizon_vol -- an uncertainty estimate, NOT VaR/a max loss
    percentile: Optional[float]             # trailing percentile rank among this estimator's own prior valid observations only
    regime: Optional[str]                   # compression | normal | expansion | insufficient_data
    observation_count: int
    status: str
    fit_metadata: dict

    def to_json(self):
        d = asdict(self)
        d["as_of"] = self.as_of.isoformat() if self.as_of else None
        d["available_at"] = self.available_at.isoformat() if self.available_at else None
        d["input_cutoff"] = self.input_cutoff.isoformat() if self.input_cutoff else None
        return d

    @staticmethod
    def from_json(d):
        d = dict(d)
        if isinstance(d.get("as_of"), str):
            d["as_of"] = date_cls.fromisoformat(d["as_of"])
        if isinstance(d.get("available_at"), str):
            d["available_at"] = datetime.fromisoformat(d["available_at"])
        if isinstance(d.get("input_cutoff"), str):
            d["input_cutoff"] = date_cls.fromisoformat(d["input_cutoff"])
        return VolatilityForecast(**d)


# ─────────────────────────────────────────────────────────────────────────
# Estimators -- each returns (daily_vol | None, observation_count, status).
# Never raise; an internal error is the caller's job to catch and convert
# to STATUS_ESTIMATOR_FAILED (see compute_volatility_forecast). Estimators
# only ever produce daily_vol -- annualization/horizon-scaling happens
# exclusively in _finalize_forecast, per the module docstring's unit rule.
# ─────────────────────────────────────────────────────────────────────────

def _log_returns(closes):
    """Daily log returns, oldest->newest, len(out) == len(closes)-1. A
    non-positive or missing close makes the whole window untrustworthy --
    returns None rather than skipping the bad point and silently
    shortening the window, since a skipped point would misalign an EWMA
    recurrence's implicit day-spacing assumption."""
    out = []
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev is None or cur is None or prev <= 0 or cur <= 0:
            return None
        out.append(math.log(cur / prev))
    return out


def realized_vol_daily(closes, window):
    """Trailing sample stdev (ddof=1) of daily log returns over the last
    `window` return observations (window+1 closes) -- standard convention
    for a finite trailing-window volatility estimate."""
    if len(closes) < window + 1:
        return None, len(closes), STATUS_INSUFFICIENT_HISTORY
    rets = _log_returns(closes[-(window + 1):])
    if rets is None:
        return None, 0, STATUS_ZERO_OR_NONFINITE
    n = len(rets)
    if n < 2:
        return None, n, STATUS_INSUFFICIENT_HISTORY
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    if var < 0 or not math.isfinite(var):
        return None, n, STATUS_ZERO_OR_NONFINITE
    return math.sqrt(var), n, STATUS_OK


def ewma_vol_daily(closes, lam, min_periods):
    """RiskMetrics-style EWMA variance recurrence:
    var_t = lam * var_{t-1} + (1 - lam) * r_t^2

    Seeded with the plain (demeaned) sample variance of the first
    `min_periods` returns for a stable, non-arbitrary starting point, then
    recursed causally over the remaining returns in strict chronological
    order -- each step only ever uses r_t and var_{t-1}, never a future
    observation, which is what makes this safe to call with progressively
    longer trailing windows (see test_causal_truncation)."""
    if len(closes) < min_periods + 1:
        return None, len(closes), STATUS_INSUFFICIENT_HISTORY
    rets = _log_returns(closes)
    if rets is None:
        return None, 0, STATUS_ZERO_OR_NONFINITE
    if len(rets) < min_periods:
        return None, len(rets), STATUS_INSUFFICIENT_HISTORY
    seed_window = rets[:min_periods]
    seed_mean = sum(seed_window) / len(seed_window)
    var = sum((r - seed_mean) ** 2 for r in seed_window) / len(seed_window)
    for r in rets[min_periods:]:
        var = lam * var + (1 - lam) * r * r
    if var < 0 or not math.isfinite(var):
        return None, len(rets), STATUS_ZERO_OR_NONFINITE
    return math.sqrt(var), len(rets), STATUS_OK


ESTIMATORS = {
    "realized_vol": lambda closes, params: realized_vol_daily(
        closes, int(params["volatility_realized_vol_window"])
    ),
    "ewma": lambda closes, params: ewma_vol_daily(
        closes, float(params["volatility_ewma_lambda"]), int(params["volatility_ewma_min_periods"])
    ),
}


def _rolling_daily_vol_series(closes, estimator_fn, params, percentile_lookback):
    """Trailing series of daily_vol values, one per trailing window ending
    at each of the last `percentile_lookback` closes (inclusive of the
    latest) -- needed for percentile ranking, same "need the whole
    distribution, not just latest" reasoning as
    market_structure._atr_series/_volatility_context. Bounded by
    percentile_lookback so this stays O(percentile_lookback) estimator
    calls, not O(len(closes))."""
    series = []
    n = len(closes)
    start = max(0, n - percentile_lookback)
    for i in range(start, n):
        val, _, status = estimator_fn(closes[: i + 1], params)
        if status == STATUS_OK and val is not None:
            series.append(val)
    return series


def _finalize_forecast(symbol, timeframe, as_of, horizon, estimator, calculation_version,
                        input_cutoff, price_at_as_of, daily_vol, observation_count, status,
                        history_daily_vols, sessions_per_year, percentile_lookback,
                        fit_metadata, available_at=None):
    """The one place daily_vol becomes annualized_vol/horizon_vol/
    expected_move_dollars/percentile/regime -- see module docstring's unit
    rule. Estimators never compute these themselves."""
    horizon_sessions = HORIZON_SESSIONS.get(horizon)
    if horizon_sessions is None:
        raise ValueError(f"unknown horizon {horizon!r} -- add it to HORIZON_SESSIONS")

    annualized_vol = horizon_vol = expected_move_dollars = None
    percentile = regime = None

    if status == STATUS_OK and daily_vol is not None:
        annualized_vol = daily_vol * math.sqrt(sessions_per_year)
        horizon_vol = daily_vol * math.sqrt(horizon_sessions)
        if price_at_as_of is not None:
            expected_move_dollars = price_at_as_of * horizon_vol
        trailing = history_daily_vols[-percentile_lookback:] if history_daily_vols else []
        percentile = percentile_rank(daily_vol, trailing) if trailing else None
        if percentile is None:
            regime = "insufficient_data"
        elif percentile < 25:
            regime = "compression"
        elif percentile > 75:
            regime = "expansion"
        else:
            regime = "normal"

    return VolatilityForecast(
        symbol=symbol,
        timeframe=timeframe,
        as_of=as_of,
        available_at=available_at or datetime.now(timezone.utc),
        horizon=horizon,
        estimator=estimator,
        calculation_version=calculation_version,
        input_cutoff=input_cutoff,
        daily_vol=daily_vol,
        annualized_vol=annualized_vol,
        horizon_vol=horizon_vol,
        expected_move_dollars=expected_move_dollars,
        percentile=percentile,
        regime=regime,
        observation_count=observation_count,
        status=status,
        fit_metadata=fit_metadata or {},
    )


def compute_volatility_forecast(conn, symbol, estimator, horizon="1d", as_of_date=None, timeframe="1d"):
    """I/O orchestrator: load symbol's daily closes, as-of slice (no
    lookahead past as_of_date), run the named estimator, derive canonical
    units, return a VolatilityForecast. Never raises -- any failure
    degrades to a VolatilityForecast with status=estimator_failed rather
    than propagating, same "never raises" convention as
    market_structure.compute_market_structure.

    A valid-but-old input (input_cutoff more than volatility_stale_after_days
    before as_of_date -- e.g. a delisted symbol or an ingest gap) downgrades
    an otherwise-OK estimate to status=stale rather than reporting a calm
    reading computed from data that's no longer current."""
    if estimator not in ESTIMATORS:
        raise ValueError(f"unknown estimator {estimator!r} -- must be one of {sorted(ESTIMATORS)}")

    params = load_params(conn)
    target_date = as_of_date or date_cls.today()

    try:
        dates, closes = load_daily_series(conn, symbol)
    except Exception as e:
        log.warning(f"volatility_forecast: could not load price history for {symbol}: {e}")
        dates, closes = [], []

    idx = asof_index(dates, target_date)
    closes_upto = closes[: idx + 1] if idx is not None else []
    input_cutoff = dates[idx] if idx is not None else None
    price_at_as_of = closes_upto[-1] if closes_upto else None

    estimator_fn = ESTIMATORS[estimator]
    try:
        daily_vol, observation_count, status = estimator_fn(closes_upto, params)
    except Exception as e:
        log.warning(f"volatility_forecast: {estimator} failed for {symbol} as_of={target_date}: {e}")
        daily_vol, observation_count, status = None, len(closes_upto), STATUS_ESTIMATOR_FAILED

    if status == STATUS_OK and input_cutoff is not None:
        if (target_date - input_cutoff).days > int(params["volatility_stale_after_days"]):
            status = STATUS_STALE

    history = []
    if status == STATUS_OK:
        history = _rolling_daily_vol_series(
            closes_upto, estimator_fn, params, int(params["volatility_percentile_lookback"])
        )

    return _finalize_forecast(
        symbol=symbol, timeframe=timeframe, as_of=target_date, horizon=horizon,
        estimator=estimator, calculation_version=CALCULATION_VERSION,
        input_cutoff=input_cutoff, price_at_as_of=price_at_as_of,
        daily_vol=daily_vol, observation_count=observation_count, status=status,
        history_daily_vols=history, sessions_per_year=int(params["volatility_sessions_per_year"]),
        percentile_lookback=int(params["volatility_percentile_lookback"]), fit_metadata={},
    )


def store_volatility_forecast(conn, forecast):
    """Upsert one forecast row, keyed the same way callers look it up
    (symbol, as_of, horizon, estimator) -- a re-run for the same day is an
    update, not a new row, same idempotency convention as
    market_structure.store_market_structure_day's (trading_date, symbol)
    ON CONFLICT DO UPDATE."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO volatility_forecast_history
                (symbol, timeframe, as_of, available_at, horizon, estimator,
                 calculation_version, input_cutoff, daily_vol, annualized_vol,
                 horizon_vol, expected_move_dollars, percentile, regime,
                 observation_count, status, fit_metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, as_of, horizon, estimator) DO UPDATE SET
                timeframe=EXCLUDED.timeframe,
                available_at=EXCLUDED.available_at,
                calculation_version=EXCLUDED.calculation_version,
                input_cutoff=EXCLUDED.input_cutoff,
                daily_vol=EXCLUDED.daily_vol,
                annualized_vol=EXCLUDED.annualized_vol,
                horizon_vol=EXCLUDED.horizon_vol,
                expected_move_dollars=EXCLUDED.expected_move_dollars,
                percentile=EXCLUDED.percentile,
                regime=EXCLUDED.regime,
                observation_count=EXCLUDED.observation_count,
                status=EXCLUDED.status,
                fit_metadata=EXCLUDED.fit_metadata
        """, (
            forecast.symbol, forecast.timeframe, forecast.as_of, forecast.available_at,
            forecast.horizon, forecast.estimator, forecast.calculation_version,
            forecast.input_cutoff, forecast.daily_vol, forecast.annualized_vol,
            forecast.horizon_vol, forecast.expected_move_dollars, forecast.percentile,
            forecast.regime, forecast.observation_count, forecast.status,
            Json(forecast.fit_metadata),
        ))
    conn.commit()


def load_latest_volatility_forecast(conn, symbol, estimator, horizon):
    """Latest persisted forecast row for (symbol, estimator, horizon), or
    None. This is the read-back path a future risk-engine consumer (VR-2)
    uses -- 'compute once, snapshot, read at decision time' per
    market_structure's precedent, never recomputed live at proposal time."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM volatility_forecast_history
                WHERE symbol=%s AND estimator=%s AND horizon=%s
                ORDER BY as_of DESC LIMIT 1
            """, (symbol, estimator, horizon))
            row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        log.warning(f"volatility_forecast: load_latest failed for {symbol}/{estimator}/{horizon}: {e}")
        return None


def update_volatility_forecasts(conn, symbols, estimators=("realized_vol", "ewma"), horizon="1d", as_of_date=None):
    """Batch compute+store, one call per (symbol, estimator) -- same shape
    as market_structure.update_market_structure. Not called from
    ingest.py's live cycle in this PR (see module docstring); this is the
    entry point a future cron/backfill wiring calls."""
    target_date = as_of_date or date_cls.today()
    for symbol in symbols:
        for estimator in estimators:
            try:
                forecast = compute_volatility_forecast(
                    conn, symbol, estimator, horizon=horizon, as_of_date=target_date
                )
                store_volatility_forecast(conn, forecast)
            except Exception as e:
                log.warning(f"update_volatility_forecasts: {symbol}/{estimator} failed: {e}")

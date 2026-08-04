"""
Tests for shared/market_regime_history.py and market_regime.classify_overall's
extraction.

classify_overall() is pure and gets a plain unit-test table covering every
branch (this is the cascade that used to be hand-duplicated between
market_regime.py's live path and backtest_portfolio_montecarlo.py's
historical path -- these cases are what "the two paths can't drift"
actually rests on). regime_for_date()/store_regime_day()/
find_similar_regime_days() are tested against a real Postgres connection,
same reasoning as test_rule_adherence.py: catches real cursor-style/SQL
issues pure-dict unit tests can't.
"""

import sys
import pathlib
from datetime import date

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _dir in (ROOT / "shared", ROOT / "ingest"):
    p = str(_dir)
    if p not in sys.path:
        sys.path.insert(0, p)

for _mod in ("market_regime", "market_regime_history"):
    sys.modules.pop(_mod, None)
import market_regime
import market_regime_history as mrh


# ─────────────────────────────────────────────────────────────────────────
# classify_overall — pure, every branch
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spy_trend,qqq_trend,vix_regime,expected_overall", [
    ("bull", "bull", "calm", "bull_calm"),
    ("bull", "neutral", "calm", "bull_calm"),
    ("bull", "bull", "elevated", "bull_volatile"),
    ("neutral", "neutral", "calm", "neutral"),
    ("neutral", "bull", "elevated", "neutral"),
    ("bear", "bear", "calm", "bear_calm"),
    ("bear", "neutral", "calm", "bear_calm"),
    ("bear", "bear", "elevated", "bear_volatile"),
    ("bear", "bear", "fear", "bear_fear"),
    ("neutral", "bull", "fear", "bear_fear"),
    ("bull", "bear", "fear", "bear_fear"),  # split trend -> market=neutral, neutral+fear -> bear_fear
    ("bull", "bear", "calm", "neutral"),    # split trend -> market=neutral, calm/elevated -> neutral
    ("unknown", "unknown", "unknown", "unknown"),
])
def test_classify_overall_branches(spy_trend, qqq_trend, vix_regime, expected_overall):
    overall, score_modifier, alloc_modifier, rationale = market_regime.classify_overall(
        spy_trend, qqq_trend, vix_regime
    )
    assert overall == expected_overall
    assert isinstance(score_modifier, int)
    assert isinstance(alloc_modifier, float)
    assert rationale


def test_classify_overall_matches_live_cascade_constants():
    """compute_market_regime() no longer has its own inline cascade -- this
    just confirms the extraction didn't change any live-facing numbers for
    the one bucket every fresh deployment starts in."""
    overall, score_modifier, alloc_modifier, _ = market_regime.classify_overall("bull", "bull", "calm")
    assert (overall, score_modifier, alloc_modifier) == ("bull_calm", 0, 1.0)


# ─────────────────────────────────────────────────────────────────────────
# asof_index
# ─────────────────────────────────────────────────────────────────────────

def test_asof_index_exact_match():
    dates = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]
    assert mrh.asof_index(dates, date(2026, 1, 5)) == 1


def test_asof_index_between_dates_uses_prior():
    dates = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]
    assert mrh.asof_index(dates, date(2026, 1, 4)) == 0  # Sat before the 1/5 bar


def test_asof_index_before_first_date_is_none():
    dates = [date(2026, 1, 2), date(2026, 1, 5)]
    assert mrh.asof_index(dates, date(2025, 12, 1)) is None


# ─────────────────────────────────────────────────────────────────────────
# regime_for_date — no-lookahead
# ─────────────────────────────────────────────────────────────────────────

def _flat_series(start_date, n, level):
    dates = [date(2020, 1, 1)]  # placeholder, overwritten below
    from datetime import timedelta
    dates = [start_date + timedelta(days=i) for i in range(n)]
    closes = [level] * n
    return dates, closes


def test_regime_for_date_ignores_prices_after_target():
    """A future price spike must not affect a historical classification --
    this is the entire point of an as-of walk instead of just reading the
    latest row."""
    spy_dates, spy_closes = _flat_series(date(2024, 1, 1), 300, 400.0)
    qqq_dates, qqq_closes = _flat_series(date(2024, 1, 1), 300, 400.0)
    vix_dates, vix_closes = _flat_series(date(2024, 1, 1), 300, 12.0)

    target = spy_dates[200]

    # Spike every price AFTER target far above SMA — if regime_for_date
    # leaked lookahead, this would flip spy_trend from neutral/bull(flat)
    # to something dominated by the spike.
    for i in range(201, 300):
        spy_closes[i] = 4000.0
        qqq_closes[i] = 4000.0
        vix_closes[i] = 90.0

    ctx = mrh.regime_for_date(spy_dates, spy_closes, qqq_dates, qqq_closes,
                               vix_dates, vix_closes, target)
    assert ctx["vix"] == 12.0
    assert ctx["vix_regime"] == "calm"


def test_regime_for_date_before_enough_history_is_unknown():
    """Fewer than SMA_FAST days of history -> both trends 'unknown', which
    classify_overall's combine step falls through to market='neutral' for
    (same as a real split-trend day) -- vix itself is still classifiable
    from a single reading, so this lands on the 'neutral' bucket, not
    'unknown' (that bucket is reserved for when even vix can't be read)."""
    spy_dates, spy_closes = _flat_series(date(2024, 1, 1), 10, 400.0)
    qqq_dates, qqq_closes = _flat_series(date(2024, 1, 1), 10, 400.0)
    vix_dates, vix_closes = _flat_series(date(2024, 1, 1), 10, 12.0)

    ctx = mrh.regime_for_date(spy_dates, spy_closes, qqq_dates, qqq_closes,
                               vix_dates, vix_closes, spy_dates[5])
    assert ctx["spy_trend"] == "unknown"
    assert ctx["qqq_trend"] == "unknown"
    assert ctx["overall"] == "neutral"


# ─────────────────────────────────────────────────────────────────────────
# store_regime_day / find_similar_regime_days — real DB round trip
# ─────────────────────────────────────────────────────────────────────────

_CTX = {
    "spy_trend": "bull", "qqq_trend": "bull", "vix": 12.5,
    "vix_regime": "calm", "overall": "bull_calm",
    "score_modifier": 0, "alloc_modifier": 1.0,
}


def test_store_and_query_round_trip(conn):
    mrh.store_regime_day(conn, date(2026, 1, 2), _CTX)

    rows = mrh.find_similar_regime_days(conn, "bull_calm")
    assert len(rows) == 1
    assert rows[0]["trading_date"] == date(2026, 1, 2)
    assert rows[0]["vix"] == pytest.approx(12.5)


def test_store_regime_day_upserts_same_date(conn):
    mrh.store_regime_day(conn, date(2026, 1, 2), _CTX)

    bear_ctx = dict(_CTX, overall="bear_fear", vix_regime="fear", vix=40.0)
    mrh.store_regime_day(conn, date(2026, 1, 2), bear_ctx)

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM market_regime_history")
        assert cur.fetchone()[0] == 1

    assert mrh.find_similar_regime_days(conn, "bull_calm") == []
    rows = mrh.find_similar_regime_days(conn, "bear_fear")
    assert len(rows) == 1


def test_find_similar_regime_days_filters_by_date_range_and_overall(conn):
    mrh.store_regime_day(conn, date(2026, 1, 2), _CTX)
    mrh.store_regime_day(conn, date(2026, 2, 2), _CTX)
    mrh.store_regime_day(conn, date(2026, 3, 2), dict(_CTX, overall="bear_calm"))

    rows = mrh.find_similar_regime_days(conn, "bull_calm", start_date=date(2026, 1, 15))
    assert [r["trading_date"] for r in rows] == [date(2026, 2, 2)]

    rows = mrh.find_similar_regime_days(conn, "bull_calm", end_date=date(2026, 1, 15))
    assert [r["trading_date"] for r in rows] == [date(2026, 1, 2)]

    assert mrh.find_similar_regime_days(conn, "bear_calm") != []


def test_record_today_uses_current_date(conn, monkeypatch):
    import datetime as dt_module

    class _FixedDate(dt_module.date):
        @classmethod
        def today(cls):
            return dt_module.date(2026, 5, 1)

    monkeypatch.setattr(mrh, "date_cls", _FixedDate)
    mrh.record_today(conn, _CTX)

    rows = mrh.find_similar_regime_days(conn, "bull_calm")
    assert rows[0]["trading_date"] == date(2026, 5, 1)

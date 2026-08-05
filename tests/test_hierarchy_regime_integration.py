"""
Integration coverage for the hierarchical regime PR's wiring across
shared/hierarchy_regime.py and shared/signals.py::compute_signals:

 - snapshot_hierarchy_for_symbol produces the documented JSON shape.
 - update_hierarchy_regime actually persists sector/security regime rows
   from plain price_history data (no network).
 - compute_signals leaves base_strategy_score == final_proposal_score
   (i.e. today's behavior, byte for byte) when regime_scoring_enabled is
   left at its default (0) -- the core "preserve existing behavior"
   requirement.
 - compute_signals shifts final_proposal_score away from base_strategy_score
   when regime_scoring_enabled=1 and a real adjustment applies.
 - Proposal generation still succeeds (fail-open) when no sector/security
   regime data exists at all.
"""

import sys
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest
import requests_mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _dir in (ROOT / "shared", ROOT / "ingest"):
    p = str(_dir)
    if p not in sys.path:
        sys.path.insert(0, p)

for _mod in ("hierarchy_regime", "sector_regime", "security_regime", "regime_scoring",
             "regime_common", "sector_mapping", "signals"):
    sys.modules.pop(_mod, None)
import hierarchy_regime as hr
import signals


# ─────────────────────────────────────────────────────────────────────────
# snapshot_hierarchy_for_symbol — JSON shape
# ─────────────────────────────────────────────────────────────────────────

def test_snapshot_shape_with_full_data(conn):
    from sector_regime import store_sector_regime_day
    from security_regime import store_security_regime_day

    store_sector_regime_day(conn, date(2026, 6, 1), "Technology", {
        "symbol": "XLK", "benchmark_symbol": "SPY", "classification": "bullish",
        "total_score": 4, "absolute_trend_score": 3, "relative_strength_score": 1,
        "breadth_score": None, "confidence": 0.8, "component_values": {},
    })
    store_security_regime_day(conn, date(2026, 6, 1), "AAPL", {
        "symbol": "AAPL", "sector": "Technology", "benchmark_symbol": "SPY",
        "classification": "bullish", "total_score": 5, "absolute_trend_score": 3,
        "vs_sector_score": 1, "vs_market_score": 1, "confidence": 0.75, "component_values": {},
    })

    snap = hr.snapshot_hierarchy_for_symbol(conn, "AAPL", "Technology", "bull_calm")

    assert set(snap.keys()) == {"market_regime", "sector_regime", "stock_regime", "hierarchy_alignment"}
    assert snap["market_regime"]["classification"] == "bull_calm"
    assert snap["sector_regime"]["sector"] == "Technology"
    assert snap["sector_regime"]["classification"] == "bullish"
    assert snap["stock_regime"]["symbol"] == "AAPL"
    assert snap["stock_regime"]["vs_sector_classification"] == "outperforming_sector"
    assert snap["hierarchy_alignment"] == "market_sector_stock_bullish"


def test_snapshot_shape_degrades_gracefully_with_no_data(conn):
    """No sector/security regime rows persisted at all -- must return an
    explicit unknown/insufficient_data shape, never raise."""
    snap = hr.snapshot_hierarchy_for_symbol(conn, "ZZZZ", None, "unknown")
    assert snap["sector_regime"]["classification"] == "insufficient_data"
    assert snap["stock_regime"]["classification"] == "insufficient_data"
    assert snap["stock_regime"]["vs_sector_classification"] == "unknown"
    assert snap["hierarchy_alignment"]  # never empty/None


# ─────────────────────────────────────────────────────────────────────────
# update_hierarchy_regime — persists from plain price_history, no network
# ─────────────────────────────────────────────────────────────────────────

def _seed_universe_sector(conn, symbol, sector):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO universe (symbol, sector) VALUES (%s, %s)
            ON CONFLICT (symbol) DO UPDATE SET sector = EXCLUDED.sector
        """, (symbol, sector))
    conn.commit()


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


def _rising_series(n, start, daily_pct):
    closes = [start]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + daily_pct))
    return closes


def test_update_hierarchy_regime_persists_rows(conn):
    _seed_universe_sector(conn, "AAPL", "Technology")
    _insert_prices(conn, "AAPL", date(2024, 1, 1), _rising_series(300, 150.0, 0.001))
    _insert_prices(conn, "XLK", date(2024, 1, 1), _rising_series(300, 100.0, 0.0008))
    _insert_prices(conn, "SPY", date(2024, 1, 1), _rising_series(300, 400.0, 0.0002))

    hr.update_hierarchy_regime(conn, ["AAPL"])

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sector_regime_history WHERE sector='Technology'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT COUNT(*) FROM security_regime_history WHERE symbol='AAPL'")
        assert cur.fetchone()[0] == 1


def test_update_hierarchy_regime_one_bad_symbol_does_not_block_others(conn):
    """A symbol with no price history at all must not stop other symbols
    from being processed."""
    _seed_universe_sector(conn, "AAPL", "Technology")
    _insert_prices(conn, "AAPL", date(2024, 1, 1), _rising_series(300, 150.0, 0.001))
    _insert_prices(conn, "XLK", date(2024, 1, 1), _rising_series(300, 100.0, 0.0008))
    _insert_prices(conn, "SPY", date(2024, 1, 1), _rising_series(300, 400.0, 0.0002))

    hr.update_hierarchy_regime(conn, ["AAPL", "NODATA"])

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM security_regime_history WHERE symbol='AAPL'")
        assert cur.fetchone()[0] == 1


# ─────────────────────────────────────────────────────────────────────────
# compute_signals end-to-end — score separation + fail-open
# ─────────────────────────────────────────────────────────────────────────

def _make_yahoo_chart_json(closes):
    n = len(closes)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamps = [int((start + timedelta(days=i)).timestamp()) for i in range(n)]
    return {
        "chart": {
            "result": [{
                "timestamp": timestamps,
                "indicators": {
                    "quote": [{
                        "open": closes, "high": [c * 1.01 for c in closes],
                        "low": [c * 0.99 for c in closes], "close": closes,
                        "volume": [1_000_000] * n,
                    }],
                    "adjclose": [{"adjclose": closes}],
                },
            }]
        }
    }


def _bullish_buy_closes():
    # Same shape/fixture test_fixture_equivalence.py uses: flat-ish then a
    # sharp drop into deeply-oversold RSI / below lower BB. Only clears a
    # *lowered* score_proposal_min (see _set_low_threshold_gate_params),
    # same as that file's proposal-and-sizing scenario -- this fixture was
    # never tuned to clear the real default of 65.
    closes = [180 + 0.05 * ((-1) ** i) for i in range(45)]
    closes += [closes[-1] * f for f in (0.95, 0.90, 0.85, 0.80, 0.76)]
    return closes


def _seed_signal_fixture(conn, symbol, closes):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO watchlist (symbol, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (symbol, symbol))
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        for i, close in enumerate(closes[-30:]):
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES (%s, %s, %s, %s, %s, %s, 1000000)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, (symbol, base + timedelta(days=i), close, close * 1.01, close * 0.99, close))
        for i in range(30):
            spy_close = 450 + (i % 3) * 0.5
            cur.execute("""
                INSERT INTO price_history (symbol, ts, open, high, low, close, volume)
                VALUES ('SPY', %s, %s, %s, %s, %s, 5000000)
                ON CONFLICT (symbol, ts) DO NOTHING
            """, (base + timedelta(days=i), spy_close, spy_close * 1.01, spy_close * 0.99, spy_close))
    conn.commit()


def _set_signal_param(conn, key, value):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO signal_params (key, value, description) VALUES (%s, %s, '')
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, value))
    conn.commit()


def _reset_dynamic_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""TRUNCATE symbol_features, signal_outcomes, signals, trade_proposals,
                        trades, portfolio_snapshots, price_history, watchlist,
                        sector_regime_history, security_regime_history
                        RESTART IDENTITY CASCADE""")
    conn.commit()


def _set_low_threshold_gate_params(conn):
    """Same explicit values test_fixture_equivalence.py's proposal-and-
    sizing scenario uses -- lowers score_proposal_min so the fixture's
    deterministic BUY signal clears the gate cascade, and pins every other
    gate to a known value rather than relying on whatever schema.sql's
    defaults happen to be."""
    _set_signal_param(conn, "score_proposal_min", 10)
    _set_signal_param(conn, "max_open_positions", 5)
    _set_signal_param(conn, "trade_allocation_pct", 0.05)
    _set_signal_param(conn, "max_position_pct", 0.20)
    _set_signal_param(conn, "buy_cooldown_days", 2)
    _set_signal_param(conn, "earnings_blackout_days", 3)
    _set_signal_param(conn, "circuit_breaker_drawdown_pct", 0.15)


def _run_compute_signals(conn, alpaca_base, closes, symbol="AAPL"):
    with requests_mock.Mocker() as m:
        m.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
              json=_make_yahoo_chart_json(closes))
        m.get(f"{alpaca_base}/v2/account", json={"cash": "10000", "portfolio_value": "10000"})
        m.get(f"{alpaca_base}/v2/positions", json=[])
        signals.compute_signals(conn, [symbol])


@pytest.fixture
def alpaca_base(monkeypatch):
    base = "https://fake-alpaca.test"
    monkeypatch.setenv("ALPACA_BASE_URL", base)
    signals.ALPACA_BASE = base
    signals.ALPACA_HEADERS = {"APCA-API-KEY-ID": "", "APCA-API-SECRET-KEY": ""}
    return base


def test_final_score_equals_base_when_regime_scoring_disabled(conn, alpaca_base):
    closes = _bullish_buy_closes()
    _reset_dynamic_tables(conn)
    _set_low_threshold_gate_params(conn)
    _set_signal_param(conn, "regime_scoring_enabled", 0)
    _seed_signal_fixture(conn, "AAPL", closes)

    _run_compute_signals(conn, alpaca_base, closes)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT signal_score, base_strategy_score, final_proposal_score, total_regime_adjustment,
                   regime_snapshot, hierarchy_alignment
            FROM trade_proposals WHERE symbol='AAPL' AND side='buy'
        """)
        row = cur.fetchone()
    assert row is not None, "expected a buy proposal from the deeply-oversold fixture"
    signal_score, base_score, final_score, total_adj, regime_snapshot, alignment = row
    assert base_score == signal_score
    assert final_score == base_score
    assert total_adj == 0
    assert regime_snapshot is not None
    assert alignment is not None


def test_final_score_diverges_when_regime_scoring_enabled(conn, alpaca_base):
    """With regime_scoring on and a stored bullish sector regime + market
    bull_calm, the market x sector adjustment should shift the final score
    away from the base score without changing the base score itself."""
    from sector_regime import store_sector_regime_day
    from market_regime import save_market_context

    closes = _bullish_buy_closes()
    _reset_dynamic_tables(conn)
    _set_low_threshold_gate_params(conn)
    _set_signal_param(conn, "regime_scoring_enabled", 1)
    _seed_signal_fixture(conn, "AAPL", closes)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO universe (symbol, sector) VALUES ('AAPL', 'Technology')
            ON CONFLICT (symbol) DO UPDATE SET sector = EXCLUDED.sector
        """)
    conn.commit()

    save_market_context(conn, {
        "spy_trend": "bull", "qqq_trend": "bull", "spy_sma50": 1, "spy_sma200": 1,
        "spy_vs_sma200_pct": 1, "qqq_sma50": 1, "qqq_sma200": 1, "qqq_vs_sma200_pct": 1,
        "vix": 12.0, "vix_regime": "calm", "overall": "bull_calm",
        "score_modifier": 0, "alloc_modifier": 1.0, "rationale": "test",
    })
    store_sector_regime_day(conn, date.today(), "Technology", {
        "symbol": "XLK", "benchmark_symbol": "SPY", "classification": "bullish",
        "total_score": 4, "absolute_trend_score": 3, "relative_strength_score": 1,
        "breadth_score": None, "confidence": 0.8, "component_values": {},
    })

    _run_compute_signals(conn, alpaca_base, closes)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT base_strategy_score, final_proposal_score, sector_regime_adjustment, total_regime_adjustment
            FROM trade_proposals WHERE symbol='AAPL' AND side='buy'
        """)
        row = cur.fetchone()
    assert row is not None
    base_score, final_score, sector_adj, total_adj = row
    assert sector_adj == 15  # regime_mkt_bull_sector_bull default
    assert total_adj == 15
    assert final_score == min(100, base_score + 15)
    assert final_score != base_score


def test_proposal_generation_survives_missing_regime_data(conn, alpaca_base):
    """No hierarchy regime data has been computed at all (update_hierarchy_regime
    never ran this cycle) -- compute_signals must still succeed and produce
    a proposal with an explicit unknown/insufficient_data snapshot."""
    closes = _bullish_buy_closes()
    _reset_dynamic_tables(conn)
    _set_low_threshold_gate_params(conn)
    _seed_signal_fixture(conn, "AAPL", closes)

    _run_compute_signals(conn, alpaca_base, closes)

    with conn.cursor() as cur:
        cur.execute("SELECT regime_snapshot FROM trade_proposals WHERE symbol='AAPL' AND side='buy'")
        row = cur.fetchone()
    assert row is not None
    snapshot = row[0]
    assert snapshot["sector_regime"]["classification"] in ("unknown", "insufficient_data")
    assert snapshot["stock_regime"]["classification"] == "insufficient_data"

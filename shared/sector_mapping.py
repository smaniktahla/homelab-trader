"""
GICS sector -> sector ETF mapping used by the sector-regime layer.

Plain dict + lookup function so this is trivially replaceable/extensible
(swap the dict, or monkeypatch get_sector_etf in tests) without touching
the classification code that consumes it.

Keys must match universe.sector's actual stored values exactly (confirmed
live 2026-08-05: `SELECT DISTINCT sector FROM universe` returns
"Information Technology", not "Technology") -- the original "Technology"
key silently made get_sector_etf() return None for every Info Tech stock
(a large chunk of the S&P 500, including AAPL/MSFT/NVDA), which
sector_regime.py/hierarchy_regime.py's "no ETF mapping -> unknown,
never blocks" fail-open design meant this degraded silently rather than
erroring -- caught via Experiment 007 (backtest_hierarchy_regime_significance.py)
excluding every Info Tech episode from its regime-alignment test.
"""

SECTOR_ETF_MAP = {
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Information Technology": "XLK",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
}


def get_sector_etf(sector):
    """sector -> ETF symbol, or None for unmapped/unknown/missing sectors
    (ETFs themselves, stale metadata, sectors outside the GICS-11 set)."""
    if not sector:
        return None
    return SECTOR_ETF_MAP.get(sector)

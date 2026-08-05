"""
GICS sector -> sector ETF mapping used by the sector-regime layer.

Plain dict + lookup function so this is trivially replaceable/extensible
(swap the dict, or monkeypatch get_sector_etf in tests) without touching
the classification code that consumes it.
"""

SECTOR_ETF_MAP = {
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Technology": "XLK",
    "Utilities": "XLU",
}


def get_sector_etf(sector):
    """sector -> ETF symbol, or None for unmapped/unknown/missing sectors
    (ETFs themselves, stale metadata, sectors outside the GICS-11 set)."""
    if not sector:
        return None
    return SECTOR_ETF_MAP.get(sector)

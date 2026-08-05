"""
Tests for shared/sector_mapping.py -- no prior coverage existed at all,
which is how the "Technology" vs "Information Technology" key mismatch
(fixed alongside this test, see the module's own docstring) went
undetected: nothing ever checked get_sector_etf() against a real
universe.sector value.
"""

import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
p = str(ROOT / "shared")
if p not in sys.path:
    sys.path.insert(0, p)

sys.modules.pop("sector_mapping", None)
import sector_mapping as sm


def test_get_sector_etf_information_technology():
    """Regression test: universe.sector stores "Information Technology"
    (confirmed live), not "Technology" -- the original key silently
    dropped every Info Tech stock from sector-regime classification."""
    assert sm.get_sector_etf("Information Technology") == "XLK"


def test_get_sector_etf_none_for_unmapped_sector():
    assert sm.get_sector_etf("Made Up Sector") is None


def test_get_sector_etf_none_for_falsy_input():
    assert sm.get_sector_etf(None) is None
    assert sm.get_sector_etf("") is None


def test_every_gics_11_sector_is_mapped():
    """Every distinct sector value universe.sector actually holds (live
    scanner.py output, confirmed 2026-08-05) must resolve to a real ETF --
    this is what the key-mismatch bug broke silently for Information
    Technology."""
    gics_11 = [
        "Communication Services", "Consumer Discretionary", "Consumer Staples",
        "Energy", "Financials", "Health Care", "Industrials",
        "Information Technology", "Materials", "Real Estate", "Utilities",
    ]
    for sector in gics_11:
        assert sm.get_sector_etf(sector) is not None, f"{sector} has no ETF mapping"

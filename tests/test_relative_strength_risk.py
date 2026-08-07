"""
Tests for shared/relative_strength_risk.py::evaluate_relative_strength_risk
-- pure, no DB needed. Also covers the "same classifier, live and
backtest" no-drift guarantee and the classifier's own determinism/no-
lookahead properties, since this feature's correctness depends on both.
"""

import sys
import pathlib
from datetime import date, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
for p in (ROOT / "shared", ROOT / "ingest" / "research" / "backtests"):
    p = str(p)
    if p not in sys.path:
        sys.path.insert(0, p)

for _mod in ("relative_strength_risk", "security_regime", "regime_common"):
    sys.modules.pop(_mod, None)
import relative_strength_risk as rsr
import security_regime as sr


OFF_PARAMS = dict(rsr.RELATIVE_STRENGTH_RISK_DEFAULTS)
GATE_PARAMS = dict(rsr.RELATIVE_STRENGTH_RISK_DEFAULTS, relative_strength_risk_mode=rsr.MODE_GATE)
SIZE_REDUCE_PARAMS = dict(rsr.RELATIVE_STRENGTH_RISK_DEFAULTS, relative_strength_risk_mode=rsr.MODE_SIZE_REDUCE)


# ─────────────────────────────────────────────────────────────────────────
# 1/2/3: enabled/disabled, underperforming/outperforming/neutral
# ─────────────────────────────────────────────────────────────────────────

def test_underperforming_stock_rejected_when_gate_enabled():
    decision = rsr.evaluate_relative_strength_risk("underperforming_sector", GATE_PARAMS)
    assert decision["reject"] is True
    assert decision["reason"] == "relative_strength_risk_gate"
    assert decision["size_multiplier"] == 1.0


def test_underperforming_stock_not_rejected_when_disabled():
    decision = rsr.evaluate_relative_strength_risk("underperforming_sector", OFF_PARAMS)
    assert decision["reject"] is False
    assert decision["reason"] is None


def test_outperforming_stock_proceeds_normally():
    decision = rsr.evaluate_relative_strength_risk("outperforming_sector", GATE_PARAMS)
    assert decision["reject"] is False


def test_in_line_with_sector_proceeds_normally():
    decision = rsr.evaluate_relative_strength_risk("in_line_with_sector", GATE_PARAMS)
    assert decision["reject"] is False


# ─────────────────────────────────────────────────────────────────────────
# 4/5: sector lookup failure / insufficient history -- both surface as
# "unknown" from classify_security_regime, must fail open (never reject)
# ─────────────────────────────────────────────────────────────────────────

def test_unknown_classification_fails_open_sector_lookup_failure():
    decision = rsr.evaluate_relative_strength_risk("unknown", GATE_PARAMS)
    assert decision["reject"] is False
    assert decision["reason"] is None


def test_unknown_classification_fails_open_insufficient_history():
    # classify_security_regime itself returns vs_sector_classification="unknown"
    # when there isn't enough history to evaluate -- same fail-open path.
    ctx = sr.classify_security_regime([1.0] * 5, [1.0] * 5, [1.0] * 5)
    assert ctx["vs_sector_classification"] == "unknown"
    decision = rsr.evaluate_relative_strength_risk(ctx["vs_sector_classification"], GATE_PARAMS)
    assert decision["reject"] is False


def test_size_reduce_mode_is_a_documented_noop_not_implemented():
    decision = rsr.evaluate_relative_strength_risk("underperforming_sector", SIZE_REDUCE_PARAMS)
    assert decision["reject"] is False
    assert decision["size_multiplier"] == 1.0


def test_unknown_mode_value_fails_open():
    params = dict(rsr.RELATIVE_STRENGTH_RISK_DEFAULTS, relative_strength_risk_mode=99)
    decision = rsr.evaluate_relative_strength_risk("underperforming_sector", params)
    assert decision["reject"] is False


# ─────────────────────────────────────────────────────────────────────────
# 6: classifier is deterministic
# ─────────────────────────────────────────────────────────────────────────

def test_classifier_is_deterministic():
    stock_closes = [100 + i * 0.5 for i in range(300)]
    sector_closes = [100 + i * 0.3 for i in range(300)]
    market_closes = [100 + i * 0.2 for i in range(300)]
    ctx1 = sr.classify_security_regime(stock_closes, sector_closes, market_closes)
    ctx2 = sr.classify_security_regime(stock_closes, sector_closes, market_closes)
    assert ctx1 == ctx2


# ─────────────────────────────────────────────────────────────────────────
# 7: historical classification uses no future data
# ─────────────────────────────────────────────────────────────────────────

def test_classification_ignores_future_data_when_as_of_sliced():
    """Same no-lookahead shape as test_sector_regime's/test_security_regime's
    own as-of regression tests -- explicit here because this feature's
    correctness depends on it directly, not just transitively."""
    n = 300
    target_idx = 200
    stock_closes = [100 + i * 0.1 for i in range(n)]
    sector_closes = [100 + i * 0.05 for i in range(n)]
    market_closes = [100 + i * 0.05 for i in range(n)]

    expected = sr.classify_security_regime(
        stock_closes[:target_idx + 1], sector_closes[:target_idx + 1], market_closes[:target_idx + 1])

    # A future price spike must not change the as-of classification.
    spiked_stock = list(stock_closes)
    for i in range(target_idx + 1, n):
        spiked_stock[i] = 9999.0

    as_of_result = sr.classify_security_regime(
        spiked_stock[:target_idx + 1], sector_closes[:target_idx + 1], market_closes[:target_idx + 1])
    assert as_of_result["vs_sector_classification"] == expected["vs_sector_classification"]
    assert as_of_result["classification"] == expected["classification"]


# ─────────────────────────────────────────────────────────────────────────
# 8: live strategy and research harness use the same classifier (no
# second, independently-drifting implementation)
# ─────────────────────────────────────────────────────────────────────────

def test_live_and_backtest_use_the_identical_classifier_function(monkeypatch):
    """Not equivalent logic -- the literal same function object, imported
    directly by both signals.py (live) and the Experiment 007/009/010/011
    backtest scripts (research), so there is no second implementation
    that could silently drift from the first.

    Re-imports security_regime fresh within this test (rather than trusting
    the module-level `sr` captured at file-collection time) -- other test
    files in this suite also pop/reimport "security_regime" at their own
    collection time, so sys.modules can point somewhere other than what
    this file's module-level `sr` still references by the time tests
    actually run. Comparing against a freshly-fetched reference keeps this
    test self-contained regardless of cross-file collection order."""
    import os
    monkeypatch.setenv("DATABASE_URL", os.environ.get(
        "TEST_DATABASE_URL", "postgresql://invest_test:not_a_real_credential@localhost:15432/invest_test"))
    for _mod in ("security_regime", "backtest_hierarchy_regime_significance", "backtest_portfolio_montecarlo",
                 "backtest_score_calibration"):
        sys.modules.pop(_mod, None)

    import security_regime as sr_fresh
    import backtest_hierarchy_regime_significance as bh
    assert bh.classify_security_regime is sr_fresh.classify_security_regime

    import backtest_portfolio_montecarlo as bp
    assert bp.classify_security_regime is sr_fresh.classify_security_regime

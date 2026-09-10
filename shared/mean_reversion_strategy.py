"""
Mean Reversion strategy adapter for shared/backtest_engine.py's Strategy
interface (Callable[[list[Bar]], str | None]) -- built for VR-3a's sizing
comparison follow-up (see ingest/research/backtests/
backtest_vr3a_sizing_comparison.py), which needs mean_reversion available
as a pure bars_seen -> signal function the same shape as
shared/bollinger_breakout_strategy.py / shared/ema_crossover_strategy.py.

This is a SIMPLIFIED proxy of the live `mean_reversion` thesis in
shared/signals.py::compute_signals(), not a full reimplementation. It
reuses signals.py's own compute_rsi/compute_bollinger/score_signal
directly (avoiding reimplementation/drift risk -- same discipline
ingest/research/backtests/backtest_score_calibration.py's Experiment 002
already established), but deliberately omits everything that needs data
beyond a single symbol's own OHLC series: regime detection,
relative-strength-vs-SPY, the ATR modifier, sector caps, and the live
risk-engine sizing gate. score_signal's regime/rs_pct/atr adjustment
multipliers are neutralized by passing regime="unknown", rs_pct=None,
atr=None -- the same "pre-multiplier raw score" isolation technique
Experiment 005 already uses (see backtest_rule_significance.py) to compare
against a threshold apples-to-apples. This is NOT a claim that it
reproduces live compute_signals() behavior exactly -- it is a reasonable
single-symbol proxy for testing sizing-policy effects on mean_reversion's
core entry logic, nothing more.

Entry: raw (pre-multiplier) buy score >= p["score_log_min"] (default 30,
the same threshold compute_signals() gates real proposals on).
Exit: thesis_complete only (price closes back at/above the middle
Bollinger band, i.e. SMA(bb_period)) -- no time_stop, no stop-loss, no
regime-deterioration exit modeled here, since those need portfolio/DB
state this pure function doesn't have access to.
"""

from signals import compute_rsi, compute_bollinger, score_signal, DEFAULTS


def make_mean_reversion_strategy(params=None):
    p = dict(DEFAULTS)
    if params:
        p.update(params)

    def strategy(bars_seen):
        closes = [b.close for b in bars_seen]
        if len(closes) < int(p["bb_period"]) + 1:
            return None
        rsi = compute_rsi(closes, p["rsi_period"])
        bb_upper, bb_middle, bb_lower, band_std = compute_bollinger(closes, p["bb_period"], p["bb_std"])
        if bb_middle is None:
            return None
        price = closes[-1]

        score, _rationale = score_signal(
            rsi, price, bb_upper, bb_lower, band_std, bb_middle,
            regime="unknown", side="buy", p=p, rs_pct=None, atr=None,
        )
        if score >= p["score_log_min"]:
            return "buy"

        if price >= bb_middle:
            return "sell"

        return None

    return strategy

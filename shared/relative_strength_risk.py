"""
Relative-strength risk eligibility filter for mean_reversion buy proposals
(shared/signals.py). Risk-control layer only, NOT a scoring/alpha
mechanism -- deliberately separate from regime_scoring.py/
structure_scoring.py's score-adjustment pattern. This module never touches
score_signal()'s output or final_score; it can only reject a buy proposal
outright (mode=gate) or, in a reserved future mode, resize it.

Backed by backtest_results experiment_ids 009 (episode-level), 010
(portfolio-level), 011 (paired significance + definition-sensitivity
robustness check): stock-vs-sector underperformance is associated with a
materially higher stop-out rate and worse max-drawdown, replicated across
an independent window resample AND an alternate relative-strength
definition (short lookback). Total return / Sharpe / Sortino / exposure-
adjusted-return improvements from gating did NOT replicate on a fresh
sample -- this module (and anything describing it, dashboards included)
must only ever be described as a risk-control filter, never as a return
or alpha enhancement. Experiment 011's short-lookback definition even
showed a significant NEGATIVE total-return effect in one run, confirming
over-aggressive gating can remove real profitable opportunity -- this is
exactly why the default is off and the only implemented mode rejects
strictly on evidence-backed criteria, not a tunable score bonus.

Modes (relative_strength_risk_mode signal_params key, default 0/off):
  0 = off        -- no-op, current behavior unchanged.
  1 = gate        -- reject the buy proposal outright when the stock is
                     classified underperforming_sector.
  2 = size_reduce -- reserved for a future PR (a softer variant than
                     outright rejection, motivated by the negative-return
                     result above), NOT implemented yet. Currently a
                     documented no-op if set, logged and never crashes.
"""

import logging

log = logging.getLogger(__name__)

MODE_OFF = 0
MODE_GATE = 1
MODE_SIZE_REDUCE = 2  # reserved, not implemented -- see module docstring

RELATIVE_STRENGTH_RISK_DEFAULTS = {
    "relative_strength_risk_mode": MODE_OFF,
    "relative_strength_risk_size_reduce_pct": 0.5,  # reserved for MODE_SIZE_REDUCE
}


def load_relative_strength_risk_params(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM signal_params")
            rows = cur.fetchall()
        params = dict(RELATIVE_STRENGTH_RISK_DEFAULTS)
        for row in rows:
            k = row[0] if isinstance(row, (list, tuple)) else row["key"]
            if k not in RELATIVE_STRENGTH_RISK_DEFAULTS:
                continue
            v = row[1] if isinstance(row, (list, tuple)) else row["value"]
            params[k] = float(v)
        return params
    except Exception as e:
        log.warning(f"Could not load relative-strength risk params, using defaults: {e}")
        return dict(RELATIVE_STRENGTH_RISK_DEFAULTS)


def evaluate_relative_strength_risk(vs_sector_classification, params):
    """Pure decision function. The caller passes in whatever
    vs_sector_classification it already has -- production reads this from
    hierarchy_regime.py's snapshot, itself backed by
    shared/security_regime.py::classify_security_regime, the exact same
    pure function Experiments 007/009/010/011 called directly against
    historical data. Same function, same decision boundary, live and
    backtest -- there is no second implementation to drift.

    Fail-safe, never guesses: an "unknown" classification (unmapped
    sector, insufficient history, or any other reason
    classify_security_regime couldn't evaluate) always passes through
    untouched -- "unable to determine sector relative strength" must never
    become a false rejection. Same fail-open treatment for any mode value
    this function doesn't yet implement (currently just MODE_SIZE_REDUCE).

    Returns {"reject": bool, "size_multiplier": float, "reason": str|None}.
    """
    mode = int(params.get("relative_strength_risk_mode", MODE_OFF))
    if mode == MODE_OFF:
        return {"reject": False, "size_multiplier": 1.0, "reason": None}
    if vs_sector_classification != "underperforming_sector":
        return {"reject": False, "size_multiplier": 1.0, "reason": None}
    if mode == MODE_GATE:
        return {"reject": True, "size_multiplier": 1.0, "reason": "relative_strength_risk_gate"}
    if mode == MODE_SIZE_REDUCE:
        log.warning("relative_strength_risk_mode=size_reduce (2) is reserved, not implemented yet -- treating as off")
        return {"reject": False, "size_multiplier": 1.0, "reason": None}
    log.warning(f"Unknown relative_strength_risk_mode={mode} -- treating as off")
    return {"reject": False, "size_multiplier": 1.0, "reason": None}

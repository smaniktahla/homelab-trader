"""
Evidence Evaluation Engine, PR 4 of the Hypothesis-Driven Trading
Architecture epic -- see docs/trade-thesis-architecture-reconciliation.md
§1a/§3. This is the milestone where trade-thesis instantiation becomes
load-bearing in production: shared/signals.py::compute_signals() calls
instantiate_buy_trade_thesis() as a side effect of a real BUY signal
crossing the proposal threshold, in the same cycle that creates the
trade_proposals row. Before this PR, trade_theses (PR 1), the Feature
Registry (PR 2), and the validator (PR 3) all existed but nothing live
called them -- same "dark until the wiring PR" staging market_structure.py
and structure_scoring.py already used.

Gated behind trade_thesis_instantiation_enabled (signal_params, default
0/off) -- same disabled-by-default precedent as structure_scoring_enabled
and relative_strength_risk_mode. With the flag off, compute_signals()'s
BUY path is byte-for-byte unchanged from pre-PR-4 behavior:
trade_proposals.trade_thesis_id stays NULL, exactly as PR 1 left it.

instantiate_buy_trade_thesis() only ever returns a row id if construction
(PR 1 grammar), semantic validation (PR 3), AND persistence all succeed --
any failure at any stage logs a warning and returns None, so a broken or
unvalidatable thesis never blocks the trade_proposals insert that would
otherwise have happened. The proposal still gets created; it just doesn't
get a trade_thesis_id, same as if the flag were off.

SELL-side instantiation is explicitly out of scope here (per §2a -- a
SELL's trade_thesis_id is a best-effort scalar hint, not something this
engine derives) and out of scope for pyramiding/re-entry onto an existing
`active` trade_theses row (per §3 -- deferred until a real re-evaluation
consumer exists to make "still active" checkable; this PR always mints a
new row for every qualifying BUY, matching the epic's own "new opportunity"
default until PR 9/10 land).

Known limitation, not fixed here: shared/feature_registry.py's BB_PERIOD/
BB_STD (20/2.0) are hardcoded, while shared/signals.py's p["bb_period"]/
p["bb_std"] are signal_params-configurable and happen to default to the
same values. If those params are ever changed away from their defaults,
this engine's technical.bb_pct_b (read fresh from the registry, not from
compute_signals()'s own live bb_upper/bb_lower for this cycle) would
silently diverge from what score_signal() actually scored against. Flagging
this rather than hiding it -- fixing it is a Feature Registry change
(PR 2 territory), not this PR's.
"""

import logging

from feature_registry import PROVIDERS, evaluate_feature
from feature_store import FEATURE_VERSION
from trade_thesis import GrammarError, TradeThesis, record_trade_thesis
from trade_thesis_validator import validate_trade_thesis

log = logging.getLogger(__name__)

TRADE_THESIS_ENGINE_DEFAULTS = {
    "trade_thesis_instantiation_enabled": 0,
}

# %B = 0.5 is the point where close == the Bollinger midline (SMA20) for a
# symmetric band -- the exact condition shared/signals.py::check_symbol_exits()
# already uses for its thesis_complete exit ("price returned to SMA20"). Reusing
# %B here instead of adding a new technical.bb_middle feature keeps this
# success_spec expressed entirely in already-registered features.
_THESIS_COMPLETE_BB_PCT_B = 0.5
# %B <= 0.1 mirrors score_signal()'s own "near/below lower band" scoring
# region -- an approximation of the real weighted scoring logic, not a
# literal re-derivation of it (score_signal() blends RSI/BB/regime/RS/ATR
# continuously; a trade thesis's entry_conditions is a falsifiable
# description of "why this looked like an entry," not a re-implementation
# of the scorer).
_OVERSOLD_BB_PCT_B = 0.1


def load_trade_thesis_engine_params(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM signal_params")
            rows = cur.fetchall()
        params = dict(TRADE_THESIS_ENGINE_DEFAULTS)
        for row in rows:
            k = row[0] if isinstance(row, (list, tuple)) else row["key"]
            if k not in TRADE_THESIS_ENGINE_DEFAULTS:
                continue
            v = row[1] if isinstance(row, (list, tuple)) else row["value"]
            params[k] = float(v)
        return params
    except Exception as e:
        log.warning(f"Could not load trade thesis engine params, using defaults: {e}")
        return dict(TRADE_THESIS_ENGINE_DEFAULTS)


def _build_buy_trade_thesis(thesis_id, symbol, as_of, rsi_oversold_threshold,
                             planned_initial_stop_price, rsi_14, bb_pct_b, close):
    entry_conditions = {
        "or": [
            {"feature": "technical.rsi_14", "op": "lt", "value": rsi_oversold_threshold},
            {"feature": "technical.bb_pct_b", "op": "lte", "value": _OVERSOLD_BB_PCT_B},
        ]
    }
    invalidation_spec = {"feature": "technical.close", "op": "lt", "value": planned_initial_stop_price}
    success_spec = {"feature": "technical.bb_pct_b", "op": "gte", "value": _THESIS_COMPLETE_BB_PCT_B}

    evidence_context = {
        "as_of": as_of.date().isoformat() if hasattr(as_of, "date") else as_of.isoformat(),
        "providers": {
            "technical": {
                "source": PROVIDERS["technical"].source_table,
                "feature_version": FEATURE_VERSION,
            },
        },
    }

    observed = {"technical.rsi_14": rsi_14, "technical.bb_pct_b": bb_pct_b, "technical.close": close}
    evidence_snapshot = {
        "supporting": [fid for fid, val in observed.items() if val is not None],
        "contradictory": [],
        "missing": [fid for fid, val in observed.items() if val is None],
    }

    provenance = {
        "entry_conditions": "explicit",
        "invalidation_spec": "explicit",
        "success_spec": "explicit",
        "evidence_context": "explicit",
    }

    hypothesis_text = (
        f"RSI/BB mean-reversion oversold entry for {symbol}: "
        f"RSI14={rsi_14}, %B={bb_pct_b}, close={close}"
    )

    return TradeThesis(
        thesis_id=thesis_id,
        symbol=symbol,
        hypothesis_type="mean_reversion_oversold",
        hypothesis_text=hypothesis_text,
        entry_conditions=entry_conditions,
        invalidation_spec=invalidation_spec,
        success_spec=success_spec,
        evidence_context=evidence_context,
        evidence_snapshot=evidence_snapshot,
        provenance=provenance,
        as_of=as_of,
    )


def instantiate_buy_trade_thesis(conn, thesis_id, symbol, as_of, rsi_oversold_threshold,
                                  planned_initial_stop_price):
    """Build, validate, and persist a trade_theses row for one BUY signal
    that already cleared every existing gate in compute_signals(). Returns
    the new row's id, or None if anything failed -- construction (grammar),
    semantic validation (against the Feature Registry), or the persistence
    write itself. Never raises past this function; a broken thesis must
    never block the trade_proposals insert the caller is about to make."""
    rsi_14 = evaluate_feature(conn, "technical.rsi_14", symbol, as_of)
    bb_pct_b = evaluate_feature(conn, "technical.bb_pct_b", symbol, as_of)
    close = evaluate_feature(conn, "technical.close", symbol, as_of)

    try:
        thesis = _build_buy_trade_thesis(
            thesis_id, symbol, as_of, rsi_oversold_threshold,
            planned_initial_stop_price, rsi_14, bb_pct_b, close,
        )
    except GrammarError as e:
        log.warning(f"trade_thesis_engine: grammar-invalid thesis for {symbol}, not instantiated: {e}")
        return None

    result = validate_trade_thesis(conn, thesis)
    if not result.is_valid:
        log.warning(f"trade_thesis_engine: semantically invalid thesis for {symbol}, not instantiated: {result.errors}")
        return None

    row_id = record_trade_thesis(conn, thesis)
    if row_id is None:
        log.warning(f"trade_thesis_engine: persistence failed for {symbol}, proposal will have no trade_thesis_id")
    return row_id

"""
Candidate backtest bridge -- Hypothesis-Driven Phase 5a-1, see
docs/hypothesis-driven-phase5a-scoping.md.

Backtests a candidate's condition trees (candidates.entry_conditions /
invalidation_spec / success_spec, PR 14) across the whole price_history
universe and appends one row per candidate to candidate_backtests. This is
the first path from a candidate to an evaluation; it deliberately stops
there -- a result never changes a candidate's or strategy version's status,
never registers anything, never touches trade_theses/proposals/orders.

Guardrails (enforced here, not by convention):
  - Sealed holdout: every query is bounded to dates before HOLDOUT_START.
    There is no parameter to move it; data from HOLDOUT_START onward is
    reserved for the Strategy Incubator's human-triggered VALIDATION /
    WALK_FORWARD stages and is never used to rank candidates.
  - Every trial counted: all results are kept (including
    insufficient_trades / unsupported_feature / error), and each row
    records trial_number = its position among this hypothesis type's
    evaluated candidates, so a best-of-N is never presented as one test.
  - Minimum evidence: fewer than MIN_TRADES trades or MIN_WEEKS distinct
    signal weeks -> status insufficient_trades.
  - Reference, not absolute: each result carries the same metrics for the
    hypothesis type's own default template, run in the same pass.

Simulation (trade level, same conventions as the live-signal replays):
  - features: shared/feature_series.py, parity-tested against
    feature_registry.evaluate_feature(); bars are split/dividend-adjusted
  - entry: entry_conditions True (Kleene: None is not True) at close t,
    fill at open t+1; one position per symbol, flat only; the live
    buy_cooldown_days gate; no entries before WARMUP bars of history or
    before market_regime_history starts
  - exit: per-family policy, identical for every candidate of a type
      live_mean_reversion: the live MR exits (shared/exit_policies.py)
      breakout: invalidation_spec, the hard stop and the time stop
    plus the candidate's own invalidation_spec / success_spec whenever
    present, closing at the next open
  - costs: ROUND_TRIP_COST per trade; trades still open at the cutoff are
    counted but excluded from metrics
Account-level effects (slots, risk budget, sector caps) are not modelled.
"""

import collections
import hashlib
import json
import logging
import os
import random
import statistics
from datetime import date, datetime, timezone

import hypothesis_library
from daily_ema_pullback_strategy import load_adjusted_bars
from exit_policies import MarketContext, TIME_STOP_BARS, WARMUP, live_cycle, simulate_exit
from feature_series import UnsupportedFeature, build_feature_series, check_supported
from hypothesis_candidates import _collect_referenced_features
from signals import load_params
from trade_thesis_invalidation import _apply_operator

log = logging.getLogger(__name__)

HOLDOUT_START = date(2025, 1, 1)
_HOLDOUT_CUTOFF_TS = datetime(2024, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
_EARLIEST_TS = datetime(1900, 1, 1, tzinfo=timezone.utc)
ROUND_TRIP_COST = 0.00125       # 5 bps slippage per side + ~$1 per fill on a typical position
MIN_TRADES = 30
MIN_WEEKS = 20
BOOTSTRAP_REPS = 1000
BOOTSTRAP_SEED = 0

EXIT_LIVE_MR = "live_mean_reversion"
EXIT_BREAKOUT = "breakout"
EXIT_FAMILIES = {
    "mean_reversion_oversold": EXIT_LIVE_MR,
    "bollinger_breakout_continuation": EXIT_BREAKOUT,
    "structural_breakout_momentum": EXIT_BREAKOUT,
    "fvg_reaction_momentum": EXIT_BREAKOUT,
}

STATUS_COMPLETE = "complete"
STATUS_INSUFFICIENT = "insufficient_trades"
STATUS_UNSUPPORTED = "unsupported_feature"
STATUS_ERROR = "error"
_COUNTED_STATUSES = (STATUS_COMPLETE, STATUS_INSUFFICIENT)

_SOURCE_FILES = ("candidate_backtest.py", "feature_series.py", "exit_policies.py")


def code_hash():
    here = os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256()
    for name in _SOURCE_FILES:
        with open(os.path.join(here, name), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:16]


# ── condition trees over series ────────────────────────────────────────────

def compile_tree(node, series):
    """fn(t) -> True/False/None over precomputed feature series, with the
    same Kleene semantics as trade_thesis_invalidation.evaluate_condition_tree."""
    if node is None:
        return None
    for comb in ("and", "or", "not"):
        if comb in node:
            break
    else:
        values = series[node["feature"]]
        op, threshold = node["op"], node["value"]

        def leaf(t):
            v = values[t]
            return None if v is None else _apply_operator(op, v, threshold)
        return leaf
    if comb == "not":
        child = compile_tree(node["not"], series)
        return lambda t: (lambda c: None if c is None else (not c))(child(t))
    children = [compile_tree(c, series) for c in node[comb]]
    if comb == "and":
        def and_(t):
            vals = [c(t) for c in children]
            if any(v is False for v in vals):
                return False
            return None if any(v is None for v in vals) else True
        return and_

    def or_(t):
        vals = [c(t) for c in children]
        if any(v is True for v in vals):
            return True
        return None if any(v is None for v in vals) else False
    return or_


def _tree_features(trees):
    out = set()
    for tree in trees:
        if tree is not None:
            out |= _collect_referenced_features(tree)
    return out


# ── data (every query bounded by the holdout) ──────────────────────────────

def _is_tradable_symbol(sym):
    return not (sym.startswith("^") or "." in sym or "=" in sym)


def load_universe(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT symbol FROM price_history WHERE ts < %s", (HOLDOUT_START,))
        return sorted(r[0] for r in cur.fetchall() if _is_tradable_symbol(r[0]))


def load_symbol_bars(conn, symbol):
    bars = load_adjusted_bars(conn, symbol, _EARLIEST_TS, _HOLDOUT_CUTOFF_TS)
    s = {"d": [b.ts.date() if isinstance(b.ts, datetime) else b.ts for b in bars],
         "o": [b.open for b in bars], "h": [b.high for b in bars],
         "l": [b.low for b in bars], "c": [b.close for b in bars]}
    if s["d"] and s["d"][-1] >= HOLDOUT_START:
        raise RuntimeError(f"holdout breach loading {symbol}: bar dated {s['d'][-1]}")
    return s


def load_market_regime(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT trading_date, overall, score_modifier FROM market_regime_history
            WHERE trading_date < %s ORDER BY trading_date
        """, (HOLDOUT_START,))
        rows = cur.fetchall()
    return [r[0] for r in rows], [(r[1], int(r[2] or 0)) for r in rows]


def load_structural_events(conn, symbol):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT confirmation_time, id, event_type FROM structural_events
            WHERE symbol=%s AND timeframe='daily' AND confirmation_time < %s
        """, (symbol, HOLDOUT_START))
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def _load_candidates(conn, candidate_ids):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.id, b.hypothesis_type, c.entry_conditions, c.invalidation_spec, c.success_spec,
                   c.parameter_values
            FROM candidates c JOIN candidate_batches b ON b.id = c.batch_id
            WHERE c.id = ANY(%s)
        """, (list(candidate_ids),))
        rows = cur.fetchall()
    return [{"id": r[0], "hypothesis_type": r[1], "entry": r[2], "invalidation": r[3], "success": r[4],
             "parameter_values": r[5]} for r in rows]


# ── simulation ─────────────────────────────────────────────────────────────

def _simulate_symbol(s, fns, family, p, ctx, discovery_start, live_cache):
    """Trades for one candidate on one symbol. fns: compiled entry/invalidation/success."""
    entry_fn, inval_fn, succ_fn = fns
    n = len(s["c"])

    def live_reason(t):
        if t not in live_cache:
            live_cache[t] = live_cycle(s, t, p, ctx)[1]
        return live_cache[t]

    def close_exit_at(t):
        reason = live_reason(t) if family == EXIT_LIVE_MR else None
        if reason == "regime_deterioration":
            return reason
        if inval_fn is not None and inval_fn(t) is True:
            return "invalidation"
        if succ_fn is not None and succ_fn(t) is True:
            return "success"
        return reason

    trades, open_at_cutoff = [], 0
    t = WARMUP - 1
    last_fill = None
    while t < n - 1:
        d = s["d"][t]
        if (d < discovery_start or entry_fn(t) is not True
                or (last_fill and (d - last_fill).days < p["buy_cooldown_days"])):
            t += 1
            continue
        e = t + 1
        fill = s["o"][e]
        last_fill = s["d"][e]
        r = simulate_exit(s, close_exit_at, lambda _t: None, e, fill, {}, p["stop_loss_pct"])
        if r is None:
            open_at_cutoff += 1
            break
        trades.append({"signal_date": d, "ret": r["exit_price"] / fill - 1 - ROUND_TRIP_COST,
                       "bars": r["x"] - e, "reason": r["reason"], "mfe": r["mfe"]})
        t = r["x"]
    return trades, open_at_cutoff


def compute_metrics(trades, open_at_cutoff=0):
    """Trade-level summary with a week-block bootstrap CI of the mean."""
    n = len(trades)
    weeks = collections.defaultdict(list)
    for tr in trades:
        weeks[tr["signal_date"].isocalendar()[:2]].append(tr["ret"])
    out = {"trades": n, "distinct_weeks": len(weeks), "open_at_cutoff": open_at_cutoff}
    if n == 0:
        return out
    rets = [tr["ret"] for tr in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    out.update({
        "mean_ret": statistics.fmean(rets),
        "median_ret": statistics.median(rets),
        "win_rate": len(wins) / n,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        "avg_bars": statistics.fmean(tr["bars"] for tr in trades),
        "exit_reasons": dict(collections.Counter(tr["reason"] for tr in trades)),
        "by_year": {str(y): {"trades": len(v), "mean_ret": statistics.fmean(v)}
                    for y, v in sorted(_group(trades, lambda tr: tr["signal_date"].year).items())},
    })
    if len(weeks) >= 2:
        rng = random.Random(BOOTSTRAP_SEED)
        keys = list(weeks)
        means = []
        for _ in range(BOOTSTRAP_REPS):
            sample = [r for k in rng.choices(keys, k=len(keys)) for r in weeks[k]]
            means.append(statistics.fmean(sample))
        means.sort()
        out["mean_ret_ci90"] = [means[int(0.05 * BOOTSTRAP_REPS)], means[int(0.95 * BOOTSTRAP_REPS) - 1]]
    return out


def _group(items, key):
    g = collections.defaultdict(list)
    for it in items:
        g[key(it)].append(it["ret"])
    return g


def _backtest_type(conn, hypothesis_type, specs, p):
    """Run every spec (candidates + the type's default template) of one
    hypothesis type in one pass over the universe. Returns
    ({spec_key: metrics}, run_config)."""
    family = EXIT_FAMILIES[hypothesis_type]
    regime_dates, regime_vals = load_market_regime(conn)
    if not regime_dates:
        raise RuntimeError("market_regime_history is empty before the holdout")
    universe = load_universe(conn)
    spy = load_symbol_bars(conn, "SPY") if "SPY" in universe else {"d": [], "c": []}
    ctx = MarketContext(regime_dates, regime_vals, spy["d"], spy["c"])
    features = _tree_features(t for spec in specs.values() for t in (spec["entry"], spec["invalidation"], spec["success"]))
    needs_events = any(f.startswith("structural_events.") for f in features)

    trades = {k: [] for k in specs}
    open_counts = {k: 0 for k in specs}
    data_end = None
    for sym in universe:
        s = load_symbol_bars(conn, sym)
        if len(s["c"]) < WARMUP + 1:
            continue
        data_end = max(data_end, s["d"][-1]) if data_end else s["d"][-1]
        series = build_feature_series(
            features, s["d"], s["c"], regime=(regime_dates, [v[0] for v in regime_vals]),
            events=load_structural_events(conn, sym) if needs_events else None)
        live_cache = {}
        for key, spec in specs.items():
            fns = (compile_tree(spec["entry"], series), compile_tree(spec["invalidation"], series),
                   compile_tree(spec["success"], series))
            tr, still_open = _simulate_symbol(s, fns, family, p, ctx, regime_dates[0], live_cache)
            trades[key].extend(tr)
            open_counts[key] += still_open

    run_config = {
        "holdout_start": HOLDOUT_START.isoformat(),
        "discovery_start": regime_dates[0].isoformat(),
        "data_end": data_end.isoformat() if data_end else None,
        "universe_symbols": len(universe),
        "exit_family": family,
        "stop_loss_pct": p["stop_loss_pct"],
        "time_stop_bars": TIME_STOP_BARS,
        "buy_cooldown_days": p["buy_cooldown_days"],
        "round_trip_cost": ROUND_TRIP_COST,
        "min_trades": MIN_TRADES, "min_weeks": MIN_WEEKS,
        "bootstrap": {"reps": BOOTSTRAP_REPS, "seed": BOOTSTRAP_SEED, "block": "iso_week"},
        "adjusted_prices": True,
        "code_hash": code_hash(),
        "git_sha": os.environ.get("GIT_SHA"),
    }
    if family == EXIT_LIVE_MR:
        run_config["live_signal_params"] = {k: p[k] for k in (
            "rsi_period", "bb_period", "bb_std", "rsi_oversold", "rsi_overbought", "rsi_strong_oversold",
            "rsi_strong_overbought", "regime_sma_fast", "regime_sma_slow", "regime_band",
            "score_log_min", "score_proposal_min")}
    return {k: compute_metrics(trades[k], open_counts[k]) for k in specs}, run_config


def _next_trial_number(conn, hypothesis_type):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM candidate_backtests
            WHERE hypothesis_type=%s AND status = ANY(%s)
        """, (hypothesis_type, list(_COUNTED_STATUSES)))
        return cur.fetchone()[0] + 1


def _insert_result(conn, candidate_id, hypothesis_type, status, run_config, metrics=None, detail=None):
    trial = _next_trial_number(conn, hypothesis_type) if status in _COUNTED_STATUSES else None
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO candidate_backtests
                (candidate_id, hypothesis_type, status, trial_number, run_config, metrics, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (candidate_id, hypothesis_type, status, trial, json.dumps(run_config),
              json.dumps(metrics, default=str) if metrics is not None else None, detail))
        new_id = cur.fetchone()[0]
    conn.commit()
    return new_id, trial


def run_candidate_backtests(conn, candidate_ids, params=None):
    """Backtest the given candidates, appending one candidate_backtests row
    each. Candidates of the same hypothesis type share one pass over the
    universe. Returns [{candidate_id, backtest_id, status, trial_number}];
    unknown candidate ids come back with status 'not_found' (no row
    written -- there is no candidate to attach it to)."""
    p = params or load_params(conn)
    cands = _load_candidates(conn, candidate_ids)
    found = {c["id"] for c in cands}
    results = [{"candidate_id": cid, "backtest_id": None, "status": "not_found", "trial_number": None}
               for cid in candidate_ids if cid not in found]

    by_type = collections.defaultdict(list)
    for c in cands:
        by_type[c["hypothesis_type"]].append(c)

    for htype, group in by_type.items():
        base_config = {"holdout_start": HOLDOUT_START.isoformat(), "code_hash": code_hash()}
        family = EXIT_FAMILIES.get(htype)
        specs = {c["id"]: c for c in group}
        template = hypothesis_library.get_hypothesis_type(conn, htype)
        if template is not None and template.default_entry_conditions is not None:
            specs["reference"] = {"entry": template.default_entry_conditions,
                                  "invalidation": template.default_invalidation_spec,
                                  "success": template.default_success_spec}
        unsupported = None
        try:
            check_supported(_tree_features(
                t for spec in specs.values() for t in (spec["entry"], spec["invalidation"], spec["success"])))
        except UnsupportedFeature as e:
            unsupported = str(e)
        if unsupported is None and family is None:
            unsupported = f"no exit family registered for hypothesis_type '{htype}'"
        if unsupported:
            for c in group:
                bid, _ = _insert_result(conn, c["id"], htype, STATUS_UNSUPPORTED, base_config, detail=unsupported)
                results.append({"candidate_id": c["id"], "backtest_id": bid, "status": STATUS_UNSUPPORTED,
                                "trial_number": None})
            continue

        try:
            metrics, run_config = _backtest_type(conn, htype, specs, p)
        except Exception as e:
            log.exception(f"candidate_backtest: {htype} failed")
            conn.rollback()
            for c in group:
                bid, _ = _insert_result(conn, c["id"], htype, STATUS_ERROR, base_config, detail=str(e)[:500])
                results.append({"candidate_id": c["id"], "backtest_id": bid, "status": STATUS_ERROR,
                                "trial_number": None})
            continue

        reference = metrics.get("reference")
        for c in group:
            m = dict(metrics[c["id"]])
            m["reference_default_template"] = reference
            enough = m["trades"] >= MIN_TRADES and m["distinct_weeks"] >= MIN_WEEKS
            status = STATUS_COMPLETE if enough else STATUS_INSUFFICIENT
            bid, trial = _insert_result(conn, c["id"], htype, status, run_config, metrics=m)
            results.append({"candidate_id": c["id"], "backtest_id": bid, "status": status, "trial_number": trial})
    return results


def list_candidate_backtests(conn, candidate_id):
    """All results for a candidate, newest first, each with the CURRENT
    cumulative count of evaluated candidates for its hypothesis type."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, candidate_id, hypothesis_type, status, trial_number, run_config, metrics, detail, created_at
            FROM candidate_backtests WHERE candidate_id=%s ORDER BY id DESC
        """, (candidate_id,))
        rows = cur.fetchall()
        out = []
        for r in rows:
            cur.execute("""
                SELECT COUNT(*) FROM candidate_backtests WHERE hypothesis_type=%s AND status = ANY(%s)
            """, (r[2], list(_COUNTED_STATUSES)))
            out.append({"id": r[0], "candidate_id": r[1], "hypothesis_type": r[2], "status": r[3],
                        "trial_number": r[4], "trials_to_date_for_type": cur.fetchone()[0],
                        "run_config": r[5], "metrics": r[6], "detail": r[7],
                        "created_at": r[8].isoformat() if r[8] else None})
    return out

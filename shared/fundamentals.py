"""
Point-in-time fundamental scoring, read from fundamental_facts (raw SEC
EDGAR observations collected by ingest/fundamentals.py). Lives in shared/
(not ingest/) for the same reason shared/feature_store.py does:
shared/signals.py::compute_signals() calls compute_fundamental_score()
directly to attach a shadow score to every signal, and shared/ must not
depend on an ingest-only module -- that would reverse the existing
dependency direction established in the signal-component work.

This is intentionally a small, first-cut scoring formula -- three metrics,
simple thresholds, no sector-relative normalization yet. It exists to
prove the pipeline (collector -> point-in-time facts -> shadow score ->
symbol_features/signal_outcomes) end to end with real data; refining the
formula itself is expected before this component ever leaves shadow mode
(component_weights stays {"technical": 1.0} until a validated backtest
says otherwise -- see docs/signal-component-architecture.md).
"""

import logging

log = logging.getLogger(__name__)

METRICS = ("Revenues", "GrossProfit", "NetIncomeLoss")


def _latest_value_as_of(conn, symbol, metric, as_of):
    """Point-in-time lookup: the most recent fundamental_facts row for
    (symbol, metric) whose accepted_at is on or before `as_of`, regardless
    of which fiscal period it describes or when it was physically inserted
    into this table. A fact filed AFTER as_of must never be used, even if
    its fiscal period predates as_of -- that's exactly the look-ahead bias
    point-in-time correctness exists to prevent."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT value, period_end FROM fundamental_facts
            WHERE symbol=%s AND metric=%s AND accepted_at <= %s
            ORDER BY accepted_at DESC LIMIT 1
        """, (symbol, metric, as_of))
        row = cur.fetchone()
    return (float(row[0]), row[1]) if row and row[0] is not None else (None, None)


def _prior_year_value_as_of(conn, symbol, metric, as_of, anchor_period_end):
    """The same metric's value from the observation whose period_end is
    closest to (but not later than) one year before anchor_period_end,
    still subject to the same accepted_at <= as_of point-in-time filter.
    Used for YoY growth. Returns None if there's no prior-year comparable
    observation available as of `as_of` -- never approximated."""
    if anchor_period_end is None:
        return None
    with conn.cursor() as cur:
        cur.execute("""
            SELECT value FROM fundamental_facts
            WHERE symbol=%s AND metric=%s AND accepted_at <= %s
              AND period_end <= %s - INTERVAL '300 days'
              AND period_end >= %s - INTERVAL '430 days'
            ORDER BY period_end DESC LIMIT 1
        """, (symbol, metric, as_of, anchor_period_end, anchor_period_end))
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def compute_fundamental_score(conn, symbol, as_of):
    """Returns a 0-100 shadow fundamental score, or None if there isn't
    enough point-in-time data to compute one -- never 0. First-cut formula:

      revenue_growth_yoy = (revenue - revenue_prior_year) / revenue_prior_year
      gross_margin       = gross_profit / revenue
      net_margin         = net_income / revenue

    Each of the three contributes up to ~33 points via simple thresholds;
    a component that can't be computed (missing data) is simply excluded
    from the sum and the total is rescaled over however many WERE
    available -- same "available-weight" spirit as
    shared/signal_components.py::weighted_component_score(), kept
    separate here since this isn't yet feeding into that function (this
    module produces one scalar; weighted_component_score combines
    multiple named components, which fundamental_score becomes exactly
    one input to once other components exist)."""
    revenue, revenue_period_end = _latest_value_as_of(conn, symbol, "Revenues", as_of)
    gross_profit, _ = _latest_value_as_of(conn, symbol, "GrossProfit", as_of)
    net_income, _ = _latest_value_as_of(conn, symbol, "NetIncomeLoss", as_of)

    if revenue is None:
        # Every sub-score below is either undefined (margins need revenue
        # as the denominator) or needs revenue itself -- without it there
        # is nothing to score.
        return None

    parts = []  # list of (score_0_to_100, weight) -- weight fixed at 1 each for this first cut

    revenue_prior = _prior_year_value_as_of(conn, symbol, "Revenues", as_of, revenue_period_end)
    if revenue_prior is not None and revenue_prior > 0:
        growth = (revenue - revenue_prior) / revenue_prior
        parts.append((_threshold_score(growth, (-0.10, 20), (0.0, 40), (0.10, 65), (0.20, 85), (0.35, 100)), 1))

    if gross_profit is not None and revenue > 0:
        gross_margin = gross_profit / revenue
        parts.append((_threshold_score(gross_margin, (0.0, 20), (0.20, 45), (0.40, 65), (0.60, 85), (0.80, 100)), 1))

    if net_income is not None and revenue > 0:
        net_margin = net_income / revenue
        parts.append((_threshold_score(net_margin, (-0.10, 15), (0.0, 35), (0.05, 55), (0.15, 80), (0.25, 100)), 1))

    if not parts:
        return None

    return round(sum(score * weight for score, weight in parts) / sum(weight for _, weight in parts), 1)


def _threshold_score(value, *breakpoints):
    """breakpoints: ascending (threshold, score) pairs. Returns the score
    for the highest threshold value meets or exceeds, or the lowest
    breakpoint's score if value is below all of them. Deliberately simple
    (piecewise-constant, not interpolated) -- a first-cut heuristic, not a
    calibrated model; see module docstring."""
    result = breakpoints[0][1]
    for threshold, score in breakpoints:
        if value >= threshold:
            result = score
    return result

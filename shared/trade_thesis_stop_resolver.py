"""
Structure-Aware Stop Resolver, PR 6 of the Hypothesis-Driven Trading
Architecture epic -- per docs/trade-thesis-architecture-reconciliation.md
§5's PR 6 bullet: "Derive thesis_invalidation_price and feed it into the
existing trade_proposals.planned_initial_stop_price -- that column and its
immutable copy-down into trades.initial_stop_price / position_lifecycles
already exist end-to-end." No new column, no new sizing engine -- this
module only changes what value planned_initial_stop_price gets computed
from.

resolve_initial_stop_price() reads (never recomputes) the nearest support
zone shared/market_structure.py's daily-timeframe classification already
persists inside market_structure_history.component_values.nearest_support
-- the same "compute once elsewhere, read back here" split every other
consumer of this infra follows (snapshot_market_structure_for_symbol,
shared/trade_thesis_invalidation.py's structure_choch check, etc). When a
real, sane support level exists, it replaces the plain percentage-based
stop (price * (1 - stop_loss_pct)); otherwise the percentage stop is used
unchanged, same result as if this module didn't exist.

"Sane" means: below the current price (a stop above price is nonsensical)
and no farther than max_structure_stop_multiple times the percentage
stop's own distance from price (signal_params, default 2.5x) -- a stale or
distant support zone must not silently multiply a trade's risk-per-share
past what risk_per_trade_pct position sizing (shared/risk_engine.py) was
tuned to assume. Falls back to the percentage stop, never blocks a
proposal, if the sanity check fails.

Gated behind structure_aware_stop_enabled (signal_params, default 0/off)
-- same disabled-by-default precedent as structure_scoring_enabled and
trade_thesis_instantiation_enabled. With the flag off,
compute_signals()'s planned_initial_stop_price is byte-for-byte the same
percentage calculation as before this PR.
"""

import logging

from market_structure import load_latest_market_structure

log = logging.getLogger(__name__)

STOP_RESOLVER_DEFAULTS = {
    "structure_aware_stop_enabled": 0,
    "max_structure_stop_multiple": 2.5,
}


def load_stop_resolver_params(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM signal_params")
            rows = cur.fetchall()
        params = dict(STOP_RESOLVER_DEFAULTS)
        for row in rows:
            k = row[0] if isinstance(row, (list, tuple)) else row["key"]
            if k not in STOP_RESOLVER_DEFAULTS:
                continue
            v = row[1] if isinstance(row, (list, tuple)) else row["value"]
            params[k] = float(v)
        return params
    except Exception as e:
        log.warning(f"Could not load stop resolver params, using defaults: {e}")
        return dict(STOP_RESOLVER_DEFAULTS)


def _nearest_support_price(conn, symbol):
    """Latest persisted daily nearest_support zone's price, or None if no
    structure snapshot exists yet or no support zone was found below
    price this cycle. Never recomputes -- reads whatever
    update_market_structure() already stored."""
    row = load_latest_market_structure(conn, symbol)
    if not row:
        return None
    component_values = row.get("component_values") or {}
    nearest_support = component_values.get("nearest_support")
    if not nearest_support or nearest_support.get("price") is None:
        return None
    return float(nearest_support["price"])


def resolve_initial_stop_price(conn, symbol, price, percentage_stop_price, params=None):
    """Returns {"stop_price", "source", "structure_support_price"}.
    source is "structure_support" when the structure-derived level was
    used, "percentage_fallback" otherwise (no support zone found, support
    zone at/above price, or support zone failed the sanity-distance cap)."""
    params = params if params is not None else STOP_RESOLVER_DEFAULTS

    support_price = _nearest_support_price(conn, symbol)
    percentage_distance = price - percentage_stop_price

    if support_price is None or support_price <= 0 or support_price >= price:
        return {"stop_price": percentage_stop_price, "source": "percentage_fallback",
                "structure_support_price": support_price}

    support_distance = price - support_price
    max_distance = percentage_distance * params.get(
        "max_structure_stop_multiple", STOP_RESOLVER_DEFAULTS["max_structure_stop_multiple"])
    if percentage_distance > 0 and support_distance > max_distance:
        log.info(
            f"Stop resolver {symbol}: support {support_price:.2f} is "
            f"{support_distance:.2f} below price {price:.2f}, exceeds "
            f"{max_distance:.2f} sanity cap -- falling back to percentage stop"
        )
        return {"stop_price": percentage_stop_price, "source": "percentage_fallback",
                "structure_support_price": support_price}

    log.info(f"Stop resolver {symbol}: using structure support {support_price:.2f} "
              f"(percentage stop would have been {percentage_stop_price:.2f})")
    return {"stop_price": support_price, "source": "structure_support",
            "structure_support_price": support_price}

"""
Market regime detection: tracks SPY, QQQ, and VIX to classify the
broad market environment. Used to gate mean-reversion signals — buying
oversold stocks in a bear market is catching a falling knife.

Regime is stored in market_context table and read by signals.py each cycle.
"""

import logging
import requests
from datetime import datetime, timezone

log = logging.getLogger(__name__)

YF_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; invest-agent/1.0)"}

# Thresholds
SMA_FAST   = 50
SMA_SLOW   = 200
VIX_CALM   = 15.0
VIX_FEAR   = 25.0

INDICES = {
    "SPY": "SPDR S&P 500 ETF — broad market",
    "QQQ": "Invesco QQQ — NASDAQ-100 tech-heavy",
}
VIX_SYMBOL = "^VIX"


def _fetch_closes_yf(symbol, yf_range="1y"):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = requests.get(url, params={"interval": "1d", "range": yf_range},
                     headers=YF_HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    result = data["chart"]["result"][0]
    raw = result["indicators"]["quote"][0]["close"]
    return [float(c) for c in raw if c is not None]


def _sma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _classify_trend(closes):
    """
    Return (trend, sma50, sma200, vs_sma200_pct).
    trend: 'bull' | 'bear' | 'neutral' | 'unknown'
    """
    if len(closes) < SMA_FAST:
        return "unknown", None, None, None

    price   = closes[-1]
    sma50   = _sma(closes, SMA_FAST)
    sma200  = _sma(closes, SMA_SLOW)

    if sma200 is None:
        # Not enough history for 200-day — use 50-day only
        if price > sma50 * 1.02:
            trend = "bull"
        elif price < sma50 * 0.98:
            trend = "bear"
        else:
            trend = "neutral"
        vs_pct = round((price - sma50) / sma50 * 100, 2)
        return trend, round(sma50, 2), None, vs_pct

    vs_pct = round((price - sma200) / sma200 * 100, 2)

    if sma50 > sma200 and price > sma50:
        trend = "bull"          # golden cross + price above both MAs
    elif sma50 < sma200 and price < sma50:
        trend = "bear"          # death cross + price below both MAs
    else:
        trend = "neutral"       # mixed signals

    return trend, round(sma50, 2), round(sma200, 2), vs_pct


def _classify_vix(vix_level):
    """Return (vix_regime, vix_label)."""
    if vix_level is None:
        return "unknown", "unknown"
    if vix_level < VIX_CALM:
        return "calm", f"calm ({vix_level:.1f})"
    if vix_level < VIX_FEAR:
        return "elevated", f"elevated ({vix_level:.1f})"
    return "fear", f"fear/crisis ({vix_level:.1f})"


def compute_market_regime():
    """
    Fetch VIX + index data, compute regime.
    Returns a dict with full market context.
    """
    result = {
        "spy_trend": "unknown",
        "qqq_trend": "unknown",
        "spy_sma50": None,
        "spy_sma200": None,
        "spy_vs_sma200_pct": None,
        "qqq_sma50": None,
        "qqq_sma200": None,
        "qqq_vs_sma200_pct": None,
        "vix": None,
        "vix_regime": "unknown",
        "overall": "unknown",
        "score_modifier": 0,    # added to proposal_min score threshold
        "alloc_modifier": 1.0,  # multiplied by trade_allocation_pct
        "rationale": "",
    }

    # ── SPY ───────────────────────────────────────────────────────────────
    try:
        spy_closes = _fetch_closes_yf("SPY", "2y")
        trend, sma50, sma200, vs_pct = _classify_trend(spy_closes)
        result["spy_trend"]          = trend
        result["spy_sma50"]          = sma50
        result["spy_sma200"]         = sma200
        result["spy_vs_sma200_pct"]  = vs_pct
        log.info(f"Market: SPY trend={trend} price={spy_closes[-1]:.2f} SMA50={sma50} SMA200={sma200} vs200={vs_pct}%")
    except Exception as e:
        log.warning(f"Market regime: SPY fetch failed: {e}")

    # ── QQQ ───────────────────────────────────────────────────────────────
    try:
        qqq_closes = _fetch_closes_yf("QQQ", "2y")
        trend, sma50, sma200, vs_pct = _classify_trend(qqq_closes)
        result["qqq_trend"]          = trend
        result["qqq_sma50"]          = sma50
        result["qqq_sma200"]         = sma200
        result["qqq_vs_sma200_pct"]  = vs_pct
        log.info(f"Market: QQQ trend={trend} price={qqq_closes[-1]:.2f} SMA50={sma50} SMA200={sma200} vs200={vs_pct}%")
    except Exception as e:
        log.warning(f"Market regime: QQQ fetch failed: {e}")

    # ── VIX ───────────────────────────────────────────────────────────────
    try:
        vix_closes = _fetch_closes_yf("^VIX", "1mo")
        vix_level = vix_closes[-1] if vix_closes else None
        vix_regime, vix_label = _classify_vix(vix_level)
        result["vix"]       = round(vix_level, 2) if vix_level else None
        result["vix_regime"] = vix_regime
        log.info(f"Market: VIX={vix_level:.1f} → {vix_regime}")
    except Exception as e:
        log.warning(f"Market regime: VIX fetch failed: {e} — defaulting to 'elevated' (fail closed)")
        result["vix_regime"] = "elevated"
        vix_regime = "elevated"  # fail closed: unknown VIX → treat as elevated, not calm

    # ── Overall regime + trading modifiers ───────────────────────────────
    spy = result["spy_trend"]
    qqq = result["qqq_trend"]

    # Combine SPY + QQQ: if both agree, strong signal; if split, neutral
    if spy == "bull" and qqq in ("bull", "neutral"):
        market = "bull"
    elif spy == "bear" and qqq in ("bear", "neutral"):
        market = "bear"
    elif spy == "bear" and qqq == "bear":
        market = "bear"
    elif spy == "bull" and qqq == "bull":
        market = "bull"
    else:
        market = "neutral"

    # Gate modifiers
    if market == "bull" and vix_regime == "calm":
        overall        = "bull_calm"
        score_modifier = 0       # no change to threshold
        alloc_modifier = 1.0
        rationale      = "Bull market, calm volatility — standard operation"
    elif market == "bull" and vix_regime == "elevated":
        overall        = "bull_volatile"
        score_modifier = 5       # slightly more selective
        alloc_modifier = 0.85
        rationale      = "Bull market but elevated VIX — raise bar slightly, reduce size"
    elif market == "neutral" and vix_regime in ("calm", "elevated"):
        overall        = "neutral"
        score_modifier = 10      # require stronger conviction
        alloc_modifier = 0.85
        rationale      = "Neutral market trend — require stronger signals before buying"
    elif market == "bear" and vix_regime == "calm":
        overall        = "bear_calm"
        score_modifier = 20      # significantly more selective
        alloc_modifier = 0.70
        rationale      = "Bear market — only very high-conviction signals; reduce position size"
    elif market in ("bear", "neutral") and vix_regime == "fear":
        overall        = "bear_fear"
        score_modifier = 35      # near-freeze on new buys
        alloc_modifier = 0.50
        rationale      = "Bear + high VIX (fear/crisis) — near-freeze on new buys, half position size"
    elif vix_regime == "fear":
        overall        = "fear"
        score_modifier = 25
        alloc_modifier = 0.60
        rationale      = "High VIX fear regime — require very strong signals, reduce size"
    else:
        # Fail closed: when we can't classify, treat as neutral/elevated rather than bull/calm.
        # This prevents a data outage from silently removing all risk controls.
        overall        = "unknown"
        score_modifier = 10
        alloc_modifier = 0.85
        rationale      = "Market regime unknown (data gap) — applying neutral-level caution"

    result["overall"]        = overall
    result["score_modifier"] = score_modifier
    result["alloc_modifier"] = alloc_modifier
    result["rationale"]      = rationale

    log.info(f"Market regime: overall={overall} score_mod=+{score_modifier} alloc_mod={alloc_modifier:.0%} — {rationale}")
    return result


def save_market_context(conn, ctx):
    """Upsert current market context to market_context table."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO market_context
                (spy_trend, qqq_trend, spy_sma50, spy_sma200, spy_vs_sma200_pct,
                 qqq_sma50, qqq_sma200, qqq_vs_sma200_pct,
                 vix, vix_regime, overall, score_modifier, alloc_modifier, rationale, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (id) DO UPDATE SET
                spy_trend=EXCLUDED.spy_trend, qqq_trend=EXCLUDED.qqq_trend,
                spy_sma50=EXCLUDED.spy_sma50, spy_sma200=EXCLUDED.spy_sma200,
                spy_vs_sma200_pct=EXCLUDED.spy_vs_sma200_pct,
                qqq_sma50=EXCLUDED.qqq_sma50, qqq_sma200=EXCLUDED.qqq_sma200,
                qqq_vs_sma200_pct=EXCLUDED.qqq_vs_sma200_pct,
                vix=EXCLUDED.vix, vix_regime=EXCLUDED.vix_regime,
                overall=EXCLUDED.overall, score_modifier=EXCLUDED.score_modifier,
                alloc_modifier=EXCLUDED.alloc_modifier, rationale=EXCLUDED.rationale,
                updated_at=NOW()
        """, (
            ctx["spy_trend"], ctx["qqq_trend"],
            ctx["spy_sma50"], ctx["spy_sma200"], ctx["spy_vs_sma200_pct"],
            ctx["qqq_sma50"], ctx["qqq_sma200"], ctx["qqq_vs_sma200_pct"],
            ctx["vix"], ctx["vix_regime"],
            ctx["overall"], ctx["score_modifier"], ctx["alloc_modifier"], ctx["rationale"],
        ))
    conn.commit()


def load_market_context(conn):
    """Load the current market context from DB. Returns dict or None."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM market_context ORDER BY updated_at DESC LIMIT 1")
            row = cur.fetchone()
        if row:
            return dict(row)
    except Exception:
        pass
    return None

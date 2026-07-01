"""
Signal generation: RSI mean reversion + Bollinger Bands + market regime detection.
Based on backtesting research: mean reversion is the only strategy that broadly survives
rigorous walk-forward validation across all asset classes and market conditions.
"""

import logging
import requests

log = logging.getLogger(__name__)

YF_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; invest-agent/1.0)"}

# Defaults — overridden at runtime by signal_params table
DEFAULTS = {
    "rsi_period": 14,
    "rsi_oversold": 30,
    "rsi_overbought": 70,
    "rsi_strong_oversold": 25,
    "rsi_strong_overbought": 75,
    "bb_period": 20,
    "bb_std": 2.0,
    "score_log_min": 30,
    "score_proposal_min": 65,
    "regime_sma_fast": 50,
    "regime_sma_slow": 200,
    "regime_band": 0.02,
}


def load_params(conn):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM signal_params")
            rows = cur.fetchall()
        params = dict(DEFAULTS)
        for row in rows:
            k = row[0] if isinstance(row, (list, tuple)) else row["key"]
            v = float(row[1] if isinstance(row, (list, tuple)) else row["value"])
            params[k] = v
        return params
    except Exception as e:
        log.warning(f"Could not load signal_params, using defaults: {e}")
        return dict(DEFAULTS)


def fetch_closes(symbol, yf_range="1y"):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = requests.get(url, params={"interval": "1d", "range": yf_range},
                     headers=YF_HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    result = data["chart"]["result"][0]
    timestamps = result["timestamp"]
    raw_closes = result["indicators"]["quote"][0]["close"]
    closes = [float(c) for c, ts in zip(raw_closes, timestamps) if c is not None]
    return closes


def compute_rsi(closes, period):
    period = int(period)
    if len(closes) < period + 2:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_bollinger(closes, period, num_std):
    period = int(period)
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    sma = sum(window) / period
    variance = sum((x - sma) ** 2 for x in window) / period
    std = variance ** 0.5
    return sma + num_std * std, sma, sma - num_std * std


def detect_regime(closes, fast, slow, band):
    fast, slow = int(fast), int(slow)
    if len(closes) < slow:
        if len(closes) < fast:
            return "unknown"
        sma_fast = sum(closes[-fast:]) / fast
        current = closes[-1]
        if current > sma_fast * (1 + band):
            return "trending_up"
        elif current < sma_fast * (1 - band):
            return "trending_down"
        return "ranging"
    sma_fast = sum(closes[-fast:]) / fast
    sma_slow = sum(closes[-slow:]) / slow
    if sma_fast > sma_slow * (1 + band):
        return "trending_up"
    elif sma_fast < sma_slow * (1 - band):
        return "trending_down"
    return "ranging"


def score_signal(rsi, price, bb_upper, bb_lower, regime, side, p):
    """Return (score 0-100, rationale string) for a given side."""
    score = 0.0
    parts = []
    oversold = p["rsi_oversold"]
    overbought = p["rsi_overbought"]
    strong_oversold = p["rsi_strong_oversold"]
    strong_overbought = p["rsi_strong_overbought"]

    if side == "buy":
        if rsi is not None:
            if rsi < strong_oversold:
                score += 45; parts.append(f"RSI {rsi:.1f} deeply oversold (<{strong_oversold})")
            elif rsi < oversold:
                score += 35; parts.append(f"RSI {rsi:.1f} oversold (<{oversold})")
            elif rsi < oversold + 5:
                score += 20; parts.append(f"RSI {rsi:.1f} near oversold")

        if bb_lower is not None:
            if price <= bb_lower:
                score += 35; parts.append(f"Price ${price:.2f} below lower BB ${bb_lower:.2f}")
            elif (bb_lower - price) / bb_lower > -0.01:
                score += 20; parts.append(f"Price ${price:.2f} near lower BB ${bb_lower:.2f}")

        if regime == "ranging":
            score = min(score * 1.15, 100); parts.append("ranging market boosts MR")
        elif regime == "trending_up":
            score *= 0.80; parts.append("uptrend reduces MR reliability")
        elif regime == "trending_down":
            score *= 0.90; parts.append("downtrend — catch-falling-knife risk")

    else:  # sell
        if rsi is not None:
            if rsi > strong_overbought:
                score += 45; parts.append(f"RSI {rsi:.1f} deeply overbought (>{strong_overbought})")
            elif rsi > overbought:
                score += 35; parts.append(f"RSI {rsi:.1f} overbought (>{overbought})")
            elif rsi > overbought - 5:
                score += 20; parts.append(f"RSI {rsi:.1f} near overbought")

        if bb_upper is not None:
            if price >= bb_upper:
                score += 35; parts.append(f"Price ${price:.2f} above upper BB ${bb_upper:.2f}")
            elif (price - bb_upper) / bb_upper > -0.01:
                score += 20; parts.append(f"Price ${price:.2f} near upper BB ${bb_upper:.2f}")

        if regime == "ranging":
            score = min(score * 1.15, 100); parts.append("ranging market boosts MR")
        elif regime == "trending_down":
            score *= 0.80; parts.append("downtrend reduces overbought signal reliability")
        elif regime == "trending_up":
            score *= 0.90; parts.append("uptrend — overbought less meaningful")

    return int(score), "; ".join(parts)


def compute_signals(conn, symbols):
    """Main entry point: compute signals for all symbols, write to DB, create proposals."""
    p = load_params(conn)
    log.info(f"Signal params: RSI({int(p['rsi_period'])}) oversold={p['rsi_oversold']} overbought={p['rsi_overbought']} "
             f"BB({int(p['bb_period'])},{p['bb_std']}) proposal_min={p['score_proposal_min']}")

    for sym in symbols:
        try:
            closes = fetch_closes(sym, "1y")
            if len(closes) < int(p["bb_period"]) + 2:
                log.warning(f"Signals: not enough data for {sym} ({len(closes)} days)")
                continue

            price = closes[-1]
            rsi = compute_rsi(closes, p["rsi_period"])
            bb_upper, bb_middle, bb_lower = compute_bollinger(closes, p["bb_period"], p["bb_std"])
            regime = detect_regime(closes, p["regime_sma_fast"], p["regime_sma_slow"], p["regime_band"])

            rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"
            bb_str = f"[{bb_lower:.2f},{bb_upper:.2f}]" if bb_lower is not None else "[?]"
            log.info(f"Signals {sym}: price={price:.2f} RSI={rsi_str} BB={bb_str} regime={regime}")

            for side in ("buy", "sell"):
                score, rationale = score_signal(rsi, price, bb_upper, bb_lower, regime, side, p)
                if score < p["score_log_min"]:
                    continue

                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO signals (symbol, signal_type, score, rationale)
                        VALUES (%s, %s, %s, %s)
                    """, (sym, f"rsi_mr_{side}", score, rationale))
                conn.commit()
                log.info(f"Signal {sym} {side}: score={score} — {rationale}")

                if score >= p["score_proposal_min"]:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT id FROM trade_proposals
                            WHERE symbol=%s AND side=%s AND decision IS NULL
                        """, (sym, side))
                        existing = cur.fetchone()
                        if existing:
                            log.info(f"Proposal for {sym} {side} already open (#{existing[0]}), skipping")
                            continue
                        cur.execute("""
                            INSERT INTO trade_proposals (symbol, side, qty, rationale, signal_score)
                            VALUES (%s, %s, NULL, %s, %s)
                        """, (sym, side, rationale, score))
                    conn.commit()
                    log.info(f"PROPOSAL created: {sym} {side} score={score}")

        except Exception as e:
            log.warning(f"Signals failed for {sym}: {e}")

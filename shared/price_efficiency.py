"""
Momentum / price-efficiency primitives, Price Structure epic PR D.
Independent of PR A-C -- no new table, no DB, no as-of/lookahead design
(these are windowed pure functions over whatever bar slice the caller
already gives them, same "just don't hand it future bars" contract as
shared/signals.py's compute_rsi/compute_bollinger/compute_atr, which this
module deliberately mirrors in shape and convention).

Bar shape: (date, open, high, low, close), oldest->newest -- same
convention shared/market_structure.py established (extends signals.py's
plainer (high, low, close) with date+open, since body/wick ratios need
open and pullback duration needs calendar position).

Five primitives, matching the epic investigation's "missing
infrastructure" list verbatim:
- candle_body_ratio / candle_wick_ratios: single-bar shape.
- directional_efficiency_ratio: Kaufman's Efficiency Ratio over a
  lookback window -- net directional distance / total path length
  traveled, 1.0 = perfectly straight-line move, near 0 = pure chop.
- bar_overlap_ratio: how much one bar's range overlaps the previous
  bar's, as a fraction of the current bar's own range.
- pullback_depth_pct / pullback_duration_bars: deliberately
  self-contained, NOT dependent on shared/market_structure.py's
  structural_swings -- a rolling N-bar high/low is used as a lightweight
  swing proxy instead. This is a real, distinct definition from PR A's
  confirmed-swing-based one, chosen specifically so this module has no
  hard dependency on PR A-C and can be computed standalone (matching the
  investigation's explicit "independent of A-C, could run in parallel"
  scoping for this PR). A future PR could add a second,
  structural-swing-anchored variant if the two definitions turn out to
  disagree in ways that matter for hypothesis testing -- not attempted
  here.
"""


def candle_body_ratio(bar):
    """|close-open| / (high-low). 0 = doji (no net directional movement
    within the bar), 1 = full-range directional bar (no wicks at all).
    None if the bar has zero range (high == low)."""
    _, o, h, l, c = bar
    rng = h - l
    if rng == 0:
        return None
    return abs(c - o) / rng


def candle_wick_ratios(bar):
    """(upper_wick_ratio, lower_wick_ratio), each as a fraction of the
    bar's own range. upper_wick = high - max(open, close); lower_wick =
    min(open, close) - low. (None, None) if the bar has zero range."""
    _, o, h, l, c = bar
    rng = h - l
    if rng == 0:
        return None, None
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    return upper_wick / rng, lower_wick / rng


def directional_efficiency_ratio(closes, lookback):
    """Kaufman's Efficiency Ratio over the trailing `lookback` closes:
    |closes[-1] - closes[-1-lookback]| / sum(|closes[i]-closes[i-1]| for
    the same window). 1.0 = every bar moved in the same direction (a
    straight-line move); near 0 = net-zero displacement despite lots of
    bar-to-bar movement (pure chop). None if there isn't enough history
    or the path length is zero (a fully flat window)."""
    lookback = int(lookback)
    if len(closes) < lookback + 1:
        return None
    window = closes[-(lookback + 1):]
    net_move = abs(window[-1] - window[0])
    path_length = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
    if path_length == 0:
        return None
    return net_move / path_length


def bar_overlap_ratio(bar, prev_bar):
    """Fraction of the current bar's own range that overlaps the
    previous bar's range: max(0, min(high,high_prev) - max(low,low_prev))
    / (high-low). High overlap (near 1) signals consolidation/chop
    between consecutive bars; low overlap (near 0, or bars that don't
    overlap at all -- a gap) signals a trending or gapping move. None if
    the current bar has zero range."""
    _, _, h, l, _ = bar
    _, _, ph, pl, _ = prev_bar
    rng = h - l
    if rng == 0:
        return None
    overlap = max(0.0, min(h, ph) - max(l, pl))
    return overlap / rng


def pullback_depth_pct(ohlc, lookback):
    """Self-contained pullback measure (rolling-window extreme, NOT
    shared/market_structure.py's confirmed-swing data -- see module
    docstring): within the trailing `lookback` bars, find the most
    extreme close (max for an apparent uptrend, min for an apparent
    downtrend, decided by comparing the window's first and last close),
    then express the current close's retracement from that extreme as a
    percentage of the extreme's own move away from the window's starting
    close. 0% = still at the extreme (no pullback yet); 100% = fully
    retraced back to (or past) the window's starting level. None if
    there isn't enough history or the window's starting move was zero
    (nothing to measure a retracement against)."""
    lookback = int(lookback)
    if len(ohlc) < lookback + 1:
        return None
    window = ohlc[-(lookback + 1):]
    closes = [b[4] for b in window]
    start, current = closes[0], closes[-1]
    uptrend = current >= start
    extreme = max(closes) if uptrend else min(closes)
    move = extreme - start
    if move == 0:
        return None
    retraced = extreme - current
    return (retraced / move) * 100.0


def pullback_duration_bars(ohlc, lookback):
    """Bars elapsed since the trailing `lookback`-window's extreme close
    (see pullback_depth_pct for the exact same extreme definition) --
    0 means the extreme IS the current bar (no pullback has started yet).
    None under the same insufficient-history condition as
    pullback_depth_pct."""
    lookback = int(lookback)
    if len(ohlc) < lookback + 1:
        return None
    window = ohlc[-(lookback + 1):]
    closes = [b[4] for b in window]
    start, current = closes[0], closes[-1]
    uptrend = current >= start
    extreme_idx = closes.index(max(closes)) if uptrend else closes.index(min(closes))
    return (len(closes) - 1) - extreme_idx

"""
Minimal Deterministic Backtest Engine, PR 15 of the Hypothesis-Driven
Trading Architecture epic. Substrate for a later "Indicator-Derived
Strategy Candidates + Signal Visualization" work item, decomposed into
several PRs because no reusable backtest engine existed anywhere in this
repo -- every prior backtest is a one-off standalone script under
ingest/research/backtests/*.py, and the one clear existing timing
precedent (backtest_rsi2_key_idea.py, backtest_score_calibration.py) fills
at the SAME bar's own close that produced the signal, the opposite of
lookahead-safe.

This PR builds ONLY the engine substrate: bar iteration, strategy decision
generation, orders, fills, position state, trade lifecycle recording, and
deterministic result serialization, with an explicit execution-timing
concept. No strategies, no indicators, no visualization, no persistence
ship here -- those are later, smaller PRs on top of this.

Execution timing is the central safety property:
- "next_bar_open" (the default): bar t closes -> indicators for t are
  known -> a signal is generated from bars[:t+1] -> earliest execution is
  bar t+1's open. This is the correct semantics for any new close-derived
  hypothesis.
- "same_bar_close": fills at the triggering bar's own close. Exists so a
  caller can explicitly reproduce the legacy one-off-script convention
  when needed -- this PR does not migrate any existing script to use this
  engine, so there is no compatibility requirement being silently changed,
  just an explicit, testable alternative to the default.

The no-lookahead guarantee is structural, not a documentation promise: a
strategy is `Callable[[list[Bar]], str | None]`, and the engine's main
loop only ever calls it with `bars[: i + 1]` -- bar i+1 and beyond are
never in scope for the call that decides bar i's action, the same way
TradeThesis's frozen dataclass (shared/trade_thesis.py) makes immutability
structurally impossible to violate rather than merely documented.

Vocabulary deliberately mirrors existing conventions rather than inventing
new terms: side is lowercase "buy"/"sell" (matches trades.side),
Trade.status is lowercase "open"/"closed" (matches
position_lifecycles.status), and gross_pnl/net_pnl match
PositionLifecycle's existing field names. No unrealized_pnl field exists
here -- in the live codebase that concept lives one layer up, in
shared/lifecycle_performance.py's aggregate view, not on the per-position
object itself, so its absence here isn't a naming gap.
"""

from dataclasses import dataclass, field
from datetime import datetime

EXECUTION_TIMINGS = frozenset({"same_bar_close", "next_bar_open"})

SIDES = frozenset({"buy", "sell"})


@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Signal:
    """One strategy decision. Always recorded, even when the engine's
    position state makes it a no-op (actionable=False) -- e.g. a "buy"
    while already long, or a "sell" while flat. Never silently dropped,
    so a later audit/visualization layer can see every decision the
    strategy made, not just the ones that resulted in a fill."""
    bar_ts: datetime
    side: str
    reason: str
    actionable: bool


@dataclass(frozen=True)
class Fill:
    signal_ts: datetime
    execution_ts: datetime
    side: str
    price: float
    qty: float


@dataclass(frozen=True)
class Trade:
    symbol: str
    status: str
    entry_signal_ts: datetime
    entry_execution_ts: datetime
    entry_price: float
    qty: float
    exit_signal_ts: datetime | None
    exit_execution_ts: datetime | None
    exit_price: float | None
    gross_pnl: float
    net_pnl: float


def _dt_to_json(dt):
    return dt.isoformat() if dt is not None else None


def _dt_from_json(s):
    return datetime.fromisoformat(s) if s is not None else None


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    execution_timing: str
    signals: list = field(default_factory=list)
    fills: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    trade_count: int = 0
    win_rate: float | None = None
    total_pnl: float = 0.0

    def to_json(self):
        """Explicit dict, not dataclasses.asdict() -- matches
        shared/trade_thesis.py::TradeThesis.to_json()'s style. Datetimes
        become ISO strings; from_json() is the exact mirror."""
        return {
            "symbol": self.symbol,
            "execution_timing": self.execution_timing,
            "signals": [
                {"bar_ts": _dt_to_json(s.bar_ts), "side": s.side, "reason": s.reason, "actionable": s.actionable}
                for s in self.signals
            ],
            "fills": [
                {"signal_ts": _dt_to_json(f.signal_ts), "execution_ts": _dt_to_json(f.execution_ts),
                 "side": f.side, "price": f.price, "qty": f.qty}
                for f in self.fills
            ],
            "trades": [
                {
                    "symbol": t.symbol, "status": t.status,
                    "entry_signal_ts": _dt_to_json(t.entry_signal_ts),
                    "entry_execution_ts": _dt_to_json(t.entry_execution_ts),
                    "entry_price": t.entry_price, "qty": t.qty,
                    "exit_signal_ts": _dt_to_json(t.exit_signal_ts),
                    "exit_execution_ts": _dt_to_json(t.exit_execution_ts),
                    "exit_price": t.exit_price,
                    "gross_pnl": t.gross_pnl, "net_pnl": t.net_pnl,
                }
                for t in self.trades
            ],
            "trade_count": self.trade_count,
            "win_rate": self.win_rate,
            "total_pnl": self.total_pnl,
        }

    @classmethod
    def from_json(cls, data):
        signals = [
            Signal(bar_ts=_dt_from_json(s["bar_ts"]), side=s["side"], reason=s["reason"], actionable=s["actionable"])
            for s in data["signals"]
        ]
        fills = [
            Fill(signal_ts=_dt_from_json(f["signal_ts"]), execution_ts=_dt_from_json(f["execution_ts"]),
                 side=f["side"], price=f["price"], qty=f["qty"])
            for f in data["fills"]
        ]
        trades = [
            Trade(
                symbol=t["symbol"], status=t["status"],
                entry_signal_ts=_dt_from_json(t["entry_signal_ts"]),
                entry_execution_ts=_dt_from_json(t["entry_execution_ts"]),
                entry_price=t["entry_price"], qty=t["qty"],
                exit_signal_ts=_dt_from_json(t["exit_signal_ts"]),
                exit_execution_ts=_dt_from_json(t["exit_execution_ts"]),
                exit_price=t["exit_price"],
                gross_pnl=t["gross_pnl"], net_pnl=t["net_pnl"],
            )
            for t in data["trades"]
        ]
        return cls(
            symbol=data["symbol"], execution_timing=data["execution_timing"],
            signals=signals, fills=fills, trades=trades,
            trade_count=data["trade_count"], win_rate=data["win_rate"], total_pnl=data["total_pnl"],
        )


def run_backtest(bars, strategy, *, execution_timing="next_bar_open", qty=1.0):
    """Walk `bars` in order, calling `strategy(bars[: i + 1])` at each bar
    i -- the strategy never sees bar i+1 or later. A returned "buy"/"sell"
    becomes a Signal (always recorded); it becomes a Fill only if
    `actionable` (buy while flat, sell while long) -- see module docstring
    for execution_timing semantics. Returns a BacktestResult."""
    if execution_timing not in EXECUTION_TIMINGS:
        raise ValueError(f"execution_timing must be one of {sorted(EXECUTION_TIMINGS)}, got {execution_timing!r}")
    if not bars:
        return BacktestResult(symbol="", execution_timing=execution_timing)

    symbol = bars[0].symbol
    signals = []
    fills = []
    trades = []
    open_trade = None          # dict of in-progress trade fields, or None if flat
    pending_fill = None        # (Signal, target_index) scheduled for next_bar_open

    def _apply_fill(signal, bar, price):
        nonlocal open_trade
        fill = Fill(signal_ts=signal.bar_ts, execution_ts=bar.ts, side=signal.side, price=price, qty=qty)
        fills.append(fill)
        if signal.side == "buy":
            open_trade = {
                "entry_signal_ts": signal.bar_ts, "entry_execution_ts": bar.ts, "entry_price": fill.price,
            }
        else:  # "sell"
            gross_pnl = (fill.price - open_trade["entry_price"]) * qty
            trades.append(Trade(
                symbol=symbol, status="closed",
                entry_signal_ts=open_trade["entry_signal_ts"], entry_execution_ts=open_trade["entry_execution_ts"],
                entry_price=open_trade["entry_price"], qty=qty,
                exit_signal_ts=signal.bar_ts, exit_execution_ts=bar.ts, exit_price=fill.price,
                gross_pnl=gross_pnl, net_pnl=gross_pnl,
            ))
            open_trade = None

    for i, bar in enumerate(bars):
        if pending_fill is not None and pending_fill[1] == i:
            pending_signal, _ = pending_fill
            _apply_fill(pending_signal, bar, bar.open)
            pending_fill = None

        decision = strategy(bars[: i + 1])
        if decision is not None:
            if decision not in SIDES:
                raise ValueError(f"strategy returned illegal side {decision!r}, must be one of {sorted(SIDES)} or None")
            actionable = (decision == "buy" and open_trade is None) or (decision == "sell" and open_trade is not None)
            signal = Signal(bar_ts=bar.ts, side=decision, reason=f"strategy_signal:{decision}", actionable=actionable)
            signals.append(signal)

            if actionable:
                if execution_timing == "same_bar_close":
                    _apply_fill(signal, bar, bar.close)
                else:  # next_bar_open
                    if i + 1 < len(bars):
                        pending_fill = (signal, i + 1)
                    # else: no next bar -- signal recorded, never filled.

    if open_trade is not None:
        trades.append(Trade(
            symbol=symbol, status="open",
            entry_signal_ts=open_trade["entry_signal_ts"], entry_execution_ts=open_trade["entry_execution_ts"],
            entry_price=open_trade["entry_price"], qty=qty,
            exit_signal_ts=None, exit_execution_ts=None, exit_price=None,
            gross_pnl=0.0, net_pnl=0.0,
        ))

    closed = [t for t in trades if t.status == "closed"]
    trade_count = len(trades)
    win_rate = (sum(1 for t in closed if t.net_pnl > 0) / len(closed)) if closed else None
    total_pnl = sum(t.net_pnl for t in trades)

    return BacktestResult(
        symbol=symbol, execution_timing=execution_timing,
        signals=signals, fills=fills, trades=trades,
        trade_count=trade_count, win_rate=win_rate, total_pnl=total_pnl,
    )


def load_bars(conn, symbol, start, end):
    """Ordered OHLCV bars for `symbol` in [start, end], from price_history.
    Distinct from regime_common.py's load_daily_ohlc()/load_daily_series()
    -- neither takes date bounds or returns volume, so this is a genuinely
    new query, not a duplicate of an existing one. Plain tuple-cursor
    convention, matching the rest of shared/."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT symbol, ts, open, high, low, close, volume
            FROM price_history
            WHERE symbol=%s AND ts >= %s AND ts <= %s
            ORDER BY ts ASC
        """, (symbol, start, end))
        rows = cur.fetchall()
    return [
        Bar(symbol=r[0], ts=r[1], open=float(r[2]), high=float(r[3]), low=float(r[4]), close=float(r[5]),
            volume=float(r[6]) if r[6] is not None else 0.0)
        for r in rows
    ]

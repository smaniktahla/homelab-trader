#!/usr/bin/env python3
"""
Morning digest runner — fetches invest data, posts ATQ task to Hermes-AI2.
Runs via cron at 8:00 AM ET on weekdays.
"""

import requests, json, sys
from datetime import datetime, timezone

INVEST_API = "http://localhost:8100"
ATQ_URL    = "http://10.10.10.226:8700/tasks"

def fetch(path):
    r = requests.get(f"{INVEST_API}{path}", timeout=10)
    r.raise_for_status()
    return r.json()

def fmt_usd(n):
    if n is None: return "—"
    return f"${float(n):,.2f}"

def build_context():
    summary   = fetch("/api/summary")
    positions = fetch("/api/positions")
    account   = fetch("/api/account")
    trades    = fetch("/api/trades?limit=10")

    # Get recent news per symbol (last 24h)
    watchlist = fetch("/api/watchlist")
    news_by_sym = {}
    for w in watchlist:
        sym = w["symbol"]
        items = fetch(f"/api/news/{sym}?limit=5")
        if items:
            news_by_sym[sym] = items

    pending_orders = [t for t in trades if t["status"] not in ("filled","canceled","expired","replaced")]

    return {
        "date": datetime.now(timezone.utc).strftime("%A, %B %d %Y"),
        "account": account,
        "positions": positions,
        "price_summary": summary,
        "news": news_by_sym,
        "pending_orders": pending_orders,
    }

def build_instructions(ctx):
    date = ctx["date"]
    acct = ctx["account"]
    positions = ctx["positions"]
    summary = ctx["price_summary"]
    news = ctx["news"]
    pending = ctx["pending_orders"]

    lines = [f"📊 *Investment Morning Digest — {date}*\n"]

    # Account snapshot
    lines.append(f"*Portfolio:* {fmt_usd(acct['equity'])} | *Cash:* {fmt_usd(acct['cash'])}")

    # Positions
    if positions:
        lines.append("\n*Positions:*")
        for p in positions:
            arrow = "▲" if p["unrealized_pl"] >= 0 else "▼"
            lines.append(f"  {p['symbol']}: {p['qty']} shares @ {fmt_usd(p['current_price'])} "
                         f"| {arrow} {fmt_usd(abs(p['unrealized_pl']))} ({p['unrealized_plpc']:+.2f}%)")
    else:
        lines.append("\n*Positions:* All cash, no open positions.")

    # Price moves
    lines.append("\n*Watchlist Moves:*")
    for s in summary:
        arrow = "▲" if (s["day_pct"] or 0) >= 0 else "▼"
        lines.append(f"  {s['symbol']}: {fmt_usd(s['price'])} {arrow} {abs(s['day_pct'] or 0):.2f}%")

    # Pending orders
    if pending:
        lines.append("\n*Pending Orders:*")
        for t in pending:
            lines.append(f"  {t['side'].upper()} {t['symbol']} — {t['status']}")

    # News summary prompt for Hermes LLM
    news_block = []
    for sym, items in news.items():
        headlines = [f"- {it['headline']}" for it in items[:5]]
        news_block.append(f"*{sym}:*\n" + "\n".join(headlines))
    news_text = "\n".join(news_block) if news_block else "No recent news."

    # Full instructions for Hermes
    instructions = f"""You are generating a morning investment digest for Salil.

Here is the pre-compiled market data:

{chr(10).join(lines)}

Recent news headlines for context:
{news_text}

Your job:
1. Review the news headlines for each symbol and identify 1-2 that are most notable or market-moving.
2. Add a brief "📰 News highlights" section to the digest summarizing only what matters.
3. If any position has a notable move (>2% gain or loss), add a short observation.
4. End with a one-line "🔍 Watch today:" callout if anything stands out.
5. Send the complete digest to Salil via WhatsApp self-chat.

Keep it concise — this is a quick morning briefing, not a research report. Plain text with minimal emoji is fine."""

    return instructions

def main():
    print(f"[digest_runner] Starting at {datetime.now().isoformat()}")
    try:
        ctx = build_context()
    except Exception as e:
        print(f"[digest_runner] Failed to fetch invest data: {e}", file=sys.stderr)
        sys.exit(1)

    instructions = build_instructions(ctx)

    task = {
        "type": "invest_digest",
        "instructions": instructions,
        "context": {
            "account": ctx["account"],
            "positions": ctx["positions"],
            "pending_orders": ctx["pending_orders"],
        },
        "assigned_to": "hermes-ai2"
    }

    r = requests.post(ATQ_URL, json=task, timeout=10)
    r.raise_for_status()
    task_id = r.json().get("id") or r.json().get("task", {}).get("id")
    print(f"[digest_runner] ATQ task created: {task_id}")

if __name__ == "__main__":
    main()

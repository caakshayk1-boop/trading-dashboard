"""
Fetch TradeFlow Pro (Vercel) signals and send to Telegram.
/vercel        → incremental (only new since last fetch today)
/vercel all    → everything generated today
/vercel ohl    → OHL/OLL setups only

Resets daily at midnight IST — each day is a fresh slate.
State stored in cache/vercel_sent.json
"""

import json
import os
import urllib.request
from datetime import datetime, timezone, timedelta

# Config — override in config.py
try:
    from config import VERCEL_URL  # type: ignore
except ImportError:
    VERCEL_URL = os.environ.get(
        "VERCEL_URL",
        "https://tradeflow-jj0g1i15o-caakshayk1-2392s-projects.vercel.app"
    )

CACHE_FILE = "cache/vercel_sent.json"
IST = timezone(timedelta(hours=5, minutes=30))


def _today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def _load_cache() -> dict:
    os.makedirs("cache", exist_ok=True)
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                data = json.load(f)
            # Wipe if stale (different day)
            if data.get("date") != _today_ist():
                return {"date": _today_ist(), "sent": []}
            return data
        except Exception:
            pass
    return {"date": _today_ist(), "sent": []}


def _save_cache(data: dict):
    os.makedirs("cache", exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f)


def _fetch(path: str):
    url = f"{VERCEL_URL}{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TradeBot/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[vercel_signals] fetch error {url}: {e}")
        return None


def _item_key(item: dict) -> str:
    """Unique key for deduplication."""
    symbol = item.get("symbol", "?")
    signal = item.get("signal") or item.get("action") or item.get("signal_type") or "?"
    stype  = item.get("_type") or item.get("type") or "item"
    return f"{symbol}:{signal}:{stype}"


def _format_signal(item: dict) -> str:
    symbol  = item.get("symbol", "?")
    signal  = (item.get("signal") or item.get("action") or "").upper()
    stype   = item.get("_type") or ""
    price   = item.get("livePrice") or item.get("price") or 0
    pct     = item.get("livePct") or 0
    setup   = item.get("setup") or item.get("strategy") or ""
    entry   = item.get("entry")
    sl      = item.get("sl") or item.get("stop")
    target  = item.get("target") or item.get("t1")
    rr      = item.get("rr")

    emoji = "🟢" if signal == "BUY" else "🔴" if signal == "SELL" else "🔵"
    tag   = "BREAKOUT" if stype == "breakout" else "AI SIGNAL"

    line = f"{emoji} *{symbol}* [{tag}] — {signal}"
    if price:
        sign = "+" if pct >= 0 else ""
        line += f"\n   Price: ₹{price} ({sign}{pct}%)"
    if setup:
        line += f"\n   Setup: {setup}"
    if entry:
        line += f"\n   Entry ₹{entry}"
        if sl:    line += f" | SL ₹{sl}"
        if target: line += f" | T1 ₹{target}"
        if rr:    line += f" | RR {rr}"
    return line


def _format_ohl(item: dict) -> str:
    symbol = item.get("symbol", "?")
    stype  = item.get("type", "?")     # OHL / OLL
    signal = item.get("signal", "?")   # BUY / SELL
    close  = item.get("close", 0)
    note   = item.get("note", "")
    emoji  = "🟢" if signal == "BUY" else "🔴"
    return (
        f"{emoji} *{symbol}* [{stype}] — {signal}\n"
        f"   ₹{close} · {note}"
    )


def get_vercel_report(mode: str = "incremental") -> str:
    """
    mode: "incremental" | "all" | "ohl"
    Returns formatted message string, or None if nothing new.
    """
    cache = _load_cache()

    # Fetch data
    sl_data  = _fetch("/api/streamlit/signals") or {}
    ohl_data = _fetch("/api/streamlit/ohl") or []

    signals   = sl_data.get("signals",   [])
    breakouts = sl_data.get("breakouts", [])
    ohl       = ohl_data if isinstance(ohl_data, list) else []

    updated_at = sl_data.get("updatedAt", "")

    # Tag types
    all_signals = (
        [dict(i, _type="breakout") for i in breakouts] +
        [dict(i, _type="signal")   for i in signals]
    )

    parts = []

    if mode == "ohl":
        if not ohl:
            return "📭 No OHL/OLL setups found today."
        buys  = [o for o in ohl if o.get("signal") == "BUY"]
        sells = [o for o in ohl if o.get("signal") == "SELL"]
        header = f"📊 *OHL/OLL Scanner — {_today_ist()}*\n"
        header += f"🟢 {len(buys)} OLL (Buy) | 🔴 {len(sells)} OHL (Sell)\n"
        parts.append(header)
        for o in ohl:
            parts.append(_format_ohl(o))
        return "\n".join(parts)

    # Signals mode
    if mode == "all":
        to_show = all_signals
        ohl_show = ohl
    else:
        # Incremental — filter already-sent
        sent = set(cache.get("sent", []))
        to_show  = [i for i in all_signals if _item_key(i) not in sent]
        ohl_show = [o for o in ohl if _item_key(o) not in sent]

    if not to_show and not ohl_show:
        if mode == "incremental":
            return "✅ No new signals since last check."
        return "📭 No signals generated today yet."

    # Mark as sent
    newly_sent = [_item_key(i) for i in to_show] + [_item_key(o) for o in ohl_show]
    cache["sent"] = list(set(cache.get("sent", []) + newly_sent))
    _save_cache(cache)

    prefix = "🔄 *Incremental Update*" if mode == "incremental" else "📋 *All Signals Today*"
    date_str = _today_ist()
    header = f"{prefix} — {date_str}\n"
    if updated_at:
        try:
            dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            ist_time = dt.astimezone(IST).strftime("%H:%M IST")
            header += f"_Scanner updated: {ist_time}_\n"
        except Exception:
            pass

    parts.append(header)

    if to_show:
        parts.append(f"\n*📡 Signals ({len(to_show)}):*")
        for item in to_show:
            parts.append(_format_signal(item))

    if ohl_show:
        buys  = [o for o in ohl_show if o.get("signal") == "BUY"]
        sells = [o for o in ohl_show if o.get("signal") == "SELL"]
        parts.append(f"\n*📊 OHL/OLL ({len(ohl_show)}):*")
        parts.append(f"🟢 {len(buys)} OLL | 🔴 {len(sells)} OHL")
        for o in ohl_show[:5]:  # Top 5 in summary mode
            parts.append(_format_ohl(o))

    return "\n".join(parts)


if __name__ == "__main__":
    print(get_vercel_report("all"))

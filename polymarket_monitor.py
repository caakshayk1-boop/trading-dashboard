"""
Polymarket BTC Market Monitor
Runs every 5 min on Railway — alerts Telegram when an arb-able BTC
price threshold market appears (the Gravia opportunity).

Alert criteria:
  - Market question contains a specific BTC price level (e.g. "above $105,000")
  - Volume > $10,000 (liquid enough to trade)
  - Market ends within 72 hours (short-term = fat latency edge)
  - YES price currently between 0.05 and 0.70 (room to move)
"""

import os
import re
import asyncio
import aiohttp
import logging
from datetime import datetime, timezone
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger("poly_monitor")

GAMMA_HOST   = "https://gamma-api.polymarket.com"
POLY_HOST    = "https://clob.polymarket.com"
CHECK_EVERY  = 300          # seconds (5 min)
MIN_VOLUME   = 10_000       # $10K minimum volume
MAX_HOURS    = 72           # only markets ending within 72 hours
MIN_PRICE    = 0.05         # YES token must be at least 5¢
MAX_PRICE    = 0.70         # and at most 70¢ (so there's upside)

# Track what we've already alerted on
alerted: set = set()


# ── Telegram ──────────────────────────────────────────────────────────────────
async def tg(session: aiohttp.ClientSession, msg: str) -> None:
    try:
        await session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=aiohttp.ClientTimeout(total=10),
        )
        log.info(f"Telegram sent: {msg[:80]}")
    except Exception as e:
        log.error(f"Telegram failed: {e}")


# ── Threshold Parser ──────────────────────────────────────────────────────────
def extract_threshold(question: str) -> float | None:
    q = question.upper()
    patterns = [
        r"\$\s*([\d,]+(?:\.\d+)?)\s*K\b",
        r"\$\s*([\d,]+(?:\.\d+)?)",
        r"\b([\d]{5,})\b",
    ]
    for pat in patterns:
        for m in re.findall(pat, question):
            val = float(str(m).replace(",", ""))
            if "K" in q and val < 1000:
                val *= 1000
            if 10_000 < val < 10_000_000:
                return val
    return None


def hours_until(end_date_str: str) -> float:
    """Hours until market closes."""
    if not end_date_str:
        return 999
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return max(0, (end - now).total_seconds() / 3600)
    except Exception:
        return 999


# ── Orderbook Check ───────────────────────────────────────────────────────────
async def get_yes_price(session: aiohttp.ClientSession, token_id: str) -> float | None:
    try:
        async with session.get(
            f"{POLY_HOST}/book",
            params={"token_id": token_id},
            timeout=aiohttp.ClientTimeout(total=4),
        ) as resp:
            if resp.status != 200:
                return None
            book = await resp.json()
            asks = book.get("asks", [])
            if not asks:
                return None
            return float(min(asks, key=lambda x: float(x["price"]))["price"])
    except Exception:
        return None


# ── Main Check ────────────────────────────────────────────────────────────────
async def check_once(session: aiohttp.ClientSession) -> None:
    try:
        async with session.get(
            f"{GAMMA_HOST}/markets",
            params={"active": "true", "closed": "false", "limit": "500"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            raw = await resp.json()
            markets = raw if isinstance(raw, list) else raw.get("markets", [])
    except Exception as e:
        log.error(f"Fetch failed: {e}")
        return

    btc_markets = []
    for m in markets:
        q = m.get("question") or ""
        if not any(k in q.upper() for k in ["BTC", "BITCOIN"]):
            continue

        threshold = extract_threshold(q)
        if not threshold:
            continue

        vol = float(m.get("volume") or 0)
        if vol < MIN_VOLUME:
            continue

        hours = hours_until(m.get("endDate", ""))
        if hours > MAX_HOURS or hours <= 0:
            continue

        btc_markets.append({
            "id": m.get("id"),
            "question": q,
            "threshold": threshold,
            "volume": vol,
            "hours": hours,
            "yes_token": (m.get("clobTokenIds") or [""])[0],
        })

    log.info(f"Scan complete — {len(btc_markets)} qualifying BTC threshold markets")

    for market in btc_markets:
        market_id = market["id"]
        if market_id in alerted:
            continue

        yes_price = None
        if market["yes_token"]:
            yes_price = await get_yes_price(session, market["yes_token"])

        if yes_price is None:
            continue
        if not (MIN_PRICE <= yes_price <= MAX_PRICE):
            continue

        # New qualifying market — alert!
        alerted.add(market_id)

        edge_estimate = f"{(0.88 - yes_price):.2f}" if yes_price < 0.55 else "watch"
        msg = (
            f"🚨 <b>GRAVIA ALERT — BTC MARKET FOUND</b>\n\n"
            f"📋 <b>{market['question']}</b>\n\n"
            f"💰 Threshold: <b>${market['threshold']:,.0f}</b>\n"
            f"📊 YES price: <b>{yes_price:.3f}¢</b>\n"
            f"📈 Volume: <b>${market['volume']:,.0f}</b>\n"
            f"⏱ Closes in: <b>{market['hours']:.1f} hours</b>\n"
            f"🎯 Est. edge if BTC crosses: <b>{edge_estimate}</b>\n\n"
            f"▶️ Run: <code>PAPER_MODE=false python3 gravia.py</code>"
        )
        await tg(session, msg)
        log.info(f"ALERT sent: {market['question'][:60]} | price={yes_price:.3f}")


# ── Loop ──────────────────────────────────────────────────────────────────────
async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | POLY_MON | %(message)s",
    )
    log.info(f"Polymarket monitor started — checking every {CHECK_EVERY}s")

    async with aiohttp.ClientSession() as session:
        # Send startup ping
        await tg(session, "✅ Polymarket monitor live — watching for BTC arb markets 24/7")

        while True:
            await check_once(session)
            await asyncio.sleep(CHECK_EVERY)


if __name__ == "__main__":
    asyncio.run(run())

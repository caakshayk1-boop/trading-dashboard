#!/usr/bin/env python3
"""
on_demand_runner.py — GitHub Actions entry point for on-demand Telegram commands.

Usage:
    python on_demand_runner.py <command> <chat_id> [args]

Commands: scan | brief | trade | cf | magic | intraday | carousel | track
Called from .github/workflows/on_demand.yml triggered by Vercel webhook.
"""

import os
import sys
import logging
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def post(chat_id: str, text: str):
    """Send Telegram message directly (no bot polling loop needed)."""
    import requests
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if not token or not chat_id:
        log.warning("TELEGRAM_TOKEN or chat_id missing — cannot send message")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
    except Exception as e:
        log.warning(f"Telegram send error: {e}")


def run(command: str, chat_id: str, args: str):
    log.info(f"on_demand: command={command} chat_id={chat_id} args={args!r}")

    if command == "scan":
        try:
            from claude_bot import _run_swing_scan
            post(chat_id, "⚡ Running swing scan... (A/A+ signals only)")
            _run_swing_scan(slot="OnDemand")
        except Exception as e:
            post(chat_id, f"❌ Scan error: `{e}`")

    elif command == "cf":
        try:
            from claude_bot import _scan_commodity_forex
            ts = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
            _scan_commodity_forex(ts, chat_id=chat_id)
        except Exception as e:
            post(chat_id, f"❌ CF scan error: `{e}`")

    elif command == "brief":
        if not args:
            post(chat_id, "Usage: `Brief: NSE:TICKER`")
            return
        try:
            from claude_bot import _handle_brief
            _handle_brief(args, chat_id)
        except Exception as e:
            post(chat_id, f"❌ Brief error: `{e}`")

    elif command == "trade":
        if not args:
            post(chat_id, "Usage: `Trade: NSE:TICKER`")
            return
        try:
            from claude_bot import _handle_trade
            _handle_trade(args, chat_id)
        except Exception as e:
            post(chat_id, f"❌ Trade error: `{e}`")

    elif command == "magic":
        try:
            from claude_bot import _run_magic_scan
            post(chat_id, "🔮 Running Magic + MagicMagic screener (~3–5 min)...")
            _run_magic_scan()
        except Exception as e:
            post(chat_id, f"❌ Magic error: `{e}`")

    elif command == "intraday":
        try:
            from claude_bot import _run_intraday_scan
            post(chat_id, "📊 Running intraday scan...")
            _run_intraday_scan()
        except Exception as e:
            post(chat_id, f"❌ Intraday error: `{e}`")

    elif command == "carousel":
        if not args:
            post(chat_id, "Usage: `Carousel: topic`")
            return
        try:
            from claude_bot import _handle_carousel
            _handle_carousel(args, chat_id)
        except Exception as e:
            post(chat_id, f"❌ Carousel error: `{e}`")

    elif command == "track":
        if not args:
            post(chat_id, "Usage: `/track SYM ENTRY SL T1 T2`")
            return
        try:
            from claude_bot import route as bot_route
            bot_route(f"/track {args}", chat_id)
        except Exception as e:
            post(chat_id, f"❌ Track error: `{e}`")

    else:
        post(chat_id, f"❌ Unknown command: `{command}`")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python on_demand_runner.py <command> <chat_id> [args]")
        sys.exit(1)

    _command  = sys.argv[1]
    _chat_id  = sys.argv[2]
    _args     = sys.argv[3] if len(sys.argv) > 3 else ""

    run(_command, _chat_id, _args)

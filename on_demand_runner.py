#!/usr/bin/env python3
"""
on_demand_runner.py — GitHub Actions entry point for on-demand Telegram commands.

Usage:
    python on_demand_runner.py <command> <chat_id> [args]

Commands: scan | brief (alias research) | trade | cf | magic | intraday | carousel | track
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

    if command in ("brief", "research"):
        command = "brief"

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
            from claude_bot import do_brief
            post(chat_id, do_brief(args))
        except Exception as e:
            log.exception("brief failed")
            post(chat_id, f"❌ Brief error: `{e}`")

    elif command == "trade":
        if not args:
            post(chat_id, "Usage: `Trade: NSE:TICKER`")
            return
        try:
            from claude_bot import do_trade
            post(chat_id, do_trade(args))
        except Exception as e:
            log.exception("trade failed")
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
            from claude_bot import do_carousel
            do_carousel(args, chat_id)          # posts its own document
        except Exception as e:
            log.exception("carousel failed")
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


def selftest() -> int:
    """Import every handler this runner dispatches to.

    The three broken commands were broken for weeks because an ImportError
    inside a per-command try/except reads exactly like a runtime failure and
    the workflow still goes green. This turns that into one loud check.
    """
    import importlib
    need = [("claude_bot", n) for n in
            ("do_brief", "do_trade", "do_carousel", "_run_swing_scan",
             "_scan_commodity_forex", "_run_magic_scan", "_run_intraday_scan", "route")]
    bad = []
    for mod, fn in need:
        try:
            if not hasattr(importlib.import_module(mod), fn):
                bad.append(f"{mod}.{fn} missing")
        except Exception as e:
            bad.append(f"{mod}: {e}")
    for b in bad:
        print(f"FAIL {b}")
    print("selftest: OK" if not bad else f"selftest: {len(bad)} broken")
    return 1 if bad else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(selftest())

    if len(sys.argv) < 3:
        print("Usage: python on_demand_runner.py <command> <chat_id> [args]")
        sys.exit(1)

    _command  = sys.argv[1]
    _chat_id  = sys.argv[2]
    _args     = sys.argv[3] if len(sys.argv) > 3 else ""

    run(_command, _chat_id, _args)

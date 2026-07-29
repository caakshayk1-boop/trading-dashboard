#!/usr/bin/env python3
"""
generate.py — Static HTML generator for GitHub Pages.
Run by GitHub Actions daily at 6am IST.
Calls all newspaper data functions → renders Jinja2 template → writes docs/index.html
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
from datetime import datetime, timezone, timedelta

# ── Bootstrap ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(pathlib.Path(__file__).parent))

# Silence APScheduler startup noise
import logging
logging.basicConfig(level=logging.WARNING)

from jinja2 import Template

# Import everything from newspaper
from newspaper import (
    fetch_weather,
    fetch_global_news,
    fetch_markets,
    get_entrepreneur_quote,
    get_world_lesson,
    get_case_study,
    get_fpna_tip,
    get_cfo_lesson,
    get_chess_lesson,
    get_wisdom_lesson,
    get_book_lesson,
    fetch_alert_log,
    get_top5_picks,
    get_tracker_stocks,
    get_money_hack,
    get_productivity_tip,
    fetch_lichess_games,
    get_lichess_summary,
    init_newspaper_db,
    TEMPLATE,
)

IST = timezone(timedelta(hours=5, minutes=30))

def generate() -> None:
    now = datetime.now(IST)
    print(f"[generate] Running at {now.strftime('%Y-%m-%d %H:%M IST')}")

    # Init DB (creates tables if not exists — returns empty tracker, that's fine)
    try:
        init_newspaper_db()
    except Exception as e:
        print(f"[generate] DB init warning: {e}")

    # Fetch all data
    print("[generate] Fetching weather...")
    weather = fetch_weather()

    print("[generate] Fetching news...")
    news = fetch_global_news()

    print("[generate] Fetching markets...")
    markets = fetch_markets()

    print("[generate] Loading rotating content...")
    quote   = get_entrepreneur_quote()
    lesson  = get_world_lesson()
    case    = get_case_study()
    fpna    = get_fpna_tip()
    cfo     = get_cfo_lesson()
    chess   = get_chess_lesson()
    wisdom  = get_wisdom_lesson()
    book    = get_book_lesson()
    money   = get_money_hack()
    prod    = get_productivity_tip()

    print("[generate] Fetching top 5 picks...")
    top5    = get_top5_picks()
    tracker = get_tracker_stocks()

    print("[generate] Fetching alert log...")
    alerts = fetch_alert_log(limit=200)
    print(f"[generate] Alerts: {len(alerts)} signals found")

    print("[generate] Fetching Lichess games...")
    lichess_games   = fetch_lichess_games()
    lichess_summary = get_lichess_summary(lichess_games)
    print(f"[generate] Lichess: {len(lichess_games)} games yesterday")

    # Render template
    print("[generate] Rendering HTML...")
    html = Template(TEMPLATE).render(
        date_str=now.strftime("%A, %B %d %Y"),
        updated_at=now.strftime("%H:%M"),
        markets=markets,
        news=news,
        weather=weather,
        quote=quote,
        lesson=lesson,
        case=case,
        fpna=fpna,
        cfo=cfo,
        chess=chess,
        wisdom=wisdom,
        book=book,
        money_hack=money,
        productivity_tip=prod,
        top5=top5,
        tracker=tracker,
        lichess_games=lichess_games,
        lichess_summary=lichess_summary,
        alerts=alerts,
    )

    # Write output
    out_dir = pathlib.Path("docs")
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "index.html"
    out_file.write_text(html, encoding="utf-8")
    (out_dir / "alerts.json").write_text(
        json.dumps(alerts, default=str, indent=2), encoding="utf-8"
    )
    size_kb = out_file.stat().st_size // 1024
    print(f"[generate] ✅ Written to {out_file} ({size_kb}KB)")

if __name__ == "__main__":
    generate()

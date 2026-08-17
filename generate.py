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
    fetch_global_news,
    fetch_markets,
    market_regime,
    get_entrepreneur_quote,
    get_world_lesson,
    get_case_study,
    get_fpna_tip,
    get_cfo_lesson,
    get_chess_lesson,
    get_wisdom_lesson,
    get_book_lesson,
    get_way,
    get_review,
    fetch_alert_log,
    get_top5_picks,
    get_fund_screen,
    get_market_intel,
    get_stock_screen,
    get_job_status,
    get_podcasts,
    fetch_smart_reads,
    open_setup_context,
    last_known_picks,
    get_tracker_stocks,
    get_money_hack,
    get_dubai_note,
    daughter_age,
    page_context,
    empty_sections,
    ENGINE_CHANGES,
    get_productivity_tip,
    fetch_lichess_games,
    get_lichess_summary,
    fetch_lichess_puzzle,
    init_newspaper_db,
    TEMPLATE,
    TV_ALIASES,
    _learning_ctx,
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
    print("[generate] Fetching news...")
    news = fetch_global_news()

    print("[generate] Fetching markets...")
    markets = fetch_markets()
    # One risk-appetite reading over the same instruments the rail shows.
    # Empty when nothing priced — the hero block hides rather than printing a
    # confident neutral 50 off a failed fetch.
    regime = market_regime(markets)
    print(f"[generate] Regime: {regime.get('label','—')} {regime.get('score','—')}"
          f"/100 over {regime.get('n', 0)} instruments")

    # Say whether the AI key arrived, never what it is. The thesis path fell
    # back silently for weeks because an absent key returns "" before any
    # request is made, so there was no error anywhere to find.
    _gk = os.environ.get("GROQ_API_KEY", "")
    print(f"[generate] GROQ_API_KEY: {'present, ' + str(len(_gk)) + ' chars' if _gk else 'ABSENT — AI copy will use fallbacks'}")

    print("[generate] Loading rotating content...")
    quote   = get_entrepreneur_quote()
    lesson  = get_world_lesson()
    case    = get_case_study()
    fpna    = get_fpna_tip()
    cfo     = get_cfo_lesson()
    chess   = get_chess_lesson()
    wisdom  = get_wisdom_lesson()
    book    = get_book_lesson()
    way     = get_way()
    review  = get_review()
    money   = get_money_hack()
    dubai   = get_dubai_note()
    daughter = daughter_age()
    import music as _music
    music_lib = _music.library()
    prod    = get_productivity_tip()

    # READ ONLY. build_if_missing=True here cost the daily job its 15-minute
    # budget — ~700 sequential NAV downloads took the run to 15m14s and GitHub
    # cancelled it, breaking the newspaper. The screen is a weekly artefact and
    # is built by its own workflow (fund_screen.yml); this build only renders
    # whatever is already cached, and hides the section when nothing is.
    # Weekly, cached, and built here when the week has rolled over. Seven feed
    # fetches — cheap next to the ~700 NAV downloads the fund screen needs, so
    # unlike that one this can build inside the daily job.
    smart_reads = fetch_smart_reads()
    print(f"[generate] Smart reads: {len(smart_reads)} from "
          f"{len({r['source'] for r in smart_reads})} sources")

    podcasts = get_podcasts(build_if_missing=True)
    print(f"[generate] Podcasts: {len(podcasts.get('episodes', []))} episodes "
          f"from {podcasts.get('shows', 0)} shows"
          f"{' (previous build)' if podcasts.get('is_fallback') else ''}")

    fund_screen = get_fund_screen()
    if fund_screen:
        fund_screen["job_status"] = get_job_status("fund_screen", fund_screen.get("generated_at"))
    _cats = fund_screen.get("categories", [])
    print(f"[generate] Fund screen (cached): {len(_cats)} categories, "
          f"{sum(len(c.get('funds', [])) for c in _cats)} funds")

    # READ ONLY, same reason as the fund screen above: NSE's endpoints can
    # hang or rate-limit, and that must never sit inside the 6 AM build.
    # market_intel.yml owns this clock.
    market_intel = get_market_intel()
    if market_intel:
        market_intel["job_status"] = get_job_status("market_intel", market_intel.get("generated_at"))
    print(f"[generate] Market intel (cached): "
          f"{len(market_intel.get('corporate_actions', []))} corporate actions, "
          f"{len(market_intel.get('market_heat', []))} sectors"
          f"{' (previous build)' if market_intel.get('is_fallback') else ''}")

    # READ ONLY, for exactly the reason above it. The Nifty 500 screen is ~11
    # minutes of sequential Yahoo fetches — two frames plus a quote per symbol —
    # so building it here would repeat the fund screen's mistake with a bigger
    # number. stock_screen.yml owns its clock.
    stock_screen = get_stock_screen()
    if stock_screen:
        stock_screen["job_status"] = get_job_status("stock_screen", stock_screen.get("generated_at"))
    _cov = stock_screen.get("coverage") or {}
    print(f"[generate] Stock screen (cached): {stock_screen.get('count', 0)} companies, "
          f"statements for {_cov.get('statements', 0)}, ROCE for {_cov.get('roce', 0)}"
          f"{' (previous build)' if stock_screen.get('is_fallback') else ''}")

    # Picks are keyed by ISO week. Nothing warms that cache on a static build —
    # under Flask a startup thread did it — so every Monday the section
    # rendered "check back Monday". Build it here, and fall back to the last
    # week we do have rather than shipping an empty section if Yahoo is down.
    print("[generate] Fetching top 5 picks...")
    try:
        top5 = get_top5_picks(build_if_missing=True)
    except Exception as e:
        print(f"[generate] ⚠️  picks build failed: {e}")
        top5 = []
    top5_week = None
    if not top5:
        top5, top5_week = last_known_picks()
        if top5:
            print(f"[generate] ⚠️  using last known picks from {top5_week}")
    print(f"[generate] Picks: {len(top5)}")
    tracker = get_tracker_stocks()

    print("[generate] Fetching alert log...")
    alerts = fetch_alert_log(limit=200)
    print(f"[generate] Alerts: {len(alerts)} signals found")

    print("[generate] Pricing open setups...")
    try:
        open_ctx = open_setup_context(alerts)
        print(f"[generate] Open setup context: {len(open_ctx)} symbols priced")
    except Exception as e:
        print(f"[generate] ⚠️  open setup context failed: {e}")
        open_ctx = {}

    print("[generate] Fetching Lichess games...")
    lichess_games   = fetch_lichess_games()
    lichess_summary = get_lichess_summary(lichess_games)
    lichess_puzzle  = fetch_lichess_puzzle()
    print(f"[generate] Lichess: {len(lichess_games)} games yesterday, puzzle: {bool(lichess_puzzle)}")

    # ── Structured data, crawl files and the social card ────────────────────
    # The site shipped with no description, canonical, OG tags, JSON-LD,
    # sitemap, robots or favicon — /robots.txt, /sitemap.xml, /favicon.ico and
    # /manifest.json all returned 404. Search engines had nothing to index and
    # a shared link rendered as a bare URL.
    site = "https://news.askakshay.com"
    jsonld = json.dumps({
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "Dataset",
                "@id": f"{site}/#dataset",
                "name": "The Daily Signal — NSE trading ledger",
                "description": ("Every trading signal generated by the Dhruvedge engine, "
                                "logged when it fires and scored when it closes. Includes "
                                "entry, stop, targets, outcome and R-multiple."),
                "url": site,
                "license": "https://creativecommons.org/licenses/by/4.0/",
                "creator": {"@id": f"{site}/#person"},
                "temporalCoverage": f"2026-05-09/{now.strftime('%Y-%m-%d')}",
                "dateModified": now.isoformat(),
                "keywords": ["NSE", "trading signals", "India equities",
                             "track record", "expectancy", "R-multiple"],
                "distribution": [{
                    "@type": "DataDownload",
                    "encodingFormat": "application/json",
                    "contentUrl": f"{site}/api/signals",
                }],
            },
            {
                "@type": "Person",
                "@id": f"{site}/#person",
                "name": "Akshay K Kothari",
                "jobTitle": "Chartered Accountant · FP&A",
                "url": "https://askakshay.com",
                "sameAs": [
                    "https://askakshay.com",
                    "https://www.linkedin.com/in/akkothari",
                    "https://terminal.askakshay.com",
                ],
            },
            {
                "@type": "WebSite",
                "@id": f"{site}/#website",
                "url": site,
                "name": "The Daily Signal",
                "publisher": {"@id": f"{site}/#person"},
                "inLanguage": "en-IN",
            },
        ],
    }, separators=(",", ":"))

    # Build id — one per generated shell. A tab compares this against
    # /edition.json to know it is showing a superseded edition.
    build_id = now.strftime("%Y%m%dT%H%M%S")

    # CSP nonce, one per build. Every inline <style> and <script> carries it,
    # which is what lets the policy be strict — no 'unsafe-inline' anywhere.
    # Regenerated each build so a leaked nonce is worthless the next morning.
    import secrets
    nonce = secrets.token_urlsafe(16)

    # Delivered as a <meta> in the document rather than a Vercel header,
    # because the nonce changes every build and vercel.json is static.
    # frame-ancestors is ignored in meta form by design — X-Frame-Options in
    # vercel.json covers that one.
    # script-src is strict — nonce only, no 'unsafe-inline'. That is the
    # directive that actually stops XSS, and it holds here because every
    # <script> on the page is ours and carries the nonce.
    #
    # style-src deliberately allows 'unsafe-inline'. A nonce cannot cover style
    # ATTRIBUTES, only <style> elements, and the moment a nonce is present the
    # browser ignores 'unsafe-inline' for that directive entirely — so the two
    # cannot coexist. This page sets per-item animation delays, ticker segment
    # colours and score-bar widths as inline style attributes computed at
    # runtime; there is no static class that expresses them. Enforcing a strict
    # style-src blocked 50+ of them and broke the layout outright.
    #
    # The trade, stated plainly: CSS injection stays possible, script injection
    # does not. That is the right side of the trade — a style attribute cannot
    # exfiltrate the ledger, and connect-src 'self' means nothing can be sent
    # anywhere regardless.
    # frame-src is the ONLY third-party grant here, and it buys the in-page
    # music player. Without it `default-src 'self'` blocks the embed outright.
    #
    # It points at YouTube. Apple Music was tried and reverted: its widget is
    # genuinely audio-only, but signed out it plays 30 seconds and full
    # playback needs a paid subscription. YouTube is the only source that is
    # free, needs no account, and carries this catalogue at full length.
    #
    # Two deliberate limits on the grant:
    #   · youtube-nocookie.com, not youtube.com — no tracking cookie is set
    #     unless playback actually starts.
    #   · The iframe is created on click, never at page load, so a reader who
    #     does not press play contacts Google not at all.
    #   · script-src is NOT widened. The alternative is YouTube's IFrame API
    #     (or Apple's MusicKit JS), which would need a third-party origin in
    #     script-src and hand it script execution on a page that fronts a
    #     trading ledger. A plain iframe cannot reach into this document, so
    #     the weaker embed is the right trade — the cost is that we cannot
    #     drive playback programmatically, only hand the player a track.
    csp = ("default-src 'self'; "
           f"script-src 'self' 'nonce-{nonce}'; "
           "style-src 'self' 'unsafe-inline'; "
           # i.ytimg.com is the player's poster frame. Without it every play
           # logs CSP violations and the player is a black box until the first
           # frame decodes. The request is made by the embed, which only
           # exists once the reader pressed play and already reached Google,
           # and images cannot execute — no new capability.
           "img-src 'self' data: https://i.ytimg.com; font-src 'self'; "
           "frame-src https://www.youtube-nocookie.com; "
           # The browser only ever talks to this origin's /api. gold-api and
           # Yahoo are called server-side.
           "connect-src 'self'; base-uri 'self'; form-action 'self'; object-src 'none'")

    # Render template — once per page. Same data, same styles; the section
    # guards in the template decide what appears on which.
    _lc = _learning_ctx()
    print("[generate] Rendering HTML...")
    tpl = Template(TEMPLATE)
    base = dict(
        tv_aliases=TV_ALIASES,
        date_str=now.strftime("%A, %B %d %Y"),
        updated_at=now.strftime("%H:%M"),
        build_id=build_id,
        nonce=nonce,
        csp=csp,
        jsonld=jsonld,
        build_date=now.strftime("%Y-%m-%d"),
        markets=markets,
        regime=regime,
        news=news,
        quote=quote,
        lesson=lesson,
        case=case,
        fpna=fpna,
        cfo=cfo,
        chess=chess,
        wisdom=wisdom,
        book=book,
        way=way,
        review=review,
        money_hack=money,
        dubai=dubai,
        daughter=daughter,
        music=music_lib,
        productivity_tip=prod,
        top5=top5,
        # Weekly, cached. build_if_missing so a fresh week actually builds it;
        # a failure returns {} and the section hides rather than failing the build.
        fund_screen=fund_screen,
        market_intel=market_intel,
        stock_screen=stock_screen,
        podcasts=podcasts,
        smart_reads=smart_reads,
        top5_week=top5_week,
        tracker=tracker,
        lichess_games=lichess_games,
        lichess_summary=lichess_summary,
        lichess_puzzle=lichess_puzzle,
        alerts=alerts,
        **_lc,
    )
    # ── today.json — the edition, machine-readable ──────────────────────────
    # The Telegram bot needs the trade ideas and the desk sections, and neither
    # is in Turso: picks are a weekly cache and the desk banks are pure Python
    # content chosen per day. A /api/picks and /api/desk would have been the
    # obvious answer and is not available — the Vercel Hobby plan caps a
    # deployment at 12 serverless functions and vercel-news is already at 12.
    #
    # So the build writes what it already has in memory to a static file. No
    # function, no database round trip, and it cannot disagree with the page
    # because it is rendered from the same objects in the same pass.
    today_json = {
        "build_id": build_id,
        "date": now.strftime("%Y-%m-%d"),
        "date_str": now.strftime("%A, %B %d %Y"),
        "picks": top5,
        # The engine log, so /engine on Telegram quotes the same rule
        # changes the page shows. Same list object the template renders —
        # the bot cannot cite a rule the site does not.
        "engine": ENGINE_CHANGES,
        "picks_week": top5_week,
        # Live price + sector per OPEN setup. Powers distance-to-entry and
        # correlation-aware heat on the page; both would otherwise need a
        # Yahoo call from the browser, which is not available to it.
        "open_context": open_ctx,
        "desk": {
            "chess": chess,
            "wisdom": wisdom,
            "book": book,
            "way": way,
            "quote": quote,
            "lesson": lesson,
            "case": case,
            "fpna": fpna,
            "cfo": cfo,
            "money_hack": money,
            "dubai": dubai,
            "daughter": daughter,
            "productivity": prod,
            "father": _lc.get("father", []),
            "life_wisdom": _lc.get("life_wisdom", []),
            "spanish": _lc.get("spanish", []),
            "vocab": _lc.get("vocab", []),
            "speaking": _lc.get("speaking", []),
            "interview_tech": _lc.get("interview_tech", []),
            "interview_soft": _lc.get("interview_soft", []),
        },
    }

    pages = {"main": "index.html", "desk": "desk.html"}

    # Write output
    out_dir = pathlib.Path("docs")
    out_dir.mkdir(exist_ok=True)
    # Chess moved to /desk in the page split, so the "would this local run
    # clobber real Lichess data?" guard has to look there, not at index.html.
    chess_file = out_dir / "desk.html"
    if not os.environ.get("LICHESS_TOKEN") and chess_file.exists():
        existing = chess_file.read_text(encoding="utf-8")
        if "game-card" in existing and "LICHESS_TOKEN" not in existing:
            print("[generate] ⚠️  Skipping render — desk.html has full chess data, no token locally")
            (out_dir / "alerts.json").write_text(json.dumps(alerts, default=str, indent=2), encoding="utf-8")
            # No edition.json write here on purpose: index.html was not
            # rewritten, so publishing a new build id would tell every open tab
            # to reload into the same page it already has.
            return
    for pg, fname in pages.items():
        ctx = dict(base)
        # Sections with nothing to render are dropped from the nav too,
        # so a reader never gets a link to a section that is not there.
        ctx.update(page_context(pg, drop=empty_sections(fund_screen, podcasts, smart_reads,
                                                        stock_screen, market_intel)))
        (out_dir / fname).write_text(tpl.render(**ctx), encoding="utf-8")
        kb = (out_dir / fname).stat().st_size // 1024
        print(f"[generate] ✅ {fname} ({kb}KB, {len(ctx['secs'])} sections)")
    # The page script. It is a real file in static/ now rather than a string
    # inside newspaper.py — see the header of static/app.js for why. Copied
    # rather than generated, so what ships is byte-identical to what CI linted.
    _appjs = pathlib.Path(__file__).parent / "static" / "app.js"
    if _appjs.exists():
        (out_dir / "app.js").write_text(_appjs.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[generate] ✅ app.js ({_appjs.stat().st_size // 1024}KB)")
    else:
        print("[generate] ❌ static/app.js MISSING — the page will have no behaviour")

    (out_dir / "today.json").write_text(
        json.dumps(today_json, default=str, indent=1), encoding="utf-8")
    print(f"[generate] ✅ today.json ({len(today_json['picks'])} picks, "
          f"{len(today_json['desk'])} desk keys)")
    (out_dir / "alerts.json").write_text(
        json.dumps(alerts, default=str, indent=2), encoding="utf-8"
    )
    # screen.json — the ~500 rows #stocks fetches lazily. Written compact, not
    # indented: it is machine-read only and indent=2 adds ~400KB of whitespace.
    #
    # THREE places or it 404s: written here, named in .vercelignore, and copied
    # in vercel-news/build.js. today.json had two of the three and silently
    # served nothing for days.
    if stock_screen.get("rows"):
        # SPLIT into table + detail. 74% of the payload is fields only the detail
        # sheet reads, and at 750 rows the combined file reached 4.3MB raw /
        # 860KB gzipped — everyone who scrolled to the section downloaded the
        # full research report for all 750 companies to read a 16-column table.
        # Two static files rather than a per-symbol route: Hobby caps this
        # project at 12 functions and it is at 12.
        import stock_screen as _ss
        _table, _detail = _ss.split_payload(stock_screen)
        (out_dir / "screen.json").write_text(
            json.dumps(_table, default=str, separators=(",", ":")), encoding="utf-8")
        (out_dir / "screen-detail.json").write_text(
            json.dumps(_detail, default=str, separators=(",", ":")), encoding="utf-8")
        _sz = (out_dir / "screen.json").stat().st_size / 1024
        _dz = (out_dir / "screen-detail.json").stat().st_size / 1024
        print(f"[generate] ✅ screen.json ({len(_table['rows'])} companies, {_sz:.0f}KB) "
              f"+ screen-detail.json ({_dz:.0f}KB, fetched only when a sheet opens)")
    else:
        # Leave any previous file in place. A build with an empty cache should
        # not delete a working screen — the section hides itself via
        # empty_sections either way, and the next weekly run restores it.
        print("[generate] ⚠ screen.json not written — stock screen cache is empty")
    # Social card, rendered from the same numbers the hero shows. Best-effort:
    # a failed card must never fail the daily build.
    try:
        import og_card
        wins = sum(1 for a in alerts if a.get("badge") == "win")
        losses = sum(1 for a in alerts if a.get("badge") == "loss")
        closed = wins + losses
        wr = round(wins / closed * 100, 1) if closed else None
        adv = sum(1 for m in markets if m.get("up"))
        ok = og_card.render(
            str(out_dir / "og.png"),
            now.strftime("%A, %B %d %Y"),
            wr, len(alerts),
            sum(1 for a in alerts if a.get("badge") == "open"),
            f"{adv}/{len(markets)}" if markets else "",
        )
        print(f"[generate] {'✅' if ok else '⚠️ '} social card")
    except Exception as e:
        print(f"[generate] ⚠️  og card skipped: {e}")

    (out_dir / "robots.txt").write_text(
        "User-agent: *\nAllow: /\n"
        # The API is data, not pages — crawling it wastes budget and can index
        # a JSON blob as a search result.
        "Disallow: /api/\n\n"
        f"Sitemap: {site}/sitemap.xml\n", encoding="utf-8")

    # One entry per archived trading day: /day/:date already renders them and
    # nothing ever told a crawler they existed.
    days = sorted({str(a.get("alert_date") or "")[:10] for a in alerts if a.get("alert_date")},
                  reverse=True)[:200]
    urls = [(site + "/", now.strftime("%Y-%m-%d"), "daily", "1.0"),
            (site + "/desk", now.strftime("%Y-%m-%d"), "daily", "0.8")]
    urls += [(f"{site}/day/{d}", d, "monthly", "0.4") for d in days if d]
    (out_dir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <url><loc>{u}</loc><lastmod>{m}</lastmod>"
                  f"<changefreq>{c}</changefreq><priority>{pr}</priority></url>\n"
                  for u, m, c, pr in urls)
        + "</urlset>\n", encoding="utf-8")

    (out_dir / "manifest.webmanifest").write_text(json.dumps({
        "name": "The Daily Signal", "short_name": "Daily Signal",
        "start_url": "/", "display": "standalone",
        "background_color": "#08090A", "theme_color": "#08090A",
        "description": "Live NSE trading ledger, scored.",
        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    }), encoding="utf-8")
    (out_dir / "icon.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        "<rect width='32' height='32' rx='7' fill='#08090A'/>"
        "<circle cx='16' cy='16' r='6' fill='#B8EF43'/></svg>", encoding="utf-8")

    (out_dir / "edition.json").write_text(json.dumps({
        "build_id":   build_id,
        "build_date": now.strftime("%Y-%m-%d"),
        "built_at":   now.isoformat(),
    }), encoding="utf-8")


if __name__ == "__main__":
    generate()

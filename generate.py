#!/usr/bin/env python3
"""
generate.py — Static HTML generator for GitHub Pages.
Run by GitHub Actions daily at 6am MYT (22:00 UTC).
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
    # The single definition of wins/losses/closed/win-rate. Imported rather
    # than reimplemented: this file had two copies of that arithmetic and the
    # template had a third, and two of the three dropped expiries.
    ledger_counts,
    fetch_global_news,
    fetch_markets,
    market_regime,
    what_matters,
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
    picks_outcomes,
    _week_key,
    get_fund_screen,
    get_market_intel,
    get_brief,
    get_stock_screen,
    get_ipos,
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
    to_myt,)

import data_health as dh

IST = timezone(timedelta(hours=5, minutes=30))
# The operator's timezone. The page is built at 6 AM MYT and read from
# Malaysia, so its date, its clock and its build stamp are all MYT. IST stays
# for anything describing a market session — NSE trades on IST regardless of
# where this runs, and converting a session time would mislead.
MYT = timezone(timedelta(hours=8))

# Countries this search is actually for. Anything else scored by the engine is
# real, but it is not what he asked for, so it is grouped separately rather
# than ranked alongside Dubai — a Nairobi role outranking a Dubai one on raw
# score would make the section useless for the question it exists to answer.
CAREER_TARGET_COUNTRIES = ("UAE", "Saudi Arabia", "Malaysia", "Oman")




def _register_health(*, now, news, markets, regime, smart_reads, brief, market_intel,
                     fund_screen, stock_screen, podcasts, careers, alerts, top5,
                     ipos=None) -> dict:
    """File every dataset the page renders with the health layer, once.

    This is the audit's central fix. Before it, each section answered "is this
    current?" in its own words and none of them could be compared: #funds said
    "0.5d old", #stocks printed a coverage count, the brief said nothing at
    all. A reader could not tell which section was oldest and neither could a
    test.

    Two rules govern everything below.

    ONE: a section fetched live in THIS build is stamped `now` only when it
    actually returned rows. Stamping an empty fetch with the build time is how
    a dead feed comes to look like a fresh one — the exact failure the audit
    named. Empty means no timestamp, which the layer reports as UNAVAILABLE.

    TWO: `expected_records` is passed only where a real denominator exists.
    The stock screen has one (its attempted universe) and careers has one
    (sources attempted). Funds do not — AMFI publishes whatever it publishes —
    so no ratio is printed rather than a confident wrong one.
    """
    dh.reset()
    stamp = now.isoformat()

    def live(name, source, rows, hours, **kw):
        """A section built inside this run. No rows → no timestamp."""
        n = len(rows) if rows is not None else 0
        return dh.track(name, source=source, expected_refresh_hours=hours,
                        generated_at=stamp if n else None, record_count=n, **kw)

    def cached(name, source, payload, hours, **kw):
        """A section read from cache, carrying its own vintage and job row."""
        payload = payload or {}
        return dh.track(name, source=source, expected_refresh_hours=hours,
                        generated_at=payload.get("generated_at"),
                        job=payload.get("job_status") or {},
                        fallback=bool(payload.get("is_fallback")),
                        build_version=payload.get("engine") or payload.get("version"),
                        **kw)

    live("World news", "RSS wires", news, 24)
    # Markets carry their own honesty flag: regime.thin means under half the
    # board priced, which is a degraded reading, not a fresh one.
    live("Markets", "Yahoo Finance", markets, 24,
         error="under half the board priced" if (regime or {}).get("thin") else None)
    live("Smart Reads", "RSS wires", smart_reads, 24)
    live("Signal ledger", "Signal engine", alerts, 24)
    live("Trade ideas", "Weekly picks engine", top5, 168)

    cached("Daily Brief", "wire clustering + Groq", brief, 24)
    cached("Market Intelligence", "NSE / Yahoo", market_intel, 24)
    cached("Podcasts", "podcast feeds", podcasts, 24)
    cached("Fund screen", "AMFI NAV", fund_screen, 168,
           record_count=sum(len(c.get("funds", []))
                            for c in (fund_screen or {}).get("categories", [])) or None)

    # The one the audit called out by name. `expected` comes from the ATTEMPT
    # (how many symbols the last build tried), never from the served payload —
    # dividing a dataset by its own length always yields 100% and would report
    # a 50-row screen as complete coverage of a 50-row universe.
    _job = (stock_screen or {}).get("job_status") or {}
    # 24, not 168: the screen rebuilds nightly as of 2026-08-27. Leaving this
    # at a week would have marked a two-day-old screen FRESH — the freshness
    # badge must describe the promise the schedule actually makes.
    cached("Stock screen", "Yahoo Finance + statements", stock_screen, 24,
           record_count=(stock_screen or {}).get("count"),
           expected_records=_job.get("expected") or (stock_screen or {}).get("attempted"))

    # Probed, not published: the denominator is how many of the universe the
    # run could actually READ, which is the number that decides whether a
    # small cohort means few listings or a broken fetch.
    dh.track("New listings", source="Yahoo firstTradeDate",
             expected_refresh_hours=168,
             generated_at=(ipos or {}).get("generated_at"),
             record_count=(ipos or {}).get("probed"),
             expected_records=(ipos or {}).get("attempted"),
             job=(ipos or {}).get("job_status") or {},
             fallback=bool((ipos or {}).get("is_fallback")))

    # Careers has the cleanest denominator on the site: sources_ok of
    # sources_attempted. 11/21 is a DEGRADED feed and should say so.
    _cs = (careers or {}).get("stats") or {}
    dh.track("Careers feed", source="21 employer / aggregator sources",
             expected_refresh_hours=24,
             generated_at=(careers or {}).get("generated_at"),
             record_count=_cs.get("sources_ok"),
             expected_records=_cs.get("sources_attempted"),
             coverage_floor=0.8,
             error=(careers or {}).get("why"))

    return dh.snapshot(now=now)


def load_careers(path) -> dict:
    """docs/jobs.json → what the Careers section renders.

    READ ONLY and presentation only. Every score, tier, freshness label and
    apply URL is taken verbatim from the file; nothing is recomputed here, so
    the page and the engine cannot drift into disagreeing about a number.

    Fails soft in every direction — missing file, unreadable JSON, wrong shape,
    no renderable rows — because this runs inside the daily paper and a career
    scrape must never be able to take the newspaper down. Each failure returns
    a `why` the build log prints and `empty_sections` uses to drop the nav
    entry, rather than raising.
    """
    try:
        raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"visible": [], "why": "docs/jobs.json not found — jobs.yml has not run yet"}
    except Exception as e:                                   # noqa: BLE001
        return {"visible": [], "why": f"docs/jobs.json unreadable: {e}"}

    if not isinstance(raw, dict) or not isinstance(raw.get("jobs"), list):
        return {"visible": [], "why": "docs/jobs.json is not the expected shape"}

    stats = raw.get("stats") or {}
    # D-tier and excluded rows never reach the primary feed (brief §16).
    rows = [j for j in raw["jobs"]
            if isinstance(j, dict) and not j.get("is_excluded") and j.get("tier") != "D"]
    rows.sort(key=lambda j: (-(j.get("opportunity_score") or 0),
                             (j.get("company") or "")))

    target = [j for j in rows if j.get("country") in CAREER_TARGET_COUNTRIES]
    other = [j for j in rows if j.get("country") not in CAREER_TARGET_COUNTRIES]

    by_country = {}
    for j in target:
        by_country.setdefault(j["country"], []).append(j)

    # Counted from the rendered set, never copied from stats — the file's
    # totals include excluded rows, and a headline that disagrees with the
    # list under it is worse than no headline (brief §23: do not manufacture).
    return {
        "visible": rows,
        "target": target,
        "other": other,
        "top": rows[:10],
        "fresh": [j for j in rows if j.get("status") == "NEW"],
        "by_country": by_country,
        "countries": [c for c in CAREER_TARGET_COUNTRIES if by_country.get(c)],
        "counts": {
            "total": len(rows),
            "s_tier": sum(1 for j in rows if j.get("tier") == "S"),
            "a_tier": sum(1 for j in rows if j.get("tier") == "A"),
            "new": sum(1 for j in rows if j.get("status") == "NEW"),
            "stale": sum(1 for j in rows if j.get("status") == "STALE"),
            "direct": sum(1 for j in rows if j.get("is_direct_apply")
                          and j.get("application_url_verified")),
            "by_country": {c: len(v) for c, v in by_country.items()},
            "other": len(other),
        },
        "stats": stats,
        "sources": raw.get("sources") or [],
        "sources_failed": [s for s in (raw.get("sources") or [])
                           if s.get("status") not in ("ok", None)],
        "generated_at": raw.get("generated_at"),
        "next_refresh": raw.get("next_refresh"),
        "why": "",
    }


def generate() -> None:
    # The page's own date and clock are the OPERATOR's, not the exchange's.
    # It is built at 6 AM MYT and read from Malaysia; dating it in IST put
    # "Friday, August 21" and "00:13 IST" in the chrome of a page whose build
    # stamp two lines below said 6:00 AM MYT — one page, two clocks, and the
    # date itself wrong for several hours every night.
    #
    # Market-hours copy elsewhere stays in IST deliberately: NSE trades on IST
    # wherever this is read.
    now = datetime.now(MYT)
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
    # The "so what" layer. Injected AI, so no key means the reads render
    # exactly as they always did rather than the section failing.
    try:
        import smart_reads as _sr
        from newspaper import groq_complete as _groq, GROQ_KEY as _gk
        _srs = _sr.enrich(smart_reads, _groq if _gk else None)
    except Exception as e:                                   # noqa: BLE001
        print(f"[generate] ⚠️  smart read structuring skipped: {e}")
        _srs = {"structured": 0, "rejected": 0, "attempted": 0, "total": len(smart_reads)}
    print(f"[generate] Smart reads: {len(smart_reads)} from "
          f"{len({r['source'] for r in smart_reads})} sources · "
          f"{_srs['structured']} structured, {_srs['rejected']} gate-rejected "
          f"of {_srs['attempted']} attempted")

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

    # market_intel.yml owns this clock, but unlike the fund screen this one
    # builds inline when the cache has nothing for today: the payload is three
    # bounded fetches (~30s), and the two workflows race. Scheduled runs land
    # 1.5-3h late, so on 2026-08-17 the paper built 45 minutes BEFORE the cache
    # was written and shipped without the section at all. See get_market_intel.
    market_intel = get_market_intel(build_if_missing=True)
    if market_intel:
        market_intel["job_status"] = get_job_status("market_intel", market_intel.get("generated_at"))
    print(f"[generate] Market intel (cached): "
          f"{len(market_intel.get('corporate_actions', []))} corporate actions, "
          f"{len(market_intel.get('market_heat', []))} sectors"
          f"{' (previous build)' if market_intel.get('is_fallback') else ''}")

    # Careers. READ ONLY from docs/jobs.json, which jobs.yml writes on its own
    # clock — the daily paper must never sit inside a 20-source scrape. The
    # file is the contract (JOBS_CONTRACT.md); everything below is presentation
    # over numbers jobs.py already computed, so the page can never disagree
    # with the engine about a score or a freshness label.
    careers = load_careers(pathlib.Path(__file__).parent / "docs" / "jobs.json")
    if careers.get("visible"):
        print(f"[generate] Careers: {len(careers['visible'])} roles "
              f"({careers['stats'].get('s_tier', 0)} S-tier, "
              f"{careers['stats'].get('sources_ok', 0)}/"
              f"{careers['stats'].get('sources_attempted', 0)} sources ok)")
    else:
        print(f"[generate] Careers: nothing renderable — {careers.get('why', 'no jobs.json')}")

    # Daily Intelligence Brief. build_if_missing for the same reason as market
    # intel: brief.yml owns the clock but the two workflows drift
    # independently, and a paper that builds first would ship without the
    # section entirely. Wrapped — a wire outage or a model outage must never
    # take the paper down, and get_brief already falls back to the previous
    # edition before it falls back to nothing.
    brief = get_brief(build_if_missing=True)
    if brief.get("events"):
        _bs = brief.get("stats", {})
        print(f"[generate] Brief: {_bs.get('events')} events from "
              f"{_bs.get('articles')} articles / {_bs.get('sources')} wires "
              f"({_bs.get('ai_written')} written, {_bs.get('ai_rejected')} QA-rejected)"
              f"{' (previous edition)' if brief.get('is_fallback') else ''}")
    else:
        print("[generate] Brief: no events — section hidden")

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

    # READ ONLY, same rule as the screens above: establishing which of 750
    # names listed recently is two passes of network per symbol.
    # ipo_tracker.yml owns that clock.
    ipos = get_ipos()
    if ipos:
        ipos["job_status"] = get_job_status("ipos", ipos.get("generated_at"))
    # IPO Radar. Three HTTP calls against NSE's own public endpoints, so unlike
    # the fund and stock screens it is cheap enough to refresh with the daily
    # build — and it has to be: subscription figures move hourly while a book is
    # open, and a fortnightly Radar would show a stale multiple on a live issue.
    try:
        import ipo_radar
        iporadar = ipo_radar.build(listing_perf=(ipos or {}).get("rows"))
        ipo_radar.OUT.parent.mkdir(parents=True, exist_ok=True)
        ipo_radar.OUT.write_text(json.dumps(iporadar, indent=1))
        print(f"[generate] IPO Radar: {iporadar['counts']['open']} open, "
              f"{iporadar['counts']['upcoming']} upcoming, "
              f"{iporadar['counts']['apply']} APPLY")
    except Exception as e:
        # Never fail the paper for one section. An empty Radar renders its own
        # empty state, which is honest; a crashed build publishes nothing.
        iporadar = None
        print(f"[generate] IPO Radar FAILED: {e}")

    # symbol -> industry for the NIFTY names the ticker quotes.
    sector_map = {}
    try:
        for _r in (stock_screen.get("rows") or []):
            if _r.get("sym") and _r.get("ind"):
                sector_map[_r["sym"]] = _r["ind"]
    except Exception as e:
        print(f"[generate] sector map unavailable: {e}")

    # Per-engine evidence. Reads the ledger already fetched for the alert log,
    # so it costs nothing and cannot disagree with the Signal Log above it.
    try:
        import engine_evidence
        import json as _json
        _sig = _json.loads((pathlib.Path(__file__).parent / "data" /
                            "all_signals.json").read_text())
        evidence = engine_evidence.build(_sig)
        print(f"[generate] Engine evidence: {len(evidence['engines'])} engines, "
              f"{len(evidence['bleeding'])} bleeding, {len(evidence['unproven'])} unproven")
    except Exception as e:
        evidence = None
        print(f"[generate] Engine evidence unavailable: {e}")

    print(f"[generate] New listings (cached): {ipos.get('count', 0)} in "
          f"{ipos.get('months', 0)} months"
          f"{' (previous build)' if ipos.get('is_fallback') else ''}")

    # ── Findings ────────────────────────────────────────────────────────────
    # Deterministic scans over datasets already built. No model writes any of
    # this, so every finding can be reproduced by hand from screen.json.
    try:
        import insights as _ins
        _srows = stock_screen.get("rows") or []
        _hidden = _ins.hidden_findings(_srows)
        findings = {
            "hidden": _hidden,
            # Names clearing more than one of the rules above. Every finding
            # is a single lens; this is the only thing on the page that asks
            # which companies several unrelated lenses agree on.
            "multi": _ins.multi_signal_names(_hidden, _srows),
            "contradictions": _ins.contradictions(
                stock_screen.get("breadth"),
                (market_intel or {}).get("fii_dii")),
            "changed": _ins.what_changed(stock_screen),
            "built_on": stock_screen.get("built_on"),
            "universe": len(_srows),
        }
        # Movers inside each sector, from the SAME screen rows. Attached to
        # market_intel because that is where the sector heat map lives, and
        # this reads directly under it — but built here, where _srows is
        # already in hand, rather than paying for the payload twice.
        if isinstance(market_intel, dict):
            market_intel["sector_movers"] = _ins.sector_movers(_srows)
            print(f"[generate] Sector movers: "
                  f"{len(market_intel['sector_movers'])} sectors")
        print(f"[generate] Findings: {len(findings['hidden'])} hidden, "
              f"{len(findings['multi'])} multi-signal, "
              f"{len(findings['contradictions'])} contradictions"
              f"{' , change report' if findings['changed'] else ''}")
    except Exception as e:                                   # noqa: BLE001
        print(f"[generate] ⚠️  findings skipped: {e}")
        findings = {}

    # Picks are keyed by ISO week. Nothing warms that cache on a static build —
    # under Flask a startup thread did it — so every Monday the section
    # rendered "check back Monday". Build it here, and fall back to the last
    # week we do have rather than shipping an empty section if Yahoo is down.
    # ── Breakout board ──────────────────────────────────────────────────────
    #
    # 20-day breakouts that are actually tradeable. Every field here already
    # ships in screen.json; this is a view over it, not a new dataset.
    #
    #   brk20            closed above the 20-day high
    #   above_mas == 3   above the 20, 50 AND 200 day averages — a breakout
    #                    inside a downtrend is a bounce, not a breakout
    #   turnover >= 10cr you can leave. A Rs 10,00,000 position in a name that
    #                    trades Rs 2cr a day IS the day's volume.
    #   rsi < 75         not already extended. Buying the third day of a
    #                    vertical move is how a breakout becomes a top.
    #
    # NOT filtered on volume, but not for the reason a previous comment here
    # claimed. That comment said vol_spike was true for all 750 rows and so
    # selected nothing; re-checked against the live file, it is a RATIO and it
    # spans 0.08 to 15.09. The real reason is that adding it would change which
    # names this published board has been showing, which is a separate decision
    # from fixing a wrong comment. The ratio now drives its own board below.
    #
    # NOT filtered on delivery percentage: NSE publishes it in the bhavcopy and
    # nothing in this build reads that file, so there is no delivery figure to
    # filter on. Turnover is the liquidity proxy actually available, and the
    # section says so rather than implying a delivery screen it cannot run.
    # ── Build log ───────────────────────────────────────────────────────────
    # Read from git rather than hand-kept: a curated changelog is a second
    # place to remember to write, and the first one to be abandoned.
    buildlog = []
    try:
        import build_log
        buildlog = build_log.build(limit=40)
        print(f"[generate] Build log: {len(buildlog)} product changes")
    except Exception as e:                                   # noqa: BLE001
        print(f"[generate] ⚠️  build log unavailable: {e}")

    breakouts = []
    try:
        _rows = (stock_screen or {}).get("rows") or []
        _cand = [r for r in _rows
                 if r.get("brk20") and r.get("above_mas") == 3
                 and (r.get("turnover_cr") or 0) >= 10
                 and (r.get("rsi") or 100) < 75]
        breakouts = sorted(_cand, key=lambda r: -(r.get("turnover_cr") or 0))[:20]
        print(f"[generate] Breakout board: {len(breakouts)} of {len(_cand)} "
              f"qualifying, from {len(_rows)} screened")
    except Exception as e:                                   # noqa: BLE001
        print(f"[generate] ⚠️  breakout board unavailable: {e}")
        breakouts = []

    # ── Volume board ────────────────────────────────────────────────────────
    # Price tells you where a name went; volume tells you whether anyone came
    # with it. A 2x day on a name that moved 8% and a 2x day on a name that
    # moved -8% are opposite events sharing one number, so the board never
    # shows the ratio alone — the week's move sits beside it and the reading
    # is spelled out in words.
    #
    # Floor of 5cr turnover: a 12x volume day on a name that trades 40 lakh is
    # a ratio artefact, not a crowd. Ratio floor of 2.0 matches the scanner's
    # own surge threshold (scanner.py) rather than inventing a second one.
    volspikes = []
    try:
        _rows = (stock_screen or {}).get("rows") or []
        _vc = [r for r in _rows
               if isinstance(r.get("vol_spike"), (int, float))
               and r["vol_spike"] >= 2.0
               and (r.get("turnover_cr") or 0) >= 5]
        _vc = sorted(_vc, key=lambda r: -(r.get("vol_spike") or 0))[:18]
        for r in _vc:
            mv = r.get("r1w")
            # Deliberately three readings, not two. Volume without direction is
            # the most common case and calling it "accumulation" would be an
            # invented fact — it gets named as what it is.
            r = dict(r)
            # +/-2%, not 3%, to match insights.py's published
            # `volume_without_price` rule EXACTLY. Findings already flags
            # "volume spike >= 2x, 1-week move within +/-2%" as a finding; a
            # board using its own threshold would have quietly disagreed with
            # it about which names are churning, and two rules on one page
            # giving different answers to one question is worse than either
            # rule alone. Same number, two views: the board is the whole
            # population, Findings is the slice worth reading about.
            if isinstance(mv, (int, float)) and mv > 2:
                r["vread"], r["vclass"] = "Bought into", "up"
            elif isinstance(mv, (int, float)) and mv < -2:
                r["vread"], r["vclass"] = "Sold into", "dn"
            else:
                r["vread"], r["vclass"] = "Volume, no price", ""
            volspikes.append(r)
        print(f"[generate] Volume board: {len(volspikes)} of {len(_vc)} "
              f"at 2x+ on 5cr+, from {len(_rows)} screened")
    except Exception as e:                                   # noqa: BLE001
        print(f"[generate] ⚠️  volume board unavailable: {e}")
        volspikes = []

    # The bar column's scale. A FIXED 10x ceiling was the first attempt and it
    # saturated on the first real build — that day's board ran to 19x, so all
    # eighteen bars painted full width and the column carried no information.
    # Scaling to the board's own top makes the bars comparative; the 10x floor
    # is what stops a quiet day, where the best spike is 2.2x, from drawing
    # that as a full bar and implying an event that did not happen.
    volspike_ceiling = 10.0
    try:
        if volspikes:
            volspike_ceiling = max(10.0, round(max(
                v.get("vol_spike") or 0 for v in volspikes), 1))
    except Exception as e:                                   # noqa: BLE001
        print(f"[generate] ⚠️  volume ceiling fell back to 10x: {e}")
        volspike_ceiling = 10.0

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
    # What the ledger now says about those five. The section renders from a
    # snapshot of the ranking with no exit state, so a pick that stopped out on
    # Monday sat on the front page all week looking live.
    try:
        picks_out = picks_outcomes(top5_week or _week_key())
    except Exception as e:                                   # noqa: BLE001
        print(f"[generate] ⚠️  pick outcomes unavailable: {e}")
        picks_out = {}
    for _p in top5:
        _o = picks_out.get(_p.get("symbol")) or picks_out.get(_p.get("name"))
        if _o:
            _p["outcome"] = _o
    print(f"[generate] Picks: {len(top5)}"
          f"{f' ({len(picks_out)} already resolved)' if picks_out else ''}")
    tracker = get_tracker_stocks()

    # ── The Rs 1 crore mandate's order book ────────────────────────────────
    #
    # #picks ranks the week's five best IDEAS. This is a different question:
    # given the mandate, what would actually be placed today, at what size,
    # with which exits. The two sit in the same section because a reader who
    # scrolls to "trade ideas" is asking the second question, and until now the
    # page only answered the first.
    #
    # Its own query rather than fetch_alert_log's: the rulebook needs `id`,
    # `target3` and `entry_triggered_at`, and widening the alert-log SELECT
    # would change a payload three other sections render from.
    print("[generate] Sizing the Rs 1 crore mandate...")
    mandate = None
    try:
        import swing_rulebook as _rb
        from newspaper import _db as _mdb
        with _mdb() as _con:
            _rows = _con.execute("""
                SELECT id, date, symbol, action, timeframe, signal_type, market,
                       entry, sl, target1, target2, target3, score, status,
                       entry_triggered_at
                FROM all_signals
                WHERE status = 'OPEN'
                  AND (entry_triggered_at IS NULL OR entry_triggered_at = '')
                  AND date >= date('now', '-30 day')
                ORDER BY date DESC
            """).fetchall()
        _cols = ["id","date","symbol","action","timeframe","signal_type","market",
                 "entry","sl","target1","target2","target3","score","status",
                 "entry_triggered_at"]
        mandate = _rb.build_book([dict(zip(_cols, r)) for r in _rows], {})
        print(f"[generate] Mandate: {len(mandate['admitted'])} to place, "
              f"{mandate['state']['deployed_pct']}% deployed, "
              f"{mandate['state']['heat_pct']}% heat")
    except Exception as e:                                   # noqa: BLE001
        # A failed sizing must not take the page down, and must not render as
        # an empty order book either — the section guard reads `mandate`, so
        # None removes the block rather than printing "0 to place", which is a
        # different and much more misleading statement.
        print(f"[generate] ⚠️  mandate sizing unavailable: {e}")
        mandate = None

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
                "description": ("Every trading signal this ledger generates, "
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
                ],
            },
            {
                "@type": "WebSite",
                # potentialAction declares the on-page ⌘K search to crawlers.
                # Only claimed because the search genuinely exists and resolves
                # a query string — declaring a SearchAction a site cannot honour
                # is the schema equivalent of a broken link.
                "potentialAction": {
                    "@type": "SearchAction",
                    "target": {
                        "@type": "EntryPoint",
                        "urlTemplate": f"{site}/?q={{search_term_string}}",
                    },
                    "query-input": "required name=search_term_string",
                },
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
           "connect-src 'self'; base-uri 'self'; form-action 'self'; object-src 'none'; "
           # NO frame-ancestors here. This policy ships as a <meta http-equiv>,
           # and frame-ancestors is one of three directives the spec makes
           # header-only (with report-uri and sandbox). In a meta tag it is not
           # merely inert — the browser logs a console error for it, which is
           # what smoke_test.py failed on. It is delivered as a real
           # Content-Security-Policy header from vercel-news/vercel.json
           # instead, which is the only place it does anything.
           #
           # Any http:// subresource that slips into generated content is
           # fetched over https instead of being blocked outright, so a mixed
           # -content mistake degrades to a working request rather than a
           # silently missing image.
           "upgrade-insecure-requests")

    # Render template — once per page. Same data, same styles; the section
    # guards in the template decide what appears on which.
    _lc = _learning_ctx()
    print("[generate] Rendering HTML...")
    tpl = Template(TEMPLATE)
    # The |myt filter is registered on Flask's environment in newspaper.py for
    # the dev server; this is the STATIC build's own environment and needs it
    # too. Without this the template renders but every |myt raises at build
    # time — two render paths, one filter, register on both.
    tpl.environment.filters["myt"] = to_myt
    # ── Data health ─────────────────────────────────────────────────────────
    # Runs after every dataset is resolved and before anything renders, so the
    # page and the health page cannot disagree: both read this one snapshot.
    health = _register_health(
        now=now, news=news, markets=markets, regime=regime, smart_reads=smart_reads,
        brief=brief, market_intel=market_intel, fund_screen=fund_screen,
        stock_screen=stock_screen, podcasts=podcasts, careers=careers,
        alerts=alerts, top5=top5, ipos=ipos)
    print(f"[generate] Data health: {health['current']}/{health['total']} current, "
          f"worst={health['worst']}"
          + (" — " + ", ".join(f"{d['dataset']}:{d['status']}"
                               for d in health["datasets"] if not d["is_current"])
             if health["degraded"] else ""))

    # ── What matters now ────────────────────────────────────────────────────
    # The hero's interpretation layer. Deterministic — see what_matters() in
    # newspaper.py for why this is Python and not a model call.
    #
    # wins/closed are recomputed here rather than read from the template,
    # because the template derives them itself in Jinja and there is no way to
    # hand a {% set %} back to Python. The expression below MIRRORS the Jinja
    # one exactly, including its rounding: Jinja's |round(0) is half-up, while
    # Python's round() is half-to-even, so 24.5% would print 25 on the page and
    # 24 in the card. +0.5 then truncate reproduces the page's number.
    # ONE definition of closed, and it includes every resolved outcome.
    #
    # This counted only badge win/loss and dropped `expired` from the
    # denominator entirely. Ten expired signals were being excluded — trades
    # that resolved without reaching a target, which is not a win by any
    # reading. The result was a hero rail showing 23.6% over 55 while
    # /api/stats showed 20.6% over 34: two numbers, two populations, no
    # statement anywhere that they were measuring different things.
    #
    # A signal that resolved is closed. If it did not reach a target it is not
    # a win. Expiries and time stops count in the denominator, which is the
    # only reading that cannot flatter the record.
    _c = ledger_counts(alerts)
    _wins, _losses = _c["wins"], _c["losses"]
    _closed, _winrate = _c["closed"], _c["winrate"]
    print(f"[generate] Hero win rate: {_winrate}% over {_closed} closed "
          f"({_wins}W, {_losses}L incl. expiries) — last {len(alerts)} alerts")
    matters = what_matters(
        regime=regime, markets=markets, market_intel=market_intel, top5=top5,
        closed=_closed, winrate=_winrate, engine_changes=ENGINE_CHANGES)
    print(f"[generate] What matters: {len(matters)} card(s) — "
          + ", ".join(c["tag"] for c in matters))

    base = dict(
        health=health,
        tv_aliases=TV_ALIASES,
        # symbol -> industry, for the live heat-map drill-down. Built here
        # because the mapping belongs to the stock screen; duplicating it into
        # the serverless ticker route is how two copies drift apart.
        #
        # The whole screen is emitted, not just the NIFTY 50 the rail quotes.
        # It is ~750 short strings and compresses to a few KB, and scoping it to
        # today's constituents would silently break the drill-down the next time
        # the index is reconstituted or another symbol set is added.
        sector_map=sector_map,
        date_str=now.strftime("%A, %B %d %Y"),
        updated_at=now.strftime("%H:%M"),
        build_id=build_id,
        nonce=nonce,
        csp=csp,
        mandate=mandate,
        breakouts=breakouts,
        volspikes=volspikes,
        volspike_ceiling=volspike_ceiling,
        buildlog=buildlog,
        jsonld=jsonld,
        build_date=now.strftime("%Y-%m-%d"),
        markets=markets,
        regime=regime,
        matters=matters,
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
        productivity_tip=prod,
        top5=top5,
        # Weekly, cached. build_if_missing so a fresh week actually builds it;
        # a failure returns {} and the section hides rather than failing the build.
        fund_screen=fund_screen,
        market_intel=market_intel,
        careers=careers,
        brief=brief,
        stock_screen=stock_screen,
        findings=findings,
        ipos=ipos,
        iporadar=iporadar,
        evidence=evidence,
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
        # New listings and data health, so the bot can answer /listings and
        # /health without a second artefact to write, allow-list in four places
        # and keep in sync. Summaries only — the full tables stay on the page.
        "ipos": {
            "count": ipos.get("count", 0),
            "months": ipos.get("months"),
            "built_on": ipos.get("built_on"),
            "summary": ipos.get("summary") or {},
            # Top movers both ways, so a phone reply is useful without
            # shipping all 32 rows through Telegram.
            "rows": sorted(ipos.get("rows") or [],
                           key=lambda r: r.get("since_listing_pct") or 0,
                           reverse=True)[:5],
            "worst": sorted(ipos.get("rows") or [],
                            key=lambda r: r.get("since_listing_pct") or 0)[:3],
        },
        "data_health": {
            "worst": health.get("worst"),
            "current": health.get("current"),
            "total": health.get("total"),
            "degraded": [
                {"dataset": d["dataset"], "status": d["status"],
                 "age": d["freshness_age"], "note": (d.get("notes") or [""])[0]}
                for d in health.get("datasets", []) if not d.get("is_current")
            ],
        },
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
            "cfo_field": _lc.get("cfo_field", []),
            # 2026-09-03: the Life sections moved to career.askakshay.com, which
            # renders them from this file client-side. These six were the only
            # desk payloads the bot never needed and so were never written here;
            # without them the new surface would have had to re-run generators
            # that already ran in this pass.
            "review": review,
            "podcasts": podcasts,
            "smart_reads": smart_reads,
            "lichess_games": lichess_games,
            "lichess_summary": lichess_summary,
            "lichess_puzzle": lichess_puzzle,
        },
    }

    # 2026-09-03: desk.html is no longer rendered. Every Life section moved to
    # career.askakshay.com, which reads them out of today.json["desk"] below.
    # SECTION_MAP has no rows for the desk page any more, so rendering it would
    # emit an empty shell with a nav pointing at nothing.
    pages = {"main": "index.html"}

    # Write output
    out_dir = pathlib.Path("docs")
    out_dir.mkdir(exist_ok=True)
    # The LICHESS_TOKEN guard that used to sit here protected desk.html from
    # being overwritten by a local run with no chess data. desk.html is not
    # written any more, and the chess payload now travels in today.json, so the
    # guard has nothing to protect. Lichess data is still FETCHED — it is part
    # of that payload.
    for pg, fname in pages.items():
        ctx = dict(base)
        # Sections with nothing to render are dropped from the nav too,
        # so a reader never gets a link to a section that is not there.
        ctx.update(page_context(pg, drop=empty_sections(fund_screen, podcasts, smart_reads,
                                                        stock_screen, market_intel, careers,
                                                        brief, health, ipos, findings,
                                                        iporadar, volspikes, buildlog,
                                                        evidence, book)))
        (out_dir / fname).write_text(tpl.render(**ctx), encoding="utf-8")
        kb = (out_dir / fname).stat().st_size // 1024
        print(f"[generate] ✅ {fname} ({kb}KB, {len(ctx['secs'])} sections)")
    # The page script. It is a real file in static/ now rather than a string
    # inside newspaper.py — see the header of static/app.js for why. Copied
    # rather than generated, so what ships is byte-identical to what CI linted.
    # The v2 shell and renderer. Copied, not generated: the client-rendered
    # site reads the same /api and docs/*.json the old template did, so there
    # is nothing for Jinja to do to it. Shipping them from static/ means what
    # reaches production is byte-identical to what CI linted.
    # v2.html/v2.js retired 2026-08-25 — the client-rendered ledger was dropped
    # in favour of restyling the server-rendered page. v2-core.js stays: it is
    # the shared machinery life.askakshay.com renders from.
    # next.* is the mobile-first surface at /next.html. Same by-name rule as
    # everything else here: a file reaches the web only if generate.py writes
    # it, .vercelignore names it AND vercel-news/build.js copies it. Two out of
    # three is a silent 404.
    for _v2 in ("v2-core.js", "life.html", "life.js", "next.html", "next.css", "next.js"):
        _src = pathlib.Path(__file__).parent / "static" / _v2
        if _src.exists():
            _body = _src.read_text(encoding="utf-8")
            # Stamp the build id onto next.html's own two assets, the same way
            # the broadsheet stamps app.js. Without it a reader who visited
            # yesterday runs yesterday's renderer against today's JSON until
            # they hard-reload — and the failure mode is not a blank page, it
            # is a page that renders confidently from keys that have moved.
            if _v2 == "next.html":
                _body = (_body.replace('href="/next.css"', f'href="/next.css?v={build_id}"')
                              .replace('src="/next.js"',  f'src="/next.js?v={build_id}"'))
            (out_dir / _v2).write_text(_body, encoding="utf-8")
            print(f"[generate] ✅ {_v2} ({_src.stat().st_size // 1024}KB)")
        else:
            print(f"[generate] ❌ static/{_v2} MISSING — that surface will 404")

    _appjs = pathlib.Path(__file__).parent / "static" / "app.js"
    if _appjs.exists():
        (out_dir / "app.js").write_text(_appjs.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[generate] ✅ app.js ({_appjs.stat().st_size // 1024}KB)")
    else:
        print("[generate] ❌ static/app.js MISSING — the page will have no behaviour")

    # ── FEEDS FOR /next.html ────────────────────────────────────────────────
    # Three payloads the mobile surface reads instead of the ones the
    # broadsheet uses. Each exists because the obvious source was the wrong
    # size or the wrong content:
    #
    #   pulse.json  screen.json is 1.26 MB and 88 fields over 750 names. A
    #               phone needs the ANSWERS — sector medians, volume spikes,
    #               breakouts, breadth — which are ~9 KB. 140x smaller, and the
    #               client does no arithmetic at all.
    #   ipo.json    today.json ships five OLD listings. The decision-relevant
    #               half of the radar — books open now, issues upcoming, and
    #               what is awaiting listing — was never published anywhere.
    #   news.json   the wire is rendered server-side into index.html and exists
    #               in no machine-readable form. Every other surface therefore
    #               has no news at all.
    try:
        from pulse_builder import build_pulse
        _pulse = build_pulse(stock_screen)
        (out_dir / "pulse.json").write_text(json.dumps(_pulse, default=str), encoding="utf-8")
        print(f"[generate] ✅ pulse.json ({len(json.dumps(_pulse)) // 1024}KB · "
              f"{len(_pulse['sectors'])} sectors, {len(_pulse['volume'])} volume spikes, "
              f"{len(_pulse['breakouts'])} breakouts)")
    except Exception as e:                                    # noqa: BLE001
        print(f"[generate] ❌ pulse.json FAILED: {e} — Markets will render empty")

    try:
        _r = iporadar if "iporadar" in dir() else None
        if _r:
            _keep = ("symbol", "company", "price_band", "price_low", "price_high",
                     "open_date", "close_date", "listing_date", "lot_size",
                     "min_investment", "issue_size_cr", "subscription_x", "gmp_text",
                     "verdict", "verdict_why", "verdict_caveat", "score", "sector",
                     "reads_for", "reads_against", "pe_post_issue", "peer_pe",
                     "peer_pe_n", "roce_pct", "revenue_cr", "pat_cr", "days_left",
                     "enriched_url", "phase", "since_listing_pct", "listed_on",
                     "last_close", "from_high_pct", "sym")
            def _trim(rows):
                return [{k: v for k, v in r.items() if k in _keep} for r in (rows or [])]
            _ipo = {
                "generated_at": _r.get("generated_at"),
                "counts": _r.get("counts"),
                "open": _trim(_r.get("open")),
                "upcoming": _trim(_r.get("upcoming")),
                "awaiting_listing": _trim(_r.get("awaiting_listing")),
                # Context, not the headline: the 90-name history is what the old
                # surface led with, and it is the least actionable thing here.
                "recent_listed": _trim((_r.get("recent_listed") or [])[:24]),
            }
            (out_dir / "ipo.json").write_text(json.dumps(_ipo, default=str), encoding="utf-8")
            print(f"[generate] ✅ ipo.json ({len(json.dumps(_ipo)) // 1024}KB · "
                  f"{len(_ipo['open'])} open, {len(_ipo['upcoming'])} upcoming)")
    except Exception as e:                                    # noqa: BLE001
        print(f"[generate] ❌ ipo.json FAILED: {e} — the IPO tab will render empty")

    # conviction.json — 3-5 names a day, ranked by arithmetic and explained by a
    # model AFTER the ranking is fixed (conviction.py refuses to let the model
    # reorder). Appended to data/conviction_log.json so the slate can be graded
    # later instead of being silently rewritten every morning.
    try:
        import conviction
        _cv = conviction.build(stock_screen)
        (out_dir / "conviction.json").write_text(json.dumps(_cv, default=str), encoding="utf-8")
        _n_log = conviction.append_log(_cv)
        print(f"[generate] ✅ conviction.json ({len(_cv['picks'])} picks · "
              f"{len({p['sector'] for p in _cv['picks']})} sectors · log {_n_log} days)")
    except Exception as e:                                    # noqa: BLE001
        print(f"[generate] ❌ conviction.json FAILED: {e} — Today loses its daily slate")

    # ── funds.json ──────────────────────────────────────────────────────────
    # The fund screen has existed for months and was rendered ONLY into the
    # newspaper's HTML — cached in sqlite, never emitted as a feed. So
    # signal.askakshay.com had no way to show it without recomputing the
    # AMFI screen a second time, which would have been two rankings of the
    # same NAVs disagreeing with each other by the hour they ran.
    #
    # Written here as the tenth build artefact, the same shape as the other
    # nine, so the mirror picks it up and one screen feeds both sites.
    try:
        _fs = fund_screen or {}
        _fcats = _fs.get("categories", [])
        _funds = {
            "ok": bool(_fcats),
            "generated_at": _fs.get("generated_at"),
            "basis": ("AMFI official NAV via api.mfapi.in. DIRECT + GROWTH plans only — "
                      "per-scheme TER is not in the free feed, and Direct vs Regular is "
                      "the one cost lever the data does show, typically 0.5-1.2% a year."),
            "categories": [{
                "key": c.get("key"), "label": c.get("label"), "blurb": c.get("blurb"),
                "funds": [{k: f.get(k) for k in
                           ("name", "code", "nav", "r1y", "r3y", "r5y", "sip10y",
                            "cagr3", "cagr5", "since", "aum", "category")
                           if f.get(k) is not None}
                          for f in (c.get("funds") or [])],
            } for c in _fcats],
        }
        (out_dir / "funds.json").write_text(json.dumps(_funds, default=str), encoding="utf-8")
        print(f"[generate] ✅ funds.json ({len(json.dumps(_funds)) // 1024}KB · "
              f"{len(_fcats)} categories · "
              f"{sum(len(c.get('funds') or []) for c in _fcats)} funds)")
    except Exception as e:                                    # noqa: BLE001
        print(f"[generate] ❌ funds.json FAILED: {e} — the Funds route loses its data")

    try:
        _news = [{"title": n.get("title"), "summary": n.get("summary"),
                  "source": n.get("source"), "link": n.get("link")}
                 for n in (news or [])][:18]
        (out_dir / "news.json").write_text(json.dumps(_news, default=str), encoding="utf-8")
        print(f"[generate] ✅ news.json ({len(json.dumps(_news)) // 1024}KB · {len(_news)} stories)")
    except Exception as e:                                    # noqa: BLE001
        print(f"[generate] ❌ news.json FAILED: {e} — Today will show no wire")

    (out_dir / "today.json").write_text(
        json.dumps(today_json, default=str, indent=1), encoding="utf-8")
    print(f"[generate] ✅ today.json ({len(today_json['picks'])} picks, "
          f"{len(today_json['desk'])} desk keys)")
    (out_dir / "alerts.json").write_text(
        json.dumps(alerts, default=str, indent=2), encoding="utf-8"
    )
    # mandate.json — the Rs 1 crore order book, for the client-rendered site.
    # FOUR places or it 404s: written here, committed by newspaper.yml's git
    # add, named in .vercelignore, and copied in vercel-news/build.js. Three of
    # the four is the documented failure mode — today.json had two and served
    # nothing for days.
    (out_dir / "mandate.json").write_text(
        json.dumps(mandate or {"admitted": [], "unavailable": True}, default=str),
        encoding="utf-8")
    print(f"[generate] ✅ mandate.json ({len((mandate or {}).get('admitted', []))} tickets)")
    # data-health.json — the machine-readable half of the honesty layer, and
    # the one artefact that must publish even when everything else is broken.
    # Small enough to indent; it is meant to be read by a human with curl when
    # the page itself is the thing under suspicion.
    #
    # FOUR places or it 404s: written here, committed by newspaper.yml's git
    # add, named in .vercelignore, and copied in vercel-news/build.js. Three of
    # the four is the documented failure mode — today.json had two and served
    # nothing for days. test_engine_regressions.py checks all four.
    (out_dir / "data-health.json").write_text(
        json.dumps(health, default=str, indent=1), encoding="utf-8")
    print(f"[generate] ✅ data-health.json ({health['total']} datasets, "
          f"worst={health['worst']})")
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
        # The THIRD copy of this arithmetic lived here and, like the
        # template's, dropped expiries — so the card advertised a win rate the
        # page it links to does not publish. Same function as both of them now.
        _oc = ledger_counts(alerts)
        wr = float(_oc["winrate"]) if _oc["closed"] else None
        adv = sum(1 for m in markets if m.get("up"))
        ok = og_card.render(
            str(out_dir / "og.png"),
            now.strftime("%A, %B %d %Y"),
            wr, len(alerts), _oc["opens"],
            f"{adv}/{len(markets)}" if markets else "",
        )
        print(f"[generate] {'✅' if ok else '⚠️ '} social card")

        # The share card. It MUST quote the same figures the site publishes,
        # so it reads /api/stats — the exact endpoint the Performance section
        # renders from — rather than recomputing from `alerts`.
        #
        # Recomputing locally looked simpler and was wrong: `alerts` is the
        # last 200 rows, /api/stats scores a different population, and a card
        # whose expectancy disagrees with the page it links to is worse than no
        # card. One number, one source.
        _exp = _dd = _wr = _closed = None
        try:
            import urllib.request as _u
            _st = json.loads(_u.urlopen(
                "https://news.askakshay.com/api/stats", timeout=20).read())
            _h = _st.get("headline") or {}
            _exp, _wr = _h.get("expectancy_r"), _h.get("win_rate")
            _closed, _dd = _h.get("trades"), _h.get("max_drawdown_r")
        except Exception as _e:                               # noqa: BLE001
            print(f"[generate] ⚠️  share card figures unavailable: {_e}")
        ok2 = og_card.render_share(
            str(out_dir / "share.png"), _exp, _closed, _wr, _dd,
            f"as of {now.strftime('%d %b %Y')}",
        )
        print(f"[generate] {'✅' if ok2 else '⚠️ '} share card"
              f"{f' ({_exp:+.3f}R over {_closed})' if _exp is not None else ' (no figures)'}")
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

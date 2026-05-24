"""
obsidian_sync.py — Push trading signals & exits to Obsidian Brain 2.0 daily notes.

Flow: Railway bot → GitHub REST API → obsidian-brain private repo
      → obsidian-git plugin on local machine pulls automatically

Usage:
    from obsidian_sync import write_signals_to_obsidian, write_exit_to_obsidian

Env vars (set on Railway):
    GITHUB_TOKEN           — same PAT used for trading-dashboard pushes
    OBSIDIAN_GITHUB_REPO   — default: caakshayk1-boop/obsidian-brain
"""

import os
import base64
import logging
import requests
from datetime import datetime, timezone, timedelta

IST_TZ = timezone(timedelta(hours=5, minutes=30))

OBSIDIAN_REPO  = os.environ.get("OBSIDIAN_GITHUB_REPO", "caakshayk1-boop/obsidian-brain")
_GH_API_BASE   = f"https://api.github.com/repos/{OBSIDIAN_REPO}/contents"
_SIGNALS_SECTION = "## 📈 Trading Signals"
_SIGNALS_ANCHOR  = "<!-- nifty200-bot-signals -->"


def _gh_headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN not set — cannot sync to Obsidian")
    return {
        "Authorization": f"token {token}",
        "Accept":        "application/vnd.github.v3+json",
    }


def _gh_get_file(path: str) -> tuple[str, str | None]:
    """
    Fetch file content and SHA from GitHub.
    Returns (content_str, sha) or ("", None) if file doesn't exist.
    """
    url = f"{_GH_API_BASE}/{path}"
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=10)
        if r.status_code == 200:
            data    = r.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            sha     = data.get("sha")
            return content, sha
        elif r.status_code == 404:
            return "", None
        else:
            logging.warning(f"obsidian_sync GET {path}: {r.status_code} {r.text[:120]}")
            return "", None
    except Exception as e:
        logging.warning(f"obsidian_sync GET {path}: {e}")
        return "", None


def _gh_put_file(path: str, content: str, message: str, sha: str | None = None) -> bool:
    """Write file to GitHub. Creates or updates depending on sha."""
    url  = f"{_GH_API_BASE}/{path}"
    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode(),
        "branch":  "main",
    }
    if sha:
        body["sha"] = sha
    try:
        r = requests.put(url, headers=_gh_headers(), json=body, timeout=20)
        if r.status_code in (200, 201):
            return True
        logging.warning(f"obsidian_sync PUT {path}: {r.status_code} {r.text[:200]}")
        return False
    except Exception as e:
        logging.warning(f"obsidian_sync PUT {path}: {e}")
        return False


# ── Daily note helpers ─────────────────────────────────────────────────────────

def _today() -> str:
    """YYYY-MM-DD in IST."""
    return datetime.now(IST_TZ).strftime("%Y-%m-%d")


def _today_note_path() -> str:
    return f"02-DAILY/{_today()}.md"


def _now_ts() -> str:
    return datetime.now(IST_TZ).strftime("%d %b %Y, %I:%M %p IST")


def _minimal_daily_note(date_str: str) -> str:
    """Skeleton daily note when none exists yet."""
    from datetime import date
    dt = date.fromisoformat(date_str)
    dow = dt.strftime("%A")
    return (
        f"# {date_str} · {dow}\n\n"
        f"**Today's single most important output:**\n\n"
        f"---\n\n"
        f"{_SIGNALS_SECTION}\n\n"
        f"{_SIGNALS_ANCHOR}\n"
    )


def _ensure_signals_section(content: str) -> str:
    """
    Guarantee the signals section + anchor exist in the note.
    If note is empty (new file), creates skeleton.
    """
    if not content:
        return _minimal_daily_note(_today())

    if _SIGNALS_SECTION not in content:
        content = content.rstrip("\n") + f"\n\n---\n\n{_SIGNALS_SECTION}\n\n{_SIGNALS_ANCHOR}\n"
    elif _SIGNALS_ANCHOR not in content:
        # Section exists but missing anchor — insert after section header
        content = content.replace(
            _SIGNALS_SECTION,
            f"{_SIGNALS_SECTION}\n\n{_SIGNALS_ANCHOR}"
        )
    return content


def _insert_after_anchor(content: str, block: str) -> str:
    """Insert block just after the anchor comment."""
    return content.replace(
        _SIGNALS_ANCHOR,
        f"{_SIGNALS_ANCHOR}\n{block}"
    )


# ── Public API ─────────────────────────────────────────────────────────────────

def write_signals_to_obsidian(signals: list) -> bool:
    """
    Append A/A+ signals to today's Obsidian daily note.
    Called by _run_swing_scan() after scan_all() finds signals.

    signals: list of signal dicts (same schema as DB rows)
    Returns True if GitHub write succeeded.
    """
    if not signals:
        return False

    # Filter A/A+ only
    top = [s for s in signals if int(s.get("score", 0)) >= 65]
    if not top:
        return False

    path          = _today_note_path()
    content, sha  = _gh_get_file(path)
    content       = _ensure_signals_section(content)
    ts            = _now_ts()
    count         = len(top)

    # Build block
    lines = [f"\n### 🔔 Swing Signals — {ts} ({count} picks)\n"]
    for s in top:
        score   = int(s.get("score", 0))
        conv    = "A+" if score >= 80 else "A"
        action  = str(s.get("action", "BUY")).upper()
        arrow   = "📈" if action == "BUY" else "📉"
        sym     = s.get("symbol", "")
        entry   = s.get("price") or s.get("entry") or 0
        sl      = s.get("sl2") or s.get("sl") or 0
        t1      = s.get("t1") or s.get("target1") or 0
        t2      = s.get("t2") or s.get("target2") or 0
        rr      = s.get("rr2") or s.get("rr") or 0
        stype   = s.get("setup_type") or s.get("signal_type") or "Swing"
        lines.append(
            f"- {arrow} **{sym}** ({action}) · Score {score} [{conv}] "
            f"· Entry ₹{entry} · SL ₹{sl} · T1 ₹{t1} · T2 ₹{t2} · RR {rr}x · _{stype}_"
        )
    lines.append("")  # trailing newline in block

    block   = "\n".join(lines)
    content = _insert_after_anchor(content, block)
    ok      = _gh_put_file(path, content, f"signals: {count} A/A+ picks {_today()} [skip ci]", sha)
    if ok:
        logging.info(f"obsidian_sync: {count} signals → {path}")
    return ok


def write_exit_to_obsidian(sym: str, event_type: str,
                            entry: float, exit_price: float,
                            pnl_pct: float) -> bool:
    """
    Append exit event (SL_HIT / T1_HIT / T2_HIT) to today's daily note.
    Called by _monitor_positions() on each exit event.

    event_type: "SL_HIT" | "T1_HIT" | "T2_HIT"
    """
    path          = _today_note_path()
    content, sha  = _gh_get_file(path)
    content       = _ensure_signals_section(content)
    ts            = _now_ts()

    emoji_map = {
        "SL_HIT": "🔴",
        "T1_HIT": "🟡",
        "T2_HIT": "🟢",
    }
    label_map = {
        "SL_HIT": "SL HIT",
        "T1_HIT": "T1 HIT",
        "T2_HIT": "T2 HIT",
    }
    emoji = emoji_map.get(event_type, "⚪")
    label = label_map.get(event_type, event_type)

    sign    = "+" if pnl_pct > 0 else ""
    block   = (
        f"\n### {emoji} Exit — {sym} {label} ({ts})\n"
        f"- Entry ₹{entry:.2f} → Exit ₹{exit_price:.2f} · P&L: **{sign}{pnl_pct:.2f}%**\n"
    )
    content = _insert_after_anchor(content, block)
    ok      = _gh_put_file(path, content, f"exit: {sym} {label} {_today()} [skip ci]", sha)
    if ok:
        logging.info(f"obsidian_sync: {sym} {label} → {path}")
    return ok


def write_cf_signals_to_obsidian(alerts: list) -> bool:
    """
    Append CF (Commodity/Forex) signals to today's daily note.
    Called by _scan_commodity_forex() after finding alerts.
    """
    if not alerts:
        return False

    path          = _today_note_path()
    content, sha  = _gh_get_file(path)
    content       = _ensure_signals_section(content)
    ts            = _now_ts()
    count         = len(alerts)

    lines = [f"\n### 🌍 CF Signals — {ts} ({count} setups)\n"]
    for a in alerts:
        arrow = "📈" if a.get("bias") == "BUY" else "📉"
        vt    = " 🔥" if a.get("vol_surge") else ""
        lines.append(
            f"- {arrow} **{a['name']}** ({a['bias']}){vt} "
            f"· Entry `{a['price']:.4f}` · SL `{a['sl']:.4f}` "
            f"· T2 `{a['t2']:.4f}` · RR {a['rr']}x "
            f"· 4H RSI {a.get('rsi_4h', 0):.0f}"
        )
    lines.append("")

    block   = "\n".join(lines)
    content = _insert_after_anchor(content, block)
    ok      = _gh_put_file(path, content, f"cf: {count} signals {_today()} [skip ci]", sha)
    if ok:
        logging.info(f"obsidian_sync: {count} CF signals → {path}")
    return ok


def write_weekly_summary_to_obsidian(stats: dict) -> bool:
    """
    Write weekly P&L summary to 03-WEEKLY/YYYY-WXX.md
    stats keys: total, wins, losses, win_rate, avg_pnl, profit_factor, best, worst
    """
    from datetime import date
    today     = date.today()
    week_num  = today.isocalendar()[1]
    year      = today.isocalendar()[0]
    path      = f"03-WEEKLY/{year}-W{week_num:02d}.md"
    ts        = _now_ts()

    content, sha = _gh_get_file(path)
    if not content:
        content = f"# Week {week_num} · {year}\n\n"

    block = (
        f"\n## 📊 Trading P&L — Updated {ts}\n\n"
        f"| Metric | Value |\n"
        f"|--------|-------|\n"
        f"| Total signals | {stats.get('total', 0)} |\n"
        f"| Win rate | **{stats.get('win_rate', 0)}%** |\n"
        f"| Avg P&L | {stats.get('avg_pnl', 0)}% |\n"
        f"| Profit factor | {stats.get('profit_factor', 0)} |\n"
        f"| Best | +{stats.get('best', 0)}% |\n"
        f"| Worst | {stats.get('worst', 0)}% |\n\n"
    )

    # Replace previous weekly block if it exists, otherwise append
    marker = "## 📊 Trading P&L"
    if marker in content:
        # Remove old block (from marker to next ## or end)
        start = content.index(marker)
        end   = content.find("\n## ", start + 1)
        content = content[:start] + (content[end:] if end != -1 else "")

    content = content.rstrip("\n") + "\n" + block
    ok = _gh_put_file(path, content, f"weekly pnl: W{week_num} update [skip ci]", sha)
    if ok:
        logging.info(f"obsidian_sync: weekly summary → {path}")
    return ok


# ── Morning Brief ──────────────────────────────────────────────────────────────

def write_morning_brief(market: dict, open_signals: list) -> bool:
    """
    Create (or update) today's daily note with a morning brief section.
    Fires at 8:00 AM IST Mon–Fri from the scheduler.

    market keys: nifty, usdinr, gold, crude, nifty_chg, gold_chg, crude_chg
    open_signals: list of OPEN signal dicts from DB
    """
    path         = _today_note_path()
    content, sha = _gh_get_file(path)
    ts           = _now_ts()

    # If note doesn't exist yet, create from full morning template
    if not content:
        from datetime import date
        dt  = date.fromisoformat(_today())
        dow = dt.strftime("%A")
        content = (
            f"# {_today()} · {dow}\n\n"
            f"**Today's single most important output:**\n\n"
            f"---\n\n"
        )

    # Build brief block
    n_chg   = market.get("nifty_chg", 0)
    g_chg   = market.get("gold_chg",  0)
    c_chg   = market.get("crude_chg", 0)
    u_chg   = market.get("usdinr_chg", 0)
    n_arrow = "▲" if n_chg >= 0 else "▼"
    g_arrow = "▲" if g_chg >= 0 else "▼"
    c_arrow = "▲" if c_chg >= 0 else "▼"

    open_a  = [s for s in open_signals if int(s.get("score", 0)) >= 65]
    sig_lines = ""
    if open_a:
        for s in open_a[:5]:
            sym   = s.get("symbol", "")
            entry = s.get("entry") or s.get("price") or 0
            sl    = s.get("sl") or s.get("sl2") or 0
            t2    = s.get("target2") or s.get("t2") or 0
            sig_lines += f"- **{sym}** · Entry ₹{entry} · SL ₹{sl} · T2 ₹{t2}\n"
    else:
        sig_lines = "_No open A/A+ positions_\n"

    brief = (
        f"\n## ☀️ Morning Brief — {ts}\n\n"
        f"### 📊 Markets\n"
        f"| Index | Price | Change |\n"
        f"|-------|-------|--------|\n"
        f"| Nifty 50 | {market.get('nifty', '—')} | {n_arrow} {abs(n_chg):.2f}% |\n"
        f"| USD/INR | {market.get('usdinr', '—')} | {'▲' if u_chg >= 0 else '▼'} {abs(u_chg):.2f}% |\n"
        f"| Gold ($/oz) | {market.get('gold', '—')} | {g_arrow} {abs(g_chg):.2f}% |\n"
        f"| Crude ($/bbl) | {market.get('crude', '—')} | {c_arrow} {abs(c_chg):.2f}% |\n\n"
        f"### 🎯 Open Positions ({len(open_a)})\n"
        f"{sig_lines}\n"
        f"### 📅 Today's Scans\n"
        f"- 09:25 AM — Swing A/A+ scan\n"
        f"- 10:00 AM — CF scan (Gold/Crude/USDINR)\n"
        f"- 11:42 AM — Swing midday scan\n"
        f"- 14:00 PM — Magic screener + CF scan\n"
        f"- 16:32 PM — EOD swing scan\n\n"
        f"### 🇦🇪 Dubai Priority\n"
        f"- [ ] Check for new FP&A roles (LinkedIn / Naukrigulf)\n"
        f"- [ ] Follow up on applications > 7 days old\n\n"
        f"---\n\n"
        f"{_SIGNALS_SECTION}\n\n"
        f"{_SIGNALS_ANCHOR}\n"
    )

    # If signals section already exists, inject brief BEFORE it
    if _SIGNALS_SECTION in content:
        content = content.replace(
            f"\n{_SIGNALS_SECTION}",
            f"{brief}"
        )
    else:
        content = content.rstrip("\n") + "\n" + brief

    ok = _gh_put_file(path, content, f"morning brief: {_today()} [skip ci]", sha)
    if ok:
        logging.info(f"obsidian_sync: morning brief → {path}")
    return ok


# ── Content Calendar ───────────────────────────────────────────────────────────

# @askakshayfinance content pillars — rotate weekly
_CONTENT_PILLARS = [
    ("💰 Finance Tip",      "FP&A / CA insight — one number, one truth"),
    ("📈 Trade Setup",      "Week's best swing setup — entry, SL, target"),
    ("🇦🇪 Dubai Journey",  "Progress update — job hunt, target, timeline"),
    ("🧠 Mindset",          "Discipline / consistency / brutalist truth"),
    ("📊 Market Read",      "Weekly market bias — Nifty + sector view"),
]

_CAPTION_HOOKS = {
    "Finance Tip":    "The number that changed how I think about {topic}.",
    "Trade Setup":    "Setup I'm watching this week. Entry. SL. Target. Nothing else.",
    "Dubai Journey":  "Week {n} of the Dubai plan. Here's where I stand.",
    "Mindset":        "The one thing separating ₹1L/month from ₹10L/month.",
    "Market Read":    "Nifty bias this week: {bias}. Here's why.",
}


def write_content_calendar(week_offset: int = 0) -> bool:
    """
    Write/update weekly content calendar to 04-CONTENT/YYYY-WXX-calendar.md
    Posts Mon–Fri, one per day. Each slot has:
      - Pillar + topic
      - Caption hook
      - Checklist: draft → score → post → engage
    Fires every Monday 7:00 AM IST from scheduler.
    """
    from datetime import date, timedelta as td
    today    = date.today() + td(weeks=week_offset)
    year, wk, _ = today.isocalendar()
    # Find Monday of this week
    monday  = today - td(days=today.weekday())
    path    = f"04-CONTENT/{year}-W{wk:02d}-calendar.md"
    ts      = _now_ts()

    content, sha = _gh_get_file(path)
    if content:
        return True   # already exists for this week — don't overwrite

    days   = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    lines  = [
        f"# Content Calendar — W{wk} · {year}\n",
        f"_Generated {ts} · @askakshayfinance_\n\n",
        f"**Goal:** 5 posts this week · Run score_caption.py before every post · Reply within 1hr of posting\n\n",
        f"---\n",
    ]

    for i, day in enumerate(days):
        post_date = monday + td(days=i)
        pillar, desc = _CONTENT_PILLARS[i % len(_CONTENT_PILLARS)]
        hook_key = pillar.split(" ", 1)[1].strip() if " " in pillar else pillar
        hook = _CAPTION_HOOKS.get(hook_key, "One truth. No fluff.")

        lines.append(
            f"\n## {pillar} — {day} {post_date.strftime('%d %b')}\n\n"
            f"**Theme:** {desc}\n\n"
            f"**Caption hook:** _{hook}_\n\n"
            f"**Topic:** \n\n"
            f"**Key stat/number:** \n\n"
            f"**CTA:** Save this. Share if it hit.\n\n"
            f"- [ ] Draft caption\n"
            f"- [ ] Run `score_caption.py` (target ≥ 75)\n"
            f"- [ ] Create visual (Canva / carousel engine)\n"
            f"- [ ] Post at 7:30 PM IST\n"
            f"- [ ] Reply to comments within 1hr\n\n"
            f"---\n"
        )

    lines.append(
        f"\n## 📊 Week Stats (fill after Sunday)\n\n"
        f"| Metric | Value |\n"
        f"|--------|-------|\n"
        f"| Posts published | |\n"
        f"| Avg reach | |\n"
        f"| Avg engagement | |\n"
        f"| New followers | |\n"
        f"| Best post | |\n"
    )

    full = "\n".join(lines)
    ok   = _gh_put_file(path, full, f"content: W{wk} calendar [skip ci]", sha)
    if ok:
        logging.info(f"obsidian_sync: content calendar → {path}")
    return ok

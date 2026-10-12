#!/usr/bin/env python3
"""engine_alerts.py — Telegram alerts for the four alpha engines. Each thing is announced exactly once.

Reads https://alpha.askakshay.com/engines.json, the same file the Engines page renders, so the phone and the page
cannot disagree. It announces:
  NEW      a signal that has just been issued (one message per signal, the plan as issued)
  UPDATE   a tracked trade that entered, reached a target, was stopped, or hit its time limit

DISCIPLINE
  * Only engines whose own record is LEANING POSITIVE or EDGE SHOWN send alerts. Benchmark confirmation was tested as a
    filter and did not improve any engine, so it is reported in the message but never withholds one. An engine whose tests say it
    does not beat random stocks does not get to buzz a phone; it stays visible on the site, labelled.
  * State (data/engine_alert_state.json) is committed by the workflow. It is the whole idempotency mechanism:
    a signal id or an event is written down only after Telegram accepted the message.
  * One trade per stock is enforced upstream (the registry); this file never has to deduplicate.
  * Volume is capped per run, and the remainder is summarised rather than dropped silently.
  * Nothing here says what to do with money. It states a plan and where the plan stands.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
import time
import urllib.request
from datetime import datetime, timezone

import requests

log = logging.getLogger("engine_alerts")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT = pathlib.Path(__file__).resolve().parent
STATE = ROOT / "data" / "engine_alert_state.json"
ALPHA = os.environ.get("ALPHA_URL", "https://alpha.askakshay.com")
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY = bool(os.environ.get("ALERT_DRY_RUN"))
MAX_PER_RUN = int(os.environ.get("ENGINE_ALERT_MAX", "10"))
ALERT_STATUSES = set((os.environ.get("ENGINE_ALERT_STATUSES") or "LEANING POSITIVE,EDGE SHOWN").split(","))
MAX_FEED_AGE_DAYS = int(os.environ.get("ENGINE_ALERT_MAX_AGE", "4"))
_MD = str.maketrans({c: "\\" + c for c in "_*[]()~`>#+-=|{}.!"})


def md(t) -> str:
    return str(t if t is not None else "").translate(_MD)


def rupees(v) -> str:
    return f"₹{v:,.2f}" if isinstance(v, (int, float)) else "—"


def _get(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "engine-alerts/1.0", **headers})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def fetch_feed() -> dict:
    """The newest of two copies: the site's, and the alpha repo's (committed every night by the engines job, so it is
    current even when the site's own deploy is blocked by a missing Cloudflare token). The repo is private, hence GH_PAT."""
    feeds = []
    pat = os.environ.get("ALPHA_REPO_TOKEN", "")
    if pat:
        try:
            feeds.append(_get("https://api.github.com/repos/caakshayk1-boop/alpha/contents/public/engines.json",
                              {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github.raw+json"}))
        except Exception as e:
            log.warning("repo copy of engines.json unreadable (%s)", e)
    try:
        feeds.append(_get(f"{ALPHA}/engines.json", {}))
    except Exception as e:
        log.warning("site copy of engines.json unreadable (%s)", e)
    if not feeds:
        raise RuntimeError("engines.json unreadable from both the repo and the site")
    return max(feeds, key=lambda f: f.get("data_through", ""))


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception as e:                                           # noqa: BLE001
            log.warning("state unreadable (%s): starting empty", e)
    return {"signals": {}}


def new_message(s: dict, st: dict) -> str:
    t = s["targets"]
    r = s["r_multiples"]
    b = s.get("benchmark") or {}
    bench = ""
    if b:
        bits = ["uptrend" if b.get("uptrend") else "not in an uptrend", "leading the Nifty" if b.get("leads_nifty") else "lagging the Nifty"]
        vs = b.get("stock_vs_benchmark_pct")
        if isinstance(vs, (int, float)):
            bits.append(f"stock {vs:+.1f}% vs it over 3 months")
        bench = f"\nBenchmark {md(b.get('name', '').replace(' (alpha group index)', ''))}: {md(', '.join(bits))}"
    h = ((st.get("trades") or {}).get("holdout") or {}).get("expectancy_r")
    holdout = f"; holdout trades {h:+.2f}R each after costs, on data it never saw" if isinstance(h, (int, float)) else ""
    also = f"\nAlso selected by {md(', '.join(s['also_names']))}" if s.get("also_names") else ""
    return (
        f"🔔 *{md(s['engine_name'])} · {md(s['sym'])}* — {md(s['setup'])} \\({md(s['horizon'])}\\)\n"
        f"{md(s['name'])}\n"
        f"Signal close {md(rupees(s['ref_price']))} \\(research plan enters at the next open\\) · Stop {md(rupees(s['sl']))} \\(−{md(s['risk_pct'])}%\\)\n"
        f"T1 {md(rupees(t[0]))} \\({md(r[0])}R\\) · T2 {md(rupees(t[1]))} \\({md(r[1])}R\\) · T3 {md(rupees(t[2]))} \\({md(r[2])}R\\)\n"
        f"Time limit {md(s['max_hold_days'])} sessions"
        f"{bench}{also}\n"
        f"Engine: ranking {md(str(st.get('ranking_status', st.get('status', ''))).lower())}{md(holdout)}\n"
        f"{md(ALPHA)}/company/?s={md(s['sym'])}\n"
        f"_A plan, not advice\\. The stop is the plan; the targets are the hope\\._"
    )


def event_message(s: dict, ev: dict) -> str | None:
    k, p, sym = ev["kind"], ev["price"], md(s["sym"])
    link = f"\n{md(ALPHA)}/engines/"
    if k == "entry":
        return f"▶️ *{sym}* entered at {md(rupees(p))} \\({md(s['engine_name'])}\\)\\. Stop {md(rupees(s['sl']))}{link}"
    if k in ("t1", "t2", "t3"):
        n = k[1]
        move = {"1": f"stop on the rest moves to entry {md(rupees(s.get('entry')))}", "2": f"stop on the rest moves to T1 {md(rupees(s['targets'][0]))}", "3": "all three targets reached"}[n]
        return f"✅ *{sym}* reached T{n} at {md(rupees(p))} — {move}{link}"
    if k == "stop":
        return f"🛑 *{sym}* stopped out at {md(rupees(p))} \\(about −1R\\)\\. The plan did what it was built to do{link}"
    if k in ("stop_after_t1", "stop_after_t2"):
        return f"🔒 *{sym}* stopped at {md(rupees(p))} after reaching T{k[-1]} — the trade closes with the gains already taken{link}"
    if k == "time":
        return f"⏱ *{sym}* hit its time limit — the remainder exits at the close, {md(rupees(p))}{link}"
    return None


def post(text: str) -> bool:
    if DRY:
        print("─" * 60 + "\n" + text + "\n")
        return True
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": text, "parse_mode": "MarkdownV2", "disable_web_page_preview": True}, timeout=20)
        if r.status_code == 200:
            return True
        log.error("telegram %s: %s", r.status_code, r.text[:300])        # the body names the offending byte offset
    except Exception as e:                                                 # noqa: BLE001
        log.error("telegram send failed: %s", e)
    return False


def plan(feed: dict, state: dict) -> list[tuple[str, str, str]]:
    """[(kind, key, message)] in the order they should be sent. Pure: nothing is sent or written here."""
    out: list[tuple[str, str, str]] = []
    engines = feed.get("engines", {})
    seen = state.setdefault("signals", {})
    for s in feed.get("signals", []):
        st = engines.get(s["engine"], {})
        if not st.get("alertable") or st.get("status") not in ALERT_STATUSES:
            continue            # the site decides which engines may buzz a phone (holdout R above its bar); this only double-checks
        rec = seen.get(s["id"])
        if rec is None:
            if s["status"] in ("pending", "open"):
                out.append(("new", s["id"], new_message(s, st)))
            else:
                seen[s["id"]] = {"events": [f"{e['kind']}:{e['date']}" for e in s.get("events", [])], "silent": True}   # closed before we ever saw it: record, say nothing
            continue
        done = set(rec.get("events", []))
        for ev in s.get("events", []):
            key = f"{ev['kind']}:{ev['date']}"
            if key in done:
                continue
            msg = event_message(s, ev)
            if msg:
                out.append(("event", f"{s['id']}|{key}", msg))
    return out


def main() -> int:
    if not (TG_TOKEN and TG_CHAT) and not DRY:
        log.info("no telegram credentials — nothing to send")
        return 0
    feed = fetch_feed()
    age = (datetime.now(timezone.utc).date() - datetime.fromisoformat(feed["data_through"]).date()).days
    if age > MAX_FEED_AGE_DAYS:
        log.error("engines.json is built on data %d days old — refusing to announce from it", age)
        return 1
    state = load_state()
    todo = plan(feed, state)
    log.info("%d message(s) planned (%d signals in feed)", len(todo), len(feed.get("signals", [])))
    by_id = {s["id"]: s for s in feed.get("signals", [])}
    sent = 0
    for kind, key, msg in todo[:MAX_PER_RUN]:
        if not post(msg):
            log.error("send failed — stopping so the unsent half is announced next run")
            break
        if kind == "new":
            # events that already exist when a signal is first announced are described by the message itself
            state["signals"][key] = {"events": [f"{e['kind']}:{e['date']}" for e in by_id[key].get("events", [])], "sent": ["new"]}
        else:
            sid, ev = key.split("|", 1)
            state["signals"].setdefault(sid, {"events": [], "sent": []})["events"].append(ev)
        sent += 1
        time.sleep(0.6)
    if len(todo) > MAX_PER_RUN:
        post(f"_\\+{len(todo) - MAX_PER_RUN} more engine updates — {md(ALPHA)}/engines/_")
    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not DRY:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1, sort_keys=True))
    log.info("sent %d", sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())

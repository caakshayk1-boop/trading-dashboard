#!/usr/bin/env python3
"""stock_alerts.py — the three state CHANGES nothing else on this estate pushes.

Akshay: "telegram rounded alerts."

────────────────────────────────────────────────────────────────────────────
WHY A FOURTH ALERT PATH, WHEN TWO TELEGRAM SENDS ALREADY EXIST
────────────────────────────────────────────────────────────────────────────
The 12:30 and 17:00 briefs report STATE: what the market did, what is open,
what the engines hold. The weekly reads report a curated selection. Neither
reports a CHANGE, and a change is the only thing worth interrupting someone
for.

Three changes now exist on this estate and are visible only if you happen to
reload the right page on the right day:

  1. THE SCREEN CHANGED ITS MIND about a company. A name going WATCH → BUY,
     or BUY → AVOID, is the single highest-information event the screen
     produces, and until now it produced it silently.

  2. A SEASONAL WINDOW OPENED. seasonality.py knows which names have risen in
     ≥ SEASON_HIT% of a calendar month across eleven years. On the first
     trading day of that month, that fact is actionable context; on the 20th
     it is a fact about the past.

  3. THE MARKET STAGE MOVED. barometer.py answers Akshay's actual question —
     "even at bad, when can we start investing" — with a four-step stage from
     "Nothing on sale" to "Genuinely cheap". A move between those steps
     happens a handful of times a decade and is exactly what he asked to be
     told about.

────────────────────────────────────────────────────────────────────────────
WHAT THIS DELIBERATELY REFUSES TO DO
────────────────────────────────────────────────────────────────────────────
NO TRADE INSTRUCTIONS. Every message here says what CHANGED, never what to
do. No engine on this estate has cleared 30 closed trades at t ≥ 2, and an
alert that reads like an order would be the loudest surface making a claim the
ledger does not support.

IT NEVER REPEATS ITSELF. State lives in data/alert_state.json, keyed on the
thing that changed, and a re-run of the same day sends nothing. A scheduled
job that can double-send is worse than one that occasionally misses — the
watchdog loop on this estate once sent 90 messages in 30 hours off one stale
assertion, and that is the failure this guards against.

IT REFUSES A STALE SCREEN. A verdict change is a difference between two
snapshots. If the newer snapshot is yesterday's, the "change" being reported
is a day old and may already have reversed — so the job exits rather than
send it.

VOLUME IS CAPPED. MAX_VERDICTS per run, ranked by how much the call moved.
989 names across a rough day can flip dozens at once; a phone that buzzes
forty times teaches its owner to ignore it, which costs more than the missed
thirty-ninth name.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import time
from datetime import date, datetime, timezone

import requests

log = logging.getLogger("stock_alerts")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT = pathlib.Path(__file__).resolve().parent
SCREEN = ROOT / "docs" / "screen.json"
SEASON = ROOT / "docs" / "seasonality.json"
BARO = ROOT / "docs" / "barometer.json"
STATE = ROOT / "data" / "alert_state.json"

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
TG_MAX = 3800                 # hard cap is 4096 and Telegram REJECTS, never truncates
_MD = str.maketrans({c: "\\" + c for c in "_*[]()~`>#+-=|{}.!"})

SITE = os.environ.get("SIGNAL_URL", "https://signal.askakshay.com")
MAX_VERDICTS = int(os.environ.get("ALERT_MAX_VERDICTS", "8"))
MAX_SEASONAL = int(os.environ.get("ALERT_MAX_SEASONAL", "6"))
SEASON_HIT = int(os.environ.get("ALERT_SEASON_HIT", "73"))
MAX_SCREEN_AGE_DAYS = int(os.environ.get("ALERT_MAX_SCREEN_AGE", "3"))
DRY = bool(os.environ.get("ALERT_DRY_RUN"))

# How far a call moved, so the cap keeps the biggest moves rather than the
# alphabetically luckiest. BUY→AVOID is a reversal; WAIT→WATCH is a shrug.
RANK = {"AVOID": 0, "WATCH": 1, "WAIT": 2, "BUY": 3}


def _md(t) -> str:
    return str(t if t is not None else "").translate(_MD)


def _load(p: pathlib.Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text()) or {}
    except Exception as e:                                       # noqa: BLE001
        log.warning(f"{p.name} unreadable ({e})")
        return {}


def _tg_post(text: str) -> bool:
    if DRY:
        print("─" * 60 + "\n" + text + "\n")
        return True
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "MarkdownV2",
                  "disable_web_page_preview": True},
            timeout=20)
        if r.status_code == 200:
            return True
        # Log the BODY. A 400 from Telegram names the offending byte offset,
        # and the status code alone has never once been enough to fix one.
        log.error(f"telegram {r.status_code}: {r.text[:400]}")
    except Exception as e:                                       # noqa: BLE001
        log.error(f"telegram send failed: {e}")
    return False


def _send(blocks: list[str], head: str) -> bool:
    """Chunked on the cap, never truncated."""
    if not blocks:
        return True
    msgs, cur = [], head
    for b in blocks:
        if len(cur) + len(b) > TG_MAX:
            msgs.append(cur)
            cur = ""
        cur += "\n" + b
    msgs.append(cur)
    ok = True
    for i, m in enumerate(msgs):
        if not _tg_post(m):
            ok = False
        if i + 1 < len(msgs):
            time.sleep(1)
    return ok


# ── 1. THE SCREEN CHANGED ITS MIND ──────────────────────────────────────────
def verdict_changes(rows: list[dict], seen: dict) -> tuple[list[str], dict]:
    """Compared against the LAST CALL THIS JOB SAW, not against yesterday.

    Keying on the previous alert rather than on a dated snapshot means a day
    the job does not run is absorbed rather than lost: the next run still
    reports WATCH → BUY even if the flip happened on the missed day. A
    date-keyed diff would have silently dropped it.
    """
    prev = seen.get("verdicts") or {}
    now, changes = {}, []
    for r in rows:
        sym = r.get("sym")
        c = str(((r.get("vd") or {}).get("c") or "")).upper()
        if not sym or c not in RANK:
            continue
        now[sym] = c
        was = prev.get(sym)
        if was and was != c and was in RANK:
            changes.append((abs(RANK[c] - RANK[was]), r, was, c))

    # First run has no previous call for anything, so EVERY name would read as
    # a change. That is 989 alerts, and it is a bootstrap artefact rather than
    # news — so the first run records the baseline and says nothing.
    if not prev:
        log.info(f"no previous verdicts on file — recording {len(now)} as the baseline, sending nothing")
        return [], now

    changes.sort(key=lambda x: (-x[0], x[1].get("sym") or ""))
    kept, dropped = changes[:MAX_VERDICTS], max(0, len(changes) - MAX_VERDICTS)

    blocks = []
    for _d, r, was, c in kept:
        sym = r.get("sym")
        arrow = "📈" if RANK[c] > RANK[was] else "📉"
        why = ((r.get("vd") or {}).get("o") or "").strip().rstrip(".")
        px = r.get("price")
        blocks.append(
            f"{arrow} *{_md(sym)}* {_md(was)} → *{_md(c)}*\n"
            f"{_md(r.get('name') or '')}"
            + (f" · ₹{_md(f'{px:,.2f}')}" if isinstance(px, (int, float)) else "") + "\n"
            + (f"_{_md(why)}_\n" if why else "")
            + f"{_md(SITE)}/stock/{_md(sym)}\n")
    if dropped:
        blocks.append(f"_\\+{_md(dropped)} more changed today — the full list is on the screen\\._\n")
    return blocks, now


# ── 2. A SEASONAL WINDOW OPENED ─────────────────────────────────────────────
def seasonal_window(rows: list[dict], seas: dict, seen: dict) -> list[str]:
    """Once per calendar month, on the first run of that month.

    THE STRENGTH OF THIS ALERT IS ALSO ITS WEAKNESS and the message says so: a
    hit rate is a record of the past with no causal claim on the future. It is
    gated on the screen's own verdict as well, so what goes out is "a name the
    screen already likes, entering a month it has historically done well in" —
    two independent readings agreeing, rather than a calendar on its own.
    """
    month = date.today().strftime("%Y-%m")
    if seen.get("season_month") == month:
        return []
    stocks = seas.get("stocks") or {}
    if not stocks:
        return []

    idx = {r.get("sym"): r for r in rows if r.get("sym")}
    m = date.today().month
    picks = []
    for sym, d in stocks.items():
        arr = (d.get("m") or [None] * 12)
        rec = arr[m - 1] if len(arr) >= m else None
        if not rec or rec[0] < SEASON_HIT:
            continue
        r = idx.get(sym)
        if not r:
            continue
        if str(((r.get("vd") or {}).get("c") or "")).upper() not in ("BUY", "WATCH"):
            continue
        picks.append((rec[0], rec[1], rec[2], r))
    picks.sort(key=lambda x: (-x[0], -x[1]))
    picks = picks[:MAX_SEASONAL]
    if not picks:
        return []

    name = date.today().strftime("%B")
    blocks = [f"🗓 *{_md(name)} — the seasonal record*\n"
              f"_Names the screen already rates that have risen in ≥{_md(SEASON_HIT)}% "
              f"of the last eleven {_md(name)}s\\. Historical, not predictive\\._\n"]
    for hit, med, n, r in picks:
        blocks.append(
            f"*{_md(r.get('sym'))}* — rose in {_md(hit)}% of {_md(n)}, "
            f"median {_md(f'{med:+.1f}')}%\n"
            f"{_md(r.get('name') or '')} · screen says {_md((r.get('vd') or {}).get('c'))}\n"
            f"{_md(SITE)}/stock/{_md(r.get('sym'))}\n")
    return blocks


# ── 3. THE MARKET STAGE MOVED ───────────────────────────────────────────────
def stage_change(baro: dict, seen: dict) -> list[str]:
    """Akshay's own question, answered only when the answer changes.

    "Even at bad, when can we start investing — COVID was bad but whoever
    invested at those lows became rich." The stage is the four-step reading
    that moves AGAINST the score by design, and a move between its steps is a
    handful-of-times-a-decade event. Reporting it every day would turn the one
    genuinely rare alert on this estate into wallpaper.
    """
    today = baro.get("today") or {}
    st = today.get("stage") or {}
    k = st.get("k")
    if not k or seen.get("stage") == k:
        return []
    was = seen.get("stage")
    if not was:                       # bootstrap: record, do not announce
        return []

    score = today.get("score")
    band = (today.get("band") or {}).get("t") or ""
    dd = today.get("drawdown_pct")
    above = today.get("above_200dma_pct")

    # The measured outcome for this stage, if a year has passed since enough
    # readings of it. This is the whole point of barometer.py's history, and
    # the alert is the first place a reader would want it.
    out = ((baro.get("outcomes") or {}).get(k) or {})
    m12 = out.get("m12") or {}
    record = ""
    if m12.get("n"):
        # Built OUTSIDE the f-string. A nested quote inside an f-string
        # expression is a syntax error before Python 3.12, and CI runs 3.11 —
        # exactly how the 6 AM brief's signal recap shipped broken and never
        # once sent. The escape has to live in a plain variable.
        avg = _md("%+.1f" % m12["avg"])
        record = ("\n_Measured: after %s past readings of this stage the index was %s%% "
                  "a year later, higher %s%% of the time\\._"
                  % (_md(m12["n"]), avg, _md(m12["hit"])))
    else:
        record = ("\n_No measured outcome yet — this job has not been recording long enough "
                  "for a twelve\\-month window to close\\._")

    return [f"🌊 *The market stage moved*\n"
            f"{_md(was)} → *{_md(st.get('t') or k)}*\n"
            f"Barometer {_md(score)}/100 \\({_md(band)}\\)"
            + (f" · index {_md(f'{dd:.1f}')}% off its high" if isinstance(dd, (int, float)) else "")
            + (f" · {_md(f'{above:.0f}')}% of names above their 200\\-day" if isinstance(above, (int, float)) else "")
            + record + f"\n{_md(SITE)}/markets\n"]


def main() -> int:
    if not (TG_TOKEN and TG_CHAT) and not DRY:
        log.info("no telegram credentials — nothing to send")
        return 0

    screen = _load(SCREEN)
    rows = screen.get("rows") or []
    if not rows:
        log.error("docs/screen.json has no rows — refusing to diff against nothing")
        return 1

    # A VERDICT CHANGE IS A DIFFERENCE BETWEEN SNAPSHOTS. If the newer snapshot
    # is days old, the "change" being announced may already have reversed, so
    # the job exits rather than send a stale one.
    built = str(screen.get("built_on") or screen.get("generated_at") or "")[:10]
    try:
        age = (date.today() - date.fromisoformat(built)).days
    except Exception:                                            # noqa: BLE001
        log.error(f"screen.json carries no readable date ({built!r}) — refusing to send")
        return 1
    if age > MAX_SCREEN_AGE_DAYS:
        log.error(f"screen is {age} days old — refusing to announce changes from it")
        return 1
    log.info(f"screen built {built} ({age}d old), {len(rows)} names")

    seen = _load(STATE)
    sent_any = False

    v_blocks, v_now = verdict_changes(rows, seen)
    if v_blocks:
        head = (f"🔁 *The screen changed its mind*\n"
                f"_{_md(built)} · what moved between calls, not what to do about it\\._\n")
        if _send(v_blocks, head):
            sent_any = True
            log.info(f"verdict changes: sent {len(v_blocks)} block(s)")

    s_blocks = seasonal_window(rows, _load(SEASON), seen)
    if s_blocks:
        if _send(s_blocks[1:], s_blocks[0]):
            sent_any = True
            seen["season_month"] = date.today().strftime("%Y-%m")
            log.info(f"seasonal window: sent {len(s_blocks) - 1} name(s)")

    baro = _load(BARO)
    b_blocks = stage_change(baro, seen)
    if b_blocks:
        if _send(b_blocks[1:], b_blocks[0]):
            sent_any = True
            log.info("stage change: sent")

    # STATE IS WRITTEN WHETHER OR NOT ANYTHING WENT OUT. The bootstrap runs
    # deliberately send nothing and MUST still record their baseline, or every
    # subsequent run would repeat the same bootstrap forever.
    seen["verdicts"] = v_now or seen.get("verdicts") or {}
    cur_stage = ((baro.get("today") or {}).get("stage") or {}).get("k")
    if cur_stage:
        seen["stage"] = cur_stage
    seen["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(seen, indent=2), encoding="utf-8")

    log.info("nothing to announce" if not sent_any else "done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

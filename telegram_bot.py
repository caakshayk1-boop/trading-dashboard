"""
Telegram bot — signal delivery + commands (/start /active /performance /mute /stats)
PDF spec: Part 9
"""
import requests, os, logging, time
from datetime import datetime
import pytz

import tracker

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Fall back to config.py when running locally
if not TELEGRAM_TOKEN:
    from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

IST = pytz.timezone("Asia/Kolkata")
_last_scan_time = None
_last_scan_count = 0

# Direction lock: symbol → {"action": "BUY"/"SELL", "date": "YYYY-MM-DD"}
# Once a BUY is sent for symbol X, block any SELL on X until next trading day.
_direction_lock: dict = {}


def _check_direction_lock(symbol: str, action: str) -> bool:
    """
    Returns True (block) if we already sent the opposite direction today.
    Clears stale locks from previous trading days automatically.
    """
    today = datetime.now(IST).strftime("%Y-%m-%d")
    lock = _direction_lock.get(symbol)
    if lock:
        if lock["date"] != today:
            del _direction_lock[symbol]  # stale — new day, clear it
            return False
        if lock["action"] != action:
            return True  # opposite signal today → block
    return False


def _set_direction_lock(symbol: str, action: str) -> None:
    today = datetime.now(IST).strftime("%Y-%m-%d")
    _direction_lock[symbol] = {"action": action, "date": today}


# Telegram throttles a chat at roughly one message per second and ~20/minute for
# groups. Nothing paced sends before, which was survivable only because the scan
# paths were capped at 5 signals; an uncapped scan sends enough messages to trip
# it. Pace proactively, and obey Retry-After when it happens anyway.
_MIN_SEND_GAP_S = 1.1
_MAX_SEND_ATTEMPTS = 3
_RETRY_AFTER_CAP_S = 30
_last_send_ts = 0.0


def _throttle():
    global _last_send_ts
    gap = time.monotonic() - _last_send_ts
    if gap < _MIN_SEND_GAP_S:
        time.sleep(_MIN_SEND_GAP_S - gap)
    _last_send_ts = time.monotonic()


def _post(text, chat_id=None):
    """Send one Telegram message. Returns True only if Telegram accepted it.

    Retries on 429 (honouring Retry-After) and on transport errors. A 400 from
    unbalanced Markdown is retried once as plain text — an unbalanced * or _
    anywhere makes Telegram reject the WHOLE alert, so the signal would be lost.
    """
    if not TELEGRAM_TOKEN:
        logging.error("_post: TELEGRAM_TOKEN is not set — message not sent")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id or TELEGRAM_CHAT_ID,
        "text":    text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    for attempt in range(1, _MAX_SEND_ATTEMPTS + 1):
        try:
            _throttle()
            r = requests.post(url, data=payload, timeout=15)
            if r.ok:
                return True

            if r.status_code == 429:
                wait = _RETRY_AFTER_CAP_S
                try:
                    wait = int(r.json().get("parameters", {}).get("retry_after", 1))
                except Exception:
                    pass
                wait = min(max(wait, 1), _RETRY_AFTER_CAP_S)
                if attempt < _MAX_SEND_ATTEMPTS:
                    logging.warning(f"_post: rate limited — retrying in {wait}s "
                                    f"(attempt {attempt}/{_MAX_SEND_ATTEMPTS})")
                    time.sleep(wait)
                    continue
                logging.error("_post: rate limited — attempts exhausted, message lost")
                return False

            if r.status_code == 400 and "parse" in r.text.lower() and "parse_mode" in payload:
                logging.warning(
                    f"_post: Markdown rejected ({r.text[:120]}) — resending as plain text")
                payload.pop("parse_mode", None)
                continue

            # 5xx is transient; 4xx other than the two above will not improve.
            if r.status_code >= 500 and attempt < _MAX_SEND_ATTEMPTS:
                logging.warning(f"_post: Telegram {r.status_code} — retrying "
                                f"(attempt {attempt}/{_MAX_SEND_ATTEMPTS})")
                time.sleep(2 * attempt)
                continue
            logging.error(f"_post: Telegram API error {r.status_code} — {r.text[:200]}")
            return False

        except requests.RequestException as e:
            if attempt < _MAX_SEND_ATTEMPTS:
                logging.warning(f"_post: transport error ({e}) — retrying "
                                f"(attempt {attempt}/{_MAX_SEND_ATTEMPTS})")
                time.sleep(2 * attempt)
                continue
            logging.error(f"_post: Exception sending Telegram message — {e}")
            return False
        except Exception as e:
            logging.error(f"_post: Exception sending Telegram message — {e}")
            return False
    return False


def _conviction(score: int) -> str:
    if score >= 80: return "A+"
    if score >= 65: return "A"
    if score >= 50: return "B"
    return "C"


def _setup_emoji(setup_type):
    return {"pullback": "🔄", "breakout": "🚀", "divergence": "📐"}.get(setup_type, "📊")


def send_alert(signal):
    """Send individual signal alert — only fires for A/A+ (score ≥ 65)."""
    global _last_scan_count
    score = int(signal.get("score", 0))
    if score < 65:
        return False  # silently skip B/C signals

    sym  = signal["symbol"]
    actn = signal.get("action", "BUY")

    # Direction lock: if we already sent the opposite direction today, skip.
    if _check_direction_lock(sym, actn):
        logging.info(f"send_alert: {sym} {actn} blocked — opposite direction already sent today")
        return False
    _set_direction_lock(sym, actn)

    _last_scan_count += 1
    conv = _conviction(score)
    ts   = datetime.now(IST).strftime("%d %b, %H:%M IST")
    dir_arrow = "📈" if actn == "BUY" else "📉"
    rr2  = signal.get("rr2", 0)
    adx  = signal.get("adx_val", 0)
    vol  = signal.get("vol_ratio", 0)
    # Volume quality label
    vol_label = (
        "🔥 SURGING" if vol >= 2.5 else
        "✅ NORMAL"  if vol >= 0.6 else
        "⚠️ THIN"
    )
    msg = (
        f"{dir_arrow} *{signal['symbol']}* | {conv} Conviction | SWING {actn}\n"
        f"Score: `{score}/100` · ADX `{adx}` · Vol `{vol}x` {vol_label}\n\n"
        f"*Entry:* ₹{signal['price']}\n"
        f"*SL:*    ₹{signal['sl2']}\n\n"
        f"*T1:* ₹{signal['t1']}  `({signal.get('rr1',0)}R)`\n"
        f"*T2:* ₹{signal['t2']}  `({rr2}R)`\n"
        f"*T3:* ₹{signal['t3']}\n\n"
        f"Qty: *{signal.get('qty',0)} shares* | TF: Swing\n"
        f"_{ts}_\n"
        f"_Dhruvedge scanner · Not SEBI advice_"
    )
    return _post(msg)


def send_top_picks(signals, top_n=5):
    """Send ranked summary — only A/A+ signals included. Returns True if sent."""
    top = [s for s in signals if int(s.get("score", 0)) >= 65][:top_n]
    if not top:
        return False
    ts = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
    lines = [f"🏆 *Top {len(top)} Swing Picks — {ts}*\n"]
    for i, s in enumerate(top, 1):
        conv = _conviction(int(s.get("score", 0)))
        e = _setup_emoji(s.get("setup_type", ""))
        lines.append(
            f"{i}. {e} *{s['symbol']}* [{conv}] — Score {s['score']}/100\n"
            f"   ₹{s['price']} | SL ₹{s['sl2']} | T2 ₹{s['t2']} | RR {s.get('rr2',0)}x"
        )
    lines.append("\n_Dhruvedge · Entry only on A/A+ · Not SEBI advice_")
    return _post("\n".join(lines))


def send_summary(signals):
    global _last_scan_time, _last_scan_count
    _last_scan_time  = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
    qualifying = [s for s in signals if int(s.get("score", 0)) >= 65]
    _last_scan_count = len(qualifying)

    if not qualifying:
        _post(
            f"✅ *Scan complete — {_last_scan_time}*\n"
            f"No A/A+ signals today. All cash held.\n"
            f"_(Score < 65 or ADX < 20 or RR < 1.5 or Vol < 2.5x — filters not met)_"
        )
        return
    lines = [f"📊 *{len(qualifying)} A/A+ Signal(s) — {_last_scan_time}*\n"]
    for s in qualifying:
        conv = _conviction(int(s.get("score", 0)))
        e = _setup_emoji(s.get("setup_type", ""))
        lines.append(
            f"{e} *{s['symbol']}* [{conv}] | Score {s['score']} | ₹{s['price']} "
            f"| RR {s.get('rr2',0)}x | ADX {s.get('adx_val',0)}"
        )
    _post("\n".join(lines))


_SCREEN_HELP = (
    "🔎 *Stock Screen* — NSE Total Market, ~750 names\n\n"
    "`/screen` — top 10 by composite\n"
    "`/screen TCS` — one company in full\n"
    "`/screen quality` — high ROCE, real growth\n"
    "`/screen cheap` — good and cheap vs its own industry\n"
    "`/screen growth` — revenue CAGR above 20%\n"
    "`/screen breakout` — breaking 20/50/52w highs\n"
    "`/screen rs` — outperforming the Nifty\n"
    "`/screen oversold` — RSI under 35\n"
    "`/screen debtfree` — D/E at or below 0.1\n"
    "`/screen micro` — Microcap 250 only\n\n"
    "*Rank for a horizon* (same data, different weights):\n"
    "`/screen investor` · `/screen positional` · `/screen swing`\n\n"
    "Rebuilt weekly from published annual statements. "
    "A ranking of public data, not advice."
)

# Preset name → predicate over a payload row. Mirrors the browser presets in
# static/app.js on purpose: the same word must select the same companies in both
# places, or the bot and the site disagree about what "cheap" means.
_SCREEN_PRESETS = {
    "quality":   lambda r: (r.get("q") or 0) >= 65 and (r.get("rev_cagr") or 0) >= 10,
    "cheap":     lambda r: (r.get("q") or 0) >= 60 and (r.get("pe_pctile") or 0) >= 60,
    "growth":    lambda r: (r.get("rev_cagr") or 0) >= 20,
    "breakout":  lambda r: bool(r.get("brk20") or r.get("brk50") or r.get("brk52w")),
    "rs":        lambda r: (r.get("rs1y") or 0) >= 15,
    "oversold":  lambda r: r.get("rsi") is not None and r["rsi"] < 35,
    "debtfree":  lambda r: r.get("de") is not None and 0 <= r["de"] <= 0.1,
    "micro":     lambda r: r.get("tier") == "micro",
    "small":     lambda r: r.get("tier") == "small",
    "mid":       lambda r: r.get("tier") == "mid",
    "large":     lambda r: r.get("tier") == "large",
}

# Ranking modes, same weight sets as the site. `/screen investor` answers a
# different question from `/screen swing` over the SAME components — see
# MODES in stock_screen.py.
_SCREEN_MODES = {"investor": "m_inv", "positional": "m_pos", "swing": "m_swing"}


def _screen_payload():
    """The cached screen, or None. Never builds — see newspaper.get_stock_screen.

    Reading the cache rather than building is the whole point: the build is ~29
    minutes of sequential Yahoo fetches and a Telegram command has to answer in
    seconds.
    """
    try:
        import newspaper
        d = newspaper.get_stock_screen()
        return d if d.get("rows") else None
    except Exception as e:
        logging.warning(f"/screen: cache read failed — {e}")
        return None


def _n(v, suf=""):
    return "—" if v is None else f"{v}{suf}"


def _screen_one(r: dict) -> str:
    """One company, in full. The mobile version of the detail sheet."""
    L = [f"*{r['sym']}* — {r.get('name','')}",
         f"_{r.get('ind','')}_" + (f" · {r['tier']}cap" if r.get("tier") else "")]
    L.append("")
    L.append(f"₹{_n(r.get('price'))}   1Y {_n(r.get('r1y'),'%')}   "
             f"RSI {_n(r.get('rsi'))}")
    L.append(f"*Composite {_n(r.get('comp'))}*  ·  Q {_n(r.get('q'))} "
             f"G {_n(r.get('g'))} V {_n(r.get('v'))} T {_n(r.get('tech'))}")
    L.append("")
    L.append(f"ROCE {_n(r.get('roce'),'%')} (3Y med {_n(r.get('roce_med'),'%')})")
    L.append(f"ROE {_n(r.get('roe'),'%')}   EBIT margin {_n(r.get('ebit_margin'),'%')}")
    L.append(f"Revenue CAGR {_n(r.get('rev_cagr'),'%')}   "
             f"EBITDA CAGR {_n(r.get('ebitda_cagr'),'%')}")
    L.append(f"D/E {_n(r.get('de'))}   PE {_n(r.get('pe'))}"
             + (f"   cheaper than {r['pe_pctile']:.0f}% of peers"
                if r.get("pe_pctile") is not None else ""))
    tags = (r.get("setup") or {}).get("tags") or []
    if tags:
        L += ["", "· " + " · ".join(tags[:4])]
    sw = r.get("swot") or {}
    for key, head in (("s", "Strengths"), ("w", "Weaknesses"), ("t", "Risks")):
        items = sw.get(key) or []
        if not items:
            continue
        L += ["", f"*{head}*"]
        # The evidence line travels with the claim here too — it is the whole
        # reason these lines are trustworthy.
        L += [f"• {i['t']}\n  _{i.get('k','')}_" for i in items[:3]]
    if r.get("ai_view"):
        L += ["", "*Analyst view* (AI, from the figures above)", f"_{r['ai_view']}_"]
    if not r.get("has_stmts"):
        L += ["", "⚠ No annual statements published for this symbol — "
                  "price-only, and it carries no composite."]
    return "\n".join(L)


def _screen_reply(text: str) -> str:
    """`/screen`, `/screen SYMBOL`, `/screen PRESET`."""
    d = _screen_payload()
    if not d:
        return ("The screen has not been built yet. It runs weekly — "
                "Sunday 02:30 IST.")
    rows = d["rows"]
    parts = text.split()
    arg = parts[1].lower() if len(parts) > 1 else ""
    built = d.get("built_on") or "?"
    uni = d.get("universe") or "NSE"

    if not arg:
        top = [r for r in rows if r.get("comp") is not None][:10]
        L = [f"🔎 *Top 10 by composite* — {uni}",
             f"_{len(rows)} companies · built {built}_", ""]
        for i, r in enumerate(top, 1):
            L.append(f"{i}. *{r['sym']}* {_n(r.get('comp'))} · "
                     f"ROCE {_n(r.get('roce'),'%')} · "
                     f"rev {_n(r.get('rev_cagr'),'%')} · PE {_n(r.get('pe'))}")
        L += ["", "`/screen SYMBOL` for one company · `/screenhelp` for presets"]
        return "\n".join(L)

    if arg in _SCREEN_MODES:
        key = _SCREEN_MODES[arg]
        sel = [r for r in rows if r.get(key) is not None]
        sel.sort(key=lambda r: -r[key])
        L = [f"🔎 *Top 10 ranked for {arg}*", f"_{uni} · built {built}_", ""]
        for i, r in enumerate(sel[:10], 1):
            L.append(f"{i}. *{r['sym']}* {_n(r.get(key))} "
                     f"_(balanced {_n(r.get('comp'))})_ · "
                     f"ROCE {_n(r.get('roce'),'%')} · RSI {_n(r.get('rsi'))}")
        L += ["", "_Same components, different weights. A name can rank high "
                  "here and low elsewhere — that is the point._"]
        return "\n".join(L)

    if arg in _SCREEN_PRESETS:
        sel = [r for r in rows if _SCREEN_PRESETS[arg](r)]
        sel.sort(key=lambda r: (r.get("comp") is None, -(r.get("comp") or 0)))
        if not sel:
            return f"Nothing in the screen matches *{arg}* this week."
        L = [f"🔎 *{arg}* — {len(sel)} of {len(rows)}", f"_built {built}_", ""]
        for i, r in enumerate(sel[:12], 1):
            L.append(f"{i}. *{r['sym']}* {_n(r.get('comp'))} · "
                     f"ROCE {_n(r.get('roce'),'%')} · rev {_n(r.get('rev_cagr'),'%')}")
        if len(sel) > 12:
            L.append(f"_…and {len(sel)-12} more_")
        return "\n".join(L)

    want = arg.upper()
    hit = next((r for r in rows if r["sym"] == want), None)
    if not hit:
        near = [r["sym"] for r in rows if want in r["sym"]
                or want in (r.get("name", "").upper())][:6]
        return (f"*{want}* is not in the screen."
                + (f"\nDid you mean: {', '.join(near)}?" if near else
                   f"\nIt covers {uni} — try `/screen` for the top 10."))
    return _screen_one(hit)


def handle_command(text, chat_id):
    """Handle bot commands — called by polling loop or webhook."""
    from tracker import get_active_signals, get_performance, mute_asset
    text = text.strip()

    if text.startswith("/vercel"):
        from vercel_signals import get_vercel_report
        parts = text.split()
        sub = parts[1].lower() if len(parts) > 1 else "incremental"
        if sub == "all":
            msg = get_vercel_report("all")
        elif sub == "ohl":
            msg = get_vercel_report("ohl")
        else:
            msg = get_vercel_report("incremental")
        _post(msg, chat_id)
        return

    if text.startswith("/screenhelp"):
        _post(_SCREEN_HELP, chat_id)

    elif text.startswith("/start"):
        _post(
            "👋 *Nifty 500 Swing Scanner*\n\n"
            "Commands:\n"
            "/active — all open signals\n"
            "/performance — win rate & stats\n"
            "/mute SYMBOL — stop alerts for a stock\n"
            "/stats — scanner health\n\n"
            "*TradeFlow Pro (Vercel):*\n"
            "/vercel — new signals since last check\n"
            "/vercel all — everything generated today\n"
            "/vercel ohl — OHL/OLL setups only\n\n"
            "Scans run: 9:30 AM | 2:00 PM | 5:30 PM IST (Mon–Fri)",
            chat_id
        )

    elif text.startswith("/active"):
        df = get_active_signals()
        if df.empty:
            _post("No active signals right now.", chat_id)
        else:
            lines = [f"📋 *Active Signals ({len(df)})*\n"]
            for _, r in df.iterrows():
                lines.append(f"• *{r['symbol']}* | Entry ₹{r['entry']} | T1 ₹{r['target1']} | SL ₹{r['sl2']}")
            _post("\n".join(lines), chat_id)

    elif text.startswith("/performance"):
        p = get_performance()
        if not p:
            _post("No closed trades yet.", chat_id)
        else:
            _post(
                f"📈 *Performance*\n\n"
                f"Total signals: {p['total']}\n"
                f"Win rate: *{p['win_rate']}%*\n"
                f"Avg P&L: {p['avg_pnl']}%\n"
                f"Avg R: {p['avg_r']}\n"
                f"Profit factor: {p['profit_factor']}\n"
                f"Best: +{p['best']}% | Worst: {p['worst']}%",
                chat_id
            )

    elif text.startswith("/screen"):
        _post(_screen_reply(text), chat_id)

    elif text.startswith("/mute"):
        parts = text.split()
        if len(parts) >= 2:
            sym = parts[1].upper()
            mute_asset(sym)
            _post(f"🔇 Muted alerts for *{sym}*. Use /unmute {sym} to re-enable.", chat_id)
        else:
            _post("Usage: /mute SYMBOL (e.g. /mute RELIANCE)", chat_id)

    elif text.startswith("/confirm") or text.startswith("/skip"):
        # The only path that can mark a fill real.
        #
        # This repo has no broker integration — upstox_provider is read-only
        # market data — so every fill the scanner records is inferred from a
        # price touch, not from an executed order. Without an explicit human
        # confirmation the terminal would report deployed capital and P&L
        # against money that was never committed.
        parts = text.split()
        cmd = "SKIPPED" if text.startswith("/skip") else "CONFIRMED"
        if len(parts) < 2:
            _post(
                "Usage:\n"
                "`/confirm SYMBOL [qty] [price]` — you placed this order\n"
                "`/skip SYMBOL` — you passed on it\n\n"
                "Without qty/price the ticket's own numbers are used.\n"
                "Only confirmed trades count toward deployed capital.",
                chat_id
            )
        else:
            sym = parts[1].upper()
            qty = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
            try:
                price = float(parts[3]) if len(parts) > 3 else None
            except ValueError:
                price = None
            ok, msg = tracker.set_fill_type(sym, cmd, qty, price)
            _post(msg if ok else f"⚠️ {msg}", chat_id)

    elif text.startswith("/book"):
        _post(tracker.book_summary(), chat_id)

    elif text.startswith("/stats"):
        _post(
            f"⚙️ *Scanner Health*\n\n"
            f"Last scan: {_last_scan_time or 'Not run yet'}\n"
            f"Signals found: {_last_scan_count}\n"
            f"Data: yfinance (Yahoo Finance)\n"
            f"Schedule: 9:30 AM | 2:00 PM | 5:30 PM IST",
            chat_id
        )


def test_connection():
    _post(
        "✅ *Nifty 500 Swing Scanner* — Bot connected!\n"
        "Scans: 9:30 AM | 2:00 PM | 5:30 PM IST (Mon–Fri)\n"
        "Regime filter active (ADX ≥ 20 required) 🎯"
    )


def start_command_polling():
    """Long-poll Telegram for commands — run in background thread."""
    import threading, time

    def _poll():
        offset = 0
        while True:
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
                r = requests.get(url, params={"offset": offset, "timeout": 30}, timeout=35)
                if r.ok:
                    for upd in r.json().get("result", []):
                        offset = upd["update_id"] + 1
                        msg = upd.get("message", {})
                        txt = msg.get("text", "")
                        cid = msg.get("chat", {}).get("id")
                        if txt.startswith("/") and cid:
                            handle_command(txt, str(cid))
            except Exception:
                pass
            time.sleep(1)

    t = threading.Thread(target=_poll, daemon=True)
    t.start()


if __name__ == "__main__":
    import time
    print("🤖 Bot starting — polling for commands...")
    test_connection()
    start_command_polling()
    print("✅ Polling active. Send /start in Telegram.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("Bot stopped.")

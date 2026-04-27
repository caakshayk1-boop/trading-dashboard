"""
Telegram bot — signal delivery + commands (/start /active /performance /mute /stats)
PDF spec: Part 9
"""
import requests, os
from datetime import datetime
import pytz

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Fall back to config.py when running locally
if not TELEGRAM_TOKEN:
    from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

IST = pytz.timezone("Asia/Kolkata")
_last_scan_time = None
_last_scan_count = 0


def _post(text, chat_id=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": chat_id or TELEGRAM_CHAT_ID,
            "text":    text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=10)
        return r.ok
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


def _stars(score):
    if score >= 90: return "⭐⭐⭐⭐⭐"
    if score >= 80: return "⭐⭐⭐⭐"
    if score >= 70: return "⭐⭐⭐"
    return "⭐⭐"


def _setup_emoji(setup_type):
    return {"pullback": "🔄", "breakout": "🚀", "divergence": "📐"}.get(setup_type, "📊")


def send_alert(signal):
    global _last_scan_count
    _last_scan_count += 1
    e  = _setup_emoji(signal.get("setup_type", ""))
    ts = datetime.now(IST).strftime("%d %b, %H:%M IST")
    msg = (
        f"📊 *SWING SIGNAL* | Score: *{signal['score']}/100* {_stars(signal['score'])}\n\n"
        f"{e} *{signal['symbol']}* — *{signal['action']}*\n"
        f"Setup: _{signal.get('setup_type','').replace('_',' ').title()}_\n\n"
        f"Entry Zone: ₹{signal['price']}\n"
        f"SL1 (tight): ₹{signal.get('sl1', signal['price'])}\n"
        f"SL2 (max):   ₹{signal['sl2']}\n\n"
        f"*Targets:*\n"
        f"T1: ₹{signal['target1']}  ({signal['rr1']}R)\n"
        f"T2: ₹{signal['target2']}  ({signal['rr2']}R)\n"
        f"T3: ₹{signal['target3']}\n\n"
        f"Position: *{signal['qty']} shares* (1% risk)\n"
        f"RSI: {signal['rsi']} | ADX: {signal['adx']} | Vol: {signal['vol_ratio']}x\n\n"
        f"*Why:*\n"
        + "\n".join(f"• {r}" for r in signal.get("reasons", "").split(", "))
        + f"\n\n📈 [Chart]({signal.get('tv_link','')})\n"
        f"_{ts}_"
    )
    return _post(msg)


def send_top_picks(signals, top_n=5):
    top = signals[:top_n]
    if not top:
        return
    lines = [f"🏆 *Top {len(top)} Swing Picks*\n"]
    for i, s in enumerate(top, 1):
        e = _setup_emoji(s.get("setup_type", ""))
        lines.append(
            f"{i}. {e} *{s['symbol']}* — Score {s['score']}/100\n"
            f"   ₹{s['price']} | SL ₹{s['sl2']} | T2 ₹{s['target2']} | {_stars(s['score'])}"
        )
    _post("\n".join(lines))


def send_summary(signals):
    global _last_scan_time, _last_scan_count
    _last_scan_time  = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
    _last_scan_count = len(signals)

    if not signals:
        _post("✅ Scan complete — no qualifying signals today.\n_(ADX regime filter or score threshold not met)_")
        return
    lines = [f"📊 *Scan Summary — {len(signals)} signal(s)*\n"]
    for s in signals:
        e = _setup_emoji(s.get("setup_type", ""))
        lines.append(f"{e} *{s['symbol']}* | Score {s['score']} | ₹{s['price']} | {s.get('setup_type','')}")
    _post("\n".join(lines))


def handle_command(text, chat_id):
    """Handle bot commands — called by polling loop or webhook."""
    from tracker import get_active_signals, get_performance, mute_asset
    text = text.strip()

    if text.startswith("/start"):
        _post(
            "👋 *Nifty 500 Swing Scanner*\n\n"
            "Commands:\n"
            "/active — all open signals\n"
            "/performance — win rate & stats\n"
            "/mute SYMBOL — stop alerts for a stock\n"
            "/stats — scanner health\n\n"
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

    elif text.startswith("/mute"):
        parts = text.split()
        if len(parts) >= 2:
            sym = parts[1].upper()
            mute_asset(sym)
            _post(f"🔇 Muted alerts for *{sym}*. Use /unmute {sym} to re-enable.", chat_id)
        else:
            _post("Usage: /mute SYMBOL (e.g. /mute RELIANCE)", chat_id)

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

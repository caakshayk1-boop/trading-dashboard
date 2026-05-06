"""
claude_bot.py — AI-powered Telegram bot
Type anything in your Telegram chat:
  Brief: NSE:COALINDIA    → 1-page stock brief
  Trade: NSE:RELIANCE     → swing trade setup
  Scan                    → run stock scanner
  Help                    → show all commands
  /active /performance /stats etc still work
"""
import os, sys, time, logging, threading
import requests
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from telegram_bot import _post, TELEGRAM_TOKEN, handle_command

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
if not GROQ_API_KEY:
    try:
        from config import GROQ_API_KEY
    except (ImportError, AttributeError):
        pass

if not GROQ_API_KEY:
    logging.error("GROQ_API_KEY not set. Get free key at console.groq.com → add to config.py")
    sys.exit(1)


# ── Stock data ────────────────────────────────────────────────────────────────

def _fetch(ticker: str):
    sym = ticker.upper().replace("NSE:", "").strip() + ".NS"
    try:
        s = yf.Ticker(sym)
        info = s.info
        hist = s.history(period="1y")
        if hist.empty:
            return None
        close     = hist["Close"].dropna()
        if close.empty:
            return None
        cmp       = round(float(close.iloc[-1]), 2)
        high_52w  = round(float(hist["High"].max()), 2)
        low_52w   = round(float(hist["Low"].min()), 2)
        ret_1y    = round((close.iloc[-1] / close.iloc[0] - 1) * 100, 1)
        vs_high   = round((cmp / high_52w - 1) * 100, 1)
        mcap      = (info.get("marketCap") or 0) / 1e7
        return {
            "symbol":    ticker.upper().replace("NSE:", "").strip(),
            "name":      info.get("longName", ""),
            "cmp":       cmp,
            "high_52w":  high_52w,
            "low_52w":   low_52w,
            "ret_1y":    ret_1y,
            "vs_high":   vs_high,
            "mcap_cr":   round(mcap, 0),
            "pe":        round(info.get("trailingPE") or 0, 1),
            "pb":        round(info.get("priceToBook") or 0, 1),
            "div_yield": round((info.get("dividendYield") or 0) * 100, 2),
            "revenue":   round((info.get("totalRevenue") or 0) / 1e7, 0),
            "pat":       round((info.get("netIncomeToCommon") or 0) / 1e7, 0),
            "sector":    info.get("sector", ""),
        }
    except Exception as e:
        logging.warning(f"yfinance error for {sym}: {e}")
        return None


# ── Claude calls ──────────────────────────────────────────────────────────────

def _ask(prompt: str, max_tokens=900) -> str:
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def do_brief(ticker: str) -> str:
    d = _fetch(ticker)
    sym = ticker.upper().replace("NSE:", "").strip()

    live = (
        f"CMP: ₹{d['cmp']}\n"
        f"52W High: ₹{d['high_52w']} | 52W Low: ₹{d['low_52w']}\n"
        f"vs 52W High: {d['vs_high']}% | 1Y Return: {d['ret_1y']}%\n"
        f"Mkt Cap: ₹{d['mcap_cr']:.0f} Cr\n"
        f"P/E: {d['pe']}x | P/B: {d['pb']}x | Div Yield: {d['div_yield']}%\n"
        f"Revenue (TTM): ₹{d['revenue']:.0f} Cr | PAT (TTM): ₹{d['pat']:.0f} Cr\n"
        f"Sector: {d['sector']}"
    ) if d else f"Symbol: {sym} — use your knowledge for financials"

    return _ask(f"""You are a CA and equity analyst (@askakshayfinance).
Write a 1-page stock brief for *{sym}* (NSE) formatted for Telegram.

Live data:
{live}

Use EXACTLY this format (Telegram Markdown — *bold*, _italic_, no headers with #):

📊 *{sym} — NSE Tear Sheet*
_{d['name'] if d else sym}_

━━━━━━━━━━━━━━━━
💰 *PRICE*
CMP: ₹[x]
52W H/L: ₹[high] / ₹[low]
vs 52W High: [x%]  |  1Y Return: [x%]

━━━━━━━━━━━━━━━━
🏦 *VALUATION*
Mkt Cap: ₹[x] Cr  ([Large/Mid/Small] Cap)
P/E: [x]x  |  P/B: [x]x  |  Div: [x]%

━━━━━━━━━━━━━━━━
📈 *FINANCIALS*
Revenue: ₹[x] Cr
PAT: ₹[x] Cr  |  Margin: [x]%
Revenue CAGR 3Y: ~[x]%

━━━━━━━━━━━━━━━━
🔑 *BUSINESS / MOAT*
• [point 1]
• [point 2]
• [point 3]

━━━━━━━━━━━━━━━━
⚠️ *KEY RISKS*
• [risk 1]
• [risk 2]

━━━━━━━━━━━━━━━━
🎯 *VERDICT*
[1-2 line view — specific, no fluff]

_Brief by @askakshayfinance_

Rules: specific numbers always, no vague statements, max 450 words.""")


def do_trade(ticker: str) -> str:
    d = _fetch(ticker)
    sym = ticker.upper().replace("NSE:", "").strip()

    live = (
        f"CMP: ₹{d['cmp']}\n"
        f"52W High: ₹{d['high_52w']} | 52W Low: ₹{d['low_52w']}\n"
        f"1Y Return: {d['ret_1y']}%"
    ) if d else f"Symbol: {sym}"

    return _ask(f"""You are a technical analyst. Generate a swing trade setup for {sym} (NSE).

Live data:
{live}

Format exactly (Telegram Markdown):

📊 *{sym} — Swing Trade Setup*

*Action:* BUY / SELL / AVOID
*Entry Zone:* ₹[x] – ₹[y]
*Stop Loss:* ₹[x]  _(tight)_  |  ₹[y]  _(wide)_
*Target 1:* ₹[x]  `(1.5R)`
*Target 2:* ₹[x]  `(2.5R)`
*Target 3:* ₹[x]  `(4R)`

*Setup:* [Breakout / Pullback / Base breakout / Reversal]
*Timeframe:* Swing (2–6 weeks)
*Risk/Reward:* [x]:1

*Thesis:*
[3 lines — TA rationale, key levels, momentum]

⚠️ *Invalidation:* [specific level + reason]

_@askakshayfinance | Not SEBI registered advice_

Use real levels based on the CMP. Specific numbers only.""", max_tokens=600)


def _send_document(chat_id: str, filename: str, content: str, caption: str = ""):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        requests.post(url, data={"chat_id": chat_id, "caption": caption},
                      files={"document": (filename, content.encode(), "text/html")}, timeout=30)
    except Exception as e:
        _post(f"❌ File send error: {e}", chat_id)


def do_carousel(topic: str, chat_id: str):
    _post(f"🎨 Generating carousel: *{topic}*... ~30 seconds.", chat_id)
    html = _ask(f"""You are a brutalist dark Instagram carousel designer for @askakshayfinance (CA/FP&A professional).

Generate a complete, self-contained HTML file for an 8-slide carousel on: "{topic}"

EXACT SPEC:
- Canvas: 1080×1350px per slide
- Background: #0A0A0A (near black)
- Accent: #FF5F1F (orange) — use sparingly, only for key numbers/alerts
- Text: #FFFFFF (white), #6E6E6E (gray for secondary)
- Fonts (Google Fonts): Oswald 700 (numbers/stats), Playfair Display 900 italic (key insight), Space Grotesk (body), Caveat (handwritten annotations)
- 8 slides formula: Hook (CA confession/shocking stat) → Audit (problem) → Finding 1 → Finding 2 → Finding 3 → Hard Truth → System/Fix → CTA (@askakshayfinance)
- Each slide: ONE insight only. Specific numbers always (₹16,309 not ₹16K).
- Orange = alarm only. Never use for decorative purposes.

HTML structure:
- Single HTML file, all CSS inline
- Slides as divs, each 1080×1350px, displayed vertically (scroll to see all)
- Import Google Fonts in <head>
- Add slide number (1/8, 2/8 etc) bottom right in gray
- Bottom left: @askakshayfinance in small gray text

Return ONLY the complete HTML. No explanation.""", max_tokens=4000)

    slug = topic.lower().replace(" ", "_")[:30]
    filename = f"carousel_{slug}.html"
    _send_document(chat_id, filename, html, f"🎨 Carousel: {topic}")
    _post("✅ Open the HTML file in your browser to view all 8 slides.", chat_id)


def do_scan() -> str:
    try:
        from scanner import scan_tg_momentum
        _post("🔍 Scanning Nifty 500 on 4H... ~90 seconds.")
        signals = scan_tg_momentum()
        if not signals:
            return "✅ Scan done — no signals matching criteria right now."

        lines = [f"📡 *4H Momentum Scan — {len(signals)} signal(s)*\n"
                 f"_RSI bottom↑ + Vol 3x+ + Bullish candle_\n"]
        for s in signals:
            lines.append(
                f"━━━━━━━━━━━━━━━━\n"
                f"📈 *{s['symbol']}* | _{s['pattern']}_\n"
                f"CMP ₹{s['price']} | Vol *{s['vol_ratio']}x* | RSI {s['rsi']} ↑ (low {s['rsi_low']})\n"
                f"SL ₹{s['sl']} | T1 ₹{s['target1']} | T2 ₹{s['target2']} | RR {s['rr']}:1\n"
                f"[Chart]({s['tv_link']})"
            )
        lines.append("\n_@askakshayfinance | Not SEBI advice_")
        _post("\n".join(lines))
        return f"✅ {len(signals)} signal(s) sent."
    except Exception as e:
        return f"❌ Scan error: {e}"


HELP_TEXT = """🤖 *Claude AI Trading Bot*

*Commands:*
`Brief: NSE:TICKER` — 1-page stock report
`Trade: NSE:TICKER` — swing trade setup
`Scan` — Nifty 500 4H momentum scan
`Carousel: [topic]` — 8-slide HTML carousel
`Help` — this message

*Legacy:*
/active /performance /stats

_@askakshayfinance_"""


# ── Command router ────────────────────────────────────────────────────────────

def route(text: str, chat_id: str):
    t = text.strip()
    tl = t.lower()

    # Legacy slash commands
    if t.startswith("/"):
        handle_command(t, chat_id)
        return

    # Help
    if tl in ("help", "?", "hi", "hello"):
        _post(HELP_TEXT, chat_id)
        return

    # Brief
    if tl.startswith("brief"):
        raw = t[5:].strip().lstrip(":").strip()
        if not raw:
            _post("Usage: `Brief: NSE:RELIANCE`", chat_id)
            return
        ticker = raw.upper().replace("NSE:", "").strip()
        _post(f"📊 Fetching brief for *{ticker}*...", chat_id)
        try:
            reply = do_brief(raw)
            _post(reply, chat_id)
        except Exception as e:
            _post(f"❌ Error: {e}", chat_id)
        return

    # Trade
    if tl.startswith("trade"):
        raw = t[5:].strip().lstrip(":").strip()
        if not raw:
            _post("Usage: `Trade: NSE:RELIANCE`", chat_id)
            return
        ticker = raw.upper().replace("NSE:", "").strip()
        _post(f"📊 Building trade setup for *{ticker}*...", chat_id)
        try:
            reply = do_trade(raw)
            _post(reply, chat_id)
        except Exception as e:
            _post(f"❌ Error: {e}", chat_id)
        return

    # Scan
    if tl.startswith("scan"):
        result = do_scan()
        _post(result, chat_id)
        return

    # Carousel
    if tl.startswith("carousel"):
        topic = t[8:].strip().lstrip(":").strip()
        if not topic:
            _post("Usage: `Carousel: 5 tax mistakes salaried people make`", chat_id)
            return
        try:
            do_carousel(topic, chat_id)
        except Exception as e:
            _post(f"❌ Error: {e}", chat_id)
        return

    # Unknown — send help nudge
    _post("Type `Help` to see commands.", chat_id)


# ── Polling loop ──────────────────────────────────────────────────────────────

def run():
    logging.info("Claude Bot started. Polling Telegram...")
    _post("🤖 *Claude AI Bot online*\nType `Help` to see commands.")
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            r = requests.get(url, params={"offset": offset, "timeout": 30}, timeout=35)
            if r.ok:
                for upd in r.json().get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message", {})
                    txt = msg.get("text", "").strip()
                    cid = str(msg.get("chat", {}).get("id", ""))
                    if txt and cid:
                        threading.Thread(target=route, args=(txt, cid), daemon=True).start()
        except Exception as e:
            logging.warning(f"Poll error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    run()

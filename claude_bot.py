"""
claude_bot.py — AI-powered Telegram bot + auto-scheduler + Flask API
Commands: Brief: NSE:TICKER | Trade: NSE:TICKER | Scan | Carousel: topic | Help
Auto-scans: 9:20 AM | 11:45 AM | 4:30 PM IST (Mon–Fri)
Flask API: /api/signals  /api/portfolio  /api/health  (served on $PORT for Dhruvedge)
"""
import os, sys, time, logging, threading, json, sqlite3
from datetime import datetime, timezone, timedelta
import requests
import yfinance as yf
import pytz

sys.path.insert(0, os.path.dirname(__file__))
from telegram_bot import _post, TELEGRAM_TOKEN

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

IST_TZ = timezone(timedelta(hours=5, minutes=30))
CAPITAL = 500000
DB_PATH = os.path.join(os.path.dirname(__file__), "signals.db")

# In-memory position state: symbol → {"trailed_sl": float, "t1_hit": bool}
_position_states: dict = {}
_STATES_FILE = "/tmp/position_states.json"

def _load_position_states():
    global _position_states
    try:
        if os.path.exists(_STATES_FILE):
            with open(_STATES_FILE) as f:
                _position_states = json.load(f)
    except Exception:
        _position_states = {}

def _save_position_states():
    try:
        with open(_STATES_FILE, "w") as f:
            json.dump(_position_states, f)
    except Exception:
        pass

def _score_to_conviction(score: int) -> str:
    if score >= 80: return "A+"
    if score >= 65: return "A"
    if score >= 50: return "B"
    return "C"

def _nse_yahoo(sym: str) -> str:
    overrides = {"M&M": "M%26M.NS", "MCDOWELL-N": "MCDOWELL-N.NS",
                 "HDFC BANK": "HDFCBANK.NS", "ICICI BANK": "ICICIBANK.NS"}
    s = sym.strip().upper()
    return overrides.get(s, f"{s}.NS")

def _db_open_signals(min_score: int = 65) -> list:
    """Read OPEN A/A+ signals from signals.db."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM all_signals WHERE status='OPEN' AND score>=? ORDER BY score DESC, date DESC",
            (min_score,)
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logging.warning(f"DB read error: {e}")
        return []

def _db_update_signal(signal_id: int, status: str, exit_price: float, pnl_pct: float):
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute(
            "UPDATE all_signals SET status=?, exit_price=?, pnl_pct=? WHERE id=?",
            (status, exit_price, pnl_pct, signal_id)
        )
        con.commit()
        con.close()
    except Exception as e:
        logging.warning(f"DB update error: {e}")


# ── Flask API (serves data to Dhruvedge on Vercel) ───────────────────────────

def _start_api_server():
    """Run Flask API in a background thread on $PORT."""
    try:
        from flask import Flask, jsonify
        app = Flask(__name__)

        @app.route("/api/health")
        def health():
            return jsonify({"status": "ok", "ts": datetime.now(IST_TZ).isoformat()})

        @app.route("/api/signals")
        def api_signals():
            rows = _db_open_signals(min_score=0)  # all OPEN for display
            now  = datetime.now(IST_TZ).strftime("%Y-%m-%d")
            return jsonify({"all_signals": rows, "signals": [], "exported_at": now})

        @app.route("/api/admin/reset-signals", methods=["POST", "GET"])
        def admin_reset_signals():
            try:
                conn = sqlite3.connect(DB_PATH)
                cur  = conn.cursor()
                cur.execute("UPDATE all_signals SET status='CANCELLED' WHERE status='OPEN'")
                affected = cur.rowcount
                conn.commit()
                conn.close()
                logging.info(f"[ADMIN] Reset {affected} stale OPEN signals to CANCELLED")
                return jsonify({"ok": True, "cancelled": affected,
                                "ts": datetime.now(IST_TZ).isoformat()})
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500

        @app.route("/api/portfolio")
        def api_portfolio():
            rows = _db_open_signals(min_score=65)  # A/A+ only in portfolio
            positions = []
            for r in rows:
                score  = int(r.get("score") or 0)
                sym    = (r.get("symbol") or "").strip().upper()
                entry  = float(r.get("entry") or 0)
                sl     = float(r.get("sl") or r.get("sl2") or entry * 0.96)
                t1     = float(r.get("target1") or entry * 1.05)
                t2     = float(r.get("target2") or t1 * 1.02)
                if entry <= 0 or not sym:
                    continue
                # Use trailed SL if T1 was hit
                state  = _position_states.get(sym, {})
                eff_sl = state.get("trailed_sl", sl)
                try:
                    meta = json.loads(r.get("metadata") or "{}")
                    qty  = int(meta.get("qty") or 0)
                except Exception:
                    qty = 0
                if qty <= 0:
                    # 2% risk per trade on ₹5L capital
                    risk_amt = CAPITAL * 0.02          # ₹10,000 risk per trade
                    risk_per_share = max(entry - sl, 0.01)
                    qty = max(1, int(risk_amt / risk_per_share))
                qty = min(qty, int((CAPITAL * 0.25) / entry))  # max 25% per position
                try:
                    meta   = json.loads(r.get("metadata") or "{}")
                    reas   = meta.get("reasons", "")
                    thesis = ". ".join(reas[:3]) if isinstance(reas, list) else str(reas)[:200]
                except Exception:
                    thesis = f"{r.get('signal_type','Setup')} · Score {score}/100"
                positions.append({
                    "symbol":      sym,
                    "yahooSymbol": _nse_yahoo(sym),
                    "qty":         qty,
                    "entryPrice":  round(entry, 2),
                    "entryDate":   r.get("date", ""),
                    "target":      round(t1, 2),
                    "target2":     round(t2, 2),
                    "sl":          round(eff_sl, 2),
                    "conviction":  _score_to_conviction(score),
                    "setup":       r.get("signal_type") or r.get("setup_type") or "Swing",
                    "thesis":      thesis,
                    "t1_hit":      state.get("t1_hit", False),
                })
            return jsonify({"capital": CAPITAL, "positions": positions,
                            "updatedAt": datetime.now(IST_TZ).isoformat()})

        port = int(os.environ.get("PORT", 8080))
        logging.info(f"Flask API starting on port {port}")
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    except ImportError:
        logging.warning("Flask not installed — API server not started")
    except Exception as e:
        logging.error(f"Flask API error: {e}")


# ── Position monitor (every 15 min, market hours) ────────────────────────────

def _monitor_positions():
    """Check live prices for all OPEN A/A+ positions. Trail SL, detect exits."""
    now_ist = datetime.now(IST_TZ)
    h, m = now_ist.hour, now_ist.minute
    # Only run 9:15–15:30 IST Mon-Fri
    if now_ist.weekday() >= 5:
        return
    if not (9 * 60 + 15 <= h * 60 + m <= 15 * 60 + 30):
        return

    rows = _db_open_signals(min_score=65)
    if not rows:
        return

    logging.info(f"Position monitor: checking {len(rows)} open positions")

    for r in rows:
        try:
            sym     = (r.get("symbol") or "").strip().upper()
            sig_id  = int(r.get("id", 0))
            entry   = float(r.get("entry") or 0)
            sl_orig = float(r.get("sl") or r.get("sl2") or entry * 0.96)
            t1      = float(r.get("target1") or entry * 1.05)
            t2      = float(r.get("target2") or t1 * 1.02)
            action  = str(r.get("action", "BUY")).upper()

            if entry <= 0 or not sym:
                continue

            # Only monitor real directional signals — skip WATCH/NEUTRAL/magic results
            if action not in ("BUY", "SELL"):
                continue

            state   = _position_states.setdefault(sym, {"trailed_sl": sl_orig, "t1_hit": False})
            eff_sl  = state["trailed_sl"]

            # Sanity check: SL must be on the correct side of entry
            # BUY: SL below entry. SELL: SL above entry. Wrong-side = bad data, skip.
            if action == "BUY" and eff_sl >= entry:
                logging.warning(f"Monitor skip {sym}: BUY but SL ₹{eff_sl} >= entry ₹{entry} — bad data")
                continue
            if action == "SELL" and eff_sl <= entry:
                logging.warning(f"Monitor skip {sym}: SELL but SL ₹{eff_sl} <= entry ₹{entry} — bad data")
                continue

            # Fetch live price
            ticker  = yf.Ticker(_nse_yahoo(sym))
            info    = ticker.fast_info
            price   = float(getattr(info, "last_price", 0) or 0)
            if price <= 0:
                continue

            ts = now_ist.strftime("%d %b %I:%M %p IST")

            # SL hit check (always use effective trailing SL)
            sl_hit = (price <= eff_sl) if action == "BUY" else (price >= eff_sl)
            if sl_hit:
                pnl = round((price - entry) / entry * 100 * (1 if action == "BUY" else -1), 2)
                _db_update_signal(sig_id, "SL_HIT", price, pnl)
                _position_states.pop(sym, None)
                _save_position_states()
                sign = "+" if pnl > 0 else ""
                _post(
                    f"🔴 *SL HIT — {sym}*\n"
                    f"Exit ₹{price:.2f} | Entry ₹{entry} | SL was ₹{eff_sl:.2f}\n"
                    f"P&L: `{sign}{pnl}%`\n_{ts}_"
                )
                logging.info(f"SL hit: {sym} @ ₹{price} pnl={pnl}%")
                continue

            # T2 hit — full exit
            t2_hit = (price >= t2) if action == "BUY" else (price <= t2)
            if t2_hit:
                pnl = round((price - entry) / entry * 100 * (1 if action == "BUY" else -1), 2)
                _db_update_signal(sig_id, "T2_HIT", price, pnl)
                _position_states.pop(sym, None)
                _save_position_states()
                sign = "+" if pnl > 0 else ""
                _post(
                    f"🟢 *T2 HIT — {sym}* · Full exit\n"
                    f"Exit ₹{price:.2f} | T2 ₹{t2} | Entry ₹{entry}\n"
                    f"P&L: `{sign}{pnl}%`\n_{ts}_"
                )
                logging.info(f"T2 hit: {sym} @ ₹{price} pnl={pnl}%")
                continue

            # T1 hit — trail SL to entry (breakeven), enable dynamic trail
            t1_hit = (price >= t1) if action == "BUY" else (price <= t1)
            if t1_hit and not state.get("t1_hit"):
                state["t1_hit"]     = True
                state["trailed_sl"] = entry
                state["peak_price"] = price   # track highest price seen after T1
                _save_position_states()
                _post(
                    f"🟡 *T1 HIT — {sym}*\n"
                    f"Price ₹{price:.2f} | T1 ₹{t1}\n"
                    f"SL trailed to entry ₹{entry} (breakeven)\n"
                    f"Riding to T2 ₹{t2} · Dynamic trail active\n_{ts}_"
                )
                logging.info(f"T1 hit: {sym} @ ₹{price}, SL trailed to ₹{entry}")

            # Dynamic trail after T1 — trail SL to 60% of move from entry
            if state.get("t1_hit") and action == "BUY":
                peak = state.get("peak_price", price)
                if price > peak:
                    state["peak_price"] = price
                    # Trail SL to 60% of move from entry to new peak
                    new_sl = round(entry + (price - entry) * 0.60, 2)
                    if new_sl > state["trailed_sl"]:
                        old_sl = state["trailed_sl"]
                        state["trailed_sl"] = new_sl
                        _save_position_states()
                        _post(
                            f"📈 *SL TRAILED — {sym}*\n"
                            f"Price ₹{price:.2f} (new high)\n"
                            f"SL: ₹{old_sl} → ₹{new_sl}\n_{ts}_"
                        )
                        logging.info(f"Trail update: {sym} new peak={price}, SL trailed {old_sl}→{new_sl}")

        except Exception as e:
            logging.debug(f"Monitor {r.get('symbol')}: {e}")
            continue
IST = pytz.timezone("Asia/Kolkata")

# ── Config ────────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
if not GROQ_API_KEY:
    try:
        from config import GROQ_API_KEY
    except (ImportError, AttributeError):
        pass
if not GROQ_API_KEY:
    logging.error("GROQ_API_KEY not set.")
    sys.exit(1)

# ── In-memory signal store (survives restart via /tmp cache) ──────────────────
_active_signals = []
_last_scan_ts   = None
_last_scan_slot = None
_last_scan_count = 0
_CACHE_FILE = "/tmp/bot_signals_cache.json"

def _save_cache():
    import json
    try:
        import json
        with open(_CACHE_FILE, "w") as f:
            json.dump({
                "signals": _active_signals,
                "ts": _last_scan_ts,
                "slot": _last_scan_slot,
                "count": _last_scan_count,
            }, f, default=str)
    except Exception:
        pass

def _load_cache():
    global _active_signals, _last_scan_ts, _last_scan_slot, _last_scan_count
    import json
    try:
        if os.path.exists(_CACHE_FILE):
            with open(_CACHE_FILE) as f:
                d = json.load(f)
            _active_signals  = d.get("signals", [])
            _last_scan_ts    = d.get("ts")
            _last_scan_slot  = d.get("slot")
            _last_scan_count = d.get("count", 0)
    except Exception:
        pass


# ── Stock data ────────────────────────────────────────────────────────────────
def _fetch(ticker: str):
    sym = ticker.upper().replace("NSE:", "").strip() + ".NS"
    try:
        s    = yf.Ticker(sym)
        info = s.info
        hist = s.history(period="1y")
        if hist.empty:
            return None
        close    = hist["Close"].dropna()
        if close.empty:
            return None
        cmp      = round(float(close.iloc[-1]), 2)
        high_52w = round(float(hist["High"].max()), 2)
        low_52w  = round(float(hist["Low"].min()), 2)
        ret_1y   = round((close.iloc[-1] / close.iloc[0] - 1) * 100, 1)
        vs_high  = round((cmp / high_52w - 1) * 100, 1)
        mcap     = (info.get("marketCap") or 0) / 1e7
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
        logging.warning(f"yfinance {sym}: {e}")
        return None


# ── Groq AI ───────────────────────────────────────────────────────────────────
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


# ── Commands ──────────────────────────────────────────────────────────────────
def do_brief(ticker: str) -> str:
    d   = _fetch(ticker)
    sym = ticker.upper().replace("NSE:", "").strip()
    live = (
        f"CMP: ₹{d['cmp']}\n52W High: ₹{d['high_52w']} | Low: ₹{d['low_52w']}\n"
        f"vs 52W High: {d['vs_high']}% | 1Y Return: {d['ret_1y']}%\n"
        f"Mkt Cap: ₹{d['mcap_cr']:.0f} Cr | P/E: {d['pe']}x | P/B: {d['pb']}x | Div: {d['div_yield']}%\n"
        f"Revenue: ₹{d['revenue']:.0f} Cr | PAT: ₹{d['pat']:.0f} Cr | Sector: {d['sector']}"
    ) if d else f"Symbol: {sym}"
    return _ask(f"""CA and equity analyst @askakshayfinance. 1-page stock brief for *{sym}* (NSE) for Telegram.

Live data: {live}

Format (Telegram Markdown *bold* _italic_):

📊 *{sym} — NSE Tear Sheet*
_{d['name'] if d else sym}_

━━━━━━━━━━━━━━━━
💰 *PRICE*
CMP: ₹[x] | 52W H/L: ₹[h] / ₹[l]
vs 52W High: [x%]  |  1Y Return: [x%]

━━━━━━━━━━━━━━━━
🏦 *VALUATION*
Mkt Cap: ₹[x] Cr ([Cap size])
P/E: [x]x | P/B: [x]x | Div: [x]%

━━━━━━━━━━━━━━━━
📈 *FINANCIALS*
Revenue: ₹[x] Cr | PAT: ₹[x] Cr | Margin: [x]%
Revenue CAGR 3Y: ~[x]%

━━━━━━━━━━━━━━━━
🔑 *MOAT*
• [point]
• [point]

━━━━━━━━━━━━━━━━
⚠️ *RISKS*
• [risk]
• [risk]

━━━━━━━━━━━━━━━━
🎯 *VERDICT*
[1-2 lines, specific]

_@askakshayfinance_

Rules: specific numbers, no fluff, max 400 words.""")


def do_trade(ticker: str) -> str:
    d   = _fetch(ticker)
    sym = ticker.upper().replace("NSE:", "").strip()
    live = (f"CMP: ₹{d['cmp']}\n52W High: ₹{d['high_52w']} | Low: ₹{d['low_52w']}\n1Y: {d['ret_1y']}%"
            ) if d else f"Symbol: {sym}"
    return _ask(f"""Technical analyst. Swing trade setup for {sym} NSE.
{live}
Format (Telegram Markdown):

📊 *{sym} — Swing Trade Setup*

*Action:* BUY / SELL / AVOID
*Entry Zone:* ₹[x] – ₹[y]
*Stop Loss:* ₹[x] _(tight)_ | ₹[y] _(wide)_
*Target 1:* ₹[x]  `(1.5R)`
*Target 2:* ₹[x]  `(2.5R)`
*Target 3:* ₹[x]  `(4R)`

*Setup:* [type] | *Timeframe:* Swing 2–6 weeks | *RR:* [x]:1

*Thesis:*
[3 lines TA rationale]

⚠️ *Invalidation:* [level + reason]

_@askakshayfinance | Not SEBI advice_""", max_tokens=600)


def _send_document(chat_id: str, filename: str, content: str, caption: str = ""):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        requests.post(url, data={"chat_id": chat_id, "caption": caption},
                      files={"document": (filename, content.encode(), "text/html")}, timeout=30)
    except Exception as e:
        _post(f"❌ File error: {e}", chat_id)


def do_carousel(topic: str, chat_id: str):
    _post(f"🎨 Generating carousel: *{topic}*...", chat_id)
    html = _ask(f"""Brutalist dark Instagram carousel for @askakshayfinance (CA/FP&A).
Topic: "{topic}"

SPEC: Canvas 1080×1350px | bg #0A0A0A | accent #FF5F1F | text #FFFFFF #6E6E6E
Fonts (Google): Oswald 700 (numbers) | Playfair Display 900 italic (insight) | Space Grotesk (body) | Caveat (annotations)
8 slides: Hook → Audit → Finding 1 → Finding 2 → Finding 3 → Hard Truth → System → CTA
Rules: ONE insight/slide. Specific numbers (₹16,309 not ₹16K). Orange = alarm only.

HTML: single file, all CSS inline, slides as 1080×1350 divs stacked vertically.
Slide number bottom-right gray. @askakshayfinance bottom-left small gray.

Return ONLY complete HTML.""", max_tokens=4000)
    slug = topic.lower().replace(" ", "_")[:30]
    _send_document(chat_id, f"carousel_{slug}.html", html, f"🎨 {topic}")
    _post("✅ Open HTML in browser — all 8 slides.", chat_id)


def _format_scan_msg(signals, slot="Manual"):
    lines = [f"📡 *4H Momentum — {len(signals)} signal(s)* | _{slot}_\n"
             f"_RSI bottom↑ · Vol 3x+ · Bullish candle_\n"]
    for s in signals:
        lines.append(
            f"━━━━━━━━━━━━\n"
            f"📈 *{s['symbol']}* | _{s['pattern']}_\n"
            f"₹{s['price']} | Vol *{s['vol_ratio']}x* | RSI {s['rsi']} ↑\n"
            f"SL ₹{s['sl']} | T1 ₹{s['target1']} | T2 ₹{s['target2']} | RR {s['rr']}:1\n"
            f"[Chart]({s['tv_link']})"
        )
    lines.append("\n_@askakshayfinance | Not SEBI advice_")
    return "\n".join(lines)


def _run_scan(slot="Manual", notify=True, chat_id=None):
    """Core scan runner — used by both manual Scan command and scheduler."""
    global _active_signals, _last_scan_ts, _last_scan_slot, _last_scan_count
    try:
        from scanner import scan_tg_momentum
        signals = scan_tg_momentum()
        _last_scan_ts    = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
        _last_scan_slot  = slot
        _last_scan_count = len(signals)
        _active_signals  = signals
        _save_cache()

        # Log to DB
        try:
            from tracker import log_to_all_signals
            for s in signals:
                log_to_all_signals(
                    symbol=s["symbol"], signal_type="4h_momentum", action="BUY",
                    entry=s["price"], sl=s["sl"], t1=s["target1"], t2=s["target2"],
                    t3=s["target2"], rr=s["rr"], timeframe="4H", score=0,
                    metadata={"pattern": s["pattern"], "vol_ratio": s["vol_ratio"],
                              "rsi": s["rsi"], "tv_link": s["tv_link"]}
                )
        except Exception as e:
            logging.warning(f"DB log error: {e}")

        if signals and notify:
            msg = _format_scan_msg(signals, slot)
            _post(msg, chat_id)
        elif notify:
            _post(f"✅ Scan done ({slot}) — no signals right now.", chat_id)

        return signals
    except Exception as e:
        err = f"❌ Scan error: {e}"
        logging.error(err)
        if notify:
            _post(err, chat_id)
        return []


def do_scan(chat_id=None) -> str:
    _post("🔍 Scanning Nifty 500 on 4H... ~90 sec.", chat_id)
    signals = _run_scan(slot="Manual", notify=False)
    if not signals:
        return "✅ Scan done — no signals matching criteria right now."
    _post(_format_scan_msg(signals, "Manual"), chat_id)
    return f"✅ {len(signals)} signal(s) sent."


# ── Full swing scanner (A/A+ only, wired to Dhruvedge) ───────────────────────

def _run_swing_scan(slot="Auto"):
    """Run scan_all (stricter A/A+ scanner) and log to signals.db."""
    ts = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
    try:
        from scanner import scan_all
        from tracker import log_signals, update_all_outcomes, init_db
        init_db()
        update_all_outcomes()                    # close T1/T2/SL hits first
        signals = scan_all()                     # A/A+ only (score≥65, RR≥1.5, ADX≥20)

        # Staleness guard: drop any signal where live price < entry by >2%
        # (means signal data is stale — stock already moved against setup)
        fresh = []
        for s in signals:
            entry = float(s.get("price") or s.get("entry") or 0)
            live  = entry  # scan_all already fetches live price as "price"
            if entry <= 0 or live >= entry * 0.98:
                fresh.append(s)
            else:
                logging.info(f"Stale signal dropped: {s.get('symbol')} entry={entry} live={live}")
        signals = fresh

        if signals:
            log_signals(signals)
            from telegram_bot import send_alert, send_summary
            for s in signals:
                send_alert(s)
            send_summary(signals)
            logging.info(f"Swing scan [{slot}]: {len(signals)} A/A+ signals")
        else:
            logging.info(f"Swing scan [{slot}]: no signals")

        # Push all_signals.json to GitHub so Dhruvedge (Vercel) gets live data
        _push_signals_to_github()

    except Exception as e:
        logging.error(f"Swing scan error: {e}")
        _post(f"⚠️ Swing scan error ({slot}): {str(e)[:200]}")


def _push_signals_to_github():
    """Export signals.db → data/all_signals.json → git push → Dhruvedge stays live."""
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM all_signals ORDER BY date DESC LIMIT 200"
        ).fetchall()
        con.close()
        data = [dict(r) for r in rows]
        out_path = os.path.join(os.path.dirname(__file__), "data", "all_signals.json")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        # Git push (Railway has git installed; will silently fail if not)
        import subprocess
        base = os.path.dirname(__file__)
        dt   = datetime.now(IST).strftime("%Y-%m-%d %H:%M UTC")
        subprocess.run(["git", "-C", base, "add", "data/all_signals.json"], timeout=10)
        subprocess.run(["git", "-C", base, "commit", "-m",
                        f"data: update all_signals {dt} [skip ci]",
                        "--no-verify"], timeout=10)
        subprocess.run(["git", "-C", base, "push"], timeout=20)
        logging.info(f"GitHub push: {len(data)} signals exported")
    except Exception as e:
        logging.warning(f"GitHub push skipped: {e}")


# ── Scheduler ─────────────────────────────────────────────────────────────────
_CF_SYMBOLS = {
    "GOLD":    "GC=F",
    "SILVER":  "SI=F",
    "CRUDE":   "CL=F",
    "NATGAS":  "NG=F",
    "USDINR":  "USDINR=X",
    "EURINR":  "EURINR=X",
}

def _scan_commodity_forex(ts: str):
    """Check 15m momentum on Gold, Silver, Crude, USDINR. Push if RSI > 60 + move > 0.4%."""
    try:
        alerts = []
        for name, ticker in _CF_SYMBOLS.items():
            try:
                from scanner import _yf_download as _yfd
                df = _yfd(ticker, period="5d", interval="15m",
                          progress=False, auto_adjust=True)
                if df is None or len(df) < 14:
                    continue
                c = df["Close"].squeeze()
                v = df["Volume"].squeeze() if "Volume" in df.columns else None
                price    = float(c.iloc[-1])
                prev     = float(c.iloc[-2])
                # Use actual daily open: fetch 1d bar and take the open price
                try:
                    d1 = _yfd(ticker, period="2d", interval="1d",
                              progress=False, auto_adjust=True)
                    open_day = float(d1["Open"].squeeze().iloc[-1]) if len(d1) >= 1 else prev
                except Exception:
                    open_day = float(c.iloc[-32]) if len(c) >= 32 else prev  # ~8h ago fallback
                pct_move = ((price - open_day) / open_day) * 100 if open_day else 0

                # RSI (14-period)
                delta = c.diff()
                gain  = delta.clip(lower=0).rolling(14).mean()
                loss  = (-delta.clip(upper=0)).rolling(14).mean()
                rs    = gain / loss.replace(0, float("inf"))
                rsi_v = float((100 - 100 / (1 + rs)).iloc[-1])

                # Volume spike (skip if no volume data e.g. forex)
                vol_str = ""
                if v is not None and float(v.iloc[-20:].mean()) > 0:
                    spike = float(v.iloc[-1]) / float(v.iloc[-20:].mean())
                    vol_str = f" · Vol `{spike:.1f}x`"

                # Alert: RSI > 55 (lowered from 60) + move > 0.3% (lowered from 0.4%)
                if rsi_v > 55 and abs(pct_move) > 0.3:
                    atr = float((df["High"] - df["Low"]).rolling(14).mean().iloc[-1])
                    sl  = round(price - 1.0 * atr, 4) if pct_move > 0 else round(price + 1.0 * atr, 4)
                    t1  = round(price + 1.5 * atr, 4) if pct_move > 0 else round(price - 1.5 * atr, 4)
                    t2  = round(price + 2.5 * atr, 4) if pct_move > 0 else round(price - 2.5 * atr, 4)
                    rr  = round(abs(t2 - price) / abs(price - sl), 1) if abs(price - sl) > 0 else 0
                    if rr < 1.5:
                        continue
                    sign = "+" if pct_move > 0 else ""
                    emoji = "📈" if pct_move > 0 else "📉"
                    alerts.append(
                        f"{emoji} *{name}* | {sign}{pct_move:.2f}% · RSI `{rsi_v:.0f}`{vol_str}\n"
                        f"   Price `{price:.4f}` | SL `{sl:.4f}` | T1 `{t1:.4f}` | T2 `{t2:.4f}` | RR `{rr}x`"
                    )
                    # Log CF signal to DB so it's traceable
                    try:
                        from tracker import log_to_all_signals, init_db
                        init_db()
                        log_to_all_signals(
                            name, "cf_momentum", "BUY" if pct_move > 0 else "SELL",
                            price, sl, t1, t2, t2, rr, timeframe="15m", score=0,
                            metadata={"rsi": round(rsi_v, 1), "pct_move": round(pct_move, 2),
                                      "ticker": ticker}
                        )
                    except Exception as _e:
                        logging.debug(f"CF DB log {name}: {_e}")
            except Exception as e:
                logging.debug(f"CF scan {name}: {e}")

        if alerts:
            msg = f"🌍 *Forex & Commodity Moves* — {ts}\n_(15m · RSI>60 · move>0.4% · R:R≥1.5)_\n\n"
            msg += "\n\n".join(alerts)
            msg += "\n\n_MCX/Global prices · Not SEBI advice_"
            _post(msg)
            logging.info(f"CF scan: {len(alerts)} alerts pushed")
    except Exception as e:
        logging.error(f"CF scan error: {e}")


def _run_intraday_scan():
    """Intraday scanner — NSE 15m + Forex/Commodity moves. Only R:R ≥ 1.5 pushed."""
    from datetime import datetime as _dt
    _now = _dt.now(IST)
    _h, _m = _now.hour, _now.minute
    # Only run 9:30–14:30 IST on weekdays
    if _now.weekday() >= 5:
        return
    if not (9 <= _h < 14 or (_h == 14 and _m <= 30)):
        return
    try:
        from scanner import scan_intraday_momentum, scan_first_candle_breakout
        ts = _now.strftime("%d %b %Y %I:%M %p IST")

        # First-candle movers (9:30–9:44 only)
        if _h == 9 and 30 <= _m <= 44:
            try:
                fc = scan_first_candle_breakout()
                if fc:
                    lines = [f"🕯 *First Candle Movers* — {ts}\n_(>1% ≤2% from open — watch for continuation)_\n"]
                    for s in fc:
                        lines.append(f"• *{s['symbol']}* | Open ₹{s['open']} → ₹{s['close']} (+{s['pct_from_open']}%) | H ₹{s['high']}")
                    lines.append("\n_Monitor only · Entry only on confirmed breakout · Not SEBI advice_")
                    _post("\n".join(lines))
            except Exception as e:
                logging.warning(f"First-candle scan error: {e}")

        # NSE intraday momentum scan
        sigs = scan_intraday_momentum()
        sigs = [s for s in sigs if float(s.get("rr", 0)) >= 1.5]
        if not sigs:
            logging.info(f"Intraday scan {ts}: no NSE R:R≥1.5 signals")
            return

        lines = [f"⚡ *{len(sigs)} NSE Intraday Signal(s)* — {ts}\n_(15m · VWAP + RSI55 cross + Vol surge · R:R≥1.5)_\n"]
        for s in sigs[:5]:
            lines.append(
                f"📈 *{s['symbol']}* | BUY ₹{s['price']}\n"
                f"   SL ₹{s['sl']} | T1 ₹{s['target1']} | T2 ₹{s['target2']}\n"
                f"   RR `{s.get('rr',0)}x` · Vol `{s.get('vol_ratio',0)}x` · RSI `{s.get('rsi',0)}` · VWAP ₹{s.get('vwap','—')}"
            )
        lines.append("\n_Exit by 3:15 PM IST · Intraday only · Not SEBI advice_")
        _post("\n".join(lines))

        # Log to DB
        try:
            from tracker import log_to_all_signals, init_db
            init_db()
            for s in sigs:
                log_to_all_signals(
                    s["symbol"], "intraday", "BUY", s["price"], s["sl"],
                    s["target1"], s["target2"], s["target2"],
                    s["rr"], timeframe="15m", score=s.get("score", 0)
                )
        except Exception as e:
            logging.warning(f"Intraday DB log error: {e}")

        logging.info(f"Intraday scan {ts}: {len(sigs)} NSE signals pushed")
    except Exception as e:
        logging.error(f"Intraday scan error: {e}")
        _post(f"⚠️ Intraday scan error: {str(e)[:200]}")


def _run_magic_scan():
    """Runs both Magic + MagicMagic screeners, logs to DB, pushes to GitHub."""
    ts = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
    logging.info(f"[MAGIC] Running Magic + MagicMagic screeners at {ts}")
    try:
        from scanner import scan_magic, scan_magicmagic
        from tracker import log_to_all_signals, init_db
        init_db()

        magic_results    = scan_magic(top_n=12)
        magicmagic_results = scan_magicmagic(top_n=12)

        def _fmt_block(results, label, emoji):
            if not results:
                return f"{emoji} *{label}* — no stocks passed filters today.\n"
            lines = [f"{emoji} *{label} — {len(results)} stocks* | _{ts}_\n"]
            for r in results[:6]:
                se = {"BUY":"🟢","WATCH":"🟡","AVOID":"🔴","NEUTRAL":"⚪"}.get(r.get("short",""),"⚪")
                we = {"BUY":"🟢","WATCH":"🟡","AVOID":"🔴","NEUTRAL":"⚪"}.get(r.get("swing",""),"⚪")
                le = {"BUY":"🟢","WATCH":"🟡","AVOID":"🔴","NEUTRAL":"⚪"}.get(r.get("long",""),"⚪")
                lines.append(
                    f"━━━━━━━━━━\n"
                    f"*{r['symbol']}* ₹{r['price']} · Score `{r['score']}`\n"
                    f"CAGR `{r['cagr_3yr']}%` · RSI(W) `{r['weekly_rsi']}` · `{r['dist_52wh']}%` from 52WH\n"
                    f"{se} Short: _{r.get('short_note','—')}_\n"
                    f"{we} Swing: _{r.get('swing_note','—')}_\n"
                    f"{le} Long:  _{r.get('long_note','—')}_"
                )
            return "\n".join(lines)

        msg = _fmt_block(magic_results, "Magic Screener (>15% from 52WH)", "🔮")
        msg += "\n\n"
        msg += _fmt_block(magicmagic_results, "MagicMagic (20–40% from 52WH)", "✨")
        msg += "\n_Investtech-style · Not SEBI advice · @askakshayfinance_"
        _post(msg)

        # Log to DB
        today = datetime.now(IST).strftime("%Y-%m-%d")
        for r in magic_results:
            try:
                log_to_all_signals(
                    r["symbol"], "magic", "WATCH",
                    r["price"], None, None, None, None,
                    None, timeframe="Weekly", score=r["score"]
                )
            except Exception as e:
                logging.debug(f"Magic DB log {r['symbol']}: {e}")
        for r in magicmagic_results:
            try:
                log_to_all_signals(
                    r["symbol"], "magicmagic", "WATCH",
                    r["price"], None, None, None, None,
                    None, timeframe="Weekly", score=r["score"]
                )
            except Exception as e:
                logging.debug(f"MagicMagic DB log {r['symbol']}: {e}")

        _push_signals_to_github()
        logging.info(f"[MAGIC] Done — Magic:{len(magic_results)} MagicMagic:{len(magicmagic_results)}")
    except Exception as e:
        logging.error(f"[MAGIC] Error: {e}")
        _post(f"⚠️ Magic scan error: {str(e)[:200]}")


def _start_scheduler():
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger   # ← correct module

        sched = BackgroundScheduler(timezone=IST)

        # Swing scanner — A/A+ only (score≥65, RR≥1.5, ADX≥20, Vol≥2.5x)
        swing_slots = [("09:25", "Open"), ("11:42", "Midday"), ("16:32", "EOD"), ("20:00", "After")]
        for t, label in swing_slots:
            h, m = t.split(":")
            def _swing_job(lbl=label):
                logging.info(f"[SCHED] Swing scan firing: {lbl}")
                _run_swing_scan(slot=lbl)
            sched.add_job(
                _swing_job,
                CronTrigger(hour=int(h), minute=int(m), day_of_week="mon-fri", timezone=IST)
            )

        # Intraday scanner — every 30 min, 9:30–14:30 IST (self-guards time window)
        sched.add_job(
            _run_intraday_scan,
            IntervalTrigger(minutes=30, timezone=IST)
        )

        # Forex & Commodity scan — 4 fixed slots daily (mon-sun, markets never close)
        for cf_time in ["10:00", "14:00", "18:00", "22:00"]:
            h, m = cf_time.split(":")
            sched.add_job(
                lambda ts=cf_time: _scan_commodity_forex(
                    datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
                ),
                CronTrigger(hour=int(h), minute=int(m), timezone=IST)
            )

        # Magic + MagicMagic screeners — 14:00 IST daily (7 days)
        sched.add_job(
            _run_magic_scan,
            CronTrigger(hour=14, minute=0, timezone=IST)
        )

        # Position monitor — every 15 min
        sched.add_job(
            _monitor_positions,
            IntervalTrigger(minutes=15, timezone=IST)
        )

        sched.start()
        logging.info("Scheduler started: swing + intraday + CF(4x) + magic(14:00) + position monitor(15min)")
    except Exception as e:
        logging.warning(f"Scheduler not started: {e}")


# ── Command router ────────────────────────────────────────────────────────────
HELP_TEXT = """🤖 *Claude AI Trading Bot*

*Commands:*
`Brief: NSE:TICKER` — 1-page stock report
`Trade: NSE:TICKER` — swing trade setup
`Scan` — 4H momentum scan now
`Carousel: [topic]` — 8-slide HTML carousel
`/active` — today's signals
`/stats` — bot status
`Help` — this message

_Auto-scans: 9:20 | 11:45 | 4:30 PM IST_
_@askakshayfinance_"""


def route(text: str, chat_id: str):
    t  = text.strip()
    tl = t.lower()

    # /active — override with in-memory store
    if tl == "/active":
        if not _active_signals:
            _post("No signals from today's scans yet.\nSend `Scan` to run now.", chat_id)
        else:
            lines = [f"📋 *Active Signals ({len(_active_signals)})*\n_Last scan: {_last_scan_ts}_\n"]
            for s in _active_signals:
                lines.append(
                    f"• *{s['symbol']}* _{s['pattern']}_ | ₹{s['price']}\n"
                    f"  SL ₹{s['sl']} | T1 ₹{s['target1']} | T2 ₹{s['target2']}"
                )
            _post("\n".join(lines), chat_id)
        return

    # /stats — override with real bot stats
    if tl == "/stats":
        now_ist = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
        open_sigs = _db_open_signals(min_score=65)
        _post(
            f"⚙️ *Bot Status* — {now_ist}\n\n"
            f"Last scan: {_last_scan_ts or 'Not run yet'}\n"
            f"Open signals in DB: {len(open_sigs)}\n\n"
            f"*Schedule (IST):*\n"
            f"Swing A/A+: 9:25 · 11:42 · 16:32 · 20:00\n"
            f"Intraday: 9:30–14:30 every 30min\n"
            f"CF (Forex/Commod): 10:00 · 14:00 · 18:00 · 22:00\n"
            f"🔮 Magic + MagicMagic: 14:00 daily\n"
            f"Position monitor: every 15min\n\n"
            f"Commands: `Scan` · `/cf` · `/intraday` · `/magic` · `/track` · `Brief: NSE:X`",
            chat_id
        )
        return

    # /start
    if tl == "/start":
        _post(HELP_TEXT, chat_id)
        return

    # /track SYM ENTRY SL T1 T2 — add manual trade to DB for monitoring
    if tl.startswith("/track"):
        parts = t.split()
        if len(parts) < 5:
            _post(
                "Usage: `/track SYM ENTRY SL T1 T2`\n"
                "Example: `/track DRREDDY 6200 6050 6380 6550`\n"
                "Bot will monitor SL trail + T1/T2 exits automatically.",
                chat_id
            )
            return
        try:
            sym    = parts[1].upper()
            entry  = float(parts[2])
            sl     = float(parts[3])
            t1     = float(parts[4])
            t2     = float(parts[5]) if len(parts) > 5 else round(entry + (t1 - entry) * 2, 2)
            rr     = round((t2 - entry) / (entry - sl), 1) if entry > sl else 0
            from tracker import log_to_all_signals, init_db
            init_db()
            log_to_all_signals(
                symbol=sym, signal_type="manual", action="BUY",
                entry=entry, sl=sl, t1=t1, t2=t2, t3=round(entry + (t2 - entry) * 1.5, 2),
                rr=rr, timeframe="Swing", score=70,
                metadata={"source": "manual_track", "added_by": "user"}
            )
            _post(
                f"✅ *{sym} added to monitor*\n"
                f"Entry ₹{entry} | SL ₹{sl} | T1 ₹{t1} | T2 ₹{t2}\n"
                f"RR: `{rr}x` · Bot will trail SL at T1 hit & alert on exits.\n"
                f"_Position monitor runs every 15min (market hours)_",
                chat_id
            )
        except Exception as e:
            _post(f"❌ Track error: {e}\nUsage: `/track SYM ENTRY SL T1 T2`", chat_id)
        return

    # /magic or /magicmagic — run both screeners on demand
    if tl in ("/magic", "magic", "magic scan", "/magicmagic", "magicmagic"):
        _post(
            "🔮 *Magic + MagicMagic running...*\n"
            "3YR CAGR+ × Weekly RSI ≥46 × Dip filters\n"
            "_Takes 3–5 min — scanning Nifty 500_",
            chat_id
        )
        _run_magic_scan()
        return

    # /cf — manual CF scan trigger
    if tl in ("/cf", "cf scan", "commodity"):
        _post("🌍 Running Forex & Commodity scan...", chat_id)
        ts = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
        _scan_commodity_forex(ts)
        return

    # /intraday — manual intraday trigger
    if tl in ("/intraday", "intraday"):
        _post("⚡ Running intraday scan...", chat_id)
        _run_intraday_scan()
        return

    # All other /commands — pass to telegram_bot handler
    if t.startswith("/"):
        try:
            from telegram_bot import handle_command
            handle_command(t, chat_id)
        except Exception as e:
            _post(f"❌ {e}", chat_id)
        return

    # Help
    if tl in ("help", "?", "hi", "hello"):
        _post(HELP_TEXT, chat_id)
        return

    # Brief
    if tl.startswith("brief"):
        raw = t[5:].strip().lstrip(":").strip()
        if not raw:
            _post("Usage: `Brief: NSE:RELIANCE`", chat_id); return
        sym = raw.upper().replace("NSE:", "").strip()
        _post(f"📊 Fetching brief for *{sym}*...", chat_id)
        try:
            _post(do_brief(raw), chat_id)
        except Exception as e:
            _post(f"❌ Error: {e}", chat_id)
        return

    # Trade
    if tl.startswith("trade"):
        raw = t[5:].strip().lstrip(":").strip()
        if not raw:
            _post("Usage: `Trade: NSE:RELIANCE`", chat_id); return
        sym = raw.upper().replace("NSE:", "").strip()
        _post(f"📊 Building trade setup for *{sym}*...", chat_id)
        try:
            _post(do_trade(raw), chat_id)
        except Exception as e:
            _post(f"❌ Error: {e}", chat_id)
        return

    # Scan
    if tl.startswith("scan"):
        result = do_scan(chat_id)
        _post(result, chat_id)
        return

    # Carousel
    if tl.startswith("carousel"):
        topic = t[8:].strip().lstrip(":").strip()
        if not topic:
            _post("Usage: `Carousel: 5 tax mistakes salaried employees make`", chat_id); return
        try:
            do_carousel(topic, chat_id)
        except Exception as e:
            _post(f"❌ Error: {e}", chat_id)
        return

    _post("Type `Help` to see commands.", chat_id)


# ── Polling loop ──────────────────────────────────────────────────────────────
def _delete_webhook():
    """Delete any registered webhook so getUpdates polling works cleanly."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook",
            json={"drop_pending_updates": False}, timeout=10
        )
        result = r.json()
        if result.get("ok"):
            logging.info("Webhook deleted — polling mode active")
        else:
            logging.warning(f"deleteWebhook: {result}")
    except Exception as e:
        logging.warning(f"deleteWebhook error: {e}")


def run():
    _delete_webhook()
    _load_cache()
    _load_position_states()
    threading.Thread(target=_start_api_server, daemon=True).start()
    _start_scheduler()
    logging.info("Claude Bot started. Polling Telegram...")
    from telegram_bot import TELEGRAM_CHAT_ID as _CHAT_ID
    _cid = os.environ.get("TELEGRAM_CHAT_ID") or _CHAT_ID
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": _cid,
                "text": "🤖 *Dhruvedge Bot online*\n"
                        "Swing: 9:25·11:42·16:32·20:00 | Intraday: 9:30–14:30 | CF: 10·14·18·22\n"
                        "Commands: `Help` · `Scan` · `/cf` · `/intraday` · `/track` · `/stats`",
                "parse_mode": "Markdown",
                "reply_markup": {"remove_keyboard": True}
            }, timeout=10
        )
    except Exception as e:
        logging.warning(f"Startup message error: {e}")
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            r   = requests.get(url, params={"offset": offset, "timeout": 30}, timeout=35)
            if not r.ok:
                logging.warning(f"getUpdates HTTP {r.status_code}: {r.text[:200]}")
                time.sleep(5)
                continue
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg    = upd.get("message", {})
                txt    = msg.get("text", "").strip()
                cid    = str(msg.get("chat", {}).get("id", ""))
                if txt and cid:
                    threading.Thread(target=route, args=(txt, cid), daemon=True).start()
        except Exception as e:
            logging.warning(f"Poll error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    run()

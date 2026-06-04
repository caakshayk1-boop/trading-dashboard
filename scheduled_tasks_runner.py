#!/usr/bin/env python3
"""
scheduled_tasks_runner.py — GitHub Actions entry point for scheduled tasks.

Usage:
    python scheduled_tasks_runner.py cf_scan
    python scheduled_tasks_runner.py daily_brief

CF scan dedup: uses Turso cf_dedup table — shared state across Railway restarts
and GH Actions runs. Same symbol+direction won't fire within CF_COOLDOWN hours.
"""

import sys
import time
import logging
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

IST          = timezone(timedelta(hours=5, minutes=30))
CF_COOLDOWN  = 4 * 3600   # 4 hours between same-direction signals per symbol


# ── Turso-based CF dedup ─────────────────────────────────────────────────────

def _cf_dedup_init(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS cf_dedup (
            symbol   TEXT NOT NULL,
            bias     TEXT NOT NULL,
            sent_at  INTEGER NOT NULL,
            PRIMARY KEY (symbol, bias)
        )
    """)
    con.commit()


def cf_already_sent(symbol: str, bias: str) -> bool:
    """Returns True if this signal was sent within CF_COOLDOWN seconds."""
    try:
        import db
        con = db.connect()
        _cf_dedup_init(con)
        row = con.execute(
            "SELECT sent_at FROM cf_dedup WHERE symbol=? AND bias=?",
            (symbol, bias)
        ).fetchone()
        con.close()
        if row:
            age = time.time() - float(row[0])
            if age < CF_COOLDOWN:
                log.info(f"CF dedup: {symbol} {bias} sent {age/3600:.1f}h ago — skip")
                return True
        return False
    except Exception as e:
        log.warning(f"cf_already_sent error: {e}")
        return False  # fail open — allow signal if DB unreachable


def cf_mark_sent(symbol: str, bias: str):
    """Record that this signal was just sent."""
    try:
        import db
        con = db.connect()
        _cf_dedup_init(con)
        con.execute(
            "INSERT OR REPLACE INTO cf_dedup (symbol, bias, sent_at) VALUES (?,?,?)",
            (symbol, bias, int(time.time()))
        )
        con.commit()
        db.sync(con)
        con.close()
    except Exception as e:
        log.warning(f"cf_mark_sent error: {e}")


def cf_expire_old():
    """Clean up entries older than 24h from cf_dedup table."""
    try:
        import db
        con = db.connect()
        _cf_dedup_init(con)
        cutoff = int(time.time()) - 86400
        con.execute("DELETE FROM cf_dedup WHERE sent_at < ?", (cutoff,))
        con.commit()
        db.sync(con)
        con.close()
    except Exception as e:
        log.warning(f"cf_expire_old error: {e}")


# ── CF scan with dedup ────────────────────────────────────────────────────────

def run_cf_scan():
    """Run CF scan with Turso-based dedup — won't repeat same signal within 4h."""
    import os, requests as req_lib
    import yfinance as yf
    import pandas as pd

    cf_expire_old()  # clean stale entries first

    TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

    def post(text: str):
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            print(text)
            return
        try:
            req_lib.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
                timeout=15,
            )
        except Exception as e:
            log.warning(f"Telegram error: {e}")

    # Import the full CF scan logic from claude_bot
    try:
        from claude_bot import _scan_commodity_forex, _CF_SYMBOLS, _rsi14, _atr14
        import yfinance as _yf

        ts = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
        log.info(f"CF scan at {ts}")

        # Run the scan — it uses its own in-memory dedup too (belt+suspenders)
        # The Turso dedup is the authoritative cross-process dedup
        alerts_raw = []
        from scanner import _yf_download as _yfd

        for name, ticker in _CF_SYMBOLS.items():
            try:
                df1h = _yfd(ticker, period="7d",  interval="1h",  progress=False, auto_adjust=True)
                df4h = _yfd(ticker, period="60d", interval="4h",  progress=False, auto_adjust=True)
                df1d = _yfd(ticker, period="5d",  interval="1d",  progress=False, auto_adjust=True)
                if df1h is None or len(df1h) < 20: continue
                if df4h is None or len(df4h) < 16: continue
                if df1d is None or len(df1d) < 2:  continue

                c1h = df1h["Close"].squeeze()
                h1h = df1h["High"].squeeze()
                l1h = df1h["Low"].squeeze()

                try:
                    live = float(_yf.Ticker(ticker).fast_info.last_price or 0)
                    price = live if live > 0 else float(c1h.iloc[-1])
                except Exception:
                    price = float(c1h.iloc[-1])
                if price <= 0: continue

                day_high = float(df1d["High"].squeeze().iloc[-1])
                day_low  = float(df1d["Low"].squeeze().iloc[-1])
                prev_cls = float(df1d["Close"].squeeze().iloc[-2])
                day_mid  = (day_high + day_low) / 2
                day_chg  = round((price - prev_cls) / prev_cls * 100, 2) if prev_cls > 0 else 0

                atr_1h = _atr14(h1h, l1h, c1h)
                if atr_1h <= 0: continue
                rsi_1h = float(_rsi14(c1h).iloc[-1])

                c4h = df4h["Close"].squeeze()
                rsi_4h_s    = _rsi14(c4h)
                rsi_4h_cur  = float(rsi_4h_s.iloc[-1])
                rsi_4h_prev = float(rsi_4h_s.iloc[-2])

                bullish_4h = rsi_4h_cur > 55 or (rsi_4h_prev < 55 and rsi_4h_cur >= 55)
                bearish_4h = rsi_4h_cur < 45 or (rsi_4h_prev > 45 and rsi_4h_cur <= 45)

                if   bullish_4h and price >= day_mid and 45 <= rsi_1h <= 75: bias = "BUY"
                elif bearish_4h and price <= day_mid and 25 <= rsi_1h <= 55: bias = "SELL"
                else: continue

                # ── TURSO DEDUP — authoritative cross-process check ──────────
                if cf_already_sent(name, bias):
                    continue

                if bias == "BUY":
                    sl = max(round(day_low * 0.9985, 4), round(price - 1.5 * atr_1h, 4))
                    if sl >= price: sl = round(price - 1.5 * atr_1h, 4)
                else:
                    sl = min(round(day_high * 1.0015, 4), round(price + 1.5 * atr_1h, 4))
                    if sl <= price: sl = round(price + 1.5 * atr_1h, 4)

                risk = abs(price - sl)
                if risk <= 0: continue

                d   = 1 if bias == "BUY" else -1
                t1  = round(price + d * 1.5 * risk, 4)
                t2  = round(price + d * 2.5 * risk, 4)
                t3  = round(price + d * 4.0 * risk, 4)
                rr  = round(abs(t2 - price) / risk, 1)
                if rr < 1.5: continue

                tv_sym  = ticker.replace("=X", "").replace("=F", "").replace("^", "")
                tv_link = f"https://in.tradingview.com/chart/?symbol={tv_sym}"
                sign    = "+" if day_chg >= 0 else ""
                emoji   = "📈" if bias == "BUY" else "📉"

                if   rsi_4h_prev < 55 and rsi_4h_cur >= 55: rsi_lbl = f"4H RSI crossed ↑55 🚀 (`{rsi_4h_cur:.0f}`)"
                elif rsi_4h_prev > 45 and rsi_4h_cur <= 45: rsi_lbl = f"4H RSI crossed ↓45 💧 (`{rsi_4h_cur:.0f}`)"
                elif bias == "BUY": rsi_lbl = f"4H RSI `{rsi_4h_cur:.0f}` (bullish zone)"
                else:               rsi_lbl = f"4H RSI `{rsi_4h_cur:.0f}` (bearish zone)"

                pct_from_high = round((day_high - price) / day_high * 100, 2) if day_high > 0 else 0
                pct_from_low  = round((price - day_low)  / day_low  * 100, 2) if day_low  > 0 else 0
                if   pct_from_high <= 0.3: level_lbl = "🔝 At Day High"
                elif pct_from_low  <= 0.3: level_lbl = "🔻 At Day Low"
                elif bias == "BUY":        level_lbl = f"Upper half · {pct_from_high:.1f}% below DH"
                else:                      level_lbl = f"Lower half · {pct_from_low:.1f}% above DL"

                alerts_raw.append({
                    "name": name, "ticker": ticker, "bias": bias, "emoji": emoji,
                    "price": price, "sl": sl, "t1": t1, "t2": t2, "t3": t3,
                    "rr": rr, "day_high": day_high, "day_low": day_low,
                    "rsi_4h": rsi_4h_cur, "rsi_1h": rsi_1h,
                    "rsi_lbl": rsi_lbl, "level_lbl": level_lbl,
                    "day_chg": day_chg, "sign": sign, "tv_link": tv_link,
                })

            except Exception as e:
                log.warning(f"CF scan {name}: {e}")

        if alerts_raw:
            lines = [f"🌍 *Forex & Commodity Signals* — {ts}\n_1H candle · 4H RSI · Live price_\n"]
            for a in alerts_raw:
                lines.append(
                    f"━━━━━━━━━━━━━━\n"
                    f"{a['emoji']} *{a['name']}* | *{a['bias']}*\n"
                    f"Price `{a['price']:.4f}` ({a['sign']}{a['day_chg']:.2f}% day)\n"
                    f"Day H/L: `{a['day_high']:.4f}` / `{a['day_low']:.4f}`\n"
                    f"📍 {a['level_lbl']}\n"
                    f"📊 {a['rsi_lbl']} · 1H RSI `{a['rsi_1h']:.0f}`\n\n"
                    f"*Entry:* `{a['price']:.4f}`\n"
                    f"*SL:*    `{a['sl']:.4f}`\n"
                    f"*T1:* `{a['t1']:.4f}` _(1.5R)_\n"
                    f"*T2:* `{a['t2']:.4f}` _(2.5R)_\n"
                    f"*T3:* `{a['t3']:.4f}` _(4R)_\n"
                    f"R:R `{a['rr']}:1` · [📊 Chart]({a['tv_link']})"
                )
                # Mark sent in Turso AFTER building message
                cf_mark_sent(a["name"], a["bias"])

            lines.append("\n_Not SEBI advice · @askakshayfinance_")
            post("\n".join(lines))
            log.info(f"CF scan: {len(alerts_raw)} signals sent")

            # Log to Turso
            try:
                from tracker import log_to_all_signals, init_db
                init_db()
                for a in alerts_raw:
                    log_to_all_signals(
                        a["name"], "cf_1h", a["bias"],
                        a["price"], a["sl"], a["t1"], a["t2"], a["t3"],
                        a["rr"], timeframe="1H", score=0,
                        metadata={"rsi_4h": round(a["rsi_4h"], 1), "ticker": a["ticker"]}
                    )
            except Exception as _e:
                log.debug(f"CF DB log: {_e}")
        else:
            log.info("CF scan: no new signals (all deduped or no setup)")

    except Exception as e:
        log.error(f"CF scan error: {e}")
        import traceback; traceback.print_exc()


def run_daily_brief():
    from daily_brief import send_brief
    log.info("Running daily brief")
    send_brief()
    log.info("Daily brief sent")


if __name__ == "__main__":
    task = sys.argv[1] if len(sys.argv) > 1 else "auto"

    if task == "cf_scan":
        run_cf_scan()
    elif task == "daily_brief":
        run_daily_brief()
    else:
        now_ist = datetime.now(IST)
        if now_ist.hour == 6 and now_ist.minute < 15:
            run_daily_brief()
        else:
            run_cf_scan()

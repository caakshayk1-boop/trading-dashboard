"""
Standalone scanner — runs completely without Streamlit/dashboard.
Called by GitHub Actions cron (cloud) or local terminal.
Mac can be OFF — GitHub Actions handles all automation.

Schedule (IST):
  09:20 — 4H early signals + Commodity signals + market open preview
  11:45 — Swing signals (Nifty 500) + F&O breakouts + 4H update
  16:30 — EOD: Breakouts (daily confirmed) + Weekly/Monthly + Commodities EOD

All 3 slots: Forex/Commodities price update sent each time.
"""
import sys, logging, os
from datetime import datetime
import pytz

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/standalone.log"),
    ]
)

IST = pytz.timezone("Asia/Kolkata")


def _send(msg):
    try:
        from telegram_bot import _post
        _post(msg)
    except Exception as e:
        logging.warning(f"Telegram send failed: {e}")


def _slot(now_ist):
    """Return scan slot name based on current IST time."""
    h, m = now_ist.hour, now_ist.minute
    # 09:00–10:30 → morning
    if 9 <= h < 10 or (h == 10 and m <= 30):
        return "morning"
    # 11:00–13:30 → midday
    if 11 <= h < 13 or (h == 13 and m <= 30):
        return "midday"
    # 15:30–17:30 → eod
    if 15 <= h < 18:
        return "eod"
    # fallback: run everything
    return "full"


def run_markets(time_str):
    """Send Forex + Commodity prices update."""
    from scanner import fetch_forex_comm
    fc = fetch_forex_comm()
    if not fc:
        return
    lines = [f"🌐 *Markets* — {time_str}\n"]
    for r in fc:
        sign = "+" if r["Chg%"] >= 0 else ""
        arrow = "▲" if r["Chg%"] >= 0 else "▼"
        lines.append(f"{arrow} *{r['Asset']}*: `{r['Last']}` ({sign}{r['Chg%']}%)")
    _send("\n".join(lines))


def run_4h_scan(time_str):
    """4H early-entry equity scan (RSI cross 55 + volume)."""
    from scanner import scan_4h
    logging.info("Running 4H RSI-55 scan...")
    sigs = scan_4h()
    logging.info(f"4H scan: {len(sigs)} signals")
    if sigs:
        lines = [f"⚡ *4H Early-Entry Signals* — {time_str}\n_(RSI crossing 55 + Volume surge)_\n"]
        for b in sigs[:8]:
            fno_tag = " `F&O`" if b.get("fno") else ""
            lines.append(
                f"• *{b['symbol']}*{fno_tag} | ₹{b['price']} | "
                f"RSI {b['rsi']} | Vol {b['vol_ratio']}x | "
                f"T1 ₹{b['target1']} | SL ₹{b['sl']} | RR {b['rr']}"
            )
        _send("\n".join(lines))
    return sigs


def run_commodity_scan(time_str):
    """Commodity signals: Gold, Silver, Crude Oil, Nat Gas."""
    from scanner import scan_commodities
    logging.info("Running commodity scan...")
    sigs = scan_commodities()
    logging.info(f"Commodity scan: {len(sigs)} signals")
    if sigs:
        lines = [f"🥇 *Commodity Signals* — {time_str}\n"]
        for s in sigs:
            action = s["action"]
            arrow  = "▲ BUY" if action == "BUY" else "▼ SELL"
            col    = "📈" if action == "BUY" else "📉"
            lines.append(
                f"{col} *{s['symbol']}* `{s['timeframe']}` | {arrow} @ {s['price']} | "
                f"SL {s['sl']} | T1 {s['target1']} | T2 {s['target2']} | RR {s['rr']} | RSI {s['rsi']}"
            )
            lines.append(f"  _{s['label']}_")
        _send("\n".join(lines))
    return sigs


def run_swing_scan(time_str):
    """Full Nifty 500 swing scan + Telegram alerts."""
    from scanner import scan_all
    from telegram_bot import send_alert, send_summary, send_top_picks
    from tracker import log_signals, update_outcomes, init_db
    from config import SEND_TOP_PICKS_ONLY

    init_db()
    logging.info("Updating open trade outcomes...")
    update_outcomes()

    logging.info("Running swing scan (Nifty 500)...")
    signals = scan_all()
    logging.info(f"Swing scan: {len(signals)} signals")

    if signals:
        log_signals(signals)
        if SEND_TOP_PICKS_ONLY:
            send_top_picks(signals, top_n=10)
        else:
            for s in signals:
                ok = send_alert(s)
                logging.info(f"Alert sent: {s['symbol']} score={s['score']} ok={ok}")
    send_summary(signals)
    return signals


def run_breakout_scan(time_str):
    """Confirmed breakouts (Daily/Weekly/Monthly)."""
    from scanner import scan_breakouts
    logging.info("Running breakout scan (F&O universe)...")
    breakouts = scan_breakouts()
    logging.info(f"Breakouts: {len(breakouts)} found")
    if breakouts:
        lines = [f"📊 *Confirmed Breakouts* — {time_str}\n"]
        for b in breakouts[:10]:
            fno_tag = " `F&O`" if b.get("fno") else ""
            tf_emoji = {"Monthly": "📅", "Weekly": "📆", "Daily": "📋"}.get(b["timeframe"], "📋")
            lines.append(
                f"{tf_emoji} *{b['symbol']}*{fno_tag} | ₹{b['price']} | "
                f"{b['timeframe']}: *{b['pattern']}* | "
                f"T1 ₹{b['target1']} | SL ₹{b['sl']} | RR {b['rr']} | Vol {b['vol_ratio']}x"
            )
        _send("\n".join(lines))
    return breakouts


def run_fno_scan(time_str, signals):
    """Send F&O suggestions from swing signals that are F&O eligible."""
    fno_sigs = [s for s in signals if s.get("fno_eligible") and s.get("fno_suggestion")]
    if not fno_sigs:
        return
    lines = [f"🎯 *F&O Trade Suggestions* — {time_str}\n"]
    for s in fno_sigs[:6]:
        f = s["fno_suggestion"]
        lines.append(
            f"• *{s['symbol']}* {f['direction']} | "
            f"ATM ₹{f['atm_strike']} | OTM ₹{f['otm_strike']} | "
            f"Risk ~{f['risk_pts']} pts | Score {s['score']}"
        )
    _send("\n".join(lines))


def main():
    now     = datetime.now(IST)
    time_str = now.strftime("%d %b %Y %I:%M %p IST")
    slot    = _slot(now)
    logging.info(f"=== Scan started: {time_str} | Slot: {slot} ===")

    _send(f"🔄 *SwingDesk Pro* scan started — {time_str}\n_Slot: {slot.upper()}_")

    try:
        # ── Always send markets update ─────────────────────────────────────────
        run_markets(time_str)

        if slot == "morning":
            # 09:20 — Pre-market: 4H early signals + commodity signals
            run_4h_scan(time_str)
            run_commodity_scan(time_str)

        elif slot == "midday":
            # 11:45 — Mid-session: swing + F&O + 4H update
            signals = run_swing_scan(time_str)
            run_fno_scan(time_str, signals)
            run_4h_scan(time_str)
            run_commodity_scan(time_str)

        elif slot in ("eod", "full"):
            # 16:30 — EOD: confirmed breakouts (candle closed) + commodities + swing recap
            run_breakout_scan(time_str)
            run_swing_scan(time_str)
            run_commodity_scan(time_str)

        logging.info(f"=== Scan finished: {slot} ===")
        return 0

    except Exception as e:
        logging.error(f"SCAN FAILED: {e}", exc_info=True)
        _send(f"⚠️ *Scanner Error* ({slot})\n`{str(e)[:200]}`\nCheck logs.")
        return 1


if __name__ == "__main__":
    sys.exit(main())

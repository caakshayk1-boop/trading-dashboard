"""
Standalone scanner — runs completely without Streamlit/dashboard.
Called by GitHub Actions cron (cloud) or local terminal.
Mac can be OFF — GitHub Actions handles all automation.

Schedule (IST):
  09:20 — 4H early signals + Commodity signals + market open preview
  11:45 — Swing signals (Nifty 500) + F&O + 4H update + commodities
  16:30 — EOD: Breakouts (daily candle closed) + Weekly/Monthly + commodities

All results are logged to signals.db so the dashboard reads live data.
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
    h, m = now_ist.hour, now_ist.minute
    if 9 <= h < 10 or (h == 10 and m <= 30):
        return "morning"
    if 11 <= h < 14:
        return "midday"
    if 15 <= h < 18:
        return "eod"
    return "full"


# ── Individual scan runners ───────────────────────────────────────────────────

def run_markets(time_str):
    from scanner import fetch_forex_comm
    fc = fetch_forex_comm()
    if not fc:
        return
    lines = [f"🌐 *Markets* — {time_str}\n"]
    for r in fc:
        sign  = "+" if r["Chg%"] >= 0 else ""
        arrow = "▲" if r["Chg%"] >= 0 else "▼"
        lines.append(f"{arrow} *{r['Asset']}*: `{r['Last']}` ({sign}{r['Chg%']}%)")
    _send("\n".join(lines))


def run_4h_scan(time_str):
    from scanner import scan_4h
    from tracker import log_4h_signals
    logging.info("Running 4H RSI-55 scan...")
    sigs = scan_4h()
    logging.info(f"4H scan: {len(sigs)} signals")
    if sigs:
        log_4h_signals(sigs)
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
    from scanner import scan_commodities
    from tracker import log_commodity_signals
    logging.info("Running commodity scan...")
    sigs = scan_commodities()
    logging.info(f"Commodity scan: {len(sigs)} signals")
    if sigs:
        log_commodity_signals(sigs)
        lines = [f"🥇 *Commodity Signals* — {time_str}\n"]
        for s in sigs:
            arrow = "▲ BUY" if s["action"] == "BUY" else "▼ SELL"
            col   = "📈" if s["action"] == "BUY" else "📉"
            lines.append(
                f"{col} *{s['symbol']}* `{s['timeframe']}` | {arrow} @ {s['price']} | "
                f"SL {s['sl']} | T1 {s['target1']} | T2 {s['target2']} | "
                f"RR {s['rr']} | RSI {s['rsi']}"
            )
            lines.append(f"  _{s['label']}_")
        _send("\n".join(lines))
    return sigs


def run_swing_scan(time_str):
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
                logging.info(f"Alert: {s['symbol']} score={s['score']} ok={ok}")
    send_summary(signals)
    return signals


def run_breakout_scan(time_str):
    from scanner import scan_breakouts
    from tracker import log_breakouts
    logging.info("Running breakout scan (F&O universe)...")
    breakouts = scan_breakouts()
    logging.info(f"Breakouts: {len(breakouts)} found")
    if breakouts:
        log_breakouts(breakouts)
        lines = [f"📊 *Confirmed Breakouts* — {time_str}\n"]
        for b in breakouts[:10]:
            fno_tag  = " `F&O`" if b.get("fno") else ""
            tf_emoji = {"Monthly": "📅", "Weekly": "📆", "Daily": "📋"}.get(b["timeframe"], "📋")
            lines.append(
                f"{tf_emoji} *{b['symbol']}*{fno_tag} | ₹{b['price']} | "
                f"{b['timeframe']}: *{b['pattern']}* | "
                f"T1 ₹{b['target1']} | SL ₹{b['sl']} | RR {b['rr']} | Vol {b['vol_ratio']}x"
            )
        _send("\n".join(lines))
    return breakouts


def run_tlm_scan(time_str, interval="4h"):
    """TLM Trendline Channel Breakout scanner — 4H intraday or Daily EOD."""
    from scanner import scan_tlm_breakouts
    from tracker import log_breakouts  # reuse breakouts table
    tf_label = "4H" if interval == "4h" else "Daily"
    logging.info(f"Running TLM channel breakout scan ({tf_label})...")
    tlm_sigs = scan_tlm_breakouts(interval=interval)
    logging.info(f"TLM scan ({tf_label}): {len(tlm_sigs)} breakouts")
    if tlm_sigs:
        # Adapt to breakouts table schema (add missing fields)
        for s in tlm_sigs:
            s.setdefault("patterns", [s.get("pattern", "TL Channel Breakout")])
            s.setdefault("target3", s.get("target2", 0))
        log_breakouts(tlm_sigs)
        lines = [f"🤖 *AI Signals* ({tf_label}) — {time_str}\n"
                 f"_(Price broke upper regression channel + volume surge)_\n"]
        for b in tlm_sigs[:8]:
            fno_tag = " `F&O`" if b.get("fno") else ""
            lines.append(
                f"• *{b['symbol']}*{fno_tag} | ₹{b['price']} | "
                f"Upper: ₹{b.get('upper_band', '?')} | "
                f"T1 ₹{b['target1']} | SL ₹{b['sl']} | RR {b['rr']} | Vol {b['vol_ratio']}x"
            )
        _send("\n".join(lines))
    return tlm_sigs


def run_fno_alerts(time_str, signals):
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


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    from tracker import log_scan_meta, init_db
    init_db()

    now      = datetime.now(IST)
    time_str = now.strftime("%d %b %Y %I:%M %p IST")
    slot     = _slot(now)
    counts   = {}

    logging.info(f"=== Scan started: {time_str} | Slot: {slot} ===")
    _send(f"🔄 *SwingDesk Pro* — {time_str}\n_Slot: {slot.upper()} starting..._")

    try:
        run_markets(time_str)

        if slot == "morning":
            # 09:20 — 4H early + TLM 4H channel breakouts + commodities
            sigs_4h  = run_4h_scan(time_str)
            tlm_4h   = run_tlm_scan(time_str, interval="4h")
            comms    = run_commodity_scan(time_str)
            counts   = {"4h": len(sigs_4h), "tlm_4h": len(tlm_4h), "commodities": len(comms)}

        elif slot == "midday":
            # 11:45 — Full swing + F&O + 4H update + TLM 4H + commodities
            signals  = run_swing_scan(time_str)
            run_fno_alerts(time_str, signals)
            sigs_4h  = run_4h_scan(time_str)
            tlm_4h   = run_tlm_scan(time_str, interval="4h")
            comms    = run_commodity_scan(time_str)
            counts   = {"swing": len(signals), "4h": len(sigs_4h), "tlm_4h": len(tlm_4h), "commodities": len(comms)}

        else:  # eod / full
            # 16:30 — Confirmed breakouts + TLM Daily EOD + swing recap + commodities
            breakouts = run_breakout_scan(time_str)
            tlm_daily = run_tlm_scan(time_str, interval="1d")
            signals   = run_swing_scan(time_str)
            comms     = run_commodity_scan(time_str)
            counts    = {"breakouts": len(breakouts), "tlm_daily": len(tlm_daily),
                         "swing": len(signals), "commodities": len(comms)}

        log_scan_meta(slot, counts)
        logging.info(f"=== Scan finished: {slot} | {counts} ===")

        # Export to data/*.json so Streamlit Cloud can read via GitHub raw URL
        from tracker import export_signals_json
        export_signals_json()
        logging.info("Signal data exported to data/")

        return 0

    except Exception as e:
        logging.error(f"SCAN FAILED: {e}", exc_info=True)
        _send(f"⚠️ *Scanner Error* ({slot})\n`{str(e)[:200]}`\nCheck logs.")
        return 1


if __name__ == "__main__":
    sys.exit(main())

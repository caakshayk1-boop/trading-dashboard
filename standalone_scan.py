"""
Standalone scanner — runs completely without Streamlit/dashboard.
Called by GitHub Actions (cloud cron) or local terminal.
Mac can be OFF — GitHub Actions handles all automation.
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
    except Exception:
        pass


def main():
    now = datetime.now(IST)
    time_str = now.strftime("%d %b %Y %I:%M %p IST")
    logging.info(f"=== Scan started: {time_str} ===")

    try:
        from scanner import scan_all, scan_breakouts, fetch_forex_comm
        from telegram_bot import send_alert, send_summary, send_top_picks
        from tracker import log_signals, update_outcomes, init_db
        from config import SEND_TOP_PICKS_ONLY

        init_db()

        # ── Swing signals ──────────────────────────────────────────────────────
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

        # ── Breakout scan ──────────────────────────────────────────────────────
        logging.info("Running breakout scan (F&O universe)...")
        breakouts = scan_breakouts()
        logging.info(f"Breakouts: {len(breakouts)} found")

        if breakouts:
            lines = [f"📊 *Confirmed Breakouts* — {time_str}\n"]
            for b in breakouts[:8]:
                fno_tag = " `F&O`" if b["fno"] else ""
                lines.append(
                    f"• *{b['symbol']}*{fno_tag} | ₹{b['price']} | "
                    f"{b['timeframe']}: {b['pattern']} | "
                    f"T1 ₹{b['target1']} | SL ₹{b['sl']} | RR {b['rr']}"
                )
            _send("\n".join(lines))

        # ── Forex & Commodities ────────────────────────────────────────────────
        logging.info("Fetching forex/commodities...")
        fc = fetch_forex_comm()
        if fc:
            lines = [f"🌐 *Markets* — {time_str}\n"]
            for r in fc:
                sign = "+" if r["Chg%"] >= 0 else ""
                lines.append(f"• {r['Asset']}: {r['Last']} ({sign}{r['Chg%']}%)")
            _send("\n".join(lines))

        logging.info("=== Scan finished successfully ===")
        return 0

    except Exception as e:
        logging.error(f"SCAN FAILED: {e}", exc_info=True)
        _send(f"⚠️ *Scanner Error*\n`{str(e)[:200]}`\nCheck logs.")
        return 1


if __name__ == "__main__":
    sys.exit(main())

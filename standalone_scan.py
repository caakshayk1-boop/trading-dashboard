"""
Standalone scanner — runs completely without Streamlit/dashboard.
Called by GitHub Actions (cloud) or macOS LaunchAgent (local cron).
Mac can be OFF — GitHub Actions handles it.
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

def main():
    now = datetime.now(IST)
    logging.info(f"=== Standalone scan started: {now.strftime('%d %b %Y %I:%M %p IST')} ===")

    try:
        from scanner import scan_all
        from telegram_bot import send_alert, send_summary, send_top_picks
        from tracker import log_signals, update_outcomes, init_db
        from config import SEND_TOP_PICKS_ONLY

        init_db()

        logging.info("Updating outcomes on open trades...")
        update_outcomes()

        logging.info("Running full scan...")
        signals = scan_all()
        logging.info(f"Scan complete: {len(signals)} signals")

        if signals:
            log_signals(signals)
            if SEND_TOP_PICKS_ONLY:
                send_top_picks(signals, top_n=5)
            else:
                for s in signals:
                    ok = send_alert(s)
                    logging.info(f"Alert sent: {s['symbol']} score={s['score']} ok={ok}")
        send_summary(signals)
        logging.info("=== Scan finished successfully ===")
        return 0

    except Exception as e:
        logging.error(f"SCAN FAILED: {e}", exc_info=True)
        # Send failure alert to Telegram
        try:
            from telegram_bot import _post
            _post(f"⚠️ *Scanner Error*\n`{str(e)[:200]}`\nCheck logs.")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())

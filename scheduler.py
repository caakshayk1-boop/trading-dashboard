"""Run this standalone to enable auto-scans without the dashboard open."""
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz, logging

from scanner import scan_all
from telegram_bot import send_alert, send_summary, send_top_picks
from tracker import log_signals, update_outcomes
from config import SCAN_TIMES, SEND_TOP_PICKS_ONLY

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
IST = pytz.timezone("Asia/Kolkata")


def run_scan():
    logging.info("Auto-scan triggered")
    signals = scan_all()
    log_signals(signals)
    update_outcomes()

    if SEND_TOP_PICKS_ONLY:
        send_top_picks(signals, top_n=5)
    else:
        for s in signals:
            send_alert(s)
    send_summary(signals)
    logging.info(f"Scan done: {len(signals)} signals")


if __name__ == "__main__":
    scheduler = BlockingScheduler(timezone=IST)
    for t in SCAN_TIMES:
        h, m = t.split(":")
        scheduler.add_job(run_scan, CronTrigger(hour=int(h), minute=int(m), timezone=IST))
        logging.info(f"Scheduled scan at {t} IST")

    logging.info("Scheduler running. Press Ctrl+C to stop.")
    scheduler.start()

"""Run this standalone to enable auto-scans without the dashboard open."""
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz, logging
from datetime import datetime

from scanner import scan_all, scan_commodities, is_trading_day
from telegram_bot import send_alert, send_summary, send_top_picks
from tracker import log_signals, update_outcomes
from config import SEND_TOP_PICKS_ONLY

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
IST = pytz.timezone("Asia/Kolkata")

# Scan schedule (IST): trading days run all 4 slots; holiday/weekend = 9:30 AM only
TRADING_DAY_SLOTS  = ["09:20", "11:42", "16:30", "20:00"]
HOLIDAY_SLOTS      = ["09:30"]          # Saturday / NSE holiday: one digest only

# Slot labels for Telegram context
SLOT_LABELS = {
    "09:20": "4H Momentum + Commodity",
    "09:30": "Market Digest (Holiday/Weekend)",
    "11:42": "Swing + F&O",
    "16:30": "Breakouts + EOD",
    "20:00": "Multibagger Weekly",
}


def run_scan(slot: str):
    now = datetime.now(IST)
    trading = is_trading_day(now)
    label   = SLOT_LABELS.get(slot, slot)

    # Skip intraday slots on non-trading days
    if not trading and slot != "09:30":
        logging.info(f"Non-trading day — skipping {slot} slot")
        return

    logging.info(f"Auto-scan [{label}] triggered")
    signals = scan_all()
    log_signals(signals)
    update_outcomes()

    # Also scan commodities on 9:20 / 9:30 slot
    if slot in ("09:20", "09:30"):
        try:
            comm = scan_commodities()
            from tracker import log_commodity_signals
            log_commodity_signals(comm)
        except Exception as e:
            logging.warning(f"Commodity scan failed: {e}")

    if SEND_TOP_PICKS_ONLY:
        send_top_picks(signals, top_n=5)
    else:
        for s in signals:
            send_alert(s)
    send_summary(signals)
    logging.info(f"Scan done [{label}]: {len(signals)} signals")


if __name__ == "__main__":
    scheduler = BlockingScheduler(timezone=IST)

    all_slots = set(TRADING_DAY_SLOTS) | set(HOLIDAY_SLOTS)
    for t in all_slots:
        h, m = t.split(":")
        scheduler.add_job(
            run_scan, CronTrigger(hour=int(h), minute=int(m), timezone=IST),
            args=[t], id=f"scan_{t}"
        )
        logging.info(f"Scheduled scan at {t} IST")

    logging.info("Scheduler running. Press Ctrl+C to stop.")
    scheduler.start()

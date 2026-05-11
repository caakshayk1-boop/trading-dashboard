"""
Intraday momentum scanner — runs every 30 min during market hours via GitHub Actions.
9:30–14:30 IST (Mon-Fri, trading days). Skips non-trading days automatically.
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
        logging.FileHandler("logs/intraday.log"),
    ]
)

IST = pytz.timezone("Asia/Kolkata")

NSE_HOLIDAYS = {
    "2025-01-26","2025-02-26","2025-03-14","2025-03-31","2025-04-10",
    "2025-04-14","2025-04-18","2025-05-01","2025-08-15","2025-08-27",
    "2025-10-02","2025-10-21","2025-10-22","2025-11-05","2025-12-25",
    "2026-01-26","2026-03-18","2026-04-02","2026-04-06","2026-04-10",
    "2026-04-14","2026-05-01","2026-08-15","2026-10-02","2026-10-20",
    "2026-10-21","2026-11-04","2026-12-25",
}


def _send(msg):
    try:
        from telegram_bot import _post
        _post(msg)
    except Exception as e:
        logging.warning(f"Telegram send failed: {e}")


def main():
    from tracker import log_to_all_signals, is_duplicate, export_signals_json, init_db
    from scanner import scan_intraday_momentum, scan_first_candle_breakout, is_trading_day
    from deploy_dhruvedge import run as deploy_dhruvedge
    init_db()

    now      = datetime.now(IST)
    today    = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%d %b %Y %I:%M %p IST")

    # Skip weekends and holidays
    if now.weekday() >= 6 or today in NSE_HOLIDAYS:
        logging.info("Weekend/holiday — intraday scan skipped")
        return 0

    if not is_trading_day(now):
        logging.info("Non-trading day — intraday scan skipped")
        return 0

    # Only run 9:30–14:30 IST
    h, m = now.hour, now.minute
    if not (9 <= h < 14 or (h == 14 and m <= 30)):
        logging.info(f"Outside market window ({h}:{m:02d} IST) — skip")
        return 0

    logging.info(f"=== Intraday scan: {time_str} ===")

    # First-candle breakout feed (9:30–9:44 IST only)
    try:
        fc_stocks = scan_first_candle_breakout()
        if fc_stocks:
            lines = [f"🕯 *First Candle Movers* — {time_str}\n_(>1% ≤2% from open · worth watching)_\n"]
            for s in fc_stocks:
                lines.append(
                    f"• *{s['symbol']}* | Open ₹{s['open']} → ₹{s['close']}"
                    f" (+{s['pct_from_open']}%) | H ₹{s['high']}"
                )
            lines.append("\n_Monitor for breakout continuation · Not SEBI advice_")
            _send("\n".join(lines))
    except Exception as e:
        logging.warning(f"First-candle scan error: {e}")

    try:
        sigs = scan_intraday_momentum()
        new_sigs = [s for s in sigs if not is_duplicate(s["symbol"], "intraday")]
        logging.info(f"Intraday: {len(new_sigs)} new signals (from {len(sigs)} raw)")

        if new_sigs:
            lines = [f"⚡ *Intraday Momentum* — {time_str}\n_(15m · VWAP + RSI55 + Vol surge)_\n"]
            for s in new_sigs[:6]:
                lines.append(
                    f"• *{s['symbol']}* | 15m | BUY ₹{s['price']}\n"
                    f"  SL ₹{s['sl']} | T1 ₹{s['target1']} | T2 ₹{s['target2']}"
                    f" | RR {s['rr']} | Vol {s['vol_ratio']}x | RSI {s['rsi']}"
                )
                log_to_all_signals(
                    s["symbol"], "intraday", "BUY", s["price"], s["sl"],
                    s["target1"], s["target2"], s["target2"],
                    s["rr"], timeframe="15m", score=s.get("score", 0)
                )
            lines.append("\n_Intraday only · Exit by 3:15 PM IST · Not SEBI advice_")
            _send("\n".join(lines))
        else:
            logging.info("No new intraday signals this cycle")

        export_signals_json()
        logging.info("Intraday scan complete")

        # Deploy Dhruvedge if new signals fired
        if new_sigs:
            try:
                deploy_dhruvedge()
            except Exception as e:
                logging.warning(f"Dhruvedge deploy failed (non-fatal): {e}")
        return 0

    except Exception as e:
        logging.error(f"Intraday scan failed: {e}", exc_info=True)
        try:
            from telegram_bot import _post
            _post(f"⚠️ *Intraday Scan Error* — {time_str}\n`{str(e)[:300]}`")
        except Exception:
            pass
        return 0  # exit 0 — Telegram alert sent, no need to fail GH Actions job


if __name__ == "__main__":
    sys.exit(main())

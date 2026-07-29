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
    """Run the CF scan via the shared cf_engine, with Turso-based dedup.

    The signal logic used to be duplicated here and in
    claude_bot.py::_scan_commodity_forex with different R-multiples and a
    different price source, so the "same" bot produced different alerts
    depending on which entry point fired. Both now call cf_engine.
    """
    import os, requests as req_lib
    import cf_engine

    cf_expire_old()

    TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

    def post(text: str):
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            print(text)
            return
        try:
            r = req_lib.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                      "parse_mode": "Markdown"},
                timeout=15,
            )
            # Malformed Markdown otherwise drops the whole alert silently.
            if not r.ok and r.status_code == 400 and "parse" in r.text.lower():
                log.warning("Markdown rejected — resending as plain text")
                req_lib.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
                    timeout=15,
                )
        except Exception as e:
            log.warning(f"Telegram error: {e}")

    ts = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
    log.info(f"CF scan at {ts}")

    try:
        signals = cf_engine.scan()
    except Exception as e:
        log.error(f"CF scan error: {e}")
        import traceback; traceback.print_exc()
        return

    fresh = [s for s in signals if not cf_already_sent(s["name"], s["bias"])]
    if not fresh:
        log.info(f"CF scan: {len(signals)} signal(s), all deduped or none found")
        return

    body = [f"\U0001F30D *Forex & Commodity Signals* — {ts}",
            "_1H entry · 4H regime · structural targets_\n"]
    for s in fresh:
        body.append(cf_engine.format_alert(s))
        cf_mark_sent(s["name"], s["bias"])
    body.append("\n_Not SEBI advice · @askakshayfinance_")
    post("\n".join(body))
    log.info(f"CF scan: {len(fresh)} signal(s) sent")

    try:
        from tracker import log_to_all_signals, init_db
        init_db()
        for s in fresh:
            log_to_all_signals(
                s["name"], "cf_1h", s["bias"], s["price"], s["sl"],
                s["t1"], s["t2"], s["t3"], s["rr"], timeframe="1H",
                score=s["score"],
                metadata={"rsi_4h": s["rsi_4h"], "vol_ratio": s["vol_ratio"],
                          "target_source": s["target_source"],
                          "sl_atr_mult": s["sl_atr_mult"]},
            )
    except Exception as _e:
        log.debug(f"CF DB log: {_e}")


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

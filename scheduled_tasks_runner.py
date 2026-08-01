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

# 48h, not 4h. backtest.py derives its cooldown from the holding horizon
# (cooldown = horizon * bar_hours = 48 * 1 = 48h for the cf profile), so the
# +0.171R/-0.029R figures assume at most one trade per symbol per 48 hours.
# Live was re-firing every 4 hours — 12x the tested rate — which is how 60
# simultaneously-"open" NATGAS rows and 56 CRUDE rows accumulated. Anything
# looser than the backtest is a different strategy with unmeasured expectancy.
CF_COOLDOWN  = 48 * 3600


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


def cf_position_open(symbol: str, bias: str) -> bool:
    """True if this symbol already has a live position in all_signals.

    cf_dedup alone was not enough: cf_expire_old() deletes its rows after 24h,
    so a symbol that stayed open for weeks became eligible to re-fire and each
    fire inserted another OPEN row. The position table is the real authority on
    whether we are already in the trade.
    """
    try:
        import db
        con = db.connect()
        row = con.execute(
            "SELECT 1 FROM all_signals WHERE symbol=? AND signal_type='cf_1h' "
            "AND status IN ('OPEN','T1_HIT') LIMIT 1", (symbol,)
        ).fetchone()
        con.close()
        if row:
            log.info(f"CF skip: {symbol} already has a live position")
            return True
        return False
    except Exception as e:
        log.warning(f"cf_position_open error: {e}")
        return False   # fail open — a missed dedup is better than a missed exit


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

    def post(text: str) -> bool:
        """Returns True only if Telegram accepted the message.

        The result used to be discarded, so a rejected alert was indistinguishable
        from a delivered one and the DB row still looked sent.
        """
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            print(text)
            return False
        try:
            r = req_lib.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                      "parse_mode": "Markdown"},
                timeout=15,
            )
            if r.ok:
                return True
            # Malformed Markdown otherwise drops the whole alert silently.
            if r.status_code == 400 and "parse" in r.text.lower():
                log.warning("Markdown rejected — resending as plain text")
                r = req_lib.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
                    timeout=15,
                )
                return bool(r.ok)
            log.error(f"Telegram API error {r.status_code} — {r.text[:200]}")
            return False
        except Exception as e:
            log.error(f"Telegram error: {e}")
            return False

    ts = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
    log.info(f"CF scan at {ts}")

    try:
        signals = cf_engine.scan()
    except Exception as e:
        log.error(f"CF scan error: {e}")
        import traceback; traceback.print_exc()
        return

    fresh = [s for s in signals
             if not cf_already_sent(s["name"], s["bias"])
             and not cf_position_open(s["name"], s["bias"])]
    if not fresh:
        log.info(f"CF scan: {len(signals)} signal(s), all deduped or none found")
        return

    body = [f"\U0001F30D *Forex & Commodity Signals* — {ts}",
            "_1H entry · 4H regime · structural targets_\n"]
    for s in fresh:
        body.append(cf_engine.format_alert(s))
        cf_mark_sent(s["name"], s["bias"])
    body.append("\n_Not SEBI advice · @askakshayfinance_")
    sent_ok = post("\n".join(body))
    log.info(f"CF scan: {len(fresh)} signal(s), telegram_ok={sent_ok}")

    try:
        from tracker import log_to_all_signals, mark_alerts_sent, init_db
        init_db()
        ids = []
        for s in fresh:
            ids.append(log_to_all_signals(
                s["name"], "cf_1h", s["bias"], s["price"], s["sl"],
                s["t1"], s["t2"], s["t3"], s["rr"], timeframe="1H",
                score=s["score"],
                metadata={"rsi_4h": s["rsi_4h"], "vol_ratio": s["vol_ratio"],
                          "target_source": s["target_source"],
                          "sl_atr_mult": s["sl_atr_mult"]},
            ))
        mark_alerts_sent(ids, sent_ok, "telegram send failed")
    except Exception as _e:
        log.warning(f"CF DB log: {_e}")


def run_daily_brief():
    from daily_brief import send_brief
    log.info("Running daily brief")
    send_brief()
    log.info("Daily brief sent")


def run_sip_bucket():
    """Propose this month's SIP bucket and mark existing holdings to market.

    build_bucket() is idempotent per calendar month, so running this daily is
    safe — the bucket is created on the first run of the month and every later
    run only refreshes prices.
    """
    import sip_engine
    log.info("SIP: building/refreshing this month's bucket")
    b = sip_engine.build_bucket()
    n = sip_engine.refresh_prices()
    log.info(f"SIP: bucket {b.get('bucket')} — {len(b.get('holdings', []))} names, "
             f"{n} prices refreshed")


if __name__ == "__main__":
    task = sys.argv[1] if len(sys.argv) > 1 else "auto"

    if task == "cf_scan":
        run_cf_scan()
    elif task == "daily_brief":
        run_daily_brief()
    elif task == "sip_bucket":
        run_sip_bucket()
    else:
        now_ist = datetime.now(IST)
        if now_ist.hour == 6 and now_ist.minute < 15:
            run_daily_brief()
        else:
            run_cf_scan()

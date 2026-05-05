"""
Standalone scanner — runs completely without Streamlit/dashboard.
Called by GitHub Actions cron (cloud) or local terminal.
Mac can be OFF — GitHub Actions handles all automation.

Schedule (IST):
  WEEKDAYS:
    09:20 — 4H early signals + AI channel breakouts + Commodity signals
    11:45 — Swing signals (Nifty 500) + F&O + 4H update + AI + commodities
    16:30 — EOD: Breakouts (daily candle closed) + AI daily + commodities

  SATURDAY 09:30:
    Full routine scan (all of above) + Potential Multibaggers list

NSE holidays: scan skipped automatically.

All results logged to signals.db + exported to data/*.json for Streamlit Cloud.
"""
import sys, logging, os
from datetime import datetime, date
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

# ── NSE Holiday Calendar 2025 ─────────────────────────────────────────────────
NSE_HOLIDAYS = {
    "2025-01-26",  # Republic Day
    "2025-02-26",  # Mahashivratri
    "2025-03-14",  # Holi
    "2025-03-31",  # Id-Ul-Fitr
    "2025-04-10",  # Shri Ram Navami
    "2025-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
    "2025-04-18",  # Good Friday
    "2025-05-01",  # Maharashtra Day
    "2025-08-15",  # Independence Day
    "2025-08-27",  # Ganesh Chaturthi
    "2025-10-02",  # Gandhi Jayanti / Dussehra
    "2025-10-21",  # Diwali Laxmi Puja
    "2025-10-22",  # Diwali Balipratipada
    "2025-11-05",  # Prakash Gurpurb
    "2025-12-25",  # Christmas Day
}


def _send(msg):
    try:
        from telegram_bot import _post
        _post(msg)
    except Exception as e:
        logging.warning(f"Telegram send failed: {e}")


# ── Price Alert Monitor (checks open signals against live prices) ─────────────
def run_price_alerts(time_str: str):
    """
    Runs every scan cycle. Checks all open signals in all_signals table.
    Sends Telegram alert when SL1 / Final SL / T1 / T2 is hit.
    Updates signal status in DB.

    Alert hierarchy (per signal, in priority order):
      SL_HIT  → red stop-loss alert + status updated (no more checks)
      T2_HIT  → green target 2 alert + close signal
      T1_HIT  → green target 1 alert + move SL to entry (signal stays open for T2)
      SL1_WARN → amber warning when price drops below SL1 (first SL level)
    """
    import yfinance as yf
    import pandas as pd
    try:
        from tracker import _conn, init_db
        init_db()
        with _conn() as c:
            open_df = pd.read_sql(
                "SELECT * FROM all_signals WHERE status='OPEN' ORDER BY date DESC",
                c)
    except Exception as e:
        logging.warning(f"price_alerts: DB read failed: {e}")
        return

    if open_df.empty:
        logging.info("price_alerts: no open signals to check")
        return

    logging.info(f"price_alerts: checking {len(open_df)} open signals")
    updates = []   # (new_status, exit_price, pnl_pct, r_mult, sig_id)
    alerts_sent = 0

    for _, row in open_df.iterrows():
        try:
            sym_yf  = row["symbol"].replace(".NS", "") + ".NS"
            # Fetch 5-min candles for the day — most recent price
            tick = yf.download(sym_yf, period="1d", interval="5m",
                               progress=False, auto_adjust=True, timeout=8)
            if tick is None or tick.empty:
                # Fallback: 1-day daily
                tick = yf.download(sym_yf, period="2d", interval="1d",
                                   progress=False, auto_adjust=True, timeout=8)
            if tick is None or tick.empty:
                continue

            cur_price = float(tick["Close"].squeeze().iloc[-1])
            cur_hi    = float(tick["High"].squeeze().max())
            cur_lo    = float(tick["Low"].squeeze().min())

            entry  = float(row["entry"])
            sl     = float(row["sl"]  or entry * 0.95)   # final SL
            sl1_v  = float(row.get("metadata") and
                           __import__("json").loads(row.get("metadata") or "{}").get("sl1", 0)
                           or entry * 0.97)              # warning SL
            t1     = float(row["target1"])
            t2     = float(row.get("target2") or t1 * 1.04)
            sig_id = int(row["id"])
            action = str(row.get("action", "BUY")).upper()
            sym    = row["symbol"]
            tf     = row.get("timeframe", "SWING")

            risk = abs(entry - sl) or 1

            # Check levels using day's H/L (intraday accuracy)
            if action == "SELL":
                sl_hit  = cur_hi >= sl
                sl1_hit = cur_hi >= sl1_v and cur_hi < sl
                t1_hit  = cur_lo <= t1 and not sl_hit
                t2_hit  = cur_lo <= t2 and not sl_hit
            else:
                sl_hit  = cur_lo <= sl
                sl1_hit = cur_lo <= sl1_v and cur_lo > sl
                t1_hit  = cur_hi >= t1 and not sl_hit
                t2_hit  = cur_hi >= t2 and not sl_hit

            if sl_hit:
                pnl   = round((sl - entry) / entry * 100 * (1 if action == "BUY" else -1), 2)
                r_m   = round((sl - entry) / (risk if action == "BUY" else -risk), 2)
                updates.append(("SL_HIT", sl, pnl, r_m, sig_id))
                _send(
                    f"🛑 *SL HIT — {sym}* | {tf}\n"
                    f"Entry ₹{entry:.1f} → SL ₹{sl:.1f} touched\n"
                    f"Loss: `{pnl}%` | R: `{r_m}`\n"
                    f"_Exit trade. Review thesis before re-entry._"
                )
                alerts_sent += 1
                logging.info(f"SL HIT: {sym} entry={entry} sl={sl} cur_lo={cur_lo}")

            elif t2_hit:
                pnl  = round((t2 - entry) / entry * 100 * (1 if action == "BUY" else -1), 2)
                r_m  = round(abs(t2 - entry) / risk, 2)
                updates.append(("T2_HIT", t2, pnl, r_m, sig_id))
                _send(
                    f"🎯🎯 *TARGET 2 HIT — {sym}* | {tf}\n"
                    f"Entry ₹{entry:.1f} → T2 ₹{t2:.1f}\n"
                    f"Gain: `+{pnl}%` | `{r_m}R` ✅\n"
                    f"_Full exit. Exceptional trade._"
                )
                alerts_sent += 1
                logging.info(f"T2 HIT: {sym} t2={t2} gain={pnl}%")

            elif t1_hit:
                pnl  = round((t1 - entry) / entry * 100 * (1 if action == "BUY" else -1), 2)
                # Mark as T1_HIT but keep open (still tracking for T2)
                updates.append(("T1_HIT", t1, pnl, round(abs(t1 - entry) / risk, 2), sig_id))
                _send(
                    f"✅ *TARGET 1 HIT — {sym}* | {tf}\n"
                    f"Entry ₹{entry:.1f} → T1 ₹{t1:.1f}\n"
                    f"Gain: `+{pnl}%` ✓\n"
                    f"_Book 50% · Move SL to entry · Trail remaining for T2 ₹{t2:.1f}_"
                )
                alerts_sent += 1

            elif sl1_hit:
                # Warning only — don't update DB status yet
                drop = round((cur_price - entry) / entry * 100, 2)
                _send(
                    f"⚠️ *SL1 WARNING — {sym}* | {tf}\n"
                    f"Price ₹{cur_price:.1f} breached warning SL ₹{sl1_v:.1f}\n"
                    f"Change: `{drop}%` | Final SL: ₹{sl:.1f}\n"
                    f"_Tighten or exit half position. Watch closely._"
                )
                alerts_sent += 1

        except Exception as e:
            logging.warning(f"price_alerts {row.get('symbol','?')}: {e}")
            continue

    # Write status updates to DB
    if updates:
        try:
            from tracker import _conn
            with _conn() as c:
                for new_status, exit_p, pnl, r_m, sig_id in updates:
                    c.execute(
                        "UPDATE all_signals SET status=?, exit_price=?, pnl_pct=?, r_multiple=? "
                        "WHERE id=? AND status='OPEN'",
                        (new_status, exit_p, pnl, r_m, sig_id))
                c.commit()
            logging.info(f"price_alerts: {len(updates)} signal statuses updated")
        except Exception as e:
            logging.warning(f"price_alerts: DB update failed: {e}")

    logging.info(f"price_alerts: done | {alerts_sent} alerts sent | {len(updates)} updated")


def _slot(now_ist, is_holiday=False):
    """Return scan slot based on IST time, weekday, holiday."""
    wd = now_ist.weekday()  # 0=Mon … 5=Sat … 6=Sun
    if wd == 5:              # Saturday
        return "weekend"
    if wd == 6:              # Sunday — no scan
        return "none"
    if is_holiday:           # NSE holiday: single 9:30 AM scan only
        h, m = now_ist.hour, now_ist.minute
        if 9 <= h < 11:
            return "holiday"
        return "none"        # skip all other slots on holiday
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


# Commodity conflict groups — don't send opposing signals for same underlying
_COMM_CONFLICT_GROUPS = [
    {"CL=F", "BZ=F"},       # WTI Crude + Brent Crude (same underlying)
    {"GC=F", "SI=F"},       # Gold + Silver (tend to correlate)
    {"NG=F"},               # Natural Gas (standalone)
]

def _filter_commodity_conflicts(sigs):
    """Remove conflicting commodity signals (e.g. BUY WTI + SELL Brent)."""
    if not sigs:
        return sigs
    # Build ticker → signal map
    ticker_map = {s["ticker"]: s for s in sigs}
    remove = set()
    for group in _COMM_CONFLICT_GROUPS:
        group_sigs = [ticker_map[t] for t in group if t in ticker_map]
        if len(group_sigs) < 2:
            continue
        actions = set(s["action"] for s in group_sigs)
        if len(actions) > 1:  # conflicting BUY + SELL in same group
            # Keep highest RR, drop the rest
            best = max(group_sigs, key=lambda x: x.get("rr", 0))
            for s in group_sigs:
                if s["ticker"] != best["ticker"]:
                    remove.add(s["ticker"])
                    logging.warning(
                        f"Commodity conflict: dropped {s['symbol']} {s['action']} "
                        f"(conflicts with {best['symbol']} {best['action']})"
                    )
    return [s for s in sigs if s["ticker"] not in remove]


def run_4h_scan(time_str):
    from scanner import scan_4h
    from tracker import log_4h_signals, log_to_all_signals, is_duplicate
    logging.info("Running 4H RSI-55 scan...")
    sigs = scan_4h()
    # Dedup: skip already-alerted symbols
    sigs = [s for s in sigs if not is_duplicate(s["symbol"], "4h")]
    logging.info(f"4H scan: {len(sigs)} signals after dedup")
    if sigs:
        log_4h_signals(sigs)
        lines = [f"⚡ *4H Signals* — {time_str}\n"]
        for b in sigs[:5]:
            fno_tag = " `F&O`" if b.get("fno") else ""
            lines.append(
                f"• *{b['symbol']}*{fno_tag} | 4H | BUY ₹{b['price']}\n"
                f"  SL ₹{b['sl']} | T1 ₹{b['target1']} | T2 ₹{b.get('target2','?')} | RR {b['rr']}"
            )
            # Log to unified performance table
            log_to_all_signals(
                b["symbol"], "4h", "BUY", b["price"], b["sl"],
                b["target1"], b.get("target2", b["target1"]), b.get("target2", b["target1"]),
                b["rr"], timeframe="4H", score=int(b.get("score", 0))
            )
        _send("\n".join(lines))
    return sigs


def run_commodity_scan(time_str):
    from scanner import scan_commodities
    from tracker import log_commodity_signals, log_to_all_signals, is_duplicate
    logging.info("Running commodity scan...")
    raw_sigs = scan_commodities()
    # Conflict filter: remove opposing signals for same commodity group
    sigs = _filter_commodity_conflicts(raw_sigs)
    # Dedup per symbol
    sigs = [s for s in sigs if not is_duplicate(s["symbol"], "commodity")]
    logging.info(f"Commodity scan: {len(sigs)} signals (from {len(raw_sigs)} raw)")
    if sigs:
        log_commodity_signals(sigs)
        lines = [f"🥇 *Commodity Signals* — {time_str}\n"]
        for s in sigs:
            arrow = "▲ BUY" if s["action"] == "BUY" else "▼ SELL"
            col   = "📈" if s["action"] == "BUY" else "📉"
            lines.append(
                f"{col} *{s['symbol']}* `{s['timeframe']}` | {arrow} @ {s['price']}\n"
                f"  SL {s['sl']} | T1 {s['target1']} | T2 {s['target2']} | RR {s['rr']}"
            )
            log_to_all_signals(
                s["symbol"], "commodity", s["action"], s["price"], s["sl"],
                s["target1"], s["target2"], s.get("target3", s["target2"]),
                s["rr"], timeframe=s.get("timeframe", "Daily"), score=0
            )
        _send("\n".join(lines))
    return sigs


def run_swing_scan(time_str):
    from scanner import scan_all
    from telegram_bot import send_alert, send_summary, send_top_picks
    from tracker import log_signals, update_outcomes, update_all_outcomes, init_db, log_to_all_signals
    from config import SEND_TOP_PICKS_ONLY

    init_db()
    logging.info("Updating open trade outcomes (swing + all)...")
    update_outcomes()
    update_all_outcomes()

    logging.info("Running swing scan (Nifty 1000)...")
    signals = scan_all()
    logging.info(f"Swing scan: {len(signals)} signals")

    if signals:
        log_signals(signals)
        # Log all to unified performance table
        for s in signals:
            log_to_all_signals(
                s["symbol"], "swing", s.get("action","BUY"), s["price"],
                s.get("sl2", s["price"]*0.96),
                s["target1"], s["target2"], s["target3"],
                s.get("rr2", 0), timeframe="SWING", score=s.get("score", 0),
                metadata={"setup_type": s.get("setup_type")}
            )
        if SEND_TOP_PICKS_ONLY:
            send_top_picks(signals, top_n=5)
        else:
            for s in signals:
                ok = send_alert(s)
                logging.info(f"Alert: {s['symbol']} score={s['score']} ok={ok}")
    send_summary(signals)
    return signals


def run_breakout_scan(time_str):
    from scanner import scan_breakouts
    from tracker import log_breakouts, log_to_all_signals, is_duplicate
    logging.info("Running breakout scan (F&O universe)...")
    all_bos = scan_breakouts()
    # Dedup
    breakouts = [b for b in all_bos if not is_duplicate(b["symbol"], "breakout")]
    logging.info(f"Breakouts: {len(breakouts)} (from {len(all_bos)} raw)")
    if breakouts:
        log_breakouts(breakouts)
        lines = [f"📊 *Breakouts* — {time_str}\n"]
        for b in breakouts[:5]:
            fno_tag  = " `F&O`" if b.get("fno") else ""
            tf_emoji = {"Monthly": "📅", "Weekly": "📆", "Daily": "📋"}.get(b["timeframe"], "📋")
            lines.append(
                f"{tf_emoji} *{b['symbol']}*{fno_tag} | {b['timeframe']} | BUY ₹{b['price']}\n"
                f"  SL ₹{b['sl']} | T1 ₹{b['target1']} | T2 ₹{b['target2']} | RR {b['rr']}"
            )
            log_to_all_signals(
                b["symbol"], "breakout", "BUY", b["price"], b["sl"],
                b["target1"], b["target2"], b.get("target3", b["target2"]),
                b["rr"], timeframe=b["timeframe"], score=0
            )
        _send("\n".join(lines))
    return breakouts


def run_tlm_scan(time_str, interval="4h"):
    """AI Channel Breakout scanner."""
    from scanner import scan_tlm_breakouts
    from tracker import log_breakouts, log_to_all_signals, is_duplicate
    tf_label = "4H" if interval == "4h" else "Daily"
    logging.info(f"Running AI channel breakout scan ({tf_label})...")
    all_sigs = scan_tlm_breakouts(interval=interval)
    tlm_sigs = [s for s in all_sigs if not is_duplicate(s["symbol"], f"ai_{tf_label.lower()}")]
    logging.info(f"AI scan ({tf_label}): {len(tlm_sigs)} (from {len(all_sigs)} raw)")
    if tlm_sigs:
        for s in tlm_sigs:
            s.setdefault("patterns", [s.get("pattern", "AI Channel Breakout")])
            s.setdefault("target3", s.get("target2", 0))
        log_breakouts(tlm_sigs)
        lines = [f"🤖 *AI Signals* ({tf_label}) — {time_str}\n"]
        for b in tlm_sigs[:5]:
            fno_tag = " `F&O`" if b.get("fno") else ""
            lines.append(
                f"• *{b['symbol']}*{fno_tag} | {tf_label} | BUY ₹{b['price']}\n"
                f"  SL ₹{b['sl']} | T1 ₹{b['target1']} | T2 ₹{b['target2']} | RR {b['rr']}"
            )
            log_to_all_signals(
                b["symbol"], f"ai_{tf_label.lower()}", "BUY", b["price"], b["sl"],
                b["target1"], b["target2"], b.get("target3", b["target2"]),
                b["rr"], timeframe=tf_label, score=0
            )
        _send("\n".join(lines))
    return tlm_sigs


def run_fno_alerts(time_str, signals):
    fno_sigs = [s for s in signals if s.get("fno_eligible") and s.get("fno_suggestion")]
    if not fno_sigs:
        return
    lines = [f"🎯 *F&O Setups* — {time_str}\n"]
    for s in fno_sigs[:4]:
        f = s["fno_suggestion"]
        lines.append(
            f"• *{s['symbol']}* {f['direction']} | "
            f"Strike ₹{f.get('use_strike', f['atm_strike'])} | "
            f"Expiry: {f['expiry']} | Hold ~{f.get('hold_days','?')}d | "
            f"Risk ~{f['risk_pts']}pts"
        )
    _send("\n".join(lines))


def run_multibagger_scan(time_str):
    """Weekly multibagger scan — Saturday only."""
    from scanner import scan_multibaggers
    from tracker import log_multibaggers
    logging.info("Running potential multibagger scan (weekly)...")
    mbs = scan_multibaggers(top_n=15)
    logging.info(f"Multibagger scan: {len(mbs)} candidates")
    if mbs:
        log_multibaggers(mbs)
        lines = [
            f"🚀 *Potential Multibaggers* — Weekly Watchlist\n"
            f"_{time_str}_\n"
            f"_(Weekly breakout + momentum + volume expansion)_\n"
        ]
        for i, m in enumerate(mbs[:10], 1):
            fno_tag = " `F&O`" if m.get("fno") else ""
            pe_str  = f" | PE {m['pe']:.0f}x" if m.get("pe") else ""
            lines.append(
                f"{i}. *{m['symbol']}*{fno_tag} | ₹{m['price']}\n"
                f"   T1 ₹{m['target1']} | T2 ₹{m['target2']} | SL ₹{m['sl']}"
                f" | RR {m['rr']}{pe_str}\n"
                f"   _{m['reason']}_"
            )
        lines.append("\n_Horizon: 6–12 months · Not SEBI advice_")
        _send("\n".join(lines))
    return mbs


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    from tracker import log_scan_meta, init_db
    init_db()

    now        = datetime.now(IST)
    today_str  = now.strftime("%Y-%m-%d")
    time_str   = now.strftime("%d %b %Y %I:%M %p IST")
    is_holiday = today_str in NSE_HOLIDAYS
    slot       = _slot(now, is_holiday=is_holiday)
    counts     = {}

    if slot == "none":
        logging.info("Sunday or post-holiday non-morning — no scan.")
        return 0

    logging.info(f"=== Scan started: {time_str} | Slot: {slot} ===")
    _send(f"🔄 *SwingDesk Pro* — {time_str}\n_Slot: {slot.upper()} starting..._")

    try:
        # ── Price alerts FIRST — check all open signals before new scan ─────────
        try:
            run_price_alerts(time_str)
        except Exception as _ae:
            logging.warning(f"price_alerts skipped: {_ae}")

        run_markets(time_str)

        if slot == "morning":
            sigs_4h = run_4h_scan(time_str)
            tlm_4h  = run_tlm_scan(time_str, interval="4h")
            comms   = run_commodity_scan(time_str)
            counts  = {"4h": len(sigs_4h), "ai_4h": len(tlm_4h), "commodities": len(comms)}

        elif slot == "midday":
            signals = run_swing_scan(time_str)
            run_fno_alerts(time_str, signals)
            sigs_4h = run_4h_scan(time_str)
            tlm_4h  = run_tlm_scan(time_str, interval="4h")
            comms   = run_commodity_scan(time_str)
            counts  = {"swing": len(signals), "4h": len(sigs_4h),
                       "ai_4h": len(tlm_4h), "commodities": len(comms)}

        elif slot == "eod":
            breakouts = run_breakout_scan(time_str)
            tlm_daily = run_tlm_scan(time_str, interval="1d")
            signals   = run_swing_scan(time_str)
            comms     = run_commodity_scan(time_str)
            counts    = {"breakouts": len(breakouts), "ai_daily": len(tlm_daily),
                         "swing": len(signals), "commodities": len(comms)}

        elif slot == "weekend":
            # Saturday 9:30 AM — full routine + multibaggers
            sigs_4h   = run_4h_scan(time_str)
            tlm_4h    = run_tlm_scan(time_str, interval="4h")
            signals   = run_swing_scan(time_str)
            run_fno_alerts(time_str, signals)
            breakouts = run_breakout_scan(time_str)
            tlm_daily = run_tlm_scan(time_str, interval="1d")
            comms     = run_commodity_scan(time_str)
            mbs       = run_multibagger_scan(time_str)
            counts    = {
                "4h": len(sigs_4h), "ai_4h": len(tlm_4h),
                "swing": len(signals), "breakouts": len(breakouts),
                "ai_daily": len(tlm_daily), "commodities": len(comms),
                "multibaggers": len(mbs),
            }

        elif slot == "holiday":
            # NSE holiday: single morning scan (markets/commodities/global only)
            comms  = run_commodity_scan(time_str)
            sigs_4h = run_4h_scan(time_str)
            counts  = {"commodities": len(comms), "4h": len(sigs_4h)}
            _send(f"🏛️ *NSE Holiday* ({now.strftime('%d %b %Y')}) — "
                  f"Markets & commodity signals only. Equities resume next trading day.")

        else:  # full (off-hours fallback)
            breakouts = run_breakout_scan(time_str)
            signals   = run_swing_scan(time_str)
            comms     = run_commodity_scan(time_str)
            counts    = {"breakouts": len(breakouts), "swing": len(signals), "commodities": len(comms)}

        log_scan_meta(slot, counts)
        logging.info(f"=== Scan finished: {slot} | {counts} ===")

        # ── Export all JSON for dashboard (always runs, even if no signals) ──
        from tracker import export_signals_json
        export_signals_json()
        logging.info("Signal data exported to data/")

        # ── Slot completion summary (always sends so you know scan ran) ──
        total = sum(counts.values())
        parts = [f"{k.upper()}: {v}" for k, v in counts.items() if v > 0]
        if total == 0:
            _send(
                f"✅ *{slot.upper()} scan complete* — {time_str}\n"
                f"_No qualifying signals. Regime/score/RR filters not met._"
            )
        else:
            _send(
                f"✅ *{slot.upper()} scan done* — {time_str}\n"
                + "\n".join(f"  • {p}" for p in parts)
            )

        return 0

    except Exception as e:
        logging.error(f"SCAN FAILED: {e}", exc_info=True)
        _send(
            f"⚠️ *Scanner Error* ({slot}) — {time_str}\n"
            f"`{str(e)[:300]}`\n_Check GitHub Actions logs._"
        )
        # Still try to export whatever was collected
        try:
            from tracker import export_signals_json
            export_signals_json()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())

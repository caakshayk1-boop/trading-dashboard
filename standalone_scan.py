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
    Full routine scan (all of above) + Potential Multibaggers
    + ai_longterm + magic / magicmagic recovery screens

NSE holidays: scan skipped automatically.

All results logged to signals.db + exported to data/*.json for Streamlit Cloud.
"""
# 3.9 compat, same reason fundamentals.py carries it: CI runs 3.11 where
# `int | None` in an annotation is fine, and this repo is still developed
# against the system 3.9 where it raises at import time.
from __future__ import annotations

import sys, logging, os, math, re
from datetime import datetime, date
import pytz
from symbols import to_yahoo

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

# ── Position lifecycle limits ─────────────────────────────────────────────────
# These are the horizons backtest.py actually measured, not new inventions:
#   PROFILES["cf"]     = {"horizon": 48, "bar_hours": 1}   → 48h  time stop
#   PROFILES["equity"] = {"horizon": 20, "bar_hours": 24}  → 20 bars ≈ 28 days
# The live tracker never implemented a time stop at all, so a 1H forex signal
# filed on 2026-06-03 was still "OPEN" on 2026-07-30 and kept being re-evaluated
# against the current day's range. 386 such rows had accumulated (311 of them
# 1H), which is what produced the 180-alert flood. Same 48-bar / 20-bar
# convention extended to the other timeframes the bot emits.
MAX_HOLD_HOURS = {
    "15M": 6,          # intraday — dead at the close regardless
    "15m": 6,
    "1H":  48,         # backtested cf horizon
    "4H":  48 * 4,     # 48 bars × 4h = 8 days
    "1D":  20 * 24,    # backtested equity horizon ≈ 28 calendar days
    "DAILY": 20 * 24,
    "SWING": 20 * 24,
    "WEEKLY": 20 * 24 * 7,
    # "1W" and "LONG" were MISSING from this table while the ledger wrote both:
    # 56 rows at "1W" and 15 at "LONG". Every one of them fell through to the
    # 20-day default and got time-stopped, which is how 6-to-12-month
    # multibagger ideas ended up EXPIRED after three weeks. GLAND was filed
    # 2026-07-11 as a 6-12 month hold and force-closed 2026-08-11.
    "1W": 20 * 24 * 7,
    "1WK": 20 * 24 * 7,
    "W": 20 * 24 * 7,
    "MONTHLY": 180 * 24,   # hard ceiling — nothing is a live position past 6mo
}

# Horizon by ENGINE, checked BEFORE the timeframe table.
#
# This is the actual fix, and the timeframe aliases above are only the patch to
# the symptom. A hold horizon is a property of the STRATEGY, not of the bar
# interval it was measured on: a multibagger idea and a swing setup can both be
# filed off weekly bars and have nothing in common about how long they live.
# Every one of these rows already declared its own horizon in
# metadata.horizon ("6-12 months") and in signal_type, and the time stop
# ignored both in favour of a lookup on "1W".
ENGINE_MAX_HOLD_HOURS = {
    "multibagger": 365 * 24,        # documented 6-12 month hold; ceiling at 12mo
    "ai_longterm": 3 * 365 * 24,    # documented 2-3 year hold, 200DMA structure stop
    # The Investtech recovery screens. Pinned here rather than left to the
    # metadata.horizon parse, so the horizon survives a row written without
    # metadata — the whole point of resolving by engine first.
    "magic": 365 * 24,              # 3-12 month recovery toward the 52-week high
    "magicmagic": 365 * 24,
}

# Deliberately NOT a number any more. An unknown timeframe used to resolve to
# 20 days, which is why a missing key closed positions instead of raising —
# the failure was silent, wrong and published. Unknown now means "do not time
# stop this", and the run says so once per signal.
_DEFAULT_MAX_HOLD_HOURS = None

# How far through its own stop a fill may be booked before the number is
# treated as a data artifact rather than a real overnight gap. A liquid NSE
# name gapping 4% is a bad morning; 14% is what the ledger recorded for
# BALKRISIND at a price that never traded after the signal existed. Beyond
# this the stop is booked instead, and the discrepancy is logged.
MAX_GAP_SLIP_PCT = 4.0

# A single scan must never be able to send 180 messages. If more alerts than
# this resolve in one run, they are collapsed into one digest. This is the
# backstop that makes the failure mode "one ugly message" instead of a flood,
# independent of whatever causes the pile-up next time.
ALERT_CAP = 8

# Quote units, so a USDJPY alert stops claiming "₹160.3" and AUDUSD stops
# printing "Entry ₹0.7 → T2 ₹0.7" after rounding to one decimal.
# Hand-maintained aliases PLUS the symbols the commodity scanner actually
# writes. The two drifted: this set held "WTI" and "BRENT" while the ledger
# stores WTIUSD and BRNUSD, so every Brent and WTI alert fell through to the
# rupee default — "Entry ₹83.59 → exit ₹90.49" for Brent crude, quoted in
# dollars per barrel. Deriving from COMMODITY_TICKERS means adding a commodity
# can no longer silently redenominate it.
_USD_QUOTED = {"GOLD", "SILVER", "CRUDE", "NATGAS", "NGAS", "XAUUSD", "XAGUSD",
               "WTI", "WTIUSD", "BRENT", "BRNUSD", "COPPER"}
try:
    from scanner import COMMODITY_TICKERS as _COMM
    _USD_QUOTED |= {k.upper() for k in _COMM}
except Exception:  # scanner imports yfinance; never let that break formatting
    pass
_FX_PAIRS   = {"USDJPY", "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCHF",
               "USDCAD", "EURJPY", "GBPJPY"}


def _unit(symbol: str) -> str:
    s = (symbol or "").upper()
    if s in _USD_QUOTED:
        return "$"
    if s in _FX_PAIRS:
        return ""          # a rate, not a money amount
    return "₹"             # NSE equities and the INR pairs (USDINR = ₹/USD)


def _fmt(symbol: str, v: float) -> str:
    """Price with enough precision to be readable. 0.6543, not 0.7."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    a = abs(v)
    dp = 5 if a < 1 else 4 if a < 10 else 2
    return f"{_unit(symbol)}{v:,.{dp}f}"


def _max_hold_hours(timeframe: str, engine: str = "", horizon: str = "") -> int | None:
    """Hours a signal may stay open. None means "horizon unknown, never expire".

    Resolution order, most specific first:
      1. the ENGINE, which is what actually owns the horizon
      2. a horizon string the signal carries about itself ("6-12 months")
      3. the timeframe table
      4. None — refuse to close something whose horizon cannot be established

    Step 4 is the important one. This used to return 20 days for anything it did
    not recognise, so a timeframe spelled "1W" instead of "WEEKLY" silently
    force-closed 6-to-12-month positions and published the result.
    """
    eng = (engine or "").strip().lower()
    if eng in ENGINE_MAX_HOLD_HOURS:
        return ENGINE_MAX_HOLD_HOURS[eng]

    # The record often states its own horizon in prose. Parse the upper bound —
    # "6-12 months" is twelve months, not six, and being generous here only ever
    # keeps a position open longer, which is the safe direction to be wrong in.
    h = (horizon or "").strip().lower()
    if h:
        nums = [int(n) for n in re.findall(r"\d+", h)]
        if nums:
            n = max(nums)
            if "year" in h:
                return n * 365 * 24
            if "month" in h:
                return n * 30 * 24
            if "week" in h:
                return n * 7 * 24
            if "day" in h:
                return n * 24

    return MAX_HOLD_HOURS.get((timeframe or "").upper(), _DEFAULT_MAX_HOLD_HOURS)

# ── NSE Holiday Calendar 2025 ─────────────────────────────────────────────────
NSE_HOLIDAYS = {
    # 2025
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
    # 2026
    "2026-01-26",  # Republic Day
    "2026-03-18",  # Holi
    "2026-04-02",  # Shri Ram Navami
    "2026-04-03",  # Good Friday
    "2026-04-06",  # Dr. Baba Saheb Ambedkar Jayanti
    "2026-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-08-15",  # Independence Day
    "2026-10-02",  # Gandhi Jayanti
    "2026-10-19",  # Diwali Laxmi Puja
    "2026-10-20",  # Diwali Balipratipada
    "2026-11-04",  # Prakash Gurpurb
    "2026-12-25",  # Christmas Day
}


# Reason the last _send() failed, for mark_alerts_sent(). Module-level so the
# 17 _send() call sites keep their one-argument signature.
_LAST_SEND_ERROR = None

# Telegram hard-caps a message at 4096 chars. Leave headroom for the header.
_TG_MAX_CHARS = 3800


def _send(msg):
    """Send to Telegram. Returns True only if Telegram accepted the message.

    The return value used to be discarded and exceptions swallowed, so a 400 or
    a 429 was indistinguishable from a successful send. Every caller that
    records delivery must now branch on this.
    """
    global _LAST_SEND_ERROR
    try:
        from telegram_bot import _post
        ok = bool(_post(msg))
        _LAST_SEND_ERROR = None if ok else "telegram API rejected the message"
        if not ok:
            logging.error("Telegram send rejected — see _post log above")
        return ok
    except Exception as e:
        _LAST_SEND_ERROR = f"{type(e).__name__}: {e}"
        logging.error(f"Telegram send failed: {e}")
        return False


def _send_chunked(header, blocks, footer=None):
    """Send `blocks` across as many messages as Telegram's size limit needs.

    Returns a list of per-block booleans, aligned to `blocks`. Callers previously
    truncated their block list to 5 to stay under the limit, which silently
    dropped the rest of the scan; the limit is characters, not signals.

    Per-block rather than a single bool because a partial failure is real: if
    chunk 2 of 3 is rejected, the signals in chunks 1 and 3 *were* delivered and
    must not be recorded as unsent.
    """
    if not blocks:
        return []
    chunks, cur, cur_len = [], [], len(header)
    for i, b in enumerate(blocks):
        if cur and cur_len + len(b) + 1 > _TG_MAX_CHARS:
            chunks.append(cur)
            cur, cur_len = [], len(header)
        cur.append((i, b))
        cur_len += len(b) + 1
    if cur:
        chunks.append(cur)

    sent = [False] * len(blocks)
    for n, chunk in enumerate(chunks, 1):
        head = header if len(chunks) == 1 else f"{header} `({n}/{len(chunks)})`"
        parts = [head] + [b for _i, b in chunk]
        if footer and n == len(chunks):
            parts.append(footer)
        ok = _send("\n".join(parts))
        for idx, _b in chunk:
            sent[idx] = ok
    delivered = sum(sent)
    if delivered != len(blocks):
        logging.error(f"Telegram: {len(blocks) - delivered}/{len(blocks)} signals "
                      f"NOT delivered across {len(chunks)} chunk(s)")
    return sent


def _record_delivery(ids, sent_flags, mark_fn):
    """Persist the real per-signal delivery outcome."""
    if not ids:
        return
    flags = list(sent_flags) + [False] * (len(ids) - len(sent_flags))
    ok_ids   = [i for i, s in zip(ids, flags) if s]
    fail_ids = [i for i, s in zip(ids, flags) if not s]
    if ok_ids:
        mark_fn(ok_ids, True)
    if fail_ids:
        mark_fn(fail_ids, False, _LAST_SEND_ERROR or "telegram send failed")


# ── Price Alert Monitor (checks open signals against live prices) ─────────────
def _bar_window(tf: str, age_hours: float):
    """(period, interval) for yfinance covering this signal's life so far."""
    tfu = (tf or "").upper()
    if tfu in ("15M", "1H", "4H"):
        days = max(2, int(age_hours / 24) + 2)
        # yfinance caps 1h history at 730d and 15m at 60d; both are far beyond
        # any live hold under MAX_HOLD_HOURS.
        return f"{min(days, 60)}d", ("15m" if tfu == "15M" else "1h")
    days = max(5, int(age_hours / 24) + 5)
    return f"{min(days, 365)}d", "1d"


def _since_entry(tick, opened_at):
    """Slice bars to those strictly after the signal was filed.

    A level only counts if price traded there while we were in the trade.

    FAILS CLOSED. The previous version returned the whole fetched frame
    whenever it could not bound the window — no timestamp, or a tz-naive index
    — and that silent fallback is what produced phantom stop-outs:

        HINDALCO, signalled 2026-08-07 with a stop at 1013.89, was booked
        SL_HIT at 990.00. That is precisely the OPEN of 2026-08-03, four days
        before the signal existed. Price never traded at 990 after the signal
        at all. 45 rows across the ledger are stopped past their own stop this
        way, the worst at -6.58R, and capping them at the -1R a stop actually
        pays moves measured expectancy from +0.090R to +0.222R over 515 trades.

    Returning an empty frame leaves the signal OPEN for the next run, which is
    the honest outcome when we cannot say what happened. Grading a trade
    against bars that predate it is never better than grading it later.
    """
    if opened_at is None:
        return tick.iloc[0:0]

    idx = tick.index
    try:
        if getattr(idx, "tz", None) is None:
            # Naive index: compare on calendar date instead of dropping the
            # bound entirely. Daily bars from yfinance arrive naive often
            # enough that discarding them outright would stall grading.
            cutoff = opened_at.date() if hasattr(opened_at, "date") else opened_at
            return tick[[d.date() > cutoff for d in idx]]
        return tick[idx > opened_at]
    except Exception:
        # Cannot bound it -> refuse to grade it.
        return tick.iloc[0:0]


def run_price_alerts(time_str: str):
    """
    Position management for every OPEN signal in all_signals.

    Rules, in priority order per signal:
      time stop → EXPIRED, real R booked at last close (matches backtest.py)
      SL_HIT    → exit; if the bar gapped through the stop, book the gap
      T2_HIT    → full exit
      T1_HIT    → partial; stays OPEN and keeps trailing for T2
      SL1_WARN  → one warning per signal, ever (flagged in alert_flags)

    Everything is evaluated only on bars printed after the signal was filed, and
    the whole run is capped at ALERT_CAP messages — past that it sends one digest.
    """
    import yfinance as yf
    import pandas as pd
    import json as _json
    from datetime import timedelta

    try:
        from tracker import _conn, init_db
        init_db()
        with _conn() as c:
            open_df = pd.read_sql(
                "SELECT * FROM all_signals WHERE status IN ('OPEN','T1_HIT') "
                "ORDER BY date DESC", c)
    except Exception as e:
        logging.warning(f"price_alerts: DB read failed: {e}")
        return

    if open_df.empty:
        logging.info("price_alerts: no open signals to check")
        return

    now = datetime.now(IST)
    logging.info(f"price_alerts: {len(open_df)} open signals to evaluate")

    updates = []   # (new_status, exit_price, pnl_pct, r_mult, sig_id)
    flags   = []   # (flag_string, sig_id)
    alerts  = []   # (kind, symbol, message) — sent after the cap check
    expired = 0

    def _opened_at(row):
        """Signal fill time in IST, from sent_at (preferred) or date.

        sent_at is IST-aware for anything filed after the tracker timezone fix;
        rows written before it hold a naive UTC stamp, which this reads as IST.
        That makes them look 5h30m younger than they are — irrelevant against a
        48h time stop, and those rows are voided by reconcile_positions anyway.
        """
        for key in ("sent_at", "date"):
            raw = row.get(key)
            if raw in (None, ""):
                continue
            txt = str(raw)
            try:
                dt = datetime.fromisoformat(txt)
                return dt if dt.tzinfo else IST.localize(dt)
            except ValueError:
                pass
            for f, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%dT%H:%M:%S", 19),
                         ("%Y-%m-%d", 10)):
                try:
                    return IST.localize(datetime.strptime(txt[:n], f))
                except ValueError:
                    continue
        return None

    for _, row in open_df.iterrows():
        sym = row["symbol"]
        try:
            sig_id = int(row["id"])
            tf     = row.get("timeframe") or "SWING"
            action = str(row.get("action", "BUY")).upper()
            entry  = float(row["entry"])
            sl     = float(row["sl"] or entry * 0.95)
            t1     = float(row["target1"])
            t2     = float(row.get("target2") or t1 * 1.04)
            meta   = _json.loads(row.get("metadata") or "{}")
            sl1_v  = float(meta.get("sl1") or entry * 0.97)
            seen   = str(row.get("alert_flags") or "")
            buy    = action != "SELL"
            risk   = abs(entry - sl) or 1.0

            opened_at = _opened_at(row)
            # Horizon from the signal itself — engine first, then its own stated
            # horizon, then the timeframe. See _max_hold_hours.
            limit_h = _max_hold_hours(tf,
                                      engine=str(row.get("signal_type") or "")
                                             or str(meta.get("engine") or ""),
                                      horizon=str(meta.get("horizon") or ""))
            if limit_h is None:
                # Unknown horizon: manage the levels, never time-stop it. The old
                # code defaulted to 20 days here and closed real positions.
                logging.warning(f"price_alerts {sym}: no horizon for tf={tf!r} "
                                f"engine={row.get('signal_type')!r} — levels only, "
                                f"no time stop")
            # An unknown open time used to be treated as "past the limit", i.e.
            # expire it immediately. With no limit that is meaningless, so an
            # undated signal is simply not aged.
            age_h = ((now - opened_at).total_seconds() / 3600.0
                     if opened_at else (0.0 if limit_h is None else limit_h + 1))

            # limit_h None means no time stop, so the bar window is sized on age
            # alone. min(x, None) is a TypeError in py3.
            period, interval = _bar_window(
                tf, age_h if limit_h is None else min(age_h, limit_h))
            tick = yf.download(to_yahoo(sym), period=period, interval=interval,
                               progress=False, auto_adjust=True, timeout=8)
            if tick is None or tick.empty:
                logging.debug(f"price_alerts {sym}: no data")
                continue

            last_close = float(tick["Close"].squeeze().iloc[-1])

            # ── Time stop ────────────────────────────────────────────────────
            # Book the real R at the last close, exactly as backtest.py does.
            # Dropping unresolved trades instead would bias the ledger toward
            # fast movers and inflate the measured edge.
            if limit_h is not None and age_h > limit_h:
                r_m = ((last_close - entry) / risk) if buy else ((entry - last_close) / risk)
                pnl = ((last_close - entry) / entry * 100) * (1 if buy else -1)
                updates.append(("EXPIRED", round(last_close, 4), round(pnl, 2),
                                round(r_m, 2), sig_id))
                expired += 1
                logging.info(f"TIME STOP: {sym} {tf} age={age_h:.0f}h "
                             f"limit={limit_h}h r={r_m:+.2f}")
                continue

            win = _since_entry(tick, opened_at)
            if win.empty:
                continue

            hi = float(win["High"].squeeze().max())
            lo = float(win["Low"].squeeze().min())

            if buy:
                sl_hit  = lo <= sl
                t2_hit  = hi >= t2 and not sl_hit
                t1_hit  = hi >= t1 and not sl_hit
                sl1_hit = lo <= sl1_v and lo > sl
            else:
                sl_hit  = hi >= sl
                t2_hit  = lo <= t2 and not sl_hit
                t1_hit  = lo <= t1 and not sl_hit
                sl1_hit = hi >= sl1_v and hi < sl

            if sl_hit:
                # If a bar opened beyond the stop we did not get filled at the
                # stop. Booking a flat -1.0R every time hid real gap risk.
                #
                # `win` is now guaranteed post-entry (see _since_entry), so the
                # bar this reads can no longer predate the signal. A second
                # guard below refuses anything that still looks impossible: a
                # genuine overnight gap in a liquid name is a few percent, not
                # fourteen, and BALKRISIND was booked 14.29% through its stop
                # at a price that never traded after the signal.
                try:
                    opens = win["Open"].squeeze()
                    lows  = win["Low"].squeeze()
                    highs = win["High"].squeeze()
                    trig  = (lows <= sl) if buy else (highs >= sl)
                    gap_o = float(opens[trig].iloc[0])
                except Exception:
                    gap_o = sl
                exit_p = min(sl, gap_o) if buy else max(sl, gap_o)

                # Sanity bound. Beyond this the number is far likelier to be a
                # data artifact than a real gap, and a fabricated -5R poisons
                # expectancy far more than an under-reported one.
                slip_pct = abs(exit_p - sl) / sl * 100 if sl else 0.0
                if slip_pct > MAX_GAP_SLIP_PCT:
                    logging.warning(
                        f"{sym}: exit {exit_p} is {slip_pct:.1f}% through stop {sl} "
                        f"— implausible, booking the stop instead")
                    exit_p = sl
                r_m = ((exit_p - entry) / risk) if buy else ((entry - exit_p) / risk)
                pnl = ((exit_p - entry) / entry * 100) * (1 if buy else -1)
                gap_note = "" if abs(exit_p - sl) < 1e-9 else "  ⚠ gapped through stop"
                updates.append(("SL_HIT", round(exit_p, 4), round(pnl, 2),
                                round(r_m, 2), sig_id))
                alerts.append(("SL", sym,
                    f"🛑 *SL HIT — {sym}* | {tf}\n"
                    f"Entry {_fmt(sym, entry)} → exit {_fmt(sym, exit_p)}{gap_note}\n"
                    f"P&L: `{pnl:+.2f}%` | R: `{r_m:+.2f}`\n"
                    f"_Exit trade. Review thesis before re-entry._"))

            elif t2_hit:
                r_m = abs(t2 - entry) / risk
                pnl = ((t2 - entry) / entry * 100) * (1 if buy else -1)
                updates.append(("T2_HIT", round(t2, 4), round(pnl, 2),
                                round(r_m, 2), sig_id))
                alerts.append(("T2", sym,
                    f"🎯🎯 *TARGET 2 HIT — {sym}* | {tf}\n"
                    f"Entry {_fmt(sym, entry)} → T2 {_fmt(sym, t2)}\n"
                    f"Gain: `{pnl:+.2f}%` | `{r_m:.2f}R` ✅\n"
                    f"_Full exit._"))

            elif t1_hit and "T1" not in seen:
                # Stays OPEN so it can still trail to T2 — the previous code set
                # status='T1_HIT', which fell out of the OPEN filter and froze
                # 24 signals there permanently.
                r_m = abs(t1 - entry) / risk
                pnl = ((t1 - entry) / entry * 100) * (1 if buy else -1)
                flags.append((seen + "T1;", sig_id))
                alerts.append(("T1", sym,
                    f"✅ *TARGET 1 HIT — {sym}* | {tf}\n"
                    f"Entry {_fmt(sym, entry)} → T1 {_fmt(sym, t1)}\n"
                    f"Gain: `{pnl:+.2f}%` | `{r_m:.2f}R` ✓\n"
                    f"_Book 50% · SL to entry · trail for T2 {_fmt(sym, t2)}_"))

            elif sl1_hit and "SL1" not in seen:
                # One warning per signal for its whole life. This never wrote to
                # the DB before, so it re-fired on every single scan.
                flags.append((seen + "SL1;", sig_id))
                chg = (last_close - entry) / entry * 100 * (1 if buy else -1)
                alerts.append(("SL1", sym,
                    f"⚠️ *SL1 WARNING — {sym}* | {tf}\n"
                    f"Price {_fmt(sym, last_close)} breached warning SL {_fmt(sym, sl1_v)}\n"
                    f"Change: `{chg:+.2f}%` | Final SL: {_fmt(sym, sl)}\n"
                    f"_Tighten or exit half. Watch closely._"))

        except Exception as e:
            logging.warning(f"price_alerts {sym}: {e}")
            continue

    # ── Send: individually up to the cap, one digest beyond it ────────────────
    if len(alerts) <= ALERT_CAP:
        for _kind, _sym, msg in alerts:
            _send(msg)
    else:
        counts = {}
        for kind, _sym, _m in alerts:
            counts[kind] = counts.get(kind, 0) + 1
        label = {"SL": "stopped out", "T2": "hit T2", "T1": "hit T1",
                 "SL1": "SL1 warnings"}
        lines = [f"📋 *Position update* — {time_str}",
                 f"_{len(alerts)} levels resolved this scan — digest, not {len(alerts)} messages._\n"]
        for k in ("T2", "T1", "SL", "SL1"):
            if counts.get(k):
                syms = [s for kk, s, _ in alerts if kk == k]
                lines.append(f"• *{counts[k]} {label[k]}*: {', '.join(syms[:12])}"
                             + ("…" if len(syms) > 12 else ""))
        lines.append("\n_Full detail in the EOD ledger._")
        _send("\n".join(lines))
        logging.warning(f"price_alerts: {len(alerts)} alerts collapsed into digest "
                        f"(cap {ALERT_CAP})")

    # ── Persist ──────────────────────────────────────────────────────────────
    try:
        from tracker import _conn
        from tracker import _now_ist
        with _conn() as c:
            for new_status, exit_p, pnl, r_m, sig_id in updates:
                c.execute(
                    "UPDATE all_signals SET status=?, exit_price=?, pnl_pct=?, "
                    "r_multiple=?, closed_at=? WHERE id=? AND status IN ('OPEN','T1_HIT')",
                    (new_status, exit_p, pnl, r_m, _now_ist(), sig_id))
            for flag, sig_id in flags:
                c.execute("UPDATE all_signals SET alert_flags=? WHERE id=?",
                          (flag, sig_id))
            c.commit()
        logging.info(f"price_alerts: {len(updates)} closed ({expired} on time stop), "
                     f"{len(flags)} flagged")
    except Exception as e:
        logging.warning(f"price_alerts: DB update failed: {e}")

    logging.info(f"price_alerts: done | {len(alerts)} alerts | {len(updates)} closed")


def _quality_fields(sig, extra_meta=None):
    """Gate verdict → the columns log_batch_to_all_signals persists.

    Carrying the grade into the ledger is what makes the v1/v2 comparison
    possible later: without it every new row looks identical to an old one and
    "did the gate help?" becomes unanswerable.
    """
    meta = dict(extra_meta or {})
    q = sig.get("quality")
    if q is not None:
        meta.update(q.as_metadata())
    return {
        "grade":        sig.get("grade"),
        "breakeven_wr": sig.get("breakeven_wr"),
        "turnover_cr":  sig.get("turnover_cr"),
        "metadata":     meta,
    }


VALID_SLOTS = {"morning", "midday", "eod", "weekend", "holiday", "full", "none"}


def _requested_slot(argv=None):
    """Slot named explicitly on the command line, or None.

    GitHub's scheduled runs are routinely delayed — measured on this repo:
    the 06:15Z cron fired at 09:17Z and the 11:00Z cron at 12:48Z on
    2026-07-31, and the Saturday 04:00Z cron at 06:27Z on 2026-08-01. Those
    delays land at 14:47 and 18:18 IST, which fall in the gaps between the
    _slot() windows (9–10:30, 11–14, 15–18) and silently drop into the "full"
    off-hours fallback. "full" skips the measured equity scan and the signal
    ledger, so a late EOD cron quietly stopped logging outcomes.

    daily_scan.yml now passes --slot per cron entry, so intent survives the
    delay. The clock stays as the fallback for manual and ad-hoc runs.
    """
    argv = sys.argv[1:] if argv is None else argv
    for i, a in enumerate(argv):
        val = None
        if a.startswith("--slot="):
            val = a.split("=", 1)[1]
        elif a == "--slot" and i + 1 < len(argv):
            val = argv[i + 1]
        if val:
            val = val.strip().lower()
            if val not in VALID_SLOTS:
                raise SystemExit(
                    f"--slot {val!r} is not one of: {', '.join(sorted(VALID_SLOTS))}")
            return val
    return None


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
    try:
        from scanner import fetch_forex_comm
        fc = fetch_forex_comm()
        if not fc:
            return
        lines = [f"🌐 *Markets* — {time_str}\n"]
        for r in fc:
            sign  = "+" if r["Chg%"] >= 0 else ""
            arrow = "▲" if r["Chg%"] >= 0 else "▼"
            # Flag when a metal fell back to the futures contract: that
            # percentage can carry a contract-roll gap and is not comparable
            # with the spot price beside it.
            note = "  ⚠ futures basis" if r.get("Basis") == "futures" else ""
            lines.append(f"{arrow} *{r['Asset']}*: `{r['Last']}` ({sign}{r['Chg%']}%){note}")
        _send("\n".join(lines))
    except Exception as e:
        logging.warning(f"run_markets skipped: {e}")


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
    from tracker import (log_4h_signals, log_batch_to_all_signals,
                         duplicate_symbols, mark_alerts_sent)
    logging.info("Running 4H RSI-55 scan...")
    raw = scan_4h()
    # Dedup: skip already-alerted symbols
    dupes = duplicate_symbols(raw, "4h")
    sigs = [s for s in raw if str(s["symbol"]).replace(".NS", "") not in dupes]
    logging.info(f"4H scan: {len(sigs)} to alert ({len(raw)} raw → {len(dupes)} deduped out)")
    if sigs:
        log_4h_signals(sigs)
        blocks, rows = [], []
        for b in sigs:
            fno_tag = " `F&O`" if b.get("fno") else ""
            blocks.append(
                f"• *{b['symbol']}*{fno_tag} | 4H | BUY ₹{b['price']}\n"
                f"  SL ₹{b['sl']} | T1 ₹{b['target1']} | T2 ₹{b.get('target2','?')} | RR {b['rr']}"
            )
            rows.append({
                "symbol": b["symbol"], "signal_type": "4h", "action": "BUY",
                "entry": b["price"], "sl": b["sl"], "t1": b["target1"],
                "t2": b.get("target2", b["target1"]), "t3": b.get("target2", b["target1"]),
                "rr": b["rr"], "timeframe": "4H", "score": int(b.get("score", 0)),
                **_quality_fields(b),
            })
        ids  = log_batch_to_all_signals(rows)
        sent = _send_chunked(f"⚡ *4H Signals* ({len(sigs)}) — {time_str}\n", blocks)
        _record_delivery(ids, sent, mark_alerts_sent)


def run_ohl_scan(time_str):
    """OLL (Open≈Low, bullish) pattern — ported from trade.askakshay.com's
    OHL/OLL scanner (see scanner.analyze_ohl's docstring for why only the
    bullish half is published). Same shape as run_4h_scan() — a same-day
    pattern, so it belongs in the daily cron, not its own workflow."""
    from scanner import scan_ohl
    from tracker import (log_batch_to_all_signals, duplicate_symbols, mark_alerts_sent)
    logging.info("Running OHL/OLL scan...")
    raw = scan_ohl()
    dupes = duplicate_symbols(raw, "ohl")
    sigs = [s for s in raw if str(s["symbol"]).replace(".NS", "") not in dupes]
    logging.info(f"OHL/OLL scan: {len(sigs)} to alert ({len(raw)} raw → {len(dupes)} deduped out)")
    if sigs:
        blocks, rows = [], []
        for b in sigs:
            fno_tag = " `F&O`" if b.get("fno") else ""
            blocks.append(
                f"• *{b['symbol']}*{fno_tag} | OLL | BUY ₹{b['price']}\n"
                f"  SL ₹{b['sl']} | T1 ₹{b['target1']} | T2 ₹{b['target2']} | RR {b['rr']}"
            )
            rows.append({
                "symbol": b["symbol"], "signal_type": "ohl", "action": "BUY",
                "entry": b["price"], "sl": b["sl"], "t1": b["target1"],
                "t2": b["target2"], "t3": b.get("target3", b["target2"]),
                "rr": b["rr"], "timeframe": "1D", "score": 0,
                **_quality_fields(b),
            })
        ids  = log_batch_to_all_signals(rows)
        sent = _send_chunked(f"📐 *OLL Signals* ({len(sigs)}) — {time_str}\n", blocks)
        _record_delivery(ids, sent, mark_alerts_sent)
    return sigs


def run_commodity_scan(time_str):
    from scanner import scan_commodities
    from tracker import (log_commodity_signals, log_batch_to_all_signals,
                         duplicate_symbols, mark_alerts_sent)
    logging.info("Running commodity scan...")
    raw_sigs = scan_commodities()
    # Conflict filter: remove opposing signals for same commodity group
    filtered = _filter_commodity_conflicts(raw_sigs)
    # Dedup per symbol
    dupes = duplicate_symbols(filtered, "commodity")
    sigs = [s for s in filtered if str(s["symbol"]).replace(".NS", "") not in dupes]
    logging.info(f"Commodity scan: {len(sigs)} to alert ({len(raw_sigs)} raw → "
                 f"{len(raw_sigs) - len(filtered)} conflicts → {len(dupes)} deduped out)")
    if sigs:
        log_commodity_signals(sigs)
        blocks, rows = [], []
        for s in sigs:
            arrow = "▲ BUY" if s["action"] == "BUY" else "▼ SELL"
            col   = "📈" if s["action"] == "BUY" else "📉"
            blocks.append(
                f"{col} *{s['symbol']}* `{s['timeframe']}` | {arrow} @ {s['price']}\n"
                f"  SL {s['sl']} | T1 {s['target1']} | T2 {s['target2']} | RR {s['rr']}"
            )
            rows.append({
                "symbol": s["symbol"], "signal_type": "commodity", "action": s["action"],
                "entry": s["price"], "sl": s["sl"], "t1": s["target1"],
                "t2": s["target2"], "t3": s.get("target3", s["target2"]),
                "rr": s["rr"], "timeframe": s.get("timeframe", "Daily"), "score": 0,
            })
        ids  = log_batch_to_all_signals(rows)
        sent = _send_chunked(f"🥇 *Commodity Signals* ({len(sigs)}) — {time_str}\n", blocks)
        _record_delivery(ids, sent, mark_alerts_sent)
    return sigs


def run_swing_scan(time_str):
    from scanner import scan_all
    from telegram_bot import send_alert, send_summary, send_top_picks
    from tracker import (log_signals, update_outcomes, update_all_outcomes, init_db,
                         log_batch_to_all_signals, mark_alerts_sent)
    try:
        from config import SEND_TOP_PICKS_ONLY
    except (ImportError, ModuleNotFoundError):
        SEND_TOP_PICKS_ONLY = os.environ.get("SEND_TOP_PICKS_ONLY", "false").lower() == "true"

    init_db()
    logging.info("Updating open trade outcomes (swing + all)...")
    update_outcomes()
    update_all_outcomes()

    logging.info("Running swing scan (Nifty 500)...")
    signals = scan_all()
    logging.info(f"Swing scan: {len(signals)} signals")

    if signals:
        log_signals(signals)
        # Log all to unified performance table
        ids = log_batch_to_all_signals([{
            "symbol": s["symbol"], "signal_type": "swing",
            "action": s.get("action", "BUY"), "entry": s["price"],
            "sl": s.get("sl2", s["price"] * 0.96), "t1": s["target1"],
            "t2": s["target2"], "t3": s["target3"], "rr": s.get("rr2", 0),
            "timeframe": "SWING", "score": s.get("score", 0),
            **_quality_fields(s, {"setup_type": s.get("setup_type")}),
        } for s in signals])
        # send_alert() drops anything under score 65, so most swing rows are
        # logged but never pushed. That is a threshold decision, not a delivery
        # failure — record it as such instead of leaving the row looking sent.
        if SEND_TOP_PICKS_ONLY:
            top_ids = [i for i, s in zip(ids, signals) if int(s.get("score", 0)) >= 65][:5]
            skipped = [i for i in ids if i not in top_ids]
            ok = bool(send_top_picks(signals, top_n=5))
            mark_alerts_sent(top_ids, ok, "telegram send failed")
            mark_alerts_sent(skipped, False, "not alerted: outside top 5 A/A+ picks")
        else:
            # Group the outcomes so the whole scan costs 3 UPDATEs, not one per
            # signal — mark_alerts_sent takes a list for exactly this reason.
            delivered, below_thresh, failed = [], [], []
            for sig_id, s in zip(ids, signals):
                score = int(s.get("score", 0))
                ok = send_alert(s)
                logging.info(f"Alert: {s['symbol']} score={score} ok={ok}")
                (delivered if ok else below_thresh if score < 65 else failed).append(sig_id)
            mark_alerts_sent(delivered, True)
            mark_alerts_sent(below_thresh, False, "not alerted: score < 65 (below A/A+)")
            mark_alerts_sent(failed, False, "telegram send failed or direction-locked")
    send_summary(signals)
    return signals


def run_measured_equity_scan(time_str):
    """Equity signals from the backtested engine (equity_engine / cf_engine core).

    Unlike run_swing_scan, these targets come from real swing structure and the
    R:R is measured off them rather than being a fixed multiple of the stop.

    EOD ONLY. The backtest evaluated on completed daily closes — 459 trades,
    45.8% win, +0.171R expectancy over 5y on 30 liquid names. Running this on a
    half-formed intraday bar would not be the same strategy and the measured
    expectancy would not carry over.
    """
    import equity_engine
    from cf_engine import format_alert
    from tracker import log_batch_to_all_signals, init_db, mark_alerts_sent

    init_db()
    logging.info("Running measured equity scan (EOD, daily close)...")
    signals = equity_engine.scan(horizon="swing")
    logging.info(f"Measured equity scan: {len(signals)} signal(s)")
    if not signals:
        return []

    # Log before sending so the ledger holds every signal even if Telegram is
    # down, then record the real delivery outcome against those same rows.
    try:
        ids = log_batch_to_all_signals([{
            "symbol": s["name"], "signal_type": "equity_measured", "action": s["bias"],
            "entry": s["price"], "sl": s["sl"], "t1": s["t1"], "t2": s["t2"],
            "t3": s["t3"], "rr": s["rr"], "timeframe": "1D", "score": s["score"],
            "metadata": {"rsi_4h": s["rsi_4h"], "vol_ratio": s["vol_ratio"],
                         "target_source": s["target_source"],
                         "sl_atr_mult": s["sl_atr_mult"]},
        } for s in signals])
    except Exception as e:
        logging.error(f"measured equity DB log failed: {e}")
        ids = []

    sent = _send_chunked(
        f"\U0001F4CF *Measured Equity Signals* ({len(signals)}) — {time_str}\n"
        "_Daily close · weekly regime · structural targets_\n",
        [format_alert(s) for s in signals],
        footer="\n_Backtested +0.171R/trade · not SEBI advice_")
    _record_delivery(ids, sent, mark_alerts_sent)
    return signals


def run_signal_ledger(time_str, days: int = 30):
    """Post the alert ledger to Telegram and mirror it into the Obsidian vault.

    Everything the bot sent, with its measured outcome in R. This is the live
    counterpart to backtest.py — if the two diverge, the config is overfit.
    """
    import signal_report
    rep = signal_report.send(days=days, telegram=True, obsidian=True)
    logging.info(f"Ledger: {rep['closed']} closed, {rep['open']} open, "
                 f"expectancy {rep['overall']['expectancy']:+.3f}R")
    return rep


def run_breakout_scan(time_str):
    from scanner import scan_breakouts
    from tracker import (log_breakouts, log_batch_to_all_signals,
                         duplicate_symbols, mark_alerts_sent)
    logging.info("Running breakout scan (F&O universe)...")
    raw_bos = scan_breakouts()
    # Drop NaN signals (yfinance sometimes returns NaN for price/ATR)
    def _valid(b):
        for k in ("price", "sl", "target1", "target2", "rr"):
            v = b.get(k)
            try:
                if v is None or math.isnan(float(v)):
                    return False
            except (TypeError, ValueError):
                return False
        return True
    all_bos = [b for b in raw_bos if _valid(b)]
    if len(all_bos) != len(raw_bos):
        # This filter used to drop signals with no trace of how many.
        bad = [b.get("symbol", "?") for b in raw_bos if not _valid(b)]
        logging.warning(f"Breakouts: dropped {len(bad)} with NaN/missing prices — "
                        f"{', '.join(bad[:10])}{'…' if len(bad) > 10 else ''}")
    dupes = duplicate_symbols(all_bos, "breakout")
    breakouts = [b for b in all_bos if str(b["symbol"]).replace(".NS", "") not in dupes]
    logging.info(f"Breakouts: {len(breakouts)} to alert "
                 f"({len(raw_bos)} raw → {len(all_bos)} valid → {len(dupes)} deduped out)")
    if breakouts:
        log_breakouts(breakouts)
        # Every deduped breakout is logged and alerted. This used to be
        # `breakouts[:5]`, which capped BOTH the message and the DB write — on
        # 2026-07-31 that turned 53 breakouts into 5 rows and silently discarded
        # the other 48 from the ledger as well as from Telegram.
        blocks, rows = [], []
        for b in breakouts:
            fno_tag  = " `F&O`" if b.get("fno") else ""
            tf_emoji = {"Monthly": "📅", "Weekly": "📆", "Daily": "📋"}.get(b["timeframe"], "📋")
            blocks.append(
                f"{tf_emoji} *{b['symbol']}*{fno_tag} | {b['timeframe']} | BUY ₹{b['price']}\n"
                f"  SL ₹{b['sl']} | T1 ₹{b['target1']} | T2 ₹{b['target2']} | RR {b['rr']}"
            )
            rows.append({
                "symbol": b["symbol"], "signal_type": "breakout", "action": "BUY",
                "entry": b["price"], "sl": b["sl"], "t1": b["target1"],
                "t2": b["target2"], "t3": b.get("target3", b["target2"]),
                "rr": b["rr"], "timeframe": b["timeframe"], "score": 0,
                **_quality_fields(b),
            })
        # One connection for the whole batch — see log_batch_to_all_signals.
        ids = log_batch_to_all_signals(rows)
        sent = _send_chunked(f"📊 *Breakouts* ({len(breakouts)}) — {time_str}\n", blocks)
        _record_delivery(ids, sent, mark_alerts_sent)
    return breakouts


def run_tlm_scan(time_str, interval="4h"):
    """AI Channel Breakout scanner."""
    from scanner import scan_tlm_breakouts
    from tracker import (log_breakouts, log_batch_to_all_signals,
                         duplicate_symbols, mark_alerts_sent)
    tf_label = "4H" if interval == "4h" else "Daily"
    sig_type = f"ai_{tf_label.lower()}"
    logging.info(f"Running AI channel breakout scan ({tf_label})...")
    all_sigs = scan_tlm_breakouts(interval=interval)
    dupes = duplicate_symbols(all_sigs, sig_type)
    tlm_sigs = [s for s in all_sigs if str(s["symbol"]).replace(".NS", "") not in dupes]
    logging.info(f"AI scan ({tf_label}): {len(tlm_sigs)} to alert "
                 f"({len(all_sigs)} raw → {len(dupes)} deduped out)")
    if tlm_sigs:
        for s in tlm_sigs:
            s.setdefault("patterns", [s.get("pattern", "AI Channel Breakout")])
            s.setdefault("target3", s.get("target2", 0))
        log_breakouts(tlm_sigs)
        blocks, rows = [], []
        for b in tlm_sigs:
            fno_tag = " `F&O`" if b.get("fno") else ""
            blocks.append(
                f"• *{b['symbol']}*{fno_tag} | {tf_label} | BUY ₹{b['price']}\n"
                f"  SL ₹{b['sl']} | T1 ₹{b['target1']} | T2 ₹{b['target2']} | RR {b['rr']}"
            )
            rows.append({
                "symbol": b["symbol"], "signal_type": sig_type, "action": "BUY",
                "entry": b["price"], "sl": b["sl"], "t1": b["target1"],
                "t2": b["target2"], "t3": b.get("target3", b["target2"]),
                "rr": b["rr"], "timeframe": tf_label, "score": 0,
            })
        ids  = log_batch_to_all_signals(rows)
        sent = _send_chunked(
            f"🤖 *AI Signals* ({tf_label}, {len(tlm_sigs)}) — {time_str}\n", blocks)
        _record_delivery(ids, sent, mark_alerts_sent)
    return tlm_sigs


def run_fno_alerts(time_str, signals):
    fno_sigs = [s for s in signals if s.get("fno_eligible") and s.get("fno_suggestion")]
    if not fno_sigs:
        return
    blocks = []
    for s in fno_sigs:
        f = s["fno_suggestion"]
        blocks.append(
            f"• *{s['symbol']}* {f['direction']} | "
            f"Strike ₹{f.get('use_strike', f['atm_strike'])} | "
            f"Expiry: {f['expiry']} | Hold ~{f.get('hold_days','?')}d | "
            f"Risk ~{f['risk_pts']}pts"
        )
    _send_chunked(f"🎯 *F&O Setups* ({len(fno_sigs)}) — {time_str}\n", blocks)


def run_intraday_scan(time_str):
    """30-min intraday momentum: VWAP + RSI55 cross + vol surge on Nifty 50 universe."""
    from scanner import scan_intraday_momentum
    from tracker import (log_batch_to_all_signals, duplicate_symbols, mark_alerts_sent)
    logging.info("Running intraday momentum scan (15m)...")
    raw = scan_intraday_momentum()
    dupes = duplicate_symbols(raw, "intraday")
    sigs = [s for s in raw if str(s["symbol"]).replace(".NS", "") not in dupes]
    logging.info(f"Intraday: {len(sigs)} to alert ({len(raw)} raw → {len(dupes)} deduped out)")
    if sigs:
        blocks, rows = [], []
        for s in sigs:
            blocks.append(
                f"• *{s['symbol']}* | 15m | BUY ₹{s['price']}\n"
                f"  SL ₹{s['sl']} | T1 ₹{s['target1']} | T2 ₹{s['target2']}"
                f" | RR {s['rr']} | Vol {s['vol_ratio']}x | RSI {s['rsi']}"
            )
            rows.append({
                "symbol": s["symbol"], "signal_type": "intraday", "action": "BUY",
                "entry": s["price"], "sl": s["sl"], "t1": s["target1"],
                "t2": s["target2"], "t3": s["target2"], "rr": s["rr"],
                "timeframe": "15m", "score": s.get("score", 0),
            })
        ids  = log_batch_to_all_signals(rows)
        sent = _send_chunked(
            f"⚡ *Intraday Momentum* ({len(sigs)}) — {time_str}\n"
            "_(15m · VWAP + RSI55 + Vol surge)_\n",
            blocks, footer="\n_Intraday only · Exit by 3:15 PM IST_")
        _record_delivery(ids, sent, mark_alerts_sent)
    return sigs


def run_ai_longterm_scan(time_str):
    """Weekly long-horizon picks — Saturday only.

    Deliberately separate from every other scan here. These are 2–3 year
    ownership ideas screened on return on capital, growth, leverage and
    valuation, with the chart used only to reject structural downtrends. They
    carry a 200DMA-structure stop, not a trade stop, and they are excluded from
    the R-multiple statistics — see ai_longterm.EXCLUDE_FROM_EXPECTANCY.
    """
    import ai_longterm as ail
    logging.info("Running AI long-term scan (weekly)...")
    picks = ail.build()
    if picks:
        _send(ail.to_telegram(picks))
    else:
        _send(f"🧠 *AI Long-Term Picks* — {time_str}\n"
              f"_Nothing cleared the business and trend screens this week._")
    return picks


def run_multibagger_scan(time_str):
    """Weekly multibagger scan — Saturday only."""
    from scanner import scan_multibaggers
    from tracker import log_multibaggers
    logging.info("Running potential multibagger scan (weekly)...")
    mbs = scan_multibaggers(top_n=15)
    logging.info(f"Multibagger scan: {len(mbs)} candidates")
    if mbs:
        log_multibaggers(mbs)
        blocks = []
        for i, m in enumerate(mbs, 1):
            fno_tag = " `F&O`" if m.get("fno") else ""
            pe_str  = f" | PE {m['pe']:.0f}x" if m.get("pe") else ""
            blocks.append(
                f"{i}. *{m['symbol']}*{fno_tag} | ₹{m['price']}\n"
                f"   T1 ₹{m['target1']} | T2 ₹{m['target2']} | SL ₹{m['sl']}"
                f" | RR {m['rr']}{pe_str}\n"
                f"   _{m['reason']}_"
            )
        _send_chunked(
            f"🚀 *Potential Multibaggers* ({len(mbs)}) — Weekly Watchlist\n"
            f"_{time_str}_\n_(Weekly breakout + momentum + volume expansion)_\n",
            blocks, footer="\n_Horizon: 6–12 months · Not SEBI advice_")
    return mbs


def run_magic_scan(time_str):
    """Weekly Investtech-style recovery screens — Saturday only.

    Two passes over the same logic, split only by how far below the 52-week high
    a candidate sits: magic takes anything >15% below, magicmagic narrows to the
    20-40% band. Both now carry a stop and three targets from
    scanner.magic_levels; a candidate whose 52-week high cannot clear its stop by
    1R is dropped by that function rather than published without levels.

    Previously reachable only through the /magic bot command, so it ran when
    someone remembered to ask. It belongs on the same Saturday clock as the other
    two weekly engines.
    """
    from scanner import scan_magic, scan_magicmagic
    from tracker import _log_magic_to_ledger
    logging.info("Running magic + magicmagic screens (weekly)...")
    today = datetime.now(IST).strftime("%Y-%m-%d")
    out = []
    for engine, fn, label, emoji, band in (
            ("magic", scan_magic, "Magic Screener", "🔮", ">15% from 52WH"),
            ("magicmagic", scan_magicmagic, "MagicMagic", "✨", "20–40% from 52WH")):
        try:
            res = fn(top_n=12)
        except Exception as e:
            logging.error(f"{engine} scan failed: {e}")
            continue
        logging.info(f"{engine}: {len(res)} candidates")
        if not res:
            continue
        _log_magic_to_ledger(res, engine, today)
        blocks = []
        for i, r in enumerate(res, 1):
            se = {"BUY": "🟢", "WATCH": "🟡", "AVOID": "🔴"}.get(r.get("short", ""), "⚪")
            we = {"BUY": "🟢", "WATCH": "🟡", "AVOID": "🔴"}.get(r.get("swing", ""), "⚪")
            le = {"BUY": "🟢", "WATCH": "🟡", "AVOID": "🔴"}.get(r.get("long", ""), "⚪")
            blocks.append(
                f"{i}. *{r['symbol']}* ₹{r['price']} · Score `{r['score']}`\n"
                f"   CAGR `{r['cagr_3yr']}%` · RSI(W) `{r['weekly_rsi']}`"
                f" · `{r['dist_52wh']}%` from 52WH\n"
                f"   SL ₹{r['sl']} | T1 ₹{r['target1']} | T2 ₹{r['target2']}"
                f" | T3 ₹{r['target3']} _(52WH)_ | RR {r.get('rr')}x\n"
                f"   {se} short · {we} swing · {le} long\n"
                f"   _{r.get('long_note', '')}_"
            )
        _send_chunked(
            f"{emoji} *{label}* ({len(res)}) — {band}\n_{time_str}_\n"
            f"_(3Y CAGR+ · weekly RSI 46+ · recovering toward the 52-week high)_\n",
            blocks,
            footer="\n_Horizon: 3–12 months · T3 is the 52-week high · "
                   "Investtech-style · Not SEBI advice_")
        out.extend(res)
    return out


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    from tracker import log_scan_meta, init_db
    init_db()

    now        = datetime.now(IST)
    today_str  = now.strftime("%Y-%m-%d")
    time_str   = now.strftime("%d %b %Y %I:%M %p IST")
    is_holiday = today_str in NSE_HOLIDAYS
    clock_slot = _slot(now, is_holiday=is_holiday)
    requested  = _requested_slot()
    counts     = {}

    if requested:
        slot = requested
        # Sunday and NSE holidays still win over an explicit --slot: the cron
        # is Mon-Fri but a queued run can spill past midnight IST, and there is
        # nothing to scan on a closed exchange whatever the workflow asked for.
        if clock_slot == "none":
            logging.info(f"--slot {requested} ignored — market closed today.")
            return 0
        if requested != clock_slot:
            logging.warning(
                f"slot override: workflow asked for {requested!r}, clock says "
                f"{clock_slot!r} (run is {now.strftime('%H:%M')} IST — cron delayed)")
    else:
        slot = clock_slot

    if slot == "none":
        logging.info("Sunday or post-holiday non-morning — no scan.")
        return 0

    logging.info(f"=== Scan started: {time_str} | Slot: {slot} ===")
    _send(f"🔄 *SwingDesk Pro* — {time_str}\n_Slot: {slot.upper()} starting..._")

    def _safe(label, fn, *args, default=None, **kwargs):
        """Run a scan function, catch + log any exception so one failure doesn't stop others."""
        try:
            result = fn(*args, **kwargs)
            return result if result is not None else (default if default is not None else [])
        except Exception as _e:
            logging.warning(f"{label} failed (skipped): {_e}")
            _send(f"⚠️ *{label} skipped* — {str(_e)[:200]}")
            return default if default is not None else []

    try:
        # ── Price alerts FIRST ────────────────────────────────────────────────
        _safe("price_alerts", run_price_alerts, time_str)
        _safe("markets",      run_markets,      time_str)

        mode = None   # set by slots that generate no signals by design

        if slot in ("morning", "midday"):
            # Signal generation removed here on 2026-07-30. Every scan that ran
            # in these slots (4h, tlm, swing, commodity) derived targets from
            # the stop distance, so R:R was a constant and none of it was ever
            # measured. When the rebuilt engine was backtested on an intraday
            # horizon it returned -0.005R over 583 trades, against +0.171R on
            # daily closes — the intraday edge is not there to capture.
            #
            # These slots now run position management only: price alerts and
            # the market snapshot, both executed above for every slot. Entries
            # come from the EOD measured scan.
            # Counts must stay numeric. A {"mode": "position-management-only"}
            # marker used to live here and blew up the completion summary with
            # "unsupported operand type(s) for +: 'int' and 'str'" — which
            # aborted the whole scan AFTER the work was done, so every midday
            # run reported itself as a Scanner Error.
            counts = {}
            mode = "position-management-only"

        elif slot == "eod":
            breakouts = _safe("breakout_scan", run_breakout_scan,  time_str)
            tlm_daily = _safe("tlm_daily",     run_tlm_scan,       time_str, interval="1d")
            signals   = _safe("swing_scan",    run_swing_scan,     time_str)
            comms     = _safe("commodity_scan",run_commodity_scan, time_str)
            # Backtested engine — runs on the completed daily bar, as tested.
            measured  = _safe("measured_equity", run_measured_equity_scan, time_str)
            # OHL/OLL reads today's Open/High/Low off the DAILY bar
            # (interval="1d"), which is only stable once the session has
            # closed — the same reason measured_equity runs here and not
            # at midday.
            ohl       = _safe("ohl_scan",      run_ohl_scan,       time_str)
            # Ledger last: every alert and its outcome, to Telegram + Obsidian.
            # Runs after the scans so today's signals are already logged.
            _safe("signal_ledger", run_signal_ledger, time_str)
            counts    = {"breakouts": len(breakouts), "ai_daily": len(tlm_daily),
                         "swing": len(signals), "commodities": len(comms),
                         "measured": len(measured), "ohl": len(ohl)}

        elif slot == "weekend":
            sigs_4h   = _safe("4h_scan",       run_4h_scan,        time_str)
            tlm_4h    = _safe("tlm_4h",        run_tlm_scan,       time_str, interval="4h")
            signals   = _safe("swing_scan",    run_swing_scan,     time_str)
            _safe("fno_alerts",                run_fno_alerts,     time_str, signals)
            breakouts = _safe("breakout_scan", run_breakout_scan,  time_str)
            tlm_daily = _safe("tlm_daily",     run_tlm_scan,       time_str, interval="1d")
            comms     = _safe("commodity_scan",run_commodity_scan, time_str)
            mbs       = _safe("multibagger",   run_multibagger_scan, time_str)
            lt        = _safe("ai_longterm",   run_ai_longterm_scan, time_str)
            # Third weekly engine on the same Saturday clock. It was only ever
            # reachable through the /magic bot command, so it ran when someone
            # remembered to ask rather than every week.
            mgc       = _safe("magic",         run_magic_scan,       time_str)
            counts    = {
                "4h": len(sigs_4h), "ai_4h": len(tlm_4h),
                "swing": len(signals), "breakouts": len(breakouts),
                "ai_daily": len(tlm_daily), "commodities": len(comms),
                "multibaggers": len(mbs), "longterm": len(lt),
                "magic": len(mgc),
            }

        elif slot == "holiday":
            comms   = _safe("commodity_scan", run_commodity_scan, time_str)
            sigs_4h = _safe("4h_scan",        run_4h_scan,        time_str)
            counts  = {"commodities": len(comms), "4h": len(sigs_4h)}
            _send(f"🏛️ *NSE Holiday* ({now.strftime('%d %b %Y')}) — "
                  f"Markets & commodity signals only. Equities resume next trading day.")

        else:  # full (off-hours fallback)
            breakouts = _safe("breakout_scan", run_breakout_scan,  time_str)
            signals   = _safe("swing_scan",    run_swing_scan,     time_str)
            comms     = _safe("commodity_scan",run_commodity_scan, time_str)
            counts    = {"breakouts": len(breakouts), "swing": len(signals), "commodities": len(comms)}

        log_scan_meta(slot, counts)
        logging.info(f"=== Scan finished: {slot} | {counts} ===")

        # ── Export all JSON for dashboard (always runs, even if no signals) ──
        from tracker import export_signals_json
        export_signals_json()
        logging.info("Signal data exported to data/")

        # ── Slot completion summary (always sends so you know scan ran) ──
        # Defensive on type: a non-numeric entry sneaking into `counts` must
        # never take down a scan whose real work already succeeded.
        nums  = {k: v for k, v in counts.items() if isinstance(v, (int, float))}
        total = sum(nums.values())
        parts = [f"{k.upper()}: {v}" for k, v in nums.items() if v > 0]
        if total == 0:
            _send(
                f"✅ *{slot.upper()} scan complete* — {time_str}\n"
                + (f"_{mode.replace('-', ' ')} — no entries by design._" if mode
                   else "_No qualifying signals. Regime/score/RR filters not met._")
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
        try:
            from tracker import export_signals_json
            export_signals_json()
        except Exception:
            pass
        return 0  # exit 0 so GH Actions job shows green — Telegram alert already sent


if __name__ == "__main__":
    sys.exit(main())

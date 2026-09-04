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
    Full routine scan + Potential Multibaggers + ai_longterm
    + magic / magicmagic recovery screens.
    NO commodity scan — COMEX is closed all day Saturday, so it would only
    ever re-read Thursday's close. See the weekend branch for the evidence.

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
    """Currency for a symbol. Delegates to symbols.py — the single source.

    This function used to own the answer via _USD_QUOTED, a set containing
    only commodities and FX. Every US equity therefore fell through to the ₹
    default: SNOW, SMCI and MSFT were all printed in rupees (live, until
    2026-08-18). The sets below are kept as a fallback for the case where
    symbols.py cannot be imported, but they are no longer the authority.
    """
    try:
        from symbols import currency_of
        return currency_of(symbol)
    except Exception:
        s = (symbol or "").upper()
        if s in _USD_QUOTED:
            return "$"
        if s in _FX_PAIRS:
            return ""      # a rate, not a money amount
        return "₹"


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


def longs_only(signals, key="action"):
    """The subset a long-only book can act on. Logging is NOT affected.

    The instruction was "never focus on sell calls for any type of trade
    including commodities, but record in the signal log". Both halves matter,
    and they pull in opposite directions:

      RECORD  — every engine keeps filing shorts and the ledger keeps every
                one. That is what makes it answerable later whether refusing
                them cost anything. Deleting them would destroy the evidence
                for the very decision being taken.
      DO NOT  — nothing short is put in front of a reader as an action, and
      ACT       swing_rulebook refuses to size one (SHORT_NOT_TAKEN).

    So this filters the ALERT list only, and always after the batch has been
    written. Call it on what goes to Telegram, never on what goes to the DB.

    204 of 783 ledger rows are SELL — 186 cf_1h and 17 commodity from v1, plus
    one live equity_measured. The engines that can still produce them are
    commodity and equity_measured.
    """
    keep, dropped = [], 0
    for s in signals or []:
        act = str((s.get(key) if isinstance(s, dict) else None) or "BUY").upper()
        if act in ("SELL", "SHORT"):
            dropped += 1
            continue
        keep.append(s)
    if dropped:
        logging.info(f"longs-only: {dropped} short signal(s) logged but not alerted")
    return keep


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


# Which markets each scan slot is allowed to grade.
#
# The scanner was built entirely on an NSE clock, and every slot graded every
# open signal regardless of where it trades. Once US names entered the ledger
# that produced this, live on 2026-08-19: the 16:30 IST end-of-day scan sent
# "SL HIT — SMCI" at 16:41 IST, when the US market had been shut for 14 hours
# and would not open for another two.
#
# The GRADING was never wrong — exits are read off completed bars, so there is
# no look-ahead. What was wrong is WHEN the reader was told: a stop broken in
# Monday night's US session surfaced on Tuesday afternoon IST, detached from
# the session it belonged to.
#
# So each slot now grades only markets whose session has actually closed:
#
#   midday / eod   NSE equities, plus COMEX and FX, which trade nearly around
#                  the clock and have no single daily close to wait for.
#   us             US equities, run in the IST morning — after the US close
#                  (20:00-21:00 UTC) and at an hour a reader is awake for.
SLOT_MARKETS = {
    "midday":  {"NSE", "BSE", "COMEX", "FX"},
    "eod":     {"NSE", "BSE", "COMEX", "FX"},
    "weekend": {"NSE", "BSE", "COMEX", "FX", "US"},
    "us":      {"US"},
}


def run_price_alerts(time_str: str, markets: set | None = None):
    """
    Position management for every OPEN signal in all_signals.

    `markets` limits the run to instruments whose market_of() is in the set.
    None means every market — the manual/fallback path, which keeps the old
    behaviour for an operator running the script by hand.

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

    if markets:
        from symbols import market_of
        before = len(open_df)
        open_df = open_df[open_df["symbol"].map(
            lambda x: market_of(str(x)) in markets)]
        skipped = before - len(open_df)
        if skipped:
            logging.info(f"price_alerts: {skipped} signal(s) held back — their "
                         f"market is not in this slot ({sorted(markets)}). They "
                         f"are graded by the slot that follows their own close.")
        if open_df.empty:
            logging.info("price_alerts: nothing to evaluate for this slot")
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

    # Returned, not just logged. The slot summary used to report only what was
    # NEWLY found — "BREAKOUTS: 3, SWING: 2" — and said nothing at all about
    # the book already open. A scan that finds nothing sent "no qualifying
    # signals" even on a day it closed four positions and warned on two more,
    # which reads as a quiet day when it was the opposite.
    try:
        with _conn() as c:
            still_open = c.execute(
                "SELECT COUNT(*) FROM all_signals WHERE status IN ('OPEN','T1_HIT')"
            ).fetchone()[0]
    except Exception:                                   # noqa: BLE001
        still_open = None
    return {"alerts": len(alerts), "closed": len(updates), "expired": expired,
            "flagged": len(flags), "open_after": still_open}


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

    # The chart pattern that actually fired.
    #
    # scanner.py detects it — _check_breakouts returns ("Weekly", "Cup &
    # Handle") and the OHL/OLL engine names its own — and then it was dropped
    # here. Every pattern signal reached the ledger carrying only its ENGINE
    # ("breakout", "ohl") and the generic remark "Breakout scan — swing
    # horizon", so the one fact that distinguishes one pattern trade from
    # another was computed and thrown away.
    #
    # That made the ledger unable to answer its own central question for these
    # engines: which patterns actually work? Expectancy could be measured per
    # engine but never per pattern.
    #
    # `patterns` (plural) is kept as well as `pattern`: a name breaking out on
    # the weekly AND monthly frame is a different setup from one breaking out
    # on the weekly alone, and the singular field only keeps the strongest.
    for key in ("pattern", "patterns"):
        val = sig.get(key)
        if val:
            meta[key] = val
    return {
        "grade":        sig.get("grade"),
        "breakeven_wr": sig.get("breakeven_wr"),
        "turnover_cr":  sig.get("turnover_cr"),
        "metadata":     meta,
    }


# "us" is the post-US-close position pass — see SLOT_MARKETS. It MUST be
# listed here: _requested_slot() rejects anything not in this set, so a
# slot added to the workflow and to SLOT_MARKETS but not to this line
# fails the job outright, every run, with a clean-looking error.
# What a no-entry slot MEANS, in the reader's terms. "position-management-only
# — no entries by design" is accurate and still reads as a failure at 18:30 on
# a phone: the operator reported "Telegram bot didn't give any signals" on a
# day the midday slot worked exactly as designed. A slot that generates nothing
# has to say WHERE the entries come from instead, or it looks broken every time
# it succeeds.
MODE_NOTE = {
    "position-management-only":
        "_Midday is position management only — it manages the open book and "
        "files no new entries. Intraday generation was removed on 2026-07-30: "
        "it measured -0.005R over 583 trades against +0.171R on daily closes. "
        "New entries come from the EOD scan after the close._",
    "us position check":
        "_US position check — grading only, no entries._",
}

VALID_SLOTS = {"morning", "midday", "eod", "weekend", "holiday", "full",
               "none", "us", "momentum"}


def _explicit_slot(argv=None) -> bool:
    """Was the slot ASKED FOR, rather than derived from the cron that fired?

    The clock override below exists for one situation: a cron lands hours late
    and its window has closed, so running the slot the clock is actually in
    beats re-running one that has passed. That reasoning applies to a cron. It
    does not apply to a deliberate dispatch.

    On 3 Sep the watchdog's repair of the end-of-day slot arrived at 21:56 IST,
    the clock said "full", the override converted it, full had already run, and
    the scan stood down under --once. Every late repair of a missed slot did
    the same thing — which is why signals stopped appearing while every run
    reported success.

    daily_scan.yml passes --explicit alongside --slot whenever the slot came
    from the workflow input rather than the cron table, so intent survives the
    trip and the clock stops overruling it.
    """
    argv = sys.argv[1:] if argv is None else argv
    return "--explicit" in argv


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


def _once_requested(argv=None) -> bool:
    """True when --once is on the command line: run this slot only if it has
    not already completed today.

    Every SCHEDULED arm passes it; a manual dispatch never does. GitHub drops
    runs rather than merely delaying them — on 2026-08-27 neither the 03:00
    nor the 05:00 cron produced a run at all and no signals went out — so each
    slot gets several crons and this is what keeps that to one scan.

    It covers drift as well as drops, which is why it is not called
    --catch-up: a "primary" delayed to 08:00 UTC would land after its own
    retries and scan the slot a second time. There is no primary, only several
    chances at the same job, and the first one to arrive does it.
    """
    argv = sys.argv[1:] if argv is None else argv
    return "--once" in argv or "--catch-up" in argv or "--catchup" in argv


def _scan_job(slot: str) -> str:
    """job_runs key for one slot on one day. Per slot, not per day: a completed
    midday must never satisfy a missing EOD."""
    return f"scan_{slot}"


def scan_already_ran(slot: str, today_ist: str) -> bool:
    """Did THIS slot already complete today (IST)?

    IST because the scan's whole calendar is the NSE session. Returns False
    whenever the answer cannot be established — no record, an unreadable
    stamp, a database that will not answer. A catch-up that re-scans is noise;
    one that stays silent because a lookup failed is the outage it exists to
    cover.
    """
    try:
        from job_runs import latest
        st = latest(_scan_job(slot))
        if not st or st.get("status") != "ok":
            return False
        return str(st.get("detail", "")).startswith(today_ist)
    except Exception as e:                              # noqa: BLE001
        logging.warning("scan_already_ran(%s): %s — assuming NOT run", slot, e)
        return False


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
    # TWO holes used to fall through to "full" at the bottom, and "full" skips
    # the measured equity scan: 10:31-10:59 (between morning and midday) and
    # 14:00-14:59 (between midday and eod). That is 90 minutes of every trading
    # day — including the last 90 before the 15:30 close — sitting in a slot
    # that does not scan. Found by test_scan_slots.py sweeping every session
    # minute, not by reading the branches. midday now runs from where morning
    # ends until eod begins, with no gap on either side.
    if (h == 10 and m > 30) or (11 <= h < 15):
        return "midday"
    if 15 <= h < 18:
        return "eod"
    return "full"


# Window OPENING time for each time-of-day slot, mirroring _slot() above.
# "full", "weekend", "holiday" and the engine-selector slots have no window and
# are never gated by this.
_SLOT_OPENS_IST = {"morning": (9, 0), "midday": (10, 31), "eod": (15, 0)}


def _before_window_opens(requested, now_ist):
    """True when an explicitly-dispatched slot is being asked for BEFORE its own
    window has opened.

    The _ORDER ladder cannot answer this: "full" sits at the top of it but
    covers BOTH pre-market (00:00-09:00) and post-market (18:00-24:00) IST, so
    ordering says 3 AM is "later" than midday. Comparing against the window's
    actual opening time is the only thing that distinguishes a late cron
    (repair it, run it) from a 3 AM watchdog (stand down).
    """
    open_at = _SLOT_OPENS_IST.get(requested)
    if not open_at:
        return False
    return (now_ist.hour, now_ist.minute) < open_at


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
    return sigs


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
        # THE FLOOR IS APPLIED BEFORE ANYTHING IS SENT OR WRITTEN.
        #
        # Live on the book: NGAS at 0.60R and 0.67R, XAUUSD at 0.67R — first
        # targets worth less than the trade risked. Lifting them here rather
        # than at the logger keeps the Telegram block and the ledger row
        # showing the same levels; lifting downstream would have alerted one
        # number and recorded another.
        from signals.indicators import enforce_r_floor
        for _s in sigs:
            _t1, _t2, _t3 = enforce_r_floor(
                _s.get("price"), _s.get("sl"), _s.get("target1"),
                _s.get("target2"), _s.get("target3"), _s.get("action") or "BUY")
            _s["target1"], _s["target2"] = _t1, _t2
            if _s.get("target3") is not None:
                _s["target3"] = _t3
            try:
                _risk = abs(float(_s["price"]) - float(_s["sl"]))
                if _risk > 0:
                    _s["rr"] = round(abs(_t1 - float(_s["price"])) / _risk, 2)
            except (TypeError, ValueError, KeyError):
                pass
        log_commodity_signals(sigs)
        blocks, rows = [], []
        for s in sigs:
            # EVERY signal is logged; only the longs get a block. Commodities
            # were called out by name in the instruction — "including
            # commodities" — because this is the engine that files the most
            # shorts: 17 of the 204 SELL rows in the ledger, and the only
            # non-v1 source of them besides equity_measured.
            if str(s.get("action") or "BUY").upper() in ("SELL", "SHORT"):
                rows.append({
                    "symbol": s["symbol"], "signal_type": "commodity", "action": s["action"],
                    "entry": s["price"], "sl": s["sl"], "t1": s["target1"],
                    "t2": s["target2"], "t3": s.get("target3", s["target2"]),
                    "rr": s["rr"], "timeframe": s.get("timeframe", "Daily"), "score": 0,
                })
                continue
            blocks.append(
                f"\U0001F4C8 *{s['symbol']}* `{s['timeframe']}` | \u25b2 BUY @ {s['price']}\n"
                f"  SL {s['sl']} | T1 {s['target1']} | T2 {s['target2']} | RR {s['rr']}"
            )
            rows.append({
                "symbol": s["symbol"], "signal_type": "commodity", "action": s["action"],
                "entry": s["price"], "sl": s["sl"], "t1": s["target1"],
                "t2": s["target2"], "t3": s.get("target3", s["target2"]),
                "rr": s["rr"], "timeframe": s.get("timeframe", "Daily"), "score": 0,
            })
        ids  = log_batch_to_all_signals(rows)
        if len(blocks) < len(sigs):
            logging.info(f"longs-only: {len(sigs) - len(blocks)} commodity short(s) "
                         f"logged but not alerted")
        sent = (_send_chunked(f"\U0001F947 *Commodity Signals* ({len(blocks)}) — {time_str}\n"
                              "_Long only \u2014 shorts are recorded, not alerted_\n", blocks)
                if blocks else True)
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

    # Logged above in full; only the longs are alerted. See longs_only.
    alertable = longs_only(signals, key="bias")
    sent = _send_chunked(
        f"\U0001F4CF *Measured Equity Signals* ({len(alertable)}) — {time_str}\n"
        "_Daily close · weekly regime · structural targets · long only_\n",
        [format_alert(s) for s in alertable],
        footer="\n_Backtested +0.171R/trade · not SEBI advice_") if alertable else True
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


def run_momentum_scan(time_str):
    """Cross-sectional momentum over the 750-name screen — see momentum_engine.

    WEEKLY, NOT DAILY, AND THAT IS THE STRATEGY RATHER THAN A SCHEDULING
    CONVENIENCE. The factor is a monthly-rebalanced one; the evidence behind it
    is for holds measured in months. Ranking it every morning would produce a
    stream of near-identical lists, log each as a fresh signal, and turn a slow
    factor into a churn machine whose costs are exactly the thing the
    low-turnover finding says eats the premium.

    It reads docs/screen.json rather than fetching prices, because the weekly
    screen has already computed every input the formula needs — returns over
    1m/6m/1y, ATR, turnover, market cap and the 200-day — for all 750 names.
    Recomputing them here would be a second source of truth for the same
    numbers.
    """
    import json as _json
    from momentum_engine import build as _mom_build
    from tracker import (log_batch_to_all_signals, duplicate_symbols,
                         mark_alerts_sent)
    logging.info("Running quant momentum scan (750-name screen)...")
    try:
        with open("docs/screen.json") as fh:
            d = _json.load(fh)
    except Exception as e:                                    # noqa: BLE001
        logging.error("momentum: screen.json unreadable (%s) — skipping", e)
        return []
    rows = next((v for v in d.values()
                 if isinstance(v, list) and v and isinstance(v[0], dict)), None)
    if not rows:
        logging.error("momentum: screen.json carries no rows — skipping")
        return []

    res = _mom_build(rows)
    logging.info("momentum: universe %d · eligible %d · rejected %d",
                 res["universe"], res["eligible"], res["rejected_by_gates"])
    for reason, n in (res.get("gate_failures") or {}).items():
        logging.info("  gate: %-46s %4d", reason, n)

    picks = res["picks"]
    dupes = duplicate_symbols(picks, "momentum_quant")
    picks = [p for p in picks
             if str(p["symbol"]).replace(".NS", "") not in dupes]
    if not picks:
        logging.info("momentum: nothing new after dedupe")
        return []

    ids = log_batch_to_all_signals([dict(
        symbol=p["symbol"], signal_type="momentum_quant", action="BUY",
        entry=p["entry"], sl=p["sl"], t1=p["target1"], t2=p["target2"],
        t3=p["target3"], rr=p["rr"], timeframe="1M", score=p["score"],
        metadata=p["components"],
    ) for p in picks])
    logged = [i for i in (ids or []) if i]
    logging.info("momentum: logged %d of %d", len(logged), len(picks))

    # ── THIS ENGINE WAS THE ONLY ONE THAT NEVER REACHED TELEGRAM ────────────
    #
    # It logged its picks to the ledger and stopped there. mark_alerts_sent was
    # imported at the top of this function and never called, which is the
    # fingerprint of a send block that was intended and never written. Every
    # other scan in this file ends with _send_chunked + _record_delivery; this
    # one ended at the database, so a name could be ranked, sized, written to
    # the ledger, rendered on the site — and nobody was told.
    #
    # The block carries the SCORE AND ITS PARTS, not just the levels. A
    # momentum pick is a claim about relative rank, and "3rd of 750 on a
    # z-score of +2.4" is the claim; the entry price alone hides it.
    #
    # Engine names and component keys carry underscores, which Telegram's
    # Markdown reads as italics and then rejects the whole message over — so
    # nothing here is interpolated raw into an _italic_ span.
    def _block(p):
        c = p.get("components") or {}
        return (f"*{p['symbol']}* — score `{p['score']:+.2f}`\n"
                f"   12m `{c.get('mom_12m_skip1')}%` · 6m `{c.get('mom_6m_skip1')}%` "
                f"· sigma `{c.get('sd1y')}%`\n"
                f"   Entry `{p['entry']}` · SL `{p['sl']}` "
                f"({(p['entry'] - p['sl']) / p['entry'] * 100:.1f}%)\n"
                f"   T1 `{p['target1']}` · T2 `{p['target2']}` · T3 `{p['target3']}` "
                f"· R:R `{p['rr']}`")

    sent = _send_chunked(
        f"\U0001F3C3 *Momentum — 750-name screen* ({len(picks)}) — {time_str}\n"
        "_Six and twelve month returns over one-year sigma, skip-month, "
        "z-scored across the universe · long only · monthly hold_\n",
        [_block(p) for p in picks],
        footer="\n_PAPER — not cleared for capital until 30 closed trades at t>=2_")
    _record_delivery(logged, sent, mark_alerts_sent)
    return picks


def run_breakout_scan(time_str):
    from scanner import scan_breakouts
    from tracker import (log_breakouts, log_batch_to_all_signals,
                         duplicate_symbols, mark_alerts_sent)
    logging.info("Running breakout scan (Nifty500, liquidity-gated)...")
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

    # Record the ATTEMPT, not just the outcome. Without this a week where
    # nothing clears the R:R 2.0 gate writes nothing at all, and the section
    # keeps serving the last batch that DID qualify with no hint it is old.
    # That is what "the multibaggers never change" was: the ledger's last
    # multibagger row is 2026-08-01 while magic and ai_longterm — same
    # Saturday job, same slot — both logged 2026-08-22. The screen was
    # running fine and qualifying nobody, silently, for four weeks.
    #
    # Deliberately NOT a loosened gate. On 2026-08-27 the screen took 11 raw
    # candidates and rejected 10 for R:R below 2.0 (CHENNPETRO 1.29,
    # FLUOROCHEM 1.07, GLAXO 0.59 — a 0.59 needs a 63% win rate merely to
    # break even). Publishing those to make the section look busy is exactly
    # the trade this screen exists to refuse. An empty week is a real result
    # and now says so.
    try:
        from job_runs import record as record_job_status
        record_job_status("multibagger", "ok" if mbs else "empty",
                          f"{len(mbs)} qualified — R:R 2.0 floor",
                          records=len(mbs), expected=15)
    except Exception as _e:                             # noqa: BLE001
        logging.warning(f"multibagger: could not record the attempt ({_e})")

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
            # THE OVERRIDE NOW HAPPENS. It used to only be announced.
            #
            # This branch logged "slot override: workflow asked for 'midday',
            # clock says 'eod'" and then kept `slot = requested` regardless, so
            # nothing was overridden and the message described behaviour the
            # code did not have.
            #
            # It matters because these crons land late. On 2026-08-31 all three
            # midday crons fired between 16:45 and 18:28 IST — hours after the
            # 15:30 close — and the 16:30 EOD cron did not fire at all. Each
            # late run asked for 'midday', was told the clock said 'eod', kept
            # 'midday' anyway, found midday already done and stood down under
            # --once. Net result: no EOD scan, no ledger settlement and no
            # alerts on a full trading day.
            #
            # A cron that arrives after its window has closed should do the work
            # the clock is actually in, not re-run a window that has passed or
            # refuse to run at all. Only a LATER clock overrides: a run that
            # arrives early keeps what the workflow asked for, and the
            # non-intraday slots (weekend, holiday) are never reinterpreted.
            # THE GUARD DID NOT DO WHAT THE PARAGRAPH ABOVE PROMISES.
            #
            # "the non-intraday slots (weekend, holiday) are never
            # reinterpreted" — but an absent key returns -1, and every clock
            # slot in the map beats -1. So a hand-dispatched `weekend`,
            # `holiday` or `momentum` on a weekday was silently rewritten to
            # whatever the clock said. Caught by asking for `momentum` at 09:20
            # IST and watching it run `morning` instead: the engine under test
            # never ran, and the log line explaining why read as if it had made
            # a considered choice.
            #
            # The rule this is meant to express is about the intraday LADDER —
            # a late `midday` should become `eod` — so both sides have to be on
            # that ladder for the comparison to mean anything. A slot that is
            # not a time of day is an engine selector, and the clock has no
            # opinion about it.
            _ORDER = {"morning": 0, "midday": 1, "eod": 2, "full": 3}
            both_intraday = requested in _ORDER and clock_slot in _ORDER
            # AN EXPLICIT REQUEST IS AN INSTRUCTION, NOT A GUESS TO CORRECT.
            # See _explicit_slot: the override is for late crons, and applying
            # it to a deliberate dispatch converted every watchdog repair into
            # a slot that had already run.
            if _explicit_slot():
                # ...but an explicit slot may only repair a LATE cron, never run
                # BEFORE its own window opens.
                #
                # 2026-09-04: something dispatched slot=midday at 03:18 AM IST
                # and again at 04:03 AM IST. Both ran, both marked midday
                # complete for the day, and every real midday dispatch from
                # 10:50 to 12:50 IST then stood down under --once. The market
                # had not opened when the midday scan ran, so the day produced
                # no new equity signals at all and the site showed yesterday's.
                # The runs were green. Nothing reported a failure.
                #
                # Standing down here (rather than downgrading) deliberately
                # leaves the slot unconsumed so its real window still gets it.
                if _before_window_opens(requested, now):
                    logging.warning(
                        "slot %r was asked for explicitly but its window has not "
                        "opened yet — the clock says %r (run is %s IST). Standing "
                        "down so the real %r window is not consumed.",
                        requested, clock_slot, now.strftime('%H:%M'), requested)
                    return 0
                logging.info(
                    "slot %r was asked for explicitly; the clock says %r and does "
                    "not override it.", requested, clock_slot)
            elif both_intraday and _ORDER[clock_slot] > _ORDER[requested]:
                logging.warning(
                    f"slot override: workflow asked for {requested!r}, clock says "
                    f"{clock_slot!r} (run is {now.strftime('%H:%M')} IST — cron "
                    f"delayed). Running {clock_slot!r}.")
                slot = clock_slot
            elif not both_intraday:
                logging.info(
                    f"workflow asked for {requested!r}; the clock says "
                    f"{clock_slot!r} but {requested!r} is not a time-of-day "
                    f"slot, so the clock does not override it.")
            else:
                logging.info(
                    f"workflow asked for {requested!r}, clock says {clock_slot!r} "
                    f"(run is {now.strftime('%H:%M')} IST — early). "
                    f"Keeping {requested!r}.")
    else:
        slot = clock_slot

    if slot == "none":
        logging.info("Sunday or post-holiday non-morning — no scan.")
        return 0

    if _once_requested() and scan_already_ran(slot, today_str):
        logging.info(f"{slot} already completed today ({today_str}) — "
                     f"standing down (--once).")
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
        # Scoped to the markets this slot is responsible for. An unrecognised
        # slot passes None, which grades everything — the manual-run path, and
        # deliberately unchanged so an operator running this by hand still
        # sees the whole book.
        book = _safe("price_alerts", run_price_alerts, time_str,
                     SLOT_MARKETS.get(slot), default={}) or {}
        _safe("markets",      run_markets,      time_str)

        mode = None   # set by slots that generate no signals by design

        if slot == "us":
            # Position management only. This slot exists to grade US positions
            # after the US close; it generates nothing, because every signal
            # engine here is built on the NSE session.
            mode = "us position check"

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

        elif slot == "momentum":
            # ONE ENGINE, ON ITS OWN.
            #
            # Momentum rides the Saturday slot with four other engines, which
            # is right for the weekly cadence and wrong for every other reason
            # you might want to run it: checking a change, seeing the current
            # ranking, or re-running after a screen rebuild. Without this,
            # doing any of those meant firing multibagger, ai_longterm, magic
            # and the 4H scan as collateral, each writing rows and sending
            # alerts nobody asked for.
            momentum  = _safe("momentum_scan", run_momentum_scan,  time_str)
            counts    = {"momentum": len(momentum)}

        elif slot == "weekend":
            # Momentum is weekly by design — see run_momentum_scan.
            momentum  = _safe("momentum_scan", run_momentum_scan,  time_str)
            sigs_4h   = _safe("4h_scan",       run_4h_scan,        time_str)
            tlm_4h    = _safe("tlm_4h",        run_tlm_scan,       time_str, interval="4h")
            signals   = _safe("swing_scan",    run_swing_scan,     time_str)
            _safe("fno_alerts",                run_fno_alerts,     time_str, signals)
            breakouts = _safe("breakout_scan", run_breakout_scan,  time_str)
            tlm_daily = _safe("tlm_daily",     run_tlm_scan,       time_str, interval="1d")
            # NO COMMODITY SCAN ON SATURDAY.
            #
            # This branch used to call run_commodity_scan and it produced a
            # natural gas signal on Saturday 2026-08-29. Commodity futures are
            # shut all day Saturday — CME runs Sunday 18:00 ET to Friday 17:00
            # ET — so the scan was reading Thursday's close and publishing an
            # entry nobody could take until Sunday evening, by which point the
            # market has had a whole session to gap away from it.
            #
            # Checked, not assumed: Yahoo's daily series for NG=F contains bars
            # for Mon-Fri only, with no Saturday bar anywhere in it, and even
            # Friday 2026-08-28 came back with a null close.
            #
            # The justification for COMEX in the weekend slot was that it
            # "trades nearly around the clock". That is true from Sunday
            # evening to Friday evening and false on the one day this slot
            # runs. The `holiday` slot below KEEPS its commodity scan on
            # purpose — an NSE holiday falls on a weekday, when COMEX is open.
            mbs       = _safe("multibagger",   run_multibagger_scan, time_str)
            lt        = _safe("ai_longterm",   run_ai_longterm_scan, time_str)
            # Third weekly engine on the same Saturday clock. It was only ever
            # reachable through the /magic bot command, so it ran when someone
            # remembered to ask rather than every week.
            mgc       = _safe("magic",         run_magic_scan,       time_str)
            counts    = {
                "momentum": len(momentum),
                "4h": len(sigs_4h), "ai_4h": len(tlm_4h),
                "swing": len(signals), "breakouts": len(breakouts),
                "ai_daily": len(tlm_daily),
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

        # Stamp the slot as done so a later catch-up cron stands down. Written
        # HERE — after the engines have run and log_scan_meta has settled, and
        # before the Telegram summary, which is best-effort. Stamping at the
        # top would mark a slot complete that then crashed, and the catch-up
        # would decline to cover the exact failure it exists for. The IST date
        # leads the detail string because scan_already_ran() reads it back.
        try:
            from job_runs import record
            record(_scan_job(slot), "ok",
                   f"{today_str} · {slot} · " +
                   ", ".join(f"{k}:{v}" for k, v in counts.items()),
                   records=sum(v for v in counts.values() if isinstance(v, int)))
        except Exception as _e:                         # noqa: BLE001
            logging.warning(f"could not stamp {slot} as complete ({_e}) — "
                            f"a catch-up may re-scan")

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
        # What happened to the EXISTING book, alongside what is new. Built
        # even when nothing new fired, because that is exactly the day the
        # existing book is the whole story.
        ex = []
        if book.get("closed"):
            _c = book["closed"]
            _e = book.get("expired") or 0
            ex.append(f"{_c} closed" + (f" ({_e} on time stop)" if _e else ""))
        if book.get("flagged"):
            ex.append(f"{book['flagged']} near stop")
        if book.get("open_after") is not None:
            ex.append(f"{book['open_after']} still open")
        existing = ("\n_Existing book:_ " + " · ".join(ex)) if ex else ""

        if total == 0:
            _send(
                f"✅ *{slot.upper()} scan complete* — {time_str}\n"
                + (MODE_NOTE.get(mode, f"_{mode.replace('-', ' ')} — no entries by design._")
                   if mode
                   else "_No new signals. Regime/score/RR filters not met._")
                + existing
            )
        else:
            _send(
                f"✅ *{slot.upper()} scan done* — {time_str}\n"
                + "_New:_\n"
                + "\n".join(f"  • {p}" for p in parts)
                + existing
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

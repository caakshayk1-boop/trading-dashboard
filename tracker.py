import sqlite3, os, json, logging, math
import pandas as pd
import yfinance as yf
from datetime import date, datetime, timedelta, timezone
import db as _db
from symbols import to_yahoo, classify as _classify

# Signals are dated in IST; runners execute in UTC. Compare against IST or a
# signal filed this evening IST looks like it belongs to "tomorrow".
_IST = timezone(timedelta(hours=5, minutes=30))


def _max_hold_hours(timeframe: str, engine: str = "", horizon: str = ""):
    """
    Holding limit for a signal. Imported from standalone_scan so the grader
    and the live scanner cannot drift apart — a second copy of this table is how
    the ledger ends up measuring a horizon the engine never traded.

    Takes engine and horizon, not just timeframe. Forwarding only the timeframe
    was itself a drift: standalone_scan resolves the horizon from the ENGINE
    first (multibagger 12 months, ai_longterm 3 years) and falls back to the
    timeframe table, so a grader passing "1W" alone would have re-applied the
    20-day default to exactly the positions fix_horizons.py had just reopened.

    Returns None when the horizon cannot be established — callers must treat
    that as "do not time-stop this", never as a number.
    """
    try:
        from standalone_scan import _max_hold_hours as _impl
        return _impl(timeframe, engine=engine, horizon=horizon)
    except Exception:
        return 20 * 24


"""
Signals dated before this are not eligible for trigger-based lifecycle.

update_all_outcomes now walks candles forward and writes entry_triggered_at on
the bar that touches entry. Without a cutoff, its first run would reach back
through every stale OPEN row, retro-fill an entry from historical candles and
book a realised R-multiple for a trade that was never taken — exactly what
reconcile_positions.py was written to prevent ("those rows were never managed,
many are duplicates of each other, and the price history at their fill time
cannot be honestly reconstructed").

Anything older is left alone for reconcile_positions.py to mark VOID. Move this
date forward only alongside a deliberate decision about the backlog behind it.
"""
LIFECYCLE_EPOCH = "2026-08-04"


def _f(v):
    """Float or None. The feed writes bare NaN where a level was not computed."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f

# date.today() and datetime.utcnow() both resolve to UTC on a GitHub Actions
# runner, so every signal filed between 00:00 and 05:30 IST was being stamped
# with the previous day's date, and every sent_at was written 5h30m behind the
# "IST" label the newspaper renders it under. Route all signal dating through
# these two helpers — never call date.today()/utcnow() directly in this module.
def _date_ist() -> date:
    return datetime.now(_IST).date()


def _today_ist() -> str:
    return _date_ist().isoformat()


def _now_ist() -> str:
    return datetime.now(_IST).isoformat()


log = logging.getLogger(__name__)

# Which signal engine produced a row. Bumped when the gating rules change in a
# way that makes old and new signals non-comparable, so performance can be
# measured per generation instead of blended into one misleading average.
#
#   v1 — everything up to 2026-08-02. No R:R floor on the breakout scan (it
#        published a 0.5R setup), R:R measured against T2 in the 4H scan and
#        T1 elsewhere, no liquidity or fundamental gates, and scan_all()'s
#        ADX check read a key that did not exist so the swing engine emitted
#        nothing for its entire life.
#   v2 — per-engine R:R floors derived from each engine's own measured win
#        rate (signals/expectancy.py), plus liquidity, PAT, D/E, growth and
#        earnings-blackout gates (signals/quality.py).
#
# v1 rows are NEVER deleted. They are 476 closed trades — the only evidence
# base this system has, and the control group the new gate is measured against.
ENGINE_VERSION = "v2"

# What each engine's rows RELATE TO, in plain words. signal_type is an engine
# name ('magic', 'ohl', 'cf_1h') — useful to the code, meaningless in a log a
# human reads. The ledger mixes a 1h commodity scalp, a weekly research pick, a
# monthly SIP allocation and a multi-year compounding idea in one table, and
# without this there is no way to tell which product a row came from or why it
# exists. Applied as a DEFAULT: any caller can pass its own `remarks=` for
# something more specific (which the weekly and monthly loggers do).
# EACH ENTRY NOW CARRIES THE ENGINE'S EXIT RULE, NOT ONLY ITS DESCRIPTION.
#
# This map is the one place both sites read an engine's plain-English identity
# from — news.askakshay.com renders it as the "Relates to" column, and it
# travels with every row into the ledger. So it is also the only place a rule
# can be stated once and appear on both sites without a second copy to keep in
# step, which is why the stop and target rule is appended here rather than
# written into either front end.
#
# The figures in brackets are MEASURED, from exit_rules_v2.py over 113 closed
# trades: stop distance in units of the name's own ATR scaled to the trade's
# holding horizon, and the resulting stop-out rate. They are stated because an
# engine's rule means little without the evidence that the rule is sane.
REMARKS = {
    # PIVOT had no entry here, so engine_rule() returned "" for it and an
    # alert naming PIVOT would have carried no explanation at all — the one
    # thing every other engine's message does. Caught by test_alert_pipeline's
    # "every named engine can explain itself" the moment pivot was added to
    # the Python name map on 2026-09-19; it had been absent from that map
    # entirely, so the gap had never been reachable before.
    "pivot":            "Reaction at a level — daily horizon. Takes price AT a level the "
                        "market has already reacted to (the 200-day, the top of a multi-week "
                        "shelf, or a swing high being retested) rather than price breaking "
                        "away from one, and requires the higher timeframe to agree and the "
                        "session to confirm. Research tier: logged and published, never "
                        "alerted, with no closed trade and no measured expectancy yet.",
    "cf_1h":            "Commodity 1h channel scan — intraday horizon. Entry on the 1h bar, "
                        "gated by 4H EMA alignment and RSI 45-75 long / 25-55 short; targets "
                        "from swing pivots and day levels, R:R measured not asserted; stop is "
                        "the WIDER of structural and ATR with a hard ATR floor. "
                        "[the only engine with a positive baseline: +0.088R over 12 closed]",
    "commodity":        "Commodity scan — intraday/swing horizon. Structural stop, ATR floor; first target floored at 1.6R from 2026-09-03 (it had published NGAS at 0.60R and XAUUSD at 0.67R — less reward than risk). "
                        "[3.51% median stop, 81.8% stop-out — thin sample, 11 closed]",
    "breakout":         "Breakout scan — swing horizon. Stop and targets scale to the HOLDING "
                        "horizon (sqrt-of-time: x2.24 weekly, x4.69 monthly) off daily ATR, "
                        "floored at 1.41xATR, capped 6-20%; R:R ladder held at 1.6/2.5/3.3. "
                        "[was 0.29xATR and 90.9% stop-out — sized for a day on a weeks-long "
                        "trade; corrected 2026-09-02, measured -0.552R to -0.195R]",
    "equity_measured":  "Daily-close equity engine — the measured swing edge. Structural stop "
                        "on the completed daily bar. [1.94xATR, 54.5% stop-out — the widest "
                        "stop and the lowest stop-out rate of the equity engines]",
    "magic":            "Magic-levels screen — daily-close swing. Stop is the WIDER of the structural and ATR candidates (corrected 2026-09-03; it took the tighter, the third copy of that fault). T1 floored at 1.6R — it was 1.0R, which put twenty-odd open signals at exactly one times risk, unprofitable at any win rate this engine has printed. [1.22xATR, 85.7% stop-out "
                        "over 7 closed — marginal, and too few to act on]",
    "magicmagic":       "Magic-levels screen (v1 engine) — daily-close swing. Same levels function as magic, so the same 2026-09-03 corrections: wider stop, T1 floored at 1.6R. [1.19xATR, 4 "
                        "closed; counted with magic as one family until each has a sample]",
    "ohl":              "Open-High-Low intraday engine — long-only, entry at open near the "
                        "day's low. Stop floored at 1.41xATR; targets stay snapped to the 10- "
                        "and 20-bar highs rather than being pushed out with the stop, so R:R "
                        "is ~1.5 rather than the ~2.4 a tighter stop implied. "
                        "[was 0.78xATR and 91.7% stop-out; corrected 2026-09-02, measured "
                        "-0.638R to -0.480R]",
    "momentum_quant":   "Quant momentum — cross-sectional rank over the 750-name screen. "
                        "12-month and 6-month returns, each skipping the most recent month "
                        "(it reverses) and divided by the name's ATR, z-scored across the "
                        "universe and averaged — NSE Indices' own Nifty500 Momentum 50 "
                        "construction. Tilted toward LOW scaled turnover: a 19-year NSE study "
                        "found high-turnover momentum returned 8.51% CAGR against 19.43% for "
                        "the low-turnover half. Gated on absolute momentum (up over 12 months "
                        "AND above the 200-day), which is the standard defence against "
                        "momentum crashes; that gate rejects ~58% of the universe. "
                        "Stop 1.41xATR, ladder 1.6/2.5/3.3R, monthly horizon. "
                        "[PAPER ONLY — Sharma et al. (2021) find the Indian momentum anomaly "
                        "fading, and the strongest supporting figures are backtests. "
                        "0 closed trades; not cleared for capital]",
    "multibagger":      "Weekly multibagger scan — research idea, not a trade. No exit rule is "
                        "claimed and none should be inferred. [1.57xATR, 72.7% stop-out]",
    "ai_longterm":      "Own the business — multi-year compounding idea, 200DMA structure stop. "
                        "The stop is the thesis breaking, not a volatility band.",
    "top5_pick":        "Weekly Top 5 trade ideas — the paper's front-page picks. Levels are computed upstream and were mirrored unchecked, which put MRK on the ledger at 0.65R; floored at 1.6R from 2026-09-03 and the R:R recomputed from the lifted target. "
                        "[7 closed, 100% stop-out — the worst record of any engine]",
    "sip_bucket":       "Monthly SIP allocation — what the SIP was told to buy. No stop and no "
                        "time horizon: it is an allocation, not a trade, and it is excluded "
                        "from every expectancy figure on both sites.",
    # Older engine names still present in the ledger. Found by the backfill
    # reporting them as unmapped rather than filling them with something
    # generic — 60 rows across these four.
    "4h":               "4-hour channel scan — intraday/swing horizon. Retired.",
    "ai_4h":            "AI channel breakout scan (4H) — intraday/swing horizon. Retired.",
    "ai_daily":         "AI channel breakout scan (daily close). Retired.",
    "intraday":         "GUST — 15-minute momentum on the Nifty 50 universe. Retired with "
                        "the intraday tier on 2026-07-30 and brought back 2026-09-17: that "
                        "tier averaged -0.005R over 583 trades, this engine on its own "
                        "closed 17 at +1.472R, t=3.69. RESEARCH tier — 13 trades short of "
                        "the 30 required before it is trusted.",
    "ohl":              "Open-equals-high reversal — retired 2026-09-17. 25 closed at "
                        "-0.265R, 20% won, t=-0.86; it never cleared a bar.",
}


def _conn():
    return _db.connect()


# Schema creation + migrations are idempotent but not free: every init_db() opens
# a connection, and under Turso connect() does a full replica sync. Nearly every
# public function here calls it, so a 53-signal scan paid that cost ~106 times
# (measured ~8.7s per insert in CI). Run it once per process, per database.
_DB_READY = None   # resolved DB target this process has already initialised


def init_db(force: bool = False):
    global _DB_READY
    target = getattr(_db, "TURSO_URL", "") or getattr(_db, "LOCAL_DB", "")
    if not force and _DB_READY == target:
        return
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT,
            symbol      TEXT,
            setup_type  TEXT,
            action      TEXT,
            entry       REAL,
            sl1         REAL,
            sl2         REAL,
            target1     REAL,
            target2     REAL,
            target3     REAL,
            score       INTEGER,
            status      TEXT DEFAULT 'OPEN',
            exit_price  REAL,
            pnl_pct     REAL,
            r_multiple  REAL,
            metadata    TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS muted_assets (
            symbol TEXT PRIMARY KEY,
            muted_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS breakouts (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            date      TEXT,
            symbol    TEXT,
            timeframe TEXT,
            pattern   TEXT,
            patterns  TEXT,
            price     REAL,
            sl        REAL,
            target1   REAL,
            target2   REAL,
            target3   REAL,
            rr        REAL,
            vol_ratio REAL,
            fno       INTEGER DEFAULT 0,
            tv_link   TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS signals_4h (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            date      TEXT,
            symbol    TEXT,
            action    TEXT,
            price     REAL,
            sl        REAL,
            target1   REAL,
            target2   REAL,
            rr        REAL,
            rsi       REAL,
            vol_ratio REAL,
            fno       INTEGER DEFAULT 0,
            reason    TEXT,
            tv_link   TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS commodity_signals (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            date      TEXT,
            symbol    TEXT,
            ticker    TEXT,
            label     TEXT,
            action    TEXT,
            timeframe TEXT,
            price     REAL,
            sl        REAL,
            target1   REAL,
            target2   REAL,
            target3   REAL,
            rr        REAL,
            rsi       REAL,
            adx       REAL,
            atr       REAL
        )""")
        # Unified performance-tracking table for ALL signal types sent to Telegram
        c.execute("""CREATE TABLE IF NOT EXISTS all_signals (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            date             TEXT NOT NULL,
            signal_type      TEXT NOT NULL,
            symbol           TEXT NOT NULL,
            action           TEXT DEFAULT 'BUY',
            timeframe        TEXT,
            entry            REAL,
            sl               REAL,
            target1          REAL,
            target2          REAL,
            target3          REAL,
            rr               REAL,
            score            INTEGER DEFAULT 0,
            status           TEXT DEFAULT 'OPEN',
            lifecycle_status TEXT DEFAULT 'Generated',
            exit_price       REAL,
            pnl_pct          REAL,
            r_multiple       REAL,
            max_profit_pct   REAL,
            max_drawdown_pct REAL,
            generated_at     TEXT,
            entry_triggered_at TEXT,
            closed_at        TEXT,
            why_triggered    TEXT,
            market           TEXT DEFAULT 'NSE',
            asset_type       TEXT DEFAULT 'Equity',
            sent_at          TEXT,
            metadata         TEXT
        )""")
        # Scan activity log
        c.execute("""CREATE TABLE IF NOT EXISTS scan_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT NOT NULL,
            slot       TEXT NOT NULL,
            symbol     TEXT,
            signal_type TEXT,
            action     TEXT,
            result     TEXT,
            note       TEXT
        )""")
        # Auto-migrate: add missing columns to existing DB
        existing = [r[1] for r in c.execute("PRAGMA table_info(signals)").fetchall()]
        migrations = [
            ("setup_type",  "ALTER TABLE signals ADD COLUMN setup_type TEXT"),
            ("action",      "ALTER TABLE signals ADD COLUMN action TEXT DEFAULT 'BUY'"),
            ("sl1",         "ALTER TABLE signals ADD COLUMN sl1 REAL"),
            ("sl2",         "ALTER TABLE signals ADD COLUMN sl2 REAL"),
            ("r_multiple",  "ALTER TABLE signals ADD COLUMN r_multiple REAL"),
            ("metadata",    "ALTER TABLE signals ADD COLUMN metadata TEXT"),
        ]
        for col, sql in migrations:
            if col not in existing:
                c.execute(sql)
        # Auto-migrate all_signals table for new columns
        as_existing = [r[1] for r in c.execute("PRAGMA table_info(all_signals)").fetchall()]
        as_migrations = [
            ("lifecycle_status",    "ALTER TABLE all_signals ADD COLUMN lifecycle_status TEXT DEFAULT 'Generated'"),
            ("max_profit_pct",      "ALTER TABLE all_signals ADD COLUMN max_profit_pct REAL"),
            ("max_drawdown_pct",    "ALTER TABLE all_signals ADD COLUMN max_drawdown_pct REAL"),
            ("generated_at",        "ALTER TABLE all_signals ADD COLUMN generated_at TEXT"),
            ("entry_triggered_at",  "ALTER TABLE all_signals ADD COLUMN entry_triggered_at TEXT"),
            ("closed_at",           "ALTER TABLE all_signals ADD COLUMN closed_at TEXT"),
            ("why_triggered",       "ALTER TABLE all_signals ADD COLUMN why_triggered TEXT"),
            ("market",              "ALTER TABLE all_signals ADD COLUMN market TEXT DEFAULT 'NSE'"),
            ("asset_type",          "ALTER TABLE all_signals ADD COLUMN asset_type TEXT DEFAULT 'Equity'"),
            # SL1 is a warning, not an exit — it never wrote to the DB, so the
            # same warning re-fired on every scan for the life of the signal.
            ("alert_flags",         "ALTER TABLE all_signals ADD COLUMN alert_flags TEXT DEFAULT ''"),
            # sent_at used to be stamped at INSERT time, before the Telegram call
            # even ran — so a failed send still looked delivered. It is now set
            # only by mark_alerts_sent() after Telegram returns OK, and the
            # failure reason lands here. sent_at IS NULL now means "never sent".
            ("send_error",          "ALTER TABLE all_signals ADD COLUMN send_error TEXT"),
            # What this row RELATES TO, in words. The ledger mixes engines with
            # completely different intents — a 1h commodity scalp, a weekly
            # research pick, a monthly SIP allocation and a multi-year
            # compounding idea all land in the same table, and signal_type
            # ('magic', 'ohl', 'cf_1h') is an engine name, not an explanation.
            # Without this the log cannot be read: there is no way to tell why
            # a row exists or which product it came from.
            ("remarks",             "ALTER TABLE all_signals ADD COLUMN remarks TEXT"),
            # SQLite backfills existing rows with the DEFAULT on ADD COLUMN, so
            # this tags all 575 pre-existing signals as v1 in one statement
            # without touching a single row of data.
            ("engine_version",      "ALTER TABLE all_signals ADD COLUMN engine_version TEXT DEFAULT 'v1'"),
            ("grade",               "ALTER TABLE all_signals ADD COLUMN grade TEXT"),
            ("breakeven_wr",        "ALTER TABLE all_signals ADD COLUMN breakeven_wr REAL"),
            ("turnover_cr",         "ALTER TABLE all_signals ADD COLUMN turnover_cr REAL"),
            # Set when a single daily bar touched both the stop and a target, so
            # the true sequence is unknowable at this resolution. The stop is
            # booked (conservative), but the fraction of the ledger resting on
            # that assumption is now measurable instead of invisible.
            ("exit_ambiguous",      "ALTER TABLE all_signals ADD COLUMN exit_ambiguous INTEGER DEFAULT 0"),
            # Difference between where the exit was booked and the level that
            # should have filled, in R. NESTLEIND booked -1.2R against a -1.0R
            # stop on 2026-07-31 and nothing recorded the extra -0.2R.
            ("slippage_r",          "ALTER TABLE all_signals ADD COLUMN slippage_r REAL"),
            # Stamped by regrade.py. NULL means the row still carries the number
            # produced by the pre-2026-08-05 grader, which tested the stop first
            # across a merged, unbounded window and graded 1H signals on daily
            # bars. Those values are not evidence; this column is how a consumer
            # tells the two populations apart.
            ("regraded_at",         "ALTER TABLE all_signals ADD COLUMN regraded_at TEXT"),
            # SIMULATED = price traded through the entry level, so the strategy
            # would have filled here. CONFIRMED = the user actually placed the
            # order and said so. SKIPPED = the user explicitly passed.
            #
            # Nothing automated may ever write CONFIRMED. There is no broker
            # integration in this repo — upstox_provider is read-only market
            # data — so a fill the bot infers from a price touch is a paper
            # trade. Without this column the terminal would show "29.8%
            # deployed" against capital that was never committed.
            ("fill_type",           "ALTER TABLE all_signals ADD COLUMN fill_type TEXT"),
            ("confirmed_at",        "ALTER TABLE all_signals ADD COLUMN confirmed_at TEXT"),
            ("confirmed_qty",       "ALTER TABLE all_signals ADD COLUMN confirmed_qty INTEGER"),
            ("confirmed_price",     "ALTER TABLE all_signals ADD COLUMN confirmed_price REAL"),
            # duplicate_symbols() drops a candidate whose symbol+engine already
            # has an OPEN row (dedupe_positions.py exists because letting a
            # second row through once gave GLAND 3 open positions and OFSS 5,
            # multiplying one real outcome's weight in every expectancy figure).
            # The row it was dropped for used to just log a debug line nobody
            # reads; this makes the re-fire visible on the still-open row
            # instead of vanishing — same invariant, no silent information loss.
            ("duplicate_note",      "ALTER TABLE all_signals ADD COLUMN duplicate_note TEXT"),
        ]
        for col, sql in as_migrations:
            if col not in as_existing:
                c.execute(sql)
        _ensure_signal_versions(c)
        _ensure_multibagger_table(c)
        c.commit()
        _db.sync(c)
        _DB_READY = target
        log.info("DB init OK")

def log_signals(signals):
    init_db()
    today = _today_ist()
    with _conn() as c:
        for s in signals:
            meta = json.dumps({
                "rsi":         s.get("rsi"),
                "adx":         s.get("adx"),
                "vol_ratio":   s.get("vol_ratio"),
                "regime":      s.get("regime"),
                "reasons":     s.get("reasons", ""),
                "fno":         s.get("fno_eligible", False),
                "rr1":         s.get("rr1", 0),
                "rr2":         s.get("rr2", 0),
                "qty":         s.get("qty", 0),
                "atr":         s.get("atr", 0),
                "tv_link":     s.get("tv_link", ""),
                "bias":        s.get("bias", "bullish"),
                "hh_hl":       s.get("hh_hl", 0),
                "fno_suggestion": s.get("fno_suggestion"),
            })
            c.execute("""INSERT INTO signals
                (date,symbol,setup_type,action,entry,sl1,sl2,target1,target2,target3,score,metadata)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (today, s["symbol"], s.get("setup_type",""), s.get("action","BUY"),
                 s["price"], s.get("sl1", s["price"]*0.96), s.get("sl2", s["price"]*0.96),
                 s["target1"], s["target2"], s["target3"], s["score"], meta))
        c.commit()
        _db.sync(c)


def get_signals_display(days=3, min_score=0):
    """Return signals as list of dicts ready for card display, parsed from DB."""
    init_db()
    cutoff = str(_date_ist() - timedelta(days=days))
    with _conn() as c:
        df = pd.read_sql(
            "SELECT * FROM signals WHERE date>=? AND score>=? AND status='OPEN' ORDER BY score DESC",
            c, params=(cutoff, min_score))
    if df.empty:
        return []
    result = []
    for _, row in df.iterrows():
        try:
            meta = json.loads(row.get("metadata") or "{}")
        except Exception:
            meta = {}
        entry = float(row["entry"])
        sl2   = float(row.get("sl2") or entry * 0.96)
        t1    = float(row["target1"])
        t2    = float(row["target2"])
        t3    = float(row["target3"])
        risk  = max(entry - sl2, 0.01)
        result.append({
            "symbol":      row["symbol"],
            "action":      row.get("action", "BUY"),
            "setup_type":  row.get("setup_type", ""),
            "price":       entry,
            "sl1":         float(row.get("sl1") or sl2),
            "sl2":         sl2,
            "target1":     t1,
            "target2":     t2,
            "target3":     t3,
            "score":       int(row["score"]),
            "status":      row.get("status", "OPEN"),
            "date":        row["date"],
            "rsi":         meta.get("rsi", 0),
            "adx":         meta.get("adx", 0),
            "vol_ratio":   meta.get("vol_ratio", 1.0),
            "regime":      meta.get("regime", ""),
            "reasons":     meta.get("reasons", ""),
            "fno_eligible":meta.get("fno", False),
            "rr1":         meta.get("rr1") or round((t1 - entry) / risk, 2),
            "rr2":         meta.get("rr2") or round((t2 - entry) / risk, 2),
            "qty":         meta.get("qty", 0),
            "atr":         meta.get("atr", 0),
            "tv_link":     meta.get("tv_link") or f"https://in.tradingview.com/chart/?symbol=NSE:{row['symbol']}",
            "bias":        meta.get("bias", "bullish"),
            "fno_suggestion": meta.get("fno_suggestion"),
        })
    return result

def is_duplicate(symbol, signal_type="swing"):
    """
    True if:
    - Active open signal for same symbol+type in last 5 days, OR
    - SL hit for same symbol+type in last 7 days.
    Sticks to existing trade plan — no new entry until trade closes.

    `symbol` may be a plain string or the candidate dict itself — pass the
    dict to get a remark left on the still-open row when this returns True
    for the OPEN case. See _note_resignal().
    """
    cand = symbol if isinstance(symbol, dict) else None
    sym_clean = str(cand.get("symbol", "") if cand else symbol).replace(".NS", "")
    init_db()
    cutoff_5d = str(_date_ist() - timedelta(days=5))
    cutoff_7d = str(_date_ist() - timedelta(days=7))
    with _conn() as c:
        # Check unified all_signals first (covers all signal types)
        row = c.execute(
            "SELECT id, date FROM all_signals WHERE symbol=? AND signal_type=? "
            # No date window — see the note in duplicate_symbols(). An open
            # position blocks a new one whether it was filed last week or last
            # quarter, which is what the long-horizon engines need.
            "AND status='OPEN'",
            (sym_clean, signal_type)
        ).fetchone()
        # ── THE SINGLE-SYMBOL PATH HAS TO AGREE WITH THE BATCH ONE ────────
        #
        # duplicate_symbols() learned to retire an earlier day's ticket and
        # this did not, so the two answered differently about the same row:
        # the batch path let the signal through while this one still refused
        # it. test_alert_pipeline's "batch dedup agrees with is_duplicate" is
        # exactly that divergence, and it was a real bug, not a stale test.
        #
        # Same rule, same reason: an earlier day's ticket is retired so the
        # newer levels stand; one filed today is a duplicate and bounces.
        # The book holds one position per name; check that before the
        # per-engine rule, so both paths answer identically.
        pos = _open_position(c, sym_clean, signal_type)
        if pos:
            if cand:
                entry = cand.get("entry", cand.get("price"))
                if entry is not None:
                    _note_confirmation(c, pos[0], sym_clean, signal_type, entry,
                                       cand.get("action", "BUY"),
                                       cand.get("reasons") or cand.get("reason"))
            log.info(f"{sym_clean}: already held ({pos[1]}) — confirmation, not a new ticket")
            return True
        if row and REPLACE_ON_REDETECT and str(row[1] or "")[:10] != str(_date_ist()):
            c.execute("DELETE FROM all_signals WHERE id=?", (row[0],))
            log.info(f"Re-detected {sym_clean} ({signal_type}) — "
                     f"retired the earlier open ticket")
            row = None
        if row:
            log.debug(f"Duplicate skip: {sym_clean} ({signal_type}) already OPEN")
            if cand:
                entry = cand.get("entry", cand.get("price"))
                if entry is not None:
                    _note_resignal(c, row[0], sym_clean, entry,
                                    cand.get("action", "BUY"),
                                    cand.get("reasons") or cand.get("reason"))
                    c.commit()
                    _db.sync(c)
            return True
        # SL hit recently — avoid re-entry
        row = c.execute(
            "SELECT id FROM all_signals WHERE symbol=? AND signal_type=? "
            "AND status='SL_HIT' AND date>=?",
            (sym_clean, signal_type, cutoff_7d)
        ).fetchone()
        if row:
            log.debug(f"Duplicate skip: {sym_clean} ({signal_type}) SL hit recently")
            return True
        # Legacy: check old signals table too
        row = c.execute(
            "SELECT id FROM signals WHERE symbol=? AND status='OPEN' AND date>=?",
            (sym_clean, cutoff_5d)
        ).fetchone()
        if row:
            return True
    return False


def _note_resignal(c, existing_id, symbol, new_entry, new_action, reason=None):
    """Append a short remark to the still-open row a candidate was just
    dropped for, so the re-fire is visible instead of a debug line nobody
    reads. Appends rather than overwrites — a position can legitimately
    re-fire more than once over its life, and each occurrence is real
    information, not noise to replace.
    """
    note = f"{_today_ist()}: re-fired ({new_action} @ {new_entry})"
    if reason:
        note += f" — {reason}"
    note += ". Not logged as a separate row; this one stays the single open record."
    c.execute(
        "UPDATE all_signals SET duplicate_note = "
        "CASE WHEN duplicate_note IS NULL OR duplicate_note = '' THEN ? "
        "ELSE duplicate_note || char(10) || ? END WHERE id = ?",
        (note, note, existing_id)
    )


def _open_position(c, symbol, signal_type):
    """The one open position on this name, if the book already holds it.

    Keyed on SYMBOL, not symbol+engine, which is the whole change: a name is
    held once. Returns (id, signal_type, date) of the position of record —
    the EARLIEST open row, because the first trigger is the one a reader
    could have acted on.

    Only engines in BOOK_ENGINES share a position. A candidate from outside
    the book (an intraday engine, a commodity, a US name) is measured on its
    own and is never blocked by the book, nor does it block it.
    """
    if not POSITION_PER_NAME or signal_type not in BOOK_ENGINES:
        return None
    ph = ",".join("?" for _ in BOOK_ENGINES)
    try:
        return c.execute(
            f"SELECT id, signal_type, date FROM all_signals "
            f"WHERE symbol=? AND status='OPEN' AND signal_type IN ({ph}) "
            f"ORDER BY date ASC, id ASC LIMIT 1",
            (symbol, *sorted(BOOK_ENGINES))).fetchone()
    except Exception as e:                              # noqa: BLE001
        # Never cost the scan its signals over a lookup. Logging a duplicate
        # is strictly better than dropping the batch.
        log.warning(f"position lookup failed for {symbol} ({e}) — treating as no position")
        return None


def _note_confirmation(c, position_id, symbol, engine, entry, action, reason=None):
    """Record a later detection as EVIDENCE on the open position.

    This is the half of the model that makes it honest rather than merely
    quiet. Refusing a second ticket without recording it would throw away the
    most interesting thing the second engine said: that it independently
    reached the same name. Kept on the position so the site can show
    "BREACH also fired, 12 Sep at 403.60" under the one row a reader holds.
    """
    note = (f"{_today_ist()}: confirmed by {engine} ({action} @ {entry})")
    if reason:
        note += f" — {reason}"
    note += ". Recorded against this position; not filed as a second ticket."
    c.execute(
        "UPDATE all_signals SET duplicate_note = "
        "CASE WHEN duplicate_note IS NULL OR duplicate_note = '' THEN ? "
        "ELSE duplicate_note || char(10) || ? END WHERE id = ?",
        (note, note, position_id))


def _cross_engine_duplicate(c, symbol, entry, today, signal_type):
    """The same position, filed by a DIFFERENT engine, today, at the same entry.

    is_duplicate() keys on symbol + signal_type, so it cannot see this: two
    engines that independently pick the same name on the same day write two
    rows with identical entry, stop and targets. That is one position, and
    counting it twice inflates expectancy, doubles the engine's apparent hit
    count, and — on the crore book — would allocate capital to it twice.

    Fifteen such pairs are in the live ledger. SONACOMS on 2026-08-25 is the
    clearest: `ohl` and `breakout` both filed 808.80 / 796.67 and both booked
    the identical -1.50%. The pair reads as two independent losses. It is one.

    A hardcoded magic<->magicmagic check already existed in
    _log_magic_to_ledger. It was correct and far too narrow: the same collision
    happens across ohl/breakout, ai_longterm/breakout, multibagger/breakout and
    ai_4h/4h. This generalises it to every engine and moves it to the write
    path, where a new engine inherits it without anyone remembering to.

    Keyed on the ENTRY PRICE, deliberately. Two engines wanting the same symbol
    at different levels are two real trades with different risk — those stay.
    Rounded to 2dp because the engines compute entry independently and float
    noise in the 6th decimal must not defeat the match.
    """
    if entry is None:
        return None
    try:
        row = c.execute(
            "SELECT id, signal_type FROM all_signals "
            "WHERE symbol=? AND date=? AND signal_type<>? AND entry IS NOT NULL "
            "AND ROUND(entry, 2)=ROUND(?, 2) ORDER BY id LIMIT 1",
            (symbol, today, signal_type, float(entry))).fetchone()
    except Exception as e:                              # noqa: BLE001
        # A failed lookup must never cost the scan its signals. Logging one
        # duplicate is strictly better than losing the whole batch.
        log.warning(f"cross-engine duplicate check failed for {symbol} ({e}) — logging it")
        return None
    return (row[0], row[1]) if row else None


# ── ONE POSITION PER NAME. THE FIRST TRIGGER OWNS IT. ───────────────────────
#
# Akshay, 2026-09-15: "retiring 1st trigger from any setup & keeping next
# signal from different engine is not good to avoid duplicate — find a better
# solution." He is right, and the reason is measurement.
#
# WHAT WAS WRONG WITH REPLACING. Retiring the earlier ticket and filing the
# newer one keeps today's levels, but it rewrites the trade: the holding period
# resets on every re-detection, and R is measured from an entry that was never
# the trigger anyone could have acted on. A book whose entry moves every week
# has no holding period and no honest expectancy.
#
# WHAT WAS WRONG WITH KEEPING BOTH. Two engines reaching the same name filed
# two tickets on ONE company. Measured on the live ledger the day this was
# written: 36 second tickets opened while the first was still open, and 30
# names carried two engines at once. PAYTM is the clearest — multibagger filed
# it 25 Jul, filed it AGAIN 1 Aug while the first was still open, and both
# closed at T2. One idea, counted twice, in a published win rate.
#
# THE MODEL. A name has ONE open position. The first trigger opens it and owns
# the entry, the stop, the targets and the date — nothing later moves them.
# Every later detection while it is open, from ANY engine in the book, is a
# CONFIRMATION recorded against that position: which engine, when, at what
# price. No second row, no second alert, no second entry in the record.
#
# WHAT THAT BUYS: a real holding period, one R per idea, and multi-engine
# agreement becomes evidence ("BREACH also fired") instead of duplicate risk.
# WHAT IT COSTS, stated plainly: a name already open cannot be re-entered at a
# better level until it closes. That is the price of a stable record, and it is
# the discipline a real book runs.
#
# SCOPE IS THE PUBLISHED BOOK. Only engines a reader can actually see and size
# share a position. An intraday engine and a six-month thesis are different
# books, and blocking one on the other would destroy its measurement.
POSITION_PER_NAME = True

# The engines that appear on signal.askakshay.com — the ones a reader can size.
# Kept here rather than imported so the bot does not depend on the website.
BOOK_ENGINES = {
    "breakout", "magic", "magicmagic", "ohl", "multibagger",
    "momentum_quant", "ai_longterm", "ledge", "keel",
    # PIVOT joins the book on the day it is written, BEFORE it has published
    # anything — so its first signal is already subject to one-position-per-name
    # rather than being exempt until someone remembers to add it. A new engine
    # outside this set is a new engine that can duplicate the whole book.
    "pivot",
}

# Retained for the rollback path only. With POSITION_PER_NAME on, nothing is
# retired: the first trigger stands and later ones attach to it.
REPLACE_ON_REDETECT = False


def duplicate_symbols(candidates, signal_type="swing"):
    """Batch form of is_duplicate — returns the set of symbols to skip.

    `candidates` may be plain symbol strings (old call sites still work) or
    the candidate dicts themselves (symbol + entry/price + action + an
    optional reason/reasons string). Pass the dicts to get a remark left on
    the still-open row for an OPEN-duplicate — see _note_resignal().

    Same three rules, resolved in one connection instead of one per symbol.
    Scanning 53 breakouts through is_duplicate() opened 106 connections; under
    Turso each of those does a replica sync.
    """
    cand_by_symbol = {}
    for item in candidates:
        if isinstance(item, dict):
            sym = str(item.get("symbol", "")).replace(".NS", "")
        else:
            sym = str(item or "").replace(".NS", "")
        if sym:
            cand_by_symbol.setdefault(sym, item if isinstance(item, dict) else None)
    clean = set(cand_by_symbol)
    if not clean:
        return set()
    init_db()
    cutoff_5d = str(_date_ist() - timedelta(days=5))
    cutoff_7d = str(_date_ist() - timedelta(days=7))
    ordered = sorted(clean)
    ph = ",".join("?" for _ in ordered)
    dupes = set()
    wrote_note = False
    with _conn() as c:
        # NO date window on the OPEN check. It used to carry cutoff_5d, and
        # that is why the long-horizon engines filed the same name over and
        # over: multibagger rescans WEEKLY and holds for 6-12 months, so
        # every re-detection landed 7 days later — outside a 5-day window —
        # and passed the guard. GLAND ended up with three open positions
        # (2026-06-13, 07-11, 07-25) and OFSS with five, which triples a
        # name's weight in the ledger when a reader holds one.
        #
        # An open position is open. Whether it was filed five days ago or
        # five months ago changes nothing about whether a second one should
        # exist. The window was only ever needed because before the time
        # stop worked, rows stayed OPEN forever and a windowless check would
        # have blocked a symbol permanently — that is no longer true (see
        # standalone_scan._max_hold_hours, now horizon-aware per engine).
        #
        # Fetched separately from the other two rules (below) because this is
        # the one case that also leaves a remark, which needs the row id, not
        # just the symbol DISTINCT gives the other two.
        # ── SIBLING ENGINES COUNT AS THE SAME ENGINE ────────────────────
        #
        # magic and magicmagic are one screen at two depths off the 52-week
        # high. _log_magic_to_ledger already refuses a same-day pair, but an
        # OPEN position from the sibling a week earlier passed straight
        # through: J&KBANK carried magic (09-12) over magicmagic (09-05), two
        # tickets on one idea.
        #
        # A reader holding the first does not want the second — it is the same
        # thesis re-detected at a different price, which is precisely what the
        # windowless OPEN check above was written to stop. It was only ever
        # scoped too narrowly: to the exact engine name rather than the family.
        _FAMILY = {"magic": ("magic", "magicmagic"),
                   "magicmagic": ("magic", "magicmagic")}
        _fam = _FAMILY.get(signal_type, (signal_type,))
        _fph = ",".join("?" for _ in _fam)
        open_rows = c.execute(
            f"SELECT id, symbol, date FROM all_signals WHERE symbol IN ({ph}) "
            f"AND signal_type IN ({_fph}) AND status='OPEN'",
            tuple(ordered) + tuple(_fam)
        ).fetchall()
        # ── ONE POSITION PER NAME, ACROSS THE WHOLE BOOK ──────────────────
        #
        # Runs BEFORE the same-engine rule below and supersedes it: if the
        # book already holds this name — under ANY book engine — the
        # candidate is a confirmation, full stop. The same-engine loop then
        # has nothing left to do for those symbols.
        if POSITION_PER_NAME and signal_type in BOOK_ENGINES:
            for sym in ordered:
                pos = _open_position(c, sym, signal_type)
                if not pos:
                    continue
                dupes.add(sym)
                cand = cand_by_symbol.get(sym)
                entry = (cand or {}).get("entry", (cand or {}).get("price")) if cand else None
                if entry is not None:
                    _note_confirmation(c, pos[0], sym, signal_type, entry,
                                       (cand or {}).get("action", "BUY"),
                                       (cand or {}).get("reasons") or (cand or {}).get("reason"))
                log.info(f"{sym}: already held ({pos[1]}, opened {str(pos[2])[:10]}) — "
                         f"{signal_type} recorded as a confirmation, not a second ticket")

        # ── LATEST WINS, SO A RE-DETECTION REPLACES RATHER THAN BOUNCES ──
        #
        # Akshay, 2026-09-14: "keep only unique ones, remove earlier ones".
        # This guard used to refuse the new signal and leave the open one
        # standing, which keeps the EARLIEST — the opposite rule, and it is
        # what let 46 stale tickets accumulate before the cleanup.
        #
        # The newest filing carries the levels the engine would place today.
        # An August entry against a September price is a plan for a trade that
        # is no longer available, so the old row is retired and the new one is
        # allowed through. The retired row is deleted rather than cancelled for
        # the reason dedupe_open.yml states: a duplicate was never a distinct
        # call, and correcting a double entry is not erasing a record.
        #
        # ── AND IT ONLY APPLIES TO A TICKET FROM AN EARLIER DAY ───────────
        #
        # The first version of this replaced ANY open row, which broke the
        # bot for two days and would have flooded the channel had it run.
        # test_alert_pipeline caught it in three places and was right in all
        # three: "re-running a scan re-alerts nothing (dedup holds)".
        #
        # Two different events were being treated as one:
        #
        #   · A LATER DAY's re-detection — the weekly multibagger rescan
        #     finding the same name again next Saturday. The new filing
        #     carries today's levels, so it should replace. This is what he
        #     asked for.
        #   · THE SAME DAY's second look — the scan re-running, the watchdog
        #     re-dispatching, a manual re-run. Nothing has changed since the
        #     row was written minutes ago. Replacing it deletes and re-files
        #     the ticket, and re-files mean RE-ALERTS: every open name
        #     re-sent to Telegram on every run, which is the alert flood this
        #     repo has already lived through once.
        #
        # So: same-day is still a duplicate and still bounces, exactly as
        # before. Only a ticket filed on an earlier date is retired.
        today_ist = str(_date_ist())
        replaced = 0
        for row_id, sym, row_date in open_rows:
            same_day = str(row_date or "")[:10] == today_ist
            if REPLACE_ON_REDETECT and not same_day:
                c.execute("DELETE FROM all_signals WHERE id=?", (row_id,))
                replaced += 1
                continue
            dupes.add(sym)
            cand = cand_by_symbol.get(sym)
            if not cand:
                continue
            entry = cand.get("entry", cand.get("price"))
            if entry is None:
                continue
            action = cand.get("action", "BUY")
            reason = cand.get("reasons") or cand.get("reason")
            _note_resignal(c, row_id, sym, entry, action, reason)
            wrote_note = True
        if replaced:
            c.commit()
            _db.sync(c)
            log.info("%s: replaced %d open ticket(s) — the newer read wins",
                     signal_type, replaced)

        for sql, params in (
            (f"SELECT DISTINCT symbol FROM all_signals WHERE symbol IN ({ph}) "
             f"AND signal_type=? AND status='SL_HIT' AND date>=?",
             tuple(ordered) + (signal_type, cutoff_7d)),
            # Legacy: the old signals table is not signal_type aware.
            (f"SELECT DISTINCT symbol FROM signals WHERE symbol IN ({ph}) "
             f"AND status='OPEN' AND date>=?",
             tuple(ordered) + (cutoff_5d,)),
        ):
            dupes.update(r[0] for r in c.execute(sql, params).fetchall())

        if wrote_note:
            c.commit()
            _db.sync(c)
    if dupes:
        log.info(f"Duplicate skip ({signal_type}): {len(dupes)} symbol(s) — "
                 f"{', '.join(sorted(dupes)[:10])}{'…' if len(dupes) > 10 else ''}")
    return dupes


def _validate_signal_ordering(action, entry, sl, t1, t2, t3=None):
    """Hard invariant gate — every signal type writes through
    log_to_all_signals()/log_batch_to_all_signals(), the only two places
    that INSERT INTO all_signals anywhere in this codebase (verified by
    grep), so this is the one place that guarantees no signal with
    inconsistent price levels reaches the ledger regardless of which
    engine produced it or whether that engine's own formula gets broken by
    a future change. A generator bug becomes a rejected-and-logged row
    here, not a published SONACOMS with target1 == target2.

    LONG:  sl < entry < t1 < t2 (<= t3 if present)
    SHORT: (t3 <=) t2 < t1 < entry < sl

    target3 allows EQUAL to target2 on purpose — engines with only two real
    target levels (run_4h_scan and others) pass t3=t2 as their established
    "no distinct third target" convention (see standalone_scan.py, e.g.
    "t3": b.get("target2", b["target1"])), not a bug. A STRICT t2<t3 here
    rejected every one of those rows and took down the whole daily scan on
    2026-08-17 — test_alert_pipeline.py's synthetic fixtures use the same
    t3=t2 convention, caught it in 28 seconds, but that pre-flight check
    running BEFORE the real scan meant zero real signals fired that day.
    Only a target3 that is actually WORSE than target2 (closer to entry,
    or on the wrong side) is rejected now — that's still a real bug, just
    not this one.

    Returns (True, None) or (False, reason-string).
    """
    required = {"entry": entry, "sl": sl, "target1": t1, "target2": t2}
    missing = [k for k, v in required.items() if v is None]
    if missing:
        return False, f"missing {', '.join(missing)}"
    for name, v in (("entry", entry), ("sl", sl), ("target1", t1), ("target2", t2), ("target3", t3)):
        if v is None:
            continue
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0:
            return False, f"{name}={v!r} is not a finite positive number"
    if t1 == t2:
        return False, f"target1 == target2 ({t1})"
    is_short = str(action or "").upper() == "SELL"
    if is_short:
        if not (t2 < t1 < entry < sl):
            return False, f"SHORT ordering violated: target2={t2} target1={t1} entry={entry} sl={sl}"
        if t3 is not None and not (t3 <= t2):
            return False, f"SHORT target3 ordering violated: target3={t3} target2={t2}"
    else:
        if not (sl < entry < t1 < t2):
            return False, f"LONG ordering violated: sl={sl} entry={entry} target1={t1} target2={t2}"
        if t3 is not None and not (t2 <= t3):
            return False, f"LONG target3 ordering violated: target2={t2} target3={t3}"
    return True, None


def log_to_all_signals(symbol, signal_type, action, entry, sl, t1, t2, t3, rr,
                        timeframe="SWING", score=0, metadata=None, remarks=None):
    """Unified signal logger — returns the new row id, or None if the
    signal was rejected by _validate_signal_ordering() (logged, not raised —
    a bad signal from one symbol must not crash the rest of the scan).

    The row is written with sent_at NULL. Delivery is recorded separately by
    mark_alerts_sent() once Telegram has actually accepted the message. The
    ledger must record every signal the scanner produced whether or not
    Telegram was reachable, so this deliberately does not depend on the send.
    """
    ok, reason = _validate_signal_ordering(action, entry, sl, t1, t2, t3)
    if not ok:
        log.error(f"all_signals REJECTED: {symbol} {signal_type} {action} — {reason} "
                  f"(entry={entry} sl={sl} t1={t1} t2={t2} t3={t3})")
        return None
    init_db()
    today = _today_ist()
    with _conn() as c:
        # market/asset_type are written HERE, not left to the schema defaults.
        # See symbols.classify: no writer ever set them, so commodities and FX
        # were stored as NSE equities and /api/ticker quoted SILVER as
        # SILVER.NS — a different company, at ₹233.
        _mkt, _atype = _classify(symbol)
        # One position, two engines. See _cross_engine_duplicate.
        clash = _cross_engine_duplicate(c, symbol, entry, today, signal_type)
        if clash:
            _note_resignal(c, clash[0], symbol, entry, action,
                           reason=f"also picked by {signal_type} at the same entry")
            c.commit()
            _db.sync(c)
            log.info(f"all_signals SKIPPED: {symbol} {signal_type} — {clash[1]} "
                     f"already filed this position today at {entry} (id={clash[0]})")
            return None
        c.execute("""INSERT INTO all_signals
            (date,signal_type,symbol,action,timeframe,entry,sl,target1,target2,target3,
             rr,score,status,sent_at,metadata,engine_version,market,asset_type,remarks)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',NULL,?,?,?,?,?)""",
            (today, signal_type, symbol, action, timeframe, entry, sl, t1, t2, t3,
             rr, score, json.dumps(metadata or {}), ENGINE_VERSION, _mkt, _atype,
             remarks or REMARKS.get(signal_type)))
        row_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
        _db.sync(c)
    log.info(f"all_signals logged: {symbol} {signal_type} {action} entry={entry} id={row_id}")
    return row_id


# ── THE TREND GATE ──────────────────────────────────────────────────────────
#
# Akshay: "quality over quantity — take inspiration from Investtech."
#
# What Investtech actually does that this book did not: it will not take a long
# against its own trend channel. Every engine here could, and did.
#
# THIS WAS MEASURED BEFORE IT WAS BUILT, against all 269 closed NSE-equity
# trades, with each name's bars cut at its own entry date so there is no
# lookahead. Expectancy of what the gate KEEPS against what it DROPS:
#
#     no filter                 264 trades   -0.044R   29.9% won
#     above the 200-day         192          -0.028R   (dropped -0.085R)
#     200-day rising            150          -0.004R   (dropped -0.097R)
#     stacked 50>200            137          +0.015R   (dropped -0.107R)
#     stacked + 200 rising      120          +0.042R   (dropped -0.115R)   <- this
#     + volume confirm           38          +0.090R   (dropped -0.066R)
#
# The separation is consistent: at every cut, what passes is better than what
# is dropped, and the dropped bucket is reliably negative. The gate turns a
# losing book into a flat one and removes 55% of the signals to do it.
#
# IT IS NOT AN EDGE AND THE COMMENT WILL NOT PRETEND IT IS. +0.042R at t=0.30
# over 120 trades is indistinguishable from zero. What the evidence supports is
# narrower and still worth having: these are the trades that were losing money,
# and they are identifiable before entry.
#
# The volume variant is better still and cuts the sample to 38, which is too
# few to set a rule on. It is left out deliberately rather than fitted.
#
# ONE FILTER THAT INVERTED, recorded so nobody adds it back: requiring a strong
# close (in the top 40% of the bar's range) made things WORSE — kept -0.122R
# against dropped +0.002R. The obvious quality test was the wrong one.
TREND_GATE_ON = os.environ.get("TREND_GATE", "1") != "0"


def _trend_ok(symbol: str):
    """(passed, why). A long needs the trend under it, or it does not go.

    Returns True on any failure to FETCH — a gate that silently drops every
    signal when Yahoo is slow is worse than no gate, and the failure mode has
    to be "publish and be judged", not "publish nothing and look healthy".
    """
    if not TREND_GATE_ON:
        return True, "gate off"
    try:
        import yfinance as yf
        sym = str(symbol or "").replace(".NS", "").replace(".BO", "")
        d = yf.Ticker(sym + ".NS").history(period="2y", interval="1d", auto_adjust=True)
        if d is None or len(d) < 220:
            return True, "no history — not gated"
        c = d["Close"]
        s50 = c.rolling(50).mean().iloc[-1]
        s200s = c.rolling(200).mean()
        s200, s200_prev = s200s.iloc[-1], s200s.iloc[-21]
        px = c.iloc[-1]
        if not (px > s50 > s200):
            return False, f"not stacked (px {px:.1f}, 50d {s50:.1f}, 200d {s200:.1f})"
        if not (s200 > s200_prev):
            return False, "200-day still falling"
        return True, "stacked, 200-day rising"
    except Exception as e:                                       # noqa: BLE001
        logging.warning(f"trend gate could not read {symbol} ({e}) — letting it through")
        return True, "gate unavailable"


def drop_retired(rows):
    """(rows that may file, rows refused) — split a batch on engine retirement.

    A PURE FUNCTION, deliberately, and separate from the write it guards. The
    first version of this lived inline inside log_batch_to_all_signals, which
    meant the only way to test it was to call a function that opens a database
    — and against Turso that is not an exception you can catch, it is a Rust
    panic through pyo3. A gate nobody can test in isolation is a gate nobody
    tests.

    WHY IT EXISTS. PLUMB (equity_measured) was retired on 2026-09-18 for being
    the only significantly losing engine — 16 closed, -0.535R, t = -2.92 — and
    it filed MARUTI and HINDUNILVR that same day. Both rows are in the ledger.
    Nobody saw them, because engines.js correctly refuses to publish a retired
    engine, so the retirement looked complete from outside while the scanner
    went on running it. Same shape as the fault the retirement commit fixed —
    lists disagreeing about which engines exist — except this time the two
    halves disagreed: the display knew and the generator did not.

    WHAT RETIREMENT MEANS, AND WHAT IT DOES NOT. It stops an engine FILING NEW
    SIGNALS. It says nothing about trades already open: `magic` has twenty of
    them and they are still managed, still stopped, still alerted — which is
    why a TIDAL alert can arrive on a Saturday with nothing new on the site.
    This function is on the write path for new rows and must never be reached
    from position management.

    An allocation is not a retired engine. top5_pick and sip_bucket are absent
    from RETIRED on purpose and pass through.

    FAILS OPEN, like the trend gate beside it: if the map cannot be imported,
    everything files. A gate that silently drops every signal when an import
    breaks looks healthy and publishes nothing.
    """
    try:
        from engine_names import RETIRED
    except Exception as e:                                     # noqa: BLE001
        logging.warning(f"retirement gate unavailable ({e}) — not filtering")
        return list(rows), []
    if not RETIRED:
        return list(rows), []
    live, refused = [], []
    for r in rows or []:
        k = str(r.get("signal_type") or "").strip()
        (refused if k in RETIRED else live).append(r)
    for r in refused:
        k = str(r.get("signal_type") or "").strip()
        logging.warning(f"  retired engine refused: {k} {r.get('symbol')} "
                        f"— retired {RETIRED.get(k)}")
    if refused:
        logging.info(f"retirement gate: {len(live)} filed, {len(refused)} refused "
                     f"of {len(rows)}")
    return live, refused


def log_batch_to_all_signals(rows, date=None):
    """Insert many signals over ONE connection. Returns row ids in input order.

    log_to_all_signals() opens a connection, init_db()s and syncs per call —
    against Turso that measured ~8.7s per row in CI. Fine when the caller was
    capped at 5 signals; a 50-signal scan would add minutes to every run.

    `rows` are dicts with the same keys as log_to_all_signals' arguments.

    `date` overrides the stamp, and exists for backfills only. Live engines
    must leave it None and take today. Without it, a backfill of four historical
    scans wrote all 56 rows stamped with the day the backfill RAN — four scan
    dates collapsed into one, which is both a lie about when the engine fired
    and a broken idempotence key: the next run finds nothing logged under the
    real dates and writes the whole set again.
    """
    if not rows:
        return []

    # A retired engine does not file. See drop_retired() for why this is a
    # separate function and for what retirement does NOT mean.
    rows, _refused = drop_retired(rows)
    if not rows:
        return []

    # THE GATE RUNS HERE because this is where every engine writes. Ten engines
    # with ten copies of one entry condition is the drift this file has already
    # recorded against four target ladders and three feed lists.
    #
    # Long NSE equities only: the gate is a statement about an equity trend and
    # says nothing about a currency or a short, and the book is long-only.
    if TREND_GATE_ON:
        kept, dropped = [], []
        for r in rows:
            mk = str(r.get("market") or "NSE").upper()
            act = str(r.get("action") or "BUY").upper()
            if mk != "NSE" or act not in ("BUY", "LONG"):
                kept.append(r)
                continue
            ok, why = _trend_ok(r.get("symbol"))
            (kept if ok else dropped).append(r)
            if not ok:
                logging.info(f"  trend gate dropped {r.get('symbol')} "
                             f"({r.get('signal_type')}): {why}")
        if dropped:
            logging.info(f"trend gate: {len(kept)} published, {len(dropped)} dropped "
                         f"of {len(rows)}")
        rows = kept
        if not rows:
            return []

    # ── ONE TICKET PER NAME PER ENGINE, ENFORCED WHERE EVERY ENGINE WRITES ──
    #
    # duplicate_symbols() checks the DATABASE before a batch is written, so two
    # candidates for the same symbol INSIDE one batch both pass it — the row
    # they would collide with does not exist yet. equity_measured filed TCS
    # twice and HDFCBANK twice on 2026-09-09, same engine, same day, two open
    # tickets each.
    #
    # That is not a re-entry, it is the same idea written down twice, and a
    # reader who sized both carries double the intended risk on one name.
    #
    # Guarded here rather than in each caller because this is the single
    # function every engine writes through: fixing it at the nine call sites
    # would be nine chances to miss one, and the tenth engine would arrive
    # without the guard. First occurrence wins — it is the one the scan ranked
    # highest — and the duplicates are counted, not silently dropped.
    _seen, _deduped, _within = set(), [], 0
    for _r in rows:
        _k = (str(_r.get("symbol", "")).replace(".NS", "").upper(),
              str(_r.get("signal_type", "")))
        if _k[0] and _k in _seen:
            _within += 1
            continue
        if _k[0]:
            _seen.add(_k)
        _deduped.append(_r)
    if _within:
        log.warning("ledger: dropped %d duplicate row(s) inside one batch — "
                    "same symbol and engine filed more than once", _within)
        rows = _deduped

    init_db()
    today = date or _today_ist()
    ids = []
    rejected = 0
    collided = 0
    with _conn() as c:
        for r in rows:
            # An allocation has no levels to order. Validating one means
            # inventing the levels first, which is the fiction this avoids.
            if r.get("signal_type") in ALLOCATION_TYPES:
                ok, reason = True, ""
            else:
                ok, reason = _validate_signal_ordering(
                    r.get("action", "BUY"), r["entry"], r["sl"], r["t1"], r["t2"], r.get("t3"))
            if not ok:
                log.error(f"all_signals batch REJECTED: {r['symbol']} {r.get('signal_type')} — {reason} "
                          f"(entry={r['entry']} sl={r['sl']} t1={r['t1']} t2={r['t2']} t3={r.get('t3')})")
                ids.append(None)  # keeps ids aligned with the input list's order/length
                rejected += 1
                continue
            # Same as log_to_all_signals: classify rather than inherit the
            # 'NSE'/'Equity' schema defaults. See symbols.classify.
            clash = _cross_engine_duplicate(c, r["symbol"], r.get("entry"),
                                            today, r["signal_type"])
            if clash:
                _note_resignal(c, clash[0], r["symbol"], r.get("entry"),
                               r.get("action", "BUY"),
                               reason=f"also picked by {r['signal_type']} at the same entry")
                log.info(f"all_signals batch SKIPPED: {r['symbol']} {r['signal_type']} — "
                         f"{clash[1]} already filed this position today (id={clash[0]})")
                ids.append(None)   # keeps ids aligned with the input list
                collided += 1
                continue
            _mkt, _atype = _classify(r["symbol"])
            c.execute("""INSERT INTO all_signals
                (date,signal_type,symbol,action,timeframe,entry,sl,target1,target2,target3,
                 rr,score,status,sent_at,metadata,engine_version,grade,breakeven_wr,turnover_cr,
                 market,asset_type,remarks)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',NULL,?,?,?,?,?,?,?,?)""",
                (today, r["signal_type"], r["symbol"], r.get("action", "BUY"),
                 r.get("timeframe", "SWING"), r["entry"], r["sl"],
                 r["t1"], r["t2"], r["t3"], r["rr"], r.get("score", 0),
                 json.dumps(r.get("metadata") or {}), ENGINE_VERSION,
                 r.get("grade"), r.get("breakeven_wr"), r.get("turnover_cr"),
                 _mkt, _atype, r.get("remarks") or REMARKS.get(r["signal_type"])))
            ids.append(c.execute("SELECT last_insert_rowid()").fetchone()[0])
        c.commit()
        _db.sync(c)
    if rejected:
        log.warning(f"all_signals batch: {rejected}/{len(rows)} row(s) rejected by ordering validation")
    if collided:
        log.warning(f"all_signals batch: {collided}/{len(rows)} row(s) already filed today "
                    f"by another engine at the same entry — noted, not double-logged")
    written = len(ids) - rejected - collided
    log.info(f"all_signals batch logged: {written}/{len(rows)} row(s) "
             f"({', '.join(r['symbol'] for r in rows[:8])}"
             f"{'…' if len(rows) > 8 else ''})")
    return ids


def mark_alerts_sent(row_ids, ok: bool, error: str = None):
    """Record the real outcome of the Telegram send for these all_signals rows.

    ok=True   → stamp sent_at, clear any previous error.
    ok=False  → leave sent_at NULL and store why. These rows are the backlog:
                `SELECT * FROM all_signals WHERE sent_at IS NULL` is exactly the
                set of signals the site shows but Telegram never delivered.
    """
    ids = [i for i in (row_ids or []) if i]
    if not ids:
        return 0
    init_db()
    placeholders = ",".join("?" for _ in ids)
    with _conn() as c:
        if ok:
            c.execute(
                f"UPDATE all_signals SET sent_at=?, send_error=NULL WHERE id IN ({placeholders})",
                tuple([_now_ist()] + ids))
        else:
            c.execute(
                f"UPDATE all_signals SET send_error=? WHERE id IN ({placeholders})",
                tuple([(error or "telegram send failed")[:300]] + ids))
        c.commit()
        _db.sync(c)
    if ok:
        log.info(f"alerts marked sent: {len(ids)} row(s)")
    else:
        log.error(f"alerts NOT delivered: {len(ids)} row(s) — {error}")
    return len(ids)

def is_muted(symbol):
    init_db()
    with _conn() as c:
        return bool(c.execute("SELECT 1 FROM muted_assets WHERE symbol=?", (symbol,)).fetchone())

def mute_asset(symbol):
    init_db()
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO muted_assets VALUES (?,?)",
                  (symbol, _now_ist()))
        c.commit()
        _db.sync(c)

def unmute_asset(symbol):
    init_db()
    with _conn() as c:
        c.execute("DELETE FROM muted_assets WHERE symbol=?", (symbol,))
        c.commit()
        _db.sync(c)

def update_outcomes():
    init_db()
    with _conn() as c:
        open_trades = pd.read_sql("SELECT * FROM signals WHERE status='OPEN'", c)

    for _, row in open_trades.iterrows():
        try:
            sym = to_yahoo(row["symbol"])
            df  = yf.download(sym, period="5d", interval="1d",
                              progress=False, auto_adjust=True)
            if df.empty:
                continue
            lo  = float(df["Low"].squeeze().min())
            hi  = float(df["High"].squeeze().max())
            entry = row["entry"]

            status, exit_p = "OPEN", None
            action = str(row.get("action", "BUY")).upper()

            if action == "SELL":
                # Short: SL above entry, targets below entry
                if hi >= row["sl2"]:
                    status, exit_p = "SL_HIT", row["sl2"]
                elif lo <= row["target2"]:
                    status, exit_p = "T2_HIT", row["target2"]
                elif lo <= row["target1"]:
                    status, exit_p = "T1_HIT", row["target1"]
            else:  # BUY
                if lo <= row["sl2"]:
                    status, exit_p = "SL_HIT", row["sl2"]
                elif hi >= row["target2"]:
                    status, exit_p = "T2_HIT", row["target2"]
                elif hi >= row["target1"]:
                    status, exit_p = "T1_HIT", row["target1"]

            if status != "OPEN":
                if action == "SELL":
                    pnl  = round((entry - exit_p) / entry * 100, 2)
                    risk = row["sl2"] - entry
                    r_mult = round((entry - exit_p) / risk, 2) if risk > 0 else 0
                else:
                    pnl = round((exit_p - entry) / entry * 100, 2)
                    risk = entry - row["sl2"]
                    r_mult = round((exit_p - entry) / risk, 2) if risk > 0 else 0
                with _conn() as c:
                    c.execute(
                        "UPDATE signals SET status=?,exit_price=?,pnl_pct=?,r_multiple=? WHERE id=?",
                        (status, exit_p, pnl, r_mult, row["id"])
                    )
                    c.commit()
                    _db.sync(c)
        except Exception:
            continue

def update_all_outcomes():
    """
    Walk each open signal's candles in order and record what actually happened.

    Replaces a version that collapsed the whole post-signal window into a single
    min/max pair and then tested the stop first:

        lo = float(low_s.min())      # lowest low since the signal, ever
        hi = float(high_s.max())     # highest high since the signal, ever
        if lo <= sl:   status = "SL_HIT"
        elif hi >= t2: status = "T2_HIT"

    Three defects, all pushing the same direction:

      1. Order was discarded. A trade that ran to T2 on day 2 and grazed its
         stop on day 30 booked as SL_HIT. With 323 stops against 178 targets in
         the ledger, this is not a rounding error.
      2. The window had no end. `yf.download(start=...)` runs to today, so given
         enough sessions every position eventually touches its stop.
      3. Entry was assumed. `entry_triggered_at` was never written by any code
         path, so the dashboard — which defines a position as a signal carrying
         that timestamp — has shown 0% deployed since the column was added,
         while this function booked P&L on trades that were never entered.

    Now: bars are walked chronologically inside a bounded window, entry must be
    touched before the trade can resolve, and the first level touched wins. A
    bar that straddles both levels books the stop and sets `exit_ambiguous`, so
    the assumption is counted rather than hidden.
    """
    init_db()
    with _conn() as c:
        try:
            open_trades = pd.read_sql(
                "SELECT * FROM all_signals WHERE status='OPEN'", c)
        except Exception:
            return

    today = datetime.now(_IST).date()

    for _, row in open_trades.iterrows():
        try:
            sym = to_yahoo(row["symbol"])
            sig_date_str = str(row.get("date", "")).strip()
            try:
                sig_dt = datetime.strptime(sig_date_str, "%Y-%m-%d").date()
            except Exception:
                continue

            # Never retro-fill an entry into the pre-cutoff backlog.
            if sig_date_str < LIFECYCLE_EPOCH and not row.get("entry_triggered_at"):
                continue

            # Start from the next session so the signal's own candle cannot
            # trip the stop it was measured from.
            start_d = sig_dt + timedelta(days=1)
            if start_d > today:
                continue

            # Bound the window. Calendar days are ~1.45x sessions; the extra
            # margin is trimmed by the session counter in the walk below.
            # Engine and its own stated horizon, not just the timeframe — see
            # _max_hold_hours above. A None means the horizon is unknown, and
            # the walk is then bounded by today rather than by a guessed limit.
            try:
                _meta = json.loads(row.get("metadata") or "{}")
            except (TypeError, ValueError):
                _meta = {}
            hold_h = _max_hold_hours(
                str(row.get("timeframe", "")),
                engine=str(row.get("signal_type") or _meta.get("engine") or ""),
                horizon=str(_meta.get("horizon") or ""))
            end_d = (today if hold_h is None
                     else min(today, start_d + timedelta(days=int(hold_h / 24) + 4)))

            df = yf.download(sym, start=start_d.isoformat(),
                             end=(end_d + timedelta(days=1)).isoformat(),
                             interval="1d", progress=False, auto_adjust=True)
            if df is None or df.empty:
                continue

            # yfinance returns MultiIndex columns — ("Close", "HINDALCO.NS") —
            # even for a single ticker. `bar["High"]` then yields a Series, and
            # float(Series) raises. The whole walk sits inside `except
            # Exception: continue`, so EVERY signal was skipped with nothing
            # but a log.warning: this grader has not resolved a single trade,
            # which is why the only outcomes in the ledger came from
            # standalone_scan's cruder path and its phantom stop-outs.
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            entry = float(row["entry"])
            raw_sl = row["sl"]
            if raw_sl is None or (isinstance(raw_sl, float) and math.isnan(raw_sl)):
                # No stop means no risk denominator, so no honest R can be
                # computed. 24 rows in the feed are in this state and were being
                # silently assigned a fabricated 4% stop.
                continue
            sl = float(raw_sl)
            t1 = _f(row["target1"])
            t2 = _f(row["target2"])
            if t1 is None and t2 is None:
                continue
            is_long = str(row.get("action", "BUY")).upper() != "SELL"

            risk = abs(entry - sl)
            if risk <= 0:
                log.warning(f"{row['symbol']}: stop on wrong side of entry — skipped")
                continue

            # None IS A VALID HORIZON HERE AND THIS LINE DID NOT KNOW IT.
            #
            # _max_hold_hours returns None for an unknown horizon on purpose —
            # "manage the levels, never time-stop it" — and the end_d line
            # eighteen lines up handles that correctly. This one divided None
            # by 24 and raised, which the outer handler swallowed as
            # "unsupported operand type(s) for /: 'NoneType' and 'int'". The
            # row was then skipped ENTIRELY: no excursions, no max_profit, no
            # resolution. Measured on 2026-09-09 it silently dropped every
            # momentum_quant and sip_bucket signal on a 1M timeframe.
            #
            # No horizon means no time stop, exactly as the comment above says.
            # The walk is already bounded by end_d, which is today.
            max_sessions = None if hold_h is None else max(1, int(hold_h / 24))
            triggered_at = row.get("entry_triggered_at")
            status, exit_p, ambiguous = None, None, 0
            sessions = 0
            last_close = None
            exit_day = None
            best_fav, worst_adv = 0.0, 0.0     # excursions, in price terms

            for ts, bar in df.iterrows():
                hi = float(bar["High"])
                lo = float(bar["Low"])
                last_close = float(bar["Close"])
                bar_day = ts.date() if hasattr(ts, "date") else ts

                # ── Entry ────────────────────────────────────────────
                # A limit order fills when price trades through it. Until it
                # does, there is no position and nothing to resolve.
                if not triggered_at:
                    touched = lo <= entry if is_long else hi >= entry
                    if not touched:
                        continue
                    triggered_at = bar_day.isoformat()
                    with _conn() as c:
                        # fill_type is written only if nothing has claimed it.
                        # A user confirmation must never be downgraded back to
                        # SIMULATED by a later scan.
                        c.execute(
                            "UPDATE all_signals SET entry_triggered_at=?, "
                            "lifecycle_status='Triggered', "
                            "fill_type=COALESCE(fill_type,'SIMULATED') WHERE id=?",
                            (triggered_at, int(row["id"]))
                        )
                        c.commit()
                        _db.sync(c)
                    log.info(f"Entry touched (SIMULATED): {row['symbol']} @ {entry} "
                             f"on {triggered_at} — no order was placed")
                    # Fall through — the fill bar can also resolve the trade.

                sessions += 1

                # ── Excursion ────────────────────────────────────────
                # How far the trade ran in favour, and how far against,
                # while it was open. Recorded in R because that is the only
                # unit comparable across a 90-rupee stock and a 4000-dollar
                # ounce of gold.
                #
                # This is what tells you whether the stops are wrong. A book
                # of losers that each ran +1.5R in favour before turning is a
                # trailing-stop problem; a book of losers that never went
                # green is a selection problem. Without it, both look
                # identical in the ledger — a column of -1R.
                fav = (hi - entry) if is_long else (entry - lo)
                adv = (entry - lo) if is_long else (hi - entry)
                if fav > best_fav: best_fav = fav
                if adv > worst_adv: worst_adv = adv

                # ── Exit, in the order the levels were reached ───────
                hit_sl = (lo <= sl) if is_long else (hi >= sl)
                target = t2 if t2 is not None else t1
                hit_t = (hi >= target) if is_long else (lo <= target)

                if hit_sl and hit_t:
                    # Daily resolution cannot say which came first. Book the
                    # stop and flag it rather than choosing the flattering one.
                    status, exit_p, ambiguous = "SL_HIT", sl, 1
                elif hit_sl:
                    status, exit_p = "SL_HIT", sl
                elif hit_t:
                    status = "T2_HIT" if t2 is not None else "T1_HIT"
                    exit_p = target
                elif max_sessions is not None and sessions >= max_sessions:
                    # Time stop. Capital held in a setup that has not worked is
                    # capital unavailable for one that will. Skipped entirely
                    # when the horizon is unknown — a guessed limit closes real
                    # positions, which is the fault _max_hold_hours was changed
                    # to stop returning a number for.
                    status, exit_p = "TIME_STOP", last_close

                if status:
                    # The bar that resolved it — NOT the clock. closed_at was
                    # datetime.now(), so it recorded when this job happened to
                    # run, which is why 25 trades in the ledger are stamped
                    # closed on a Saturday with the exchange shut. A close date
                    # that cannot be reconciled against a session is worthless
                    # for any holding-period or day-of-week analysis.
                    exit_day = bar_day.isoformat()
                    break

            if not status or exit_p is None:
                continue

            # Book at the level that would have filled, not the bar's extreme.
            direction = 1 if is_long else -1
            pnl = round((exit_p - entry) / entry * 100 * direction, 2)
            r_m = round((exit_p - entry) / risk * direction, 2)

            with _conn() as c:
                c.execute(
                    "UPDATE all_signals SET status=?,exit_price=?,pnl_pct=?,"
                    "r_multiple=?,exit_ambiguous=?,closed_at=?,"
                    "max_profit_pct=?,max_drawdown_pct=? WHERE id=?",
                    (status, exit_p, pnl, r_m, ambiguous,
                     exit_day or datetime.now(_IST).date().isoformat(),
                     round(best_fav / risk, 3), round(-worst_adv / risk, 3),
                     int(row["id"]))
                )
                c.commit()
                _db.sync(c)
            log.info(
                f"Outcome updated: {row['symbol']} {status} "
                f"pnl={pnl}% r={r_m}{' AMBIGUOUS' if ambiguous else ''}"
            )
        except Exception as e:
            log.warning(f"update_all_outcomes {row.get('symbol','?')}: {e}")
            continue


def set_fill_type(symbol: str, fill_type: str, qty=None, price=None):
    """
    Mark the newest open position in `symbol` as CONFIRMED or SKIPPED.

    This is the only writer of CONFIRMED anywhere in the codebase, and it is
    reachable only from a Telegram command a human typed. The scanner writes
    SIMULATED and nothing else — it infers fills from price touching a level,
    which is a paper trade, not an execution.

    Returns (ok, message) so the caller can reply either way.
    """
    init_db()
    symbol = symbol.upper()
    with _conn() as c:
        c.row_factory = _db.Row
        row = c.execute(
            "SELECT id, symbol, entry, sl, target2, target1, fill_type "
            "FROM all_signals WHERE UPPER(symbol)=? AND status='OPEN' "
            "ORDER BY date DESC LIMIT 1",
            (symbol,)
        ).fetchone()

    if row is None:
        return False, (f"No open setup for *{symbol}*. "
                       f"Use /book to see what is live.")

    prev = row["fill_type"] or "SIMULATED"
    if fill_type == "SKIPPED":
        with _conn() as c:
            c.execute("UPDATE all_signals SET fill_type='SKIPPED' WHERE id=?",
                      (int(row["id"]),))
            c.commit()
            _db.sync(c)
        return True, (f"⏭ *{symbol}* marked SKIPPED. It stays in the shadow "
                      f"ledger for measurement but claims no capital.")

    fill_price = price if price is not None else float(row["entry"])
    with _conn() as c:
        c.execute(
            "UPDATE all_signals SET fill_type='CONFIRMED', confirmed_at=?, "
            "confirmed_qty=?, confirmed_price=? WHERE id=?",
            (datetime.now(_IST).isoformat(), qty, fill_price, int(row["id"]))
        )
        c.commit()
        _db.sync(c)

    qty_txt = f"{qty} sh" if qty else "ticket qty"
    return True, (f"✅ *{symbol}* CONFIRMED at ₹{fill_price:,.2f} ({qty_txt}).\n"
                  f"Was {prev}. It now counts toward deployed capital and P&L.")


def book_summary() -> str:
    """Live book split by whether the capital is real."""
    init_db()
    with _conn() as c:
        c.row_factory = _db.Row
        rows = c.execute(
            "SELECT symbol, entry, sl, fill_type, confirmed_qty, confirmed_price, "
            "entry_triggered_at, signal_type FROM all_signals "
            "WHERE status='OPEN' AND entry_triggered_at IS NOT NULL "
            "ORDER BY date DESC"
        ).fetchall()

    if not rows:
        return ("📋 *Book*\n\nNothing has triggered yet. "
                "Setups are waiting for price to reach entry.")

    conf = [r for r in rows if (r["fill_type"] or "") == "CONFIRMED"]
    sim = [r for r in rows if (r["fill_type"] or "SIMULATED") == "SIMULATED"]
    skip = [r for r in rows if (r["fill_type"] or "") == "SKIPPED"]

    out = ["📋 *Book*", ""]
    if conf:
        out.append("*CONFIRMED — real capital*")
        for r in conf:
            px = float(r["confirmed_price"] or r["entry"])
            qty = r["confirmed_qty"]
            qty_txt = f" × {qty}" if qty else ""
            out.append(f"  • {r['symbol']} @ ₹{px:,.2f}{qty_txt}")
    else:
        out.append("*CONFIRMED — real capital*\n  none")
    out.append("")
    out.append(f"*SIMULATED — paper only ({len(sim)})*")
    for r in sim[:10]:
        out.append(f"  • {r['symbol']} @ ₹{float(r['entry']):,.2f} · {r['signal_type']}")
    if skip:
        out.append("")
        out.append(f"*SKIPPED* — {', '.join(r['symbol'] for r in skip[:10])}")
    out.append("")
    out.append("_Simulated fills are inferred from price touching entry. "
               "No order was placed. Use /confirm SYMBOL when you place one._")
    return "\n".join(out)


def get_performance():
    """Performance from ALL signal types (unified all_signals table)."""
    init_db()
    try:
        with _conn() as c:
            df = pd.read_sql("SELECT * FROM all_signals", c)
    except Exception:
        df = pd.DataFrame()
    # Fallback: also pull from legacy signals table
    try:
        with _conn() as c:
            df_leg = pd.read_sql("SELECT *, 'swing' AS signal_type FROM signals", c)
        if not df_leg.empty:
            # align columns
            df_leg = df_leg.rename(columns={"sl2": "sl", "r_multiple": "r_multiple"})
            common = [c for c in df_leg.columns if c in df.columns or df.empty]
            if df.empty:
                df = df_leg[common] if common else df_leg
            else:
                df = pd.concat([df, df_leg[[c for c in df_leg.columns if c in df.columns]]], ignore_index=True)
    except Exception:
        pass
    if df.empty:
        return {}
    closed = df[df["status"] != "OPEN"].copy()
    if "pnl_pct" not in closed.columns:
        return {}
    closed["pnl_pct"] = pd.to_numeric(closed["pnl_pct"], errors="coerce").fillna(0)
    wins   = closed[closed["pnl_pct"] > 0]
    losses = closed[closed["pnl_pct"] <= 0]
    gross_profit = wins["pnl_pct"].sum() if len(wins) > 0 else 0
    gross_loss   = abs(losses["pnl_pct"].sum()) if len(losses) > 0 else 1
    by_type = {}
    if "signal_type" in closed.columns and len(closed) > 0:
        by_type = closed.groupby("signal_type")["pnl_pct"].mean().round(2).to_dict()
    return {
        "total":         len(df),
        "closed":        len(closed),
        "open":          len(df[df["status"] == "OPEN"]),
        "win_rate":      round(len(wins) / len(closed) * 100, 1) if len(closed) > 0 else 0,
        "avg_pnl":       round(float(closed["pnl_pct"].mean()), 2) if len(closed) > 0 else 0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0,
        "avg_r":         round(float(closed["r_multiple"].mean()), 2) if "r_multiple" in closed.columns and len(closed) > 0 else 0,
        "best":          round(float(closed["pnl_pct"].max()), 2) if len(closed) > 0 else 0,
        "worst":         round(float(closed["pnl_pct"].min()), 2) if len(closed) > 0 else 0,
        "by_type":       by_type,
    }

def get_site_record():
    """What signal.askakshay.com is accountable for — the number for the phone.

    get_performance() above opens with "Performance from ALL signal types" and
    means it: every engine including retired ones, every date including the
    months before this site existed, shorts that were never sent to anybody,
    and COMEX gold priced in dollars. That is a real figure about the LEDGER
    and it is the wrong figure to answer "how are we doing" with, because it
    is not the figure any page shows.

    On 2026-09-19 the site published 45 signals since launch with 13 closed at
    7.7% and -0.765R. /performance, reading the same database, would have
    quoted a different total, a different win rate and a different expectancy
    — a fifth answer to one question, and the only one that arrives unprompted
    on a phone.

    This applies engine_names.in_book(), which mirrors ENGINE_BOOK.inBook() in
    the browser: live engine, long only, rupee-priced, on or after LAUNCH.

    r_multiple IS CHECKED FOR NULL FIRST. float(None) raises but
    pd.to_numeric(...).fillna(0) does not — it turns an ungraded row into a
    closed trade booked at exactly 0R, which lands in the count, the win rate
    and the expectancy. The site fixed this same hole in three places; this is
    the fourth.
    """
    from engine_names import LAUNCH, in_book
    init_db()
    try:
        with _conn() as c:
            df = pd.read_sql("SELECT * FROM all_signals", c)
    except Exception:                                       # noqa: BLE001
        return {}
    if df.empty:
        return {}

    df = df[df.apply(lambda r: in_book(r.to_dict()), axis=1)]
    if df.empty:
        return {"published": 0, "closed": 0, "open": 0, "launch": LAUNCH}

    r = pd.to_numeric(df.get("r_multiple"), errors="coerce")
    status = df.get("status").astype(str).str.upper()
    badge = df.get("badge", pd.Series("", index=df.index)).astype(str).str.lower()
    scored = r.notna() & (status != "OPEN") & (badge != "open")

    closed = df[scored]
    rr = r[scored]
    n = len(closed)
    wins = int((rr > 0).sum())
    out = {"published": len(df), "closed": n, "open": int((status == "OPEN").sum()),
           "launch": LAUNCH, "wins": wins, "losses": n - wins}
    if n:
        mean = float(rr.mean())
        out["win_rate"] = round(wins / n * 100, 1)
        out["avg_r"] = round(mean, 3)
        if n > 1:
            sd = float(rr.std(ddof=1))
            out["t"] = round(mean / (sd / (n ** 0.5)), 2) if sd else None
        by = {}
        for k, grp in closed.groupby(closed["signal_type"].astype(str)):
            gr = pd.to_numeric(grp["r_multiple"], errors="coerce").dropna()
            if len(gr):
                by[k] = {"n": len(gr), "avg_r": round(float(gr.mean()), 3)}
        out["by_engine"] = by
    return out


def get_active_signals():
    init_db()
    with _conn() as c:
        return pd.read_sql("SELECT * FROM signals WHERE status='OPEN' ORDER BY date DESC", c)

def get_history():
    init_db()
    with _conn() as c:
        return pd.read_sql("SELECT * FROM signals ORDER BY date DESC", c)  # full history


# ── Breakouts ─────────────────────────────────────────────────────────────────
def log_breakouts(breakouts):
    init_db()
    today = _today_ist()
    with _conn() as c:
        # Replace only the symbols in this batch. Deleting the whole day meant
        # the EOD run wiped the midday run's rows — and because dedup excludes
        # anything already alerted, those symbols were not in the EOD list to be
        # re-inserted, so the day's breakout history lost them entirely.
        syms = sorted({b["symbol"] for b in breakouts})
        if syms:
            ph = ",".join("?" for _ in syms)
            c.execute(f"DELETE FROM breakouts WHERE date=? AND symbol IN ({ph})",
                      tuple([today] + syms))
        for b in breakouts:
            c.execute("""INSERT INTO breakouts
                (date,symbol,timeframe,pattern,patterns,price,sl,target1,target2,target3,rr,vol_ratio,fno,tv_link)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (today, b["symbol"], b["timeframe"], b["pattern"],
                 json.dumps(b.get("patterns", [])),
                 b["price"], b["sl"], b["target1"], b["target2"], b["target3"],
                 b["rr"], b["vol_ratio"], int(b.get("fno", False)), b.get("tv_link", "")))
        c.commit()
        _db.sync(c)

def get_breakouts(days=3):
    init_db()
    cutoff = str(_date_ist() - timedelta(days=days))
    with _conn() as c:
        df = pd.read_sql(
            "SELECT * FROM breakouts WHERE date>=? ORDER BY date DESC, rr DESC",
            c, params=(cutoff,))
    if not df.empty and "patterns" in df.columns:
        df["patterns"] = df["patterns"].apply(
            lambda x: json.loads(x) if x else [])
    return df


# ── 4H Signals ────────────────────────────────────────────────────────────────
def log_4h_signals(signals):
    init_db()
    today = _today_ist()
    with _conn() as c:
        c.execute("DELETE FROM signals_4h WHERE date=?", (today,))
        for s in signals:
            c.execute("""INSERT INTO signals_4h
                (date,symbol,action,price,sl,target1,target2,rr,rsi,vol_ratio,fno,reason,tv_link)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (today, s["symbol"], s["action"], s["price"],
                 s["sl"], s["target1"], s["target2"], s["rr"],
                 s["rsi"], s["vol_ratio"], int(s.get("fno", False)),
                 s.get("reason", ""), s.get("tv_link", "")))
        c.commit()
        _db.sync(c)

def get_4h_signals(days=1):
    init_db()
    cutoff = str(_date_ist() - timedelta(days=days))
    with _conn() as c:
        return pd.read_sql(
            "SELECT * FROM signals_4h WHERE date>=? ORDER BY date DESC, vol_ratio DESC",
            c, params=(cutoff,))


# ── Commodity Signals ─────────────────────────────────────────────────────────
def log_commodity_signals(signals):
    init_db()
    today = _today_ist()
    with _conn() as c:
        c.execute("DELETE FROM commodity_signals WHERE date=?", (today,))
        for s in signals:
            c.execute("""INSERT INTO commodity_signals
                (date,symbol,ticker,label,action,timeframe,price,sl,target1,target2,target3,rr,rsi,adx,atr)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (today, s["symbol"], s.get("ticker",""), s.get("label",""),
                 s["action"], s.get("timeframe","Daily"),
                 s["price"], s["sl"], s["target1"], s["target2"], s.get("target3", s["target2"]),
                 s["rr"], s.get("rsi",0), s.get("adx",0), s.get("atr",0)))
        c.commit()
        _db.sync(c)

def get_commodity_signals(days=1):
    init_db()
    cutoff = str(_date_ist() - timedelta(days=days))
    with _conn() as c:
        return pd.read_sql(
            "SELECT * FROM commodity_signals WHERE date>=? ORDER BY date DESC, adx DESC",
            c, params=(cutoff,))


# ── Last scan metadata ────────────────────────────────────────────────────────
def log_scan_meta(slot, counts: dict):
    """Record when each scan ran and how many signals were found."""
    init_db()
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS scan_meta (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            ts    TEXT,
            slot  TEXT,
            data  TEXT
        )""")
        c.execute("INSERT INTO scan_meta (ts,slot,data) VALUES (?,?,?)",
                  (_now_ist(), slot, json.dumps(counts)))
        c.commit()
        _db.sync(c)

def _json_safe(obj):
    """Recursively replace NaN/Inf with None so the output is spec-valid JSON.

    Python's json.dump emits bare NaN/Infinity by default, which every
    JS JSON.parse() rejects. Anything written for the web must pass through
    this first, then be dumped with allow_nan=False so a regression fails
    loudly in CI instead of silently shipping a broken API response.
    """
    if isinstance(obj, float):          # np.float64 subclasses float
        return None if (obj != obj or obj in (_INF, -_INF)) else obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


_INF = float("inf")


def export_signals_json():
    """Export all signal tables to data/*.json for GitHub raw URL access."""
    import os
    os.makedirs("data", exist_ok=True)
    init_db()

    def _df_to_json(df, path):
        if df is None or df.empty:
            with open(path, "w") as f:
                json.dump([], f)
            return
        # NaN → None. df.where(pd.notnull(df), None) is NOT enough: pandas
        # coerces None back to NaN in float columns, so bare NaN reached the
        # file. NaN is invalid JSON — JS JSON.parse() rejects it and
        # terminal.askakshay.com/api/signals returned {"source":"error"}.
        records = _json_safe(df.to_dict(orient="records"))
        with open(path, "w") as f:
            json.dump(records, f, default=str, allow_nan=False)

    # Export each table — no LIMIT, full permanent history
    with _conn() as c:
        sigs = pd.read_sql(
            "SELECT * FROM signals ORDER BY date DESC, score DESC", c)
        _df_to_json(sigs, "data/signals.json")

        bos = pd.read_sql(
            "SELECT * FROM breakouts ORDER BY date DESC", c)
        _df_to_json(bos, "data/breakouts.json")

        s4h = pd.read_sql(
            "SELECT * FROM signals_4h ORDER BY date DESC", c)
        _df_to_json(s4h, "data/signals_4h.json")

        comm = pd.read_sql(
            "SELECT * FROM commodity_signals ORDER BY date DESC", c)
        _df_to_json(comm, "data/commodity_signals.json")

        try:
            mbs = pd.read_sql(
                "SELECT * FROM multibaggers ORDER BY date DESC", c)
            _df_to_json(mbs, "data/multibaggers.json")
        except Exception:
            with open("data/multibaggers.json", "w") as f:
                json.dump([], f)

        # Unified all_signals — complete history, all signal types
        try:
            all_s = pd.read_sql(
                "SELECT * FROM all_signals ORDER BY date DESC", c)
            _df_to_json(all_s, "data/all_signals.json")
        except Exception:
            with open("data/all_signals.json", "w") as f:
                json.dump([], f)

    # Scan meta
    ts, slot, counts = get_last_scan()
    with open("data/scan_meta.json", "w") as f:
        json.dump(_json_safe({"ts": ts, "slot": slot, "counts": counts}), f,
                  default=str, allow_nan=False)

    log.info("data/*.json exported successfully")


# ── Multibagger Signals (Weekly — Saturday) ───────────────────────────────────

# ── Signal history ───────────────────────────────────────────────────────────
# Material fields. A change to any of these changes what the engine SAID about
# a position, so the previous statement is archived before it is overwritten.
#
# Deliberately excludes sent_at, send_error, alert_flags and duplicate_note:
# those record what happened to the MESSAGE, not what the engine claimed about
# the trade, and versioning them would bury the real changes in noise.
VERSIONED_COLUMNS = ("entry", "sl", "target1", "target2", "target3", "rr",
                     "status", "exit_price", "pnl_pct", "r_multiple", "grade",
                     "score", "lifecycle_status")


def _ensure_signal_versions(c) -> None:
    """Append-only history of every material change to a signal.

    WHY A TRIGGER, NOT A FUNCTION CALL

    Sixteen separate places update all_signals, across twelve files — regrade,
    the phantom-exit fix, the horizon fix, dedupe, reconcile, the scanner, the
    bot, four backfills. Requiring each to remember to snapshot first means the
    history is only as good as the least careful writer, and a new writer added
    next month starts out silently broken.

    A BEFORE UPDATE trigger cannot be forgotten and cannot be bypassed. Every
    writer, including ones that do not exist yet, is covered the moment it
    executes an UPDATE.

    WHAT IT GUARANTEES

    The ledger already publishes corrections openly — expectancy went +0.090R
    to -0.182R on a re-grade and the site said so. What it could not do was
    show what a signal looked like BEFORE the correction, because the row was
    overwritten in place. This makes that recoverable: for any signal, the
    exact levels and status the engine published at the time, and every
    revision since, with the reason implicit in what changed.

    The WHEN clause keeps it honest AND cheap — a re-run that writes identical
    values produces no version, so the history contains changes, not activity.
    """
    c.execute("""CREATE TABLE IF NOT EXISTS signal_versions (
        version_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id   INTEGER NOT NULL,
        captured_at TEXT    NOT NULL,
        entry REAL, sl REAL, target1 REAL, target2 REAL, target3 REAL, rr REAL,
        status TEXT, exit_price REAL, pnl_pct REAL, r_multiple REAL,
        grade TEXT, score REAL, lifecycle_status TEXT,
        engine_version TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sigver_signal "
              "ON signal_versions(signal_id, version_id)")

    changed = " OR ".join(
        f"IFNULL(OLD.{col},'~') IS NOT IFNULL(NEW.{col},'~')" for col in VERSIONED_COLUMNS)
    c.execute(f"""
        CREATE TRIGGER IF NOT EXISTS all_signals_version
        BEFORE UPDATE ON all_signals
        WHEN {changed}
        BEGIN
            INSERT INTO signal_versions
                (signal_id, captured_at, entry, sl, target1, target2, target3, rr,
                 status, exit_price, pnl_pct, r_multiple, grade, score,
                 lifecycle_status, engine_version)
            VALUES
                (OLD.id, datetime('now'), OLD.entry, OLD.sl, OLD.target1, OLD.target2,
                 OLD.target3, OLD.rr, OLD.status, OLD.exit_price, OLD.pnl_pct,
                 OLD.r_multiple, OLD.grade, OLD.score, OLD.lifecycle_status,
                 OLD.engine_version);
        END""")


def signal_history(signal_id: int) -> list[dict]:
    """Every archived version of one signal, oldest first.

    The CURRENT state is not in here — it is the row in all_signals. This is
    what the engine said before each change.
    """
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM signal_versions WHERE signal_id=? ORDER BY version_id",
                (signal_id,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:                                   # noqa: BLE001
        log.warning(f"signal_history({signal_id}): {e}")
        return []


def _ensure_multibagger_table(c):
    c.execute("""CREATE TABLE IF NOT EXISTS multibaggers (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        date      TEXT,
        symbol    TEXT,
        price     REAL,
        high_52w  REAL,
        low_52w   REAL,
        range_pos REAL,
        wk_rsi    REAL,
        wk_adx    REAL,
        vol_ratio REAL,
        sl        REAL,
        support1  REAL,
        support2  REAL,
        target1   REAL,
        target2   REAL,
        target3   REAL,
        rr        REAL,
        score     REAL,
        pe        REAL,
        fno       INTEGER DEFAULT 0,
        reason    TEXT,
        tv_link   TEXT
    )""")

def log_multibaggers(signals):
    init_db()
    today = _today_ist()
    with _conn() as c:
        _ensure_multibagger_table(c)
        c.execute("DELETE FROM multibaggers WHERE date=?", (today,))
        for s in signals:
            c.execute("""INSERT INTO multibaggers
                (date,symbol,price,high_52w,low_52w,range_pos,wk_rsi,wk_adx,
                 vol_ratio,sl,support1,support2,target1,target2,target3,
                 rr,score,pe,fno,reason,tv_link)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (today, s["symbol"], s["price"], s["high_52w"], s["low_52w"],
                 s["range_pos"], s["wk_rsi"], s["wk_adx"], s["vol_ratio"],
                 s["sl"], s["support1"], s["support2"],
                 s["target1"], s["target2"], s["target3"],
                 s["rr"], s["score"], s.get("pe"),
                 int(s.get("fno", False)), s.get("reason",""), s.get("tv_link","")))
        c.commit()
        _db.sync(c)
    # Same ideas, into the ledger. Written AFTER the block above closes:
    # log_batch_to_all_signals opens its own connection and syncs, and nesting
    # that inside a live `with _conn()` is how you get two writers to the same
    # Turso replica in one call.
    _log_multibaggers_to_ledger(signals, today)


# The multibagger scan has always written to its own `multibaggers` table and
# nowhere else. That table is REPLACED per scan date and read by exactly one
# consumer — the ticker's 💎 segment, which takes the top five by score. So a
# name like TORNTPHARM or KARURVYSYA appeared on the site as a price and a
# target, with no entry, no stop, no scan date, no outcome, and it never
# reached the Signal Log. Nothing ever graded them; the previous week's ideas
# were deleted rather than resolved.
#
# They are real signals — entry, stop, three targets, R:R and a score, same as
# any other engine — so they belong in `all_signals` with everything else. The
# per-table write above stays: the ticker query is fine and this is additive.
#
# Excluded from expectancy, for the same reason ai_longterm is: a 6–12 month
# hold off weekly bars cannot resolve on a swing horizon, and letting it into
# the R statistics would corrupt the only honest number on the site. See
# EXCLUDE_FROM_EXPECTANCY below and the NON_TRADING list in api/stats.js —
# a new non-trading engine has to be named in BOTH.
MULTIBAGGER_SIGNAL_TYPE = "multibagger"
MULTIBAGGER_TIMEFRAME = "1W"

TOP5_SIGNAL_TYPE = "top5_pick"
SIP_SIGNAL_TYPE = "sip_bucket"

# Research and allocation artefacts, NOT traded setups. They must never reach
# an expectancy number — and a new one has to be named in BOTH this tuple and
# NON_TRADING in vercel-news/api/stats.js, or the two disagree and the site
# publishes a different track record from the bot.
EXCLUDE_FROM_EXPECTANCY = (MULTIBAGGER_SIGNAL_TYPE, TOP5_SIGNAL_TYPE, SIP_SIGNAL_TYPE)

# Types that are ALLOCATIONS, not trades. They have no stop and no target,
# because nothing about them is a bet with an exit — a monthly SIP instalment
# is money going in on a schedule.
#
# They used to carry levels anyway. The ordering gate below requires a
# complete, correctly ordered set, so the SIP mirror manufactured one at
# ±0.1% of price: COALINDIA went into the ledger at entry 407.10, stop 406.69,
# T1 407.51, T2 407.91 (live until 2026-08-18). Those numbers were never
# decided by anything. They were shaped like a trade plan so a validator would
# accept them, and a reader had no way to tell them from a real one.
#
# NULL is the honest value, so these skip the gate instead of feeding it
# fiction. The gate still applies in full to everything that IS a trade.
ALLOCATION_TYPES = (SIP_SIGNAL_TYPE,)


def log_top5_picks(picks, week_key: str, date=None) -> list:
    """Mirror the weekly Top 5 trade ideas into all_signals.

    These are the paper's front-page picks and until now they lived ONLY in the
    newspaper cache — chosen every ISO week, shown to a reader as the week's
    ideas, and never recorded anywhere that could later say whether they
    worked. The scan engines were all accountable; the most prominent ideas on
    the page were not.

    Idempotent per week, like the multibagger mirror: the weekly build and any
    manual re-run both stamp the same week, and appending would double the log.
    Never raises — the picks are already cached and rendered by the time this
    runs, so a ledger failure must not cost the reader the section.
    """
    if not picks:
        return []
    today = date or _today_ist()
    try:
        with _conn() as c:
            n = c.execute(
                "SELECT COUNT(*) FROM all_signals WHERE signal_type=? AND date=?",
                (TOP5_SIGNAL_TYPE, today)).fetchone()[0]
            if n:
                c.execute("DELETE FROM all_signals WHERE signal_type=? AND date=?",
                          (TOP5_SIGNAL_TYPE, today))
                c.commit()
                _db.sync(c)
                log.info(f"top5: replaced {n} ledger row(s) already written today")

        rows = []
        for p in picks:
            entry = p.get("price") or p.get("entry")
            sl, t1 = p.get("stop_loss") or p.get("sl"), p.get("target") or p.get("t1")
            if not (entry and sl and t1):
                continue           # incomplete idea — logged as nothing, never guessed
            t2 = p.get("target2") or round(entry + (t1 - entry) * 1.6, 2)
            # ── A FLOOR ONLY LIFTS, AND THAT WAS HALF THE PROBLEM ───────────
            #
            # These levels are computed upstream and mirrored here unchecked,
            # which is how MRK reached the ledger at 0.65R. The floor fixed
            # that end. It cannot fix the other one, by design: enforce_r_floor
            # leaves anything already beyond the floor exactly as the engine
            # placed it, because a structural level the engine chose beats a
            # multiple of risk.
            #
            # That principle holds at 3R and breaks at 24R. Live on
            # 2026-09-06 this engine published COIN with a first target at
            # 10.94R and DUOL at 24.01R — a 118% move, called a FIRST target,
            # on a ledger where 1.6R is reached about 19% of the time. A target
            # nobody reaches is not a structural level, it is noise wearing the
            # label of a plan.
            #
            # So the far number is kept and RE-LABELLED. Anything past the
            # engine's own third rung cannot be its first: T3 becomes the
            # upstream target, and T1/T2 are re-derived from the engine's floor
            # so the near rungs are levels a trade can actually reach.
            from signals.indicators import enforce_r_floor, R1_MULT, R2_MULT, R3_MULT
            from signals import expectancy as _exp
            t1, t2, _t3 = enforce_r_floor(entry, sl, t1, t2, None, "BUY",
                                          engine=TOP5_SIGNAL_TYPE)
            _t3 = None          # set only when the ladder is rebuilt below
            _risk = entry - sl
            if _risk > 0:
                _floor = max(R1_MULT, float(_exp.floor_for(TOP5_SIGNAL_TYPE,
                                                           default=R1_MULT)))
                _ceiling = _floor + (R3_MULT - R1_MULT)
                if (t1 - entry) / _risk > _ceiling:
                    _t3 = t1                                   # keep the reach
                    t1 = round(entry + _floor * _risk, 2)
                    t2 = round(entry + (_floor + R2_MULT - R1_MULT) * _risk, 2)
                    log.info("top5 %s: first target was %.1fR — re-laddered to "
                             "%.1f/%.1f and the original kept as T3",
                             p["symbol"], (_t3 - entry) / _risk, _floor,
                             _floor + R2_MULT - R1_MULT)
            rows.append({
                "symbol": p["symbol"], "signal_type": TOP5_SIGNAL_TYPE,
                "action": "BUY", "timeframe": "1W",
                "entry": entry, "sl": sl, "t1": t1, "t2": t2,
                "t3": _t3 if _t3 else t2,
                "rr": (round((t1 - entry) / (entry - sl), 2)
                       if entry > sl else None),
                "score": int(round(p.get("score") or 0)),
                "remarks": f"Weekly Top 5 trade idea · {week_key}",
                "metadata": {"engine": "top5", "week": week_key,
                             "cadence": "weekly · ISO week",
                             "thesis": p.get("thesis") or p.get("why")},
            })
        return log_batch_to_all_signals(rows, date=today) if rows else []
    except Exception as e:                                   # noqa: BLE001
        log.warning(f"top5 ledger mirror failed: {e}")
        return []


def log_sip_bucket(allocations, month_key: str, date=None) -> list:
    """Mirror the monthly SIP allocation into all_signals.

    What the SIP was told to buy, and when. The bucket is rebuilt monthly and
    was previously visible only as the current month's allocation — there was
    no record of what it said in June, so no way to see how the allocation
    drifted or whether it followed the screen.

    These are allocations, not trades: no stop, no target, and they are named
    in EXCLUDE_FROM_EXPECTANCY so they can never contaminate a win rate. Entry
    is the NAV/price the allocation was decided at, which is the only honest
    reference point for it.
    """
    if not allocations:
        return []
    today = date or _today_ist()
    try:
        with _conn() as c:
            n = c.execute(
                "SELECT COUNT(*) FROM all_signals WHERE signal_type=? AND date LIKE ?",
                (SIP_SIGNAL_TYPE, f"{month_key}%")).fetchone()[0]
            if n:
                c.execute("DELETE FROM all_signals WHERE signal_type=? AND date LIKE ?",
                          (SIP_SIGNAL_TYPE, f"{month_key}%"))
                c.commit()
                _db.sync(c)
                log.info(f"sip: replaced {n} ledger row(s) already written for {month_key}")

        rows = []
        for a in allocations:
            price = a.get("nav") or a.get("price") or a.get("entry")
            if not price:
                continue
            pct = a.get("pct") or a.get("weight")
            rows.append({
                "symbol": (a.get("symbol") or a.get("name") or "")[:64],
                "signal_type": SIP_SIGNAL_TYPE,
                "action": "BUY", "timeframe": "1M",
                # An allocation has no stop or target, so it carries none.
                # This used to write ±0.1% of price to satisfy the ordering
                # gate; allocations now skip that gate (see ALLOCATION_TYPES)
                # and the columns stay NULL, which is what the page renders
                # as a blank rather than as a plan nobody made.
                "entry": price, "sl": None,
                "t1": None, "t2": None, "t3": None,
                "rr": None, "score": 0,
                "remarks": (f"Monthly SIP allocation · {month_key}"
                            + (f" · {pct}% of bucket" if pct else "")),
                "metadata": {"engine": "sip", "month": month_key,
                             "cadence": "monthly", "allocation_pct": pct,
                             "bucket": a.get("bucket") or a.get("category"),
                             # Which column the price came from. "decided at"
                             # and "worth now" are different claims and the log
                             # must not present one as the other.
                             "price_basis": a.get("price_basis") or "ref_price"},
            })
        return log_batch_to_all_signals(rows, date=today) if rows else []
    except Exception as e:                                   # noqa: BLE001
        log.warning(f"sip ledger mirror failed: {e}")
        return []


def _log_multibaggers_to_ledger(signals, today):
    """Mirror one multibagger scan into all_signals. Never raises.

    Best-effort on purpose: the scan's own table is already committed by the
    time this runs, so a ledger failure must not lose the scan or fail the
    Saturday job. It logs loudly instead.
    """
    if not signals:
        return []
    try:
        # Replace, don't append. The Saturday cron and a manual re-run both
        # stamp the same date, and a second run would otherwise double every
        # idea in the log — the same trap ai_longterm hit.
        with _conn() as c:
            n = c.execute(
                "SELECT COUNT(*) FROM all_signals WHERE signal_type=? AND date=?",
                (MULTIBAGGER_SIGNAL_TYPE, today)).fetchone()[0]
            if n:
                c.execute("DELETE FROM all_signals WHERE signal_type=? AND date=?",
                          (MULTIBAGGER_SIGNAL_TYPE, today))
                c.commit()
                _db.sync(c)
                log.info(f"multibagger: replaced {n} ledger row(s) already written today")

        rows = [{
            "symbol": s["symbol"], "signal_type": MULTIBAGGER_SIGNAL_TYPE,
            "action": "BUY", "timeframe": MULTIBAGGER_TIMEFRAME,
            "entry": s["price"], "sl": s["sl"],
            "t1": s["target1"], "t2": s["target2"], "t3": s["target3"],
            "rr": s["rr"], "score": int(round(s.get("score") or 0)),
            "grade": _multibagger_grade(s.get("score") or 0),
            "metadata": {
                "engine": "multibagger", "horizon": "6-12 months",
                "cadence": "weekly · Saturday 09:30 IST",
                # Every number the scan actually gated on, so the Signal Log
                # can answer "why is this here?" without re-running anything.
                "range_pos": s.get("range_pos"), "wk_rsi": s.get("wk_rsi"),
                "wk_adx": s.get("wk_adx"), "vol_ratio": s.get("vol_ratio"),
                "high_52w": s.get("high_52w"), "low_52w": s.get("low_52w"),
                "support1": s.get("support1"), "support2": s.get("support2"),
                "pe": s.get("pe"), "fno": bool(s.get("fno", False)),
                "reason": s.get("reason", ""), "tv_link": s.get("tv_link", ""),
            },
        } for s in signals]
        # `today` here is the SCAN date, which for a backfill is not today.
        ids = log_batch_to_all_signals(rows, date=today)
        log.info(f"multibagger: wrote {len(ids)} row(s) to the ledger "
                 f"({', '.join(r['symbol'] for r in rows[:8])}"
                 f"{'…' if len(rows) > 8 else ''})")
        return ids
    except Exception as e:
        log.error(f"multibagger: scan saved but NOT logged to the ledger — {e}")
        return []


# ── magic / magicmagic ───────────────────────────────────────────────────────
MAGIC_TIMEFRAME = "1W"
# Same shape as the multibagger writer below, and for the same reasons. The one
# real difference: these rows used to be written as action=WATCH with sl, t1, t2
# and t3 all NULL, which meant 24 of them sat OPEN forever with nothing that
# could ever resolve. They now carry levels from scanner.magic_levels, so they
# are gradeable signals rather than a shortlist.
#
# Still excluded from headline expectancy. The reason has CHANGED, though, and
# the distinction matters: it used to be "no levels exist", it is now "a new
# engine with no measured sample". Flip it by removing them from
# api/stats.js NON_TRADING and signal_report once there are enough closed trades
# to mean something — not before, because a handful of closes would set the
# published win rate on noise.
MAGIC_HORIZONS = {
    # The Investtech read this screen is built on is a 1-4 week short, 1-3 month
    # swing and 3-12 month long view. The position is managed on the swing/long
    # thesis (recovery to the 52-week high), so the horizon is the long one.
    "magic": "3-12 months",
    "magicmagic": "3-12 months",
}


def _log_magic_to_ledger(signals, engine, today):
    """Mirror one magic/magicmagic scan into all_signals. Never raises.

    Best-effort like the multibagger writer: the scan's own output is already
    delivered by the time this runs, so a ledger failure must not fail the
    Saturday job.
    """
    if not signals:
        return []
    if engine not in MAGIC_HORIZONS:
        log.error(f"_log_magic_to_ledger: unknown engine {engine!r}")
        return []
    try:
        # Replace, don't append — the Saturday cron and a manual re-run both
        # stamp the same date. The same trap multibagger and ai_longterm hit.
        with _conn() as c:
            n = c.execute(
                "SELECT COUNT(*) FROM all_signals WHERE signal_type=? AND date=?",
                (engine, today)).fetchone()[0]
            if n:
                c.execute("DELETE FROM all_signals WHERE signal_type=? AND date=?",
                          (engine, today))
                c.commit()
                _db.sync(c)
                log.info(f"{engine}: replaced {n} ledger row(s) already written today")

        # magic and magicmagic are different screens, but they overlap: a name
        # 20-40% off its 52-week high can satisfy both on the same day, and when
        # it does the two write byte-identical levels. Ten (symbol, date) pairs
        # in the live ledger are one idea recorded twice — PARADEEP 2026-08-15
        # and DIXON 2026-08-15 among them. Both rows resolve to the same R, so
        # the idea is counted twice in expectancy and once again in every
        # per-engine breakdown. Whichever screen runs second yields.
        sibling = "magicmagic" if engine == "magic" else "magic"
        already = set()
        try:
            with _conn() as c:
                already = {(r[0], round(float(r[1]), 4)) for r in c.execute(
                    "SELECT symbol, entry FROM all_signals "
                    "WHERE signal_type=? AND date=? AND entry IS NOT NULL",
                    (sibling, today)).fetchall()}
        except Exception as e:                          # noqa: BLE001
            # A failed lookup must not cost the scan. Worst case we log the
            # duplicate we already log today, which is no worse than now.
            log.warning(f"{engine}: duplicate check failed ({e}) — logging all rows")

        rows, skipped = [], 0
        for s in signals:
            # A candidate without levels is not written. scanner.magic_levels
            # returns None when the 52-week high cannot clear the stop by 1R, and
            # such a row would be another permanently-unresolvable OPEN.
            if not all(s.get(k) is not None for k in ("sl", "target1", "target2", "target3")):
                log.warning(f"{engine}: {s.get('symbol')} has no levels — not logged")
                continue
            if s.get("price") is not None and (s["symbol"], round(float(s["price"]), 4)) in already:
                log.info(f"{engine}: {s['symbol']} already logged today by "
                         f"{sibling} at the same entry — not double-counted")
                skipped += 1
                continue
            rows.append({
                "symbol": s["symbol"], "signal_type": engine,
                "action": "BUY", "timeframe": MAGIC_TIMEFRAME,
                "entry": s["price"], "sl": s["sl"],
                "t1": s["target1"], "t2": s["target2"], "t3": s["target3"],
                "rr": s.get("rr"), "score": int(round(s.get("score") or 0)),
                "grade": _multibagger_grade(s.get("score") or 0),
                "metadata": {
                    "engine": engine, "horizon": MAGIC_HORIZONS[engine],
                    "cadence": "weekly · Saturday 09:30 IST",
                    # Every gate the screen actually applied, so the Signal Log
                    # can answer "why is this here?" without re-running anything.
                    "cagr_3yr": s.get("cagr_3yr"), "weekly_rsi": s.get("weekly_rsi"),
                    "dist_52wh": s.get("dist_52wh"), "dist_52wl": s.get("dist_52wl"),
                    "high_52w": s.get("hi52"), "low_52w": s.get("lo52"),
                    "atr": s.get("atr"), "swing_low": s.get("swing_low"),
                    "rr_t1": s.get("rr_t1"),
                    # The three Investtech reads, verbatim, because they are the
                    # thesis and a reader will want the same words the alert used.
                    "short": s.get("short"), "short_note": s.get("short_note"),
                    "swing": s.get("swing"), "swing_note": s.get("swing_note"),
                    "long": s.get("long"), "long_note": s.get("long_note"),
                    "target_basis": "52-week high; T1/T2 staged on the recovery",
                },
            })
        if not rows:
            log.warning(f"{engine}: nothing had levels — nothing written")
            return []
        ids = log_batch_to_all_signals(rows, date=today)
        log.info(f"{engine}: wrote {len(ids)} row(s) to the ledger "
                 f"({', '.join(r['symbol'] for r in rows[:8])}"
                 f"{'…' if len(rows) > 8 else ''})"
                 + (f" · {skipped} skipped as {sibling} duplicates" if skipped else ""))
        return ids
    except Exception as e:
        log.error(f"{engine}: ledger write failed — {e}")
        return []


def _multibagger_grade(score: float) -> str:
    """Same bands ai_longterm uses, so one grade means one thing site-wide."""
    return "A+" if score >= 80 else "A" if score >= 70 else "B" if score >= 60 else "C"


def get_multibaggers(days=7):
    init_db()
    try:
        cutoff = str(_date_ist() - timedelta(days=days))
        with _conn() as c:
            _ensure_multibagger_table(c)
            return pd.read_sql(
                "SELECT * FROM multibaggers WHERE date>=? ORDER BY score DESC",
                c, params=(cutoff,))
    except Exception:
        return pd.DataFrame()


def get_last_scan():
    """Returns (ts_str, slot, counts_dict) of most recent scan."""
    try:
        with _conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS scan_meta (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, slot TEXT, data TEXT)""")
            row = c.execute(
                "SELECT ts, slot, data FROM scan_meta ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row:
            # ── IST WAS BEING ADDED TWICE ────────────────────────────────────
            #
            # log_scan_meta stores _now_ist(), which is
            # datetime.now(_IST).isoformat() — an ISO string that ALREADY
            # carries +05:30. This then called .replace(tzinfo=utc) on it,
            # which does not convert: it DISCARDS the +05:30 and relabels the
            # same wall clock as UTC. astimezone(IST) then added 5:30 again.
            #
            # Measured on the 2026-09-22 eod scan: the run started at 16:43
            # IST, logged "Scan started: 22 Sep 2026 04:43 PM IST" correctly,
            # and data/scan_meta.json published "22 Sep 2026 10:21 PM IST".
            # Every "last scan" stamp the dashboard has ever shown is five and
            # a half hours in the future — a freshness figure that always reads
            # NEWER than the truth, on a site whose whole discipline is that a
            # section may not look more current than its data.
            #
            # A naive row is treated as IST rather than UTC, because the only
            # writer is a function called _now_ist.
            dt = datetime.fromisoformat(row[0])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_IST)
            return (dt.astimezone(_IST).strftime("%d %b %Y %I:%M %p IST"),
                    row[1], json.loads(row[2]))
    except Exception:
        pass
    return None, None, {}


# ── Realized performance (live counterpart to backtest.py) ───────────────────

def signal_stats(days: int = 90, signal_type: str = None) -> dict:
    """Measured performance of closed signals, grouped by signal type.

    Reports the same metrics as backtest.py (win rate, expectancy in R, total
    R) so the backtested expectation and what actually happened are directly
    comparable. If live diverges hard from backtest, the engine config is
    overfit and should be re-swept.
    """
    init_db()
    since  = (datetime.now(_IST).date() - timedelta(days=days)).isoformat()
    q = ("SELECT signal_type, r_multiple FROM all_signals "
         "WHERE status NOT IN ('OPEN','CANCELLED') "
         "AND r_multiple IS NOT NULL AND date >= ?")
    params = [since]
    if signal_type:
        q += " AND signal_type = ?"
        params.append(signal_type)

    with _conn() as c:
        df = pd.read_sql(q, c, params=params)
    if df.empty:
        return {}

    def _agg(r):
        r = r.astype(float)
        return {"n": int(len(r)), "win_rate": round(float((r > 0).mean()) * 100, 1),
                "expectancy": round(float(r.mean()), 3),
                "total_r": round(float(r.sum()), 1)}

    out = {st: _agg(g["r_multiple"]) for st, g in df.groupby("signal_type")}
    out["ALL"] = _agg(df["r_multiple"])
    return out


def format_stats(days: int = 90) -> str:
    """Telegram-ready performance block."""
    s = signal_stats(days)
    if not s:
        return f"\U0001F4CA *Signal Performance* — no closed signals in {days}d"
    lines = [f"\U0001F4CA *Signal Performance* — last {days}d", ""]
    for k in sorted(s, key=lambda x: (x == "ALL", x)):
        v = s[k]
        lines.append(f"`{k:10}` n=`{v['n']}` · win `{v['win_rate']}%` · "
                     f"exp `{v['expectancy']:+.2f}R` · tot `{v['total_r']:+.1f}R`")
    return "\n".join(lines)

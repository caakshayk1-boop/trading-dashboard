import sqlite3, os, json, logging, math
import pandas as pd
import yfinance as yf
from datetime import date, datetime, timedelta, timezone
import db as _db
from symbols import to_yahoo

# Signals are dated in IST; runners execute in UTC. Compare against IST or a
# signal filed this evening IST looks like it belongs to "tomorrow".
_IST = timezone(timedelta(hours=5, minutes=30))


def _max_hold_hours(timeframe: str) -> int:
    """
    Holding limit for a timeframe. Imported from standalone_scan so the grader
    and the live scanner cannot drift apart — a second copy of this table is how
    the ledger ends up measuring a horizon the engine never traded.
    """
    try:
        from standalone_scan import _max_hold_hours as _impl
        return _impl(timeframe)
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
        ]
        for col, sql in as_migrations:
            if col not in as_existing:
                c.execute(sql)
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
    """
    init_db()
    cutoff_5d = str(_date_ist() - timedelta(days=5))
    cutoff_7d = str(_date_ist() - timedelta(days=7))
    sym_clean = symbol.replace(".NS", "")
    with _conn() as c:
        # Check unified all_signals first (covers all signal types)
        row = c.execute(
            "SELECT id FROM all_signals WHERE symbol=? AND signal_type=? "
            "AND status='OPEN' AND date>=?",
            (sym_clean, signal_type, cutoff_5d)
        ).fetchone()
        if row:
            log.debug(f"Duplicate skip: {sym_clean} ({signal_type}) already OPEN")
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


def duplicate_symbols(symbols, signal_type="swing"):
    """Batch form of is_duplicate — returns the set of symbols to skip.

    Same three rules, resolved in one connection instead of one per symbol.
    Scanning 53 breakouts through is_duplicate() opened 106 connections; under
    Turso each of those does a replica sync.
    """
    clean = {str(s).replace(".NS", "") for s in symbols if s}
    if not clean:
        return set()
    init_db()
    cutoff_5d = str(_date_ist() - timedelta(days=5))
    cutoff_7d = str(_date_ist() - timedelta(days=7))
    ordered = sorted(clean)
    ph = ",".join("?" for _ in ordered)
    dupes = set()
    with _conn() as c:
        for sql, params in (
            (f"SELECT DISTINCT symbol FROM all_signals WHERE symbol IN ({ph}) "
             f"AND signal_type=? AND status='OPEN' AND date>=?",
             tuple(ordered) + (signal_type, cutoff_5d)),
            (f"SELECT DISTINCT symbol FROM all_signals WHERE symbol IN ({ph}) "
             f"AND signal_type=? AND status='SL_HIT' AND date>=?",
             tuple(ordered) + (signal_type, cutoff_7d)),
            # Legacy: the old signals table is not signal_type aware.
            (f"SELECT DISTINCT symbol FROM signals WHERE symbol IN ({ph}) "
             f"AND status='OPEN' AND date>=?",
             tuple(ordered) + (cutoff_5d,)),
        ):
            dupes.update(r[0] for r in c.execute(sql, params).fetchall())
    if dupes:
        log.info(f"Duplicate skip ({signal_type}): {len(dupes)} symbol(s) — "
                 f"{', '.join(sorted(dupes)[:10])}{'…' if len(dupes) > 10 else ''}")
    return dupes


def log_to_all_signals(symbol, signal_type, action, entry, sl, t1, t2, t3, rr,
                        timeframe="SWING", score=0, metadata=None):
    """Unified signal logger — returns the new row id.

    The row is written with sent_at NULL. Delivery is recorded separately by
    mark_alerts_sent() once Telegram has actually accepted the message. The
    ledger must record every signal the scanner produced whether or not
    Telegram was reachable, so this deliberately does not depend on the send.
    """
    init_db()
    today = _today_ist()
    with _conn() as c:
        c.execute("""INSERT INTO all_signals
            (date,signal_type,symbol,action,timeframe,entry,sl,target1,target2,target3,
             rr,score,status,sent_at,metadata,engine_version)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',NULL,?,?)""",
            (today, signal_type, symbol, action, timeframe, entry, sl, t1, t2, t3,
             rr, score, json.dumps(metadata or {}), ENGINE_VERSION))
        row_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
        _db.sync(c)
    log.info(f"all_signals logged: {symbol} {signal_type} {action} entry={entry} id={row_id}")
    return row_id


def log_batch_to_all_signals(rows):
    """Insert many signals over ONE connection. Returns row ids in input order.

    log_to_all_signals() opens a connection, init_db()s and syncs per call —
    against Turso that measured ~8.7s per row in CI. Fine when the caller was
    capped at 5 signals; a 50-signal scan would add minutes to every run.

    `rows` are dicts with the same keys as log_to_all_signals' arguments.
    """
    if not rows:
        return []
    init_db()
    today = _today_ist()
    ids = []
    with _conn() as c:
        for r in rows:
            c.execute("""INSERT INTO all_signals
                (date,signal_type,symbol,action,timeframe,entry,sl,target1,target2,target3,
                 rr,score,status,sent_at,metadata,engine_version,grade,breakeven_wr,turnover_cr)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',NULL,?,?,?,?,?)""",
                (today, r["signal_type"], r["symbol"], r.get("action", "BUY"),
                 r.get("timeframe", "SWING"), r["entry"], r["sl"],
                 r["t1"], r["t2"], r["t3"], r["rr"], r.get("score", 0),
                 json.dumps(r.get("metadata") or {}), ENGINE_VERSION,
                 r.get("grade"), r.get("breakeven_wr"), r.get("turnover_cr")))
            ids.append(c.execute("SELECT last_insert_rowid()").fetchone()[0])
        c.commit()
        _db.sync(c)
    log.info(f"all_signals batch logged: {len(ids)} row(s) "
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
            hold_h = _max_hold_hours(str(row.get("timeframe", "")))
            end_d = min(today, start_d + timedelta(days=int(hold_h / 24) + 4))

            df = yf.download(sym, start=start_d.isoformat(),
                             end=(end_d + timedelta(days=1)).isoformat(),
                             interval="1d", progress=False, auto_adjust=True)
            if df is None or df.empty:
                continue

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

            max_sessions = max(1, int(hold_h / 24))
            triggered_at = row.get("entry_triggered_at")
            status, exit_p, ambiguous = None, None, 0
            sessions = 0
            last_close = None

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
                        c.execute(
                            "UPDATE all_signals SET entry_triggered_at=?, "
                            "lifecycle_status='Triggered' WHERE id=?",
                            (triggered_at, int(row["id"]))
                        )
                        c.commit()
                        _db.sync(c)
                    log.info(f"Entry triggered: {row['symbol']} @ {entry} on {triggered_at}")
                    # Fall through — the fill bar can also resolve the trade.

                sessions += 1

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
                elif sessions >= max_sessions:
                    # Time stop. Capital held in a setup that has not worked is
                    # capital unavailable for one that will.
                    status, exit_p = "TIME_STOP", last_close

                if status:
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
                    "r_multiple=?,exit_ambiguous=?,closed_at=? WHERE id=?",
                    (status, exit_p, pnl, r_m, ambiguous,
                     datetime.now(_IST).isoformat(), int(row["id"]))
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
            import pytz
            from datetime import timezone
            ist = pytz.timezone("Asia/Kolkata")
            utc_dt = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
            ist_dt = utc_dt.astimezone(ist)
            return ist_dt.strftime("%d %b %Y %I:%M %p IST"), row[1], json.loads(row[2])
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

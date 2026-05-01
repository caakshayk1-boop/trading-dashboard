import sqlite3, os, json
import pandas as pd
import yfinance as yf
from datetime import date, datetime, timedelta

DB = "signals.db"

def _conn():
    return sqlite3.connect(DB)

def init_db():
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
        _ensure_multibagger_table(c)
        c.commit()

def log_signals(signals):
    init_db()
    today = str(date.today())
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


def get_signals_display(days=3, min_score=0):
    """Return signals as list of dicts ready for card display, parsed from DB."""
    init_db()
    cutoff = str(date.today() - timedelta(days=days))
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

def is_duplicate(symbol):
    """PDF 5f: no signal if active < 3 days or SL hit in last 5 days."""
    init_db()
    cutoff_3d = str(date.today() - timedelta(days=3))
    cutoff_5d = str(date.today() - timedelta(days=5))
    with _conn() as c:
        # Active signal < 3 days old
        row = c.execute(
            "SELECT id FROM signals WHERE symbol=? AND status='OPEN' AND date>=?",
            (symbol, cutoff_3d)
        ).fetchone()
        if row:
            return True
        # SL hit in last 5 days
        row = c.execute(
            "SELECT id FROM signals WHERE symbol=? AND status='SL_HIT' AND date>=?",
            (symbol, cutoff_5d)
        ).fetchone()
        if row:
            return True
    return False

def is_muted(symbol):
    init_db()
    with _conn() as c:
        return bool(c.execute("SELECT 1 FROM muted_assets WHERE symbol=?", (symbol,)).fetchone())

def mute_asset(symbol):
    init_db()
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO muted_assets VALUES (?,?)",
                  (symbol, str(datetime.utcnow())))
        c.commit()

def unmute_asset(symbol):
    init_db()
    with _conn() as c:
        c.execute("DELETE FROM muted_assets WHERE symbol=?", (symbol,))
        c.commit()

def update_outcomes():
    init_db()
    with _conn() as c:
        open_trades = pd.read_sql("SELECT * FROM signals WHERE status='OPEN'", c)

    for _, row in open_trades.iterrows():
        try:
            sym = row["symbol"] + ".NS"
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
        except Exception:
            continue

def get_performance():
    init_db()
    with _conn() as c:
        df = pd.read_sql("SELECT * FROM signals", c)
    if df.empty:
        return {}
    closed = df[df["status"] != "OPEN"]
    wins   = closed[closed["pnl_pct"] > 0]
    losses = closed[closed["pnl_pct"] <= 0]
    gross_profit = wins["pnl_pct"].sum() if len(wins) > 0 else 0
    gross_loss   = abs(losses["pnl_pct"].sum()) if len(losses) > 0 else 1
    return {
        "total":         len(df),
        "closed":        len(closed),
        "open":          len(df[df["status"] == "OPEN"]),
        "win_rate":      round(len(wins) / len(closed) * 100, 1) if len(closed) > 0 else 0,
        "avg_pnl":       round(closed["pnl_pct"].mean(), 2) if len(closed) > 0 else 0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0,
        "avg_r":         round(closed["r_multiple"].mean(), 2) if len(closed) > 0 else 0,
        "best":          round(closed["pnl_pct"].max(), 2) if len(closed) > 0 else 0,
        "worst":         round(closed["pnl_pct"].min(), 2) if len(closed) > 0 else 0,
        "by_setup":      closed.groupby("setup_type")["pnl_pct"].mean().round(2).to_dict() if len(closed) > 0 else {},
    }

def get_active_signals():
    init_db()
    with _conn() as c:
        return pd.read_sql("SELECT * FROM signals WHERE status='OPEN' ORDER BY date DESC", c)

def get_history():
    init_db()
    with _conn() as c:
        return pd.read_sql("SELECT * FROM signals ORDER BY date DESC LIMIT 200", c)


# ── Breakouts ─────────────────────────────────────────────────────────────────
def log_breakouts(breakouts):
    init_db()
    today = str(date.today())
    with _conn() as c:
        # Clear today's breakouts (re-scan replaces)
        c.execute("DELETE FROM breakouts WHERE date=?", (today,))
        for b in breakouts:
            c.execute("""INSERT INTO breakouts
                (date,symbol,timeframe,pattern,patterns,price,sl,target1,target2,target3,rr,vol_ratio,fno,tv_link)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (today, b["symbol"], b["timeframe"], b["pattern"],
                 json.dumps(b.get("patterns", [])),
                 b["price"], b["sl"], b["target1"], b["target2"], b["target3"],
                 b["rr"], b["vol_ratio"], int(b.get("fno", False)), b.get("tv_link", "")))
        c.commit()

def get_breakouts(days=3):
    init_db()
    cutoff = str(date.today() - timedelta(days=days))
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
    today = str(date.today())
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

def get_4h_signals(days=1):
    init_db()
    cutoff = str(date.today() - timedelta(days=days))
    with _conn() as c:
        return pd.read_sql(
            "SELECT * FROM signals_4h WHERE date>=? ORDER BY date DESC, vol_ratio DESC",
            c, params=(cutoff,))


# ── Commodity Signals ─────────────────────────────────────────────────────────
def log_commodity_signals(signals):
    init_db()
    today = str(date.today())
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

def get_commodity_signals(days=1):
    init_db()
    cutoff = str(date.today() - timedelta(days=days))
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
                  (str(datetime.utcnow()), slot, json.dumps(counts)))
        c.commit()

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
        # Convert to records, handle NaN
        records = df.where(pd.notnull(df), None).to_dict(orient="records")
        with open(path, "w") as f:
            json.dump(records, f, default=str)

    # Export each table
    with _conn() as c:
        sigs = pd.read_sql(
            "SELECT * FROM signals WHERE status='OPEN' ORDER BY score DESC LIMIT 50", c)
        _df_to_json(sigs, "data/signals.json")

        bos = pd.read_sql(
            "SELECT * FROM breakouts ORDER BY date DESC LIMIT 50", c)
        _df_to_json(bos, "data/breakouts.json")

        s4h = pd.read_sql(
            "SELECT * FROM signals_4h ORDER BY date DESC LIMIT 50", c)
        _df_to_json(s4h, "data/signals_4h.json")

        comm = pd.read_sql(
            "SELECT * FROM commodity_signals ORDER BY date DESC LIMIT 30", c)
        _df_to_json(comm, "data/commodity_signals.json")

        try:
            mbs = pd.read_sql(
                "SELECT * FROM multibaggers ORDER BY date DESC LIMIT 30", c)
            _df_to_json(mbs, "data/multibaggers.json")
        except Exception:
            with open("data/multibaggers.json", "w") as f:
                json.dump([], f)

    # Scan meta
    ts, slot, counts = get_last_scan()
    with open("data/scan_meta.json", "w") as f:
        json.dump({"ts": ts, "slot": slot, "counts": counts}, f)

    import logging
    logging.info("data/*.json exported successfully")


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
    today = str(date.today())
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

def get_multibaggers(days=7):
    init_db()
    try:
        cutoff = str(date.today() - timedelta(days=days))
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

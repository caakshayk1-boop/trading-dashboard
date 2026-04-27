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
        c.commit()

def log_signals(signals):
    init_db()
    today = str(date.today())
    with _conn() as c:
        for s in signals:
            meta = json.dumps({
                "rsi": s.get("rsi"), "adx": s.get("adx"),
                "vol_ratio": s.get("vol_ratio"), "regime": s.get("regime"),
                "reasons": s.get("reasons"),
            })
            c.execute("""INSERT INTO signals
                (date,symbol,setup_type,action,entry,sl1,sl2,target1,target2,target3,score,metadata)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (today, s["symbol"], s.get("setup_type",""), s.get("action","BUY"),
                 s["price"], s.get("sl1", s["price"]*0.96), s.get("sl2", s["price"]*0.96),
                 s["target1"], s["target2"], s["target3"], s["score"], meta))
        c.commit()

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
            if lo <= row["sl2"]:
                status, exit_p = "SL_HIT", row["sl2"]
            elif hi >= row["target2"]:
                status, exit_p = "T2_HIT", row["target2"]
            elif hi >= row["target1"]:
                status, exit_p = "T1_HIT", row["target1"]

            if status != "OPEN":
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

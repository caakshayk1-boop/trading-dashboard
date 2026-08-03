"""Regression guard for the alert pipeline. Run: python test_alert_pipeline.py

Covers the failure that made the site show signals Telegram never sent
(2026-07-31: 53 breakouts scanned, 5 logged, all stamped as delivered):

  * sent_at is written only after Telegram accepts — never at INSERT time
  * a failed or partly failed send leaves rows unsent, with the reason
  * no scan path truncates its signal list positionally
  * message chunking respects Telegram's 4096-char limit, dropping nothing
  * _post retries 429/5xx/transport errors and gives up honestly
  * dedup and inserts are O(1) DB connections, not O(n)
  * a later scan does not wipe an earlier scan's breakout history

Network is stubbed and the DB is a throwaway temp file — this sends nothing
and touches no real data.
"""
import os, sys, shutil, tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.environ.pop("TURSO_URL", None)
os.environ.pop("TURSO_TOKEN", None)
os.environ["TELEGRAM_TOKEN"] = "test-token"
os.environ["TELEGRAM_CHAT_ID"] = "test-chat"

TMP = tempfile.mkdtemp()
import db
db.LOCAL_DB = os.path.join(TMP, "test_signals.db")
db.TURSO_URL = ""
db.TURSO_TOKEN = ""

import tracker, standalone_scan, telegram_bot

fails = []
def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        fails.append(name)

# Counts DB connections opened while `fn` runs. Under Turso every connect()
# does a full replica sync, so O(n) connections is the dominant scan cost.
_real_conn = tracker._conn
def _count_conns(fn):
    calls = []
    def counting(*a, **k):
        calls.append(1)
        return _real_conn(*a, **k)
    tracker._conn = counting
    try:
        fn()
    finally:
        tracker._conn = _real_conn
    return len(calls)

# ── 1. migration + truthful sent_at ──────────────────────────────────────────
tracker.init_db()
with tracker._conn() as c:
    cols = [r[1] for r in c.execute("PRAGMA table_info(all_signals)").fetchall()]
check("migration adds send_error", "send_error" in cols)

rid = tracker.log_to_all_signals("TESTSYM", "breakout", "BUY", 100, 95, 105, 110, 115, 1.5)
check("log returns a row id", isinstance(rid, int) and rid > 0, f"id={rid}")

with tracker._conn() as c:
    sent, err = c.execute("SELECT sent_at, send_error FROM all_signals WHERE id=?", (rid,)).fetchone()
check("insert leaves sent_at NULL", sent is None, f"sent_at={sent!r}")

# ── 2. mark_alerts_sent both ways ────────────────────────────────────────────
tracker.mark_alerts_sent([rid], False, "boom 429")
with tracker._conn() as c:
    sent, err = c.execute("SELECT sent_at, send_error FROM all_signals WHERE id=?", (rid,)).fetchone()
check("failed send: sent_at stays NULL", sent is None, f"sent_at={sent!r}")
check("failed send: reason stored", err == "boom 429", f"send_error={err!r}")

tracker.mark_alerts_sent([rid], True)
with tracker._conn() as c:
    sent, err = c.execute("SELECT sent_at, send_error FROM all_signals WHERE id=?", (rid,)).fetchone()
check("ok send: sent_at stamped", sent is not None, f"sent_at={sent!r}")
check("ok send: error cleared", err is None)

# ── 3. _send propagates failure ──────────────────────────────────────────────
posted = []
def fake_post_ok(msg, chat_id=None):
    posted.append(msg); return True
def fake_post_fail(msg, chat_id=None):
    posted.append(msg); return False

telegram_bot._post = fake_post_ok
check("_send True on success", standalone_scan._send("hi") is True)
telegram_bot._post = fake_post_fail
check("_send False on rejection", standalone_scan._send("hi") is False)
check("_send records reason", standalone_scan._LAST_SEND_ERROR is not None,
      standalone_scan._LAST_SEND_ERROR)
def fake_post_raise(msg, chat_id=None):
    raise RuntimeError("network down")
telegram_bot._post = fake_post_raise
check("_send False on exception", standalone_scan._send("hi") is False)

# ── 4. chunking ──────────────────────────────────────────────────────────────
telegram_bot._post = fake_post_ok
posted.clear()
blocks = [f"block {i} " + "x" * 200 for i in range(60)]
ok = standalone_scan._send_chunked("HEAD\n", blocks, footer="FOOT")
check("_send_chunked reports every block delivered",
      isinstance(ok, list) and len(ok) == 60 and all(ok), f"{sum(ok)}/60")
check("_send_chunked split into >1 msg", len(posted) > 1, f"{len(posted)} messages")
check("every chunk under Telegram limit", all(len(m) <= 4096 for m in posted),
      f"max={max(len(m) for m in posted)}")
joined = "".join(posted)
check("no block dropped", all(f"block {i} " in joined for i in range(60)))
check("footer only on last", posted[-1].endswith("FOOT") and not posted[0].endswith("FOOT"))

posted.clear()
telegram_bot._post = fake_post_fail
check("_send_chunked reports all-failed",
      standalone_scan._send_chunked("H\n", ["a", "b"]) == [False, False])

# Partial failure: only the rejected chunk's signals must be recorded unsent.
# One block per chunk (each block alone exceeds the budget), 2nd send fails.
posted.clear()
seq = [True, False, True]
calls = {"n": 0}
def fake_post_seq(msg, chat_id=None):
    posted.append(msg)
    r = seq[calls["n"] % len(seq)]
    calls["n"] += 1
    return r
telegram_bot._post = fake_post_seq
big = ["A" * 3000, "B" * 3000, "C" * 3000]
flags = standalone_scan._send_chunked("H\n", big)
check("partial failure isolates the failed chunk", flags == [True, False, True], str(flags))

marked = []
standalone_scan._record_delivery([101, 102, 103], flags,
                                 lambda ids, ok, err=None: marked.append((sorted(ids), ok)))
check("_record_delivery splits by real outcome",
      sorted(marked) == sorted([([101, 103], True), ([102], False)]), str(marked))

# ── 4b. init_db is memoised per database ─────────────────────────────────────
n_first = _count_conns(lambda: tracker.init_db(force=True))
n_again = _count_conns(lambda: [tracker.init_db() for _ in range(20)])
check("init_db runs once per process", n_first >= 1 and n_again == 0,
      f"forced={n_first}, 20 subsequent calls={n_again}")

# ── 4c. batch dedup resolves all symbols in one connection ───────────────────
tracker.log_batch_to_all_signals([
    {"symbol": "DUPSYM", "signal_type": "breakout", "action": "BUY", "entry": 100,
     "sl": 95, "t1": 105, "t2": 110, "t3": 115, "rr": 1.5, "timeframe": "Weekly"}])
syms = [f"FRESH{i}" for i in range(40)] + ["DUPSYM"]
dupes = []
n_dedup_conns = _count_conns(
    lambda: dupes.append(tracker.duplicate_symbols(syms, "breakout")))
check("batch dedup finds the open signal", dupes[0] == {"DUPSYM"}, str(dupes[0]))
check("batch dedup is O(1) connections", n_dedup_conns <= 1,
      f"{n_dedup_conns} connections for {len(syms)} symbols")
check("batch dedup agrees with is_duplicate",
      tracker.is_duplicate("DUPSYM", "breakout") is True
      and tracker.is_duplicate("FRESH0", "breakout") is False)

# ── 5. run_breakout_scan end-to-end: 53 in, 53 logged ────────────────────────
import scanner
N = 53
fake_bos = [{"symbol": f"SYM{i}", "timeframe": "Weekly" if i % 2 else "Monthly",
             "pattern": "p", "patterns": ["p"], "price": 100.0 + i, "sl": 95.0 + i,
             "target1": 105.0 + i, "target2": 110.0 + i, "target3": 115.0 + i,
             "rr": 1.5, "vol_ratio": 1.2, "fno": True, "tv_link": ""}
            for i in range(N)]
scanner.scan_breakouts = lambda *a, **k: list(fake_bos)

telegram_bot._post = fake_post_ok
posted.clear()

# The insert path must not open a connection per signal — that measured ~8.7s
# per row against Turso in CI, so 53 rows would add ~7.5 min to every scan.
batch_rows =[{"symbol": f"BATCH{i}", "signal_type": "breakout", "action": "BUY",
               "entry": 100.0, "sl": 95.0, "t1": 105.0, "t2": 110.0, "t3": 115.0,
               "rr": 1.5, "timeframe": "Weekly", "score": 0} for i in range(N)]
batch_ids = []
n_batch_conns = _count_conns(
    lambda: batch_ids.extend(tracker.log_batch_to_all_signals(batch_rows)))
n_loop_conns = _count_conns(
    lambda: [tracker.log_to_all_signals(f"LOOP{i}", "breakout", "BUY", 100, 95,
                                        105, 110, 115, 1.5) for i in range(N)])
check("batch insert returns one id per row", len(batch_ids) == N, f"ids={len(batch_ids)}")
check("batch insert is O(1) connections, not O(n)", n_batch_conns <= 2,
      f"batch={n_batch_conns} vs per-call loop={n_loop_conns} for {N} rows")
with tracker._conn() as c:
    n_b = c.execute("SELECT COUNT(*) FROM all_signals WHERE symbol LIKE 'BATCH%'").fetchone()[0]
    n_bnull = c.execute("SELECT COUNT(*) FROM all_signals WHERE symbol LIKE 'BATCH%' "
                        "AND sent_at IS NULL").fetchone()[0]
check("batch rows all persisted", n_b == N, f"n={n_b}")
check("batch rows start unsent", n_bnull == N, f"unsent={n_bnull}")

out = standalone_scan.run_breakout_scan("01 Aug 2026 09:00 AM IST")
with tracker._conn() as c:
    n_logged = c.execute(
        "SELECT COUNT(*) FROM all_signals WHERE signal_type='breakout' AND symbol LIKE 'SYM%'"
    ).fetchone()[0]
    n_sent = c.execute(
        "SELECT COUNT(*) FROM all_signals WHERE signal_type='breakout' "
        "AND symbol LIKE 'SYM%' AND sent_at IS NOT NULL"
    ).fetchone()[0]
check("all 53 breakouts logged (was 5)", n_logged == N, f"logged={n_logged}")
check("all 53 marked sent", n_sent == N, f"sent={n_sent}")
check("message count > 1", len(posted) > 1, f"{len(posted)} messages")
joined = "".join(posted)
missing = [b["symbol"] for b in fake_bos if f"*{b['symbol']}*" not in joined]
check("every symbol reached Telegram", not missing, f"missing={missing[:5]}")

# ── 6. failed send leaves the whole batch unsent ─────────────────────────────
scanner.scan_breakouts = lambda *a, **k: [
    {**b, "symbol": f"FAIL{i}"} for i, b in enumerate(fake_bos[:6])]
telegram_bot._post = fake_post_fail
standalone_scan.run_breakout_scan("01 Aug 2026 09:05 AM IST")
with tracker._conn() as c:
    rows = c.execute(
        "SELECT COUNT(*), SUM(sent_at IS NULL), MAX(send_error) FROM all_signals "
        "WHERE symbol LIKE 'FAIL%'").fetchone()
check("failed batch still logged", rows[0] == 6, f"n={rows[0]}")
check("failed batch NOT marked sent", rows[1] == 6, f"unsent={rows[1]}")
check("failed batch carries reason", bool(rows[2]), f"err={rows[2]!r}")

# ── 7. sort key: R:R leads, timeframe tiebreaks ──────────────────────────────
res = [{"symbol": "WEEK_HI", "timeframe": "Weekly",  "rr": 3.0},
       {"symbol": "MON_LO",  "timeframe": "Monthly", "rr": 1.5},
       {"symbol": "MON_HI",  "timeframe": "Monthly", "rr": 3.0}]
tf_rank = {"Monthly": 3, "Weekly": 2, "Daily": 1}
res.sort(key=lambda x: (x["rr"], tf_rank.get(x["timeframe"], 0)), reverse=True)
check("high-RR Weekly outranks low-RR Monthly",
      [r["symbol"] for r in res] == ["MON_HI", "WEEK_HI", "MON_LO"],
      str([r["symbol"] for r in res]))

# ── 8. _post retry behaviour (429 / 5xx / Markdown) ──────────────────────────
import importlib
telegram_bot = importlib.reload(telegram_bot)
telegram_bot._MIN_SEND_GAP_S = 0        # don't actually pace during tests
telegram_bot.TELEGRAM_TOKEN = "t"
telegram_bot.TELEGRAM_CHAT_ID = "c"

class FakeResp:
    def __init__(self, status, body="", js=None):
        self.status_code, self.text, self._js = status, body, js or {}
        self.ok = 200 <= status < 300
    def json(self): return self._js

slept = []
telegram_bot.time.sleep = lambda s: slept.append(s)

def stub_requests(responses):
    seen = []
    def post(url, data=None, timeout=None):
        seen.append(dict(data or {}))
        return responses[min(len(seen) - 1, len(responses) - 1)]
    telegram_bot.requests.post = post
    return seen

# 429 then success — must retry, honour retry_after, and report True
slept.clear()
seen = stub_requests([FakeResp(429, "slow down", {"parameters": {"retry_after": 3}}),
                      FakeResp(200)])
check("_post retries on 429 and succeeds", telegram_bot._post("hi") is True)
check("_post honours Retry-After", 3 in slept, f"slept={slept}")

# 429 forever — must give up and report False, not claim success
slept.clear()
stub_requests([FakeResp(429, "slow", {"parameters": {"retry_after": 1}})])
check("_post gives up after repeated 429", telegram_bot._post("hi") is False)

# 400 parse error — retries once without parse_mode
seen = stub_requests([FakeResp(400, "Bad Request: can't parse entities"), FakeResp(200)])
check("_post falls back to plain text on parse error", telegram_bot._post("*bad") is True)
check("_post dropped parse_mode on the retry",
      len(seen) == 2 and "parse_mode" in seen[0] and "parse_mode" not in seen[1])

# 5xx — transient, retried
stub_requests([FakeResp(503, "upstream"), FakeResp(200)])
check("_post retries 5xx", telegram_bot._post("hi") is True)

# 403 — permanent, no retry
seen = stub_requests([FakeResp(403, "bot was blocked")])
check("_post does not retry a permanent 4xx",
      telegram_bot._post("hi") is False and len(seen) == 1, f"attempts={len(seen)}")

# transport exception then success
class Boom(telegram_bot.requests.RequestException): pass
calls = {"n": 0}
def flaky_post(url, data=None, timeout=None):
    calls["n"] += 1
    if calls["n"] == 1:
        raise Boom("connection reset")
    return FakeResp(200)
telegram_bot.requests.post = flaky_post
check("_post retries transport errors", telegram_bot._post("hi") is True)

# ── 9. the other converted scan paths are uncapped and marked accurately ─────
importlib.reload(telegram_bot)
telegram_bot._post = fake_post_ok
standalone_scan.telegram_bot = telegram_bot
import types

def run_path(name, fn, sig_type, n, make):
    posted.clear()
    fn()
    with tracker._conn() as c:
        logged, sent = c.execute(
            "SELECT COUNT(*), SUM(sent_at IS NOT NULL) FROM all_signals "
            "WHERE signal_type=?", (sig_type,)).fetchone()
    check(f"{name}: all {n} logged (no positional cap)", logged == n, f"logged={logged}")
    check(f"{name}: all {n} marked sent", sent == n, f"sent={sent}")

M = 40
scanner.scan_4h = lambda *a, **k: [
    {"symbol": f"H4_{i}", "price": 100.0+i, "sl": 95.0+i, "target1": 105.0+i,
     "target2": 110.0+i, "rr": 1.5, "score": 70, "fno": True} for i in range(M)]
tracker.log_4h_signals = lambda *a, **k: None
run_path("4h_scan", lambda: standalone_scan.run_4h_scan("t"), "4h", M, None)

scanner.scan_tlm_breakouts = lambda interval="4h", *a, **k: [
    {"symbol": f"AI_{i}", "price": 100.0+i, "sl": 95.0+i, "target1": 105.0+i,
     "target2": 110.0+i, "target3": 115.0+i, "rr": 2.0, "pattern": "chan",
     "vol_ratio": 1.1, "fno": False, "timeframe": "4H"} for i in range(M)]
run_path("tlm_scan", lambda: standalone_scan.run_tlm_scan("t", interval="4h"),
         "ai_4h", M, None)

scanner.scan_commodities = lambda *a, **k: [
    {"symbol": f"CM_{i}", "ticker": f"C{i}=F", "action": "BUY", "price": 100.0+i,
     "sl": 95.0+i, "target1": 105.0+i, "target2": 110.0+i, "target3": 115.0+i,
     "rr": 1.8, "timeframe": "Daily"} for i in range(M)]
tracker.log_commodity_signals = lambda *a, **k: None
run_path("commodity_scan", lambda: standalone_scan.run_commodity_scan("t"),
         "commodity", M, None)

scanner.scan_intraday_momentum = lambda *a, **k: [
    {"symbol": f"ID_{i}", "price": 100.0+i, "sl": 95.0+i, "target1": 105.0+i,
     "target2": 110.0+i, "rr": 1.4, "vol_ratio": 2.0, "rsi": 60, "score": 55}
    for i in range(M)]
run_path("intraday_scan", lambda: standalone_scan.run_intraday_scan("t"),
         "intraday", M, None)

# Second run of the same scan must dedup everything, not re-alert
before = posted[:]
posted.clear()
out2 = standalone_scan.run_4h_scan("t2")
check("re-running a scan re-alerts nothing (dedup holds)",
      out2 == [] and not posted, f"{len(out2)} signals, {len(posted)} messages")

# ── 10. a later run must not wipe an earlier run's breakout history ──────────
def _bo(sym):
    return {"symbol": sym, "timeframe": "Weekly", "pattern": "p", "patterns": ["p"],
            "price": 100.0, "sl": 95.0, "target1": 105.0, "target2": 110.0,
            "target3": 115.0, "rr": 1.5, "vol_ratio": 1.1, "fno": 0, "tv_link": ""}

tracker.log_breakouts([_bo("MIDDAY1"), _bo("MIDDAY2")])
# EOD run: different symbols, because dedup excluded the midday ones.
tracker.log_breakouts([_bo("EOD1"), _bo("EOD2")])
with tracker._conn() as c:
    kept = {r[0] for r in c.execute(
        "SELECT symbol FROM breakouts WHERE symbol LIKE 'MIDDAY%' OR symbol LIKE 'EOD%'"
    ).fetchall()}
check("later scan preserves earlier breakout rows",
      kept == {"MIDDAY1", "MIDDAY2", "EOD1", "EOD2"}, str(sorted(kept)))
# Re-scanning the same symbol still replaces rather than duplicating.
tracker.log_breakouts([_bo("EOD1"), _bo("EOD2")])
with tracker._conn() as c:
    n_eod1 = c.execute(
        "SELECT COUNT(*) FROM breakouts WHERE symbol='EOD1'").fetchone()[0]
check("re-scan replaces the same symbol, no duplicate row", n_eod1 == 1, f"rows={n_eod1}")

# ── the completion summary must survive whatever lands in `counts` ────────────
# A {"mode": "position-management-only"} marker in the midday counts made
# sum(counts.values()) raise "unsupported operand type(s) for +: 'int' and
# 'str'". That was caught by the outer handler, so every midday run reported
# itself to Telegram as a Scanner Error AFTER completing its work correctly.
def _summarise(counts, mode=None):
    nums = {k: v for k, v in counts.items() if isinstance(v, (int, float))}
    total = sum(nums.values())
    parts = [f"{k.upper()}: {v}" for k, v in nums.items() if v > 0]
    return total, parts

try:
    t, p = _summarise({"mode": "position-management-only"})
    check("summary survives a non-numeric count", t == 0 and p == [], f"total={t} parts={p}")
except TypeError as e:
    check("summary survives a non-numeric count", False, str(e))

t, p = _summarise({"breakouts": 3, "swing": 0, "commodities": 2})
check("summary still totals numeric counts", t == 5 and len(p) == 2, f"total={t} parts={p}")

# The midday slot itself must now hand back numbers only.
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "standalone_scan.py")).read()
check("midday slot emits numeric counts only",
      'counts = {"mode": "position-management-only"}' not in src)

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("ALL CHECKS PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)

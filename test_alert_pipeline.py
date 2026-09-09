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

# ── The engine name map does not drift ───────────────────────────────────────
# engine_names.py is a MIRROR of ENGINE_REGISTRY in the signal site's
# signal.js. Nothing enforces that across two repositories, so what IS enforced
# here is the half that can be: every engine this ledger knows about has a
# published name, and no alert can print a database key at a reader.
import engine_names as en

_known = set(en.ENGINE_NAMES) | set(en.LEDGER_ONLY)
_unnamed = sorted(set(en._remarks()) - _known)
check("every engine in tracker.REMARKS has a published name",
      not _unnamed, f"unnamed: {_unnamed}")

# REMARKS is read by parsing tracker.py, not by importing it — importing pulls
# pandas and yfinance, and the first version of this returned "" everywhere
# while looking like it worked.
check("REMARKS is readable without importing tracker", len(en._remarks()) > 5,
      f"{len(en._remarks())} entries")

# The two engines that write their own reasons carry no REMARKS entry, so the
# rule fallback is empty for them by design — their metadata `why` is better.
# Anything else with neither would alert with no explanation at all.
_mute = [k for k in en.ENGINE_NAMES
         if not en.engine_rule(k) and k not in ("ledge", "keel",
                                                "buoy", "anchor", "bedrock")]
check("every named engine can explain itself", not _mute, f"silent: {_mute}")

check("a blank signal_type still names something",
      en.engine_name("") == "Unattributed" and en.engine_name(None))
check("an unknown key is made readable, not printed raw",
      en.engine_name("some_new_engine") == "SOME NEW ENGINE")
# The count that disagreed with itself in public: the home page counted
# NAMES (8) and the signals page counted KEYS (9), because TIDAL is one screen
# at two depths. Both numbers were right and the page never said which it
# meant. This pins the arithmetic on the Python side; signal.js pins the same
# arithmetic on the JS side, and neither may state a bare total again.
_t = en.published_tally()
check("eight engines publish over nine configurations",
      _t == {"names": 8, "keys": 9, "research": 3}, _t)
check("the tally states its own arithmetic",
      "8 names over 9 configurations" in en.tally_note()
      and "not cleared to publish" in en.tally_note(), en.tally_note())
check("the research floor is named but never counted as publishing",
      all(k in en.ENGINE_NAMES and not en.is_published_engine(k)
          for k in en.RESEARCH_ONLY))

# The engine's lifetime record must not ride along on a message about one trade.
check("the rule drops the engine's lifetime measurement",
      "[" not in en.engine_rule("breakout")
      and "stop-out" not in en.engine_rule("breakout"),
      en.engine_rule("breakout"))

# why_lines is per-signal and must never invent one.
check("no metadata means no measured reason", en.why_lines({}) == []
      and en.why_lines(None) == [] and en.why_lines("not json") == [])
check("a measured reason is read from the row",
      en.why_lines({"why": ["a", "b", "c"]}) == ["a", "b"])
check("a why block labels which claim it is making",
      en.why_block("ledge", {"why": ["x"]}).startswith("*What fired it:*")
      and en.why_block("breakout", {}).startswith("_Engine rule:"))
check("an engine with neither yields nothing, not an empty heading",
      en.why_block("keel", {}) == "")

check("age reads in hours under two days and days after",
      en.held_for(6) == "held 6h" and en.held_for(72) == "held 3d"
      and en.held_for(None) == "")
check("a signal with no filing date says nothing rather than guessing",
      en.filed_on(None) == ""
      and en.context_line("ledge") == "LEDGE · Base breakout")


# ── Position alerts actually render ──────────────────────────────────────────
# Every branch of run_price_alerts composes its own message, and the whole loop
# body sits inside `except Exception: logging.warning(...); continue`. So a
# NameError in one composer does not crash the scan — it deletes that alert and
# writes a line to a log nobody reads, which is the same silent-loss shape as
# 2026-07-31. These checks run each branch end to end against a stubbed price
# feed and read the message that comes out.
import types
from datetime import timedelta as _td

_IST = standalone_scan.IST
_TODAY = standalone_scan.datetime.now(_IST)


# Two quiet bars ahead of the one that triggers. A single-bar frame is not a
# realistic feed — _bar_window never asks for less than 5 days — and pandas
# .squeeze() collapses a one-row column to a scalar, which fails differently
# from anything production can produce.
_QUIET = (100.0, 101.0, 99.5, 100.0)


def _bars(trigger, quiet=_QUIET):
    """A price frame ending today: two quiet days, then the bar that fires.

    Naive DatetimeIndex, as yfinance returns for daily bars. Dated so every row
    falls after a signal filed three days ago — _since_entry grades only bars
    printed while the trade was open, and correctly refuses everything else.
    """
    import pandas as _pd
    rows = [quiet, quiet, trigger]
    idx = [_TODAY.replace(tzinfo=None) - _td(days=n) for n in (2, 1, 0)]
    return _pd.DataFrame(rows, index=_pd.DatetimeIndex(idx),
                         columns=["Open", "High", "Low", "Close"])


_FEED = {}


_CALLS = []          # every download this suite triggers, for the batch check


class _FakeYF(types.ModuleType):
    @staticmethod
    def download(ticker, **kw):
        """Answers one ticker or a list, as yfinance does.

        A list comes back with MultiIndex columns in the layout `group_by`
        asks for. Getting that layout wrong is not a loud failure — _own_frame
        returns None for every ticker and the batch degrades to one request
        each, correct and no faster — so the fake has to be honest about it or
        the test proves nothing.
        """
        import pandas as _pd
        if isinstance(ticker, (list, tuple, set)):
            names = [str(t) for t in ticker]
            _CALLS.append(("batch", tuple(sorted(names))))
            frames = {n: _FEED.get(n.replace(".NS", "")) for n in names}
            frames = {n: f for n, f in frames.items() if f is not None and len(f)}
            if not frames:
                return _pd.DataFrame()
            wide = {}
            for n, f in frames.items():
                for col in ("Open", "High", "Low", "Close"):
                    if kw.get("group_by") == "ticker":
                        wide[(n, col)] = f[col]
                    else:                       # "column" — yfinance's default
                        wide[(col, n)] = f[col]
            out = _pd.DataFrame(wide)
            out.columns = _pd.MultiIndex.from_tuples(out.columns)
            return out
        _CALLS.append(("single", str(ticker)))
        return _FEED.get(str(ticker).replace(".NS", ""), _pd.DataFrame())


sys.modules["yfinance"] = _FakeYF("yfinance")


def _file(sym, sig_type, days_ago, tf="SWING", meta=None):
    """One OPEN row, filed `days_ago` days back. Returns its id."""
    d = (_TODAY - _td(days=days_ago)).strftime("%Y-%m-%d")
    rid = tracker.log_to_all_signals(
        sym, sig_type, "BUY", 100.0, 95.0, 108.0, 116.0, 124.0, rr=1.6,
        timeframe=tf, score=70, metadata=dict(meta or {}, sl1=97.0))
    if rid:
        with tracker._conn() as c:
            c.execute("UPDATE all_signals SET date=? WHERE id=?", (d, rid))
            c.commit()
    return rid


def _run_alerts():
    """Run the position pass with Telegram captured. Returns messages sent."""
    sent = []
    real_send = standalone_scan._send
    standalone_scan._send = lambda m, *a, **k: sent.append(m)
    try:
        standalone_scan.run_price_alerts("test-slot")
    finally:
        standalone_scan._send = real_send
    return sent


def _clear_open():
    with tracker._conn() as c:
        c.execute("UPDATE all_signals SET status='VOID' "
                  "WHERE status IN ('OPEN','T1_HIT')")
        c.commit()


LEDGE_META = {"why": ["Closed above a 24-bar base at 103.0",
                      "Volume 2.1x its 20-day average"],
              "invalidate": "A daily CLOSE back under 95.0 — the base failed"}

# high, low chosen per branch; open/close kept inside the range.
# entry 100 · stop 95 · warning stop 97 · T1 108 · T2 116
_CASES = [
    # name        symbol    engine     filed  triggering bar (O,H,L,C)   expect
    ("stop-out",  "TSTSL",  "ledge",     3,  (99, 100, 94, 96),      "SL HIT"),
    ("target 2",  "TSTT2",  "ledge",     3,  (101, 117, 100, 116),   "TARGET 2 HIT"),
    ("target 1",  "TSTT1",  "ledge",     3,  (101, 109, 100, 108),   "TARGET 1 HIT"),
    ("warning",   "TSTS1",  "ledge",     3,  (99, 100, 96.5, 97),    "SL1 WARNING"),
    ("time stop", "TSTEX",  "breakout", 40,  (101, 102, 100, 101),   "TIME STOP"),
]

for label, sym, eng, days, bar, heading in _CASES:
    _clear_open()
    _FEED.clear()
    _FEED[sym] = _bars(bar)
    _file(sym, eng, days, meta=(LEDGE_META if eng == "ledge" else {}))
    msgs = [m for m in _run_alerts() if sym in m]
    check(f"{label} sends an alert", len(msgs) == 1,
          f"got {len(msgs)} message(s)")
    msg = msgs[0] if msgs else ""
    check(f"{label} names the branch", heading in msg, msg[:60])
    # The engine's PUBLISHED name, never the database key.
    want = "LEDGE" if eng == "ledge" else "BREACH"
    check(f"{label} names the engine as {want}", want in msg, msg[:120])
    check(f"{label} does not print the raw key", eng not in msg, msg[:120])
    # When it was filed and how long it has been held.
    check(f"{label} says when it was filed", "filed " in msg, msg[:120])
    check(f"{label} says how long it was held", "held " in msg, msg[:120])
    # Something explaining the setup — measured for LEDGE, the rule otherwise.
    if eng == "ledge":
        check(f"{label} carries the measured reason",
              "What fired it:" in msg and "24-bar base" in msg, msg)
    else:
        check(f"{label} carries the engine rule", "Engine rule:" in msg, msg)
    # No empty headings, no stray separators.
    check(f"{label} has no blank-run or trailing gap",
          "\n\n\n" not in msg and not msg.endswith("\n"), repr(msg[-30:]))

# ── The book is fetched in batches, not one request per position ─────────────
#
# This graded the book with a separate yf.download per open signal — 109 open
# positions, 109 sequential round trips, inside the run that now carries the
# whole trading day. Batching is only safe because _own_frame resolves a ticker
# out of yfinance's MultiIndex instead of letting .squeeze() pick a neighbour's
# column.
#
# The failure mode this guards is quiet: get the `group_by` layout wrong and
# _own_frame returns None for every ticker, every signal falls back to its own
# request, and the result is CORRECT and no faster, with nothing saying so.

_clear_open(); _FEED.clear(); _CALLS.clear()
for _s, _bar in (("BAT001", (99, 100, 94, 96)),        # stop-out
                 ("BAT002", (101, 109, 100, 108)),     # T1
                 ("BAT003", (101, 102, 100, 101))):    # nothing
    _FEED[_s] = _bars(_bar)
    _file(_s, "ledge", 3, meta=LEDGE_META)
_msgs = _run_alerts()

_batches = [c for c in _CALLS if c[0] == "batch"]
_singles = [c for c in _CALLS if c[0] == "single"]
check("three positions are fetched in one request, not three",
      len(_batches) == 1 and not _singles,
      f"{len(_batches)} batch, {len(_singles)} single")
check("the batch asked for every open ticker",
      _batches and set(_batches[0][1]) ==
      {"BAT001.NS", "BAT002.NS", "BAT003.NS"},
      _batches[0][1] if _batches else "no batch")

# Same book, same verdicts. A faster path that grades differently is not an
# optimisation, and the wrong `group_by` would hand every signal its
# neighbour's prices rather than None if _own_frame were not in the way.
check("the batch grades exactly the positions that moved",
      len([m for m in _msgs if "BAT001" in m]) == 1
      and len([m for m in _msgs if "BAT002" in m]) == 1
      and not [m for m in _msgs if "BAT003" in m],
      [m.split("\n")[0] for m in _msgs])
check("the batch grades each one against its OWN bars",
      any("SL HIT — BAT001" in m for m in _msgs)
      and any("TARGET 1 HIT — BAT002" in m for m in _msgs),
      [m.split("\n")[0] for m in _msgs])

# A ticker the batch could not return must still be graded, one request of its
# own — which is what every signal did before the prefetch existed.
_clear_open(); _FEED.clear(); _CALLS.clear()
_FEED["BAT004"] = _bars((99, 100, 94, 96))
_file("BAT004", "ledge", 3, meta=LEDGE_META)
_file("BATMISS", "ledge", 3, meta=LEDGE_META)      # no feed entry at all
_msgs = _run_alerts()
check("a ticker missing from the batch falls back to its own request",
      any(c == ("single", "BATMISS.NS") for c in _CALLS),
      [c for c in _CALLS])
check("the fallback does not cost the batched ones a second request",
      len([c for c in _CALLS if c[0] == "single"]) == 1,
      [c for c in _CALLS if c[0] == "single"])
check("a missing ticker grades nothing and blocks nothing",
      len([m for m in _msgs if "BAT004" in m]) == 1
      and not [m for m in _msgs if "BATMISS" in m],
      [m.split("\n")[0] for m in _msgs])

# One window per signal, computed once. The prefetch sizes each interval group
# to its OLDEST signal; a young signal in an old group must still be graded on
# the bars printed after IT was filed, not the group's.
_clear_open(); _FEED.clear(); _CALLS.clear()
_FEED["BATOLD"] = _bars((101, 102, 100, 101))
_FEED["BATNEW"] = _bars((99, 100, 94, 96))
_file("BATOLD", "ledge", 3, meta=LEDGE_META)
_file("BATNEW", "ledge", 0, meta=LEDGE_META)       # filed today
_msgs = _run_alerts()
check("a signal filed today is not graded on bars that predate it",
      not [m for m in _msgs if "BATNEW" in m],
      [m.split("\n")[0] for m in _msgs])


# ── The frame a signal is graded on ──────────────────────────────────────────
# These three shapes all reached production and all failed SILENTLY, because
# the loop body sits inside `except Exception: continue`. The download now goes
# through scanner._own_frame; these check that it actually helps.

# A frame with exactly ONE usable bar. `df["Close"].squeeze()` returns a scalar
# there, so `.iloc[-1]` raised AttributeError and the position was never graded
# — no alert, no ledger row, one line in a log.
_clear_open(); _FEED.clear()
import pandas as _pd
_one = _pd.DataFrame([(99.0, 100.0, 94.0, 96.0)],
                     index=_pd.DatetimeIndex([_TODAY.replace(tzinfo=None)]),
                     columns=["Open", "High", "Low", "Close"])
_FEED["TST1B"] = _one
_file("TST1B", "ledge", 3, meta=LEDGE_META)
_m = [m for m in _run_alerts() if "TST1B" in m]
check("a one-bar frame is still graded", len(_m) == 1 and "SL HIT" in _m[0],
      _m[0][:80] if _m else "no message — the position was skipped")

# A partial last bar, on the TIME-STOP path — the one branch that books
# `last_close` itself as the exit price, the P&L and the R.
#
# NaN sails through every comparison without raising (`nan <= sl` is False), so
# nothing upstream rejects it, and `round(nan, 2)` went into the ledger as a
# result. The last daily bar being partial is routine, not exotic. The bar must
# be dropped and the one before it graded.
_clear_open(); _FEED.clear()
_nanbar = _bars((101.0, 102.0, 100.0, 101.0))
_nanbar.loc[_nanbar.index[-1], "Close"] = float("nan")
_FEED["TSTNA"] = _nanbar
_file("TSTNA", "breakout", 40)          # 40d against a 480h horizon → EXPIRED
_m = [m for m in _run_alerts() if "TSTNA" in m]
check("a partial last bar still produces a time stop", len(_m) == 1,
      f"{len(_m)} message(s)")
check("the time stop quotes a real price, not nan",
      _m and "nan" not in _m[0].lower(), _m[0] if _m else "no message")
with tracker._conn() as _c:
    _row = _c.execute(
        "SELECT status, exit_price, pnl_pct, r_multiple FROM all_signals "
        "WHERE symbol='TSTNA'").fetchone()
check("the time stop was actually booked", _row and _row[0] == "EXPIRED", _row)
# NaN survives into SQLite as either a NaN float or a NULL, depending on the
# driver — neither is a result, so both fail this.
_nums = list(_row[1:]) if _row else []
check("nothing NaN or NULL was booked as a result",
      _nums and all(v is not None and v == v for v in _nums), _row)

# A frame carrying somebody else's ticker. _own_frame returns None rather than
# picking a neighbour's column — the failure that quoted BPCL at 176.70 when
# BPCL was 317.00, and gave two other companies that same price.
_clear_open(); _FEED.clear()
_wrong = _pd.DataFrame(
    [(99.0, 100.0, 94.0, 96.0)] * 3,
    index=_pd.DatetimeIndex([_TODAY.replace(tzinfo=None) - _td(days=n)
                             for n in (2, 1, 0)]),
    columns=_pd.MultiIndex.from_product(
        [["Open", "High", "Low", "Close"], ["SOMEONEELSE.NS"]]))
_FEED["TSTXX"] = _wrong
_file("TSTXX", "ledge", 3, meta=LEDGE_META)
_m = [m for m in _run_alerts() if "TSTXX" in m]
check("another ticker's frame grades nothing rather than the wrong thing",
      not _m, _m[0][:80] if _m else "")

# The grading path must not squeeze. Squeeze is what made all three of the
# above possible, and it reads as harmless.
_src_alerts = open(os.path.join(REPO, "standalone_scan.py"), encoding="utf-8").read()
_body = _src_alerts[_src_alerts.index("def run_price_alerts("):]
_body = _body[:_body.index("\ndef ")]
_code = "\n".join(l for l in _body.splitlines() if not l.lstrip().startswith("#"))
check("the grading path no longer squeezes a frame", ".squeeze()" not in _code)
check("the grading path resolves the ticker before reading it",
      "_own_frame" in _code)


# The two facts the old messages could not express.
_clear_open(); _FEED.clear()
_FEED["TSTRN"] = _bars((99, 100, 94, 96))
_rid = _file("TSTRN", "ledge", 3, meta=LEDGE_META)
with tracker._conn() as c:
    c.execute("UPDATE all_signals SET alert_flags='T1;' WHERE id=?", (_rid,))
    c.commit()
_m = [m for m in _run_alerts() if "TSTRN" in m]
check("a stop-out after T1 says the runner closed",
      len(_m) == 1 and "T1 was booked earlier" in _m[0], _m[0] if _m else "no message")

_clear_open(); _FEED.clear()
_FEED["TSTNV"] = _bars((99, 100, 94, 96))
_file("TSTNV", "ledge", 3, meta=LEDGE_META)
_m = [m for m in _run_alerts() if "TSTNV" in m]
check("a stop-out that never reached T1 says so",
      len(_m) == 1 and "Never reached T1" in _m[0], _m[0] if _m else "no message")

# A warning must state the room left in the unit the position is sized in.
_clear_open(); _FEED.clear()
_FEED["TSTRM"] = _bars((99, 100, 96.5, 97))
_file("TSTRM", "ledge", 3, meta=LEDGE_META)
_m = [m for m in _run_alerts() if "TSTRM" in m]
check("a warning states the R left to the final stop",
      len(_m) == 1 and "R` left to the final stop" in _m[0],
      _m[0] if _m else "no message")

# A row whose engine writes no `why` and has no REMARKS entry must still send.
# The composer drops the block; it must not drop the message.
_clear_open(); _FEED.clear()
_FEED["TSTBR"] = _bars((99, 100, 94, 96))
_file("TSTBR", "keel", 3)
_m = [m for m in _run_alerts() if "TSTBR" in m]
check("an engine with neither reason nor rule still alerts",
      len(_m) == 1 and "KEEL" in _m[0], _m[0] if _m else "no message")

# Intraday horizons must not report as "0d".
check("a sub-day horizon reads in hours",
      standalone_scan._horizon_txt(8) == "8h"
      and standalone_scan._horizon_txt(480) == "20d")

# _lines drops what the row could not fill, rather than leaving the heading.
check("_lines collapses dropped blocks",
      standalone_scan._lines("a", "", "", "b", "") == "a\n\nb",
      repr(standalone_scan._lines("a", "", "", "b", "")))
check("_lines drops leading blanks",
      standalone_scan._lines("", "a") == "a")

_clear_open()

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("ALL CHECKS PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)

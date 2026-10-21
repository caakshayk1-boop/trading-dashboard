#!/usr/bin/env python3
"""test_engine_alerts.py — engine alerts announce each thing once, only for engines that have earned a buzz.
No network, no credentials. Usage: python test_engine_alerts.py"""
import sys
import engine_alerts as A

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  {detail}"))
    ok, fail = ok + bool(cond), fail + (not cond)


A.ALPHA = "https://alpha.askakshay.com"


def sig(i, engine="breakout", status="pending", events=None, **kw):
    return {"id": f"{engine}:{i}:2026-10-09", "engine": engine, "engine_name": engine.title(), "sym": i, "name": f"{i} Ltd", "setup": "Darvas box",
            "horizon": "positional", "status": status, "entry_zone": [100.0, 101.0], "ref_price": 100.0, "sl": 95.0, "risk_pct": 5.0, "targets": [105.0, 110.0, 120.0],
            "r_multiples": [1.0, 2.0, 4.0], "max_hold_days": 40, "events": events or [], "also_names": [], "entry": 100.0,
            "bench_ok": True, "benchmark": {"name": "Nifty IT", "uptrend": True, "leads_nifty": True, "stock_vs_benchmark_pct": 6.2}, **kw}


feed = {"data_through": "2026-10-09", "engines": {"breakout": {"status": "LEANING POSITIVE", "alertable": True, "trades": {"holdout": {"expectancy_r": 0.17}}}, "rebound": {"status": "WORSE THAN RANDOM", "alertable": False}, "swing": {"status": "LEANING POSITIVE", "alertable": False}},
        "signals": [sig("AAA"), sig("BBB", "rebound"), sig("CCC", "swing")]}

state = {}
todo = A.plan(feed, state)
check("only an engine the site marks alertable alerts (a good label alone is not enough)", [k for _, k, _ in todo] == ["breakout:AAA:2026-10-09"], [k for _, k, _ in todo])
msg = todo[0][2]
check("a new signal states the plan", all(x in msg for x in ("Signal close", "next open", "Stop", "T1", "T2", "T3", "Time limit")))
check("it names the benchmark and how it is behaving", "Nifty IT" in msg and "uptrend" in msg and "leading the Nifty" in msg)
check("it links to alpha only, at the canonical company address", "alpha\\.askakshay\\.com/company/AAA/" in msg and "signal\\.askakshay" not in msg)
check("it says plan, not advice", "not advice" in msg)

weak = {**feed, "signals": [sig("DDD", bench_ok=False)]}
check("benchmark confirmation informs but does not withhold an alert", len(A.plan(weak, {})) == 1)

# a send is recorded, and the same feed then says nothing
state = {"signals": {"breakout:AAA:2026-10-09": {"events": [], "sent": ["new"]}}}
check("an announced signal is not announced again", A.plan(feed, state) == [])

# the lifecycle: entry, T1, stop each announced once
ev = [{"kind": "entry", "date": "2026-10-12", "price": 100.0, "fraction": 1.0}, {"kind": "t1", "date": "2026-10-14", "price": 105.0, "fraction": 0.333}]
feed2 = {**feed, "signals": [sig("AAA", status="open", events=ev, hit=1)]}
todo2 = A.plan(feed2, state)
check("entry and T1 are each announced", [k.split("|")[1] for _, k, _ in todo2] == ["entry:2026-10-12", "t1:2026-10-14"], [k for _, k, _ in todo2])
check("T1 says the stop moves to entry", "moves to entry" in todo2[1][2])
state["signals"]["breakout:AAA:2026-10-09"]["events"] = ["entry:2026-10-12", "t1:2026-10-14"]
check("announced events are not repeated", A.plan(feed2, state) == [])
ev3 = ev + [{"kind": "stop_after_t1", "date": "2026-10-16", "price": 100.0, "fraction": 0.667}]
todo3 = A.plan({**feed, "signals": [sig("AAA", status="closed", events=ev3, hit=1)]}, state)
check("a close is announced", len(todo3) == 1 and "stopped" in todo3[0][2])

# a signal that closed before the job ever saw it is recorded silently
feed4 = {**feed, "signals": [sig("ZZZ", status="closed", events=ev3, hit=1)]}
st4 = {}
check("a trade that closed unseen stays silent", A.plan(feed4, st4) == [] and "breakout:ZZZ:2026-10-09" in st4["signals"])

# markdown must survive awkward names
weird = sig("M&M", name="Mahindra_&_Mahindra (1)*", sl=95.0)
m = A.new_message(weird, {"status": "LEANING POSITIVE"})
check("awkward names are escaped", "Mahindra\\_&\\_Mahindra \\(1\\)\\*" in m, m[:200])

# Swing v2 (alpha, 21 Oct 2026): one engine, alertable is the whole verdict, two targets, profit lock and trail
v2 = {"schema": "alpha-signals/2-compat", "data_through": "2026-10-21", "engines": {"swing": {"key": "swing", "status": "GATE OFF", "alertable": False}},
      "signals": [{**sig("SWG", engine="swing", status="pending"), "targets": [110.0, 120.0], "r_multiples": [2.0, 4.0], "engine_name": "Swing", "setup": "Pullback"}]}
check("v2 feed: alerts off means nothing is announced", A.plan(v2, {}) == [])
v2on = {**v2, "engines": {"swing": {"key": "swing", "status": "GATE OFF", "alertable": True}}}
t2 = A.plan(v2on, {})
check("v2 feed: alertable alone lets a new plan through", len(t2) == 1 and "sell half" in t2[0][2], t2)
lock = {**v2on["signals"][0], "status": "closed", "stop_now": 102.5, "events": [{"kind": "entry", "date": "2026-10-22", "price": 100.0, "fraction": 1.0}, {"kind": "t1", "date": "2026-10-23", "price": 110.0, "fraction": 0.5}, {"kind": "lock", "date": "2026-10-24", "price": 102.5, "fraction": 0.5}]}
msgs = [A.event_message(lock, e) for e in lock["events"]]
check("v2 events: T1 says half sold and a locked profit; the lock exit says it closes in profit", "half sold" in msgs[1] and "locked profit" in msgs[1] and "profit lock" in msgs[2], msgs)
print(f"\n{ok} passed · {fail} failed")
sys.exit(1 if fail else 0)

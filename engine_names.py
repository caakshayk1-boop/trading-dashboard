"""
engine_names.py — how a signal describes itself to a human.

WHY THIS FILE EXISTS
--------------------
`signal_type` is a database key: 'breakout', 'magic', 'equity_measured'. The
sites render those as BREACH, TIDAL and PLUMB, and until now that mapping lived
in exactly one place — `ENGINE_REGISTRY` in signal.askakshay.com's signal.js.

So a reader who saw "TIDAL" on the site and then got a Telegram alert saying
"magicmagic" had no way to know they were the same engine. The alert was
naming an internal key at a person.

This is the Python side of that map, plus the two functions that turn a ledger
row into sentences: what the engine SAW when it filed (per signal, measured),
and what the engine DOES in general (standing rule). Those are different
claims and this module refuses to blur them — see `why_lines` vs `engine_rule`.

It is a MIRROR of the JS registry, and mirrors drift, so:

  · the JS copy (public/signal.js, ENGINE_REGISTRY) is the one the sites read
  · this copy is the one the bot and the brief read
  · `test_engine_names.py` asserts every key here exists in tracker.REMARKS,
    which is the ledger's own list of engines — so an engine added to the
    ledger and forgotten here fails a test rather than printing a raw key

The alternative was to publish names from Python into engines.json and have
the site read them. That is the better shape and it is not free: signal.js
already hardcodes the registry with each engine's band, role and timeframe,
and half-migrating it would leave two sources that BOTH claim to be
authoritative. One documented mirror beats that.

TIDAL twice, on purpose
-----------------------
`magic` and `magicmagic` are one screen run at two depths. They share a name
and are separated by their band, exactly as the site separates them.
"""

import json as _json

# key → (published name, what it hunts, the clock it runs on, band or None)
ENGINE_NAMES = {
    "pivot":           ("PIVOT",  "Reaction at a level","Daily → weeks",      None),
    "breakout":        ("BREACH", "Breakouts",        "Daily → weeks",        None),
    "magic":           ("TIDAL",  "Recovery",         "Weekly → months",      ">15% off the high"),
    "magicmagic":      ("TIDAL",  "Recovery",         "Weekly → months",      "20–40% off the high"),
    "equity_measured": ("PLUMB",  "Measured equity",  "Daily → days",         None),
    "multibagger":     ("ASCENT", "Leaders",          "Weekly → 6–12 months", None),
    "momentum_quant":  ("VECTOR", "Momentum",         "Monthly → months",     None),
    "ai_longterm":     ("NORTH",  "Long horizon",     "Weekly → months",      None),
    "ledge":           ("LEDGE",  "Base breakout",    "Daily → weeks",        "≥12% off the high"),
    "keel":            ("KEEL",   "Divergence turn",  "Daily → weeks",        "≥12% off the high"),
    "intraday":        ("GUST",   "Intraday momentum","15-minute → the close",  None),
    # Research floor — measured, published, NOT cleared to file signals. Named
    # here so an alert that somehow carries one is still legible rather than
    # printing a raw key; the site keeps them off /signals and /engines.
    "buoy":            ("BUOY",    "Reclaim",         "4-hour → days",        None),
    "anchor":          ("ANCHOR",  "Floor reversal",  "Daily → weeks",        None),
    "bedrock":         ("BEDROCK", "Bottom reversal", "Daily → weeks",        None),
}

# ── WHICH OF THOSE ARE STILL RUNNING ─────────────────────────────────────────
#
# A NAME IS PERMANENT; A FLOOR SLOT IS NOT. An engine that is switched off
# keeps its entry above, because its closed trades stay in the ledger forever
# and must keep rendering as a name rather than as a key. What changes is
# whether this site COUNTS it: a retired engine is out of the published
# record, out of the roster, and out of the regime table.
#
# THIS MAP EXISTS BECAUSE ONE FACT WAS WRITTEN IN THREE PLACES AND TWO WENT
# STALE. On 2026-09-19 signal.js had retired magic, equity_measured and
# ai_longterm; regime.py's own RETIRED still listed only four intraday-era
# keys from August; and this module still carried `intraday` as ledger-only
# although the site had promoted it to GUST. What a reader saw was a front
# page reporting 65 published / 12 closed / 8.3% -- twenty rows of it from
# engines the same page said were retired -- beside a regime panel reporting
# 11 closed / 9.1% on a different population, with nothing to say which was
# the record. Neither was.
#
# regime.py imports this, and so does anything else that needs to know. The
# dates match the JS registry, which test_engine_names.py now asserts rather
# than trusts.
RETIRED = {
    "magic":           "2026-09-18",   # TIDAL's shallow band; magicmagic keeps the name
    "equity_measured": "2026-09-18",   # PLUMB -- 16 closed, -0.535R, t=-2.92
    "ai_longterm":     "2026-09-18",   # NORTH -- no closed trade, no measured basis
    "ohl":             "2026-09-17",
    "4h":              "2026-08-01",
    "ai_4h":           "2026-07-29",
    "ai_daily":        "2026-07-29",
}

# The research floor: measured and published, never cleared to file a signal.
RESEARCH = ("buoy", "anchor", "bedrock")

# What this site publishes TODAY -- derived, because a second hand-written
# list is the exact thing this block exists to stop.
LIVE = {k: v for k, v in ENGINE_NAMES.items()
        if k not in RETIRED and k not in RESEARCH}


# Engines that exist in the ledger but are not on the published floor. They are
# named rather than hidden: a row from one of them must still read as something
# rather than as a key, and calling it "not a published engine" is the honest
# label. Mirrors the "keys in the ledger that are not on this floor" block the
# site already renders on /engines.
LEDGER_ONLY = {
    "cf_1h":          "Commodity 1h channel",
    "commodity":      "Commodity scan",
    "ohl":            "Open-High-Low intraday",
    "4h":             "4-hour scan",
    "ai_4h":          "AI 4-hour",
    "ai_daily":       "AI daily",
    "top5_pick":      "Weekly Top 5",
    "sip_bucket":     "SIP allocation",
    "basebreak":      "Base break",
    # ── TWO WRITERS THAT WERE ON NO LIST AT ALL ──────────────────────────────
    #
    # Found 2026-09-21 by the completeness check below, which did not exist
    # before it. `swing` is written by standalone_scan.run_swing_scan, which
    # runs in the eod, weekend and full slots; `manual` by claude_bot when a
    # signal is filed by hand. Neither is in engines.js, neither was in
    # ENGINE_NAMES, and neither was here — so engine_name() fell through to
    # its last resort and would have printed the key back, uppercased, as if
    # SWING were a published engine's name.
    #
    # The 2026-09-18 cleanup unwired four writers the roster did not name —
    # 4h, ai_4h, ai_daily, commodity — on exactly that argument. These two
    # survived it because nothing enumerated the writers; the check was
    # ENGINE_NAMES -> tracker.REMARKS, which only ever asks about keys
    # somebody had already remembered to name.
    #
    # Naming them is not a decision to keep running them. run_swing_scan has
    # returned 0 signals on every scan_meta record back to 2026-09-04 and the
    # legacy `signals` table it also writes is empty; whether it stays wired
    # is a decision about signal generation and is not made here.
    "swing":          "Swing scan",
    "manual":         "Filed by hand",
    # claude_bot's own scan runner, which is both the /scan command and the
    # bot's scheduler. Found by the same check, in the same pass.
    "4h_momentum":    "4-hour momentum",
}


# ── WHICH ENGINES ARE ALLOWED TO REACH THE PHONE ─────────────────────────────
#
# This was a comment, and a comment cannot stop a function calling _send().
#
# On 2026-09-17 the intraday engine was brought back under a note reading "IT
# IS RESEARCH TIER AND ALERTS NOTHING ... it is logged and shown and never
# sent — the same terms PIVOT came back on". run_intraday_scan calls
# _send_chunked. The policy and the code disagreed, and the only reason nobody
# got the alerts is that the slot it was wired to does not exist, so it has
# never run at all.
#
# PIVOT does it correctly: it writes the ledger, records the delivery as
# sent=False WITH a reason, and sends nothing — so afterwards "deliberately not
# sent" is distinguishable from "tried and failed". That is the behaviour this
# makes general.
#
# The bar is this book's own: 30 closed trades at t >= 2. An engine that has
# not cleared it may run forward, may fill the ledger and may be published —
# it may not interrupt somebody's evening. Promoting one for having impressed
# on a small sample is the precise error the thirty-trade rule exists to stop.
#
# ── GUST WAS PROMOTED ON 2026-09-21, BELOW THAT BAR, BY DECISION ─────────────
#
# Akshay, asked directly whether the account can now trade a 15-minute chart:
# yes. That is the fact this turned on, and it is not one the ledger can
# answer. swing_rulebook's 2026-08-25 review put intraday OUT_OF_MANDATE for
# the CLOCK, not the record — "intraday posts +1.47R at t=+3.69 and is out for
# trading a 15-minute chart, which this account cannot." When the account
# changes, that reason stops applying.
#
# WHAT THE PROMOTION DOES NOT DO IS MAKE THE SAMPLE BIGGER, and this is
# recorded rather than glossed:
#
#   · 17 closed, thirteen short of this book's own thirty.
#   · every one of them filed 2026-06-11 to 2026-07-29 — ALL PRE-LAUNCH, on a
#     ledger the repo says has been re-graded twice under stop rules it has
#     since called wrong. None of it is in the published record, because
#     in_book starts at 2026-09-02.
#   · nothing since it was re-wired to the midday slot. The forward sample is
#     zero.
#
# So this is a decision taken on an operational change with the statistical
# case stated as weaker than it looks, not a promotion the numbers earned.
# t >= 2 at 30 closed remains the bar for every other engine; if GUST's
# forward record does not hold up, it comes back here and the reason gets
# written beside it.
#
# It self-corrects one thing automatically: standalone_scan's midday slot sets
# quiet_on_success from may_alert("intraday"), so the slot resumes reporting
# itself the moment this entry is removed. That was built to read the gate
# rather than a second list, exactly so this edit is the only edit.
ALERTS_SUPPRESSED = {
    # key                reason written into the delivery record
    "pivot":    "research tier — logged, never alerted. No closed trade and no "
                "measured expectancy yet.",
    # ── cf_1h COMES BACK SILENT, 2026-09-21 ──────────────────────────────────
    #
    # It posts the best t-statistic in the ledger — 367 closed, +0.334R,
    # t = +3.85 — and every one of those 367 was filed between 2026-06-03 and
    # 2026-08-21 by ACCIDENT. cf_scan was the FALLTHROUGH case in
    # scheduled_tasks.yml: any unrecognised cron string became a CF scan. When
    # the cron strings were corrected the accidental trigger vanished, which is
    # why the engine stopped dead and nothing reported it.
    #
    # It is being given a real cron for the first time, and it comes back on
    # PIVOT's terms rather than its own record, because that record cannot
    # carry the decision:
    #
    #   · 227 COMEX + 140 FX, zero NSE. swing_rulebook refuses all 367 —
    #     "not an Indian listed equity" — so no site publishes it and the
    #     mandate will not fund it.
    #   · 186 of 367 are SELL against a long-only book.
    #   · all of it pre-LAUNCH, so none of it is in the published record.
    #
    # Filing silently rebuilds a forward sample under current rules. Alerting
    # on it would put GOLD, CRUDE and USDJPY shorts on a phone for an engine
    # no page shows — the precise outcome the fallthrough was removed to stop.
    "cf_1h":    "filing silently — a real cron since 2026-09-21, rebuilding a "
                "forward sample. Its 367-trade record was produced by an "
                "accidental cron fallthrough, is entirely pre-launch, and is "
                "FX and commodity against a long-only Indian-equity book.",
}


def may_alert(signal_type) -> tuple:
    """(allowed, reason) — may this engine's signals be sent to Telegram?

    `reason` is None when allowed, and the sentence to write into the delivery
    record when not. Every scan that sends must ask; a scan that asks and is
    refused still logs to the ledger, because running forward silently is the
    whole point of the research tier.
    """
    k = str(signal_type or "").strip()
    # A RETIRED ENGINE DOES NOT ALERT EITHER, and this half was missing.
    #
    # tracker.drop_retired() stops a retired engine writing to the ledger. It
    # does not stop it SENDING: run_4h_scan does
    #
    #     ids  = log_batch_to_all_signals(rows)     <- refused, ids = []
    #     sent = _send_chunked(...)                 <- fires anyway
    #
    # so the write gate on its own produced a Telegram alert with no ledger row
    # behind it at all — strictly worse than the bug it fixed, because an alert
    # you cannot look up afterwards is the one thing this book must never send.
    # Both halves of the refusal belong together.
    #
    # The reason is DIFFERENT from a research-tier one on purpose. A research
    # engine logs and does not alert, because running it forward silently is
    # the point. A retired engine does neither; it is finished.
    if k in RETIRED:
        return False, (f"{k} was retired on {RETIRED[k]} — it files nothing "
                       f"and alerts nothing. Trades opened before that date "
                       f"are still managed and still alert.")
    if k in ALERTS_SUPPRESSED:
        return False, ALERTS_SUPPRESSED[k]
    return True, None


def engine_name(signal_type) -> str:
    """The published name, or the plainest thing available.

    Never returns the raw key when a better word exists, and never returns an
    empty string — an alert with a blank engine is worse than one naming a key.
    """
    k = str(signal_type or "").strip()
    if not k:
        return "Unattributed"
    if k in ENGINE_NAMES:
        return ENGINE_NAMES[k][0]
    if k in LEDGER_ONLY:
        return LEDGER_ONLY[k]
    # Unknown key: make it readable rather than pretending it is a name.
    return k.replace("_", " ").upper()


def engine_role(signal_type) -> str:
    """What the engine hunts. Empty when unknown — a guessed role is worse."""
    k = str(signal_type or "").strip()
    return ENGINE_NAMES[k][1] if k in ENGINE_NAMES else ""


def engine_band(signal_type) -> str:
    """The band that separates two runs of one screen. Empty for most."""
    k = str(signal_type or "").strip()
    return (ENGINE_NAMES[k][3] or "") if k in ENGINE_NAMES else ""


def _name_is_shared(name) -> bool:
    """True when more than one KEY publishes this name.

    Counts EVERY entry, retired ones included — the same set the browser's
    `shared()` counts. A retired engine keeps its name forever because its
    closed trades stay in the ledger forever, so the ambiguity it created
    does not end when it stops filing. TIDAL is shared today and will still
    be shared after `magic` has been switched off for a year.
    """
    return sum(1 for v in ENGINE_NAMES.values() if v[0] == name) > 1


def engine_label(signal_type) -> str:
    """The name to PRINT: the published name, plus the band when it is needed.

    Mirrors ENGINE_BOOK.label() in engines.js, line for line, and exists for
    the reason that function exists.

    `magic` and `magicmagic` are one screen read at two depths and share the
    name TIDAL on purpose. On the site that is fine — the card prints the band
    underneath. In a Telegram alert there is no card and no underneath, so two
    engines with different rules, different bands and different records both
    arrived on the phone reading exactly `TIDAL`, and a reader could not tell
    which one had fired. `magic` is retired with twenty positions still open
    and `magicmagic` is live: the two alerts a reader most needs to tell apart
    were the two that were indistinguishable.

    NOT a change to engine_name(). That function is held equal to the
    browser's `name()` in both directions by test_engine_names.py, which is
    the check that would have caught PLUMB filing after its own retirement.
    Widening it to sometimes return a name-plus-band would have made that
    comparison meaningless. This is a second function, mirroring a second
    function, pinned by its own check.

    The band is appended ONLY where the name is shared, which is also the
    browser's rule: LEDGE and KEEL both carry a band and neither prints it,
    because neither name is ambiguous and a band on an unambiguous name is
    noise dressed as precision.
    """
    k = str(signal_type or "").strip()
    if k not in ENGINE_NAMES:
        return engine_name(k)
    name, band = ENGINE_NAMES[k][0], ENGINE_NAMES[k][3]
    return f"{name} · {band}" if band and _name_is_shared(name) else name


def is_published_engine(signal_type) -> bool:
    """True for the engines the site lists as publishing.

    The research three are deliberately NOT in this set: they are measured and
    shown, and nothing they produce is filed as a signal.
    """
    k = str(signal_type or "").strip()
    # AND NOT RETIRED. This read `k in ENGINE_NAMES and k not in RESEARCH_ONLY`
    # and so kept counting switched-off engines: on 2026-09-19 published_tally
    # returned 10 names over 11 keys while the site — correctly — said eight.
    # It is the same "count every key in the registry" error that had put
    # retired engines back into the front page's own record, made twice,
    # independently, in two languages.
    return k in ENGINE_NAMES and k not in RESEARCH_ONLY and k not in RETIRED


# ── WHERE THIS SITE'S OWN RECORD BEGINS ──────────────────────────────────────
#
# Everything before it belongs to news.askakshay.com: a different site, under
# stop rules this book has since said were wrong, on a ledger that has been
# re-graded twice. regime.py had its own copy of this date and telegram's
# /performance had no notion of it at all.
#
# Kept here beside RETIRED because the two are one question — "is this row part
# of what this site is accountable for" — and answering it from two files is
# how the front page came to publish a record containing retired engines.
# engines.js carries the same date for the browser, and test_engine_names.py
# asserts the two agree.
LAUNCH = "2026-09-02"


# ── THE POPULATION, ONE PREDICATE ────────────────────────────────────────────
#
# Mirrors ENGINE_BOOK.inBook() in engines.js exactly, and exists because
# tracker.get_performance() — the function behind Telegram's /performance —
# opens with "Performance from ALL signal types". It counted retired engines,
# news.askakshay.com's commodity and top5_pick rows, COMEX gold, US equities,
# shorts that were never sent to anybody, and the whole history before this
# site existed. So the number on the phone was a fifth answer to "what is the
# record", alongside the four already on the pages.
#
# THE RUPEE TEST IS A GUARD, NOT A FILTER. Applying the engine rule already
# leaves nothing but Indian names; if that ever stops being true the bot must
# not quietly start counting a gold future.
def in_book(row) -> bool:
    """Is this ledger row part of what signal.askakshay.com is accountable for?"""
    if not row:
        return False
    g = row.get if hasattr(row, "get") else (lambda k, d=None: getattr(row, k, d))
    if str(g("signal_type") or "") not in LIVE:
        return False
    if str(g("action") or "BUY").upper() == "SELL":
        return False
    cur = str(g("currency") or "₹")
    if cur != "₹":
        return False
    return str(g("date") or "")[:10] >= LAUNCH


# The research floor: measured, published on /research, and NOT cleared to
# file signals. Kept as its own set so `published_tally` cannot drift from
# `is_published_engine` — the two disagreeing is exactly how the site came to
# print "8 engines" on one page and "9" on another.
RESEARCH_ONLY = ("buoy", "anchor", "bedrock")


def published_tally() -> dict:
    """The arithmetic behind "how many engines", never a bare total.

    Eight NAMES run over nine KEYS, because TIDAL is one screen at two depths.
    Counting keys gives 9, counting names gives 8, and the two pages of the
    site did one each — which is the discrepancy a reader actually noticed.
    Anything that states a count here states the arithmetic with it.
    """
    keys = [k for k in ENGINE_NAMES if is_published_engine(k)]
    return {"names": len({ENGINE_NAMES[k][0] for k in keys}),
            "keys": len(keys),
            "research": len(RESEARCH_ONLY)}


def tally_note() -> str:
    """One sentence a message can print, matching the site's wording exactly."""
    t = published_tally()
    band = (f" — {t['names']} names over {t['keys']} configurations, "
            f"because TIDAL runs two bands") if t["keys"] > t["names"] else ""
    return (f"{t['names']} engines publish{band}. A further {t['research']} are "
            f"measured but not cleared to publish.")


def engine_line(signal_type) -> str:
    """One line naming the engine and its job: "LEDGE · Base breakout"."""
    name = engine_label(signal_type)
    role = engine_role(signal_type)
    return f"{name} · {role}" if role else name


def _meta(metadata):
    """Whatever the row carries as metadata, as a dict. Never raises."""
    if isinstance(metadata, dict):
        return metadata
    try:
        d = _json.loads(str(metadata or "") or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:                                   # noqa: BLE001
        return {}


def why_lines(metadata, limit=2) -> list:
    """What the engine MEASURED on the bar that fired, for this signal.

    LEDGE and KEEL write a `why` list at file time — "Closed above a 24-bar
    base at 3455.0", "Volume 2.1x its 20-day average". Those are observations
    about THIS trade, taken from the bar, and they are the only thing in the
    row that honestly answers "why did this fire".

    Most engines write nothing here, and this returns an empty list rather than
    inventing a reason. `engine_rule` is the fallback, and it is deliberately a
    separate function so a caller cannot print a generic rule under a heading
    that claims it describes this trade.
    """
    w = _meta(metadata).get("why")
    if isinstance(w, str):
        w = [w]
    if not isinstance(w, (list, tuple)):
        return []
    out = [str(x).strip() for x in w if str(x or "").strip()]
    return out[:limit]


def invalidated_by(metadata) -> str:
    """The condition that kills the setup, as written at file time.

    Only worth printing while the trade is still alive. After a stop-out it is
    a restatement of what just happened.
    """
    v = _meta(metadata).get("invalidate")
    return str(v).strip() if v else ""


_REMARKS_CACHE = None


def _remarks() -> dict:
    """tracker.REMARKS, read WITHOUT importing tracker.

    `import tracker` pulls pandas and yfinance. Making the engine's own
    description depend on a market-data library means the description silently
    vanishes anywhere those are absent — which is every test runner in this
    repo, and was how the first version of this file returned "" for every
    engine while looking like it worked.

    REMARKS is a literal dict of literal strings, so ast.literal_eval on that
    one assignment is exact, side-effect-free and offline. It fails closed: an
    unreadable tracker.py yields {} and the caller prints no rule, never a
    wrong one.
    """
    global _REMARKS_CACHE
    if _REMARKS_CACHE is not None:
        return _REMARKS_CACHE
    _REMARKS_CACHE = {}
    try:
        import ast, os
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "tracker.py"), encoding="utf-8").read()
        for node in ast.parse(src).body:
            if (isinstance(node, ast.Assign)
                    and any(getattr(t, "id", "") == "REMARKS" for t in node.targets)):
                val = ast.literal_eval(node.value)
                if isinstance(val, dict):
                    _REMARKS_CACHE = {str(k): str(v) for k, v in val.items()}
                break
    except Exception:                                   # noqa: BLE001
        _REMARKS_CACHE = {}
    return _REMARKS_CACHE


def engine_rule(signal_type, remarks=None, limit=120) -> str:
    """The engine's standing rule — what it does on every trade, not this one.

    `remarks` is preferred because it is what the row itself carries, but note
    that log_to_all_signals defaults it to tracker.REMARKS[signal_type], so in
    practice both paths usually yield the same standing text. That is exactly
    why this is not called a "why": it describes the ENGINE.

    The bracketed measurement at the end of every REMARKS entry — "[3.51%
    median stop, 81.8% stop-out]" — is dropped. It is the engine's lifetime
    record, it is on the site, and in a message about ONE trade it reads as a
    comment on that trade, which it is not.
    """
    txt = str(remarks or "").strip()
    if not txt:
        txt = str(_remarks().get(str(signal_type or ""), "") or "")
    if not txt:
        return ""
    txt = txt.split("[")[0].strip(" .·—-")
    # The first sentence is the rule; the rest is usually its history.
    first = txt.split(". ")[0].strip()
    if len(first) > limit:
        first = first[: limit - 1].rsplit(" ", 1)[0] + "…"
    return first


def held_for(hours) -> str:
    """How long the position has been open, in the coarsest honest unit.

    Hours under a day, days after that. "held 0.4d" is precision the reader
    cannot use and the underlying timestamp does not always support — rows
    filed before the tracker timezone fix are off by 5h30m.
    """
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return ""
    if h < 0:
        return ""
    if h < 48:
        return f"held {int(round(h))}h"
    return f"held {int(h // 24)}d"


def filed_on(opened_at) -> str:
    """The signal's own date, as "02 Sep". Empty when it has none.

    An alert that cannot say when the trade was filed says nothing rather than
    guessing today — the age is the whole point of the line.
    """
    if opened_at is None:
        return ""
    try:
        return opened_at.strftime("%d %b")
    except Exception:                                   # noqa: BLE001
        return ""


def context_line(signal_type, opened_at=None, age_hours=None) -> str:
    """The header's second line: engine, its job, when filed, how long held.

    "LEDGE · Base breakout · filed 02 Sep, held 7d"

    Each part drops out independently when unknown, so the line degrades to
    "LEDGE" rather than to "LEDGE · · filed , held".
    """
    parts = [engine_line(signal_type)]
    when = filed_on(opened_at)
    held = held_for(age_hours) if age_hours is not None else ""
    if when and held:
        parts.append(f"filed {when}, {held}")
    elif when:
        parts.append(f"filed {when}")
    elif held:
        parts.append(held)
    return " · ".join(p for p in parts if p)


def why_block(signal_type, metadata=None, remarks=None, limit=2) -> str:
    """The measured reasons if the row has them, else the engine's rule.

    Returns a block ready to drop into a Telegram message, already labelled
    with the claim it is actually making:

      "What fired it:"   the bar's own numbers, this signal only
      "Engine rule:"     what this engine does on every trade

    Empty string when the row carries neither, because a heading over nothing
    is worse than no heading.
    """
    ws = why_lines(metadata, limit=limit)
    if ws:
        return "*What fired it:*\n" + "\n".join(f"• {w}" for w in ws)
    rule = engine_rule(signal_type, remarks)
    return f"_Engine rule: {rule}_" if rule else ""

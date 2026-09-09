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
    "breakout":        ("BREACH", "Breakouts",        "Daily → weeks",        None),
    "magic":           ("TIDAL",  "Recovery",         "Weekly → months",      ">15% off the high"),
    "magicmagic":      ("TIDAL",  "Recovery",         "Weekly → months",      "20–40% off the high"),
    "equity_measured": ("PLUMB",  "Measured equity",  "Daily → days",         None),
    "multibagger":     ("ASCENT", "Leaders",          "Weekly → 6–12 months", None),
    "momentum_quant":  ("VECTOR", "Momentum",         "Monthly → months",     None),
    "ai_longterm":     ("NORTH",  "Long horizon",     "Weekly → months",      None),
    "ledge":           ("LEDGE",  "Base breakout",    "Daily → weeks",        "≥12% off the high"),
    "keel":            ("KEEL",   "Divergence turn",  "Daily → weeks",        "≥12% off the high"),
    # Research floor — measured, published, NOT cleared to file signals. Named
    # here so an alert that somehow carries one is still legible rather than
    # printing a raw key; the site keeps them off /signals and /engines.
    "buoy":            ("BUOY",    "Reclaim",         "4-hour → days",        None),
    "anchor":          ("ANCHOR",  "Floor reversal",  "Daily → weeks",        None),
    "bedrock":         ("BEDROCK", "Bottom reversal", "Daily → weeks",        None),
}

# Engines that exist in the ledger but are not on the published floor. They are
# named rather than hidden: a row from one of them must still read as something
# rather than as a key, and calling it "not a published engine" is the honest
# label. Mirrors the "keys in the ledger that are not on this floor" block the
# site already renders on /engines.
LEDGER_ONLY = {
    "cf_1h":          "Commodity 1h channel",
    "commodity":      "Commodity scan",
    "ohl":            "Open-High-Low intraday",
    "intraday":       "Intraday scan",
    "4h":             "4-hour scan",
    "ai_4h":          "AI 4-hour",
    "ai_daily":       "AI daily",
    "top5_pick":      "Weekly Top 5",
    "sip_bucket":     "SIP allocation",
    "basebreak":      "Base break",
}


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


def is_published_engine(signal_type) -> bool:
    """True for the engines the site lists as publishing.

    The research three are deliberately NOT in this set: they are measured and
    shown, and nothing they produce is filed as a signal.
    """
    k = str(signal_type or "").strip()
    return k in ENGINE_NAMES and k not in RESEARCH_ONLY


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
    name = engine_name(signal_type)
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

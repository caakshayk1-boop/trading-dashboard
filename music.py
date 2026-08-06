#!/usr/bin/env python3
"""
music.py — the record shelf.

Three crates:

    SONGS    yours. anything, no genre policing.
    BHAKTI   yours.
    GLOBAL   all-time greats, English and Hindi, plus remixes and live
             performances. Not yours — a fixed canon that rotates.

Each crate shows five and reveals the rest on click. The five shown ROTATE
DAILY on the ordinal scheme the rest of the site uses, so the shelf is
different every morning without the list changing.

To add a song, append a tuple to the right list:

    ("Title", "Artist")                     → search link
    ("Title", "Artist", "VIDEO_ID")         → opens that exact video

VIDEO_ID is the part after `v=` in a YouTube URL. Pinning matters more than it
looks: the shelf now plays the track in an embedded player on the page, and an
embed needs a real id — an unpinned track can only fall back to opening a
YouTube search.

Run `python3 music.py --check` after editing.

It asks YouTube's oEmbed endpoint about every id and reports three failures a
human reading this file cannot see:

    DEAD    the id 404s — the video was removed, or a character is wrong
    WRONG   the id resolves, but to a different song
    DUP     two entries share one id, so one of them is lying

All three were live here on 2026-08-06: 24 of 40 ids were dead (one was a
single-character typo), "Kal Ho Naa Ho" pointed at a talk about AI music,
"Mitwa — Unplugged" at an Adele track, and "Bohemian Rhapsody — Live Aid"
carried Billie Jean's id. None of it was visible until the player tried to play
them, because a wrong id and a right id look identical in this file.
"""

from __future__ import annotations

import urllib.parse
from datetime import date

# ── Anything ────────────────────────────────────────────────────────────────
SONGS: list[tuple] = [
    ("Kabira", "Arijit Singh, Tochi Raina", "jHNNMj5bNQw"),
    ("Tum Hi Ho", "Arijit Singh", "Umqb9KENgmk"),
    ("Iktara", "Kavita Seth, Amitabh Bhattacharya", "ZlOZktsODpA"),
    ("Ilahi", "Arijit Singh", "fdubeMFwuGs"),
    ("Phir Le Aya Dil", "Arijit Singh", "R4YeD7aoOmU"),
    ("Tera Ban Jaunga", "Akhil Sachdeva, Tulsi Kumar", "Qdz5n1Xe5Qo"),
    ("Agar Tum Saath Ho", "Alka Yagnik, Arijit Singh", "xRb8hxwN5zc"),
    ("Channa Mereya", "Arijit Singh", "284Ov7ysmfA"),
    ("Ae Dil Hai Mushkil", "Arijit Singh", "6FURuLYrR_Q"),
    ("Tujhe Kitna Chahne Lage", "Arijit Singh", "AgX2II9si7w"),
]

# ── Bhakti ──────────────────────────────────────────────────────────────────
BHAKTI: list[tuple] = [
    ("Achyutam Keshavam", "Vikram Hazra", "QyMZxGlXulY"),
    ("Hanuman Chalisa", "Hariharan", "AETFvQonfV8"),
    ("Shiv Tandav Stotram", "Shankar Mahadevan", "S980-z1qx3g"),
    ("Vaishnav Jan To", "Lata Mangeshkar", "ri7IPwgqE34"),
    ("Om Jai Jagdish Hare", "Anuradha Paudwal", "3ucCEjXS9n8"),
    ("Gayatri Mantra", "Anuradha Paudwal", "nwRoHC83wx0"),
    ("Krishna Govind Govind Gopal", "Jubin Nautiyal", "1qmPNot9NJs"),
    ("Mere Gharib Nawaz", "Rahat Fateh Ali Khan"),
    ("Shree Ram Chandra Kripalu", "Jagjit Singh", "Eiw-BtnlmT4"),
    ("Deva Shree Ganesha", "Ajay Gogavale", "RYqJ5w-GrfM"),
]

# ── Global · all time ───────────────────────────────────────────────────────
# English classics, Hindi classics, and the live/remix cuts that earned their
# own place. Deliberately not a chart snapshot — charts date, canon does not.
GLOBAL: list[tuple] = [
    # English — the ones that survived
    ("Bohemian Rhapsody", "Queen", "fJ9rUzIMcZQ"),
    ("Hotel California", "Eagles", "09839DpTctU"),
    ("Billie Jean", "Michael Jackson", "Zi_XLOBDo_Y"),
    ("Comfortably Numb", "Pink Floyd", "_FrOQC-zEog"),
    ("Stairway to Heaven", "Led Zeppelin", "QkF3oxziUI4"),
    ("Imagine", "John Lennon", "YkgkThdzX-8"),
    ("Smells Like Teen Spirit", "Nirvana", "hTWKbfoikeg"),
    ("Wonderwall", "Oasis", "bx1Bh8ZvH84"),
    # Hindi — the ones that survived
    ("Lag Ja Gale", "Lata Mangeshkar", "br6C4U3Dyfo"),
    ("Kal Ho Naa Ho", "Sonu Nigam", "g0eO74UmRBs"),
    ("Tere Bina Zindagi Se", "Lata Mangeshkar, Kishore Kumar", "thUliYpZQxk"),
    ("Ae Mere Watan Ke Logo", "Lata Mangeshkar", "Wvr8sX5-T_8"),
    ("Chaiyya Chaiyya", "Sukhwinder Singh, Sapna Awasthi", "lZLxjLYyhYQ"),
    ("Kun Faya Kun", "A.R. Rahman, Javed Ali, Mohit Chauhan", "T94PHkuydcw"),
    # Live and remixed — performances that beat the studio cut
    ("Afreen Afreen — Coke Studio", "Rahat Fateh Ali Khan, Momina Mustehsan", "kw4tT7SCmaY"),
    ("Tajdar-e-Haram — Coke Studio", "Atif Aslam", "a18py61_F_w"),
    ("Ranjish Hi Sahi — Live", "Mehdi Hassan", "vzog7FYnKt8"),
    ("Bohemian Rhapsody — Live Aid 1985", "Queen", "vbvyNnw8Qjg"),
    ("Sandese Aate Hain", "Sonu Nigam, Roop Kumar Rathod", "qp0Y-CUKGes"),
    ("Mitwa — Unplugged", "Shafqat Amanat Ali", "ru_5PA8cwkE"),
]

TOP_N = 5          # shown by default; the rest sit behind "show all"


def _entry(row: tuple) -> dict:
    title = row[0]
    artist = row[1] if len(row) > 1 else ""
    vid = row[2] if len(row) > 2 else None
    if vid:
        # Direct play. A search URL opens a results page and costs another
        # click, which defeats the point of a one-tap shelf.
        url = f"https://www.youtube.com/watch?v={vid}"
    else:
        q = urllib.parse.quote_plus(f"{title} {artist}".strip())
        url = f"https://www.youtube.com/results?search_query={q}"
    # vid is carried through, not just folded into the URL. The shelf plays
    # tracks in an embedded player on the page now, and an embed needs the bare
    # id — parsing it back out of the watch URL in the template would be
    # re-deriving something we already had.
    return {"title": title, "artist": artist, "url": url,
            "vid": vid or "", "pinned": bool(vid)}


def _rotate(rows: list, on: date | None = None) -> list:
    """Same list, different five on top each day.

    The shelf never changes but what greets you does. Ordinal-based, matching
    every other rotating bank on the site, so the whole page turns over
    together rather than one section drifting on its own clock.
    """
    if not rows:
        return rows
    n = (on or date.today()).toordinal() % len(rows)
    return rows[n:] + rows[:n]


def library(on: date | None = None) -> dict:
    songs = [_entry(r) for r in _rotate(SONGS, on)]
    bhakti = [_entry(r) for r in _rotate(BHAKTI, on)]
    glob = [_entry(r) for r in _rotate(GLOBAL, on)]
    return {
        "songs": songs,
        "bhakti": bhakti,
        "global": glob,
        "top_n": TOP_N,
        "total": len(songs) + len(bhakti) + len(glob),
        "pinned": sum(1 for c in (songs, bhakti, glob) for t in c if t["pinned"]),
    }


def check() -> int:
    """Verify every pinned id against YouTube. Returns an exit code.

    Deliberately NOT wired into the 6 AM build: it is ~40 requests to a third
    party, and a YouTube rate-limit on the Actions runner must never be the
    reason the newspaper fails to publish. Run it by hand after editing the
    lists — that is when ids actually change.
    """
    import collections, json, re, ssl, unicodedata, urllib.request

    ctx = ssl.create_default_context()

    def oembed(vid):
        u = ("https://www.youtube.com/oembed?url="
             + urllib.parse.quote(f"https://www.youtube.com/watch?v={vid}", safe="")
             + "&format=json")
        try:
            with urllib.request.urlopen(u, timeout=12, context=ctx) as r:
                return json.load(r)
        except Exception:
            return None

    def norm(s):
        return re.sub(r"[^a-z0-9 ]", " ", unicodedata.normalize("NFKD", s).lower())

    rows = [(name, r[0], r[2] if len(r) > 2 else None)
            for lst, name in ((SONGS, "songs"), (BHAKTI, "bhakti"), (GLOBAL, "global"))
            for r in lst]

    bad = 0
    ids = [v for _, _, v in rows if v]
    for vid, n in collections.Counter(ids).items():
        if n > 1:
            bad += 1
            names = [t for _, t, v in rows if v == vid]
            print(f"DUP    {vid} shared by {names}")

    for name, title, vid in rows:
        if not vid:
            print(f"note   {name:7} {title} — search link, cannot play in page")
            continue
        d = oembed(vid)
        if d is None:
            bad += 1
            print(f"DEAD   {name:7} {title} [{vid}]")
            continue
        # Compare against the part before an em-dash: "Afreen Afreen — Coke
        # Studio" should match on the song, not on the series name.
        key = [w for w in norm(title.split("—")[0]).split() if len(w) > 2]
        if key and sum(1 for w in key if w in norm(d["title"])) / len(key) < 0.5:
            bad += 1
            print(f"WRONG  {name:7} {title} [{vid}] resolves to {d['title'][:50]!r}")

    print(f"\n{len(rows)} tracks · {len(ids)} pinned · {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        raise SystemExit(check())

    lib = library()
    for name in ("songs", "bhakti", "global"):
        print(f"\n{name.upper()} ({len(lib[name])}) — top {lib['top_n']} today")
        for i, s in enumerate(lib[name], 1):
            mark = "▶" if s["pinned"] else "?"
            head = "  " if i <= lib["top_n"] else "  ·"
            print(f"{head}{i:>2}. {mark} {s['title']:<38} {s['artist'][:30]}")
    print(f"\ntotal {lib['total']} · {lib['pinned']} direct-play links")

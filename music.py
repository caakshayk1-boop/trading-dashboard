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

    ("Title", "Artist")                              → search link only
    ("Title", "Artist", "YT_ID")                     → plays on the page
    ("Title", "Artist", "YT_ID", "ALBUM_ID/TRACK_ID") → plays, plus an Apple link

YT_ID is what makes a track playable — the part after `v=` in a YouTube URL.
YouTube is the player because it is the only source that is free, needs no
account, and carries this catalogue at full length. Apple Music was tried and
reverted: signed out its widget plays 30 seconds, and full playback needs a
subscription.

The fourth field is optional and is only a LINK for anyone who does subscribe.
It is a pair, because Apple addresses a track by album with `?i=`; a bare
`/song/<id>` URL answers 200 and renders an empty placeholder.

Both ids for a song, in one command:

    curl -s 'https://itunes.apple.com/search?term=SONG+ARTIST&entity=song&limit=3&country=my' \
      | python3 -m json.tool | grep -E 'collectionId|trackId|trackName|artistName'

Run `python3 music.py --check` after editing.

It asks Apple whether each id still exists on this storefront, then asks
YouTube's oEmbed endpoint about every video id and reports the failures a human
reading this file cannot see:

    APPLE   the album/track pair is not on the storefront — it will not play

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

import random
import urllib.parse
from datetime import date

# ── Anything ────────────────────────────────────────────────────────────────
SONGS: list[tuple] = [
    ("Kabira", "Arijit Singh, Tochi Raina", "jHNNMj5bNQw", "1070912669/1070912834"),
    ("Tum Hi Ho", "Arijit Singh", "Umqb9KENgmk", "1073359412/1073359419"),
    ("Iktara", "Kavita Seth, Amitabh Bhattacharya", "ZlOZktsODpA", "327458432/327458459"),
    ("Ilahi", "Arijit Singh", "fdubeMFwuGs", "1070912669/1070912815"),
    ("Phir Le Aya Dil", "Arijit Singh", "R4YeD7aoOmU", "1179378486/1179378861"),
    ("Tera Ban Jaunga", "Akhil Sachdeva, Tulsi Kumar", "Qdz5n1Xe5Qo", "1468448737/1468448744"),
    ("Agar Tum Saath Ho", "Alka Yagnik, Arijit Singh", "xRb8hxwN5zc", "1455982272/1455982281"),
    ("Channa Mereya", "Arijit Singh", "284Ov7ysmfA", "1169015635/1169015785"),
    ("Ae Dil Hai Mushkil", "Arijit Singh", "6FURuLYrR_Q", "1169015635/1169015776"),
    ("Tujhe Kitna Chahne Lage", "Arijit Singh", "AgX2II9si7w", "1468448737/1468448742"),
]

# ── Bhakti ──────────────────────────────────────────────────────────────────
BHAKTI: list[tuple] = [
    ("Achyutam Keshavam", "Vikram Hazra", "QyMZxGlXulY", "1549681883/1549682352"),
    ("Hanuman Chalisa", "Hariharan", "AETFvQonfV8", "1597935332/1597935470"),
    ("Shiv Tandav Stotram", "Shankar Mahadevan", "S980-z1qx3g", "1086564371/1086564393"),
    ("Vaishnav Jan To", "Lata Mangeshkar", "ri7IPwgqE34", "1533997028/1533997029"),
    ("Om Jai Jagdish Hare", "Anuradha Paudwal", "3ucCEjXS9n8", "1197839175/1197839249"),
    ("Gayatri Mantra", "Anuradha Paudwal", "nwRoHC83wx0", "730850559/730850604"),
    ("Krishna Govind Govind Gopal", "Jubin Nautiyal", "1qmPNot9NJs", "1525864229/1525864230"),
    ("Mere Gharib Nawaz", "Rahat Fateh Ali Khan"),
    ("Shree Ram Chandra Kripalu", "Jagjit Singh", "Eiw-BtnlmT4", "1819531083/1819531086"),
    ("Deva Shree Ganesha", "Ajay Gogavale", "RYqJ5w-GrfM", "1435887894/1435888021"),
]

# ── Global · all time ───────────────────────────────────────────────────────
# English classics, Hindi classics, and the live/remix cuts that earned their
# own place. Deliberately not a chart snapshot — charts date, canon does not.
GLOBAL: list[tuple] = [
    # English — the ones that survived
    ("Bohemian Rhapsody", "Queen", "fJ9rUzIMcZQ", "6781027361/6781027645"),
    ("Hotel California", "Eagles", "09839DpTctU", "635770200/635770202"),
    ("Billie Jean", "Michael Jackson", "Zi_XLOBDo_Y", "273598907/273598914"),
    ("Comfortably Numb", "Pink Floyd", "_FrOQC-zEog", "1065975633/1065976170"),
    ("Stairway to Heaven", "Led Zeppelin", "QkF3oxziUI4", "580708175/580708180"),
    ("Imagine", "John Lennon", "YkgkThdzX-8", "1436922341/1436922357"),
    ("Smells Like Teen Spirit", "Nirvana", "hTWKbfoikeg"),
    ("Wonderwall", "Oasis", "bx1Bh8ZvH84", "537714887/537715121"),
    # Hindi — the ones that survived
    ("Lag Ja Gale", "Lata Mangeshkar", "br6C4U3Dyfo", "1355010914/1355010915"),
    ("Kal Ho Naa Ho", "Sonu Nigam", "g0eO74UmRBs", "300388644/300388792"),
    ("Tere Bina Zindagi Se", "Lata Mangeshkar, Kishore Kumar", "thUliYpZQxk", "1337281139/1337283476"),
    ("Ae Mere Watan Ke Logo", "Lata Mangeshkar", "Wvr8sX5-T_8", "1791061171/1791061176"),
    ("Chaiyya Chaiyya", "Sukhwinder Singh, Sapna Awasthi", "lZLxjLYyhYQ", "1130322055/1130322151"),
    ("Kun Faya Kun", "A.R. Rahman, Javed Ali, Mohit Chauhan", "T94PHkuydcw", "1123241840/1123241921"),
    # Live and remixed — performances that beat the studio cut
    ("Afreen Afreen — Coke Studio", "Rahat Fateh Ali Khan, Momina Mustehsan", "kw4tT7SCmaY", "1150347362/1150347366"),
    ("Tajdar-e-Haram — Coke Studio", "Atif Aslam", "a18py61_F_w", "1798501071/1798501074"),
    ("Ranjish Hi Sahi — Live", "Mehdi Hassan", "vzog7FYnKt8", "1442971448/1442971450"),
    ("Bohemian Rhapsody — Live Aid 1985", "Queen", "vbvyNnw8Qjg", "6781027361/6781027645"),
    ("Sandese Aate Hain", "Sonu Nigam, Roop Kumar Rathod", "qp0Y-CUKGes", "1126255127/1126255391"),
    ("Mitwa — Unplugged", "Shafqat Amanat Ali", "ru_5PA8cwkE", "305752278/305752291"),
]

TOP_N = 5          # shown by default; the rest sit behind "show all"


# Apple Music storefront. "my" because that is where the listener actually is;
# it is not cosmetic, catalogues differ by region. Resolving this shelf against
# the Malaysian storefront matched 38/40, against the Indian one 21/40.
STOREFRONT = "my"


def _entry(row: tuple) -> dict:
    title = row[0]
    artist = row[1] if len(row) > 1 else ""
    vid = row[2] if len(row) > 2 else None
    apple = row[3] if len(row) > 3 else None
    if vid:
        # The fallback link, and the only thing the two unmatched tracks have.
        url = f"https://www.youtube.com/watch?v={vid}"
    else:
        q = urllib.parse.quote_plus(f"{title} {artist}".strip())
        url = f"https://www.youtube.com/results?search_query={q}"
    # What actually plays is the YouTube embed, because it is the only source
    # that is free, needs no account, and carries this whole catalogue at full
    # length. Apple's widget is audio-only and looked like the better fit until
    # the constraint that matters showed up: signed out it plays a 30-second
    # preview, and full playback needs a paid subscription.
    #
    # There is no legal alternative. Every free, no-auth music API (Deezer,
    # TheAudioDB and friends) returns metadata and 30-second previews; the ones
    # that stream in full are licensed catalogues of royalty-free music, which
    # is not the music on this shelf. Unofficial JioSaavn-style endpoints do
    # return full-song CDN links and are deliberately not used here: they break
    # without notice, and re-streaming that audio is infringement.
    embed = f"https://www.youtube-nocookie.com/embed/{vid}" if vid else ""

    # Kept as a secondary link, not the player. Costs nothing and is the better
    # destination for anyone who does subscribe.
    apple_url = ""
    if apple and "/" in apple:
        album, track = apple.split("/", 1)
        apple_url = f"https://music.apple.com/{STOREFRONT}/album/{album}?i={track}"

    return {
        "title": title, "artist": artist, "url": url,
        "vid": vid or "", "pinned": bool(vid),
        "apple": apple or "",
        "embed": embed,
        "apple_url": apple_url,
    }


def _rotate(rows: list, on: date | None = None) -> list:
    """Same list, a genuinely different five on top each day.

    The old version sliced at `ordinal % len` and concatenated, which advances
    the window by exactly ONE position a day. With a 10-track crate that means
    four of the five on display yesterday are still on display today — the
    shelf technically rotated and looked frozen for a week.

    Deterministic shuffle instead: seed a PRNG with the date, shuffle a copy.
    Same date always gives the same order, so the page is stable all day and
    reproducible for any date, but consecutive days share no structure and the
    visible five actually turn over. Every track still appears — this reorders
    the crate, it does not sample from it.
    """
    if not rows:
        return rows
    d = on or date.today()
    out = list(rows)
    # Seeded on the date alone, so all three crates reshuffle together on the
    # same clock as the rest of the site's daily banks.
    random.Random(d.toordinal()).shuffle(out)
    return out


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
        # What can actually play in the dock, which is the YouTube embed —
        # NOT the Apple id, which is only a secondary link now.
        "playable": sum(1 for c in (songs, bhakti, glob) for t in c if t["embed"]),
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

    rows = [(name, r[0], r[2] if len(r) > 2 else None, r[3] if len(r) > 3 else None)
            for lst, name in ((SONGS, "songs"), (BHAKTI, "bhakti"), (GLOBAL, "global"))
            for r in lst]

    # Apple ids first — that is what actually plays on the page now.
    apple_bad = 0
    for name, title, _vid, apple in rows:
        if not apple:
            print(f"note   {name:7} {title} — no Apple id, link-only")
            continue
        if "/" not in apple:
            apple_bad += 1
            print(f"APPLE  {name:7} {title} [{apple}] must be \"ALBUM_ID/TRACK_ID\"")
            continue
        album, track = apple.split("/", 1)
        u = f"https://itunes.apple.com/lookup?id={track}&country={STOREFRONT}"
        try:
            with urllib.request.urlopen(u, timeout=12, context=ctx) as r:
                res = json.load(r).get("results", [])
        except Exception:
            res = []
        if not res:
            apple_bad += 1
            print(f"APPLE  {name:7} {title} [{apple}] not on the '{STOREFRONT}' storefront")
            continue
        # The album id is what the embed URL is actually built from, so a
        # mismatched pair renders the wrong record or nothing at all.
        got = str(res[0].get("collectionId", ""))
        if got and got != album:
            apple_bad += 1
            print(f"APPLE  {name:7} {title} album {album} != {got} for that track")

    bad = apple_bad
    ids = [v for _, _, v, _ in rows if v]
    for vid, n in collections.Counter(ids).items():
        if n > 1:
            bad += 1
            names = [t for _, t, v, _a in rows if v == vid]
            print(f"DUP    {vid} shared by {names}")

    # YouTube ids are now only the fallback link, but a dead link is still a
    # dead link — 24 of them were rotting here unnoticed until 2026-08-06.
    for name, title, vid, _apple in rows:
        if not vid:
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

    # Playable means the YouTube id, because that is what the dock embeds.
    # The Apple pair is only a secondary link.
    print(f"\n{len(rows)} tracks · {len(ids)} playable on the page · "
          f"{sum(1 for _n, _t, _v, a in rows if a)} with an Apple link · "
          f"{bad} problem(s)")
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

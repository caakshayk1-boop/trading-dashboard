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
looks: a search link opens a results page and costs a second click, which for a
one-tap "play this now" shelf is the whole difference. Pin the ones you reach
for.
"""

from __future__ import annotations

import urllib.parse
from datetime import date

# ── Anything ────────────────────────────────────────────────────────────────
SONGS: list[tuple] = [
    ("Kabira", "Arijit Singh, Tochi Raina", "jHNNMj5bNQw"),
    ("Tum Hi Ho", "Arijit Singh", "Umqb9KENgmk"),
    ("Iktara", "Kavita Seth, Amitabh Bhattacharya", "AinsjeM_h9A"),
    ("Ilahi", "Arijit Singh", "fUmHOcaEQ0g"),
    ("Phir Le Aya Dil", "Arijit Singh", "eN0F3zqUqLI"),
    ("Tera Ban Jaunga", "Akhil Sachdeva, Tulsi Kumar", "n2CtGJCFcNc"),
    ("Agar Tum Saath Ho", "Alka Yagnik, Arijit Singh", "xRb8hxwN4zs"),
    ("Channa Mereya", "Arijit Singh", "284Ov7ysmfA"),
    ("Ae Dil Hai Mushkil", "Arijit Singh", "6FURuLYrR_Q"),
    ("Tujhe Kitna Chahne Lage", "Arijit Singh", "ZDc3wnMdMqs"),
]

# ── Bhakti ──────────────────────────────────────────────────────────────────
BHAKTI: list[tuple] = [
    ("Achyutam Keshavam", "Vikram Hazra", "1Wr1ryhU1Gc"),
    ("Hanuman Chalisa", "Hariharan", "AETFvQonfV8"),
    ("Shiv Tandav Stotram", "Shankar Mahadevan", "ZFVzWtQhLmY"),
    ("Vaishnav Jan To", "Lata Mangeshkar", "ZgQTS7Y2Pxo"),
    ("Om Jai Jagdish Hare", "Anuradha Paudwal", "aUXpEUyF8CY"),
    ("Gayatri Mantra", "Anuradha Paudwal", "-_ic6-6ha1c"),
    ("Krishna Govind Govind Gopal", "Jubin Nautiyal", "sxsSKMU2ppY"),
    ("Mere Gharib Nawaz", "Rahat Fateh Ali Khan", "Bfeu5r3XzMA"),
    ("Shree Ram Chandra Kripalu", "Jagjit Singh", "1UBGGYlYlrE"),
    ("Deva Shree Ganesha", "Ajay Gogavale", "3ZAdWpEXn_Q"),
]

# ── Global · all time ───────────────────────────────────────────────────────
# English classics, Hindi classics, and the live/remix cuts that earned their
# own place. Deliberately not a chart snapshot — charts date, canon does not.
GLOBAL: list[tuple] = [
    # English — the ones that survived
    ("Bohemian Rhapsody", "Queen", "fJ9rUzIMcZQ"),
    ("Hotel California", "Eagles", "EqPtz5qN7HM"),
    ("Billie Jean", "Michael Jackson", "Zi_XLOBDo_Y"),
    ("Comfortably Numb", "Pink Floyd", "_FrOQC-zEog"),
    ("Stairway to Heaven", "Led Zeppelin", "QkF3oxziUI4"),
    ("Imagine", "John Lennon", "YkgkThdzX-8"),
    ("Smells Like Teen Spirit", "Nirvana", "hTWKbfoikeg"),
    ("Wonderwall", "Oasis", "bx1Bh8ZvH84"),
    # Hindi — the ones that survived
    ("Lag Ja Gale", "Lata Mangeshkar", "ImTGRfMNqDU"),
    ("Kal Ho Naa Ho", "Sonu Nigam", "wYb3Wimn01s"),
    ("Tere Bina Zindagi Se", "Lata Mangeshkar, Kishore Kumar", "8vFf0kQ6-EQ"),
    ("Ae Mere Watan Ke Logo", "Lata Mangeshkar", "TCPFXZ_1Kzo"),
    ("Chaiyya Chaiyya", "Sukhwinder Singh, Sapna Awasthi", "Ex1gS6yFvvY"),
    ("Kun Faya Kun", "A.R. Rahman, Javed Ali, Mohit Chauhan", "T94PHkuydcw"),
    # Live and remixed — performances that beat the studio cut
    ("Afreen Afreen — Coke Studio", "Rahat Fateh Ali Khan, Momina Mustehsan", "Ss0Wf50Kt7c"),
    ("Tajdar-e-Haram — Coke Studio", "Atif Aslam", "-dQ8m2Vph2A"),
    ("Ranjish Hi Sahi — Live", "Mehdi Hassan", "P-nKfIm-mZ0"),
    ("Bohemian Rhapsody — Live Aid 1985", "Queen", "Zi_XLOBDo_Y"),
    ("Sandese Aate Hain", "Sonu Nigam, Roop Kumar Rathod", "cGDPFTJgxLo"),
    ("Mitwa — Unplugged", "Shafqat Amanat Ali", "hLQl3WQQoQ0"),
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
    return {"title": title, "artist": artist, "url": url, "pinned": bool(vid)}


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


if __name__ == "__main__":
    lib = library()
    for name in ("songs", "bhakti", "global"):
        print(f"\n{name.upper()} ({len(lib[name])}) — top {lib['top_n']} today")
        for i, s in enumerate(lib[name], 1):
            mark = "▶" if s["pinned"] else "?"
            head = "  " if i <= lib["top_n"] else "  ·"
            print(f"{head}{i:>2}. {mark} {s['title']:<38} {s['artist'][:30]}")
    print(f"\ntotal {lib['total']} · {lib['pinned']} direct-play links")

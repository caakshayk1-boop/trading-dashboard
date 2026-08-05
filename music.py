#!/usr/bin/env python3
"""
music.py — the record shelf.

Two crates, side by side: anything, and bhakti. You add a line, the page picks
it up on the next 6 AM build. Nothing here calls an API and nothing needs a
key — a YouTube search URL built from the title and artist resolves to the
right track reliably enough, and a direct link can be pinned when it does not.

To add a song, append a tuple to the right list:

    ("Title", "Artist")                     → search link, good enough usually
    ("Title", "Artist", "VIDEO_ID")         → pinned to one exact video

VIDEO_ID is the part after `v=` in a YouTube URL. Pin it when the search would
be ambiguous — covers, live versions, or a title that is a common word.

Order is the shelf order. The page shows the first five and reveals the rest on
click, so put the ones you actually reach for at the top.
"""

from __future__ import annotations

import urllib.parse

# ── Anything ────────────────────────────────────────────────────────────────
# Films, ghazals, rock, whatever is on. No genre policing.
SONGS: list[tuple] = [
    ("Kabira", "Arijit Singh, Tochi Raina"),
    ("Tum Hi Ho", "Arijit Singh"),
    ("Iktara", "Kavita Seth, Amitabh Bhattacharya"),
    ("Ilahi", "Arijit Singh"),
    ("Phir Le Aya Dil", "Arijit Singh"),
    ("Tera Ban Jaunga", "Akhil Sachdeva, Tulsi Kumar"),
    ("Agar Tum Saath Ho", "Alka Yagnik, Arijit Singh"),
    ("Channa Mereya", "Arijit Singh"),
    ("Ae Dil Hai Mushkil", "Arijit Singh"),
    ("Tujhe Kitna Chahne Lage", "Arijit Singh"),
]

# ── Bhakti ──────────────────────────────────────────────────────────────────
BHAKTI: list[tuple] = [
    ("Achyutam Keshavam", "Vikram Hazra"),
    ("Hanuman Chalisa", "Hariharan"),
    ("Shiv Tandav Stotram", "Shankar Mahadevan"),
    ("Vaishnav Jan To", "Lata Mangeshkar"),
    ("Om Jai Jagdish Hare", "Anuradha Paudwal"),
    ("Gayatri Mantra", "Anuradha Paudwal"),
    ("Krishna Govind Govind Gopal", "Jubin Nautiyal"),
    ("Mere Gharib Nawaz", "Rahat Fateh Ali Khan"),
    ("Shree Ram Chandra Kripalu", "Jagjit Singh"),
    ("Deva Shree Ganesha", "Ajay Gogavale"),
]

TOP_N = 5          # shown by default; the rest sit behind "show all"


def _entry(row: tuple) -> dict:
    title = row[0]
    artist = row[1] if len(row) > 1 else ""
    vid = row[2] if len(row) > 2 else None
    if vid:
        url = f"https://www.youtube.com/watch?v={vid}"
    else:
        q = urllib.parse.quote_plus(f"{title} {artist}".strip())
        url = f"https://www.youtube.com/results?search_query={q}"
    return {"title": title, "artist": artist, "url": url, "pinned": bool(vid)}


def library() -> dict:
    """Both crates, shaped for the template."""
    return {
        "songs": [_entry(r) for r in SONGS],
        "bhakti": [_entry(r) for r in BHAKTI],
        "top_n": TOP_N,
        "total": len(SONGS) + len(BHAKTI),
    }


if __name__ == "__main__":
    lib = library()
    for name in ("songs", "bhakti"):
        print(f"\n{name.upper()} ({len(lib[name])})")
        for i, s in enumerate(lib[name], 1):
            mark = "pinned" if s["pinned"] else "search"
            print(f"  {i:>2}. {s['title']:<34} {s['artist']:<28} [{mark}]")
    print(f"\ntotal {lib['total']} · showing top {lib['top_n']} of each")

#!/usr/bin/env python3
"""Latin + latin-ext woff2 subsets for the site's three families.

WHY THIS EXISTS. The eight files in docs/fonts/ are CYRILLIC subsets — every
@font-face carried unicode-range U+0400-045F — so no Latin glyph and no digit
has ever rendered in Fira Sans or JetBrains Mono on this site. Verified against
production in-browser: `'Fira Sans', monospace` measured identically to plain
`monospace` for "Numbers first 24078". The three JetBrainsMono files were also
byte-identical, so weights 500 and 700 were copies of 400.

TWO THINGS THAT COST BYTES IF YOU GET THEM WRONG.

Axes must be PINNED. Newsreader and JetBrains Mono are variable fonts, and an
unpinned css2 request hands back the whole variable file once per weight you
ask for — the same 132KB of Newsreader three times over. Pinning opsz and wght
returns a static instance instead: 24KB.

latin-ext is not optional. The rupee sign is U+20B9, which the `latin` subset
does not contain, and this page is made of rupee figures.
"""
import pathlib
import re
import sys
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
OUT = pathlib.Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)

# (css2 query, family, file slug, weight, style)
REQ = []
for w in (400, 500, 600, 700, 800):
    REQ.append((f"Fira+Sans:wght@{w}", "Fira Sans", "FiraSans", w, "normal"))
for w in (400, 500, 700):
    REQ.append((f"JetBrains+Mono:wght@{w}", "JetBrains Mono", "JetBrainsMono", w, "normal"))
# opsz pinned at 36: this face is only ever used at display sizes.
for w in (400, 600):
    REQ.append((f"Newsreader:opsz,wght@36,{w}", "Newsreader", "Newsreader", w, "normal"))
REQ.append(("Newsreader:ital,opsz,wght@1,36,400", "Newsreader", "Newsreader", 400, "italic"))


def get(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as f:
        return f.read() if binary else f.read().decode()


faces, seen = [], {}
for query, family, slug, weight, style in REQ:
    css = get(f"https://fonts.googleapis.com/css2?family={query}&display=swap")
    for m in re.finditer(r"/\*\s*(\S+)\s*\*/\s*@font-face\s*\{(.*?)\}", css, re.S):
        subset, body = m.group(1), m.group(2)
        if subset not in ("latin", "latin-ext"):
            continue
        url = re.search(r"url\((https://[^)]+\.woff2)\)", body).group(1)
        rng = re.search(r"unicode-range:\s*([^;]+);", body).group(1).strip()
        sfx = "i" if style == "italic" else ""
        name = f"{slug}-{weight}{sfx}-{subset}.woff2"
        if url not in seen:
            seen[url] = get(url, binary=True)
        (OUT / name).write_bytes(seen[url])
        faces.append((family, style, weight, name, rng, len(seen[url])))

for f in sorted(faces, key=lambda f: f[3]):
    print(f"  {f[3]:<34} {f[5]:>7,}B")
print(f"\n{len(faces)} files · {sum(f[5] for f in faces) / 1024:.0f}KB total")

lines = [f"@font-face{{font-family:'{fam}';font-style:{st};font-weight:{wt};"
         f"font-display:swap;src:url('/fonts/{nm}') format('woff2');"
         f"unicode-range:{rg}}}"
         for fam, st, wt, nm, rg, _ in
         sorted(faces, key=lambda f: (f[0], f[1], int(f[2]), f[3]))]
(OUT / "_faces.css").write_text("\n".join(lines) + "\n")
print(f"wrote _faces.css ({len(lines)} rules)")

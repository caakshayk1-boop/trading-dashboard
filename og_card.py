#!/usr/bin/env python3
"""
og_card.py — renders the 1200x630 social card for The Daily Signal.

Why this exists
---------------
Sharing the site produced a bare URL: no title, no image, no description. That
removes every reason to paste the link, which is the cheapest distribution a
publication has. A generic logo card would fix the mechanics and say nothing;
this one puts the day's actual numbers on it — win rate, signals logged, open
setups — so the card itself is the argument.

Rendered once per build by generate.py, written to docs/og.png, and served
static. No runtime cost, no image service, no new dependency beyond Pillow.
"""

from __future__ import annotations

import logging
import pathlib

log = logging.getLogger(__name__)

W, H = 1200, 630
BG = (8, 9, 10)
SURFACE = (18, 19, 22)
LIME = (184, 239, 67)
TEXT = (240, 240, 240)
MUTED = (154, 161, 171)
DIM = (123, 131, 144)
UP = (61, 220, 151)
LINE = (32, 34, 38)

# Ubuntu runners ship DejaVu; macOS has the others. Checked in order, and the
# card degrades to Pillow's bitmap font rather than failing the build.
_SANS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]
_SANS_REG = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
]
_MONO = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
]


def _font(paths, size):
    from PIL import ImageFont
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def render(out_path: str, date_str: str, win_rate, signals, open_setups,
           advancing: str = "") -> bool:
    """Write the card. Returns False rather than raising — a missing social
    image must never fail the daily build."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        log.warning("og_card: Pillow not installed, skipping social card")
        return False

    try:
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)

        # Faint vertical rule grid, echoing the site's column lines.
        for x in range(0, W, 150):
            d.line([(x, 0), (x, H)], fill=LINE, width=1)

        f_brand = _font(_SANS, 30)
        f_head = _font(_SANS, 86)
        f_sub = _font(_SANS_REG, 27)
        f_num = _font(_MONO, 62)
        f_lab = _font(_SANS_REG, 20)
        f_foot = _font(_MONO, 22)

        # Masthead
        d.ellipse([72, 68, 88, 84], fill=LIME)
        d.text((104, 62), "THE DAILY", font=f_brand, fill=TEXT)
        bw = d.textlength("THE DAILY ", font=f_brand)
        d.text((104 + bw, 62), "SIGNAL", font=f_brand, fill=LIME)
        d.text((W - 72, 68), date_str.upper(), font=f_lab, fill=DIM, anchor="ra")

        # The claim
        d.text((72, 168), "Numbers first.", font=f_head, fill=TEXT)
        d.text((72, 262), "Noise last.", font=f_head, fill=LIME)

        d.text((72, 382),
               "A public NSE trading ledger. Every signal scored when it closes —",
               font=f_sub, fill=MUTED)
        d.text((72, 418), "wins and losses both.", font=f_sub, fill=MUTED)

        # The proof: real numbers from the build that produced this card.
        d.rectangle([0, 480, W, H], fill=SURFACE)
        d.line([(0, 480), (W, 480)], fill=LINE, width=1)

        cells = [
            (f"{win_rate}%" if win_rate not in (None, "") else "—", "SIGNAL WIN RATE", LIME),
            (str(signals or 0), "SIGNALS LOGGED", TEXT),
            (str(open_setups or 0), "OPEN SETUPS", (106, 168, 255)),
            (advancing or "—", "MARKETS ADVANCING", UP),
        ]
        cw = W // len(cells)
        for i, (val, lab, col) in enumerate(cells):
            x = 72 + i * cw
            if i:
                d.line([(x - 40, 512), (x - 40, 598)], fill=LINE, width=1)
            d.text((x, 512), val, font=f_num, fill=col)
            d.text((x, 584), lab, font=f_lab, fill=DIM)

        pathlib.Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, "PNG", optimize=True)
        return True
    except Exception as e:
        log.warning(f"og_card: render failed — {e}")
        return False


if __name__ == "__main__":
    ok = render("docs/og.png", "Wednesday, August 05 2026",
                35.3, 582, 38, "27/46")
    print("wrote docs/og.png" if ok else "render failed")

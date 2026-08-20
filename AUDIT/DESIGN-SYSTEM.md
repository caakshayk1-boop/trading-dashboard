# AskAkshay design system

One document rather than the seven the brief lists. Seven files describing one
token block would be six files that go stale — the audit content is here, split
by heading.

**Restore point: `v1.0-pre-4pillar`.** Everything below is additive to it.

---

## 1. Visual audit — what was there

| Area | Before | Problem |
|---|---|---|
| Surfaces | 4 (`--bg`, `--bg2`, `--surface`, `--surface2`) | A card inside a panel inside a modal all rendered at the same depth. Nothing to climb. |
| Themes | Dark only | No light mode at all. |
| Type sizes | **30 distinct hardcoded px values** | Each one a decision nobody made. `10.5px`, `9.5px`, `12.5px`, `13.5px` all coexisted. |
| Numbers | Proportional figures | Prices wobbled digit-by-digit down a column. |
| Spacing | Ad-hoc px | No scale. |
| Motion | Per-component durations | No shared timing. |

## 2. Colour

Two themes, both designed. **Light is not an inversion** — a palette tuned for
glowing text on a dark ground has the wrong contrast curve on paper and reads
as washed-out grey.

### Surfaces — five steps, each a small tonal lift

```
--bg        page ground
--bg2       section band
--surface   card
--surface2  card, raised
--surface3  elevated / hover
--overlay   modal, drawer, palette
```

### Ink, measured against `--bg` with full alpha compositing

| Token | Dark | Light | Use |
|---|---:|---:|---|
| `--text` | 16.1:1 | 15.8:1 | body, headings |
| `--muted` | 7.4:1 | 6.1:1 | secondary |
| `--dim` | 4.9:1 | 4.6:1 | timestamps, table meta |

Measured against `--bg2` (#F5F4F0 light), the **darkest** surface these three
regularly print on — not against `--bg`.

That distinction was wrong before. `--dim` was tuned to 4.6:1 against the page
ground and shipped at **4.23:1** on a section band, which is where it actually
appears: table headers, provenance strips, card metadata. A contrast figure
quoted against the lightest possible ground is a figure that is never true
where the text is. Light `--dim` is now `#656C74`.

Verified in-browser by compositing every ancestor background with its alpha and
measuring the real ratio. Worst case across the sampled surfaces: **4.84 dark,
4.71 light**. Both clear AA.

### Surfaces that were not tokens

Nine colours were hardcoded hex from before the light theme existed and stayed
hardcoded after it. Each was a near-white or near-black, so each was legible in
exactly one theme:

| Was | Where | Now |
|---|---|---|
| `#0E0F12` | far end of the `.pick` card gradient | `--pick-edge` |
| `#0E0F12` | sticky table header | `--bg2` |
| `#E6EAF0` | `.sym` — every ticker symbol on the site | `--text` |
| `#C4CAD2` ×5 | `.essay` / `.mind` / `.way` body copy | `--muted` |
| `#DDE2E8` | chess opening line | `--text` |
| `#F2E4C0` | chess best-move SAN | `--gold` |
| `#06251A` | ink on a `--up` fill | `--on-up` |
| `#232529` | scrollbar thumb | `--scroll-thumb` |
| `rgba(255,255,255,.035)` | the ghosted 01–05 pick numeral | `--rank-ink` |
| `rgba(184,239,67,.11)` / `rgba(106,168,255,.07)` | hero orbs | `--orb-a` / `--orb-b` |

The worst of these was the Trade Ideas grid: five cards on a near-black
gradient with dark ink on top, **unreadable in light mode**. The orbs were the
subtler one — a translucent glow is an *additive* device, so the same fill that
reads as light on a dark ground reads as a smudge on paper.

A guard script now walks the stylesheet and fails on any hex whose relative
luminance is above 0.72 or below 0.20 outside a token declaration.

### Accent

The lime stays. It **is** the identity, and a rebrand that discards the one
memorable thing about a product is a redesign, not a rebrand. Everything
around it changed.

In light mode the hue is preserved and the luminance is not: `#C2F04A` on
white is **1.6:1** — a signature colour that becomes unreadable is a branding
failure. Light mode uses `#5C7A0B`.

### Semantic colour

`--up` `--down` `--gold` carry financial meaning only. Never a button, never
decoration. **Colour is never the sole carrier of state** — every status has a
word and a glyph beside it.

## 3. Typography

### The fonts were never loading

Found 2026-08-20, and it invalidates every typographic claim made below it
before that date.

All eight woff2 files in `docs/fonts/` were **Cyrillic subsets**. Every
`@font-face` carried `unicode-range: U+0301, U+0400-045F, U+0490-0491,
U+04B0-04B1, U+2116` — so the browser was instructed to use Fira Sans and
JetBrains Mono *only for Cyrillic*, on a site that contains none. Every Latin
letter and every digit fell through to the system font.

Confirmed against production, not inferred: measuring the string
`Numbers first 24078` set in `'Fira Sans', monospace` gave **exactly** the same
width as plain `monospace` — the declared family covered none of it. Same for
JetBrains Mono, which `var(--mono)` uses in 248 places.

The three `JetBrainsMono-*.woff2` files were also byte-identical, so weights
500 and 700 were copies of 400.

Net effect: the site rendered in San Francisco on a Mac, Segoe UI on Windows,
Roboto on Android. The "designed" typography was whatever the reader's OS had.
The two `<link rel=preload>` tags pointed at two of these files, so both
preloads 404'd as well.

**Fixed.** Latin + latin-ext static subsets for all three families, 22 faces.
`latin-ext` is not optional — the rupee sign is U+20B9, outside the `latin`
subset, and this page is made of rupee figures. Regenerate with
`tools_fetch_fonts.py`; pin the variable axes or Google returns the whole
132KB variable file once per weight requested.

Actual transfer on a first load of `/`: **290KB across 15 files.** `unicode-range`
means a subset is fetched only when the page contains a glyph from that range at
that weight, so Newsreader 400 roman ships but is never requested.

### Three families, three jobs

| Face | Carries | Never carries |
|---|---|---|
| **Newsreader** (serif) | hero, section headlines, the world lead | numbers, labels, tables |
| **Fira Sans** | body copy, card readings | prices |
| **JetBrains Mono** | every number, label, nav item, table | prose |

The serif is pinned at optical size 36 — it is only ever used at display sizes,
so one optical cut is the correct one. The hero's `<em>` was forced upright
because the old stack had no italic worth falling back to; Newsreader ships one,
and roman-against-italic is most of the reason to set a headline in a serif at
all.

Display tracking was retuned from `-3px` to `-1.4px`: a serif's serifs already
close the gaps between letters, so the negative tracking that tightens Fira Sans
collides Newsreader.


Fourteen named steps replacing thirty ad-hoc sizes:

```
--t-display  --t-h1  --t-h2  --t-h3  --t-h4
--t-body-lg  --t-body  --t-body-sm  --t-caption  --t-label  --t-overline
--t-data-lg  --t-data  --t-data-sm
```

Numbers have their **own ramp** — a price and a paragraph should never share a
size by accident.

`font-variant-numeric: tabular-nums` is applied to every numeric surface
(`.num`, tables, KPI values), so digits align vertically down a column.

**Migration status:** the scale exists and new components use it. The 30 legacy
hardcoded sizes are not yet all migrated — that is mechanical work across 5,200
lines and is listed as remaining rather than claimed as done.

## 4. Spacing

`--s1` 4 · `--s2` 8 · `--s3` 12 · `--s4` 16 · `--s5` 20 · `--s6` 24 · `--s7` 32
· `--s8` 40 · `--s9` 48 · `--s10` 64 · `--s11` 80

`--measure: 68ch` — editorial text never spans a dashboard's full width.

## 5. Motion

```
--m-micro  130ms   hover, icon, toggle
--m-std    200ms   surface, theme cross-fade
--m-panel  280ms   drawer, modal
```

Only surfaces and ink cross-fade on a theme switch. Animating more makes the
whole page appear to move.

`prefers-reduced-motion: reduce` collapses every animation and transition to
0.01ms globally.

## 6. Theme switching

Three states — **light → dark → system** — cycled from one header control.
Two states cannot express "follow my OS", which is where most readers actually
are.

The stored choice is applied by an inline, synchronous script in `<head>`,
before first paint. Deferring it by one frame makes the page paint dark and
repaint light, which is the most visible way a theme toggle can look broken.

`:root:not([data-theme])` scopes the `prefers-color-scheme` block, so an
explicit choice always beats the OS.

The control's `aria-label` carries the current state — a screen reader cannot
see the glyph.

---

## 7. What is NOT done

Named honestly. None are regressions; all are remaining work:

| Item | State |
|---|---|
| Migrating 30 legacy font sizes onto the scale | Scale exists; components not yet migrated |
| Card taxonomy (Insight / Signal / Metric / Event…) | One `.card` still does most jobs |
| Skeleton loading states | Spinners and text still used |
| Icon system | Emoji still used in places |
| Command palette redesign | Functional, not restyled |
| Page transitions | Not implemented (single-document site) |
| Density modes (compact/comfortable/reading) | Not implemented |

The token layer is the foundation those depend on, which is why it came first:
every one of them is cheaper to build now than it was before.


---

## 8. What changed on 2026-08-20

**The interpretation layer.** `what_matters()` reads the day's regime, board,
flows, ideas and ledger and returns one to five tagged cards — Risk, Momentum,
Watch, Opportunity, Record — each with the section that proves it. It is Python,
not a model call: the same build always produces the same reading, and every
card traces to its inputs. A card is emitted only when its trigger fires, so a
quiet morning gets fewer cards rather than an invented one.

**The lead idea.** Rank 01 in Trade Ideas now occupies a 2×2 block with its
score breakdown open by default. Same markup and same fields as the other four
— nothing is summarised away — at a size that matches what the score already
claims. Gated on five ideas and ≥1080px; below either, the grid stays uniform.

**Light mode is now actually usable.** See §2.

**The fonts now load.** All three families were shipping Cyrillic-only
subsets, so no Latin glyph on the site had ever rendered in them. Newsreader
was added as the display face in the same pass. See §3 — this is the largest
single visual change of the session, and it was invisible until measured.

**Asymmetric composition** — struck from the not-done list above: the homepage
now has a decision layer at the top and a weighted opportunity grid, rather than
seventeen equally-loud section stacks.

### Still not done, and why

**"Why this matters for markets" on world stories.** The World section already
carries a lead, summaries and compact stories. The missing piece is the
market-relevance sentence, and doing it honestly needs `brief_engine.py`'s QA
gate (which already catches invented names and numbers) wired into the homepage
feed, plus Groq headroom the 8k TPM budget does not currently have. Half-built,
it would be AI wearing raw data's clothes — the one thing §43 forbids.

**A "So what?" decision strip.** Deliberately not built. Its content — watch
these instruments, these are the setups, these are the research names — is
already on the page, and What Matters Now already links to each. Adding it
would duplicate the layer above it, which is what the "What moved" grid did
before it was deleted for the same reason.

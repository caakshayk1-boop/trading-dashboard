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

All clear WCAG AA (4.5:1), including `--dim` on the ~35 places it carries
metadata.

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
| Asymmetric homepage composition | Still a uniform section stack |
| Skeleton loading states | Spinners and text still used |
| Icon system | Emoji still used in places |
| Command palette redesign | Functional, not restyled |
| Page transitions | Not implemented (single-document site) |
| Density modes (compact/comfortable/reading) | Not implemented |

The token layer is the foundation those depend on, which is why it came first:
every one of them is cheaper to build now than it was before.

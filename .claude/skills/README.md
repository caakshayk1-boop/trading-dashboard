# Vendored skills

Third-party Claude skills, copied in rather than referenced, because this
container is ephemeral — anything not committed is gone when it is reclaimed.

## Source

`emilkowalski/skills` @ https://github.com/emilkowalski/skills — MIT,
Copyright (c) 2026 Emil Kowalski. The upstream LICENSE is reproduced in
`LICENSE-emilkowalski` beside this file. Updating means re-copying from
upstream; nothing here is edited locally, so a diff against upstream is the
whole change history.

## What is here, and what is deliberately not

  apple-design      290 lines. Motion, materials, depth, typography (optical
                    sizing, tracking, leading), gesture and spring behaviour,
                    reduced-motion. Written from Apple's WWDC design talks,
                    translated for the web.
  emil-design-eng   674 lines. UI polish, component design, animation
                    decisions, the invisible details.

The other eleven skills in that repo are NOT vendored: animate-expo,
mobile-native, write-swift, ask-sonner and pick-ui-library target React Native,
Swift and React component libraries, none of which this repo uses. The four
animation skills (animate, animation-vocabulary, improve-animations,
review-animations, find-animation-opportunities) are plausible for the
frontends and were left out only to keep the skill list small enough that
triggering stays accurate — a skill nobody's task matches is a skill that
dilutes every description around it. Copy one in if a job calls for it.

## What these do NOT override

Both are about how an interface FEELS. Neither knows anything about this
repo's honesty rules, and those win on every collision:

  · No section may phrase its own freshness — `{{ dh('Dataset name') }}` is
    the only vocabulary for it (data_health.py).
  · Missing renders as the WORD for its absence, never a zero.
  · No denominator, no ratio.
  · SECTION_MAP order IS document order; test_page_structure.py fails the
    build when they drift.
  · Nothing predicts. No probability, target or forecast anywhere.

A prettier page that reads more confident than its data is a regression here,
not an improvement.

# Regulatory classification brief — AskAkshay Edge

**Status: OPEN. This is the launch gate.** Nothing in Phase 2 that costs money
— Apple Developer membership, Play Console, design, native build — should be
committed until this is answered in writing by counsel.

This document exists to be sent to a lawyer. It is not legal advice and was not
written by one.

---

## 1. What is actually being sold

A $9.99/month subscription to a product that, as designed, outputs:

| Output | Example | Why it matters here |
|---|---|---|
| Market context | "Regime: Neutral, 58/100" | Closest to general information |
| News summaries | Smart Reads | Closest to publishing |
| Screens/rankings | 750 companies ranked on published accounts | Arguably research |
| **Directional signals on named securities** | "TCS · BULLISH · 82/100" | **The problem** |
| **Entry / invalidation levels** | "invalid below ₹X" | **The bigger problem** |

The first three are defensible as information or research in most regimes. The
last two are what move the product toward *recommendation* and, if personalised
to a user's holdings or risk profile, toward *advice*.

## 2. The question for counsel

> In each target jurisdiction, is AskAkshay Edge — as described above, sold by
> subscription to retail consumers — (a) general financial information,
> (b) investment research, (c) an investment recommendation, or (d) personalised
> investment advice? What licence, registration or exemption applies to the
> answer, and what must change in the product to land in a lighter category?

Ask for the answer **per output type**, not for the product as a whole. It is
likely that Smart Reads is fine and the signal page is not, and knowing that
line is what lets the product ship at all.

## 3. Jurisdictions, in priority order

| Where | Why it is first | Known position to verify |
|---|---|---|
| **Malaysia** | Where Akshay is resident | The Securities Commission has stated that digital/algorithmic investment advice is still investment advice and that carrying on that business requires a licence |
| **India** | Where the securities are listed and the audience is | SEBI runs separate Research Analyst and Investment Adviser regimes; which one bites depends on the answer above |
| Everywhere else | — | **Do not select "worldwide" at launch.** Store distribution is per-country and reversible; a licensing breach is not |

## 4. What a disclaimer does and does not do

"For educational purposes only" does not reclassify a regulated activity. It is
necessary and it is not sufficient. Counsel should draft the actual wording
against the answer in §2, not the other way round.

## 5. Store-level gates that sit on top of the legal answer

These are separate from the licensing question and both must be cleared.

- **Apple** — review guidelines require apps used for financial trading,
  investing or money management to be submitted by the institution providing
  the service, with the necessary licensing. Verify current text before
  submitting; this affects whether the developer account must be an entity.
- **Google Play** — financial products/services policy plus a mandatory
  Financial Features declaration, including on testing tracks. The declaration
  must be answered against what the app *does*, not how it is marketed.

Both should be re-read at submission time rather than trusted from this note —
these policies change, and this one is dated.

## 6. What is safe to build before the answer

Everything that is true regardless of the classification:

- the data/intelligence engine and its health layer
- signal auditability and immutability (§ below)
- authentication, accounts, account deletion
- the entitlement engine (`edge/entitlements.py`) — payment-source agnostic
- the API contract

What is **not** safe to build first: the native apps, the store subscriptions,
and any marketing that describes the product in words counsel has not seen.

## 7. Corporate structure — decide alongside

Running a paid financial product through personal accounts is a problem for
Apple's entity requirement, for tax, and for liability. Sequence:

    entity → bank account → Apple Developer (org) → Play Console → Stripe

---

*Written 2026-08-18. Every regulatory statement here must be re-verified with
counsel before it is relied on.*

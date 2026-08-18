# Launch gates — AskAkshay Edge

Ten gates. Each is PASS/FAIL, not a percentage. Nothing goes public until all
ten pass, and they are ordered so the expensive work sits behind the cheap
answers.

| # | Gate | The question | Status | Blocked on |
|---|---|---|---|---|
| 1 | **Data** | Can I trust every number the product shows, and does it say so when I cannot? | 🟡 IN PROGRESS | Data Health layer shipped; Smart Reads + signal freshness still open |
| 2 | **Signal** | Can I reproduce every historical signal exactly as it was issued? | 🔴 OPEN | Signal immutability + methodology versioning |
| 3 | **Regulatory** | Can I legally offer this in each launch jurisdiction? | 🔴 OPEN | `REGULATORY_BRIEF.md` — counsel |
| 4 | **Product** | Can a new user understand this in 60 seconds? | 🔴 OPEN | Edge web app does not exist |
| 5 | **Subscription** | Can a user purchase, renew, cancel, restore and be refunded correctly? | 🟡 PARTIAL | Entitlement engine done + tested; no store integration |
| 6 | **Security** | Can the frontend be manipulated to unlock premium? Must be NO. | 🟡 PARTIAL | Server-side by construction; needs auth + a real pen test |
| 7 | **Mobile** | Does it work on real devices? | 🔴 OPEN | No app |
| 8 | **Store** | Can an Apple/Google reviewer understand and test it? | 🔴 OPEN | Needs demo account + comp entitlement (supported) + review notes |
| 9 | **Beta** | Would 20–50 independent testers pay? | 🔴 OPEN | Nothing to test |
| 10 | **Launch** | All nine above green | 🔴 OPEN | — |

## Rules for this file

- A gate goes green only with evidence attached — a test run, a document, a
  screenshot of the store declaration. Not a judgement.
- Gate 3 blocks 7, 8, 9 and 10. Do not spend money past it.
- Gate 5 must be provable with the store's *sandbox*, never against production
  billing.
- Gate 6 fails if any premium check exists client-side. `edge/entitlements.py`
  is the only place that decides.

## The KPI that decides whether this was worth doing

Not downloads, not signups, not traffic.

> **30-day paid retention.**

If someone pays $9.99 and cancels after one month, this is a curiosity product,
not a subscription product.

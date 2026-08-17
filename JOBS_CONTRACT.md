# docs/jobs.json — data contract

Frozen 2026-08-18. `jobs.py` writes this file; `newspaper.py` renders it.
Neither side may change a field name without changing this file first.

## Why a static JSON file and not an API route

`vercel-news/api/` holds exactly **12 functions and the Vercel Hobby cap is 12**.
A 13th route fails the deploy outright (this already happened once, commit
c2de962, "paper wallet route silently broke the deploy"). So the career section
ships as a static artefact, exactly like `docs/screen.json`.

## The four allow-lists

A file under `docs/` reaches production only if it is named in **all four**:

1. written by the producer (`jobs.py` / `generate.py`)
2. `git add`ed by name in `.github/workflows/*.yml`
3. `!docs/jobs.json` in `.vercelignore`
4. copied by name in `vercel-news/build.js`

Miss one and the URL 404s with a green build log. See
`memory/project_daily_signal_site.md`.

## Top level

```jsonc
{
  "generated_at": "2026-08-18T04:30:00Z",   // UTC ISO8601, when the scrape ran
  "next_refresh": "2026-08-19T04:30:00Z",   // UTC ISO8601 or null if unknown
  "sources": [                               // one row per source ATTEMPTED
    {
      "name": "Alshaya Group",
      "kind": "employer",                    // employer | aggregator | recruiter
      "status": "ok",                        // ok | blocked | error | empty
      "jobs_found": 14,
      "detail": "",                          // error text when status != ok
      "last_success": "2026-08-18T04:30:00Z" // UTC ISO8601 or null
    }
  ],
  "stats": {
    "total_active": 0, "high_fit": 0, "s_tier": 0,
    "by_country": {"UAE": 0, "Saudi Arabia": 0, "Malaysia": 0, "Oman": 0},
    "duplicates_removed": 0, "stale_removed": 0,
    "sources_ok": 0, "sources_attempted": 0
  },
  "jobs": [ /* Job objects, pre-sorted by opportunity_score DESC */ ]
}
```

## Job object

Every field must be present. Use `null` for unknown — **never invent a value**.
The renderer prints "Not disclosed" for `null` and "Unverified" for
`application_url_verified: false`.

```jsonc
{
  "id": "sha1-of-fingerprint",       // stable across refreshes
  "company": "Alshaya Group",
  "company_group": "Alshaya",        // null if standalone
  "title": "Senior Finance Manager",  // exact, as posted
  "normalized_title": "senior finance manager",
  "location": "Dubai",
  "country": "UAE",                  // UAE | Saudi Arabia | Malaysia | Oman | <other>
  "region": "GCC",                   // GCC | SEA | Other
  "department": "Finance",           // null if not stated
  "employment_type": "Full-time",    // null if not stated
  "posted_date": "2026-08-14",       // ISO date or null. NEVER guess.
  "closing_date": null,
  "scraped_at": "2026-08-18T04:30:00Z",
  "last_verified_at": "2026-08-18T04:30:00Z",
  "status": "NEW",                   // NEW|ACTIVE|AGING|STALE|CLOSED|REMOVED|LINK_BROKEN
  "source": "Alshaya Group",         // primary source name
  "sources": ["Alshaya Group", "GulfTalent"],  // all sources seen, deduped
  "source_url": "https://...",
  "application_url": "https://...",  // null if none found
  "is_direct_apply": true,           // true only for employer ATS/application page
  "application_url_verified": true,  // true only if HEAD/GET returned < 400
  "source_confidence": "high",       // high (employer) | medium (recruiter) | low
  "salary_min": null, "salary_max": null, "salary_currency": null,
  "experience_min": 8, "experience_max": null,
  "description": "...",              // full text from the DETAIL page, not the card
  "responsibilities": ["..."],
  "requirements": ["..."],
  "skills": ["FP&A", "IFRS"],
  "nationality_requirement": null,
  "work_authorization_requirement": null,
  "emiratisation_requirement": false,
  "saudization_requirement": false,
  "candidate_fit_score": 91,         // 0-100 int
  "employer_score": 88,              // 0-100 int
  "career_upside_score": 84,         // 0-100 int
  "opportunity_score": 89,           // 0-100 int, the combined rank key
  "score_breakdown": {               // must sum consistently with candidate_fit_score
    "seniority": 18, "functional": 20, "industry": 15, "geographic": 10,
    "scope": 9, "leadership": 8, "qualification": 5, "upside": 6
  },
  "tier": "S",                       // S|A|B|C|D
  "application_priority": "APPLY NOW", // APPLY NOW|HIGH PRIORITY|GOOD FIT|OPTIONAL|SKIP
  "why_fit": ["...", "..."],         // 2-4 SPECIFIC reasons, must cite the resume
  "watch_out": ["..."],              // 1-2 honest negatives
  "resume_match": {                  // only required for tier S and A, else null
    "strong": ["IFRS consolidation", "multi-country P&L"],
    "missing": ["SAP"],
    "tailoring_recommended": true
  },
  "duplicate_group": "sha1-...",     // shared by all rows folded into this one
  "is_excluded": false,              // true => never rendered in the primary feed
  "exclusion_reason": null           // e.g. "transactional AP role"
}
```

## Candidate profile — the scoring baseline

Source of truth: `~/Downloads/CA Akshay Kothari FC Resume.pdf`. Do not invent
experience beyond this.

| Attribute | Value |
|---|---|
| Qualification | Chartered Accountant, ICAI, 2017. B.Com (Hons), Calcutta, 2013 |
| Experience | 10+ years total, 7+ post-qualification |
| Current role | FP&A Manager, Lifestyle Retail Malaysia Sdn Bhd (**Landmark Group**), May 2023– |
| Prior | Asst Manager Accounts & Finance, same group, 2019–2023; Asst Manager Accounts, Emami Realty (real estate), Kolkata, 2016–2019 |
| Scope | MYR 400M+ P&L, 60+ retail stores, Malaysia + Indonesia (2 countries) |
| Brands | Max Fashion, Babyshop |
| Domain | Retail / fashion / lifestyle; earlier real estate |
| Technical | IFRS, MPERS, group consolidation, IFRS 16 (60+ leases), D365 ERP lead (close −40%), Power BI, ROI/IRR/NPV modelling, GST/TDS |
| Exposure | Board, CEO, CFO quarterly reporting; capital allocation influence; store feasibility |
| Leadership | Mentored team of 4 analysts; business partner to Ops, Retail Heads, Buying, IR |
| Base | Kuala Lumpur, Malaysia. Phone +60. |
| Targets | UAE/Dubai, Saudi (Riyadh/Jeddah/Khobar/Dammam), Malaysia (KL/Selangor/PJ), Oman (Muscat) |

**Landmark Group is his current employer.** Peer retail groups — Alshaya, Majid
Al Futtaim, Chalhoub, Al-Futtaim, GMG, Apparel Group, Azadea, Americana, Lulu,
Al Tayer, Cenomi — should score *higher* on industry fit than a generic
multinational, because the multi-country retail P&L transfers directly.

## Scoring weights — candidate_fit_score, 100 points

| Component | Max | Rewards |
|---|---|---|
| Seniority | 20 | Manager/Controller/Head level with real ownership. Penalise both junior AND roles needing 15+ yrs or CFO-level track record |
| Functional | 20 | FP&A, commercial finance, controllership, business finance, business partnering |
| Industry | 15 | Retail/consumer/FMCG top; real estate strong (Emami Realty); hospitality/tech/industrial partial |
| Geographic | 10 | UAE, Saudi, Malaysia, Oman |
| Scope | 10 | Regional / multi-country / multi-entity / P&L ownership |
| Leadership | 10 | Team leadership, C-suite/Board exposure |
| Qualification | 5 | CA/ACCA/CPA accepted; ERP/Power BI/analytics |
| Career upside | 10 | Brand quality, progression toward FD/CFO |

**Do not score on title alone.** A "Business Controller" with regional P&L
outranks a "Finance Manager" doing AP/AR supervision. Read the responsibilities.

## Hard exclusions — set `is_excluded: true`

Accountant / Senior Accountant / AP / AR / Payroll manager; tax-only;
treasury-only; audit-only or internal-audit-only; junior analyst; intern;
graduate; roles requiring a nationality he cannot hold (Emirati/Saudi national
**only** postings); roles demanding a qualification he lacks; roles materially
below FP&A Manager level.

Emiratisation/Saudization *preference* is a `watch_out`, not an exclusion.
Only a hard national-only requirement excludes.

## Freshness

| Age from `posted_date` | status |
|---|---|
| 0–7 days | NEW |
| 8–21 days | ACTIVE |
| 22–45 days | AGING |
| 46+ days | STALE |

`posted_date: null` → status `ACTIVE`, and `watch_out` must say the posting date
is not published. Never fabricate a date to manufacture freshness.
Age alone never sets CLOSED — only a revalidation that finds the posting gone
sets `REMOVED`, and only a failed URL check sets `LINK_BROKEN`.

## Tiers

S ≥ 85 · A 75–84 · B 65–74 · C 50–64 · D < 50.
D and `is_excluded` never render in the primary feed.

## Failure handling

A source that fails must **never** blank the dataset. Load the previous
`docs/jobs.json`, keep its jobs, mark that source `status: "blocked"|"error"`
and carry its old `last_success` forward. Only rows re-confirmed by a
successful scrape get a fresh `last_verified_at`.

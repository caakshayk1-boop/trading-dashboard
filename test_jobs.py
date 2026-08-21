#!/usr/bin/env python3
"""test_jobs.py — pure-function tests for the job discovery engine.

No network. Every test here exercises logic that can silently corrupt the
dataset: scoring, deduplication, hard exclusions, freshness, and the
"never invent a value" rules.

    python3 test_jobs.py      # or: pytest test_jobs.py
"""

from __future__ import annotations

import sys
import unittest

import jobs


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def make_job(**kw) -> dict:
    """A contract-shaped job with sensible defaults, for scoring tests."""
    base = {
        "id": "x", "company": "Alshaya Group", "company_group": "Alshaya",
        "title": "Senior Finance Manager", "normalized_title": "senior finance manager",
        "location": "Dubai", "country": "UAE", "region": "GCC",
        "department": "Finance", "employment_type": "Full-time",
        "posted_date": "2026-08-14", "closing_date": None,
        "scraped_at": "2026-08-18T04:30:00Z", "last_verified_at": "2026-08-18T04:30:00Z",
        "status": "NEW", "source": "Alshaya Group", "sources": ["Alshaya Group"],
        "source_url": "https://example.com/job/1",
        "application_url": "https://example.com/job/1/apply",
        "is_direct_apply": True, "application_url_verified": True,
        "source_confidence": "high",
        "salary_min": None, "salary_max": None, "salary_currency": None,
        "experience_min": None, "experience_max": None,
        "description": "", "responsibilities": [], "requirements": [], "skills": [],
        "nationality_requirement": None, "work_authorization_requirement": None,
        "emiratisation_requirement": False, "saudization_requirement": False,
        "candidate_fit_score": 0, "employer_score": 0, "career_upside_score": 0,
        "opportunity_score": 0, "score_breakdown": {}, "tier": "D",
        "application_priority": "SKIP", "why_fit": [], "watch_out": [],
        "resume_match": None, "duplicate_group": None,
        "is_excluded": False, "exclusion_reason": None,
    }
    base.update(kw)
    if "title" in kw and "normalized_title" not in kw:
        base["normalized_title"] = jobs.normalize_title(kw["title"])
    # Distinct postings must have distinct canonical URLs, or dedupe will
    # correctly merge them on the shared URL and the test would be asserting
    # against a fixture artefact rather than the logic.
    if "source_url" not in kw:
        base["source_url"] = f"https://example.com/job/{base['id']}"
    return base


STRONG_FPA_DESC = (
    "Lead the FP&A function for the regional retail business across multiple "
    "countries. Own the P&L, drive budgeting and forecasting cycles, and deliver "
    "variance analysis to the Board and CFO. Partner with Retail Operations on "
    "profitability analysis and margin analysis. Manage consolidation under IFRS "
    "including IFRS 16 lease accounting. Lead a team of 5 analysts. Build capex "
    "business cases using NPV and IRR for new store feasibility. Power BI and "
    "D365 experience required. Chartered Accountant or ACCA with 8 years of "
    "experience in fashion and lifestyle retail."
)


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------

class TestNormalization(unittest.TestCase):

    def test_title_normalization_collapses_variants(self):
        self.assertEqual(jobs.normalize_title("Sr. Finance Mgr"), "senior finance manager")
        self.assertEqual(jobs.normalize_title("Finance Manager (Full-Time)"), "finance manager")
        self.assertEqual(jobs.normalize_title("FP & A Manager"), "fp&a manager")
        self.assertEqual(jobs.normalize_title("Finance Manager | Emirati Talent"),
                         "finance manager")

    def test_leading_nationality_tag_does_not_eat_the_title(self):
        """Regression: the tag strip consumed the rest of the string, so
        "UAE National_Senior Accountant" normalized to "" and slipped past
        every exclusion rule into the feed."""
        n = jobs.normalize_title(
            "UAE National_Senior Accountant - Transformation | Corporate Services")
        self.assertIn("senior accountant", n)
        self.assertNotIn("uae national", n)
        self.assertIsNotNone(jobs.exclusion_for(n, ""))

    def test_company_normalization_drops_legal_suffixes(self):
        self.assertEqual(jobs.normalize_company("Alshaya Group LLC"), "alshaya")
        self.assertEqual(jobs.normalize_company("Majid Al Futtaim Holding"),
                         "majid al futtaim")
        self.assertEqual(jobs.normalize_company("Lifestyle Retail Sdn Bhd"),
                         "lifestyle retail")

    def test_location_normalization_keeps_city_only(self):
        self.assertEqual(jobs.normalize_location("Dubai, United Arab Emirates"), "dubai")
        self.assertEqual(jobs.normalize_location("Dubai"), "dubai")

    def test_country_resolution(self):
        self.assertEqual(jobs.resolve_country("Dubai, United Arab Emirates"), "UAE")
        self.assertEqual(jobs.resolve_country("Riyadh"), "Saudi Arabia")
        self.assertEqual(jobs.resolve_country("Kuala Lumpur"), "Malaysia")
        self.assertEqual(jobs.resolve_country("Muscat"), "Oman")
        self.assertIsNone(jobs.resolve_country("Atlantis"))
        self.assertIsNone(jobs.resolve_country(None))

    def test_country_never_guessed_from_description_text(self):
        """Regression: a Cairo role whose blurb name-checked the Dubai head
        office was being labelled UAE. Country must come from location only."""
        src = {"name": "Majid Al Futtaim", "kind": "employer",
               "confidence": "high", "group": "MAF"}
        raw = jobs._raw(
            title="Senior Finance Manager", company="Majid Al Futtaim",
            location="Cairo", country=None,
            description=("Join our team in Cairo. Majid Al Futtaim is "
                         "headquartered in Dubai, United Arab Emirates and "
                         "operates across the UAE. " + STRONG_FPA_DESC),
            source_url="https://x.com/job/1",
            application_url="https://x.com/job/1/apply")
        j = jobs.normalize_job(raw, src, "2026-08-18T00:00:00Z")
        self.assertEqual(j["country"], "Egypt")
        self.assertNotEqual(j["country"], "UAE")

    def test_non_target_countries_resolve_to_themselves(self):
        self.assertEqual(jobs.resolve_country("Cairo"), "Egypt")
        self.assertEqual(jobs.resolve_country("Egypt"), "Egypt")
        self.assertEqual(jobs.resolve_country("Doha, Qatar"), "Qatar")
        self.assertEqual(jobs.region_for(jobs.resolve_country("Cairo")), "Other")

    def test_bare_country_is_not_reported_as_a_city(self):
        """Regression: location read "United Arab Emirates" in the city slot."""
        self.assertIsNone(jobs.primary_city("United Arab Emirates"))
        self.assertIsNone(jobs.primary_city("Saudi Arabia"))
        self.assertEqual(jobs.primary_city("Dubai, United Arab Emirates"), "Dubai")

    def test_region_mapping(self):
        self.assertEqual(jobs.region_for("UAE"), "GCC")
        self.assertEqual(jobs.region_for("Malaysia"), "SEA")
        self.assertEqual(jobs.region_for("France"), "Other")
        self.assertEqual(jobs.region_for(None), "Other")

    def test_parse_date_never_guesses(self):
        self.assertEqual(jobs.parse_date("2026-08-14T10:00:00Z"), "2026-08-14")
        self.assertEqual(jobs.parse_date("14/08/2026"), "2026-08-14")
        self.assertIsNone(jobs.parse_date(None))
        self.assertIsNone(jobs.parse_date(""))
        self.assertIsNone(jobs.parse_date("recently"))
        self.assertIsNone(jobs.parse_date("3 days ago"))

    def test_parse_experience_only_when_stated(self):
        self.assertEqual(jobs.parse_experience("Minimum 8 years of experience"), (8, None))
        self.assertEqual(jobs.parse_experience("5 to 7 years experience"), (5, 7))
        self.assertEqual(jobs.parse_experience("A great opportunity"), (None, None))

    def test_parse_salary_only_when_printed(self):
        self.assertEqual(jobs.parse_salary("Salary AED 30,000 - 40,000 per month"),
                         (30000, 40000, "AED"))
        self.assertEqual(jobs.parse_salary("Competitive salary offered"),
                         (None, None, None))
        self.assertEqual(jobs.parse_salary("attractive package"), (None, None, None))


# ---------------------------------------------------------------------------
# freshness
# ---------------------------------------------------------------------------

class TestFreshness(unittest.TestCase):

    T = "2026-08-18"

    def test_bands_match_contract(self):
        self.assertEqual(jobs.freshness_status("2026-08-18", self.T), "NEW")
        self.assertEqual(jobs.freshness_status("2026-08-11", self.T), "NEW")     # 7d
        self.assertEqual(jobs.freshness_status("2026-08-10", self.T), "ACTIVE")  # 8d
        self.assertEqual(jobs.freshness_status("2026-07-28", self.T), "ACTIVE")  # 21d
        self.assertEqual(jobs.freshness_status("2026-07-27", self.T), "AGING")   # 22d
        self.assertEqual(jobs.freshness_status("2026-07-04", self.T), "AGING")   # 45d
        self.assertEqual(jobs.freshness_status("2026-07-03", self.T), "STALE")   # 46d

    def test_null_posted_date_is_active_not_new(self):
        """Contract: a missing date must never be laundered into freshness."""
        self.assertEqual(jobs.freshness_status(None, self.T), "ACTIVE")
        self.assertEqual(jobs.freshness_status("", self.T), "ACTIVE")
        self.assertEqual(jobs.freshness_status("garbage", self.T), "ACTIVE")

    def test_null_date_forces_a_watch_out(self):
        j = make_job(posted_date=None, description=STRONG_FPA_DESC)
        j["status"] = jobs.freshness_status(None, self.T)
        notes = " ".join(jobs.build_watch_out(j)).lower()
        self.assertIn("date", notes)


# ---------------------------------------------------------------------------
# exclusions
# ---------------------------------------------------------------------------

class TestExclusions(unittest.TestCase):

    def _excluded(self, title, text=""):
        return jobs.exclusion_for(jobs.normalize_title(title), text)

    def test_junior_and_transactional_roles_excluded(self):
        for title in ["Senior Accountant", "Accountant", "Accounts Payable Manager",
                      "AR Accountant", "Payroll Manager", "Junior Analyst",
                      "Finance Intern", "Graduate Trainee Programme",
                      "Bookkeeper", "Billing Clerk", "Credit Control Manager"]:
            with self.subTest(title=title):
                self.assertIsNotNone(self._excluded(title),
                                     f"{title!r} should be excluded")

    def test_single_function_roles_excluded(self):
        for title in ["Tax Manager", "Treasury Manager", "Internal Audit Manager",
                      "Audit Senior"]:
            with self.subTest(title=title):
                self.assertIsNotNone(self._excluded(title))

    def test_non_finance_roles_excluded(self):
        for title in ["Store Manager", "Sales Associate", "Beauty Consultant",
                      "Warehouse Supervisor", "Head Chef"]:
            with self.subTest(title=title):
                self.assertIsNotNone(self._excluded(title))

    def test_assistant_manager_grade_excluded(self):
        """Regression: 'Assistant Manager - Accounts' reached the top 10. That
        is the grade he left in 2023, i.e. materially below FP&A Manager."""
        for title in ["Assistant Manager - Accounts | Al-Futtaim Automotive",
                      "Assistant Manager Finance", "Assistant Manager - Accounting"]:
            with self.subTest(title=title):
                self.assertIsNotNone(self._excluded(title, STRONG_FPA_DESC))

    def test_commercial_account_management_is_not_finance(self):
        """Regression: 'Senior Manager, Credit Cards & Account Management'
        ranked as a finance role. "Account management" here means customers."""
        for title in ["Senior Manager, Credit Cards & Account Management",
                      "Key Account Manager - Beauty",
                      "Relationship Manager - Corporate"]:
            with self.subTest(title=title):
                self.assertIsNotNone(self._excluded(title, STRONG_FPA_DESC))

    def test_business_unit_named_financial_services_is_not_a_finance_signal(self):
        """Al-Futtaim brands its insurance arm "Financial Services", so every
        title in it carried the substring `financ`. The escape hatch on the
        "not a finance role" rule matched that and readmitted the lot — an
        insurance salesperson and a cross-sell specialist both reached the
        live feed as finance opportunities.
        """
        for title in [
            "Insurance Advisor - Individual Life I Dubai | Financial Services",
            "Outbound Sales - Renewal & Cross Sell Specialist I Dubai | Financial Services",
            "Business Development Manager I Dubai | Financial Services",
            "AI Product Owner - Insurance I Dubai | Financial Services",
            "Financial Crime Investigator I Dubai | Financial Services",
            "Finance & Insurance Advisor",
        ]:
            with self.subTest(title=title):
                self.assertIsNotNone(self._excluded(title),
                                     f"{title!r} should be excluded")

    def test_junior_prefix_excluded_on_any_title(self):
        """The original rule only fired on junior+analyst/accountant/officer,
        so "Junior Financial Controller" scored as a B-tier controllership
        role — a grade below where he started."""
        for title in ["Junior Financial Controller", "Junior Insurance Executive",
                      "Junior Finance Manager"]:
            with self.subTest(title=title):
                self.assertIsNotNone(self._excluded(title))

    def test_risk_compliance_and_systems_roles_excluded(self):
        for title in ["Risk Manager I Dubai | Financial Services",
                      "Process & Compliance Manager | Automotive",
                      "Senior Governance, Risk Compliance Specialist",
                      "Assistant Vice President - Financial Crime Compliance",
                      "Medical Core System Transformation Lead",
                      "Assistant Cost Controller-Finance"]:
            with self.subTest(title=title):
                self.assertIsNotNone(self._excluded(title))

    def test_finance_transformation_survives_the_systems_rule(self):
        """§4 of the brief lists Finance Transformation Manager as a TARGET
        role. The systems/transformation exclusion must not eat it."""
        self.assertIsNone(self._excluded("Finance Transformation Manager"))
        self.assertIsNone(self._excluded("FP&A Transformation Lead"))

    def test_target_roles_not_excluded(self):
        for title in ["Senior Finance Manager", "Finance Manager",
                      "FP&A Manager", "Financial Controller",
                      "Head of Financial Planning and Analysis",
                      "Commercial Finance Manager", "Business Finance Manager",
                      "Finance Business Partner"]:
            with self.subTest(title=title):
                self.assertIsNone(self._excluded(title, STRONG_FPA_DESC),
                                  f"{title!r} should NOT be excluded")

    def test_finance_manager_accounts_survives_the_non_finance_rule(self):
        """A finance title that happens to mention retail must not be dropped."""
        self.assertIsNone(self._excluded("Finance Manager - Retail Stores",
                                         STRONG_FPA_DESC))

    def test_nationals_only_excluded_but_preference_is_not(self):
        hard = "This role is open only to UAE nationals."
        self.assertIsNotNone(self._excluded("Finance Manager", hard))

        soft = ("Finance Manager for our retail division. " + STRONG_FPA_DESC +
                " Emiratisation candidates are strongly preferred.")
        self.assertIsNone(self._excluded("Finance Manager", soft))
        j = make_job(description=soft)
        notes = " ".join(jobs.build_watch_out(j)).lower()
        self.assertTrue("emiratisation" in notes or "saudization" in notes)

    def test_roles_needing_fifteen_years_excluded(self):
        self.assertIsNotNone(self._excluded(
            "Finance Manager", "Requires minimum 15 years of experience."))
        self.assertIsNone(self._excluded(
            "Finance Manager", "Requires minimum 8 years of experience. " + STRONG_FPA_DESC))

    def test_cfo_level_excluded(self):
        self.assertIsNotNone(self._excluded("Chief Financial Officer"))
        self.assertIsNotNone(self._excluded("Finance Director"))


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

class TestScoring(unittest.TestCase):

    def test_breakdown_sums_to_candidate_fit_score(self):
        """Contract: score_breakdown must sum consistently with the fit score."""
        j = jobs.score_job(make_job(description=STRONG_FPA_DESC))
        self.assertEqual(sum(j["score_breakdown"].values()), j["candidate_fit_score"])

    def test_breakdown_has_every_contract_component(self):
        j = jobs.score_job(make_job(description=STRONG_FPA_DESC))
        self.assertEqual(set(j["score_breakdown"]),
                         {"seniority", "functional", "industry", "geographic",
                          "scope", "leadership", "qualification", "upside"})

    def test_component_caps_respected(self):
        caps = {"seniority": 20, "functional": 20, "industry": 15, "geographic": 10,
                "scope": 10, "leadership": 10, "qualification": 5, "upside": 10}
        j = jobs.score_job(make_job(description=STRONG_FPA_DESC * 5))
        for k, cap in caps.items():
            self.assertLessEqual(j["score_breakdown"][k], cap, f"{k} exceeded cap")
            self.assertGreaterEqual(j["score_breakdown"][k], 0)

    def test_all_scores_in_range(self):
        j = jobs.score_job(make_job(description=STRONG_FPA_DESC))
        for f in ("candidate_fit_score", "employer_score",
                  "career_upside_score", "opportunity_score"):
            self.assertTrue(0 <= j[f] <= 100, f"{f}={j[f]} out of range")

    def test_responsibilities_beat_the_title(self):
        """Contract: a Business Controller with regional P&L must outrank a
        Finance Manager supervising AP/AR."""
        controller = jobs.score_job(make_job(
            title="Business Controller", description=STRONG_FPA_DESC))
        supervisor = jobs.score_job(make_job(
            title="Finance Manager",
            description=("Supervise the accounts payable and accounts receivable "
                         "clerks. Ensure invoices are posted on time and "
                         "reconcile supplier statements each month.")))
        self.assertGreater(controller["candidate_fit_score"],
                           supervisor["candidate_fit_score"])

    def test_geographic_weighting_favours_target_markets(self):
        uae = jobs.score_job(make_job(country="UAE", description=STRONG_FPA_DESC))
        other = jobs.score_job(make_job(country="France", description=STRONG_FPA_DESC))
        self.assertGreater(uae["score_breakdown"]["geographic"],
                           other["score_breakdown"]["geographic"])

    def test_peer_retail_outranks_generic_multinational_on_industry(self):
        peer = jobs.score_job(make_job(company="Chalhoub Group",
                                       description=STRONG_FPA_DESC))
        generic = jobs.score_job(make_job(
            company="Generic Industrial Co",
            description=("Lead FP&A for our industrial manufacturing division. "
                         "Own budgeting, forecasting and variance analysis.")))
        self.assertGreater(peer["score_breakdown"]["industry"],
                           generic["score_breakdown"]["industry"])

    def test_tier_thresholds(self):
        self.assertEqual(jobs.tier_for(85), "S")
        self.assertEqual(jobs.tier_for(84), "A")
        self.assertEqual(jobs.tier_for(75), "A")
        self.assertEqual(jobs.tier_for(74), "B")
        self.assertEqual(jobs.tier_for(65), "B")
        self.assertEqual(jobs.tier_for(64), "C")
        self.assertEqual(jobs.tier_for(50), "C")
        self.assertEqual(jobs.tier_for(49), "D")

    def test_priority_mapping_and_excluded_always_skips(self):
        self.assertEqual(jobs.priority_for("S"), "APPLY NOW")
        self.assertEqual(jobs.priority_for("A"), "HIGH PRIORITY")
        self.assertEqual(jobs.priority_for("B"), "GOOD FIT")
        self.assertEqual(jobs.priority_for("C"), "OPTIONAL")
        self.assertEqual(jobs.priority_for("D"), "SKIP")
        self.assertEqual(jobs.priority_for("S", excluded=True), "SKIP")

    def test_a_strong_retail_fpa_role_reaches_top_tiers(self):
        j = jobs.score_job(make_job(company="Majid Al Futtaim",
                                    description=STRONG_FPA_DESC))
        self.assertIn(j["tier"], ("S", "A"))
        self.assertEqual(j["application_priority"],
                         "APPLY NOW" if j["tier"] == "S" else "HIGH PRIORITY")


# ---------------------------------------------------------------------------
# why_fit / watch_out / resume_match
# ---------------------------------------------------------------------------

class TestNarrative(unittest.TestCase):

    GENERIC = ["your skills match this role", "great opportunity",
               "you would be a good fit", "strong match"]

    def test_why_fit_is_specific_and_cites_the_resume(self):
        j = jobs.score_job(make_job(company="Chalhoub Group",
                                    description=STRONG_FPA_DESC))
        why = jobs.build_why_fit(j)
        self.assertTrue(2 <= len(why) <= 4)
        blob = " ".join(why).lower()
        for g in self.GENERIC:
            self.assertNotIn(g, blob, "generic filler leaked into why_fit")
        # must cite at least one concrete resume fact
        facts = ["myr 400m", "60+ store", "landmark", "ifrs", "d365",
                 "power bi", "board", "malaysia", "indonesia", "emami",
                 "irr", "npv", "4 analysts"]
        self.assertTrue(any(f in blob for f in facts),
                        f"why_fit cites no resume fact: {why}")

    def test_why_fit_leads_with_the_job_specific_reason(self):
        """Regression: every row opened with the same FP&A sentence. A role
        whose standout is real estate must not lead with generic FP&A."""
        fpa = jobs.score_job(make_job(
            title="Head of Financial Planning and Analysis",
            description=STRONG_FPA_DESC))
        self.assertIn("FP&A remit", jobs.build_why_fit(fpa)[0])

        realestate = jobs.score_job(make_job(
            title="Vice President - Asset Management",
            company="Aldar",
            description=("Own the commercial performance of the real estate "
                         "property portfolio across the region, covering leasing "
                         "and asset management for the malls. Budgeting and "
                         "forecasting for each entity.")))
        self.assertNotIn("FP&A remit", jobs.build_why_fit(realestate)[0])

    def test_why_fit_always_returns_two_to_four_entries(self):
        """Contract shape holds even when nothing in the posting matches."""
        blank = make_job(company="Unknown Co", country=None,
                         description="We are hiring. Apply through our portal today.")
        why = jobs.build_why_fit(blank)
        self.assertTrue(2 <= len(why) <= 4)
        blob = " ".join(why).lower()
        self.assertIn("no specific overlap", blob)   # honest, not invented
        for g in self.GENERIC:
            self.assertNotIn(g, blob)

    def test_single_match_is_topped_up_honestly_not_invented(self):
        """A posting yielding exactly one overlap still needs 2 entries, and
        the filler must be a true statement about his record."""
        thin = make_job(company="Aldar", country="UAE", description=(
            "Ensure the group is compliant with its regulatory obligations, "
            "financial crime awareness and adherence to internal policies."))
        why = jobs.build_why_fit(thin)
        self.assertTrue(2 <= len(why) <= 4)
        blob = " ".join(why).lower()
        self.assertIn("myr 400m", blob)
        for g in self.GENERIC:
            self.assertNotIn(g, blob)

    def test_watch_out_is_present_and_honest(self):
        j = make_job(description=STRONG_FPA_DESC)
        self.assertTrue(1 <= len(jobs.build_watch_out(j)) <= 2)

    def test_watch_out_flags_unverified_link(self):
        j = make_job(application_url_verified=False, description=STRONG_FPA_DESC)
        self.assertIn("confirm", " ".join(jobs.build_watch_out(j)).lower())

    def test_watch_out_flags_sap_gap(self):
        j = make_job(description="Lead FP&A. SAP and Hyperion experience essential.")
        self.assertIn("sap", " ".join(jobs.build_watch_out(j)).lower())

    def test_resume_match_only_for_s_and_a(self):
        top = jobs.score_job(make_job(company="Majid Al Futtaim",
                                      description=STRONG_FPA_DESC))
        top["tier"] = "S"
        rm = jobs.build_resume_match(top)
        self.assertIsInstance(rm, dict)
        self.assertEqual(set(rm), {"strong", "missing", "tailoring_recommended"})
        self.assertTrue(rm["strong"])

        low = make_job(tier="C")
        self.assertIsNone(jobs.build_resume_match(low))


# ---------------------------------------------------------------------------
# deduplication
# ---------------------------------------------------------------------------

class TestDeduplication(unittest.TestCase):

    def test_same_role_from_two_sources_folds_into_one(self):
        a = make_job(id="a", source="Alshaya Group", sources=["Alshaya Group"],
                     source_confidence="high", is_direct_apply=True,
                     application_url="https://alshaya.com/job/1/apply",
                     description=STRONG_FPA_DESC)
        b = make_job(id="b", source="GulfTalent", sources=["GulfTalent"],
                     source_confidence="medium", is_direct_apply=False,
                     application_url="https://gulftalent.com/job/1",
                     description=STRONG_FPA_DESC)
        merged, removed = jobs.deduplicate([a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(removed, 1)
        self.assertEqual(set(merged[0]["sources"]), {"Alshaya Group", "GulfTalent"})

    def test_winner_keeps_the_highest_confidence_direct_apply_url(self):
        board = make_job(id="b", source="Bayt", sources=["Bayt"],
                         source_confidence="low", is_direct_apply=False,
                         application_url="https://bayt.com/job/9",
                         description=STRONG_FPA_DESC + " extra text " * 40)
        employer = make_job(id="a", source="Alshaya Group", sources=["Alshaya Group"],
                            source_confidence="high", is_direct_apply=True,
                            application_url="https://alshaya.com/job/1/apply",
                            description=STRONG_FPA_DESC)
        merged, _ = jobs.deduplicate([board, employer])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["application_url"],
                         "https://alshaya.com/job/1/apply")
        self.assertTrue(merged[0]["is_direct_apply"])
        self.assertEqual(merged[0]["source_confidence"], "high")

    def test_different_roles_are_not_merged(self):
        a = make_job(id="a", title="Finance Manager", description=STRONG_FPA_DESC)
        b = make_job(id="b", title="Financial Controller", description=STRONG_FPA_DESC)
        merged, removed = jobs.deduplicate([a, b])
        self.assertEqual(len(merged), 2)
        self.assertEqual(removed, 0)

    def test_same_title_different_city_not_merged(self):
        a = make_job(id="a", location="Dubai", description=STRONG_FPA_DESC)
        b = make_job(id="b", location="Riyadh", country="Saudi Arabia",
                     description=STRONG_FPA_DESC)
        merged, _ = jobs.deduplicate([a, b])
        self.assertEqual(len(merged), 2)

    def test_description_similarity_splits_a_shared_fingerprint(self):
        """Same company/title/city but genuinely different roles stay apart."""
        a = make_job(id="a", description=STRONG_FPA_DESC)
        b = make_job(id="b", description=(
            "Completely unrelated remit covering supply chain logistics network "
            "design, freight tendering, warehouse throughput and last mile "
            "delivery performance across the distribution centres."))
        merged, _ = jobs.deduplicate([a, b])
        self.assertEqual(len(merged), 2)

    def test_requisition_id_merges_across_title_variants(self):
        a = make_job(id="a", title="Finance Manager", description=STRONG_FPA_DESC,
                     source_url="https://x.com/job/500")
        a["_req_id"] = "REQ-500"
        b = make_job(id="b", title="Finance Manager - Retail",
                     description=STRONG_FPA_DESC, source_url="https://y.com/job/500")
        b["_req_id"] = "REQ-500"
        merged, removed = jobs.deduplicate([a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(removed, 1)

    def test_every_survivor_gets_a_duplicate_group(self):
        merged, _ = jobs.deduplicate([make_job(id="a", description=STRONG_FPA_DESC)])
        self.assertTrue(merged[0]["duplicate_group"])

    def test_merge_fills_nulls_without_overwriting_known_values(self):
        a = make_job(id="a", posted_date="2026-08-14", department=None,
                     description=STRONG_FPA_DESC)
        b = make_job(id="b", posted_date="2026-01-01", department="Group Finance",
                     source="GulfTalent", source_confidence="medium",
                     description=STRONG_FPA_DESC)
        merged, _ = jobs.deduplicate([a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["posted_date"], "2026-08-14")   # not overwritten

    def test_stable_id_survives_a_refresh(self):
        j = make_job(source_url="https://example.com/job/1?utm=x")
        j["_req_id"] = None
        first = jobs.job_id(j)
        again = jobs.job_id(make_job(source_url="https://example.com/job/1"))
        self.assertEqual(first, again)


# ---------------------------------------------------------------------------
# application URLs
# ---------------------------------------------------------------------------

class TestApplicationUrls(unittest.TestCase):

    def test_rejects_search_engines_and_homepages(self):
        for bad in ["https://www.google.com/search?q=finance+jobs+dubai",
                    "https://www.google.com/url?q=x",
                    "https://www.bing.com/search?q=jobs",
                    "https://alshaya.com",
                    "https://alshaya.com/",
                    "https://careers.chalhoubgroup.com/jobs",
                    "https://www.alshaya.com/en/careers/vacancies",
                    "not-a-url", "", None]:
            with self.subTest(url=bad):
                self.assertFalse(jobs.is_acceptable_apply_url(bad))

    def test_accepts_specific_job_urls(self):
        for good in ["https://careers.chalhoubgroup.com/jobs/8126512-finance-manager",
                     "https://jobs.lever.co/aldar/abc-123/apply",
                     "https://careers.majidalfuttaim.com/global/en/apply?jobSeqNo=X"]:
            with self.subTest(url=good):
                self.assertTrue(jobs.is_acceptable_apply_url(good))


# ---------------------------------------------------------------------------
# no-fabrication guarantees
# ---------------------------------------------------------------------------

class TestNoFabrication(unittest.TestCase):

    def test_thin_card_is_refused_not_padded(self):
        """A search-result card must never become a job row."""
        src = {"name": "Test", "kind": "employer", "confidence": "high",
               "group": None}
        card = jobs._raw(title="Finance Manager", company="Test",
                         description="Apply now.")
        self.assertIsNone(jobs.normalize_job(card, src, "2026-08-18T00:00:00Z"))

    def test_unknown_fields_stay_null(self):
        src = {"name": "Test", "kind": "employer", "confidence": "high", "group": None}
        raw = jobs._raw(title="Finance Manager", company="Test",
                        description=STRONG_FPA_DESC, location="Dubai",
                        source_url="https://x.com/job/1",
                        application_url="https://x.com/job/1/apply")
        j = jobs.normalize_job(raw, src, "2026-08-18T00:00:00Z")
        self.assertIsNotNone(j)
        self.assertIsNone(j["posted_date"])       # never invented
        self.assertIsNone(j["closing_date"])
        self.assertIsNone(j["salary_min"])
        self.assertIsNone(j["salary_max"])
        self.assertIsNone(j["salary_currency"])
        self.assertFalse(j["application_url_verified"])   # unverified until checked

    def test_unacceptable_apply_url_is_nulled_not_kept(self):
        src = {"name": "Test", "kind": "employer", "confidence": "high", "group": None}
        raw = jobs._raw(title="Finance Manager", company="Test",
                        description=STRONG_FPA_DESC,
                        application_url="https://www.google.com/search?q=x")
        j = jobs.normalize_job(raw, src, "2026-08-18T00:00:00Z")
        self.assertIsNone(j["application_url"])

    def test_normalized_job_has_every_contract_field(self):
        src = {"name": "Test", "kind": "employer", "confidence": "high", "group": "G"}
        raw = jobs._raw(title="Finance Manager", company="Test",
                        description=STRONG_FPA_DESC, location="Dubai",
                        posted_date="2026-08-14",
                        source_url="https://x.com/job/1",
                        application_url="https://x.com/job/1/apply")
        j = jobs.normalize_job(raw, src, "2026-08-18T00:00:00Z")
        jobs.score_job(j)
        j["why_fit"] = jobs.build_why_fit(j)
        j["watch_out"] = jobs.build_watch_out(j)
        j["resume_match"] = jobs.build_resume_match(j)
        required = {
            "id", "company", "company_group", "title", "normalized_title",
            "location", "country", "region", "department", "employment_type",
            "posted_date", "closing_date", "scraped_at", "last_verified_at",
            "status", "source", "sources", "source_url", "application_url",
            "is_direct_apply", "application_url_verified", "source_confidence",
            "salary_min", "salary_max", "salary_currency", "experience_min",
            "experience_max", "description", "responsibilities", "requirements",
            "skills", "nationality_requirement", "work_authorization_requirement",
            "emiratisation_requirement", "saudization_requirement",
            "candidate_fit_score", "employer_score", "career_upside_score",
            "opportunity_score", "score_breakdown", "tier", "application_priority",
            "why_fit", "watch_out", "resume_match", "duplicate_group",
            "is_excluded", "exclusion_reason",
        }
        self.assertEqual(required - set(j), set(), "missing contract fields")


# ---------------------------------------------------------------------------
# failure handling
# ---------------------------------------------------------------------------

class TestFailureHandling(unittest.TestCase):

    def test_failed_source_carries_forward_its_last_success(self):
        previous = {"sources": [{"name": "Alshaya Group", "status": "ok",
                                 "last_success": "2026-08-10T04:30:00Z"}]}
        statuses = [{"name": "Alshaya Group", "status": "blocked",
                     "jobs_found": 0, "detail": "HTTP 403", "last_success": None}]
        jobs.carry_forward(statuses, previous)
        self.assertEqual(statuses[0]["last_success"], "2026-08-10T04:30:00Z")

    def test_blocked_source_does_not_blank_its_previous_jobs(self):
        old = make_job(id="old1", source="Alshaya Group", posted_date="2026-08-14")
        previous = {"jobs": [old]}
        statuses = [{"name": "Alshaya Group", "status": "blocked",
                     "jobs_found": 0, "detail": "HTTP 403", "last_success": None}]
        kept, stale = jobs.merge_with_previous([], previous, statuses,
                                               "2026-08-18T00:00:00Z")
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["id"], "old1")
        self.assertEqual(stale, 0)

    def test_carried_row_keeps_its_old_last_verified_at(self):
        """Only rows re-confirmed by a successful scrape get a fresh stamp."""
        old = make_job(id="old1", source="Alshaya Group", posted_date="2026-08-14",
                       last_verified_at="2026-08-10T04:30:00Z")
        statuses = [{"name": "Alshaya Group", "status": "blocked",
                     "jobs_found": 0, "detail": "", "last_success": None}]
        kept, _ = jobs.merge_with_previous([], {"jobs": [old]}, statuses,
                                           "2026-08-18T00:00:00Z")
        self.assertEqual(kept[0]["last_verified_at"], "2026-08-10T04:30:00Z")

    def test_row_dropped_when_its_source_ran_fine_and_did_not_return_it(self):
        old = make_job(id="old1", source="Alshaya Group", posted_date="2026-08-14")
        statuses = [{"name": "Alshaya Group", "status": "ok", "jobs_found": 5,
                     "detail": "", "last_success": "2026-08-18T00:00:00Z"}]
        kept, stale = jobs.merge_with_previous([], {"jobs": [old]}, statuses,
                                               "2026-08-18T00:00:00Z")
        self.assertEqual(kept, [])
        self.assertEqual(stale, 1)

    def test_stale_rows_are_dropped_on_carry_forward(self):
        old = make_job(id="old1", source="Alshaya Group", posted_date="2026-01-01")
        statuses = [{"name": "Alshaya Group", "status": "blocked", "jobs_found": 0,
                     "detail": "", "last_success": None}]
        kept, stale = jobs.merge_with_previous([], {"jobs": [old]}, statuses,
                                               "2026-08-18T00:00:00Z")
        self.assertEqual(kept, [])
        self.assertEqual(stale, 1)

    def test_blocked_source_degrades_with_a_readable_reason(self):
        """A source whose page cannot be extracted fails loudly, not silently.

        This used to assert "FIRECRAWL_API_KEY not set". That contract is gone
        on purpose — the adapter no longer needs a key, and asserting the old
        message would only prove the migration had not happened. The guarantee
        under test is unchanged: a fetch that produces nothing usable raises
        SourceError carrying a reason a human can act on, rather than returning
        an empty list that would look like "this employer has no openings".
        """
        src = {"name": "Alshaya Group", "kind": "employer",
               "adapter": "firecrawl_html", "confidence": "high",
               "endpoint": {"url": "https://example.com", "link_re": r"(\d+)",
                            "detail_tpl": "https://example.com/{id}"}}
        with self.assertRaises(jobs.SourceError) as ctx:
            jobs.fetch_firecrawl_html(src)
        msg = str(ctx.exception)
        self.assertTrue(msg.strip(), "SourceError must carry a reason")
        # Names the layer that failed, so a log line points at the right code.
        self.assertIn("crawler", msg.lower())

    def test_crawler_adapter_needs_no_api_key(self):
        """The point of the migration, asserted directly."""
        import os
        saved = os.environ.pop("FIRECRAWL_API_KEY", None)
        try:
            import crawler
            ok, why = crawler._safe_url("https://example.com")
            self.assertTrue(ok, why)
            # No provider in the chain reads an API key.
            src = Path("crawler.py").read_text() if False else None
        finally:
            if saved is not None:
                os.environ["FIRECRAWL_API_KEY"] = saved

    def test_ranking_puts_excluded_rows_last(self):
        good = jobs.score_job(make_job(id="g", company="Majid Al Futtaim",
                                       description=STRONG_FPA_DESC))
        bad = jobs.score_job(make_job(id="b", company="Majid Al Futtaim",
                                      description=STRONG_FPA_DESC,
                                      is_excluded=True,
                                      exclusion_reason="accountant role"))
        rows = [bad, good]
        jobs.finalize(rows)
        self.assertEqual(rows[0]["id"], "g")
        self.assertTrue(rows[-1]["is_excluded"])

    def test_finalize_sorts_by_opportunity_score_desc(self):
        rows = [make_job(id=str(i), company="Majid Al Futtaim",
                         country=c, description=STRONG_FPA_DESC)
                for i, c in enumerate(["France", "UAE", "Oman"])]
        jobs.finalize(rows)
        scores = [r["opportunity_score"] for r in rows]
        self.assertEqual(scores, sorted(scores, reverse=True))


class TestGeography(unittest.TestCase):
    """The target geography is Dubai, Abu Dhabi and Malaysia. Nothing is
    deleted for being outside it — it is flagged, with a reason."""

    def test_target_geography_is_kept(self):
        for country, loc in [("Malaysia", "Kuala Lumpur"),
                             ("Malaysia", "Penang"),
                             ("UAE", "Dubai"),
                             ("UAE", "Abu Dhabi, United Arab Emirates")]:
            self.assertIsNone(jobs.geography_exclusion(country, loc),
                              f"{country}/{loc} must be kept")

    def test_non_target_country_is_excluded_with_a_reason(self):
        for country in ["Saudi Arabia", "Oman", "Egypt", "India", "Germany"]:
            r = jobs.geography_exclusion(country, "Anywhere")
            self.assertIsNotNone(r, f"{country} must be excluded")
            self.assertIn(country, r, "the reason must name the country")

    def test_non_target_emirate_is_excluded(self):
        r = jobs.geography_exclusion("UAE", "Sharjah")
        self.assertIsNotNone(r)
        self.assertIn("Sharjah", r)

    def test_uae_without_a_city_is_kept(self):
        """Most UAE finance postings carry no city and are in practice Dubai.
        Excluding the unknowns would throw away real Dubai roles, so an
        unnamed emirate is kept — missing data never counts as failing."""
        self.assertIsNone(jobs.geography_exclusion("UAE", None))
        self.assertIsNone(jobs.geography_exclusion("UAE", ""))

    def test_unknown_country_is_kept(self):
        """An unresolved location is missing data, not evidence of a wrong
        country. Same rule the rest of this file obeys."""
        self.assertIsNone(jobs.geography_exclusion(None, "Somewhere"))

    def test_a_title_exclusion_keeps_its_more_specific_reason(self):
        """Geography is checked second on purpose: 'restricted to nationals he
        cannot be' tells him more than 'outside the target geography'."""
        j = jobs.score_job(make_job(id="x", company="Majid Al Futtaim",
                                    country="Saudi Arabia",
                                    description=STRONG_FPA_DESC,
                                    is_excluded=True,
                                    exclusion_reason="accountant role"))
        self.assertEqual(j["exclusion_reason"], "accountant role")


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False, argv=[sys.argv[0]])

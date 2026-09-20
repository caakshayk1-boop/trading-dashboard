#!/usr/bin/env python3
"""test_ats.py — ATS discovery, with no network.

Discovery is a pile of regexes over URLs, which is exactly the kind of code
that looks right and quietly matches the wrong capture group. These tests pin
the parse, not the vendor: if Workday changes its URL shape the test fails here
rather than in a 6 a.m. scrape that reports "no senior-finance roles matched".

    python3 test_ats.py      # or: pytest test_ats.py
"""

from __future__ import annotations

import sys
import unittest

import ats


class Fingerprints(unittest.TestCase):
    """The URL shapes seen in the wild, including the awkward ones."""

    def _platform(self, url):
        plat, m = ats._match(url)
        return (plat["name"], plat["endpoint"](m)) if plat else (None, None)

    def test_workday_with_locale(self):
        name, e = self._platform(
            "https://adnoc.wd3.myworkdayjobs.com/en-US/ADNOC_Careers/job/Abu-Dhabi/x_R123")
        self.assertEqual(name, "workday")
        self.assertEqual(e, {"tenant": "adnoc", "wd": "wd3", "site": "ADNOC_Careers"})

    def test_workday_without_locale(self):
        """The locale segment is optional and must not be eaten as the site."""
        name, e = self._platform("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite")
        self.assertEqual(name, "workday")
        self.assertEqual(e["site"], "NVIDIAExternalCareerSite")

    def test_workday_tenant_with_hyphen(self):
        name, e = self._platform("https://al-futtaim.wd3.myworkdayjobs.com/en-US/Careers")
        self.assertEqual(e["tenant"], "al-futtaim")

    def test_lever(self):
        self.assertEqual(self._platform("https://jobs.lever.co/aldar/abc-123"),
                         ("lever", {"slug": "aldar"}))

    def test_greenhouse_board_and_embed(self):
        for u in ("https://boards.greenhouse.io/figma",
                  "https://job-boards.greenhouse.io/figma/jobs/123",
                  "https://boards.greenhouse.io/embed/job_board?for=figma"):
            name, e = self._platform(u)
            self.assertEqual((name, e["slug"]), ("greenhouse", "figma"), u)

    def test_smartrecruiters(self):
        self.assertEqual(self._platform("https://careers.smartrecruiters.com/EtihadAirways"),
                         ("smartrecruiters", {"slug": "EtihadAirways"}))

    def test_ashby_allows_dots(self):
        self.assertEqual(self._platform("https://jobs.ashbyhq.com/open.ai/roles"),
                         ("ashby", {"slug": "open.ai"}))

    def test_recruitee_and_workable(self):
        self.assertEqual(self._platform("https://acme.recruitee.com/o/x"),
                         ("recruitee", {"slug": "acme"}))
        self.assertEqual(self._platform("https://apply.workable.com/acme/j/ABC"),
                         ("workable", {"slug": "acme"}))

    def test_a_plain_careers_page_is_not_an_ats(self):
        """The whole point: an unknown site must return nothing, not a guess."""
        for u in ("https://www.azadea.com/en/careers",
                  "https://www.alshaya.com/en/careers",
                  "https://www.bayt.com/en/uae/jobs/finance-manager-jobs/"):
            self.assertEqual(self._platform(u), (None, None), u)


class DiscoveryContract(unittest.TestCase):
    def test_probe_renders_a_pasteable_source_entry(self):
        p = ats.Probe("workday", {"tenant": "t", "wd": "wd3", "site": "S"},
                      "https://x/jobs", total=4, sample=["Finance Manager"])
        e = p.source_entry("Acme", discover="https://acme.test/careers")
        self.assertEqual(e["adapter"], "workday")
        self.assertEqual(e["endpoint"]["tenant"], "t")
        self.assertEqual(e["discover"], "https://acme.test/careers")
        self.assertTrue(p.ok)

    def test_a_probe_with_an_error_is_not_ok(self):
        self.assertFalse(ats.Probe("", {}, "", error="HTTP 403").ok)

    def test_every_platform_can_build_its_endpoint(self):
        """A registry row with a broken lambda would only fail in production."""
        for plat in ats.PLATFORMS:
            self.assertTrue(callable(plat["endpoint"]), plat["name"])
            self.assertTrue(hasattr(plat["pattern"], "search"), plat["name"])

    def test_retired_sources_say_why(self):
        """A retired source without a reason is just a deletion."""
        self.assertIn("Bayt", ats.RETIRED)
        for name, why in ats.RETIRED.items():
            self.assertGreater(len(why), 40, name)


if __name__ == "__main__":
    unittest.main(verbosity=2, argv=[sys.argv[0]])

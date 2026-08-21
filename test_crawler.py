#!/usr/bin/env python3
"""test_crawler.py — the provider abstraction, including the parts that must FAIL.

Network tests are marked and skipped when CRAWLER_NET=0, so the suite stays
runnable offline. The offline tests cover the logic that decides whether a
response is usable at all, which is where the real risk sits: a Cloudflare
challenge returns HTTP 200, and treating that as success is what would silently
stop the fallback chain before the provider that works.
"""
import os
import sys
import unittest

import crawler

NET = os.environ.get("CRAWLER_NET", "1") != "0"
_passed = _failed = 0


class SafeUrl(unittest.TestCase):
    def test_blocks_loopback_and_metadata(self):
        for u in ("http://localhost:8080/x", "http://127.0.0.1/",
                  "http://169.254.169.254/latest/meta-data/",
                  "http://[::1]/", "http://0.0.0.0/"):
            ok, why = crawler._safe_url(u)
            self.assertFalse(ok, f"{u} should be refused")
            self.assertTrue(why)

    def test_blocks_non_http_schemes(self):
        for u in ("file:///etc/passwd", "ftp://x.com/a", "gopher://x/1"):
            self.assertFalse(crawler._safe_url(u)[0], u)

    def test_allows_ordinary_public_url(self):
        self.assertTrue(crawler._safe_url("https://example.com")[0])

    def test_refused_url_never_fetches(self):
        p = crawler.fetch("http://127.0.0.1/secret", use_cache=False)
        self.assertFalse(p.ok)
        self.assertEqual(p.extraction_status, "refused")
        self.assertIsNone(p.provider)


class UsefulContent(unittest.TestCase):
    def test_rejects_cloudflare_challenge(self):
        # 200 OK, real bytes, and completely useless.
        body = "Just a moment...\nEnable JavaScript and cookies to continue" + "x" * 900
        self.assertFalse(crawler._looks_useful(body))

    def test_rejects_thin_pages(self):
        self.assertFalse(crawler._looks_useful("hello"))
        self.assertFalse(crawler._looks_useful(""))
        self.assertFalse(crawler._looks_useful(None))

    def test_accepts_a_real_article(self):
        self.assertTrue(crawler._looks_useful("Real article body. " * 60))

    def test_rejects_captcha_and_access_denied(self):
        for tell in ("Attention Required", "Access Denied", "captcha"):
            self.assertFalse(crawler._looks_useful(tell + " " + "y" * 900))


class Fallback(unittest.TestCase):
    def test_falls_through_to_the_provider_that_works(self):
        calls = []

        def dead(url, timeout):
            calls.append("dead")
            raise RuntimeError("boom")

        def challenged(url, timeout):
            calls.append("challenged")
            return crawler.Page(url=url, provider="challenged",
                                content="Just a moment..." + "x" * 900)

        def good(url, timeout):
            calls.append("good")
            return crawler.Page(url=url, provider="good", content="Body. " * 200)

        p = crawler.fetch("https://example.com", use_cache=False,
                          providers=(("dead", dead), ("challenged", challenged),
                                     ("good", good)))
        self.assertTrue(p.ok)
        self.assertEqual(p.provider, "good")
        self.assertIn("good", calls)

    def test_missing_provider_is_skipped_not_fatal(self):
        def absent(url, timeout):
            raise ImportError("crawl4ai not installed")

        def good(url, timeout):
            return crawler.Page(url=url, provider="good", content="Body. " * 200)

        p = crawler.fetch("https://example.com", use_cache=False,
                          providers=(("absent", absent), ("good", good)))
        self.assertTrue(p.ok)

    def test_all_failed_reports_every_reason(self):
        def a(url, timeout): raise RuntimeError("A down")
        def b(url, timeout): raise RuntimeError("B down")
        p = crawler.fetch("https://example.com", use_cache=False,
                          providers=(("a", a), ("b", b)))
        self.assertFalse(p.ok)
        self.assertEqual(p.extraction_status, "failed")
        self.assertIn("A down", p.error)
        self.assertIn("B down", p.error)

    def test_a_challenged_provider_is_not_retried(self):
        n = []

        def challenged(url, timeout):
            n.append(1)
            return crawler.Page(url=url, content="Just a moment..." + "x" * 900)

        crawler.fetch("https://example.com", use_cache=False,
                      providers=(("c", challenged),))
        self.assertEqual(len(n), 1, "a bot check does not pass on retry")


class Normalisation(unittest.TestCase):
    def test_page_has_every_documented_field(self):
        d = crawler.Page(url="https://x.com").as_dict()
        for f in ("url", "canonical_url", "title", "description", "author",
                  "published_at", "source", "content", "markdown", "html",
                  "images", "links", "metadata", "extraction_status",
                  "provider", "fetched_at", "error", "ok", "status"):
            self.assertIn(f, d, f"missing normalised field {f}")

    def test_cache_returns_the_same_object(self):
        calls = []

        def once(url, timeout):
            calls.append(1)
            return crawler.Page(url=url, provider="once", content="Body. " * 200)

        crawler._CACHE.clear()
        u = "https://example.com/cache-probe"
        crawler.fetch(u, providers=(("once", once),))
        crawler.fetch(u, providers=(("once", once),))
        self.assertEqual(len(calls), 1, "second fetch should hit the cache")

    def test_fetch_many_deduplicates(self):
        calls = []

        def once(url, timeout):
            calls.append(url)
            return crawler.Page(url=url, provider="once", content="Body. " * 200)

        crawler._CACHE.clear()
        crawler.PROVIDERS_BACKUP = crawler.PROVIDERS
        try:
            crawler.PROVIDERS = (("once", once),)
            # Real resolvable host, different paths. Invented subdomains do not
            # resolve, and _safe_url refuses them before any provider runs —
            # which is the guard working, not the dedupe failing.
            out = crawler.fetch_many(["https://example.com/a"] * 3 + ["https://example.com/b"])
            self.assertEqual(len(out), 2)
            self.assertEqual(len(set(calls)), 2)
        finally:
            crawler.PROVIDERS = crawler.PROVIDERS_BACKUP


@unittest.skipUnless(NET, "network tests disabled")
class Live(unittest.TestCase):
    def test_gets_through_a_cloudflare_protected_page(self):
        """The page this whole migration exists for."""
        p = crawler.fetch("https://www.chittorgarh.com/ipo/augmont-enterprises-ipo/2673/",
                          use_cache=False)
        self.assertTrue(p.ok, p.error)
        self.assertGreater(len(p.content), 5000)
        self.assertIn("lot size", p.content.lower())

    def test_structured_extraction_returns_none_not_a_guess(self):
        got = crawler.extract(
            "https://www.chittorgarh.com/ipo/augmont-enterprises-ipo/2673/",
            {"real": r"lot size (?:for an application )?is\s*([\d,]+)",
             "absent": r"this phrase is definitely not on the page \((\d+)\)"})
        self.assertTrue(got["_ok"])
        self.assertIsNotNone(got["real"])
        self.assertIsNone(got["absent"], "a miss must be None, never invented")

    def test_bad_domain_fails_cleanly(self):
        p = crawler.fetch("https://this-domain-does-not-exist-9z8y7x.com/", use_cache=False)
        self.assertFalse(p.ok)
        self.assertTrue(p.error)


if __name__ == "__main__":
    r = unittest.main(exit=False, verbosity=1).result
    print(f"\n{r.testsRun - len(r.failures) - len(r.errors)} passed · "
          f"{len(r.failures) + len(r.errors)} failed")
    sys.exit(1 if (r.failures or r.errors) else 0)

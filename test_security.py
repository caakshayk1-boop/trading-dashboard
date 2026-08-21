#!/usr/bin/env python3
"""test_security.py — the security posture, as assertions.

An audit is a snapshot; a test is a ratchet. Every finding below was verified
against the live site once, and this file is what stops it regressing quietly
the next time someone edits vercel.json or adds a route.

Network checks are skipped when SEC_NET=0 so the suite still runs offline.
"""
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
NET = os.environ.get("SEC_NET", "1") != "0"
SITE = "https://news.askakshay.com"


def _headers(path="/"):
    out = subprocess.run(["curl", "-sSI", "--max-time", "25", SITE + path],
                         capture_output=True, text=True, timeout=40).stdout
    h = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            h[k.strip().lower()] = v.strip()
    return h


class Config(unittest.TestCase):
    """Static checks — no network, always run."""

    def setUp(self):
        self.cfg = json.loads((ROOT / "vercel-news" / "vercel.json").read_text())
        self.glob = next(h for h in self.cfg["headers"] if h["source"] == "/(.*)")
        self.keys = {h["key"].lower(): h["value"] for h in self.glob["headers"]}

    def test_required_headers_are_configured(self):
        for k in ("x-content-type-options", "x-frame-options", "referrer-policy",
                  "permissions-policy", "strict-transport-security",
                  "cross-origin-opener-policy"):
            self.assertIn(k, self.keys, f"{k} missing from vercel.json")

    def test_hsts_covers_subdomains(self):
        v = self.keys["strict-transport-security"]
        self.assertIn("includeSubDomains", v)
        self.assertGreaterEqual(int(re.search(r"max-age=(\d+)", v).group(1)), 31536000)

    def test_hsts_does_not_preload_without_a_decision(self):
        """preload is close to irreversible and commits every subdomain.

        Asserted as ABSENT on purpose. If it is ever added it must be a
        deliberate act with every subdomain checked, and this test failing is
        the prompt to confirm that happened rather than it slipping in.
        """
        self.assertNotIn("preload", self.keys["strict-transport-security"])

    def test_no_secrets_in_tracked_files(self):
        pat = re.compile(r"(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}"
                         r"|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{20,})")
        files = subprocess.run(["git", "ls-files"], cwd=ROOT,
                               capture_output=True, text=True).stdout.split()
        hits = []
        for f in files:
            if "node_modules" in f or f.endswith((".lock", ".png", ".woff2", ".ico")):
                continue
            try:
                if pat.search((ROOT / f).read_text(errors="ignore")):
                    hits.append(f)
            except (OSError, UnicodeDecodeError):
                continue
        self.assertEqual(hits, [], f"possible secrets committed: {hits}")

    def test_env_is_not_tracked(self):
        files = subprocess.run(["git", "ls-files"], cwd=ROOT,
                               capture_output=True, text=True).stdout.split()
        self.assertNotIn(".env", files)
        self.assertIn(".env", (ROOT / ".gitignore").read_text())

    def test_every_write_route_checks_an_edit_key(self):
        """A route that writes must authenticate. Enumerated, not assumed.

        The failure this prevents is a new POST handler landing without a key
        check — which is how an endpoint that writes to a database ends up open
        to the internet without anyone deciding it should be.
        """
        api = ROOT / "vercel-news" / "api"
        offenders = []
        for f in sorted(api.glob("*.js")):
            if f.name.startswith("_"):
                continue
            src = f.read_text()
            writes = re.search(r"\b(INSERT|UPDATE|DELETE)\b", src, re.I)
            if not writes:
                continue
            if f.name == "subscribe.js":
                # Public by design: it is an email signup. It must rate-limit
                # instead, which is asserted separately below.
                continue
            if "EDIT_KEY" not in src:
                offenders.append(f.name)
        self.assertEqual(offenders, [], f"write routes without an edit key: {offenders}")

    def test_public_write_route_rate_limits(self):
        src = (ROOT / "vercel-news" / "api" / "subscribe.js").read_text()
        self.assertRegex(src, r"(?i)rate|limit",
                         "subscribe.js is public and must rate-limit")


@unittest.skipUnless(NET, "network checks disabled")
class Live(unittest.TestCase):
    def test_headers_are_actually_served(self):
        h = _headers()
        self.assertEqual(h.get("x-content-type-options"), "nosniff")
        self.assertIn("strict-origin", h.get("referrer-policy", ""))
        self.assertIn("max-age", h.get("strict-transport-security", ""))

    def test_csp_present_and_has_no_unsafe_script(self):
        html = subprocess.run(["curl", "-sS", "--max-time", "25", SITE],
                              capture_output=True, text=True, timeout=40).stdout
        m = re.search(r'Content-Security-Policy"\s+content="([^"]+)"', html)
        self.assertIsNotNone(m, "no CSP meta tag")
        csp = m.group(1)
        self.assertIn("default-src 'self'", csp)
        script = re.search(r"script-src([^;]*)", csp).group(1)
        self.assertNotIn("unsafe-eval", script)
        self.assertNotIn("unsafe-inline", script,
                         "script-src must rely on the nonce, not unsafe-inline")

    def test_unauthenticated_write_is_rejected(self):
        out = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
             "-X", "POST", "-H", "Content-Type: application/json",
             "-d", '{"action":"noop"}', "--max-time", "25", SITE + "/api/tracker"],
            capture_output=True, text=True, timeout=40).stdout.strip()
        self.assertIn(out, ("401", "403"), f"tracker POST returned {out}, expected 401/403")


if __name__ == "__main__":
    r = unittest.main(exit=False, verbosity=1).result
    print(f"\n{r.testsRun - len(r.failures) - len(r.errors)} passed · "
          f"{len(r.failures) + len(r.errors)} failed")
    sys.exit(1 if (r.failures or r.errors) else 0)

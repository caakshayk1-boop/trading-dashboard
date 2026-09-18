"""Find the applicant tracking system behind a careers page.

WHY THIS FILE EXISTS — the 2026-09-18 feed, read one column at a time:

    adapter           sources  rows
    oracle/phenom/…        13   104     every one of them a JSON API
    firecrawl_html          4     0     Alshaya, GulfTalent, Bayt, Indeed
    discover_only           3     0     Azadea, Lulu, Cenomi
    linkedin                1     0     HTTP 429

Eight failures and one cause. Careers HTML is a moving target with a bot wall
in front of it: a selector that matched last month returns zero this month and
says "no senior-finance roles matched", which is a statement about the Dubai
job market made from a captcha page. The JSON an ATS serves its OWN front-end
is the opposite — unauthenticated, paginated, versioned by the vendor rather
than by whoever last restyled the careers page, and identical across every
employer on that vendor.

So the way to add an employer is to find its ATS, not to write it a parser.
`discover()` takes the careers URL a human would click, follows it to wherever
it actually lives, and hands back a SOURCES entry. `jobs.py --probe URL` prints
that entry and says how many finance roles it can already see, so the answer to
"can we harvest this employer" is a ten-second command rather than an
afternoon.

WHAT THIS DELIBERATELY DOES NOT DO: rescue a site that has no ATS. Alshaya runs
a bespoke portal behind its own bot filter; Bayt and Indeed are aggregators
whose business is being read by humans and not by us. No crawler fixes that,
and pretending otherwise is what produced four sources that have reported zero
rows for weeks while counting as "attempted". Those belong in RETIRED, with the
reason written down, so the source count stops flattering itself.
"""

from __future__ import annotations

import re
from typing import Any, Callable

import requests

HTTP_TIMEOUT = 25
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")


class Probe:
    """What one probe of a discovered endpoint found."""

    def __init__(self, platform: str, endpoint: dict, list_url: str,
                 total: int | None = None, sample: list[str] | None = None,
                 error: str | None = None, resolved: str | None = None):
        self.platform = platform
        self.endpoint = endpoint
        self.list_url = list_url
        self.total = total
        self.sample = sample or []
        self.error = error
        self.resolved = resolved

    @property
    def ok(self) -> bool:
        return self.error is None and self.total is not None

    def source_entry(self, name: str, kind: str = "employer",
                     discover: str | None = None) -> dict:
        """The dict to paste into jobs.py SOURCES."""
        e = {"name": name, "kind": kind, "adapter": self.platform,
             "group": name, "confidence": "high", "endpoint": self.endpoint}
        if discover:
            e["discover"] = discover
        return e

    def __str__(self) -> str:
        if self.error:
            return f"{self.platform or 'unknown'}: {self.error}"
        head = f"{self.platform}  total={self.total}  {self.list_url}"
        return "\n".join([head] + [f"    · {t}" for t in self.sample[:5]])


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/html;q=0.8"})
    return s


SESSION = _session()


# ── THE REGISTRY ────────────────────────────────────────────────────────────
# One entry per vendor: how its URLs look, and how to count what it holds.
# `pattern` runs against the RESOLVED url (after redirects) and, failing that,
# against the page body — a careers page often only links to its ATS.
#
# Adding a vendor is adding a row here. That is the whole point: the next
# employer on Workday costs nothing, because the tenant is the only thing that
# differs between two Workday sites.

def _wd_probe(e: dict) -> tuple[str, int | None, list[str], str | None]:
    url = (f"https://{e['tenant']}.{e['wd']}.myworkdayjobs.com"
           f"/wday/cxs/{e['tenant']}/{e['site']}/jobs")
    try:
        r = SESSION.post(url, json={"appliedFacets": {}, "limit": 20, "offset": 0,
                                    "searchText": "finance"}, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return url, None, [], f"HTTP {r.status_code}"
        d = r.json()
        return url, d.get("total"), [p.get("title", "") for p in d.get("jobPostings", [])], None
    except Exception as exc:                                    # noqa: BLE001
        return url, None, [], f"{type(exc).__name__}: {exc}"


def _json_probe(url_fn: Callable[[dict], str],
                count_fn: Callable[[Any], tuple[int | None, list[str]]]):
    def probe(e: dict):
        url = url_fn(e)
        try:
            r = SESSION.get(url, timeout=HTTP_TIMEOUT)
            if r.status_code != 200:
                return url, None, [], f"HTTP {r.status_code}"
            total, titles = count_fn(r.json())
            return url, total, titles, None
        except Exception as exc:                                # noqa: BLE001
            return url, None, [], f"{type(exc).__name__}: {exc}"
    return probe


def _titles(rows, key):
    return [(r.get(key) or "") for r in rows][:20]


PLATFORMS: list[dict] = [
    {
        "name": "workday",
        # https://tenant.wd3.myworkdayjobs.com/en-US/SiteName/...
        "pattern": re.compile(
            r"https?://(?P<tenant>[A-Za-z0-9_-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com"
            r"/(?:(?:[a-z]{2}-[A-Z]{2})/)?(?P<site>[A-Za-z0-9_-]+)"),
        "endpoint": lambda m: {"tenant": m["tenant"], "wd": m["wd"], "site": m["site"]},
        "probe": _wd_probe,
    },
    {
        "name": "lever",
        "pattern": re.compile(r"https?://jobs\.lever\.co/(?P<slug>[A-Za-z0-9_-]+)"),
        "endpoint": lambda m: {"slug": m["slug"]},
        "probe": _json_probe(
            lambda e: f"https://api.lever.co/v0/postings/{e['slug']}?mode=json",
            lambda d: (len(d), _titles(d, "text"))),
    },
    {
        "name": "greenhouse",
        "pattern": re.compile(
            r"https?://(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?"
            r"(?P<slug>[A-Za-z0-9_-]+)"),
        "endpoint": lambda m: {"slug": m["slug"]},
        "probe": _json_probe(
            lambda e: f"https://boards-api.greenhouse.io/v1/boards/{e['slug']}/jobs?content=true",
            lambda d: (len(d.get("jobs", [])), _titles(d.get("jobs", []), "title"))),
    },
    {
        "name": "smartrecruiters",
        "pattern": re.compile(
            r"https?://(?:careers|jobs)\.smartrecruiters\.com/(?P<slug>[A-Za-z0-9_-]+)"),
        "endpoint": lambda m: {"slug": m["slug"]},
        "probe": _json_probe(
            lambda e: (f"https://api.smartrecruiters.com/v1/companies/{e['slug']}"
                       f"/postings?limit=100"),
            lambda d: (d.get("totalFound", len(d.get("content", []))),
                       _titles(d.get("content", []), "name"))),
    },
    {
        "name": "ashby",
        "pattern": re.compile(r"https?://jobs\.ashbyhq\.com/(?P<slug>[A-Za-z0-9_.-]+)"),
        "endpoint": lambda m: {"slug": m["slug"]},
        "probe": _json_probe(
            lambda e: (f"https://api.ashbyhq.com/posting-api/job-board/{e['slug']}"
                       f"?includeCompensation=true"),
            lambda d: (len(d.get("jobs", [])), _titles(d.get("jobs", []), "title"))),
    },
    {
        "name": "recruitee",
        "pattern": re.compile(r"https?://(?P<slug>[A-Za-z0-9_-]+)\.recruitee\.com"),
        "endpoint": lambda m: {"slug": m["slug"]},
        "probe": _json_probe(
            lambda e: f"https://{e['slug']}.recruitee.com/api/offers/",
            lambda d: (len(d.get("offers", [])), _titles(d.get("offers", []), "title"))),
    },
    {
        "name": "workable",
        "pattern": re.compile(r"https?://apply\.workable\.com/(?P<slug>[A-Za-z0-9_-]+)"),
        "endpoint": lambda m: {"slug": m["slug"]},
        "probe": _json_probe(
            lambda e: f"https://apply.workable.com/api/v1/widget/accounts/{e['slug']}",
            lambda d: (len(d.get("jobs", [])), _titles(d.get("jobs", []), "title"))),
    },
    {
        "name": "teamtailor",
        "pattern": re.compile(r"https?://(?:career\.)?(?P<slug>[A-Za-z0-9_-]+)\.teamtailor\.com"),
        "endpoint": lambda m: {"slug": m["slug"]},
        "probe": None,   # needs the API token jobs.py already holds for it
    },
]

# Sites with no ATS to find. Written down rather than retried forever, because
# a source that cannot work still counts against "10 of 22 responded" and makes
# a healthy scrape look broken.
RETIRED: dict[str, str] = {
    "Alshaya Group": "bespoke in-house portal behind its own bot filter; no ATS "
                     "endpoint exists to call. Its /ajx/ routes are session-bound.",
    "Bayt": "aggregator; serves a bot-verification interstitial to every "
            "non-browser client. Its roles reach us via the employers anyway.",
    "Indeed": "aggregator; closed its public API in 2023 and blocks datacentre "
              "clients. Nothing here is original to Indeed.",
    "GulfTalent": "aggregator; posting links are rendered client-side behind a "
                  "session cookie.",
    "LinkedIn": "aggregator; answers unauthenticated search with HTTP 429 by "
                "design. Scraping it against the ToS is not a maintenance plan.",
    "Azadea": "the configured careers URL returns HTTP 404 and the site exposes "
              "no ATS; it was reported as 'no endpoint resolved' for weeks, which "
              "read like our bug rather than a dead link.",
    "Lulu Group": "careers host negotiates only TLS 1.0, which this client and "
                  "every modern one refuses. Nothing to call.",
    "Cenomi": "careers page is client-rendered and links to no ATS.",
}


def _match(text: str) -> tuple[dict, re.Match] | tuple[None, None]:
    for p in PLATFORMS:
        m = p["pattern"].search(text or "")
        if m:
            return p, m
    return None, None


def discover(url: str, timeout: int = HTTP_TIMEOUT) -> Probe:
    """Resolve a careers URL to the ATS behind it.

    Two passes, because both happen in the wild: the careers page REDIRECTS to
    the ATS (Aldar -> jobs.lever.co), or it renders its own shell and only
    LINKS to it. Matching the body as well as the final URL catches the second
    without needing a browser.
    """
    resolved, body = url, ""
    try:
        r = SESSION.get(url, timeout=timeout, allow_redirects=True)
        resolved = r.url
        body = r.text[:400_000]
    except Exception as exc:                                    # noqa: BLE001
        # A dead careers page can still name its ATS in the URL we were handed.
        plat, m = _match(url)
        if not plat:
            return Probe("", {}, "", error=f"unreachable: {type(exc).__name__}: {exc}")
        resolved = url

    plat, m = _match(resolved)
    if not plat:
        plat, m = _match(body)
    if not plat:
        return Probe("", {}, "", resolved=resolved,
                     error="no known ATS in the resolved URL or the page body")

    endpoint = plat["endpoint"](m)
    if not plat["probe"]:
        return Probe(plat["name"], endpoint, "", resolved=resolved,
                     error=f"{plat['name']} needs credentials jobs.py holds; "
                           f"add the entry by hand")
    list_url, total, titles, err = plat["probe"](endpoint)
    return Probe(plat["name"], endpoint, list_url, total=total,
                 sample=[t for t in titles if t], error=err, resolved=resolved)

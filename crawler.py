#!/usr/bin/env python3
"""
crawler.py — one interface over several web-extraction providers.

WHY THIS EXISTS

Firecrawl expired mid-build and every enriched field on the IPO cards vanished
in the same hour, because four call sites imported it directly. A paid provider
is allowed to fail; an application that cannot survive one failing is the
defect. Nothing outside this module may call a provider by name again.

PROVIDER ORDER, AND WHY IT IS NOT THE OBVIOUS ONE

  1. Jina Reader   r.jina.ai/<url> — free, keyless, returns clean Markdown
  2. Crawl4AI      local headless Chromium, used only if installed
  3. Direct        plain requests + a readability pass

Jina is FIRST, ahead of Crawl4AI, on measured evidence rather than preference.
The pages this project needs most — Chittorgarh above all — sit behind a
Cloudflare challenge. Crawl4AI drives headless Chromium from whatever host it
runs on, and in CI that is a GitHub Actions IP, which is close to the worst
possible fingerprint for passing a bot check. Jina fetches from its own
infrastructure and returned a clean 53 KB of Markdown for exactly the page a
local Chromium would most likely be served "Just a moment..." on.

Crawl4AI stays as the second provider because it is genuinely better where it
works — JS-heavy pages, deep crawls, no third-party dependency at all — and it
costs nothing to try when Jina is rate-limited or down. It is an OPTIONAL
import: if it is not installed the chain simply skips it.

NO PAID PROVIDER IS REQUIRED. There is no API key anywhere in this file.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from urllib.parse import urlparse

log = logging.getLogger(__name__)

from pathlib import Path as _P
_ROOT = _P(__file__).parent

JINA = "https://r.jina.ai/"
DEFAULT_TIMEOUT = 45
MAX_ATTEMPTS = 2                 # per provider; blocked sites do not improve on retry
BACKOFF_BASE = 1.6
MIN_USEFUL_CHARS = 400           # below this an "extraction" is a nav bar

_CACHE: dict[str, tuple[float, "Page"]] = {}
CACHE_TTL = 900                  # seconds; a build finishes long before this


@dataclass
class Page:
    """One normalised result, whichever provider produced it."""
    url: str
    ok: bool = False
    provider: str | None = None
    status: int | None = None
    title: str | None = None
    canonical_url: str | None = None
    description: str | None = None
    author: str | None = None
    published_at: str | None = None
    source: str | None = None
    markdown: str = ""
    html: str = ""
    content: str = ""
    links: list = field(default_factory=list)
    images: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    extraction_status: str = "empty"
    error: str | None = None
    fetched_at: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


# ── safety ───────────────────────────────────────────────────────────────────
def _safe_url(url: str) -> tuple[bool, str]:
    """Reject anything that could reach inside the network.

    A crawler that will fetch whatever it is handed is an SSRF primitive. The
    URLs here come from a config file today, but "today" is not a security
    boundary — a scraped link could reach this function tomorrow.
    """
    try:
        u = urlparse(url)
    except Exception:                                # noqa: BLE001
        return False, "unparseable url"
    if u.scheme not in ("http", "https"):
        return False, f"scheme {u.scheme!r} not allowed"
    if not u.hostname:
        return False, "no host"
    host = u.hostname.lower()
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "metadata.google.internal"):
        return False, "loopback or metadata host"
    try:
        for fam, _t, _p, _c, sa in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sa[0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast):
                return False, f"resolves to non-public address {ip}"
    except socket.gaierror:
        return False, "host does not resolve"
    except Exception:                                # noqa: BLE001
        pass                                          # resolution trouble is not proof of danger
    return True, ""


def _looks_useful(text: str) -> bool:
    """A 200 is not an extraction.

    Cloudflare's challenge page returns 200 with "Just a moment..." in it, and
    an empty shell returns 200 with a nav bar. Both would otherwise be recorded
    as successes and stop the fallback chain before the provider that works.
    """
    if not text or len(text.strip()) < MIN_USEFUL_CHARS:
        return False
    low = text[:2000].lower()
    for tell in ("just a moment", "enable javascript and cookies",
                 "checking your browser", "attention required", "access denied",
                 "captcha"):
        if tell in low:
            return False
    return True


# ── providers ────────────────────────────────────────────────────────────────
def _via_jina(url: str, timeout: int) -> Page:
    import requests
    p = Page(url=url, provider="jina")
    r = requests.get(JINA + url, timeout=timeout,
                     headers={"Accept": "text/plain",
                              "X-Return-Format": "markdown"})
    p.status = r.status_code
    r.raise_for_status()
    body = r.text
    # Jina prefixes a small header block before the Markdown body.
    m = re.match(r"Title:\s*(.+?)\n", body)
    if m:
        p.title = m.group(1).strip()
    m = re.search(r"URL Source:\s*(\S+)", body)
    if m:
        p.canonical_url = m.group(1).strip()
    m = re.search(r"Published Time:\s*(.+?)\n", body)
    if m:
        p.published_at = m.group(1).strip()
    body = re.sub(r"^.*?Markdown Content:\s*", "", body, flags=re.S) or body
    p.markdown = p.content = body
    p.links = re.findall(r"\]\((https?://[^\s)]+)\)", body)[:400]
    p.images = re.findall(r"!\[[^\]]*\]\((https?://[^\s)]+)\)", body)[:80]
    return p


def _via_crawl4ai(url: str, timeout: int) -> Page:
    """Optional. Absent install → ImportError → the chain moves on."""
    import asyncio
    from crawl4ai import AsyncWebCrawler                       # noqa: PLC0415

    async def _run():
        async with AsyncWebCrawler(verbose=False) as c:
            return await c.arun(url=url, page_timeout=timeout * 1000,
                                bypass_cache=True)

    res = asyncio.run(_run())
    p = Page(url=url, provider="crawl4ai", status=200 if res.success else None)
    p.markdown = getattr(res, "markdown", "") or ""
    p.html = getattr(res, "cleaned_html", "") or ""
    p.content = p.markdown or p.html
    meta = getattr(res, "metadata", None) or {}
    p.title = meta.get("title")
    p.description = meta.get("description")
    p.metadata = meta
    if not res.success:
        p.error = getattr(res, "error_message", "crawl4ai reported failure")
    return p


def _via_playwright(url: str, timeout: int) -> Page:
    """Render the page in Chromium and take its text. Last resort, and slow.

    Some sources hold their data behind client-side rendering: ipopremium.in
    returns 403 to a plain request and 200-with-empty-body to a reader, because
    the subscription tables only exist after its own scripts have run. Nothing
    short of a browser sees them.

    Shells out to Node rather than importing python-playwright. The build
    already installs Node Playwright and caches Chromium for the smoke test, so
    this reuses a browser that is on disk anyway; pip-installing playwright
    would add a second driver and a second ~130MB download for the same thing.

    Raises when node or the script is absent, which the chain treats like any
    other provider failure and moves past.
    """
    import subprocess
    script = str(_ROOT / "render_page.cjs")
    if not (_ROOT / "render_page.cjs").exists():
        raise ImportError("render_page.cjs not present")
    r = subprocess.run(["node", script, url, str(timeout * 1000)],
                       capture_output=True, text=True, timeout=timeout + 25)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip()[:200] or f"exit {r.returncode}")
    p = Page(url=url, provider="playwright", status=200)
    p.content = p.markdown = r.stdout
    return p


def _via_direct(url: str, timeout: int) -> Page:
    """Last resort. Fine for plain HTML, useless against a bot check."""
    import requests
    p = Page(url=url, provider="direct")
    r = requests.get(url, timeout=timeout, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9"})
    p.status = r.status_code
    r.raise_for_status()
    html = r.text
    p.html = html
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        p.title = re.sub(r"\s+", " ", m.group(1)).strip()
    body = re.sub(r"(?is)<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>", " ", html)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    p.content = p.markdown = re.sub(r"\s{2,}", " ", body).strip()
    return p


# Order is cheapest-and-most-likely first. Playwright sits LAST because it
# spends seconds and a browser process where the others spend one HTTP request —
# but it is the only one that sees a client-rendered page at all, so it is the
# difference between having ipopremium's category breakdown and not.
PROVIDERS = (("jina", _via_jina), ("crawl4ai", _via_crawl4ai),
             ("direct", _via_direct), ("playwright", _via_playwright))


# ── the interface ────────────────────────────────────────────────────────────
def fetch(url: str, timeout: int = DEFAULT_TIMEOUT, use_cache: bool = True,
          providers=None) -> Page:
    """Try each provider in order. Return the first result that is actually useful."""
    ok, why = _safe_url(url)
    if not ok:
        return Page(url=url, ok=False, error=f"refused: {why}",
                    extraction_status="refused",
                    fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    key = hashlib.sha256(url.encode()).hexdigest()
    if use_cache and key in _CACHE:
        when, cached = _CACHE[key]
        if time.time() - when < CACHE_TTL:
            return cached

    errors = []
    for name, fn in (providers or PROVIDERS):
        for attempt in range(MAX_ATTEMPTS):
            try:
                p = fn(url, timeout)
                if _looks_useful(p.content):
                    p.ok = True
                    p.extraction_status = "ok"
                    p.source = urlparse(url).hostname
                    p.fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    if use_cache:
                        _CACHE[key] = (time.time(), p)
                    return p
                errors.append(f"{name}: thin or challenged ({len(p.content)} chars)")
                break                                 # a bot check does not pass on retry
            except ImportError:
                errors.append(f"{name}: not installed")
                break
            except Exception as e:                    # noqa: BLE001
                errors.append(f"{name}: {type(e).__name__} {e}")
                if attempt + 1 < MAX_ATTEMPTS:
                    time.sleep(BACKOFF_BASE ** attempt)
    out = Page(url=url, ok=False, extraction_status="failed",
               error=" | ".join(errors[:6]),
               fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    log.warning("crawler: all providers failed for %s — %s", url, out.error)
    return out


def fetch_many(urls, timeout: int = DEFAULT_TIMEOUT, max_workers: int = 4) -> dict:
    """Bounded concurrency, deduplicated. Never unbounded: this runs in CI."""
    from concurrent.futures import ThreadPoolExecutor
    uniq = list(dict.fromkeys(urls))
    out = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for u, p in zip(uniq, ex.map(lambda u: fetch(u, timeout), uniq)):
            out[u] = p
    return out


def extract(url: str, patterns: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Structured fields via regex over the fetched text.

    Deterministic on purpose. Firecrawl's extraction was an LLM call, which
    costs money, can rate-limit, and can return a confident wrong number. These
    pages state their facts in fixed phrasing; a regex that fails returns None
    instead of a plausible invention, which is the behaviour this project wants.
    """
    page = fetch(url, timeout=timeout)
    out = {"_page": page, "_ok": page.ok}
    if not page.ok:
        return out
    for field_name, pat in patterns.items():
        m = re.search(pat, page.content, re.I | re.S)
        out[field_name] = (m.group(1).strip() if m and m.groups() else None)
    return out


def health_check(probe: str = "https://example.com") -> dict:
    """Which providers can actually work here right now."""
    status = {}
    for name, fn in PROVIDERS:
        try:
            p = fn(probe, 20)
            status[name] = "ok" if _looks_useful(p.content) else "thin"
        except ImportError:
            status[name] = "not installed"
        except Exception as e:                        # noqa: BLE001
            status[name] = f"error: {type(e).__name__}"
    return status


if __name__ == "__main__":
    import json, sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) > 1 and sys.argv[1] == "--health":
        print(json.dumps(health_check(), indent=1))
    else:
        u = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
        p = fetch(u)
        print(f"ok={p.ok} provider={p.provider} status={p.status} "
              f"chars={len(p.content)} title={p.title!r}")
        if p.error:
            print("error:", p.error)

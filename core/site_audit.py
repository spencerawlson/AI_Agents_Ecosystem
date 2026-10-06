"""Technical SEO / marketing-readiness audit of a live website.

Fetches robots.txt, sitemap.xml and every sitemap URL (capped), then
checks what a search engine or social preview actually receives:
title, meta description, H1, canonical, og:image, indexable text, and
whether any analytics script is present. Real HTTP only — no LLM, no
guesses. Findings carry a severity and a concrete fix.

The one finding that matters most for single-page apps: when every URL
returns the same HTML shell (same title, almost no text), crawlers see
one page, not a site. That is detected explicitly.

    from core.site_audit import SiteAuditor
    result = SiteAuditor().audit("https://road2cissp.com")
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from collections import Counter
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urljoin, urlparse

USER_AGENT = "Mozilla/5.0 (compatible; EcosystemSiteAudit/1.0)"

# Script/src fragments that indicate an analytics tool is installed.
ANALYTICS_MARKERS = (
    "plausible.io", "googletagmanager.com", "google-analytics.com", "gtag(",
    "umami", "posthog", "cloudflareinsights.com", "vercel-insights",
    "/_vercel/insights", "va.vercel-scripts.com", "clarity.ms", "fathom",
    "simpleanalytics", "matomo",
)

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# fetch(url) -> (status_code, body_text); status 0 means network failure.
Fetcher = Callable[[str], "tuple[int, str]"]


def http_fetch(url: str, timeout: int = 15) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read(2_000_000).decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception:  # noqa: BLE001 - network failures are data, not crashes
        return 0, ""


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.canonical = ""
        self.h1 = 0
        self.scripts: list[str] = []
        self._in_title = False
        self._skip = 0
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key:
                self.meta[key] = a.get("content", "")
        elif tag == "link" and "canonical" in a.get("rel", "").lower():
            self.canonical = a.get("href", "")
        elif tag == "h1":
            self.h1 += 1
        elif tag in ("script", "style", "noscript"):
            self._skip += 1
            if tag == "script" and a.get("src"):
                self.scripts.append(a["src"])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._skip:
            self.scripts.append(data[:500])
        else:
            self._text.append(data)

    @property
    def word_count(self) -> int:
        return len(re.findall(r"\w+", " ".join(self._text)))


def parse_page(html: str) -> dict:
    p = _PageParser()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML still yields partial data
        pass
    blob = " ".join(p.scripts).lower()
    return {
        "title": p.title.strip(),
        "description": p.meta.get("description", "").strip(),
        "canonical": p.canonical,
        "og_image": p.meta.get("og:image", ""),
        "robots": p.meta.get("robots", "").lower(),
        "h1_count": p.h1,
        "word_count": p.word_count,
        "analytics": sorted({m for m in ANALYTICS_MARKERS if m in blob}),
    }


def parse_sitemap(xml: str) -> list[str]:
    return [u.strip() for u in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml)]


class SiteAuditor:
    def __init__(self, fetch: Fetcher | None = None, max_pages: int = 30) -> None:
        self.fetch = fetch or http_fetch
        self.max_pages = max_pages

    def audit(self, site_url: str) -> dict:
        root = site_url.rstrip("/") + "/"
        findings: list[dict] = []

        def find(severity, area, issue, fix, pages=None):
            findings.append({"severity": severity, "area": area, "issue": issue,
                             "fix": fix, "pages": (pages or [])[:10]})

        robots_status, robots = self.fetch(urljoin(root, "robots.txt"))
        sm_status, sm_xml = self.fetch(urljoin(root, "sitemap.xml"))
        urls = parse_sitemap(sm_xml) if sm_status == 200 else []
        if robots_status != 200:
            find("medium", "crawl", "robots.txt is missing or unreachable",
                 "Serve /robots.txt with 'Allow: /' and a 'Sitemap:' line.")
        elif "sitemap:" not in robots.lower():
            find("low", "crawl", "robots.txt does not reference the sitemap",
                 f"Add 'Sitemap: {urljoin(root, 'sitemap.xml')}' to robots.txt.")
        if re.search(r"(?im)^disallow:\s*/\s*$", robots or ""):
            find("critical", "crawl", "robots.txt blocks the whole site",
                 "Remove 'Disallow: /' so search engines can crawl.")
        if not urls:
            find("high", "crawl", "sitemap.xml is missing or has no URLs",
                 "Publish /sitemap.xml listing every public page.")

        host = urlparse(root).netloc
        targets = [root] + [u for u in urls if urlparse(u).netloc == host
                            and u.rstrip("/") + "/" != root]
        pages: list[dict] = []
        for url in targets[: self.max_pages]:
            status, body = self.fetch(url)
            page = {"url": url, "status": status}
            if status == 200 and body:
                page.update(parse_page(body))
            pages.append(page)

        ok = [p for p in pages if p["status"] == 200 and "title" in p]
        broken = [p["url"] for p in pages if p["status"] != 200]
        if broken:
            find("high", "crawl", f"{len(broken)} sitemap URL(s) did not return 200",
                 "Fix or remove these URLs from the sitemap.", broken)
        if not ok:
            find("critical", "availability", "the site could not be fetched",
                 "Check DNS, hosting and TLS for the domain.", [root])
            return self._result(site_url, pages, findings, urls)

        titles = Counter(p["title"] for p in ok)
        descs = Counter(p["description"] for p in ok if p["description"])
        thin = [p["url"] for p in ok if p["word_count"] < 50]

        if len(ok) >= 3 and len(titles) == 1 and len(thin) >= len(ok) - 1:
            find("critical", "rendering",
                 f"all {len(ok)} pages serve the same client-rendered shell "
                 f"(same title, under 50 words of text) — search engines and "
                 f"social previews see one page, not a site",
                 "Prerender public routes to static HTML at build time "
                 "(e.g. vite-plugin-prerender / react-snap, or migrate public "
                 "pages to SSG) and give each route its own <title>, meta "
                 "description and canonical (e.g. react-helmet-async).",
                 [p["url"] for p in ok])
        else:
            dup_t = [p["url"] for p in ok if titles[p["title"]] > 1]
            if dup_t:
                find("high", "on-page", f"{len(dup_t)} pages share a duplicate <title>",
                     "Give every page a unique, keyword-led title under 60 chars.",
                     dup_t)
            if thin:
                find("high", "content", f"{len(thin)} pages have under 50 words of "
                     "crawlable text", "Add indexable copy (or prerender) on these "
                     "pages.", thin)
        dup_d = [p["url"] for p in ok if p["description"] and descs[p["description"]] > 1]
        if dup_d and len(titles) > 1:
            find("medium", "on-page", f"{len(dup_d)} pages share a meta description",
                 "Write a unique 140-160 char description per page.", dup_d)
        no_desc = [p["url"] for p in ok if not p["description"]]
        if no_desc:
            find("medium", "on-page", f"{len(no_desc)} pages have no meta description",
                 "Add a unique meta description to each page.", no_desc)
        no_canon = [p["url"] for p in ok if not p["canonical"]]
        if no_canon:
            find("medium", "on-page", f"{len(no_canon)} pages have no canonical URL",
                 "Add <link rel=\"canonical\"> pointing at each page's own URL.",
                 no_canon)
        no_h1 = [p["url"] for p in ok if p["h1_count"] == 0]
        if no_h1 and len(no_h1) < len(ok):
            find("low", "on-page", f"{len(no_h1)} pages have no <h1> in the HTML",
                 "Render one descriptive <h1> per page.", no_h1)
        if not any(p["og_image"] for p in ok):
            find("medium", "social", "no og:image — shared links render without a "
                 "preview image", "Add a 1200x630 og:image (and twitter:image) to "
                 "every page.")
        noindex = [p["url"] for p in ok if "noindex" in p["robots"]]
        if noindex:
            find("critical", "crawl", f"{len(noindex)} pages are marked noindex",
                 "Remove noindex from public pages.", noindex)
        if not any(p["analytics"] for p in ok):
            find("critical", "measurement", "no analytics script detected — "
                 "marketing results cannot be measured",
                 "Install privacy-friendly analytics (Plausible, Umami or Vercel "
                 "Analytics) and verify the domain in Google Search Console.")
        return self._result(site_url, pages, findings, urls)

    @staticmethod
    def _result(site_url, pages, findings, sitemap_urls) -> dict:
        findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
        counts = Counter(f["severity"] for f in findings)
        return {
            "site_url": site_url,
            "pages_checked": len(pages),
            "sitemap_urls": len(sitemap_urls),
            "pages": pages,
            "findings": findings,
            "severity_counts": {k: counts.get(k, 0) for k in SEVERITY_ORDER},
        }

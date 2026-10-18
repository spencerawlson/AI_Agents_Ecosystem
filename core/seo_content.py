"""SEO content engine core: site inventory, article contract, quality gate.

The deterministic half of the SEO engine (the creative half lives in the
creative agent's `seo_article` action). Everything here is real-data and
pure-function where it matters:

  - SiteInventory crawls the target site's OWN sitemap over HTTP and
    records the pages that actually exist. Internal links in generated
    articles may only point at inventory URLs — a link the writer invents
    is a hard quality error, never a suggestion.
  - validate_article_package() is the deterministic quality gate
    (quintuple-check layer 3 for this pipeline): word-count band, title /
    meta length, slug hygiene and collision against the live site,
    keyword presence without stuffing, FAQ presence, no placeholders.
  - build_jsonld() emits schema.org BlogPosting + FAQPage structured data.
  - render_markdown() renders the publishable file (YAML front matter +
    body) used by the file-export publisher for Next.js / static sites.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlparse

from core.site_audit import http_fetch

# ---------------------------------------------------------------------------
# Site inventory (real HTTP against the target site)
# ---------------------------------------------------------------------------


@dataclass
class SitePage:
    url: str
    title: str = ""

    @property
    def path(self) -> str:
        return urlparse(self.url).path or "/"


@dataclass
class SiteInventory:
    site_url: str
    pages: list[SitePage] = field(default_factory=list)

    @property
    def urls(self) -> set[str]:
        return {p.url for p in self.pages}

    @property
    def paths(self) -> set[str]:
        return {p.path for p in self.pages}


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _locs(xml: str) -> list[str]:
    return [m.strip() for m in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml)]


def fetch_inventory(site_url: str, fetcher=http_fetch,
                      max_pages: int = 80, timeout: int = 15) -> SiteInventory:
    """Crawl sitemap.xml (one level of sitemap-index recursion) and fetch
    each page's <title>. Raises ValueError when the site exposes no usable
    sitemap — the engine refuses to guess a site's structure."""
    base = site_url.rstrip("/")
    status, body = fetcher(f"{base}/sitemap.xml", timeout=timeout)
    if status != 200 or "<loc>" not in body:
        raise ValueError(f"no usable sitemap at {base}/sitemap.xml "
                         f"(HTTP {status})")
    urls = _locs(body)
    if "<sitemapindex" in body:  # index of sitemaps: one recursion level
        expanded: list[str] = []
        for child in urls[:10]:
            s2, b2 = fetcher(child, timeout=timeout)
            if s2 == 200:
                expanded.extend(_locs(b2))
        urls = expanded
    inv = SiteInventory(site_url=base)
    seen: set[str] = set()
    for url in urls:
        if url in seen or len(inv.pages) >= max_pages:
            continue
        seen.add(url)
        page = SitePage(url=url)
        s3, b3 = fetcher(url, timeout=timeout)
        if s3 == 200 and b3:
            parser = _TitleParser()
            try:
                parser.feed(b3)
                page.title = " ".join(parser.title.split())[:160]
            except Exception:  # noqa: BLE001 - a bad page must not kill the crawl
                page.title = ""
        inv.pages.append(page)
    if not inv.pages:
        raise ValueError(f"sitemap at {base} listed no fetchable pages")
    return inv


_STOPWORDS = {"the", "a", "an", "and", "or", "of", "for", "to", "in", "on",
              "how", "what", "why", "your", "you", "with", "vs"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower())
            if len(t) > 2 and t not in _STOPWORDS}


def suggest_internal_links(keyword: str, inventory: SiteInventory,
                           limit: int = 8) -> list[dict]:
    """Rank the site's real pages by topical overlap with the keyword.
    Only inventory pages are ever returned — these become the writer's
    allowed link set."""
    kw = _tokens(keyword)
    scored = []
    for page in inventory.pages:
        hay = _tokens(page.title) | _tokens(page.path.replace("-", " "))
        overlap = len(kw & hay)
        scored.append((overlap, page))
    scored.sort(key=lambda x: (-x[0], x[1].url))
    return [{"url": p.url, "title": p.title} for _, p in scored[:limit]]


# ---------------------------------------------------------------------------
# Article package contract + deterministic quality gate
# ---------------------------------------------------------------------------

REQUIRED_KEYS = ("title", "meta_description", "slug", "target_keyword",
                 "body_markdown", "faq", "internal_links")

PLACEHOLDER_MARKERS = ("lorem ipsum", "todo:", "tbd", "xxx", "[insert",
                       "placeholder")

_LINK_RE = re.compile(r"\]\((https?://[^)\s]+)\)")


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)


def word_count(markdown: str) -> int:
    text = re.sub(r"[#>*`_\[\]()]", " ", markdown)
    return len([w for w in text.split() if any(c.isalnum() for c in w)])


def validate_article_package(pkg: dict, inventory: SiteInventory,
                             min_words: int = 900,
                             max_words: int = 3500) -> dict:
    """Deterministic quality gate. Returns {"passed", "errors",
    "warnings", "error_count", "warning_count"} — errors block
    publishing, warnings are reported to the human. The scalar summary
    fields are load-bearing: the framework's peer reviewer treats a
    report whose values are all empty lists as a placeholder field, so
    a clean article (no errors, no warnings) failed review on the
    shape of its own success report until these were added."""
    errors: list[str] = []
    warnings: list[str] = []

    for key in REQUIRED_KEYS:
        if not pkg.get(key):
            errors.append(f"missing or empty: {key}")
    if errors:
        return {"passed": False, "errors": errors, "warnings": warnings,
                "error_count": len(errors), "warning_count": 0}

    title = str(pkg["title"])
    meta = str(pkg["meta_description"])
    slug = str(pkg["slug"])
    body = str(pkg["body_markdown"])
    keyword = str(pkg["target_keyword"]).lower()

    if len(title) > 65:
        warnings.append(f"title is {len(title)} chars (aim <= 60, hard cap 65+)")
    if not (100 <= len(meta) <= 165):
        warnings.append(f"meta description is {len(meta)} chars (aim 120-160)")

    if slug != slugify(slug):
        errors.append(f"slug is not clean kebab-case: {slug!r}")
    if f"/{slug}" in inventory.paths or f"/{slug}/" in inventory.paths:
        errors.append(f"slug collides with an existing page path: /{slug}")

    wc = word_count(body)
    if wc < min_words:
        errors.append(f"body is {wc} words (minimum {min_words})")
    elif wc > max_words:
        warnings.append(f"body is {wc} words (soft cap {max_words})")

    body_links = _LINK_RE.findall(body)
    internal = [u for u in body_links
                if urlparse(u).netloc == urlparse(inventory.site_url).netloc]
    declared = [str(u) for u in pkg.get("internal_links", [])]
    if len(set(internal)) < 3:
        errors.append(f"only {len(set(internal))} internal links in body "
                      f"(minimum 3)")
    for url in set(internal) | set(declared):
        if url not in inventory.urls:
            errors.append(f"internal link not on the live site: {url}")
    for url in body_links:
        if url.startswith("http://") and url not in inventory.urls:
            warnings.append(f"external link is not https: {url}")

    lowered = body.lower()
    hits = lowered.count(keyword)
    if hits == 0:
        errors.append("target keyword never appears in the body")
    elif wc and (hits * len(keyword.split())) / wc > 0.03:
        errors.append(f"keyword density too high ({hits} mentions) — stuffing")
    if keyword not in title.lower():
        warnings.append("target keyword not in the title")

    faq = pkg.get("faq") or []
    if len(faq) < 3:
        errors.append(f"FAQ has {len(faq)} entries (minimum 3)")
    for item in faq:
        if not (isinstance(item, dict) and item.get("q") and item.get("a")):
            errors.append("FAQ entry missing q or a")
            break

    for marker in PLACEHOLDER_MARKERS:
        if marker in lowered:
            errors.append(f"placeholder text present: {marker!r}")
            break

    if "## " not in body:
        errors.append("body has no H2 sections")

    return {"passed": not errors, "errors": errors, "warnings": warnings,
            "error_count": len(errors), "warning_count": len(warnings)}


# ---------------------------------------------------------------------------
# Structured data + rendering
# ---------------------------------------------------------------------------


def build_jsonld(pkg: dict, site_url: str, author: str = "") -> dict:
    base = site_url.rstrip("/")
    posting = {
        "@type": "BlogPosting",
        "headline": pkg.get("title", ""),
        "description": pkg.get("meta_description", ""),
        "url": f"{base}/{pkg.get('slug', '')}",
        "mainEntityOfPage": f"{base}/{pkg.get('slug', '')}",
    }
    if author:
        posting["author"] = {"@type": "Organization", "name": author}
    graph: list[dict] = [posting]
    faq = pkg.get("faq") or []
    if faq:
        graph.append({
            "@type": "FAQPage",
            "mainEntity": [
                {"@type": "Question", "name": item["q"],
                 "acceptedAnswer": {"@type": "Answer", "text": item["a"]}}
                for item in faq if isinstance(item, dict)
            ],
        })
    return {"@context": "https://schema.org", "@graph": graph}


def render_markdown(pkg: dict, jsonld: dict) -> str:
    """Publishable article file: YAML front matter + schema script + body.
    Consumed by the file-export publisher (Next.js / static sites)."""
    def yq(value: str) -> str:
        return '"' + str(value).replace('"', '\\"') + '"'

    front = "\n".join([
        "---",
        f"title: {yq(pkg.get('title', ''))}",
        f"description: {yq(pkg.get('meta_description', ''))}",
        f"slug: {pkg.get('slug', '')}",
        f"target_keyword: {yq(pkg.get('target_keyword', ''))}",
        "draft: true",
        "---",
        "",
    ])
    schema = ('<script type="application/ld+json">\n'
              + json.dumps(jsonld, indent=2) + "\n</script>\n")
    return front + schema + "\n" + str(pkg.get("body_markdown", "")).strip() + "\n"

"""GEO audit: can AI search engines find, read and cite this site?

GEO (Generative Engine Optimization) is the new SEO: when someone
asks ChatGPT, Perplexity or Google's AI Overviews a question, does
your site get cited as the answer? HeyCatch sells an "AI-search
visibility" check. This module is the ecosystem's version — and it is
deliberately limited to what the site itself controls, because there
is no honest way to ask an AI engine "did you cite me" without an
API key or a manual check:

What it measures (all real HTTP, no key):

- **AI-crawler access.** ``robots.txt`` is parsed for the bots that
  feed AI answers: GPTBot, ChatGPT-User, OAI-SearchBot, CCBot,
  PerplexityBot, Google-Extended, ClaudeBot, anthropic-ai, cohere-ai,
  Meta-ExternalAgent, Applebot-Extended, DuckAssistBot. A bot that is
  disallowed — or a ``Disallow: /`` for everyone — is a critical
  finding: an engine cannot cite what it cannot crawl. A robots file
  that is missing, or that returns the app shell instead of rules
  (a client-rendered SPA answering every path with HTML), is
  reported as such, not scored as if it were fine.
- **llms.txt.** The emerging standard (llmstxt.org): a Markdown map
  of the site at ``/llms.txt`` for AI crawlers. Absent is a high
  finding.
- **Answer-ready pages.** For every page in the sitemap (capped):
  one H1, a direct-answer first paragraph (40–300 characters right
  under the H1 — the shape AI answers quote), an FAQ section
  (``FAQPage`` JSON-LD or an FAQ heading), JSON-LD structured data
  of any kind, and at least one outbound citation link. Each is
  reported per page and aggregated, so one thin page does not sink
  the score and a site-wide gap is visible as one finding.

What it does NOT measure: actual citations in ChatGPT / Perplexity /
AI Overviews. That needs a manual spot-check ("ask Perplexity about
X, see if road2cissp.com is cited") or a paid API. The report says
so, and the score is a readiness score — 0–100 from the findings
(critical −25, high −15, medium −8, low −3) — not a citation count.

Reuses the repo's Fetcher pattern (see ``core/site_audit.py``):
every function takes a ``fetch`` returning ``(status, body)``, so
tests inject fake pages and production uses the real HTTP fetch.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from core.site_audit import Fetcher, http_fetch, parse_sitemap

# AI crawlers that feed answers, and what they are called in robots.txt.
AI_BOTS = (
    ("GPTBot", "GPTBot"),
    ("ChatGPT-User", "ChatGPT-User"),
    ("OAI-SearchBot", "OAI-SearchBot"),
    ("CCBot", "CCBot"),
    ("PerplexityBot", "PerplexityBot"),
    ("Google-Extended", "Google-Extended"),
    ("ClaudeBot", "ClaudeBot"),
    ("anthropic-ai", "anthropic-ai"),
    ("cohere-ai", "cohere-ai"),
    ("Meta-ExternalAgent", "Meta-ExternalAgent"),
    ("Applebot-Extended", "Applebot-Extended"),
    ("DuckAssistBot", "DuckAssistBot"),
)

PENALTIES = {"critical": 25, "high": 15, "medium": 8, "low": 3}


def parse_robots(text: str) -> dict:
    """robots.txt -> {bot_name: "allow" | "disallow" | "unmentioned",
    "*": ..., "blocks_all": bool}. Groups are User-agent blocks; the
    ``*`` group applies to every bot not named elsewhere."""
    groups: dict[str, list[str]] = {}
    current: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            current = groups.setdefault(value, [])
        elif key == "disallow" and current is not None:
            current.append(value)
    result: dict[str, str] = {}
    star = groups.get("*", [])
    star_exists = "*" in groups
    blocks_all = any(d in ("", "/") for d in star)
    for _label, agent in AI_BOTS:
        if agent in groups:
            disallows = groups[agent]
        elif star_exists:
            disallows = star
        else:
            result[agent] = "unmentioned"
            continue
        if any(d in ("", "/") for d in disallows):
            result[agent] = "disallow"
        else:
            # Disallows exist but not for the root (or none at all):
            # the bot can crawl the site.
            result[agent] = "allow"
    result["*"] = "disallow" if blocks_all else "allow"
    result["blocks_all"] = blocks_all  # type: ignore[assignment]
    return result


class _GeoPageParser(HTMLParser):
    """What an AI answer needs from a page: one H1, a direct-answer
    first paragraph, FAQ structure, JSON-LD, outbound citations."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.h1_texts: list[str] = []
        self.h2_texts: list[str] = []
        self.paragraphs: list[str] = []
        self.json_ld_types: list[str] = []
        self.external_links = 0
        self._tag: str | None = None
        self._buf: list[str] = []
        self._in_json_ld = False
        self._skip = 0
        self._base_host = ""

    def _flush(self):
        text = " ".join("".join(self._buf).split())
        if self._tag == "h1" and text:
            self.h1_texts.append(text)
        elif self._tag == "h2" and text:
            self.h2_texts.append(text)
        elif self._tag == "p" and text:
            self.paragraphs.append(text)
        elif self._tag == "json-ld" and text:
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            for item in data if isinstance(data, list) else [data]:
                if isinstance(item, dict) and item.get("@type"):
                    types = item["@type"]
                    self.json_ld_types.extend(
                        types if isinstance(types, list) else [types])
        self._tag = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("script", "style"):
            self._skip += 1
            if tag == "script" and a.get("type") == "application/ld+json":
                self._tag = "json-ld"
                self._buf = []
                self._in_json_ld = True
            return
        if tag == "a":
            href = a.get("href", "")
            host = urlparse(href).netloc
            if host and host != self._base_host:
                self.external_links += 1
        if tag in ("h1", "h2", "p") and self._tag is None:
            self._tag = tag
            self._buf = []

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            if self._skip:
                self._skip -= 1
            if self._in_json_ld and tag == "script":
                self._flush()
                self._in_json_ld = False
            return
        if tag == self._tag:
            self._flush()

    def handle_data(self, data):
        if self._skip and not self._in_json_ld:
            return
        if self._tag is not None:
            self._buf.append(data)


def parse_geo_page(html: str, base_host: str = "") -> dict:
    """One page -> the GEO facts about it."""
    parser = _GeoPageParser()
    parser._base_host = base_host
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML still yields partial data
        pass
    first_p = parser.paragraphs[0] if parser.paragraphs else ""
    has_faq = ("FAQPage" in parser.json_ld_types
               or any("faq" in h.lower() or "frequently" in h.lower()
                      for h in parser.h2_texts))
    return {
        "h1_count": len(parser.h1_texts),
        "h1": parser.h1_texts[0] if parser.h1_texts else "",
        "first_paragraph": first_p,
        "first_paragraph_chars": len(first_p),
        "has_direct_answer": 40 <= len(first_p) <= 300,
        "has_faq": has_faq,
        "json_ld_types": sorted(set(parser.json_ld_types)),
        "has_json_ld": bool(parser.json_ld_types),
        "external_links": parser.external_links,
        "word_count": len(re.findall(r"\w+", " ".join(parser.paragraphs))),
    }


def score_findings(findings: list[dict]) -> int:
    return max(0, 100 - sum(
        PENALTIES.get(f["severity"], 0) for f in findings))


class GeoAuditor:
    def __init__(self, fetch: Fetcher | None = None, max_pages: int = 10):
        self.fetch = fetch or http_fetch
        self.max_pages = max_pages

    def audit(self, site_url: str) -> dict:
        root = site_url.rstrip("/") + "/"
        host = urlparse(root).netloc
        findings: list[dict] = []

        def find(severity, area, issue, fix, pages=None):
            findings.append({"severity": severity, "area": area,
                             "issue": issue, "fix": fix,
                             "pages": (pages or [])[:10]})

        # 1. AI-crawler access.
        robots_status, robots = self.fetch(urljoin(root, "robots.txt"))
        robots_parsed: dict = {}
        if robots_status == 0:
            find("medium", "crawl",
                 "robots.txt could not be fetched (network failure at "
                 "audit time)",
                 "Re-run the audit; if it persists, check the site.")
        elif (robots_status != 200
              or re.search(r"(?im)^\s*user-agent\s*:", robots or "") is None):
            find("high", "crawl",
                 "robots.txt is missing or is not a real robots file "
                 "(AI crawlers get the app shell instead of rules)",
                 "Serve a real /robots.txt that allows the AI crawlers "
                 "below, plus a 'Sitemap:' line.")
        else:
            robots_parsed = parse_robots(robots)
            if robots_parsed.get("blocks_all"):
                find("critical", "crawl",
                     "robots.txt blocks the whole site for every crawler",
                     "Remove 'Disallow: /' so search and AI engines can crawl.")
            blocked = [name for _label, name in AI_BOTS
                       if robots_parsed.get(name) == "disallow"]
            if blocked:
                find("critical", "crawl",
                     f"robots.txt blocks AI crawlers: {', '.join(blocked)}",
                     "Allow these user-agents in robots.txt — an AI engine "
                     "cannot cite what it cannot crawl.")

        # 2. llms.txt.
        llms_status, llms = self.fetch(urljoin(root, "llms.txt"))
        has_llms = (llms_status == 200 and bool(llms)
                    and re.search(r"(?m)^#\s+\S", llms or "") is not None)
        if llms_status == 0:
            find("medium", "llms-txt",
                 "llms.txt could not be fetched (network failure at "
                 "audit time)",
                 "Re-run the audit; if it persists, check the site.")
        elif not has_llms:
            find("high", "llms-txt",
                 "no /llms.txt — AI crawlers have no guided map of the site",
                 "Publish /llms.txt (llmstxt.org): an H1 with the site "
                 "name and a Markdown list of the key pages with one-line "
                 "descriptions.")

        # 3. Answer-ready pages.
        sm_status, sm_xml = self.fetch(urljoin(root, "sitemap.xml"))
        urls = parse_sitemap(sm_xml) if sm_status == 200 else []
        if sm_status == 0:
            find("medium", "crawl",
                 "sitemap.xml could not be fetched (network failure at "
                 "audit time)",
                 "Re-run the audit; if it persists, check the site.")
        elif not urls:
            find("high", "crawl",
                 "sitemap.xml is missing or has no URLs — AI engines "
                 "cannot enumerate the site's pages",
                 "Publish /sitemap.xml listing every public page, and "
                 "reference it from robots.txt.")
        targets = [root] + [u for u in urls if urlparse(u).netloc == host
                            and u.rstrip("/") + "/" != root]
        pages: list[dict] = []
        for url in targets[: self.max_pages]:
            status, body = self.fetch(url)
            page = {"url": url, "status": status}
            if status == 200 and body:
                page.update(parse_geo_page(body, base_host=host))
            pages.append(page)
        ok = [p for p in pages if p["status"] == 200 and "h1_count" in p]
        if not ok:
            root_status = pages[0]["status"] if pages else 0
            if root_status == 200:
                issue = ("the homepage returned 200 with an empty body — "
                         "crawlers see nothing")
            else:
                issue = "the site could not be fetched"
            find("critical", "availability", issue,
                 "Check DNS, hosting and TLS for the domain.", [root])
            return self._result(site_url, pages, findings, robots_parsed,
                                has_llms)
        no_h1 = [p["url"] for p in ok if p["h1_count"] != 1]
        if no_h1:
            find("high", "structure",
                 f"{len(no_h1)} page(s) do not have exactly one H1",
                 "Give every page one H1 that names what the page answers.",
                 no_h1)
        no_answer = [p["url"] for p in ok if not p["has_direct_answer"]]
        if no_answer:
            find("medium", "structure",
                 f"{len(no_answer)} page(s) have no direct-answer first "
                 "paragraph (40–300 chars under the H1)",
                 "Open each page with one paragraph that directly answers "
                 "its question — that is the text AI answers quote.",
                 no_answer)
        no_ld = [p["url"] for p in ok if not p["has_json_ld"]]
        if len(no_ld) >= max(1, len(ok) // 2):
            find("high", "structured-data",
                 f"{len(no_ld)} of {len(ok)} page(s) have no JSON-LD "
                 "structured data",
                 "Add JSON-LD (Article / FAQPage / Course) so AI engines "
                 "can parse what each page is.",
                 no_ld)
        no_faq = [p["url"] for p in ok if not p["has_faq"]]
        if len(no_faq) >= max(1, len(ok) // 2):
            find("medium", "structure",
                 f"{len(no_faq)} of {len(ok)} page(s) have no FAQ section",
                 "Add an FAQ block (with FAQPage JSON-LD) answering the "
                 "questions learners actually ask.",
                 no_faq)
        no_cite = [p["url"] for p in ok if p["external_links"] == 0]
        if len(no_cite) >= max(1, len(ok) // 2):
            find("low", "structure",
                 f"{len(no_cite)} of {len(ok)} page(s) cite no outside "
                 "sources",
                 "Link out to the authoritative source for key claims — "
                 "cited answers are the ones AI engines trust.",
                 no_cite)
        return self._result(site_url, pages, findings, robots_parsed,
                            has_llms)

    @staticmethod
    def _result(site_url, pages, findings, robots, has_llms):
        return {
            "site_url": site_url,
            "score": score_findings(findings),
            "findings": findings,
            "pages_checked": len([p for p in pages if "h1_count" in p]),
            "robots": {name: robots.get(name, "unmentioned")
                       for _label, name in AI_BOTS} if robots else {},
            "has_llms_txt": has_llms,
            "note": "Readiness score only: actual citations in ChatGPT / "
                    "Perplexity / AI Overviews are not measured here — "
                    "spot-check them by hand.",
        }

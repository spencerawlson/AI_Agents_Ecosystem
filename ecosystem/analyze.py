"""Analyze any site for a market: paste a URL, name the market, get
the analysis.

This is the onboarding HeyCatch has and the ecosystem lacked: until
now every analysis was hardcoded to one business (road2cissp.com, or
a configured app in ``app_store_audit.py``). ``analyze.py`` takes a
website URL plus a market — country, language, community, keywords,
optionally an app ID — and runs the audits the repo already has
against that site, combined into one market report:

    python ecosystem/analyze.py analyze --url https://example.com --country ca
    python ecosystem/analyze.py analyze --url https://example.com --country ca \\
        --language fr --community r/quebec --keywords "déménagement, logement"
    python ecosystem/analyze.py analyze --url https://example.com --country us \\
        --app-id 6828243842
    python ecosystem/analyze.py        # no arguments: interactive prompts

What runs (all real HTTP, no key, no LLM, no spend):

- **Site audit** (``core.site_audit.SiteAuditor``) — crawl, sitemap,
  titles, descriptions, thin pages, the SPA-shell finding.
- **GEO audit** (``core.geo_audit.GeoAuditor``) — AI-crawler access in
  robots.txt, llms.txt, answer-ready pages.
- **Market fit** — the homepage's ``<html lang>`` against
  ``--language``, and whether ``hreflang`` alternates exist when a
  language is given. A mismatch is a finding, not a silent pass.
- **Community listening** (``core.community_listening.listen_reddit``)
  — only when ``--community`` AND ``--keywords`` are both given. A
  community without keywords is recorded as the target audience and
  listening is skipped: keywords are the owner's words, never
  invented. Listening is read-only — threads are reported, nothing
  is posted, no replies are drafted. Reddit works from a residential
  IP (FedServ); from a blocked network it reports ``unavailable``
  with the reason, never faked threads. X and Threads are listed as
  unavailable in every report (no public search without API
  credentials).
- **App-store audit** (``core.app_store_audit.audit_app``) — only
  when ``--app-id`` is given, in the ``--country`` storefront.

Scores are 0–100 from the findings (critical −25, high −15,
medium −8, low −3) — the site audit's score uses the same penalties
as the GEO score, so the two are comparable. A fetch that fails at
audit time is reported as a network failure to re-run, never scored
as a defect of the site. Reports land in
``reports/analyze_<host-slug>_<date>.md``.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.app_store_audit import audit_app  # noqa: E402
from core.community_listening import listen_reddit  # noqa: E402
from core.community_listening import reddit_fetch as _reddit_fetch  # noqa: E402
from core.geo_audit import GeoAuditor, score_findings  # noqa: E402
from core.site_audit import SiteAuditor, http_fetch  # noqa: E402
from dashboard.reports import reports_dir  # noqa: E402


def slug_for_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return re.sub(r"[^a-z0-9]+", "-", host).strip("-") or "site"


def normalize_subreddit(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("r/"):
        value = value[2:]
    return value.strip()


def parse_keywords(value: str | None) -> list[str]:
    if not value:
        return []
    return [k.strip() for k in value.split(",") if k.strip()]


def _primary_lang(lang: str | None) -> str:
    return (lang or "").strip().lower().split("-")[0]


class _MarketPageParser(HTMLParser):
    """The homepage's language signals: <html lang> and hreflang
    alternates."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lang = ""
        self.hreflangs: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html" and not self.lang:
            self.lang = a.get("lang", "").strip()
        elif tag == "link" and "alternate" in a.get("rel", "").lower():
            hl = a.get("hreflang", "").strip()
            if hl:
                self.hreflangs.append(hl)


def check_market_fit(url: str, language: str | None,
                     fetch=None) -> dict:
    """One fetch of the homepage: lang + hreflang against the market
    language. Returns {"lang", "hreflangs", "findings"}."""
    fetch = fetch or http_fetch
    findings: list[dict] = []
    root = url.rstrip("/") + "/"
    status, body = fetch(root)
    if status == 0:
        return {"lang": "", "hreflangs": [], "fetched": False,
                "findings": [{
                    "severity": "medium", "area": "market-fit",
                    "issue": "the homepage could not be fetched "
                             "(network failure at audit time)",
                    "fix": "Re-run the analysis; if it persists, "
                           "check the site."}]}
    if status != 200 or not body:
        return {"lang": "", "hreflangs": [], "fetched": False,
                "findings": [{
                    "severity": "high", "area": "market-fit",
                    "issue": f"the homepage returned HTTP {status} "
                             "— the market fit could not be checked",
                    "fix": "Check the site is up and the URL is right."}]}
    parser = _MarketPageParser()
    try:
        parser.feed(body)
    except Exception:  # noqa: BLE001 - malformed HTML still yields partial data
        pass
    want = _primary_lang(language)
    if language:
        if not parser.lang:
            findings.append({
                "severity": "medium", "area": "market-fit",
                "issue": "the page has no <html lang> — search engines "
                         "and AI answers cannot tell what language it is",
                "fix": f'Set <html lang="{want}"> on every page.'})
        elif _primary_lang(parser.lang) != want:
            findings.append({
                "severity": "medium", "area": "market-fit",
                "issue": f"the page language is {parser.lang}, but the "
                         f"market language is {language}",
                "fix": "Serve a version of the site in the market's "
                       "language, or pick the language the site "
                       "actually uses."})
        if not parser.hreflangs:
            findings.append({
                "severity": "low", "area": "market-fit",
                "issue": "no hreflang alternates — search engines cannot "
                         "serve the right language version per market",
                "fix": "Add <link rel=\"alternate\" hreflang=\"...\"> for "
                       "each language version."})
        elif not any(_primary_lang(h) == want for h in parser.hreflangs):
            findings.append({
                "severity": "low", "area": "market-fit",
                "issue": "hreflang alternates exist but none matches the "
                         f"market language ({language})",
                "fix": "Add an hreflang alternate for the market's "
                       "language."})
    return {"lang": parser.lang, "hreflangs": parser.hreflangs,
            "fetched": True, "findings": findings}


def run_analysis(url: str, country: str, language: str | None = None,
                 community: str | None = None,
                 keywords: list[str] | None = None,
                 app_id: str | None = None,
                 fetch=None, reddit_fetch=None,
                 now: datetime | None = None) -> dict:
    """Run every audit for one site + market. All fetches are
    injected, so tests pass fake ones and production uses real HTTP."""
    now = now or datetime.now(timezone.utc)
    fetch = fetch or http_fetch
    keywords = keywords or []
    community = normalize_subreddit(community) if community else ""
    rfetch = reddit_fetch or _reddit_fetch

    site = SiteAuditor(fetch=fetch).audit(url)
    geo = GeoAuditor(fetch=fetch).audit(url)
    market = check_market_fit(url, language, fetch=fetch)

    community_result: dict | None = None
    if community and keywords:
        community_result = listen_reddit([community], keywords,
                                         fetch=rfetch)
    elif community:
        community_result = {
            "status": "needs_keywords",
            "community": community,
            "note": "Listening needs keywords — the words the market "
                    "uses. Re-run with --keywords; nothing was searched.",
        }

    app_result: dict | None = None
    if app_id:
        try:
            app_result = audit_app(app_id, country=country, fetch=fetch)
        except ValueError as exc:
            app_result = {"listed": None, "score": None, "error": str(exc),
                          "findings": [{
                              "severity": "medium", "area": "listing",
                              "issue": "the App Store lookup failed "
                                       f"({exc})",
                              "fix": "Re-run the analysis; if it "
                                     "persists, check the network."}]}

    all_findings = (list(site["findings"]) + list(geo["findings"])
                    + list(market["findings"]))
    return {
        "url": url,
        "country": country,
        "language": language or "",
        "community": community,
        "keywords": keywords,
        "app_id": app_id or "",
        "at": now.isoformat(),
        "site_score": score_findings(site["findings"]),
        "geo_score": geo["score"],
        "site": site,
        "geo": geo,
        "market": market,
        "community_result": community_result,
        "app_result": app_result,
        "findings": all_findings,
        "finding_counts": {
            sev: sum(1 for f in all_findings if f["severity"] == sev)
            for sev in ("critical", "high", "medium", "low")},
    }


def render_report(result: dict) -> str:
    lines = [
        f"# Market analysis — {result['url']} ({result['at'][:10]})", "",
        f"Market: country **{result['country']}**"
        + (f", language **{result['language']}**" if result["language"] else "")
        + (f", community **r/{result['community']}**" if result["community"] else "")
        + (f", keywords: {', '.join(result['keywords'])}" if result["keywords"] else "")
        + (f", app ID {result['app_id']}" if result["app_id"] else ""),
        "",
        f"Site score: **{result['site_score']}/100** "
        f"({result['site']['pages_checked']} pages checked, "
        f"{result['site']['sitemap_urls']} sitemap URLs) — "
        f"GEO score: **{result['geo_score']}/100** "
        f"({result['geo']['pages_checked']} pages checked).",
        "",
        "## Findings", "",
    ]
    for f in result["findings"]:
        pages = (f" ({', '.join(f['pages'][:3])})"
                 if f.get("pages") else "")
        lines.append(f"- **[{f['severity']}] {f['area']}** — {f['issue']}"
                     f"{pages} — Fix: {f['fix']}")
    if not result["findings"]:
        lines.append("- None. The site is in good shape for this market.")
    lines += ["", "## Market fit", ""]
    market = result["market"]
    if market["fetched"]:
        lines.append(f"- Page language: {market['lang'] or '(none set)'}")
        lines.append("- hreflang alternates: "
                     + (", ".join(market["hreflangs"])
                        if market["hreflangs"] else "(none)"))
    else:
        lines.append("- The homepage could not be checked (see findings).")
    lines += ["", "## Community", ""]
    comm = result["community_result"]
    if comm is None:
        lines.append("- No community given — no listening ran.")
    elif comm.get("status") == "needs_keywords":
        lines.append(f"- r/{comm['community']} is the target community, "
                     "but no keywords were given, so nothing was searched. "
                     "Re-run with --keywords.")
    else:
        sources = comm.get("sources", {})
        reddit = sources.get("reddit", {})
        lines.append(f"- Reddit: {reddit.get('status', '?')}"
                     + (f" — {reddit['reason']}" if reddit.get("reason") else "")
                     + f" ({reddit.get('queries', 0)} queries, "
                     f"{reddit.get('raw_found', 0)} threads found)")
        lines.append("- X: unavailable — no public search without API "
                     "credentials")
        lines.append("- Threads: unavailable — no public search without "
                     "API credentials")
        threads = [t for t in comm.get("threads", [])
                   if t.get("intent_score", 0) >= 50]
        lines.append(f"- Threads worth a reply (score ≥ 50): {len(threads)}")
        for t in threads[:10]:
            lines.append(f"  - [{t.get('intent_score', '?')}] "
                         f"{t.get('title', '')} "
                         f"— r/{t.get('subreddit', '')} "
                         f"({t.get('url', '')})")
        lines.append("")
        lines.append("NOTHING WAS POSTED — listening is read-only; "
                     "threads are reported here for the owner to answer "
                     "by hand.")
    lines += ["", "## App store", ""]
    app = result["app_result"]
    if app is None:
        lines.append("- No app ID given — no listing audit ran.")
    elif app.get("error"):
        lines.append(f"- Lookup failed: {app['error']}")
    elif not app.get("listed"):
        lines.append("- Not on the App Store in the storefronts checked.")
    else:
        d = app["details"]
        lines.append(f"- {d['name']} v{d['version']}: score "
                     f"**{app['score']}/100**, {d['ratings']} ratings, "
                     f"{d['screenshots']} screenshots.")
    lines += ["", "## Limits", "",
              "- Scores are readiness scores from the audits' findings, "
              "not traffic or citation counts.",
              "- Actual citations in ChatGPT / Perplexity / AI Overviews "
              "are not measured — spot-check them by hand.",
              "- No competitor data, no live social APIs, no auto-publish: "
              "everything here is a draft for the owner to act on.",
              ""]
    return "\n".join(lines)


def _prompt(text: str, required: bool = False) -> str:
    while True:
        value = input(text).strip()
        if value or not required:
            return value
        print("  (required — type a value)")


def _prompt_interactive() -> dict:
    print("Analyze a site for a market.")
    print("Press Enter to skip an optional field.\n")
    url = _prompt("Website URL (e.g. https://example.com): ", required=True)
    country = _prompt("Market country code (e.g. ca, us, ht): ",
                      required=True)
    language = _prompt("Market language (e.g. en, fr, ht): ")
    community = _prompt("Community subreddit (e.g. r/cissp): ")
    keywords = _prompt("Keywords, comma-separated: ")
    app_id = _prompt("App Store app ID (optional): ")
    return {"url": url, "country": country, "language": language or None,
            "community": community or None, "keywords": keywords,
            "app_id": app_id or None}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze any site for a market: URL + market in, "
                    "one report out.")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("analyze", help="analyze a site for a market")
    p.add_argument("--url", required=True, help="website URL to analyze")
    p.add_argument("--country", required=True,
                   help="market country code (ca, us, ht, ...)")
    p.add_argument("--language", default=None,
                   help="market language code (en, fr, ht, ...)")
    p.add_argument("--community", default=None,
                   help="target subreddit (r/cissp or cissp)")
    p.add_argument("--keywords", default=None,
                   help="comma-separated keywords for community listening")
    p.add_argument("--app-id", default=None,
                   help="App Store app ID for a listing audit")
    args = parser.parse_args(argv)
    if args.cmd != "analyze":
        # No arguments at all: interactive mode.
        if argv:
            parser.print_help()
            return 2
        opts = _prompt_interactive()
        url = opts["url"]
        country = opts["country"]
        language = opts["language"]
        community = opts["community"]
        keywords = parse_keywords(opts["keywords"])
        app_id = opts["app_id"]
    else:
        url, country = args.url, args.country
        language, community = args.language, args.community
        keywords = parse_keywords(args.keywords)
        app_id = args.app_id
    if not re.match(r"^https?://", url) or not urlparse(url).netloc:
        print(f"error: {url!r} is not a website URL — "
              "use https://example.com")
        return 2
    now = datetime.now(timezone.utc)
    result = run_analysis(url, country, language=language,
                          community=community, keywords=keywords,
                          app_id=app_id, now=now)
    print(f"{url} for market {country}:")
    print(f"  site score {result['site_score']}/100, "
          f"GEO score {result['geo_score']}/100, "
          f"{len(result['findings'])} findings")
    for sev in ("critical", "high", "medium", "low"):
        n = result["finding_counts"][sev]
        if n:
            print(f"  {sev}: {n}")
    if result["community_result"] is not None:
        comm = result["community_result"]
        if comm.get("status") == "needs_keywords":
            print("  community: r/" + result["community"] +
                  " — listening skipped, no keywords given")
        else:
            worthy = [t for t in comm.get("threads", [])
                      if t.get("intent_score", 0) >= 50]
            print(f"  community: {len(worthy)} threads "
                  "worth a reply")
    if result["app_result"] is not None:
        app = result["app_result"]
        if app.get("error"):
            print(f"  app store: lookup failed ({app['error']})")
        elif not app.get("listed"):
            print("  app store: not listed")
        else:
            print(f"  app store: {app['details']['name']} "
                  f"— score {app['score']}/100")
    path = reports_dir() / f"analyze_{slug_for_url(url)}_{now:%Y%m%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(result), encoding="utf-8")
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

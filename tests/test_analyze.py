"""Analyze any site for a market: combined audits, market-fit
checks, community listening, app-store wiring, and the interactive
prompts. No network anywhere here — every fetch is fake."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ecosystem.analyze as analyze
from ecosystem.analyze import (
    check_market_fit,
    normalize_subreddit,
    parse_keywords,
    render_report,
    run_analysis,
    slug_for_url,
)

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
SITE = "https://example.com"

GOOD_ROBOTS = ("User-agent: *\nAllow: /\n"
               "Sitemap: https://example.com/sitemap.xml\n")

GOOD_PAGE = """<html lang="en"><head><title>Example — Home</title>
<meta name="description" content="An example site for testing the home page.">
<link rel="canonical" href="https://example.com/">
<meta property="og:image" content="https://example.com/og.png">
<link rel="alternate" hreflang="en" href="https://example.com/">
<link rel="alternate" hreflang="fr" href="https://example.com/fr/">
<script src="https://plausible.io/js/script.js"></script>
<script type="application/ld+json">{"@type": "FAQPage"}</script>
</head><body><h1>What is Example?</h1>
<p>Example is a small site that shows how the analyzer reads a page for a market.</p>
<h2>Frequently asked questions</h2>
<p>More detail here, written out at length so the page has enough crawlable
text for the audit to read: the site explains its purpose, its audience, and
the way its pages link together for visitors who arrive from search.</p>
<a href="https://www.example.org/">An outside source</a></body></html>"""

GOOD_PAGE_2 = GOOD_PAGE.replace("Example — Home", "Example — Guide").replace(
    "<h1>What is Example?</h1>", "<h1>How does Example work?</h1>").replace(
    'content="An example site for testing the home page."',
    'content="An example site for testing the guide page."').replace(
    '<link rel="canonical" href="https://example.com/">',
    '<link rel="canonical" href="https://example.com/page0">')

REDDIT_LISTING = {
    "data": {"children": [{"data": {
        "id": "abc1", "subreddit": "cissp",
        "title": "How do I pass cissp?",
        "selftext": "I am studying and stuck on domain 3.",
        "permalink": "/r/cissp/comments/abc1/how_do_i_pass_cissp/",
        "author": "learner1", "score": 12, "num_comments": 5,
        "created_utc": NOW.timestamp() - 86400,
        "over_18": False, "locked": False}}]}}

ITUNES_RECORD = {
    "trackName": "Example App",
    "description": "An example app. " * 60,
    "screenshotUrls": ["https://example.com/1.png"] * 4,
    "userRatingCount": 12, "averageUserRating": 4.5, "version": "1.0",
    "currentVersionReleaseDate": "2026-11-01T00:00:00Z",
    "releaseNotes": "First release.",
    "languageCodesISO2A": ["EN", "FR"],
}


def _site_mapping(homepage=GOOD_PAGE, page2=GOOD_PAGE_2):
    return {
        f"{SITE}/robots.txt": (200, GOOD_ROBOTS),
        f"{SITE}/llms.txt": (200, "# Example\n\n- [Home](/)\n"),
        f"{SITE}/sitemap.xml": (
            200, "<urlset><url><loc>https://example.com/page0</loc></url>"
                 "</urlset>"),
        f"{SITE}/": (200, homepage),
        f"{SITE}/page0": (200, page2),
    }


def _fetch(mapping):
    def fake(url):
        if "search.json" in url:
            return 200, json.dumps(REDDIT_LISTING)
        if "itunes.apple.com" in url:
            return 200, json.dumps(
                {"resultCount": 1, "results": [ITUNES_RECORD]})
        return mapping.get(url, (0, ""))
    return fake


# -- helpers ------------------------------------------------------------------

def test_slug_for_url():
    assert slug_for_url("https://example.com/") == "example-com"
    assert slug_for_url("https://www.shop.example.co.uk/x") == \
        "www-shop-example-co-uk"
    assert slug_for_url("https://example.com:8080/") == "example-com-8080"


def test_normalize_subreddit():
    assert normalize_subreddit("r/cissp") == "cissp"
    assert normalize_subreddit("R/CISSP") == "CISSP"
    assert normalize_subreddit("  cissp  ") == "cissp"


def test_parse_keywords():
    assert parse_keywords("a, b ,c") == ["a", "b", "c"]
    assert parse_keywords("") == []
    assert parse_keywords(None) == []
    assert parse_keywords("one") == ["one"]


# -- market fit ---------------------------------------------------------------

def test_market_fit_matching_language():
    result = check_market_fit(SITE, "en", fetch=_fetch(_site_mapping()))
    assert result["lang"] == "en"
    assert result["hreflangs"] == ["en", "fr"]
    assert result["findings"] == []


def test_market_fit_missing_lang():
    page = GOOD_PAGE.replace('<html lang="en">', "<html>")
    result = check_market_fit(SITE, "en", fetch=_fetch(_site_mapping(page)))
    assert any("no <html lang>" in f["issue"] for f in result["findings"])


def test_market_fit_wrong_language():
    result = check_market_fit(SITE, "fr", fetch=_fetch(_site_mapping()))
    assert any("page language is en" in f["issue"]
               for f in result["findings"])
    assert any("hreflang" not in f["issue"] for f in result["findings"])


def test_market_fit_no_hreflang_is_not_a_finding():
    # A single-language site serving its market in that language has
    # nothing to alternate between — no hreflang is correct, not a
    # defect. (This was a false positive: it cost road2cissp.com a
    # low finding for being English-only.)
    page = GOOD_PAGE.replace(
        '<link rel="alternate" hreflang="en" href="https://example.com/">\n', "")
    page = page.replace(
        '<link rel="alternate" hreflang="fr" href="https://example.com/fr/">\n', "")
    result = check_market_fit(SITE, "en", fetch=_fetch(_site_mapping(page)))
    assert result["findings"] == []
    assert result["hreflangs"] == []


def test_market_fit_hreflang_missing_market_language():
    # Alternates exist but none matches the market language — that
    # IS a finding (the site has other versions, just not this one).
    page = GOOD_PAGE.replace(
        '<link rel="alternate" hreflang="fr" href="https://example.com/fr/">\n', "")
    result = check_market_fit(SITE, "fr", fetch=_fetch(_site_mapping(page)))
    assert any("hreflang alternates exist but none matches" in f["issue"]
               for f in result["findings"])


def test_market_fit_no_language_given_checks_nothing():
    result = check_market_fit(SITE, None, fetch=_fetch(_site_mapping()))
    assert result["findings"] == []
    assert result["lang"] == "en"


def test_market_fit_unreachable_homepage():
    result = check_market_fit(SITE, "en", fetch=_fetch({}))
    assert result["fetched"] is False
    assert any("network failure" in f["issue"] for f in result["findings"])


# -- combined analysis --------------------------------------------------------

def test_run_analysis_clean_site():
    result = run_analysis(SITE, "ca", language="en", now=NOW,
                          fetch=_fetch(_site_mapping()))
    assert result["site_score"] == 100
    assert result["geo_score"] == 100
    assert result["findings"] == []
    assert result["community_result"] is None
    assert result["app_result"] is None
    assert result["market"]["lang"] == "en"


def test_run_analysis_combines_findings():
    mapping = _site_mapping()
    mapping[f"{SITE}/llms.txt"] = (404, "")
    result = run_analysis(SITE, "ca", language="fr", now=NOW,
                          fetch=_fetch(mapping))
    assert result["geo_score"] == 85  # one high finding
    assert any("no /llms.txt" in f["issue"] for f in result["findings"])
    assert any("page language is en" in f["issue"]
               for f in result["findings"])
    assert result["site_score"] == 100


def test_run_analysis_community_without_keywords_skips_listening():
    calls = []

    def counting_fetch(url):
        calls.append(url)
        return _fetch(_site_mapping())(url)

    def must_not_fetch(url):
        raise AssertionError("reddit must not be fetched")

    result = run_analysis(SITE, "ca", community="r/cissp", now=NOW,
                          fetch=counting_fetch,
                          reddit_fetch=must_not_fetch)
    assert result["community_result"]["status"] == "needs_keywords"
    assert not any("search.json" in u for u in calls)


def test_run_analysis_community_with_keywords_listens():
    result = run_analysis(SITE, "ca", community="r/cissp",
                          keywords=["pass cissp"], now=NOW,
                          fetch=_fetch(_site_mapping()),
                          reddit_fetch=_fetch({}))
    comm = result["community_result"]
    assert comm["sources"]["reddit"]["status"] == "ok"
    assert len(comm["threads"]) == 1
    assert comm["threads"][0]["intent_score"] >= 50
    assert comm["threads"][0]["title"] == "How do I pass cissp?"


def test_run_analysis_community_blocked_reports_unavailable():
    def blocked(url):
        return 403, "whoa there, pardner"

    result = run_analysis(SITE, "ca", community="cissp",
                          keywords=["pass cissp"], now=NOW,
                          fetch=_fetch(_site_mapping()),
                          reddit_fetch=blocked)
    comm = result["community_result"]
    assert comm["sources"]["reddit"]["status"] == "unavailable"
    assert comm["threads"] == []
    assert "residential IP" in comm["sources"]["reddit"]["reason"]


def test_run_analysis_app_store_wired():
    result = run_analysis(SITE, "ca", app_id="123", now=NOW,
                          fetch=_fetch(_site_mapping()))
    app = result["app_result"]
    assert app["listed"] is True
    assert app["score"] == 100
    assert app["details"]["name"] == "Example App"


def test_run_analysis_app_lookup_failure_is_reported_not_fatal():
    def failing(url):
        if "itunes.apple.com" in url:
            return 0, ""
        return _fetch(_site_mapping())(url)

    result = run_analysis(SITE, "ca", app_id="123", now=NOW, fetch=failing)
    assert result["app_result"]["listed"] is None
    assert "network failure" in result["app_result"]["error"]


# -- report rendering ---------------------------------------------------------

def test_render_report_clean_site():
    result = run_analysis(SITE, "ca", language="en", now=NOW,
                          fetch=_fetch(_site_mapping()))
    text = render_report(result)
    assert "Site score: **100/100**" in text
    assert "GEO score: **100/100**" in text
    assert "Page language: en" in text
    assert "No community given" in text
    assert "No app ID given" in text
    assert "not measured" in text


def test_render_report_with_community_and_posted_banner():
    result = run_analysis(SITE, "ca", community="cissp",
                          keywords=["pass cissp"], now=NOW,
                          fetch=_fetch(_site_mapping()),
                          reddit_fetch=_fetch({}))
    text = render_report(result)
    assert "Threads worth a reply (score ≥ 50): 1" in text
    assert "How do I pass cissp?" in text
    assert "NOTHING WAS POSTED" in text
    assert "X: unavailable" in text


# -- CLI ----------------------------------------------------------------------

def test_cli_analyze_writes_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    fetch = _fetch(_site_mapping())
    monkeypatch.setattr(analyze, "http_fetch", fetch)
    assert analyze.main(["analyze", "--url", SITE, "--country", "ca",
                         "--language", "en"]) == 0
    out = capsys.readouterr().out
    assert "site score 100/100" in out
    reports = list((tmp_path / "reports").glob("analyze_*.md"))
    assert len(reports) == 1
    assert "example-com" in reports[0].name
    assert "Site score: **100/100**" in reports[0].read_text()


def test_cli_bad_url_fails(capsys):
    assert analyze.main(["analyze", "--url", "example.com",
                         "--country", "ca"]) == 2
    assert "not a website URL" in capsys.readouterr().out


def test_cli_interactive_prompts(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(analyze, "http_fetch", _fetch(_site_mapping()))
    answers = iter([SITE, "ca", "en", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert analyze.main([]) == 0
    out = capsys.readouterr().out
    assert "site score 100/100" in out
    assert list((tmp_path / "reports").glob("analyze_*.md"))


def test_cli_interactive_reprompts_for_required(tmp_path, monkeypatch):
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(analyze, "http_fetch", _fetch(_site_mapping()))
    answers = iter(["", SITE, "", "ca", "", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert analyze.main([]) == 0
    assert list((tmp_path / "reports").glob("analyze_*.md"))

"""GEO audit: robots parsing, llms.txt, answer-ready pages, scoring,
and the `geo` CLI. No network, no LLM, no spend anywhere here —
every fetch is a fake dict of URL -> (status, body)."""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.geo_audit import GeoAuditor, parse_geo_page, parse_robots
import businesses.road2cissp.growth as growth
from businesses.road2cissp.growth import load_state, save_state

SITE = "https://road2cissp.com"

GOOD_ROBOTS = """User-agent: *
Allow: /
Sitemap: https://road2cissp.com/sitemap.xml
"""

BLOCKED_ROBOTS = """User-agent: GPTBot
Disallow: /
User-agent: *
Allow: /
"""

GOOD_PAGE = """<html><head><title>CISSP study plan</title>
<script type="application/ld+json">{"@type": "FAQPage"}</script>
</head><body><h1>How do I pass the CISSP?</h1>
<p>Study the eight domains in order, do 400 practice questions, and take two full exam sims before booking.</p>
<h2>Frequently asked questions</h2>
<p>More detail here.</p>
<a href="https://www.isc2.org/">ISC2</a></body></html>"""

THIN_PAGE = """<html><head><title>Empty</title></head>
<body><div id="root"></div></body></html>"""


def _fetch(mapping):
    def fake(url):
        return mapping.get(url, (0, ""))
    return fake


def _site_mapping(robots=GOOD_ROBOTS, pages=None, llms=None, sitemap=None):
    pages = pages if pages is not None else [GOOD_PAGE]
    urls = [f"{SITE}/page{i}" for i in range(len(pages))]
    sm = sitemap if sitemap is not None else (
        "<urlset>" + "".join(f"<url><loc>{u}</loc></url>" for u in urls)
        + "</urlset>")
    mapping = {
        f"{SITE}/robots.txt": (200, robots),
        f"{SITE}/llms.txt": (200, llms if llms is not None else
                             "# Road to CISSP\n\n- [Study](/study)\n"),
        f"{SITE}/sitemap.xml": (200, sm),
        f"{SITE}/": (200, pages[0]),
    }
    for u, body in zip(urls, pages):
        mapping[u] = (200, body)
    return mapping


# -- robots parsing -----------------------------------------------------------

def test_parse_robots_allows_all_by_default():
    parsed = parse_robots(GOOD_ROBOTS)
    assert parsed["GPTBot"] == "allow"
    assert parsed["PerplexityBot"] == "allow"
    assert parsed["blocks_all"] is False


def test_parse_robots_named_bot_blocked():
    parsed = parse_robots(BLOCKED_ROBOTS)
    assert parsed["GPTBot"] == "disallow"
    assert parsed["PerplexityBot"] == "allow"  # * group allows


def test_parse_robots_blocks_all():
    parsed = parse_robots("User-agent: *\nDisallow: /\n")
    assert parsed["blocks_all"] is True
    assert parsed["GPTBot"] == "disallow"


def test_parse_robots_partial_disallow_is_still_allowed():
    parsed = parse_robots("User-agent: *\nDisallow: /admin/\n")
    assert parsed["GPTBot"] == "allow"
    assert parsed["blocks_all"] is False


# -- page parsing -------------------------------------------------------------

def test_parse_geo_page_good():
    page = parse_geo_page(GOOD_PAGE, base_host="road2cissp.com")
    assert page["h1_count"] == 1
    assert page["has_direct_answer"] is True
    assert page["has_faq"] is True
    assert page["has_json_ld"] is True
    assert page["json_ld_types"] == ["FAQPage"]
    assert page["external_links"] == 1


def test_parse_geo_page_thin():
    page = parse_geo_page(THIN_PAGE, base_host="road2cissp.com")
    assert page["h1_count"] == 0
    assert page["has_direct_answer"] is False
    assert page["has_faq"] is False
    assert page["has_json_ld"] is False
    assert page["external_links"] == 0


# -- audit --------------------------------------------------------------------

def test_audit_good_site_scores_100():
    result = GeoAuditor(fetch=_fetch(_site_mapping())).audit(SITE)
    assert result["score"] == 100
    assert result["findings"] == []
    assert result["has_llms_txt"] is True
    assert result["pages_checked"] == 2


def test_audit_blocks_ai_crawlers_is_critical():
    result = GeoAuditor(
        fetch=_fetch(_site_mapping(robots=BLOCKED_ROBOTS))).audit(SITE)
    assert result["score"] == 75  # one critical finding
    assert any(f["severity"] == "critical"
               and "GPTBot" in f["issue"] for f in result["findings"])


def test_audit_missing_robots_and_llms():
    mapping = _site_mapping()
    mapping[f"{SITE}/robots.txt"] = (404, "")
    mapping[f"{SITE}/llms.txt"] = (404, "")
    result = GeoAuditor(fetch=_fetch(mapping)).audit(SITE)
    assert result["score"] == 70  # two high findings
    assert result["has_llms_txt"] is False


def test_audit_app_shell_robots_is_not_a_robots_file():
    # A client-rendered SPA answering every path with HTML: the
    # "robots.txt" body has no User-agent lines, so it is not rules.
    mapping = _site_mapping(robots="<html><body>Redirecting...</body></html>")
    result = GeoAuditor(fetch=_fetch(mapping)).audit(SITE)
    assert any(f["area"] == "crawl" and "not a real robots file" in f["issue"]
               for f in result["findings"])


def test_audit_thin_pages_flagged():
    result = GeoAuditor(
        fetch=_fetch(_site_mapping(pages=[THIN_PAGE, THIN_PAGE]))).audit(SITE)
    issues = [f["issue"] for f in result["findings"]]
    assert any("exactly one H1" in i for i in issues)
    assert any("direct-answer" in i for i in issues)
    assert any("JSON-LD" in i for i in issues)
    assert any("FAQ" in i for i in issues)
    assert any("cite no outside" in i for i in issues)
    # high 15 (H1) + medium 8 (answer) + high 15 (JSON-LD)
    # + medium 8 (FAQ) + low 3 (citations) = 49 off 100.
    assert result["score"] == 51


def test_audit_unreachable_site_is_critical():
    result = GeoAuditor(fetch=_fetch({})).audit(SITE)
    # A network failure is reported as "could not be fetched — re-run",
    # not scored as a site defect: medium 8 (robots) + medium 8 (llms)
    # + medium 8 (sitemap) + critical 25 (unavailable) = 49 off 100.
    assert result["score"] == 51
    assert any(f["severity"] == "critical"
               and f["area"] == "availability" for f in result["findings"])
    assert any("network failure" in f["issue"] for f in result["findings"])


def test_audit_shell_html_with_user_agent_string_is_not_robots():
    # A SPA shell whose JS bundle mentions "user-agent" must not pass
    # as a robots file — only a line starting with User-agent: counts.
    shell = ('<html><head><script>const a = "user-agent"; '
             "fetch('/api/x')</script></head><body></body></html>")
    mapping = _site_mapping(robots=shell)
    result = GeoAuditor(fetch=_fetch(mapping)).audit(SITE)
    assert any(f["area"] == "crawl" and "not a real robots file" in f["issue"]
               for f in result["findings"])


# -- CLI ----------------------------------------------------------------------

def test_cli_geo_writes_report_and_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(growth, "default_state_path",
                        lambda: tmp_path / "state.json")
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    save_state(tmp_path / "state.json", {})
    fake_result = {"site_url": SITE, "score": 85, "findings": [
        {"severity": "medium", "area": "structure",
         "issue": "1 page(s) have no FAQ section",
         "fix": "Add an FAQ block.", "pages": [f"{SITE}/page0"]}],
        "pages_checked": 2, "robots": {"GPTBot": "allow"},
        "has_llms_txt": True,
        "note": "Readiness score only."}

    class _FakeAuditor:
        def __init__(self, *a, **k):
            pass

        def audit(self, site_url):
            assert site_url == SITE
            return fake_result

    with patch("core.geo_audit.GeoAuditor", _FakeAuditor):
        assert growth.main(["geo"]) == 0
    out = capsys.readouterr().out
    assert "GEO score 85/100" in out
    from datetime import datetime as _dt
    report = tmp_path / "reports" / f"geo_{_dt.now(timezone.utc):%Y%m%d}.md"
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    assert "GEO readiness score: **85/100**" in text
    assert "no FAQ section" in text
    saved = load_state(tmp_path / "state.json")
    assert saved["geo"]["score"] == 85
    assert saved["geo"]["findings"] == 1

    assert growth.main(["status"]) == 0
    assert "GEO score: 85/100" in capsys.readouterr().out

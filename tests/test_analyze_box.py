"""The "Analyze a site" panel on the Ventoryn dashboard (/ops) and the
mobile crew carousel.

The panel runs the same pipeline as the `analyze` CLI, in a background
job: POST /api/analyze starts it, the page polls GET for the result,
and the report is written to reports/analyze_<host>_<date>.md exactly
like the CLI writes it. Nothing is published or posted anywhere. No
network anywhere here — every fetch is fake, except the URL validator's
best-effort DNS check (capped at 3s, and a failed lookup just lets the
fetch report honestly).
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ecosystem.analyze as analyze
from dashboard.analyze_jobs import validate_country, validate_language, validate_url
from dashboard.app import create_app
from fastapi.testclient import TestClient

from core.ledger import Ledger
from core.registry import BusinessRegistry

from _signin import operator_store_and_config, sign_in

SITE = "https://example.com"

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

ROBOTS = ("User-agent: *\nAllow: /\n"
          "Sitemap: https://example.com/sitemap.xml\n")

SITEMAP = ('<?xml version="1.0"?><urlset '
           'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           '<url><loc>https://example.com/</loc></url></urlset>')


def _fake_fetch(url, timeout=10.0):
    if url.endswith("/robots.txt"):
        return 200, ROBOTS
    if url.endswith("/sitemap.xml"):
        return 200, SITEMAP
    if url.endswith("/llms.txt"):
        return 404, ""
    return 200, GOOD_PAGE


def _dead_fetch(url, timeout=10.0):
    return 0, ""


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("ECOSYSTEM_STATE_DB", str(tmp_path / "state.db"))
    store, config = operator_store_and_config()
    app = create_app(registry=BusinessRegistry(), ledger=Ledger(),
                     saas_store=store, saas_config=config)
    client = TestClient(app)
    sign_in(client, store)
    return client


def _poll_until_done(client, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/analyze/{job_id}")
        assert r.status_code == 200
        job = r.json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


# -- URL validation ----------------------------------------------------------

def test_validate_url_accepts_https():
    assert validate_url("https://road2cissp.com") == "https://road2cissp.com"
    assert validate_url("  http://example.com/page  ") == "http://example.com/page"


def test_validate_url_accepts_bare_domain():
    """A bare domain works — https:// is added when no scheme is
    present, after trimming whitespace."""
    assert validate_url("example.com") == "https://example.com"
    assert validate_url("  example.com/page  ") == "https://example.com/page"
    assert validate_url("example.com:8000") == "https://example.com:8000"


@pytest.mark.parametrize("bad,reason", [
    ("", "empty"),
    ("not a url", "not a domain"),
    ("example", "single label, no dot"),
    ("ftp://example.com", "not http/https"),
    ("http://localhost:8000/ops", "localhost"),
    ("http://localhost/", "localhost"),
    ("http://127.0.0.1/", "loopback IP"),
    ("http://192.168.1.53:8000/", "private LAN IP"),
    ("http://10.0.0.5/", "private IP"),
    ("http://169.254.1.1/", "link-local IP"),
])
def test_validate_url_rejects(bad, reason):
    with pytest.raises(ValueError):
        validate_url(bad)


def test_validate_country_and_language():
    assert validate_country("") == "ca"
    assert validate_country("FR") == "fr"
    assert validate_language("") is None
    assert validate_language("EN-CA") == "en-ca"
    with pytest.raises(ValueError):
        validate_country("canada")
    with pytest.raises(ValueError):
        validate_language("english")


# -- Endpoint: bad input ------------------------------------------------------

def test_analyze_rejects_bad_url(client):
    for bad in ("", "not a url", "example", "ftp://example.com",
                "http://localhost:8000/ops", "http://127.0.0.1/"):
        r = client.post("/api/analyze", json={"url": bad, "country": "ca"})
        assert r.status_code == 400, bad
        assert r.json()["detail"]


def test_analyze_accepts_bare_domain(client):
    """The landing box and the /ops panel both post here — a bare
    domain starts a job, and the stored URL carries https://."""
    client.app.state.analyze_runner = lambda url, country, language: {
        "site_score": 90, "geo_score": 80, "fetchable": True,
        "unavailable_reason": None,
        "finding_counts": {"critical": 0, "high": 0, "medium": 0,
                           "low": 0},
        "findings": [], "report_name": "analyze_example-com_20261108.md",
    }
    r = client.post("/api/analyze", json={"url": "example.com",
                                          "country": "ca"})
    assert r.status_code == 202
    assert r.json()["url"] == "https://example.com"
    job = _poll_until_done(client, r.json()["job_id"])
    assert job["status"] == "done"
    assert job["url"] == "https://example.com"


def test_analyze_rejects_bad_country_and_language(client):
    r = client.post("/api/analyze",
                    json={"url": SITE, "country": "canada"})
    assert r.status_code == 400
    r = client.post("/api/analyze",
                    json={"url": SITE, "country": "ca", "language": "english"})
    assert r.status_code == 400


def test_analyze_unknown_job_is_404(client):
    assert client.get("/api/analyze/nope").status_code == 404
    assert client.get("/api/analyze/nope/report").status_code == 404


# -- Endpoint: job lifecycle (fake runner, no network) ------------------------

def test_analyze_job_lifecycle(client, tmp_path, monkeypatch):
    from dashboard.reports import reports_dir

    report_name = "analyze_example-com_20261108.md"

    def fake_runner(url, country, language):
        path = reports_dir() / report_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Site analysis: https://example.com\n\n"
                        "Site score: **88/100**\n\nGEO score: **71/100**\n", encoding="utf-8")
        return {
            "site_score": 88, "geo_score": 71, "fetchable": True,
            "unavailable_reason": None,
            "finding_counts": {"critical": 0, "high": 1, "medium": 2, "low": 0},
            "findings": [{"severity": "high", "area": "content",
                          "issue": "a page is thin", "fix": "add copy"}],
            "report_name": report_name,
        }

    client.app.state.analyze_runner = fake_runner
    r = client.post("/api/analyze",
                    json={"url": SITE, "country": "ca", "language": "en"})
    assert r.status_code == 202
    started = r.json()
    assert started["job_id"]
    assert started["status"] in ("queued", "running")
    assert started["url"] == SITE
    assert started["country"] == "ca"

    job = _poll_until_done(client, started["job_id"])
    assert job["status"] == "done"
    assert job["result"]["site_score"] == 88
    assert job["result"]["geo_score"] == 71
    assert job["result"]["findings"][0]["issue"] == "a page is thin"
    assert job["report_url"] == f"/api/analyze/{started['job_id']}/report"

    r = client.get(job["report_url"])
    assert r.status_code == 200
    assert "Site score: **88/100**" in r.text
    assert "GEO score: **71/100**" in r.text
    assert (tmp_path / "reports" / report_name).is_file()


def test_analyze_failed_runner_reports_failed(client):
    def boom(url, country, language):
        raise RuntimeError("the audit crashed")

    client.app.state.analyze_runner = boom
    r = client.post("/api/analyze", json={"url": SITE, "country": "ca"})
    assert r.status_code == 202
    job = _poll_until_done(client, r.json()["job_id"])
    assert job["status"] == "failed"
    assert "the audit crashed" in job["error"]
    assert job["result"] is None


# -- Endpoint: real pipeline (fake fetches), real report file -----------------

def test_analyze_real_pipeline_writes_report(client, tmp_path, monkeypatch):
    """The default runner runs the CLI's pipeline in-process and writes
    the same report file the CLI writes — both scores in the file."""
    monkeypatch.setattr(analyze, "http_fetch", _fake_fetch)
    r = client.post("/api/analyze",
                    json={"url": SITE, "country": "ca", "language": "en"})
    assert r.status_code == 202
    job = _poll_until_done(client, r.json()["job_id"], timeout=15.0)
    assert job["status"] == "done", job.get("error")
    assert isinstance(job["result"]["site_score"], int)
    assert isinstance(job["result"]["geo_score"], int)
    reports = list((tmp_path / "reports").glob("analyze_*.md"))
    assert len(reports) == 1
    assert "example-com" in reports[0].name
    text = reports[0].read_text(encoding="utf-8")
    assert "Site score: **" in text
    assert "GEO score: **" in text
    assert "no auto-publish" in text
    assert "everything here is a draft for the owner to act on" in text
    r = client.get(job["report_url"])
    assert r.status_code == 200
    assert "Site score: **" in r.text


def test_analyze_unreachable_site_has_no_scores(client, monkeypatch):
    """A site that cannot be fetched gets no scores — the panel shows
    'Unavailable' with the reason instead of a number built from
    fetch failures."""
    monkeypatch.setattr(analyze, "http_fetch", _dead_fetch)
    r = client.post("/api/analyze", json={"url": SITE, "country": "ca"})
    assert r.status_code == 202
    job = _poll_until_done(client, r.json()["job_id"], timeout=15.0)
    assert job["status"] == "done", job.get("error")
    assert job["result"]["fetchable"] is False
    assert job["result"]["site_score"] is None
    assert job["result"]["geo_score"] is None
    assert "could not be fetched" in job["result"]["unavailable_reason"]


# -- No-publish guard ----------------------------------------------------------

def test_analyze_publishes_nothing(client, tmp_path, monkeypatch):
    """The job's only output is the local report file: no other file
    appears, and the panel says so on the page."""
    monkeypatch.setattr(analyze, "http_fetch", _fake_fetch)
    r = client.post("/api/analyze", json={"url": SITE, "country": "ca"})
    job = _poll_until_done(client, r.json()["job_id"], timeout=15.0)
    assert job["status"] == "done"
    written = [p for p in (tmp_path / "reports").rglob("*") if p.is_file()]
    assert len(written) == 1
    assert written[0].suffix == ".md"

    page = client.get("/ops")
    assert "Nothing is published" in page.text


# -- Served page: analyze panel + carousel ------------------------------------

def test_ops_page_has_analyze_panel(client):
    r = client.get("/ops")
    assert r.status_code == 200
    assert "Analyze a site" in r.text
    assert 'id="analyze-panel"' in r.text
    assert 'id="analyze-form"' in r.text
    assert 'id="analyze-url"' in r.text
    assert 'id="analyze-country"' in r.text
    # Market country defaults to ca — the full ISO selector has ca
    # selected (an <option>, not a text input's value any more).
    assert "<option value='ca' selected>" in r.text
    assert 'Canada (ca)' in r.text
    assert 'id="analyze-language"' in r.text
    assert 'id="analyze-submit"' in r.text
    assert 'id="analyze-status"' in r.text
    assert 'id="analyze-result"' in r.text
    # Async job: POST starts it, the page polls for the result.
    assert 'fetch("/api/analyze"' in r.text
    assert '"/api/analyze/" + encodeURIComponent' in r.text
    assert "pollAnalyzeJob" in r.text
    # The panel stacks single-column on phones, 44px+ tap targets.
    assert "#analyze-panel .analyze-row { flex-direction: column; align-items: stretch; }" in r.text
    assert ".analyze-field input" in r.text
    assert "min-height: 44px" in r.text


def test_ops_page_has_carousel(client):
    r = client.get("/ops")
    assert r.status_code == 200
    # Scroll-snap strip, one card per view, scrollbar hidden.
    assert "scroll-snap-type: x mandatory" in r.text
    assert "scroll-snap-align: start" in r.text
    assert "#stations::-webkit-scrollbar { display: none; }" in r.text
    # Prev/next buttons (44px+ tap targets, aria-labels), dots, "n of 8".
    assert 'id="crew-prev"' in r.text
    assert 'aria-label="Previous agent"' in r.text
    assert 'id="crew-next"' in r.text
    assert 'aria-label="Next agent"' in r.text
    assert 'id="crew-dots"' in r.text
    assert 'id="crew-pos"' in r.text
    assert "1 of 8" in r.text
    # The JS that drives it, and the scroll save/restore across the
    # 5s re-render of the cards.
    assert "buildCrewNav" in r.text
    assert "crewScrollTo" in r.text
    assert "crewUpdateNav" in r.text
    assert "savedScroll" in r.text
    # Desktop grid rule untouched.
    assert "#stations { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }" in r.text
    assert "#stations { grid-template-columns: 1fr; }" not in r.text

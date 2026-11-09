"""Phase 1: the /api/analyze preview is summary-only and rate-limited.

The defect (FIRST_CUSTOMER_BASELINE.md R2): POST /api/analyze was
unauthenticated and unmetered, and it returned the full Markdown
report — the paid product — to anyone. Now the endpoint is the free
preview for everyone, signed in or not: scores, GEO score, top
findings, no report file, no report URL. The full report comes only
from the paid Analyze job on a website page (5 credits — pinned by
test_saas_shell.test_job_analyze_charges_and_reports). Anonymous
previews are limited to 5 per client IP per hour.

The fake runner below returns a report_name on purpose, to prove the
preview strips it rather than trusting the runner.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ecosystem.analyze as analyze
from ventoryn.analyze_jobs import (
    PREVIEW_PER_IP_PER_HOUR,
    new_job_store,
    preview_allowed,
    preview_client_ip,
    record_preview_hit,
)
from ventoryn.app import create_app
from fastapi.testclient import TestClient

from core.ledger import Ledger
from core.registry import BusinessRegistry

from _signin import operator_store_and_config, sign_in, sign_in_raw

SITE = "https://example.com"

GOOD_PAGE = """<html lang="en"><head><title>Example — Home</title>
<meta name="description" content="An example site for testing the home page.">
</head><body><h1>What is Example?</h1>
<p>Example is a small site that shows how the analyzer reads a page for a market.</p>
</body></html>"""


def _fake_fetch(url, timeout=10.0):
    if url.endswith("/robots.txt"):
        return 200, "User-agent: *\nAllow: /\n"
    if url.endswith("/sitemap.xml"):
        return 200, ('<?xml version="1.0"?><urlset '
                     'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                     '<url><loc>https://example.com/</loc></url></urlset>')
    if url.endswith("/llms.txt"):
        return 404, ""
    return 200, GOOD_PAGE


def _fake_runner(url, country, language):
    return {
        "site_score": 90, "geo_score": 80, "fetchable": True,
        "unavailable_reason": None,
        "finding_counts": {"critical": 0, "high": 0, "medium": 0,
                           "low": 0},
        "findings": [{"severity": "low", "area": "content",
                      "issue": "a page is thin", "fix": "add copy"}],
        "report_name": "analyze_example-com_20261108.md",
    }


def _make_client(tmp_path, monkeypatch, signed_in=True, totp=True):
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("ECOSYSTEM_STATE_DB", str(tmp_path / "state.db"))
    store, config = operator_store_and_config()
    app = create_app(registry=BusinessRegistry(), ledger=Ledger(),
                     saas_store=store, saas_config=config)
    client = TestClient(app)
    if signed_in:
        if totp:
            sign_in(client, store)
        else:
            sign_in_raw(client, store)
    return client, store


@pytest.fixture
def anon_client(tmp_path, monkeypatch):
    client, _store = _make_client(tmp_path, monkeypatch, signed_in=False)
    return client


@pytest.fixture
def client(tmp_path, monkeypatch):
    client, _store = _make_client(tmp_path, monkeypatch, signed_in=True)
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


# -- Preview is summary-only -------------------------------------------------

def test_anonymous_preview_is_summary_only(anon_client):
    """Anonymous preview: scores + findings, but no report_url, no
    report name, and the report endpoint 404s."""
    anon_client.app.state.analyze_runner = _fake_runner
    r = anon_client.post("/api/analyze",
                         json={"url": SITE, "country": "ca"})
    assert r.status_code == 202
    job = _poll_until_done(anon_client, r.json()["job_id"])
    assert job["status"] == "done"
    assert job["result"]["site_score"] == 90
    assert job["result"]["geo_score"] == 80
    assert job["result"]["findings"][0]["issue"] == "a page is thin"
    assert job["result"].get("report_name") is None
    assert "report_url" not in job
    assert job.get("report_name") is None
    r = anon_client.get(f"/api/analyze/{job['job_id']}/report")
    assert r.status_code == 404


def test_anonymous_preview_writes_no_report_file(anon_client, tmp_path,
                                                 monkeypatch):
    """The real pipeline, run as an anonymous preview, writes no
    report file at all."""
    monkeypatch.setattr(analyze, "http_fetch", _fake_fetch)
    r = anon_client.post("/api/analyze",
                         json={"url": SITE, "country": "ca"})
    assert r.status_code == 202
    job = _poll_until_done(anon_client, r.json()["job_id"], timeout=15.0)
    assert job["status"] == "done", job.get("error")
    assert isinstance(job["result"]["site_score"], int)
    reports = list((tmp_path / "reports").glob("analyze_*.md"))
    assert reports == []
    assert "report_url" not in job


def test_signed_in_preview_is_summary_only(client):
    """Signed-in users get the same summary-only preview: the full
    report is not free with a login either — it comes only from the
    paid Analyze job (5 credits)."""
    client.app.state.analyze_runner = _fake_runner
    r = client.post("/api/analyze", json={"url": SITE, "country": "ca"})
    assert r.status_code == 202
    job = _poll_until_done(client, r.json()["job_id"])
    assert job["status"] == "done"
    assert job["result"]["site_score"] == 90
    assert "report_url" not in job
    assert job["result"].get("report_name") is None
    r = client.get(f"/api/analyze/{job['job_id']}/report")
    assert r.status_code == 404


def test_unverified_session_cannot_use_preview(tmp_path, monkeypatch):
    """The /api/analyze carve-out is gone from the forced-2FA gate: a
    signed-in session that has not finished 2FA is sent to setup,
    like everywhere else. Anonymous requests still pass — they carry
    no session."""
    client, _store = _make_client(tmp_path, monkeypatch, signed_in=True,
                                  totp=False)
    r = client.post("/api/analyze", json={"url": SITE, "country": "ca"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/account/two-factor/setup")


# -- Per-IP rate limit on anonymous previews ---------------------------------

def test_anonymous_preview_rate_limit(anon_client):
    """5 anonymous previews per IP per hour; the 6th is refused with
    429, a plain message, and a Retry-After header."""
    anon_client.app.state.analyze_runner = _fake_runner
    assert PREVIEW_PER_IP_PER_HOUR == 5
    for i in range(5):
        r = anon_client.post("/api/analyze",
                             json={"url": SITE, "country": "ca"})
        assert r.status_code == 202, i
    r = anon_client.post("/api/analyze",
                         json={"url": SITE, "country": "ca"})
    assert r.status_code == 429
    assert "Free preview limit" in r.json()["detail"]
    assert int(r.headers["retry-after"]) > 0


def test_signed_in_preview_not_rate_limited(client):
    """The limit is on anonymous previews only: signed-in usage is
    attributed to an account, so six in a row still start."""
    client.app.state.analyze_runner = _fake_runner
    for i in range(6):
        r = client.post("/api/analyze",
                        json={"url": SITE, "country": "ca"})
        assert r.status_code == 202, i


def test_rejected_preview_attempts_do_not_burn_quota(anon_client):
    """A 400 (bad URL) never counts toward the 5 — only a started
    preview burns quota."""
    for bad in ("", "not a url", "example", "ftp://example.com",
                "http://localhost:8000/ops"):
        r = anon_client.post("/api/analyze",
                             json={"url": bad, "country": "ca"})
        assert r.status_code == 400, bad
    anon_client.app.state.analyze_runner = _fake_runner
    r = anon_client.post("/api/analyze",
                         json={"url": SITE, "country": "ca"})
    assert r.status_code == 202


# -- Limiter units -----------------------------------------------------------

def test_preview_client_ip_prefers_cf_connecting_ip():
    """Production runs behind the Cloudflare tunnel, which sets
    CF-Connecting-IP to the real visitor."""
    assert preview_client_ip({"cf-connecting-ip": "203.0.113.7"},
                             "127.0.0.1") == "203.0.113.7"
    assert preview_client_ip({"CF-Connecting-IP": "203.0.113.7"},
                             "127.0.0.1") == "203.0.113.7"


def test_preview_client_ip_falls_back_to_peer():
    """No tunnel header: the TCP peer. X-Forwarded-For is
    deliberately NOT trusted — any direct caller can forge it."""
    assert preview_client_ip({}, "127.0.0.1") == "127.0.0.1"
    assert preview_client_ip({"x-forwarded-for": "203.0.113.7, 10.0.0.1"},
                             "127.0.0.1") == "127.0.0.1"
    assert preview_client_ip({}, "") == "unknown"


def test_cf_header_from_a_direct_caller_is_ignored():
    """Only the local tunnel (a loopback peer) may name the client.
    A caller hitting the 0.0.0.0 port directly cannot pick its own
    rate-limit key with a forged CF-Connecting-IP."""
    assert preview_client_ip({"cf-connecting-ip": "203.0.113.7"},
                             "198.51.100.20") == "198.51.100.20"
    assert preview_client_ip({"cf-connecting-ip": "203.0.113.7"},
                             "::1") == "203.0.113.7"


def test_preview_allowed_allows_five_then_denies():
    store = new_job_store()
    for _ in range(5):
        allowed, _retry = preview_allowed(store, "203.0.113.7")
        assert allowed
        record_preview_hit(store, "203.0.113.7")
    allowed, retry = preview_allowed(store, "203.0.113.7")
    assert not allowed
    assert retry > 0
    # Another IP is unaffected.
    allowed, _retry = preview_allowed(store, "198.51.100.9")
    assert allowed


def test_preview_window_prunes_stale_hits():
    store = new_job_store()
    for _ in range(5):
        record_preview_hit(store, "203.0.113.7")
    allowed, _retry = preview_allowed(store, "203.0.113.7")
    assert not allowed
    # Age every hit past the window: the IP gets a fresh allowance.
    store["preview_hits"]["203.0.113.7"] = [
        time.monotonic() - 4000 for _ in range(5)]
    allowed, _retry = preview_allowed(store, "203.0.113.7")
    assert allowed

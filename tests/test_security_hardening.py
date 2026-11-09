"""Security hardening: headers, cross-site request refusal, SSRF-safe
fetching, rate limits on the public analysis, secure cookies."""

import socket
import sys
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _signin import operator_store_and_config, sign_in
from core import netguard
from ventoryn.app import create_app
from ventoryn.saas import SaasConfig


def _fast_runner(url, country, language):
    return {"site_score": 90, "geo_score": 80, "fetchable": True,
            "unavailable_reason": None,
            "finding_counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
            "findings": [], "report_name": None}


def _app():
    store, config = operator_store_and_config()
    app = create_app(saas_store=store, saas_config=config)
    app.state.analyze_runner = _fast_runner
    return app, store


# -- headers -----------------------------------------------------------------

def test_security_headers_on_every_page():
    app, _ = _app()
    client = TestClient(app)
    for path in ("/", "/ops", "/admin/login", "/does-not-exist"):
        r = client.get(path, follow_redirects=False)
        h = r.headers
        assert "frame-ancestors 'none'" in h["content-security-policy"], path
        assert "object-src 'none'" in h["content-security-policy"]
        assert h["x-frame-options"] == "DENY"
        assert h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "strict-origin-when-cross-origin"
        assert "camera=()" in h["permissions-policy"]
        assert h["cross-origin-opener-policy"] == "same-origin"


def test_hsts_only_over_https():
    app, _ = _app()
    assert "strict-transport-security" not in TestClient(app).get("/").headers
    store, _ = operator_store_and_config()
    https_app = create_app(saas_store=store, saas_config=SaasConfig(
        base_url="https://ventoryn.app"))
    h = TestClient(https_app).get("/").headers
    assert h["strict-transport-security"].startswith("max-age=31536000")


# -- cross-site requests -----------------------------------------------------

def test_cross_site_post_is_refused():
    app, store = _app()
    client = TestClient(app)
    sign_in(client, store)
    r = client.post("/account/logout",
                    headers={"Origin": "https://evil.example"},
                    follow_redirects=False)
    assert r.status_code == 403
    r = client.post("/api/analyze", json={"url": "example.com", "country": "ca"},
                    headers={"Referer": "https://evil.example/page"})
    assert r.status_code == 403
    r = client.post("/api/analyze", json={"url": "example.com", "country": "ca"},
                    headers={"Origin": "null"})
    assert r.status_code == 403


def test_same_site_post_is_allowed():
    app, _ = _app()
    client = TestClient(app)
    r = client.post("/api/analyze", json={"url": "example.com", "country": "ca"},
                    headers={"Origin": "http://testserver"})
    assert r.status_code == 202


def test_stripe_webhook_is_exempt_from_origin_check():
    app, _ = _app()
    r = TestClient(app).post("/account/billing/webhook", content=b"{}",
                             headers={"Origin": "https://stripe.example"})
    assert r.status_code != 403  # judged by its signature, not its origin


# -- SSRF-safe fetching ------------------------------------------------------

@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.1.2.3", "192.168.0.1", "172.16.5.5", "169.254.169.254",
    "0.0.0.0", "::1", "fe80::1", "::ffff:127.0.0.1", "224.0.0.1"])
def test_internal_addresses_are_blocked(ip):
    assert netguard.is_blocked_address(ip)


@pytest.mark.parametrize("ip", ["93.184.216.34", "1.1.1.1", "198.18.0.7",
                                "2606:4700::1111"])
def test_public_addresses_are_allowed(ip):
    assert not netguard.is_blocked_address(ip)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/",
    "http://[::1]:8000/admin", "http://localhost/", "http://app.localhost/",
    "file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/"])
def test_check_url_blocks_internal_and_non_web(url):
    with pytest.raises(netguard.BlockedAddressError):
        netguard.check_url(url)


def test_hostname_with_any_private_answer_is_blocked(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve",
                        lambda host: ["93.184.216.34", "10.0.0.5"])
    with pytest.raises(netguard.BlockedAddressError):
        netguard.check_url("https://rebind.example.com/")


def test_redirect_to_internal_address_is_refused():
    handler = netguard._GuardedRedirectHandler()
    req = urllib.request.Request("https://example.com/")
    for target in ("http://169.254.169.254/latest/meta-data/",
                   "file:///etc/passwd"):
        with pytest.raises(netguard.BlockedAddressError):
            handler.redirect_request(req, None, 302, "Found", {}, target)


def test_peer_check_catches_rebinding_at_connect_time():
    # A socket that actually landed on loopback is closed before any
    # request byte is sent — whatever DNS said a moment earlier.
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.create_connection(server.getsockname())
    try:
        with pytest.raises(netguard.BlockedAddressError):
            netguard._check_peer(client)
    finally:
        client.close()
        server.close()


def test_http_fetch_reports_blocked_target_as_unreachable():
    from core.site_audit import http_fetch
    assert http_fetch("http://169.254.169.254/latest/meta-data/") == (0, "")


def test_analyze_rejects_host_with_private_dns_answer(monkeypatch):
    import ventoryn.analyze_jobs as jobs
    monkeypatch.setattr(jobs, "_resolved_addresses",
                        lambda host: ["93.184.216.34", "192.168.1.10"])
    with pytest.raises(ValueError):
        jobs.validate_url("https://mixed.example.com")


# -- rate limits on the public analysis -------------------------------------

def test_anonymous_analysis_is_rate_limited_per_client():
    app, _ = _app()
    client = TestClient(app)
    body = {"url": "example.com", "country": "ca"}
    codes = [client.post("/api/analyze", json=body).status_code
             for _ in range(6)]
    assert codes[:5] == [202] * 5
    assert codes[5] == 429


def test_invalid_requests_do_not_use_up_the_limit():
    app, _ = _app()
    client = TestClient(app)
    for _ in range(10):
        assert client.post("/api/analyze",
                           json={"url": "not a url", "country": "ca"}
                           ).status_code == 400
    assert client.post("/api/analyze",
                       json={"url": "example.com", "country": "ca"}
                       ).status_code == 202


def test_global_running_cap():
    app, _ = _app()
    store = app.state.analyze_jobs
    with store["lock"]:
        for i in range(app.state.analyze_max_running):
            store["jobs"][f"busy{i}"] = {"status": "running",
                                         "created_at": f"2026-11-09T00:00:0{i}"}
    r = TestClient(app).post("/api/analyze",
                             json={"url": "example.com", "country": "ca"})
    assert r.status_code == 429
    assert r.headers["retry-after"] == "60"


# -- cookies -----------------------------------------------------------------

def test_cookies_secure_by_default_over_https():
    assert SaasConfig.from_env({"SAAS_BASE_URL": "https://ventoryn.app"}
                               ).cookie_secure is True
    assert SaasConfig.from_env({"SAAS_BASE_URL": "http://localhost:8000"}
                               ).cookie_secure is False
    assert SaasConfig.from_env({"SAAS_BASE_URL": "https://ventoryn.app",
                                "SAAS_COOKIE_SECURE": "0"}).cookie_secure is False
    assert SaasConfig.from_env({"SAAS_COOKIE_SECURE": "1"}).cookie_secure is True

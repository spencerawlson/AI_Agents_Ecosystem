"""Ventoryn Analytics: cookieless first-party analytics for customer sites."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _signin import operator_store_and_config, sign_in
from ventoryn.analytics import (DIRECT, SiteAnalytics, analytics_for,
                                check_install, host_matches, snippet)
from ventoryn.app import create_app

BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def env():
    store, config = operator_store_and_config()
    app = create_app(saas_store=store, saas_config=config)
    client = TestClient(app)
    account = sign_in(client, store, email="ada@example.com", sub="sub-ada",
                      name="Ada")
    site = store.create_site(account["id"], "https://shop.example.com")
    return app, client, store, account, site


def _collect(client, site, url=None, ref="", name="pageview", origin=None,
             ua=BROWSER):
    headers = {"user-agent": ua, "content-type": "text/plain"}
    if origin is not None:
        headers["origin"] = origin
    body = ('{"s":"%s","n":"%s","u":"%s","r":"%s"}'
            % (site["id"], name, url or "https://shop.example.com/", ref))
    return client.post("/api/va/collect", content=body, headers=headers)


# -- script + endpoint --------------------------------------------------------

def test_tracker_script_is_served(env):
    _app, client, *_ = env
    r = client.get("/v.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert "/api/va/collect" in r.text and "sendBeacon" in r.text
    assert "cookie" not in r.text.lower() and "localStorage" not in r.text


def test_pageview_from_the_customers_site_is_recorded(env):
    _app, client, store, _account, site = env
    # Cross-site by design: the CSRF check must not block it.
    r = _collect(client, site, origin="https://shop.example.com")
    assert r.status_code == 204
    s = analytics_for(store).summary(site["id"])
    assert s["installed"] and s["visitors"] == 1 and s["pageviews"] == 1


@pytest.mark.parametrize("kwargs", [
    {"url": "https://evil.example.org/"},                 # page not on the site
    {"origin": "https://evil.example.org"},               # posted from elsewhere
    {"ua": "Googlebot/2.1 (+http://www.google.com/bot.html)"},
    {"ua": ""},
    {"name": "<script>"},
    {"url": "javascript:alert(1)"},
])
def test_bad_events_are_dropped_silently(env, kwargs):
    _app, client, store, _account, site = env
    assert _collect(client, site, **kwargs).status_code == 204
    assert not analytics_for(store).summary(site["id"])["installed"]


def test_unknown_site_gets_the_same_answer(env):
    _app, client, store, _account, site = env
    fake = dict(site, id="0" * 24)
    assert _collect(client, fake).status_code == 204


def test_oversized_body_is_refused(env):
    _app, client, *_ = env
    r = client.post("/api/va/collect", content=b"x" * 5000,
                    headers={"user-agent": BROWSER})
    assert r.status_code == 413


def test_collect_is_rate_limited_per_client(env):
    app, client, _store, _account, site = env
    app.state.va_limiter.limit = 3
    codes = [_collect(client, site).status_code for _ in range(5)]
    assert codes[:3] == [204, 204, 204] and codes[3] == 429


# -- privacy -------------------------------------------------------------------

def test_visitor_is_a_daily_hash_and_raw_data_is_rolled_up(env):
    _app, _client, store, _account, site = env
    va = SiteAnalytics(store)
    for path in ("/", "/pricing"):
        va.record(site, "pageview", f"https://shop.example.com{path}", "",
                  "203.0.113.9", BROWSER, now=NOW)
    va.record(site, "pageview", "https://shop.example.com/", "",
              "203.0.113.9", BROWSER, now=NOW + timedelta(days=1))
    s = va.summary(site["id"], now=NOW + timedelta(days=1))
    # Same person, two days: two daily visitors, three page views.
    assert s["visitors"] == 2 and s["pageviews"] == 3
    # Day one was rolled up: its raw rows and salt are gone.
    rows = [tuple(r) for r in store._conn.execute("SELECT DISTINCT day FROM va_events")]
    assert rows == [("2026-11-10",)]
    salts = [tuple(r) for r in store._conn.execute("SELECT day FROM va_salts")]
    assert salts == [("2026-11-10",)]
    # No IP or user agent anywhere in the database.
    dump = "\n".join(store._conn.iterdump())
    assert "203.0.113.9" not in dump and "Chrome/130" not in dump


def test_sources(env):
    _app, _client, store, _account, site = env
    va = SiteAnalytics(store)
    va.record(site, "pageview", "https://shop.example.com/?utm_source=Newsletter",
              "", "1.1.1.1", BROWSER, now=NOW)
    va.record(site, "pageview", "https://shop.example.com/", "https://www.google.com/",
              "1.1.1.2", BROWSER, now=NOW)
    va.record(site, "pageview", "https://shop.example.com/", "", "1.1.1.3",
              BROWSER, now=NOW)
    # An internal click does not replace the entry source.
    va.record(site, "pageview", "https://shop.example.com/a", "https://shop.example.com/",
              "1.1.1.2", BROWSER, now=NOW)
    sources = dict(va.summary(site["id"], now=NOW)["sources"])
    assert sources == {"newsletter": 1, "google.com": 1, DIRECT: 1}


def test_only_tags_ventoryn_issued_count_as_signed_visits(env):
    _app, _client, store, _account, site = env
    store.record_signature(site["id"], "vtn_act-1.abc123", "act-1",
                           "marketing", "job-1", "https://shop.example.com/")
    va = SiteAnalytics(store)
    va.record(site, "pageview",
              "https://shop.example.com/?utm_source=ventoryn&utm_content=vtn_act-1.abc123",
              "https://reddit.com/", "1.1.1.1", BROWSER, now=NOW)
    va.record(site, "pageview",
              "https://shop.example.com/?utm_content=vtn_forged.zzz",
              "", "1.1.1.2", BROWSER, now=NOW)
    s = va.summary(site["id"], now=NOW)
    assert s["signed_visits"] == 1 and list(s["tags"]) == ["vtn_act-1.abc123"]


# -- the platform ------------------------------------------------------------

def test_site_page_shows_snippet_and_status(env):
    _app, client, _store, _account, site = env
    html = client.get(f"/websites/sites/{site['id']}").text
    assert "Ventoryn Analytics" in html and "Not installed" in html
    assert f'data-site=&quot;{site["id"]}&quot;' in html
    assert "/v.js" in html
    _collect(client, site)
    html = client.get(f"/websites/sites/{site['id']}").text
    assert "Receiving data" in html


def test_funnel_uses_analytics_then_plausible(env):
    _app, client, _store, _account, site = env
    html = client.get(f"/websites/sites/{site['id']}").text
    assert "install <a href=" in html  # not installed, no Plausible
    _collect(client, site)
    _collect(client, site, name="signup")
    html = client.get(f"/websites/sites/{site['id']}").text
    assert "(Ventoryn Analytics)" in html
    analytics = client.get("/analytics").text
    assert "(Ventoryn Analytics)" in analytics


def test_signed_visit_is_attribution_evidence(env):
    _app, client, store, _account, site = env
    store.record_signature(site["id"], "vtn_act-9.def456", "act-9",
                           "marketing", "job-9", "https://shop.example.com/")
    _collect(client, site,
             url="https://shop.example.com/?utm_content=vtn_act-9.def456")
    html = client.get("/report").text
    assert "Ventoryn Analytics recorded a visit through a signed" in html


def test_check_installation(env, monkeypatch):
    _app, client, _store, _account, site = env
    monkeypatch.setattr("core.site_audit.http_fetch", lambda url: (
        200, f"<head>{snippet(site['id'], 'https://ventoryn.app')}</head>"))
    r = client.post(f"/websites/sites/{site['id']}/analytics/check",
                    follow_redirects=True)
    assert "snippet is on your home page" in r.text
    monkeypatch.setattr("core.site_audit.http_fetch",
                        lambda url: (200, "<head></head>"))
    r = client.post(f"/websites/sites/{site['id']}/analytics/check",
                    follow_redirects=True)
    assert "not on your home page yet" in r.text


def test_check_install_unit():
    site = {"id": "abc123", "url": "https://x.example"}
    assert check_install(site, fetch=lambda u: (0, ""))[0] is False
    assert check_install(site, fetch=lambda u: (500, ""))[0] is False
    ok, msg = check_install(site, fetch=lambda u: (200, '<script src="/v.js" data-site="zzz">'))
    assert not ok and "different site id" in msg


def test_host_matching():
    assert host_matches("www.example.com", "example.com")
    assert host_matches("shop.example.com", "www.example.com")
    assert not host_matches("example.com.evil.org", "example.com")
    assert not host_matches("notexample.com", "example.com")

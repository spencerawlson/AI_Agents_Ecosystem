"""Axionory tracks itself with Axionory Analytics (site 8e7c68c8885a1ef3e072a01c)."""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _signin import operator_store_and_config, sign_in
from ventoryn.app import create_app

TAG = '<script defer data-site="8e7c68c8885a1ef3e072a01c" src="/v.js"></script>'


def _client():
    store, config = operator_store_and_config()
    client = TestClient(create_app(saas_store=store, saas_config=config))
    return client, store


def test_landing_and_app_pages_carry_the_tag():
    client, store = _client()
    landing = client.get("/").text
    assert TAG in landing and landing.index(TAG) < landing.index("</head>")
    sign_in(client, store)
    for path in ("/ops", "/portfolio", "/account/dashboard"):
        html = client.get(path).text
        assert html.count(TAG) == 1, path
        assert html.index(TAG) < html.index("</head>"), path


def test_tag_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("VENTORYN_OWN_SITE_ID", "off")
    client, _ = _client()
    assert 'src="/v.js"' not in client.get("/").text.split("</head>")[0]


def test_own_site_counts_visits_on_every_platform_domain(monkeypatch):
    import json
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from _signin import operator_store_and_config, sign_in
    from ventoryn.analytics import analytics_for
    from ventoryn.app import create_app

    store, config = operator_store_and_config()
    config = replace(config, base_url="https://www.axionory.com",
                     extra_base_urls=("https://ventoryn.app",))
    client = TestClient(create_app(saas_store=store, saas_config=config))
    acct = sign_in(client, store, email="op2@example.com", sub="s-op2", name="Op")
    own = store.create_site(acct["id"], "https://www.axionory.com")
    other = store.create_site(acct["id"], "https://shop.example.com")
    monkeypatch.setenv("VENTORYN_OWN_SITE_ID", own["id"])
    ua = {"user-agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0 Safari/537.36"}

    def hit(site, url):
        client.post("/api/va/collect", content=json.dumps({"s": site["id"], "n": "pageview", "u": url, "r": ""}),
                    headers={**ua, "content-type": "text/plain"})

    hit(own, "https://www.axionory.com/")
    hit(own, "https://ventoryn.app/pricing")
    hit(own, "https://evil.example/")                 # not ours: dropped
    hit(other, "https://ventoryn.app/")               # another site: only its own host
    va = analytics_for(store)
    assert va.summary(own["id"])["visitors"] >= 1
    assert va.summary(own["id"])["pageviews"] == 2  # axionory.com + ventoryn.app, not evil.example
    assert va.summary(other["id"])["installed"] is False

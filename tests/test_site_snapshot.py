"""Website snapshot: preview, specs, traffic band, presence — real data only."""

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ventoryn.saas_routes as saas_routes
from _signin import operator_store_and_config, sign_in
from ventoryn import site_snapshot
from ventoryn.app import create_app
from ventoryn.site_screenshot import _allowed

HOME = """<!doctype html><html lang="en"><head>
<title>Ada's Shop — handmade planners</title>
<meta name="description" content="Handmade paper planners shipped across Canada, made to order in small batches.">
<meta name="viewport" content="width=device-width">
<script type="application/ld+json">{"@type":"Organization"}</script>
<script src="https://cdn.shopify.com/s/files/theme.js"></script>
<script defer src="https://plausible.io/js/script.js"></script>
</head><body><h1>Planners</h1>
<a href="https://instagram.com/adasshop">IG</a><a href="https://www.tiktok.com/@adasshop">TT</a>
</body></html>"""
ROBOTS = "User-agent: *\nDisallow: /cart\nUser-agent: GPTBot\nDisallow: /\nSitemap: https://shop.example.com/sitemap.xml\n"


def fake_pages(url):
    if url.endswith("/robots.txt"):
        return {"status": 200, "body": ROBOTS, "headers": {}, "bytes": 0, "ms": 5}
    if url.endswith("/llms.txt"):
        return {"status": 404, "body": "", "headers": {}, "bytes": 0, "ms": 5}
    return {"status": 200, "final_url": "https://shop.example.com/", "body": HOME,
            "headers": {"server": "cloudflare", "cf-ray": "x",
                        "strict-transport-security": "max-age=1"},
            "bytes": len(HOME), "ms": 420}


def fake_json_factory(calls):
    def fake_json(url, headers=None, data=None):
        calls.append((url, headers, data))
        if "tranco-list.eu" in url:
            return 200, {"ranks": [{"date": "2026-11-01", "rank": 812345},
                                   {"date": "2026-11-08", "rank": 754321}]}
        if "chromeuxreport" in url:
            return 404, None
        if "hn.algolia.com" in url:
            return 200, {"nbHits": 2}
        if "web.archive.org" in url:
            return 200, [["timestamp"], ["20190314000000"]]
        if "api.search.brave.com" in url:
            from urllib.parse import unquote
            if "-site:" in unquote(url):
                return 200, {"web": {"results": [{"url": "https://blog.example.org/x"},
                                                 {"url": "https://news.example.net/y"}]}}
            return 200, {"web": {"results": [{"url": "u"}] * 7}}
        return 0, None
    return fake_json


SITE = {"id": "site123", "url": "https://shop.example.com", "host": "shop.example.com"}


def _by_label(items):
    return {i["label"]: i for i in items}


def test_collect_without_keys_is_honest():
    calls = []
    data = site_snapshot.collect(SITE, env={}, page_fetch=fake_pages,
                                 json_get=fake_json_factory(calls))
    p = data["preview"]
    assert p["title"].startswith("Ada's Shop") and "Handmade" in p["description"]
    specs = _by_label(data["specs"])
    assert specs["Platform"]["value"] == "Shopify"
    assert specs["Hosting / CDN"]["value"] == "Cloudflare"
    assert specs["HTTPS"]["value"] == "Yes + HSTS"
    assert specs["Mobile-ready"]["value"] == "Yes"
    assert specs["Sitemap"]["value"] == "Found"
    assert specs["llms.txt (for AI assistants)"]["status"] == "warn"
    assert "Plausible" in specs["Analytics installed"]["value"]
    assert specs["Social profiles linked"]["value"] == "Instagram, TikTok"
    traffic = _by_label(data["traffic"])
    assert traffic["Global popularity rank (Tranco)"]["value"] == "#754,321"
    assert traffic["Traffic size"]["value"].startswith("Established")
    assert traffic["Real Chrome visitors (Google CrUX)"]["status"] == "not_connected"
    presence = _by_label(data["presence"])
    assert presence["In search results (Brave Search)"]["status"] == "not_connected"
    assert presence["Hacker News"]["value"] == "2 submission(s)"
    assert presence["Internet Archive"]["value"] == "First saved 2019-03"
    assert presence["AI assistants allowed to read it"]["value"].startswith("11 of 12")
    # No key -> no call to the keyed APIs at all.
    assert not any("chromeuxreport" in c[0] or "brave" in c[0] for c in calls)


def test_collect_with_keys_uses_them():
    calls = []
    env = {"GOOGLE_CRUX_API_KEY": "crux-k", "BRAVE_SEARCH_API_KEY": "brave-k"}
    data = site_snapshot.collect(SITE, env=env, page_fetch=fake_pages,
                                 json_get=fake_json_factory(calls))
    traffic = _by_label(data["traffic"])
    assert traffic["Real Chrome visitors (Google CrUX)"]["value"].startswith("Too few visitors")
    presence = _by_label(data["presence"])
    assert presence["Pages found in search (Brave)"]["value"] == "7 page(s)"
    assert "blog.example.org" in presence["Mentioned on other sites"]["value"]
    brave = [c for c in calls if "brave" in c[0]]
    assert brave and brave[0][1] == {"X-Subscription-Token": "brave-k"}
    crux = [c for c in calls if "chromeuxreport" in c[0]][0]
    assert crux[2] == {"origin": "https://shop.example.com"}


def test_unreachable_site_reports_it():
    data = site_snapshot.collect(
        SITE, env={}, json_get=lambda *a, **k: (0, None),
        page_fetch=lambda url: {"status": 0, "body": "", "headers": {}, "bytes": 0,
                                "ms": 0, "error": "connection refused"})
    assert data["preview"]["reachable"] is False
    assert data["specs"][0]["status"] == "unavailable"
    assert "connection refused" in data["specs"][0]["value"]


def test_traffic_uses_ventoryn_analytics_when_installed():
    data = site_snapshot.collect(
        SITE, env={}, page_fetch=fake_pages, json_get=lambda *a, **k: (0, None),
        analytics=lambda sid: {"installed": True, "visitors": 321, "pageviews": 900})
    t = _by_label(data["traffic"])
    assert "321 visitors" in t["Visitors, last 30 days (Ventoryn Analytics)"]["value"]


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://169.254.169.254/",
                                 "ws://localhost:8000/", "file:///etc/passwd"])
def test_screenshot_browser_cannot_reach_internal_addresses(url):
    assert not _allowed(url)


def test_size_bands():
    assert site_snapshot.size_band(5_000, None).startswith("Very high")
    assert site_snapshot.size_band(50_000, None).startswith("High")
    assert site_snapshot.size_band(None, True).startswith("Measurable")
    assert site_snapshot.size_band(None, False).startswith("Small")
    assert site_snapshot.size_band(None, None) == "Not ranked publicly"


# -- the platform -------------------------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(saas_routes, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(site_snapshot, "collect", lambda site, **kw: site_snapshot.collect.__wrapped__(site, **kw)
                        if hasattr(site_snapshot.collect, "__wrapped__") else {
                            "collected_at": "2026-11-09T12:00:00+00:00",
                            "preview": {"reachable": True, "title": "Ada's Shop", "description": "Planners",
                                        "screenshot": None, "screenshot_note": "unavailable — test"},
                            "specs": [{"label": "Platform", "value": "Shopify", "status": "ok", "note": ""}],
                            "traffic": [{"label": "Traffic size", "value": "Small", "status": "ok", "note": ""}],
                            "presence": [{"label": "Hacker News", "value": "Never submitted",
                                          "status": "ok", "note": ""}]})
    store, config = operator_store_and_config()
    app = create_app(saas_store=store, saas_config=config)
    client = TestClient(app)
    account = sign_in(client, store, email="ada@example.com", sub="sub-ada", name="Ada")
    return client, store, account, tmp_path


def _wait_done(store, site_id, timeout=5):
    snaps = site_snapshot.snapshot_store_for(store)
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = snaps.get(site_id)
        if s and s["status"] != "running":
            return s
        time.sleep(0.05)
    raise AssertionError("snapshot did not finish")


def test_adding_a_site_starts_the_snapshot_and_page_shows_it(env):
    client, store, account, _ = env
    r = client.post("/websites/sites", data={"url": "https://shop.example.com",
                                            "country": "ca", "language": "en"},
                    follow_redirects=False)
    assert r.status_code == 303
    site = store.list_sites(account["id"])[0]
    assert _wait_done(store, site["id"])["status"] == "done"
    html = client.get(f"/websites/sites/{site['id']}").text
    assert "Website snapshot" in html and "Shopify" in html
    assert "Where it appears online" in html
    assert "Refresh snapshot" in html


def test_refresh_respects_cooldown(env):
    client, store, account, _ = env
    site = store.create_site(account["id"], "https://shop.example.com")
    assert site_snapshot.start(store, site, "unused") is True
    _wait_done(store, site["id"])
    assert site_snapshot.start(store, site, "unused") is False  # cooldown


def test_screenshot_is_owner_only(env):
    client, store, account, reports = env
    site = store.create_site(account["id"], "https://shop.example.com")
    (reports / "snapshots").mkdir()
    (reports / "snapshots" / f"{site['id']}.png").write_bytes(b"\x89PNG fake")
    r = client.get(f"/websites/sites/{site['id']}/snapshot-image")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert "private" in r.headers["cache-control"]
    other = TestClient(client.app)
    sign_in(other, store, email="bob@example.com", sub="sub-bob", name="Bob")
    assert other.get(f"/websites/sites/{site['id']}/snapshot-image").status_code == 404
    assert TestClient(client.app).get(
        f"/websites/sites/{site['id']}/snapshot-image", follow_redirects=False).status_code in (303, 307)


def test_old_site_without_snapshot_gets_one_when_opened(env):
    # Sites added before the snapshot feature never had a run; opening
    # the site page must start one, not show "Looking…" forever.
    client, store, account, _ = env
    site = store.create_site(account["id"], "https://old.example.com")  # no start()
    assert site_snapshot.snapshot_store_for(store).get(site["id"]) is None
    client.get(f"/websites/sites/{site['id']}")
    assert _wait_done(store, site["id"])["status"] == "done"

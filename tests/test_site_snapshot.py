"""Website snapshot: preview, specs, traffic band, presence — real data only."""

import re
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
from ventoryn.saas import SaasStore
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


def test_finish_after_store_closed_is_a_quiet_no_op():
    """Regression: a background snapshot thread (site_snapshot.start)
    is fire-and-forget — nothing joins it before the store closes
    (app shutdown, or a test fixture tearing down). finish() used to
    raise sqlite3.ProgrammingError in that case, an unhandled
    exception in a daemon thread. It is now a quiet no-op — a
    snapshot is a disposable refresh, and mark_running's cooldown
    self-heals a row stuck at "running" either way."""
    store = SaasStore(":memory:")
    snaps = site_snapshot.snapshot_store_for(store)
    assert snaps.mark_running("site123") is True
    store.close()
    snaps.finish("site123", "done", {"ok": True})  # must not raise


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
    assert "321 visitors" in t["Visitors, last 30 days (Axionory Analytics)"]["value"]


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


# -- "how to fix it" tips + generated drafts ---------------------------------

BAD_HOME = """<!doctype html><html><head>
<title>Hi</title>
</head><body>
<a href="/about">About</a><a href="/shop">Shop</a><a href="/about">About again</a>
<a href="https://other.example.com/x">Elsewhere</a><a href="#top">Top</a>
<a href="mailto:hi@example.com">Mail</a><a href="/robots.txt">Robots</a>
<a href="/sitemap.xml">Map</a>
</body></html>"""
BAD_SITE = {"id": "bad1", "url": "http://bad.example.com", "host": "bad.example.com"}


def fake_bad_pages(url):
    if url.endswith(("/robots.txt", "/sitemap.xml", "/llms.txt")):
        return {"status": 404, "body": "", "headers": {}, "bytes": 0, "ms": 5}
    return {"status": 200, "final_url": "http://bad.example.com/", "body": BAD_HOME,
            "headers": {}, "bytes": len(BAD_HOME), "ms": 2400}


def fake_bad_json(url, headers=None, data=None):
    if "tranco-list.eu" in url:
        return 200, {"ranks": []}
    if "hn.algolia.com" in url:
        return 200, {"nbHits": 0}
    if "web.archive.org" in url:
        return 200, [["timestamp"]]
    if "api.search.brave.com" in url:
        return 200, {"web": {"results": []}}
    return 0, None


def _collect_bad():
    return site_snapshot.collect(
        BAD_SITE, env={"BRAVE_SEARCH_API_KEY": "brave-k"},
        page_fetch=fake_bad_pages, json_get=fake_bad_json)


def test_hn_and_archive_never_are_warn():
    # "Never submitted" / "Never archived" are actionable gaps, not green.
    data = _collect_bad()
    presence = _by_label(data["presence"])
    assert presence["Hacker News"]["status"] == "warn"
    assert presence["Internet Archive"]["status"] == "warn"
    assert presence["Pages found in search (Brave)"]["status"] == "warn"
    assert presence["Mentioned on other sites"]["status"] == "warn"


def test_flagged_rows_carry_tips_and_ok_rows_do_not():
    data = _collect_bad()
    html = site_snapshot.render_card(BAD_SITE, {"status": "done", "data": data})
    # 11 flagged Specs rows + 4 flagged Presence rows carry a tip.
    assert html.count("class='v-snap-tip'") == 15
    assert "Add a plain-text file called llms.txt" in html
    assert "Submit the site at news.ycombinator.com/submit" in html
    assert "Save Page Now" in html
    assert "Brave has no submit form" in html
    # Ok rows carry no info badge: the value closes the span directly.
    assert "Custom / not detected</span>" in html
    assert "All (no robots.txt)</span>" in html
    # Tooltip markup: button + describedby + role, one id per tip, no dupes.
    assert "role='tooltip'" in html
    assert "aria-label='How to fix: llms.txt (for AI assistants)'" in html
    ids = re.findall(r"id='(v-snap-tip-\d+)'", html)
    assert len(ids) == 15 and len(set(ids)) == 15
    # 6 of the 15 flagged rows also carry a generated draft.
    assert html.count("<details class='v-snap-gen'>") == 6
    assert "Generate the llms.txt" in html and "Generate the submission" in html
    # The tip's inline code is escaped, never emitted as markup.
    assert "&lt;meta name=" in html
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' not in html


def test_tip_badge_is_round_green_info():
    # Regression: the platform's global `button` rule sets min-height: 44px,
    # which stretched the 16px badge into a tall oval on the live site.
    # The badge must override it, and render as a green circle with a
    # white "i" (the Revenue card's emerald), not an amber "?" outline.
    rule = site_snapshot.SNAPSHOT_CSS.split(".v-snap-tip {", 1)[1].split("}", 1)[0]
    assert "min-height: 0" in rule and "min-width: 0" in rule
    assert "#064e3b" in rule and "#065f46" in rule
    assert "color: #fff" in rule
    data = _collect_bad()
    html = site_snapshot.render_card(BAD_SITE, {"status": "done", "data": data})
    assert ">i</button>" in html
    assert ">?</button>" not in html


def test_title_ignores_svg_titles():
    # Regression: <title> elements inside an inline SVG (a map labelling
    # each region) are accessible names, not the page title. The parser
    # used to concatenate them onto the real title — ShipItAyiti's Haiti
    # map read as a 163-character title for a 47-character one.
    svg_home = """<!doctype html><html lang="en"><head>
<title>ShipItAyiti — The reliable way to ship to Haiti</title>
<meta name="description" content="Boxes, barrels, orders from any online store, and freight from Montreal to every town in Haiti.">
</head><body><h1>The reliable way to ship to Haiti.</h1>
<svg viewBox="0 0 100 100" role="img" aria-label="Map of Haiti">
<g><title>Montreal</title><path d="M1 1"/></g>
<g><title>Miami (soon)</title><path d="M2 2"/></g>
<g><title>Cap-Haïtien</title><path d="M3 3"/></g>
<g><title>Nippes</title><path d="M4 4"/></g>
</svg></body></html>"""

    def fake_svg_pages(url):
        if url.endswith(("/robots.txt", "/sitemap.xml", "/llms.txt")):
            return {"status": 404, "body": "", "headers": {}, "bytes": 0, "ms": 5}
        return {"status": 200, "final_url": "http://svg.example.com/", "body": svg_home,
                "headers": {}, "bytes": len(svg_home), "ms": 100}

    data = site_snapshot.collect(
        {"id": "svg1", "url": "http://svg.example.com", "host": "svg.example.com"},
        env={"BRAVE_SEARCH_API_KEY": "brave-k"},
        page_fetch=fake_svg_pages, json_get=fake_bad_json)
    specs = _by_label(data["specs"])
    assert specs["Title"]["value"] == "47 characters"
    assert specs["Title"]["status"] == "ok"
    assert data["preview"]["title"] == "ShipItAyiti — The reliable way to ship to Haiti"


def test_good_site_has_only_two_tips():
    calls = []
    data = site_snapshot.collect(SITE, env={}, page_fetch=fake_pages,
                                 json_get=fake_json_factory(calls))
    html = site_snapshot.render_card(SITE, {"status": "done", "data": data})
    # Only the two genuinely flagged rows: missing llms.txt, one blocked AI crawler.
    assert html.count("class='v-snap-tip'") == 2
    assert "Add a plain-text file called llms.txt" in html
    assert "blocks some AI crawlers" in html


def test_tip_for_ok_rows_is_none():
    assert site_snapshot._tip_for({"label": "Platform", "value": "Shopify", "status": "ok"}) is None
    assert site_snapshot._tip_for({"label": "Platform", "value": "x", "status": "warn"}) is None
    tip = site_snapshot._tip_for({"label": "llms.txt (for AI assistants)",
                                  "value": "Missing", "status": "warn"})
    assert tip and "llms.txt" in tip["text"] and tip["link"][0] == "https://llmstxt.org/"


def test_tip_links_are_official():
    allowed = {"letsencrypt.org", "schema.org", "search.google.com", "llmstxt.org",
               "news.ycombinator.com", "web.archive.org"}
    for label, entry in site_snapshot._TIPS.items():
        text, link = entry("") if callable(entry) else entry
        assert text and len(text) <= 400, label
        if link:
            from urllib.parse import urlsplit
            assert urlsplit(link[0]).hostname in allowed, (label, link)


def test_generated_drafts_use_only_real_data():
    data = _collect_bad()
    specs = _by_label(data["specs"])
    llms = specs["llms.txt (for AI assistants)"]["generated"]["content"]
    assert "[Home](http://bad.example.com)" in llms
    assert "[About](http://bad.example.com/about)" in llms
    assert "[Shop](http://bad.example.com/shop)" in llms
    assert "other.example.com" not in llms and "robots.txt" not in llms
    assert "mailto" not in llms and "#top" not in llms
    assert llms.count("http://bad.example.com/about") == 1  # deduped
    jd = specs["Structured data"]["generated"]["content"]
    assert '"name": "Hi"' in jd and '"url": "http://bad.example.com/"' in jd
    assert "description" not in jd and "inLanguage" not in jd  # missing -> omitted, not invented
    robots = specs["robots.txt"]["generated"]["content"]
    assert robots.startswith("User-agent: *\nAllow: /\n")
    assert "Sitemap:" not in robots  # no sitemap exists, so none is claimed
    sitemap = specs["Sitemap"]["generated"]["content"]
    assert "<loc>http://bad.example.com/about</loc>" in sitemap
    assert "other.example.com" not in sitemap
    hn = _by_label(data["presence"])["Hacker News"]["generated"]["content"]
    assert hn == "Title: Hi\nURL: http://bad.example.com/"
    title = specs["Title"]["generated"]["content"]
    assert title == "Hi — bad.example.com"  # built from the page's own words only
    assert specs["Meta description"].get("generated") is None  # "Hi" cannot make 50 chars


def test_no_generate_where_not_draftable():
    data = _collect_bad()
    presence = _by_label(data["presence"])
    assert "generated" not in presence["Pages found in search (Brave)"]
    assert "generated" not in presence["Mentioned on other sites"]
    assert "generated" not in presence["Internet Archive"]
    traffic = _by_label(data["traffic"])
    assert "generated" not in traffic["Visitors (real numbers)"]
    assert "generated" not in traffic["Global popularity rank (Tranco)"]


def test_draft_helpers():
    snap = site_snapshot
    assert snap._same_origin_pages("https://x.example.com", [
        "/about", "https://x.example.com/about/", "/about?y=1", "https://elsewhere.example.org/",
        "#top", "mailto:a@b.c", "/robots.txt", "/sitemap.xml", "/llms.txt", "/shop",
    ]) == ["https://x.example.com", "https://x.example.com/about", "https://x.example.com/shop"]
    assert snap._draft_llms_txt("https://x.example.com", "Shop", "Nice shop", ["/about"]) == (
        "# Shop\n\n> Nice shop\n\n## Pages\n\n- [Home](https://x.example.com)\n"
        "- [About](https://x.example.com/about)\n")
    assert snap._draft_robots_txt("https://x.example.com", True) == (
        "User-agent: *\nAllow: /\nSitemap: https://x.example.com/sitemap.xml\n")
    assert snap._draft_robots_txt("https://x.example.com", False) == "User-agent: *\nAllow: /\n"
    assert snap._draft_sitemap_xml("https://x.example.com", ["/a&b"]) == (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        '  <url>\n    <loc>https://x.example.com</loc>\n  </url>\n'
        '  <url>\n    <loc>https://x.example.com/a&amp;b</loc>\n  </url>\n</urlset>\n')
    assert snap._draft_hn_submission("", "Big Launch", "", "https://x.example.com") == (
        "Title: Big Launch\nURL: https://x.example.com/")
    assert snap._draft_hn_submission("", "", "", "https://x.example.com") is None
    long_desc = ("word " * 40).strip()
    trimmed = snap._draft_meta_description("T", "H", long_desc)
    assert 50 <= len(trimmed) <= 155 and not trimmed.endswith("wor")
    assert snap._draft_meta_description(
        "Ada's Shop — handmade planners shipped across Canada", "Planners", "") == (
        "Planners — Ada's Shop — handmade planners shipped across Canada")
    assert snap._draft_meta_description("Hi", "", "") is None
    assert snap._draft_title("A" * 80, "", "", "x.example.com") == "A" * 60
    assert snap._draft_title("", "Planners", "Ada's Shop", "x.example.com") == (
        "Planners — Ada's Shop — x.example.com")
    assert snap._draft_title("", "", "", "x.io") is None

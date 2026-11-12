"""Ventoryn's own robots.txt, sitemap.xml and llms.txt."""

import sys
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _signin import operator_store_and_config
from ventoryn.app import create_app


def _client():
    store, config = operator_store_and_config()
    config = replace(config, base_url="https://ventoryn.app")
    return TestClient(create_app(saas_store=store, saas_config=config))


def test_robots_points_to_sitemap_and_hides_private_areas():
    r = _client().get("/robots.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "Sitemap: https://ventoryn.app/sitemap.xml" in r.text
    for p in ("/account/", "/api/", "/admin", "/integrations"):
        assert f"Disallow: {p}" in r.text


def test_sitemap_lists_only_the_public_home_page():
    r = _client().get("/sitemap.xml")
    assert r.status_code == 200 and "xml" in r.headers["content-type"]
    assert "<loc>https://ventoryn.app/</loc>" in r.text
    assert r.text.count("<loc>") == 3  # home, privacy, terms


def test_llms_txt_is_public_plain_text():
    r = _client().get("/llms.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert r.text.startswith("# Ventoryn\n")
    assert "$20/month" in r.text and "https://ventoryn.app/" in r.text
    assert "{base}" not in r.text


def test_meta_description_fits_search_results():
    import html, re
    page = _client().get("/").text
    m = re.search(r'<meta name="description" content="([^"]*)">', page)
    assert m and 50 <= len(html.unescape(m.group(1))) <= 160

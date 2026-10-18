"""Tests for the SEO engine: inventory crawl, quality gate, publishers,
and the creative agent's seo_article action (mocked LLM, real gate)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.seo_content import (SiteInventory, SitePage, build_jsonld,
                              fetch_inventory, render_markdown, slugify,
                              suggest_internal_links,
                              validate_article_package, word_count)
from core.seo_publish import (FileExportPublisher, PublisherError,
                              WordPressPublisher, markdown_to_html)

SITE = "https://example.com"
PAGES = [
    SitePage(f"{SITE}/", "Home — Example"),
    SitePage(f"{SITE}/guides/delivery-checklist", "Delivery Checklist Guide"),
    SitePage(f"{SITE}/guides/proof-of-delivery", "Proof of Delivery Guide"),
    SitePage(f"{SITE}/pricing", "Pricing"),
    SitePage(f"{SITE}/features/photo-proof", "Photo Proof Features"),
]


def _inventory():
    return SiteInventory(site_url=SITE, pages=list(PAGES))


def _good_pkg():
    body_sections = []
    for i in range(6):
        body_sections.append(
            f"## Section {i}\n\n"
            + " ".join(["Practical delivery teams confirm each drop with a "
                        "photo, a timestamp and a customer signature so "
                        "disputes end before they start."] * 12))
    body = ("# Proof of Delivery: The Complete Guide\n\n"
            "Proof of delivery matters for every route.\n\n"
            + "\n\n".join(body_sections)
            + f"\n\nStart with our [delivery checklist]({SITE}/guides/delivery-checklist), "
              f"read the [proof of delivery guide]({SITE}/guides/proof-of-delivery), "
              f"and see [photo proof features]({SITE}/features/photo-proof).")
    return {
        "title": "Proof of Delivery: A Complete Guide for Delivery Teams",
        "meta_description": ("How proof of delivery works, what to capture "
                             "at the door, and how photo proof ends disputes "
                             "for delivery teams of any size today."),
        "slug": "proof-of-delivery-guide",
        "target_keyword": "proof of delivery",
        "body_markdown": body,
        "faq": [
            {"q": "What is proof of delivery?",
             "a": "Evidence a delivery was completed: photo, time, signature."},
            {"q": "Why does photo proof matter?",
             "a": "It resolves disputes with objective evidence."},
            {"q": "What should a POD include?",
             "a": "Photo, timestamp, location and recipient confirmation."},
        ],
        "internal_links": [
            f"{SITE}/guides/delivery-checklist",
            f"{SITE}/guides/proof-of-delivery",
            f"{SITE}/features/photo-proof",
        ],
    }


# -- inventory ---------------------------------------------------------------


def test_slugify():
    assert slugify("Proof of Delivery: A Guide!") == "proof-of-delivery-a-guide"
    assert slugify("  Multiple   Spaces  ") == "multiple-spaces"


def test_fetch_inventory_from_sitemap():
    sitemap = (
        '<?xml version="1.0"?><urlset>'
        f"<url><loc>{SITE}/</loc></url>"
        f"<url><loc>{SITE}/pricing</loc></url></urlset>")
    pages = {
        f"{SITE}/": "<html><head><title>Home</title></head></html>",
        f"{SITE}/pricing": "<html><head><title>Pricing</title></head></html>",
    }

    def fetcher(url, timeout=15):
        if url.endswith("/sitemap.xml"):
            return 200, sitemap
        return 200, pages.get(url, "")

    inv = fetch_inventory(SITE, fetcher=fetcher)
    assert inv.urls == {f"{SITE}/", f"{SITE}/pricing"}
    assert inv.pages[1].title == "Pricing"


def test_fetch_inventory_requires_sitemap():
    with pytest.raises(ValueError):
        fetch_inventory(SITE, fetcher=lambda url, timeout=15: (404, ""))


def test_suggest_internal_links_prefers_topical_pages():
    links = suggest_internal_links("proof of delivery", _inventory(), limit=3)
    urls = [c["url"] for c in links]
    assert f"{SITE}/guides/proof-of-delivery" in urls
    assert all(u in _inventory().urls for u in urls)


# -- quality gate ------------------------------------------------------------


def test_good_package_passes_gate():
    pkg = _good_pkg()
    assert word_count(pkg["body_markdown"]) >= 900
    result = validate_article_package(pkg, _inventory())
    assert result["errors"] == []


def test_gate_blocks_hallucinated_link():
    pkg = _good_pkg()
    pkg["body_markdown"] += f"\n\nSee [invented]({SITE}/no-such-page)."
    pkg["internal_links"].append(f"{SITE}/no-such-page")
    result = validate_article_package(pkg, _inventory())
    assert any("not on the live site" in e for e in result["errors"])


def test_gate_blocks_slug_collision():
    pkg = _good_pkg()
    pkg["slug"] = "pricing"
    result = validate_article_package(pkg, _inventory())
    assert any("collides" in e for e in result["errors"])


def test_gate_blocks_thin_and_stuffed():
    pkg = _good_pkg()
    pkg["body_markdown"] = ("## Only\n\nproof of delivery " * 40
                             + f"[a]({SITE}/pricing) [b]({SITE}/) "
                             + f"[c]({SITE}/guides/proof-of-delivery)")
    pkg["internal_links"] = [f"{SITE}/pricing"]
    result = validate_article_package(pkg, _inventory())
    assert any("words" in e or "density" in e for e in result["errors"])


def test_gate_blocks_missing_faq_and_placeholder():
    pkg = _good_pkg()
    pkg["faq"] = [{"q": "One?", "a": "Only one."}]
    pkg["body_markdown"] += "\n\nTODO: write this section"
    result = validate_article_package(pkg, _inventory())
    assert any("FAQ" in e for e in result["errors"])
    assert any("placeholder" in e for e in result["errors"])


# -- structured data + rendering ----------------------------------------------


def test_jsonld_and_render():
    pkg = _good_pkg()
    ld = build_jsonld(pkg, SITE, author="Example Co")
    assert ld["@graph"][0]["@type"] == "BlogPosting"
    assert ld["@graph"][1]["@type"] == "FAQPage"
    assert len(ld["@graph"][1]["mainEntity"]) == 3
    md = render_markdown(pkg, ld)
    assert md.startswith("---")
    assert "application/ld+json" in md
    assert pkg["body_markdown"][:40] in md


def test_markdown_to_html_subset():
    html = markdown_to_html(
        "## Head\n\nSome **bold** text and [a link](https://x.io/p).\n\n"
        "- one\n- two\n\nAfter list.")
    assert "<h2>Head</h2>" in html
    assert "<strong>bold</strong>" in html
    assert '<a href="https://x.io/p">a link</a>' in html
    assert html.count("<li>") == 2
    assert html.endswith("<p>After list.</p>")


# -- publishers ---------------------------------------------------------------


def test_file_export_writes_and_refuses_overwrite(tmp_path):
    pub = FileExportPublisher(tmp_path)
    out = pub.publish(_good_pkg(), SITE, author="Example Co")
    assert Path(out["markdown"]).exists()
    assert Path(out["schema"]).exists()
    with pytest.raises(PublisherError):
        pub.publish(_good_pkg(), SITE)


def test_wordpress_publisher_posts_draft(monkeypatch):
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"id": 42, "link": "https://example.com/?p=42", "status": "draft", "slug": "proof-of-delivery-guide"}'

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        import json as _json
        captured["payload"] = _json.loads(req.data.decode())
        return FakeResp()

    monkeypatch.setattr("core.seo_publish.urllib.request.urlopen",
                        fake_urlopen)
    pub = WordPressPublisher(SITE, "editor", "app-pass")
    out = pub.create_post(_good_pkg())
    assert out["id"] == 42 and out["status"] == "draft"
    assert captured["url"] == f"{SITE}/wp-json/wp/v2/posts"
    assert captured["payload"]["status"] == "draft"
    assert captured["payload"]["slug"] == "proof-of-delivery-guide"
    assert "application/ld+json" in captured["payload"]["content"]


def test_wordpress_publisher_requires_credentials():
    with pytest.raises(PublisherError):
        WordPressPublisher(SITE, "", "")


# -- creative agent action -----------------------------------------------------


def _fake_gateway(pkg):
    gw_cls = MagicMock()
    gw_cls.enabled.return_value = True
    gw_cls.SMART = "smart"
    inst = MagicMock()
    inst.complete.return_value = {
        "text": "{}", "json": pkg, "model": "fake",
        "input_tokens": 900, "output_tokens": 2400, "cost_usd": 0.05,
    }
    gw_cls.return_value = inst
    return gw_cls


def test_seo_article_action_runs_gate():
    from agents.creative.agent import CreativeAgent
    from orchestrator.models import Task

    inv = _inventory()
    pkg = _good_pkg()
    task = Task(agent_type="creative", business_id="biz_1",
                inputs={"action": "seo_article",
                        "keyword": "proof of delivery",
                        "niche": "proof of delivery",
                        "site_url": SITE,
                        "facts": ["Example is a delivery tool"],
                        "link_candidates": [
                            {"url": u, "title": ""} for u in pkg["internal_links"]],
                        "inventory_urls": sorted(inv.urls),
                        "word_target": 1200},
                budget_usd=10.0, budget_tokens=50000)
    with patch("core.llm.LLMGateway", _fake_gateway(pkg)):
        out = CreativeAgent()(task)
    assert out["package"]["slug"] == "proof-of-delivery-guide"
    assert out["quality"]["errors"] == []
    assert out["asset_id"]

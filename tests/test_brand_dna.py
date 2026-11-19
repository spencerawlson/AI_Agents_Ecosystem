"""Brand DNA: learn a brand from its URL in one step — evidence only."""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_saas_shell import _account, _login, client, config, store  # noqa: F401
from ventoryn import brand_dna, campaigns
from ventoryn.saas import SaasStore

HOME = """<!doctype html><html lang="en"><head>
<title>Ada's Shop — handmade planners</title>
<meta name="description" content="Handmade paper planners shipped across Canada, made to order in small batches.">
<meta property="og:site_name" content="Ada's Shop">
<meta name="theme-color" content="#1A4D2E">
<style>.hero { background: #f5efe0; color: #c0392b; }</style>
<script type="application/ld+json">{"@type": "Product", "name": "Weekly Planner"}</script>
<script type="application/ld+json">{"@graph": [{"@type": "Product", "name": "Monthly Planner"}]}</script>
</head><body>
<h1>Planners for busy parents</h1>
<p>Welcome! We love making premium handcrafted planners for busy parents
and for small businesses. Every planner ships with free shipping and a
money-back guarantee.</p>
<p>Limited time sale — 20% off this week!</p>
<p style="color: #2c3e50;">Made to order in small batches.</p>
<a href="/products/weekly-planner">Weekly Planner</a>
<a href="/shop/gift-cards">Gift Cards</a>
<a href="/about">Our story</a>
</body></html>"""

BARE = ("<!doctype html><html><head><title>Untitled</title></head>"
        "<body><p>Nothing here yet.</p></body></html>")

SITE = {"id": "site123", "url": "https://shop.example.com",
        "host": "shop.example.com"}


def fake_home(url):
    return {"status": 200, "final_url": "https://shop.example.com/",
            "body": HOME, "headers": {}, "bytes": len(HOME), "ms": 5}


def fake_bare(url):
    return {"status": 200, "final_url": "https://bare.example/",
            "body": BARE, "headers": {}, "bytes": len(BARE), "ms": 5}


def fake_down(url):
    return {"status": 0, "final_url": url, "headers": {}, "body": "",
            "bytes": 0, "ms": 0, "error": "connection refused"}


class FakeLLM:
    def __init__(self):
        self.prompts = []

    def complete(self, prompt, **kw):
        self.prompts.append(prompt)
        return {"json": {"posts": [{"text": "Post one",
                                    "link": "https://shop.example.com"}]}}


def test_extract_reads_tone_colours_products_audience_triggers():
    profile = brand_dna.extract_profile(SITE["url"], HOME,
                                        final_url="https://shop.example.com/")
    assert profile["name"] == "Ada's Shop"
    assert "Handmade paper planners" in profile["tagline"]
    assert profile["language"] == "en"
    assert profile["tone"] == ["warm", "premium"]
    assert profile["colours"] == ["#1a4d2e", "#f5efe0", "#c0392b", "#2c3e50"]
    assert [p["name"] for p in profile["products"]] == [
        "Weekly Planner", "Monthly Planner", "Gift Cards"]
    assert profile["audience"] == ["small businesses", "busy parents"] \
        or set(profile["audience"]) == {"small businesses", "busy parents"}
    assert set(profile["buying_triggers"]) == {
        "free shipping", "money-back guarantee", "limited time", "sale",
        "% off"}
    assert profile["reachable"] is True


def test_extract_empty_evidence_stays_empty():
    profile = brand_dna.extract_profile("https://bare.example/", BARE)
    assert profile["name"] == "Untitled"
    assert profile["tagline"] == ""
    assert profile["language"] == ""
    assert profile["tone"] == []
    assert profile["colours"] == []
    assert profile["products"] == []
    assert profile["audience"] == []
    assert profile["buying_triggers"] == []


def test_marker_words_do_not_fire_inside_other_words():
    # "fun" in "refund" is not playful; "sale" in "wholesale" is not a sale.
    body = ("<html><body><p>Refund policy: wholesale prices for our "
            "fundamental range.</p></body></html>")
    profile = brand_dna.extract_profile("https://x.example/", body)
    assert profile["tone"] == []
    assert profile["buying_triggers"] == []


def test_validate_url():
    assert brand_dna.validate_url("shop.example.com") == "https://shop.example.com"
    assert brand_dna.validate_url("https://shop.example.com/") == "https://shop.example.com/"
    with pytest.raises(ValueError):
        brand_dna.validate_url("not a url")
    with pytest.raises(ValueError):
        brand_dna.validate_url("http://localhost:8000")
    with pytest.raises(ValueError):
        brand_dna.validate_url("http://127.0.0.1/")


def test_storage_round_trip_per_site():
    store = SaasStore(":memory:")
    other = {"id": "site999", "url": "https://other.example",
             "host": "other.example"}
    brand_dna.learn(store, SITE, page_fetch=fake_home)
    brand_dna.learn(store, other, page_fetch=fake_bare)
    mine = brand_dna.brand_dna_store_for(store).get("site123")
    theirs = brand_dna.brand_dna_store_for(store).get("site999")
    assert mine["status"] == "done"
    assert mine["data"]["name"] == "Ada's Shop"
    assert "Weekly Planner" in [p["name"] for p in mine["data"]["products"]]
    assert theirs["status"] == "done"
    assert theirs["data"]["products"] == []
    assert brand_dna.brand_dna_store_for(store).get("nope") is None
    store.close()


def test_learn_unreachable_is_failed_not_a_profile():
    store = SaasStore(":memory:")
    profile = brand_dna.learn(store, SITE, page_fetch=fake_down)
    assert profile["reachable"] is False
    rec = brand_dna.brand_dna_store_for(store).get("site123")
    assert rec["status"] == "failed"
    assert "connection refused" in rec["data"]["error"]
    store.close()


def test_start_runs_in_background_and_cooldown_blocks_second():
    store = SaasStore(":memory:")
    assert brand_dna.start(
        store, SITE,
        collector=lambda s: brand_dna.collect(s, page_fetch=fake_home)) is True
    for _ in range(100):
        rec = brand_dna.brand_dna_store_for(store).get("site123")
        if rec and rec["status"] == "done":
            break
        time.sleep(0.05)
    assert rec["status"] == "done"
    assert rec["data"]["name"] == "Ada's Shop"
    # Refresh cooldown: a second learn right away is refused.
    assert brand_dna.start(store, SITE) is False
    assert brand_dna.learn(store, SITE, page_fetch=fake_home) is None
    store.close()


def test_draft_posts_uses_profile_when_present():
    site = {"id": "s1", "url": "https://shop.example.com", "country": "ca",
            "language": "en"}
    profile = brand_dna.extract_profile(SITE["url"], HOME,
                                        final_url="https://shop.example.com/")
    llm = FakeLLM()
    posts = campaigns.draft_posts(site, days=1, posts_per_day=1,
                                  with_link=False, llm=llm, inventory=None,
                                  brand_profile=profile)
    assert len(posts) == 1
    prompt = llm.prompts[0]
    assert "Tone of voice: warm, premium." in prompt
    assert "Products: Weekly Planner, Monthly Planner, Gift Cards." in prompt
    assert "Who it is for: small businesses, busy parents." in prompt \
        or "Who it is for: busy parents, small businesses." in prompt
    assert "What makes buyers act:" in prompt and "free shipping" in prompt


def test_draft_posts_prompt_unchanged_without_profile():
    site = {"id": "s1", "url": "https://shop.example.com", "country": "ca",
            "language": "en"}
    llm_plain = FakeLLM()
    campaigns.draft_posts(site, days=1, posts_per_day=1, with_link=False,
                          llm=llm_plain, inventory=None)
    llm_none = FakeLLM()
    campaigns.draft_posts(site, days=1, posts_per_day=1, with_link=False,
                          llm=llm_none, inventory=None, brand_profile=None)
    assert llm_plain.prompts[0] == llm_none.prompts[0]
    assert "Brand voice" not in llm_plain.prompts[0]
    # An empty profile (nothing learned) adds no brand section either.
    llm_empty = FakeLLM()
    empty = {"tone": [], "products": [], "audience": [],
             "buying_triggers": []}
    campaigns.draft_posts(site, days=1, posts_per_day=1, with_link=False,
                          llm=llm_empty, inventory=None,
                          brand_profile=empty)
    assert llm_empty.prompts[0] == llm_plain.prompts[0]


def test_site_detail_shows_brand_dna_card(client, store):
    acct = _login(client, store)
    site = store.create_site(acct["id"], "https://shop.example.com")
    brand_dna.learn(store, site, page_fetch=fake_home)
    html = client.get(f"/websites/sites/{site['id']}").text
    assert "Brand DNA" in html
    assert "Weekly Planner" in html
    assert "warm, premium" in html
    assert "free shipping" in html


def test_site_detail_without_profile_says_not_learned_yet(client, store):
    acct = _login(client, store)
    site = store.create_site(acct["id"], "https://bare.example")
    html = client.get(f"/websites/sites/{site['id']}").text
    assert "Brand DNA" in html
    # The background learn may or may not have finished in the test's
    # lifetime; either state is honest — never an invented profile.
    assert ("Learning your brand" in html
            or "Not found on the site yet" in html
            or "could not be learned" in html)

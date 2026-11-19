"""Languages (Gap 7): validate, store per site, generate in the language,
tone per language.

No live LLM calls anywhere: generation is asserted on the prompt the
code builds, with fake LLMs. No network.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_saas_shell import _account, _login, client, config, store  # noqa: F401
from test_seo_engine import _good_pkg, _inventory  # noqa: F401
from ventoryn import campaigns
from ventoryn.analyze_jobs import validate_language as analyze_validate
from ventoryn.languages import (
    DEFAULT_LANGUAGE,
    language_datalist,
    language_options,
    resolve_language,
    tone_for_language,
    validate_language,
)


# -- validation ---------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ("en", "en"),
    ("fr", "fr"),
    ("EN-CA", "en-ca"),
    ("fr-ca", "fr-ca"),
    ("zh-hans", "zh-hans"),
    ("zh-Hant", "zh-hant"),
    ("es-419", "es-419"),
    ("sr-latn", "sr-latn"),
    ("de-ch-1901", "de-ch-1901"),
    ("pt-br", "pt-br"),
    ("  ht  ", "ht"),
])
def test_validate_language_accepts_well_formed_tags(raw, want):
    assert validate_language(raw) == want


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_validate_language_empty_is_none(raw):
    assert validate_language(raw) is None


@pytest.mark.parametrize("bad", [
    "english", "e", "abcdef", "en--ca", "en-ca-", "-en", "en-",
    "123", "en_ca", "en ca", "a1", "en-1", "e1-ca", "en-c",
])
def test_validate_language_rejects_garbage(bad):
    with pytest.raises(ValueError):
        validate_language(bad)


def test_analyze_jobs_validate_language_delegates():
    # Same import path as before, same empty-means-None behaviour,
    # wider acceptance.
    assert analyze_validate("") is None
    assert analyze_validate("EN-CA") == "en-ca"
    assert analyze_validate("zh-hans") == "zh-hans"
    with pytest.raises(ValueError):
        analyze_validate("english")


def test_resolve_language_falls_back_to_english():
    assert resolve_language("") == "en"
    assert resolve_language(None) == "en"
    assert resolve_language("fr") == "fr"
    assert resolve_language(None, default="fr") == "fr"
    with pytest.raises(ValueError):
        resolve_language("english")


# -- storage: the site's language choice, per site ----------------------------

def test_create_site_stores_the_full_tag(store):
    acct = _account(store)
    assert store.create_site(acct["id"], "https://shop.example.com",
                             "ca", "fr-ca")["language"] == "fr-ca"
    assert store.create_site(acct["id"], "https://blog.example.org",
                             "ca", "zh-hans")["language"] == "zh-hans"


def test_create_site_defaults_to_english(store):
    acct = _account(store)
    site = store.create_site(acct["id"], "https://shop.example.com")
    assert site["language"] == "en"


def test_create_site_rejects_a_bad_tag(store):
    acct = _account(store)
    with pytest.raises(ValueError):
        store.create_site(acct["id"], "https://shop.example.com", "ca", "english")


def test_set_site_language_round_trips_per_site(store):
    acct = _account(store)
    a = store.create_site(acct["id"], "https://shop.example.com")
    b = store.create_site(acct["id"], "https://blog.example.org")
    assert store.set_site_language(a["id"], "es-419") == "es-419"
    assert store.get_site(a["id"])["language"] == "es-419"
    assert store.get_site(b["id"])["language"] == "en"  # untouched
    assert store.set_site_language(a["id"], "") == "en"  # empty → English
    assert store.get_site(a["id"])["language"] == "en"


def test_set_site_language_rejects_garbage_and_keeps_the_old_value(store):
    acct = _account(store)
    site = store.create_site(acct["id"], "https://shop.example.com", "ca", "fr")
    with pytest.raises(ValueError):
        store.set_site_language(site["id"], "not a language")
    assert store.get_site(site["id"])["language"] == "fr"


# -- generation: the language reaches the prompt -------------------------------

class _CapturingLLM:
    """Fake LLM: records the prompt, returns canned posts."""

    def __init__(self, posts):
        self.posts = posts
        self.prompt = None

    def complete(self, prompt, **kw):
        self.prompt = prompt
        return {"json": {"posts": self.posts}}


def _draft(site, llm, **kw):
    return campaigns.draft_posts(
        site, days=1, posts_per_day=1, with_link=True, llm=llm,
        inventory=SimpleNamespace(pages=[]), **kw)


def test_draft_posts_prompt_carries_the_site_language(store):
    acct = _account(store)
    site = store.create_site(acct["id"], "https://shop.example.com", "ca", "fr-ca")
    llm = _CapturingLLM([{"text": "Bonjour", "link": site["url"]}])
    _draft(site, llm)
    assert "language: fr-ca" in llm.prompt
    assert "Write in this language: fr-ca." in llm.prompt


def test_draft_posts_defaults_to_english_when_the_site_has_no_language():
    site = {"id": "s1", "account_id": "a1", "url": "https://shop.example.com"}
    llm = _CapturingLLM([{"text": "Hello", "link": site["url"]}])
    _draft(site, llm)
    assert "language: en" in llm.prompt
    assert "Write in this language: en." in llm.prompt


def test_draft_posts_falls_back_to_english_for_an_empty_language(store):
    acct = _account(store)
    site = store.create_site(acct["id"], "https://shop.example.com")
    site["language"] = ""
    llm = _CapturingLLM([{"text": "Hello", "link": site["url"]}])
    _draft(site, llm)
    assert "language: en" in llm.prompt


# -- tone per language ---------------------------------------------------------

def test_tone_for_language_exact_then_primary_then_default():
    profile = {"tone": ["warm", "friendly"],
               "tones": {"fr": ["chaleureux"], "es-419": ["cálido"]}}
    assert tone_for_language(profile, "fr") == ["chaleureux"]
    assert tone_for_language(profile, "fr-ca") == ["chaleureux"]  # primary fallback
    assert tone_for_language(profile, "es-419") == ["cálido"]  # exact wins
    assert tone_for_language(profile, "de") == ["warm", "friendly"]  # default
    assert tone_for_language(profile, "es") == ["warm", "friendly"]


def test_tone_for_language_without_a_tones_map_uses_the_default():
    assert tone_for_language({"tone": ["professional"]}, "fr") == ["professional"]
    assert tone_for_language(None, "fr") == []
    assert tone_for_language({}, "fr") == []


def test_draft_posts_uses_the_per_language_tone_when_present(store):
    acct = _account(store)
    site = store.create_site(acct["id"], "https://shop.example.com", "ca", "fr")
    profile = {"tone": ["warm", "friendly"], "tones": {"fr": ["chaleureux"]}}
    llm = _CapturingLLM([{"text": "Bonjour", "link": site["url"]}])
    _draft(site, llm, brand_profile=profile)
    assert "Match this brand tone: chaleureux." in llm.prompt
    assert "warm" not in llm.prompt  # the French tone replaces the default


def test_draft_posts_uses_the_default_tone_when_none_is_provided_for_the_language(store):
    acct = _account(store)
    site = store.create_site(acct["id"], "https://shop.example.com", "ca", "de")
    # No German tone supplied: the default tone is used, never a
    # machine-translated one presented as the brand's.
    profile = {"tone": ["warm", "friendly"], "tones": {"fr": ["chaleureux"]}}
    llm = _CapturingLLM([{"text": "Hallo", "link": site["url"]}])
    _draft(site, llm, brand_profile=profile)
    assert "Match this brand tone: warm, friendly." in llm.prompt
    assert "chaleureux" not in llm.prompt


def test_draft_posts_without_a_profile_has_no_tone_line(store):
    acct = _account(store)
    site = store.create_site(acct["id"], "https://shop.example.com", "ca", "fr")
    llm = _CapturingLLM([{"text": "Bonjour", "link": site["url"]}])
    _draft(site, llm)
    assert "brand tone" not in llm.prompt


# -- SEO article: the other generation entry point -----------------------------

def _fake_gateway(pkg, captured):
    gw_cls = MagicMock()
    gw_cls.enabled.return_value = True
    gw_cls.SMART = "smart"
    inst = MagicMock()

    def complete(prompt, **kw):
        captured.append(prompt)
        return {"text": "{}", "json": pkg, "model": "fake",
                "input_tokens": 900, "output_tokens": 2400, "cost_usd": 0.05}

    inst.complete.side_effect = complete
    gw_cls.return_value = inst
    return gw_cls


def _seo_task(language=None):
    from orchestrator.models import Task

    inv = _inventory()
    pkg = _good_pkg()
    inputs = {"action": "seo_article",
              "keyword": "proof of delivery",
              "niche": "proof of delivery",
              "site_url": "https://example.com",
              "facts": ["Example is a delivery tool"],
              "link_candidates": [{"url": u, "title": ""}
                                  for u in pkg["internal_links"]],
              "inventory_urls": sorted(inv.urls),
              "word_target": 1200}
    if language is not None:
        inputs["language"] = language
    return Task(agent_type="creative", business_id="biz_1", inputs=inputs,
                budget_usd=10.0, budget_tokens=50000), pkg


def test_seo_article_prompt_carries_the_language():
    from agents.creative.agent import CreativeAgent

    task, pkg = _seo_task(language="es")
    captured = []
    with patch("core.llm.LLMGateway", _fake_gateway(pkg, captured)):
        CreativeAgent()(task)
    assert captured and "Write in this language: es." in captured[0]


def test_seo_article_defaults_to_english_without_a_language():
    from agents.creative.agent import CreativeAgent

    task, pkg = _seo_task()
    captured = []
    with patch("core.llm.LLMGateway", _fake_gateway(pkg, captured)):
        CreativeAgent()(task)
    assert captured and "Write in this language: en." in captured[0]


# -- UI helpers -----------------------------------------------------------------

def test_language_options_marks_the_selected_language():
    html = language_options("fr")
    assert "<option value='fr' selected>French (fr)</option>" in html
    assert "<option value='en'>English (en)</option>" in html
    assert "99" not in html  # no language count is claimed


def test_language_datalist_offers_common_choices_with_free_entry():
    html = language_datalist("zh-hans")
    assert "<datalist id='language-choices'>" in html
    assert "<option value='zh-hans'>Chinese (Simplified) (zh-hans)</option>" in html
    assert "value='zh-hans'" in html
    assert "type='text'" in html  # free entry: any well-formed tag


# -- routes: change the language on the site page ------------------------------

def test_site_language_route_updates_the_site(client, store):
    acct = _login(client, store)
    site = store.create_site(acct["id"], "https://shop.example.com")
    r = client.post(f"/websites/sites/{site['id']}/language",
                    data={"language": "fr-ca"}, follow_redirects=False)
    assert r.status_code == 303
    assert store.get_site(site["id"])["language"] == "fr-ca"


def test_site_language_route_rejects_garbage(client, store):
    acct = _login(client, store)
    site = store.create_site(acct["id"], "https://shop.example.com")
    r = client.post(f"/websites/sites/{site['id']}/language",
                    data={"language": "english"})
    assert r.status_code == 400
    assert store.get_site(site["id"])["language"] == "en"


def test_site_detail_shows_the_content_language_card(client, store):
    acct = _login(client, store)
    site = store.create_site(acct["id"], "https://shop.example.com", "ca", "fr")
    html = client.get(f"/websites/sites/{site['id']}").text
    assert "Content language" in html
    assert "generates in <strong>fr</strong>" in html
    assert f"action='/websites/sites/{site['id']}/language'" in html


def test_add_site_form_offers_language_choices_with_free_entry(client, store):
    _login(client, store)
    html = client.get("/websites/sites/new").text
    assert "language-choices" in html
    assert "fr-ca" in html

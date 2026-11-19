"""Bulk content generation (Gap 2): allowance caps, wallet metering
through the existing path, draft-only guarantee, Brand DNA
pass-through, per-site isolation.

Fakes only — no live LLM, no network, no real charges.
"""

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ventoryn import bulk_content, campaigns
from ventoryn.bulk_content import (
    BulkError,
    BulkNotAvailable,
    BulkOverAllowance,
    bulk_run_store_for,
    run_bulk,
)
from ventoryn.usage_metering import record_llm_usage
from test_saas_shell import _account, store  # noqa: F401  (fixture + helper)

PROFILE = {
    "url": "https://shop.example.com",
    "host": "shop.example.com",
    "name": "Shop Example",
    "tagline": "Friendly gear for busy parents",
    "language": "en",
    "tone": ["warm", "playful"],
    "colours": ["#123456"],
    "products": [{"name": "Trail Backpack", "url": "https://shop.example.com/p/backpack"}],
    "audience": ["parents"],
    "buying_triggers": ["free shipping"],
    "reachable": True,
    "error": None,
    "collected_at": "2026-11-19T10:00:00+00:00",
}


def _tier_account(store, plan, wallet_cents=10000):
    account = _account(store)
    store.set_plan(account["id"], plan)
    if wallet_cents:
        store.grant_wallet_credit(account["id"], wallet_cents, "test top-up")
    return store.get_account(account["id"])


def _site(store, account, url="https://shop.example.com"):
    return store.create_site(account["id"], url)


def _fake_generate(site, kind, count, *, llm=None, inventory=None,
                   brand_profile=None):
    return [{"kind": kind,
             "text": f"{kind} draft {i} for {site['host']}",
             "link": site["url"] if kind != "email" else ""}
            for i in range(count)]


class _FakeInventory:
    def __init__(self, site):
        self.pages = [SimpleNamespace(url=site["url"], title="Shop home"),
                      SimpleNamespace(url=site["url"] + "/about",
                                      title="About us")]


class FakeLLM:
    """A gateway fake that answers every generator prompt and, like
    the real core.llm hook, records each call as usage."""

    def __init__(self, cost_usd=0.02):
        self.prompts = []
        self.cost_usd = cost_usd

    def complete(self, prompt, tier=None, system=None, json_mode=None,
                 timeout=None):
        self.prompts.append(prompt)
        record_llm_usage({"model": "test/fake", "input_tokens": 100,
                          "output_tokens": 50, "cost_usd": self.cost_usd})
        m = re.search(r"Write (\d+) DIFFERENT", prompt)
        n = int(m.group(1)) if m else 1
        if "short ads" in prompt:
            return {"json": {"ads": [
                {"headline": f"Ad headline {i}", "body": f"Ad body {i}.",
                 "link": "https://shop.example.com/about"}
                for i in range(n)]}}
        if "marketing emails" in prompt:
            return {"json": {"emails": [
                {"subject": f"Email subject {i}", "body": f"Email body {i}."}
                for i in range(n)]}}
        return {"json": {"posts": [
            {"text": f"Social post {i} about the shop.",
             "link": "https://shop.example.com/about"}
            for i in range(n)]}}


def _pending_drafts(store, campaign_id):
    return [d for d in store.campaign_drafts(campaign_id)
            if d["status"] == "pending"]


# -- allowance ---------------------------------------------------------------

def test_under_cap_run_succeeds_and_saves_pending_drafts(store):
    account = _tier_account(store, "starter")
    site = _site(store, account)
    run = run_bulk(store, site, account, {"social": 2, "ad": 1, "email": 1},
                   generate=_fake_generate)
    assert run["status"] == "done"
    assert run["assets"] == {"social": 2, "ad": 1, "email": 1}
    assert run["total_assets"] == 4
    assert len(run["campaign_ids"]) == 3
    for cid in run["campaign_ids"]:
        campaign = store.get_campaign(cid)
        assert campaign["status"] == "proposed"
        assert campaign["source"] == "bulk"
        assert campaign["site_id"] == site["id"]
        drafts = store.campaign_drafts(cid)
        assert drafts and all(d["status"] == "pending" for d in drafts)
    kinds = {d["kind"] for cid in run["campaign_ids"]
             for d in store.campaign_drafts(cid)}
    assert kinds == {"x", "ad", "email"}
    stored = bulk_run_store_for(store).get(run["id"])
    assert stored["status"] == "done" and stored["assets"] == 4


def test_over_per_run_cap_rejected_before_any_wallet_charge(store):
    account = _tier_account(store, "starter")
    site = _site(store, account)
    before = store.wallet_balance_cents(account["id"])
    with pytest.raises(BulkOverAllowance):
        run_bulk(store, site, account, {"social": 26},
                 generate=_fake_generate)
    assert store.wallet_balance_cents(account["id"]) == before
    assert store.wallet_reserved_cents(account["id"]) == 0
    assert store.wallet_transactions(account["id"]) == [
        t for t in store.wallet_transactions(account["id"])
        if t["delta_cents"] > 0]  # only the test top-up, no debit
    assert bulk_run_store_for(store).list_for_site(site["id"]) == []
    assert store.list_campaigns(account["id"]) == []


def test_over_monthly_cap_rejected_after_prior_runs(store):
    account = _tier_account(store, "starter")
    site = _site(store, account)
    for _ in range(5):  # 5 x 25 = 125, the Starter monthly cap
        run_bulk(store, site, account, {"social": 25},
                 generate=_fake_generate)
    assert bulk_run_store_for(store).monthly_assets(account["id"]) == 125
    with pytest.raises(BulkOverAllowance):
        run_bulk(store, site, account, {"social": 1},
                 generate=_fake_generate)
    assert bulk_run_store_for(store).monthly_assets(account["id"]) == 125


def test_per_plan_caps_differ(store):
    starter = _tier_account(store, "starter")
    site = _site(store, starter)
    with pytest.raises(BulkOverAllowance):
        run_bulk(store, site, starter, {"social": 26},
                 generate=_fake_generate)
    # Same store, new account on Pro: 26 is fine, 100 is the cap.
    pro = _account(store, sub="google-sub-2", email="pro@example.com")
    store.set_plan(pro["id"], "pro")
    store.grant_wallet_credit(pro["id"], 100000, "test top-up")
    pro = store.get_account(pro["id"])
    site2 = _site(store, pro, "https://pro.example.com")
    run = run_bulk(store, site2, pro, {"social": 100},
                   generate=_fake_generate)
    assert run["total_assets"] == 100
    with pytest.raises(BulkOverAllowance):
        run_bulk(store, site2, pro, {"social": 101},
                 generate=_fake_generate)


def test_legacy_account_cannot_run_bulk(store):
    account = _account(store)  # plan == "" (legacy)
    site = _site(store, account)
    with pytest.raises(BulkNotAvailable):
        run_bulk(store, site, account, {"social": 1},
                 generate=_fake_generate)


def test_invalid_mix_rejected_before_any_charge(store):
    account = _tier_account(store, "starter")
    site = _site(store, account)
    before = store.wallet_balance_cents(account["id"])
    for bad in ({}, {"social": 0}, {"social": -1}, {"social": True},
                {"blog": 3}, {"social": "5"}, "social"):
        with pytest.raises((ValueError, BulkError)):
            run_bulk(store, site, account, bad, generate=_fake_generate)
    assert store.wallet_balance_cents(account["id"]) == before
    assert store.list_campaigns(account["id"]) == []


# -- wallet, through the existing metering path ------------------------------

def test_wallet_charged_exactly_once_via_existing_metering(store):
    account = _tier_account(store, "starter")
    site = _site(store, account)
    llm = FakeLLM(cost_usd=0.02)
    before = store.wallet_balance_cents(account["id"])
    run = run_bulk(store, site, account, {"social": 2, "ad": 1, "email": 1},
                   llm=llm, inventory=_FakeInventory(site))
    # 3 LLM calls x 2c provider cost = 6c; priced at 1.5x, rounded up
    # per event: 3c each -> 9c total.
    assert run["wallet_charged_cents"] == 9
    assert store.wallet_balance_cents(account["id"]) == before - 9
    assert store.wallet_reserved_cents(account["id"]) == 0
    debits = [t for t in store.wallet_transactions(account["id"])
              if t["delta_cents"] < 0]
    assert len(debits) == 1 and debits[0]["delta_cents"] == -9
    events = store.usage_events_for_job(run["job_id"])
    assert len(events) == 3  # one per generator call
    assert all(e["customer_charge_cents"] == 3 for e in events)
    assert all(e["provider_cost_cents"] == 2 for e in events)
    # Re-settling the same job is a no-op: no second charge.
    from ventoryn import pricing
    job = store.get_job(run["job_id"])
    again = pricing.price_and_settle_job(store, run["job_id"],
                                         job["wallet_reservation_id"])
    assert again == 9
    assert store.wallet_balance_cents(account["id"]) == before - 9
    assert len([t for t in store.wallet_transactions(account["id"])
                if t["delta_cents"] < 0]) == 1


def test_failed_run_settles_only_what_actually_ran(store):
    account = _tier_account(store, "starter")
    site = _site(store, account)
    llm = FakeLLM(cost_usd=0.02)

    def boom(site, kind, count, **kw):
        if kind == "ad":
            raise RuntimeError("ad generator exploded")
        return _fake_generate(site, kind, count, **kw)

    before = store.wallet_balance_cents(account["id"])
    with pytest.raises(RuntimeError):
        run_bulk(store, site, account, {"social": 2, "ad": 1},
                 llm=llm, inventory=_FakeInventory(site), generate=boom)
    # Social ran through the fake generator (no LLM call), the ad
    # generator failed before any call: no usage, reservation released.
    assert store.wallet_balance_cents(account["id"]) == before
    assert store.wallet_reserved_cents(account["id"]) == 0
    assert store.list_campaigns(account["id"]) == []  # nothing saved
    runs = bulk_run_store_for(store).list_for_site(site["id"])
    assert len(runs) == 1 and runs[0]["status"] == "failed"


# -- draft-only guarantee ------------------------------------------------------

def test_nothing_posts_without_approval(store):
    account = _tier_account(store, "starter")
    site = _site(store, account)
    run = run_bulk(store, site, account, {"social": 2, "ad": 1, "email": 1},
                   generate=_fake_generate)
    posted = []

    def recorder(store, config, account, site_id, draft_id):
        posted.append(draft_id)
        return True, "posted"

    assert campaigns.post_due(store, None, poster=recorder) == []
    assert posted == []
    for cid in run["campaign_ids"]:
        assert _pending_drafts(store, cid)
        assert not [d for d in store.campaign_drafts(cid)
                    if d["status"] in ("scheduled", "posted")]


def test_after_approval_x_posts_but_ads_and_emails_are_held(store):
    account = _tier_account(store, "starter")
    site = _site(store, account)
    run = run_bulk(store, site, account, {"social": 1, "ad": 1, "email": 1},
                   generate=_fake_generate)
    for cid in run["campaign_ids"]:
        campaigns.schedule(store, store.get_campaign(cid))
    posted = []

    def recorder(store, config, account, site_id, draft_id):
        posted.append(draft_id)
        return True, "posted"

    done = campaigns.post_due(store, None, poster=recorder)
    assert len(posted) == 1  # only the X draft reaches the poster
    x_draft = [d for cid in run["campaign_ids"]
               for d in store.campaign_drafts(cid) if d["kind"] == "x"][0]
    assert posted == [x_draft["id"]]
    held = [(did, ok, msg) for did, ok, msg in done if not ok]
    assert len(held) == 2  # ad + email held, never handed to the poster
    for cid in run["campaign_ids"]:
        for d in store.campaign_drafts(cid):
            if d["kind"] != "x":
                assert store.get_social_draft(d["id"])["status"] == "held"


def test_reject_removes_the_whole_bulk_campaign(store):
    account = _tier_account(store, "starter")
    site = _site(store, account)
    run = run_bulk(store, site, account, {"social": 2}, generate=_fake_generate)
    cid = run["campaign_ids"][0]
    campaigns.reject(store, store.get_campaign(cid))
    assert store.get_campaign(cid)["status"] == "rejected"
    assert _pending_drafts(store, cid) == []


# -- Brand DNA pass-through ---------------------------------------------------

def test_brand_profile_passed_through_to_every_generator(store):
    account = _tier_account(store, "starter")
    site = _site(store, account)
    seen = {}

    def spy(site, kind, count, *, llm=None, inventory=None,
            brand_profile=None):
        seen[kind] = brand_profile
        return _fake_generate(site, kind, count)

    run_bulk(store, site, account, {"social": 1, "ad": 1, "email": 1},
             generate=spy, brand_profile=PROFILE)
    assert seen == {"social": PROFILE, "ad": PROFILE, "email": PROFILE}


def test_brand_profile_shapes_the_real_social_prompt(store):
    account = _tier_account(store, "starter")
    site = _site(store, account)
    llm = FakeLLM()
    run_bulk(store, site, account, {"social": 1}, llm=llm,
             inventory=_FakeInventory(site), brand_profile=PROFILE)
    prompt = llm.prompts[0]
    assert "Tone of voice: warm, playful." in prompt
    assert "Products: Trail Backpack." in prompt
    assert "Who it is for: parents." in prompt
    assert "What makes buyers act: free shipping." in prompt
    # And without a profile the prompt is exactly as before.
    llm2 = FakeLLM()
    run_bulk(store, site, account, {"social": 1}, llm=llm2,
             inventory=_FakeInventory(site))
    assert "Brand voice" not in llm2.prompts[0]


def test_brand_profile_shapes_ad_and_email_prompts(store):
    account = _tier_account(store, "starter")
    site = _site(store, account)
    llm = FakeLLM()
    run_bulk(store, site, account, {"ad": 1, "email": 1}, llm=llm,
             inventory=_FakeInventory(site), brand_profile=PROFILE)
    assert all("Tone of voice: warm, playful." in p for p in llm.prompts)
    assert all("Products: Trail Backpack." in p for p in llm.prompts)


# -- per-site isolation -------------------------------------------------------

def test_bulk_runs_are_isolated_per_site_and_account(store):
    account = _tier_account(store, "starter")
    site_a = _site(store, account, "https://a.example.com")
    site_b = _site(store, account, "https://b.example.com")
    run_bulk(store, site_a, account, {"social": 3}, generate=_fake_generate)
    assert len(bulk_run_store_for(store).list_for_site(site_a["id"])) == 1
    assert bulk_run_store_for(store).list_for_site(site_b["id"]) == []
    assert store.open_campaign_for_site(site_b["id"]) is None
    assert store.open_campaign_for_site(site_a["id"]) is not None
    # Monthly usage counts per account, not per site — and a second
    # account's runs do not count toward the first's cap.
    assert bulk_run_store_for(store).monthly_assets(account["id"]) == 3
    other = _account(store, sub="google-sub-2", email="other@example.com")
    assert bulk_run_store_for(store).monthly_assets(other["id"]) == 0

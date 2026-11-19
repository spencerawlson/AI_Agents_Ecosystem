"""Gap 6 — the publishing queue: honest per-channel states, derived
from the SaasStore + SaasConfig, and a queue that holds approved
drafts per channel and refuses to publish on a blocked or
not-configured channel.

No live provider calls anywhere: the queue never calls a provider
itself, and publish attempts in tests use fakes only.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from core.audit import AuditLog
from core.ledger import Ledger
from core.registry import BusinessRegistry
from orchestrator.approvals import ApprovalGate
from ventoryn.app import create_app
from ventoryn import campaigns as _campaigns
from ventoryn.publishing_queue import (
    BLOCKED,
    NOT_CONFIGURED,
    READY,
    PublishingQueue,
    channel_state,
    channel_states_for_site,
    check_publish_allowed,
)
import ventoryn.saas_routes as saas_routes

from _signin import (
    fresh_totp_code,
    operator_store_and_config,
    sign_in,
)

CUSTOMER_A = ("ada@example.com", "google-sub-ada", "Ada")

# TikTok's operator-app state comes from the environment; tests pass
# an explicit env mapping instead of touching os.environ.
TIKTOK_ENV = {
    "TIKTOK_CLIENT_KEY": "test-key",
    "TIKTOK_CLIENT_SECRET": "test-secret",
    "TIKTOK_AUDITED": "1",
}
TIKTOK_ENV_UNAUDITED = {
    "TIKTOK_CLIENT_KEY": "test-key",
    "TIKTOK_CLIENT_SECRET": "test-secret",
}


def _store_config(env=None):
    store, config = operator_store_and_config()
    return store, config


def _site(store, account_id, url="https://ada.example.com"):
    return store.create_site(account_id, url)


def _draft(store, site_id, kind, text="Hello from Axionory",
           link_url="", campaign_id=""):
    return store.create_social_draft(site_id, kind, text, link_url,
                                     campaign_id)


def _connect(store, site_id, kind, note="Connected"):
    """Mark a connection connected, as the OAuth callback leaves it."""
    store.ensure_connections(site_id)
    detail = {"access_token": "token-1", "provider_user_id": "user-1"}
    if kind == "facebook":
        detail.update({"page_id": "page-1", "page_name": "Ada Shop",
                       "ig_user_id": "ig-1"})
    if kind == "instagram":
        detail.update({"ig_user_id": "ig-1", "page_id": "page-1"})
    store.update_connection(site_id, kind, "connected", note, detail)


def _queue(store, config, env=None):
    return PublishingQueue(store, config, env=env)


# ---------------------------------------------------------------------------
# State derivation — Meta (Facebook / Instagram)
# ---------------------------------------------------------------------------

def test_facebook_not_configured_when_app_unconfigured():
    store, config = _store_config()
    site = _site(store, "acct-1")
    state = channel_state(store, config, site["id"], "facebook")
    assert state["state"] == NOT_CONFIGURED
    assert "Meta app" in state["reason"]
    assert "VENTORYN_FACEBOOK_APP_ID" in state["unblock_step"]
    assert state["ever_posted"] is False


def test_facebook_blocked_on_business_verification_when_app_configured():
    store, config = _store_config()
    config.facebook_app_id = "fb-id"
    config.facebook_app_secret = "fb-secret"
    site = _site(store, "acct-1")
    state = channel_state(store, config, site["id"], "facebook")
    assert state["state"] == BLOCKED
    assert state["reason"] == "Meta business verification step 3 pending."
    assert "business verification" in state["unblock_step"]
    # The block holds even once the site connects its Page — the
    # gate is the verification, not the connection.
    _connect(store, site["id"], "facebook")
    state = channel_state(store, config, site["id"], "facebook")
    assert state["state"] == BLOCKED


def test_instagram_blocked_on_business_verification_too():
    store, config = _store_config()
    config.facebook_app_id = "fb-id"
    config.facebook_app_secret = "fb-secret"
    site = _site(store, "acct-1")
    state = channel_state(store, config, site["id"], "instagram")
    assert state["state"] == BLOCKED
    assert "business verification" in state["reason"].lower()


def test_facebook_ready_when_verified_and_connected():
    store, config = _store_config()
    config.facebook_app_id = "fb-id"
    config.facebook_app_secret = "fb-secret"
    config.meta_business_verified = True
    site = _site(store, "acct-1")
    # Verified but not connected yet: still not_configured.
    state = channel_state(store, config, site["id"], "facebook")
    assert state["state"] == NOT_CONFIGURED
    assert "Connect your Facebook Page" in state["unblock_step"]
    _connect(store, site["id"], "facebook")
    state = channel_state(store, config, site["id"], "facebook")
    assert state["state"] == READY
    assert state["ever_posted"] is False  # connected != working


def test_facebook_state_flips_when_verification_clears_and_reverts():
    store, config = _store_config()
    config.facebook_app_id = "fb-id"
    config.facebook_app_secret = "fb-secret"
    site = _site(store, "acct-1")
    _connect(store, site["id"], "facebook")
    assert channel_state(store, config, site["id"],
                         "facebook")["state"] == BLOCKED
    config.meta_business_verified = True
    assert channel_state(store, config, site["id"],
                         "facebook")["state"] == READY
    config.meta_business_verified = False
    assert channel_state(store, config, site["id"],
                         "facebook")["state"] == BLOCKED


def test_facebook_state_flips_when_connection_drops():
    store, config = _store_config()
    config.facebook_app_id = "fb-id"
    config.facebook_app_secret = "fb-secret"
    config.meta_business_verified = True
    site = _site(store, "acct-1")
    _connect(store, site["id"], "facebook")
    assert channel_state(store, config, site["id"],
                         "facebook")["state"] == READY
    store.disconnect_connection(site["id"], "facebook")
    assert channel_state(store, config, site["id"],
                         "facebook")["state"] == NOT_CONFIGURED


# ---------------------------------------------------------------------------
# State derivation — X, TikTok, Reddit, LinkedIn, YouTube, Pinterest
# ---------------------------------------------------------------------------

def test_x_not_configured_when_app_unregistered():
    store, config = _store_config()
    site = _site(store, "acct-1")
    state = channel_state(store, config, site["id"], "x")
    assert state["state"] == NOT_CONFIGURED
    assert "X app" in state["reason"]


def test_x_blocked_on_zero_credits_when_app_registered():
    store, config = _store_config()
    config.x_app_registered = True
    config.x_client_id = "x-id"
    config.x_client_secret = "x-secret"
    site = _site(store, "acct-1")
    state = channel_state(store, config, site["id"], "x")
    assert state["state"] == BLOCKED
    assert "402" in state["reason"]
    assert "console.x.com" in state["unblock_step"]
    _connect(store, site["id"], "x")
    assert channel_state(store, config, site["id"],
                         "x")["state"] == BLOCKED


def test_x_ready_when_credits_confirmed_and_connected():
    store, config = _store_config()
    config.x_app_registered = True
    config.x_client_id = "x-id"
    config.x_client_secret = "x-secret"
    config.x_credits_available = True
    site = _site(store, "acct-1")
    assert channel_state(store, config, site["id"],
                         "x")["state"] == NOT_CONFIGURED
    _connect(store, site["id"], "x")
    assert channel_state(store, config, site["id"],
                         "x")["state"] == READY


def test_tiktok_not_configured_without_client_credentials():
    store, config = _store_config()
    site = _site(store, "acct-1")
    state = channel_state(store, config, site["id"], "tiktok", env={})
    assert state["state"] == NOT_CONFIGURED
    assert "TIKTOK_CLIENT_KEY" in state["unblock_step"]


def test_tiktok_blocked_in_review_until_audit_passes():
    store, config = _store_config()
    site = _site(store, "acct-1")
    state = channel_state(store, config, site["id"], "tiktok",
                          env=TIKTOK_ENV_UNAUDITED)
    assert state["state"] == BLOCKED
    assert "Nov 12, 2026" in state["reason"]
    assert "audit" in state["reason"].lower()
    assert "SELF_ONLY" in state["unblock_step"]


def test_tiktok_ready_when_audited_and_connected():
    store, config = _store_config()
    site = _site(store, "acct-1")
    state = channel_state(store, config, site["id"], "tiktok",
                          env=TIKTOK_ENV)
    assert state["state"] == NOT_CONFIGURED  # not connected yet
    assert "disconnected" in state["reason"]
    _connect(store, site["id"], "tiktok")
    state = channel_state(store, config, site["id"], "tiktok",
                          env=TIKTOK_ENV)
    assert state["state"] == READY


def test_reddit_not_configured_without_app():
    store, config = _store_config()
    site = _site(store, "acct-1")
    state = channel_state(store, config, site["id"], "reddit")
    assert state["state"] == NOT_CONFIGURED
    assert "No Reddit account" in state["reason"]
    assert "Ads Manager" in state["unblock_step"]


def test_reddit_blocked_when_connected_but_no_publish_path():
    store, config = _store_config()
    config.reddit_app_registered = True
    site = _site(store, "acct-1")
    _connect(store, site["id"], "reddit")
    state = channel_state(store, config, site["id"], "reddit")
    assert state["state"] == BLOCKED
    assert "no Reddit publish path" in state["reason"]


def test_linkedin_youtube_pinterest_not_configured_with_exact_step():
    store, config = _store_config()
    site = _site(store, "acct-1")
    linkedin = channel_state(store, config, site["id"], "linkedin")
    assert linkedin["state"] == NOT_CONFIGURED
    assert "no LinkedIn developer app" in linkedin["reason"]
    assert "Standard Tier" in linkedin["unblock_step"]
    youtube = channel_state(store, config, site["id"], "youtube")
    assert youtube["state"] == NOT_CONFIGURED
    assert "no Google Cloud project" in youtube["reason"]
    assert "API audit" in youtube["unblock_step"]
    pinterest = channel_state(store, config, site["id"], "pinterest")
    assert pinterest["state"] == NOT_CONFIGURED
    assert "No Pinterest Business account" in pinterest["reason"]
    assert "Standard access" in pinterest["unblock_step"]


def test_all_channels_listed_with_states():
    store, config = _store_config()
    site = _site(store, "acct-1")
    states = channel_states_for_site(store, config, site["id"], env={})
    assert [s["channel"] for s in states] == [
        "facebook", "instagram", "x", "tiktok", "reddit", "linkedin",
        "youtube", "pinterest"]
    assert all(s["state"] in (READY, BLOCKED, NOT_CONFIGURED)
               for s in states)
    # Nothing is ready on a fresh site with nothing configured.
    assert all(s["state"] != READY for s in states)


def test_ever_posted_turns_true_only_after_a_real_post():
    store, config = _store_config()
    config.facebook_app_id = "fb-id"
    config.facebook_app_secret = "fb-secret"
    config.meta_business_verified = True
    site = _site(store, "acct-1")
    _connect(store, site["id"], "facebook")
    draft = _draft(store, site["id"], "facebook")
    assert channel_state(store, config, site["id"],
                         "facebook")["ever_posted"] is False
    store.decide_social_draft(draft["id"], "posted", tweet_id="fb-1")
    assert channel_state(store, config, site["id"],
                         "facebook")["ever_posted"] is True


# ---------------------------------------------------------------------------
# Queue intake
# ---------------------------------------------------------------------------

def test_queue_accepts_approved_draft_and_holds_it_per_channel():
    store, config = _store_config()
    config.facebook_app_id = "fb-id"
    config.facebook_app_secret = "fb-secret"
    site = _site(store, "acct-1")
    queue = _queue(store, config)
    draft = _draft(store, site["id"], "facebook")
    item = queue.enqueue_approved_draft(site["id"], draft["id"])
    assert item is not None
    assert item["channel"] == "facebook"
    assert "business verification" in item["reason"]
    queued = queue.list_queued(site["id"])
    assert len(queued) == 1
    assert queued[0]["draft"]["id"] == draft["id"]
    assert queued[0]["channel_state"]["state"] == BLOCKED


def test_queue_dedupes_and_rejects_decided_or_unknown_drafts():
    store, config = _store_config()
    site = _site(store, "acct-1")
    queue = _queue(store, config)
    draft = _draft(store, site["id"], "x")
    first = queue.enqueue_approved_draft(site["id"], draft["id"])
    second = queue.enqueue_approved_draft(site["id"], draft["id"])
    assert first["id"] == second["id"]
    assert len(queue.list_queued(site["id"])) == 1
    assert queue.enqueue_approved_draft(site["id"], "nope") is None
    posted = _draft(store, site["id"], "x", text="already out")
    store.decide_social_draft(posted["id"], "posted", tweet_id="x-1")
    assert queue.enqueue_approved_draft(site["id"], posted["id"]) is None
    rejected = _draft(store, site["id"], "x", text="no")
    store.decide_social_draft(rejected["id"], "rejected")
    assert queue.enqueue_approved_draft(site["id"],
                                        rejected["id"]) is None


def test_queue_prunes_items_whose_draft_gets_decided():
    store, config = _store_config()
    site = _site(store, "acct-1")
    queue = _queue(store, config)
    draft = _draft(store, site["id"], "x")
    queue.enqueue_approved_draft(site["id"], draft["id"])
    assert len(queue.list_queued(site["id"])) == 1
    store.decide_social_draft(draft["id"], "posted", tweet_id="x-1")
    assert queue.list_queued(site["id"]) == []


def test_queue_remove_cancels_item_but_keeps_draft_pending():
    store, config = _store_config()
    site = _site(store, "acct-1")
    queue = _queue(store, config)
    draft = _draft(store, site["id"], "x")
    item = queue.enqueue_approved_draft(site["id"], draft["id"])
    assert queue.remove(item["id"]) is True
    assert queue.list_queued(site["id"]) == []
    assert store.get_social_draft(draft["id"])["status"] == "pending"


def test_campaign_drafts_enqueue_after_approval():
    store, config = _store_config()
    site = _site(store, "acct-1")
    campaign = store.create_campaign(site["id"], "acct-1", days=7,
                                     posts_per_day=1.0, with_link=False,
                                     platform="x")
    d1 = _draft(store, site["id"], "x", text="one",
                campaign_id=campaign["id"])
    d2 = _draft(store, site["id"], "x", text="two",
                campaign_id=campaign["id"])
    _campaigns.schedule(store, campaign)
    assert store.get_campaign(campaign["id"])["status"] == "scheduled"
    queue = _queue(store, config)
    items = [queue.enqueue_approved_draft(site["id"], d["id"])
             for d in (d1, d2)]
    assert all(i is not None for i in items)
    assert {i["channel"] for i in queue.list_queued(site["id"])} == {"x"}


# ---------------------------------------------------------------------------
# Publish refusal + delegation
# ---------------------------------------------------------------------------

def test_blocked_channel_refuses_publish_and_never_calls_publish_fn():
    store, config = _store_config()
    config.facebook_app_id = "fb-id"
    config.facebook_app_secret = "fb-secret"
    config.social_publishing_enabled = True
    site = _site(store, "acct-1")
    queue = _queue(store, config)
    draft = _draft(store, site["id"], "facebook")
    item = queue.enqueue_approved_draft(site["id"], draft["id"])
    calls = []

    def _fake_publish():
        calls.append(1)
        return True, "Posted to Facebook (id fb-1)."

    posted, message = queue.request_publish(item["id"], _fake_publish)
    assert posted is False
    assert "business verification" in message
    assert "Nothing was posted" in message
    assert calls == []
    # No silent drop: the item is still queued, the draft still pending.
    assert len(queue.list_queued(site["id"])) == 1
    assert store.get_social_draft(draft["id"])["status"] == "pending"


def test_not_configured_channel_refuses_publish():
    store, config = _store_config()
    config.social_publishing_enabled = True
    site = _site(store, "acct-1")
    queue = _queue(store, config)
    # LinkedIn has no draft kind in the store; use X unconfigured.
    draft = _draft(store, site["id"], "x")
    item = queue.enqueue_approved_draft(site["id"], draft["id"])
    calls = []

    def _fake_publish():
        calls.append(1)
        return True, "posted"

    posted, message = queue.request_publish(item["id"], _fake_publish)
    assert posted is False
    assert "X app is not registered" in message
    assert calls == []


def test_publish_flag_off_refuses_even_a_ready_channel():
    store, config = _store_config()
    config.facebook_app_id = "fb-id"
    config.facebook_app_secret = "fb-secret"
    config.meta_business_verified = True
    # social_publishing_enabled stays False, like production.
    site = _site(store, "acct-1")
    _connect(store, site["id"], "facebook")
    queue = _queue(store, config)
    draft = _draft(store, site["id"], "facebook")
    item = queue.enqueue_approved_draft(site["id"], draft["id"])
    calls = []

    def _fake_publish():
        calls.append(1)
        return True, "posted"

    posted, message = queue.request_publish(item["id"], _fake_publish)
    assert posted is False
    assert "Social publishing is off" in message
    assert calls == []


def test_ready_channel_delegates_to_publish_fn_and_prunes():
    store, config = _store_config()
    config.facebook_app_id = "fb-id"
    config.facebook_app_secret = "fb-secret"
    config.meta_business_verified = True
    config.social_publishing_enabled = True
    site = _site(store, "acct-1")
    _connect(store, site["id"], "facebook")
    queue = _queue(store, config)
    draft = _draft(store, site["id"], "facebook")
    item = queue.enqueue_approved_draft(site["id"], draft["id"])
    calls = []

    def _fake_publish():
        calls.append(1)
        return True, "Posted to Facebook (id fb-1)."

    posted, message = queue.request_publish(item["id"], _fake_publish)
    assert posted is True
    assert "fb-1" in message
    assert calls == [1]
    assert queue.list_queued(site["id"]) == []


def test_ready_channel_publish_failure_keeps_item_queued():
    store, config = _store_config()
    config.facebook_app_id = "fb-id"
    config.facebook_app_secret = "fb-secret"
    config.meta_business_verified = True
    config.social_publishing_enabled = True
    site = _site(store, "acct-1")
    _connect(store, site["id"], "facebook")
    queue = _queue(store, config)
    draft = _draft(store, site["id"], "facebook")
    item = queue.enqueue_approved_draft(site["id"], draft["id"])
    posted, _message = queue.request_publish(
        item["id"], lambda: (False, "Facebook refused the post."))
    assert posted is False
    assert len(queue.list_queued(site["id"])) == 1


def test_request_publish_without_publish_fn_refuses():
    store, config = _store_config()
    config.facebook_app_id = "fb-id"
    config.facebook_app_secret = "fb-secret"
    config.meta_business_verified = True
    config.social_publishing_enabled = True
    site = _site(store, "acct-1")
    _connect(store, site["id"], "facebook")
    queue = _queue(store, config)
    draft = _draft(store, site["id"], "facebook")
    item = queue.enqueue_approved_draft(site["id"], draft["id"])
    posted, message = queue.request_publish(item["id"])
    assert posted is False
    assert "Nothing was posted" in message
    assert len(queue.list_queued(site["id"])) == 1


def test_check_publish_allowed_gate():
    store, config = _store_config()
    site = _site(store, "acct-1")
    allowed, _reason = check_publish_allowed(store, config, site["id"],
                                             "facebook")
    assert allowed is False  # publishing flag off, like production
    config.social_publishing_enabled = True
    allowed, reason = check_publish_allowed(store, config, site["id"],
                                            "facebook")
    assert allowed is False
    assert "Meta app" in reason
    config.facebook_app_id = "fb-id"
    config.facebook_app_secret = "fb-secret"
    config.meta_business_verified = True
    _connect(store, site["id"], "facebook")
    allowed, _reason = check_publish_allowed(store, config, site["id"],
                                             "facebook")
    assert allowed is True


# ---------------------------------------------------------------------------
# Routes: site-detail publishing section + approve-refusal enqueues
# ---------------------------------------------------------------------------

def _app_with(facebook=False, x=False, publishing=False):
    store, config = operator_store_and_config()
    if facebook:
        config.facebook_app_id = "test-fb-id"
        config.facebook_app_secret = "test-fb-secret"
    if x:
        config.x_app_registered = True
        config.x_client_id = "test-x-id"
        config.x_client_secret = "test-x-secret"
    if publishing:
        config.social_publishing_enabled = True
    app = create_app(registry=BusinessRegistry(),
                     ledger=Ledger(),
                     approvals=ApprovalGate(),
                     audit=AuditLog(),
                     saas_store=store, saas_config=config)
    return app, store, config


def _seed_site(store, account, url="https://ada.example.com"):
    return store.create_site(account["id"], url)


def test_site_detail_shows_publishing_status_for_every_channel(monkeypatch):
    # Configure the operator apps so the page shows the blocked
    # reasons (verification / 402 / in-review), not the
    # not-configured ones.
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "test-key")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "test-secret")
    app, store, _config = _app_with(facebook=True, x=True)
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub=CUSTOMER_A[1], name=CUSTOMER_A[2])
    site = _seed_site(store, account)
    r = client.get(f"/websites/sites/{site['id']}")
    assert r.status_code == 200
    for name in ("Facebook", "Instagram", "TikTok", "LinkedIn",
                 "YouTube", "Reddit", "Pinterest"):
        assert name in r.text, name
    # Honest wording: no channel is shown as working.
    assert "No successful post from Axionory yet" in r.text
    # The exact unblock steps are on the page.
    assert "Meta business verification step 3 pending" in r.text
    assert "console.x.com" in r.text
    assert "Nov 12, 2026" in r.text


def test_approve_refused_draft_is_held_in_the_queue():
    app, store, config = _app_with(facebook=True)
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub=CUSTOMER_A[1], name=CUSTOMER_A[2])
    site = _seed_site(store, account)
    r = client.post(f"/websites/sites/{site['id']}/social/facebook/drafts",
                    data={"text": "Hello from Axionory",
                          "link_url": "https://ada.example.com/welcome"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    draft = [d for d in store.list_social_drafts(site["id"])
             if d["kind"] == "facebook"][-1]
    # Publishing flag is off, so the approve attempt is refused —
    # and the human-approved draft is held in the queue, not lost.
    r = client.post(
        f"/websites/sites/{site['id']}/social/facebook/drafts/"
        f"{draft['id']}/approve",
        data={"totp_code": fresh_totp_code(store, account["id"])},
        follow_redirects=False)
    assert r.status_code == 400
    assert "Social publishing is off" in r.text
    assert store.get_social_draft(draft["id"])["status"] == "pending"
    queue = PublishingQueue(store, config)
    queued = queue.list_queued(site["id"])
    assert len(queued) == 1
    assert queued[0]["draft"]["id"] == draft["id"]
    # And the site page now shows the held draft under Publishing.
    r = client.get(f"/websites/sites/{site['id']}")
    assert r.status_code == 200
    assert "Waiting to publish" in r.text
    assert "Hello from Axionory" in r.text

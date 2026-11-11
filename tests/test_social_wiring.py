"""Wiring: Facebook/Instagram OAuth, drafts, approval-first
approve-and-post, Founding Customer dashboard, and cost caps —
through the real routes, adapted to main's draft system.

Main's X stays on its own inline implementation (tests/test_x_social.py
covers it) — this file covers only the ported Facebook/Instagram parts.
Main's draft system has no separate decide/publish steps: approving a
draft posts it in one step (approve-and-post), and rejecting it is
terminal. There is no state machine here — see the reconciliation
notes for why.

No live provider calls anywhere: OAuth exchanges and the publish
client are fakes patched in. The feature flag stays OFF unless a
test turns it on, exactly like production.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from core.audit import AuditLog
from core.ledger import Ledger
from core.registry import BusinessRegistry
from ventoryn.app import create_app
from ventoryn.saas import SaasConfig
from orchestrator.approvals import ApprovalGate
import ventoryn.saas_routes as saas_routes

from _signin import (
    fresh_totp_code,
    operator_store_and_config,
    sign_in,
)

CUSTOMER_A = ("ada@example.com", "google-sub-ada", "Ada")
CUSTOMER_B = ("ben@example.com", "google-sub-ben", "Ben")


@pytest.fixture(autouse=True)
def _token_key(monkeypatch):
    # Draft links are signed at creation (ventoryn/signature.py reads
    # VENTORYN_TOKEN_KEY from the environment directly) and OAuth
    # tokens are encrypted at rest — both need a key, as on the
    # server. A fixed test value; never a real key.
    monkeypatch.setenv("VENTORYN_TOKEN_KEY", "test-token-key-wiring")


def _app_with(x=False, facebook=False, publishing=False):
    """An app whose config has the social apps registered when asked.
    Nothing is registered by default — the honest not-connected
    state is the default under test, like production."""
    store, config = operator_store_and_config()
    if x:
        config.x_app_registered = True
        config.x_client_id = "test-x-id"
        config.x_client_secret = "test-x-secret"
    if facebook:
        config.facebook_app_id = "test-fb-id"
        config.facebook_app_secret = "test-fb-secret"
    if publishing:
        config.social_publishing_enabled = True
    app = create_app(registry=BusinessRegistry(),
                     ledger=Ledger(),
                     approvals=ApprovalGate(),
                     audit=AuditLog(),
                     saas_store=store, saas_config=config)
    return app, store, config


def _customer_client(app, store, who=CUSTOMER_A):
    email, sub, name = who
    client = TestClient(app)
    account = sign_in(client, store, email=email, sub=sub, name=name)
    return client, account


def _seed_site(store, account, url="https://ada.example.com"):
    return store.create_site(account["id"], url)


def _make_draft(client, store, site_id, provider="facebook",
                text="Hello from Ventoryn",
                target_url="https://ada.example.com/welcome"):
    r = client.post(
        f"/websites/sites/{site_id}/social/{provider}/drafts",
        data={"text": text, "link_url": target_url},
        follow_redirects=False)
    assert r.status_code == 303, r.text
    drafts = [d for d in store.list_social_drafts(site_id)
              if d["kind"] == provider]
    assert drafts, "compose left no draft"
    return drafts[-1]


def _approve_draft(client, store, account, site_id, provider, draft_id):
    # Approving is a sensitive action: the account has TOTP enabled
    # (sign_in forces it), so a fresh code travels with the post.
    return client.post(
        f"/websites/sites/{site_id}/social/{provider}/drafts/{draft_id}"
        "/approve",
        data={"totp_code": fresh_totp_code(store, account["id"])},
        follow_redirects=False)


def _reject_draft(client, site_id, provider, draft_id):
    r = client.post(
        f"/websites/sites/{site_id}/social/{provider}/drafts/{draft_id}"
        "/reject", data={}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def _connect_facebook(store, site_id):
    """A connected Facebook Page, as the OAuth callback leaves it.
    ensure_connections first: update_connection only UPDATEs, so the
    row must exist (the site page backfills it on load)."""
    store.ensure_connections(site_id)
    store.update_connection(
        site_id, "facebook", "connected",
        "Connected — Facebook Page Ada Shop linked.",
        {"access_token": "page-token-1", "provider_user_id": "page-1",
         "page_id": "page-1", "page_name": "Ada Shop",
         "ig_user_id": "ig-1"})


# ---------------------------------------------------------------------------
# OAuth start
# ---------------------------------------------------------------------------

def test_oauth_start_redirects_to_provider_authorize(monkeypatch):
    app, store, _config = _app_with(facebook=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    seen = {}

    def fake_build_authorize_url(provider, config, redirect_uri, state):
        seen.update(provider=provider, redirect_uri=redirect_uri,
                    state=state)
        return ("https://www.facebook.com/v18.0/dialog/oauth"
                "?client_id=test-fb-id", None)

    monkeypatch.setattr(saas_routes, "build_authorize_url",
                        fake_build_authorize_url)
    r = client.get(
        f"/websites/sites/{site['id']}/connections/facebook/start",
        follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith(
        "https://www.facebook.com/v18.0/dialog/oauth")
    assert seen["provider"] == "facebook"
    assert seen["redirect_uri"].endswith(
        "/account/auth/facebook/callback")
    assert seen["state"]


def test_oauth_start_refuses_when_app_unconfigured():
    # No fake: the real start route refuses honestly when the Meta
    # app is not registered/configured.
    app, store, _config = _app_with()
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    r = client.get(
        f"/websites/sites/{site['id']}/connections/facebook/start",
        follow_redirects=False)
    assert r.status_code == 400
    assert "Meta app registration" in r.text


def test_oauth_start_refuses_another_accounts_site():
    app, store, _config = _app_with(facebook=True)
    client, account_a = _customer_client(app, store)
    _client_b, account_b = _customer_client(app, store, CUSTOMER_B)
    site_b = _seed_site(store, account_b, "https://ben.example.com")
    r = client.get(
        f"/websites/sites/{site_b['id']}/connections/facebook/start",
        follow_redirects=False)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# OAuth callback
# ---------------------------------------------------------------------------

def test_oauth_callback_facebook_single_page_finishes(monkeypatch):
    app, store, _config = _app_with(facebook=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    state = store.create_connection_oauth_state(site["id"], "facebook",
                                                "")
    monkeypatch.setattr(
        saas_routes, "exchange_code",
        lambda provider, config, code, redirect_uri: {
            "access_token": "fb-user-token",
            "provider_user_id": "fb-user-1", "expires_in": 3600})
    monkeypatch.setattr(
        saas_routes, "exchange_long_lived_token",
        lambda token, config, client=None: {
            "access_token": "fb-ll-token", "expires_in": 5184000})
    monkeypatch.setattr(
        saas_routes, "list_facebook_pages",
        lambda token, config, client=None: [{"id": "page-1",
                                             "name": "Ada Shop"}])
    monkeypatch.setattr(
        saas_routes, "exchange_for_page_token",
        lambda token, config, page_id=None, client=None: {
            "access_token": "page-token-1", "page_id": "page-1",
            "page_name": "Ada Shop",
            "instagram_business_account_id": "ig-1"})
    r = client.get(
        f"/account/auth/facebook/callback?code=authcode&state={state}",
        follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/websites/sites/{site['id']}"
    conn = store.get_connection(site["id"], "facebook")
    assert conn["status"] == "connected"
    assert conn["_detail_raw"]["page_id"] == "page-1"
    assert conn["_detail_raw"]["page_name"] == "Ada Shop"
    # The stored detail is encrypted — the plaintext Page token is
    # never in the database row.
    row = store._conn.execute(
        "SELECT detail FROM saas_connections WHERE site_id = ? "
        "AND kind = 'facebook'", (site["id"],)).fetchone()
    assert "page-token-1" not in (row[0] or "")


def test_oauth_callback_rejects_another_accounts_tokens(monkeypatch):
    app, store, _config = _app_with(facebook=True)
    client, account = _customer_client(app, store)
    _client_b, account_b = _customer_client(app, store, CUSTOMER_B)
    site_b = _seed_site(store, account_b, "https://ben.example.com")
    # The state row belongs to B's site, but A is signed in.
    state = store.create_connection_oauth_state(site_b["id"],
                                                "facebook", "")
    monkeypatch.setattr(
        saas_routes, "exchange_code",
        lambda provider, config, code, redirect_uri: {
            "access_token": "fb-user-token",
            "provider_user_id": "fb-user-1", "expires_in": 3600})
    r = client.get(
        f"/account/auth/facebook/callback?code=authcode&state={state}",
        follow_redirects=False)
    assert r.status_code == 400
    assert "not valid" in r.text


def test_oauth_callback_instagram_finds_business_account(monkeypatch):
    app, store, _config = _app_with(facebook=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    state = store.create_connection_oauth_state(site["id"], "instagram",
                                                "")
    monkeypatch.setattr(
        saas_routes, "exchange_code",
        lambda provider, config, code, redirect_uri: {
            "access_token": "ig-user-token",
            "provider_user_id": "ig-user-9", "expires_in": 3600})
    monkeypatch.setattr(
        saas_routes, "exchange_long_lived_token",
        lambda token, config, client=None: {
            "access_token": "ig-ll-token", "expires_in": 5184000})
    monkeypatch.setattr(
        saas_routes, "find_instagram_account",
        lambda token, config, client=None: "ig-99")
    r = client.get(
        f"/account/auth/instagram/callback?code=authcode&state={state}",
        follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/websites/sites/{site['id']}"
    conn = store.get_connection(site["id"], "instagram")
    assert conn["status"] == "connected"
    assert conn["_detail_raw"]["ig_user_id"] == "ig-99"


# ---------------------------------------------------------------------------
# Site page: Founding Customer, drafts, connect buttons, backfill
# ---------------------------------------------------------------------------

def test_site_page_shows_founding_customer_zero_and_social_sections():
    app, store, _config = _app_with()
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    r = client.get(f"/websites/sites/{site['id']}")
    assert r.status_code == 200
    assert "0 / 100" in r.text
    assert "Founding Customer Program" in r.text
    # The Facebook/Instagram connection cards render (draft rows only
    # appear once a Page/account is connected).
    assert "<h3>facebook" in r.text
    assert "<h3>instagram" in r.text
    # Nothing is faked as connected, and no Connect button appears
    # while the apps are unregistered.
    assert "Connect with Facebook" not in r.text
    assert "Connect with Instagram" not in r.text
    assert "registers Ventoryn's Meta app" in r.text
    # No First-100 branding anywhere customer-facing.
    assert "First 100" not in r.text
    assert "first 100" not in r.text


def test_site_page_shows_connect_buttons_when_app_configured():
    app, store, _config = _app_with(x=True, facebook=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    r = client.get(f"/websites/sites/{site['id']}")
    assert r.status_code == 200
    assert "Connect with X" in r.text
    assert f"/websites/sites/{site['id']}/connections/x/start" in r.text
    assert "Connect with Facebook" in r.text
    assert (f"/websites/sites/{site['id']}/connections/facebook/start"
            in r.text)
    assert "Connect with Instagram" in r.text
    assert (f"/websites/sites/{site['id']}/connections/instagram/start"
            in r.text)


def test_backfill_adds_facebook_instagram_to_old_site():
    app, store, _config = _app_with()
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    # Simulate a site created before facebook/instagram existed.
    with store._conn:
        store._conn.execute(
            "DELETE FROM saas_connections WHERE site_id = ? "
            "AND kind IN ('facebook', 'instagram')", (site["id"],))
    kinds = {c["kind"] for c in store.get_connections(site["id"])}
    assert "facebook" not in kinds
    r = client.get(f"/websites/sites/{site['id']}")
    assert r.status_code == 200
    kinds = {c["kind"]: c for c in store.get_connections(site["id"])}
    assert kinds["facebook"]["status"] == "not_connected"
    assert kinds["instagram"]["status"] == "not_connected"


def test_first100_counts_verified_only():
    app, store, _config = _app_with()
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    # One real Stripe payment (verified) + one owner-typed manual
    # sale (never verified).
    store.record_conversion(site["id"], 1900, "usd", "stripe",
                            stripe_event_id="evt-live-1",
                            attribution="ventoryn",
                            signature_tag="vtn_act-1", action_id="act-1",
                            stripe_customer_id="cus_1",
                            customer_email="buyer@example.com",
                            livemode=1)
    store.record_conversion(site["id"], 1900, "usd", "manual",
                            attribution="unattributed", livemode=0)
    r = client.get(f"/websites/sites/{site['id']}")
    assert r.status_code == 200
    assert "1 / 100" in r.text
    # Main's paying count is live-mode only (paying_customer_count
    # excludes livemode=0), so the manual sale is recorded but not
    # counted as paying — 1 paying, not 2. The Stripe sale is the
    # one verified customer and the one Ventoryn-attributed sale.
    assert "1 paying customers recorded" in r.text
    assert "1 Ventoryn-attributed" in r.text


# ---------------------------------------------------------------------------
# Draft flow: compose -> approve-and-post / reject (main's draft system)
# ---------------------------------------------------------------------------

def test_draft_approve_shows_on_approvals_and_ops():
    # A pending Facebook draft is visible on /approvals and on the
    # Overview strip, with the cost note shown before Approve — and
    # no bare $0 figure anywhere. Approving posts in one step on
    # main, so this test stops at visibility, then rejects: the
    # reject is terminal and the draft lands in Decided.
    app, store, _config = _app_with()
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    draft = _make_draft(client, store, site["id"])
    assert draft["status"] == "pending"

    r = client.get("/approvals")
    assert r.status_code == 200
    assert "Pending (1)" in r.text
    assert "Hello from Ventoryn" in r.text
    assert "Cost if published" in r.text
    assert "do not quote $0 to a customer" in r.text
    assert "$0.00" not in r.text

    r = client.get("/ops")
    assert r.status_code == 200
    assert "Needs your decision" in r.text
    assert "Hello from Ventoryn" in r.text
    assert "Cost if published" in r.text

    _reject_draft(client, site["id"], "facebook", draft["id"])
    assert store.get_social_draft(
        draft["id"])["status"] == "rejected"
    r = client.get("/approvals")
    assert r.status_code == 200
    assert "Pending (0)" in r.text
    assert "Rejected" in r.text


def test_cross_account_decide_is_blocked():
    app, store, _config = _app_with()
    client_a, account_a = _customer_client(app, store)
    client_b, _account_b = _customer_client(app, store, CUSTOMER_B)
    site = _seed_site(store, account_a)
    draft = _make_draft(client_a, store, site["id"])
    # B's approve hits the ownership check first (404), before any
    # TOTP check — no code needed to prove the block.
    r = client_b.post(
        f"/websites/sites/{site['id']}/social/facebook/drafts/"
        f"{draft['id']}/approve", data={}, follow_redirects=False)
    assert r.status_code == 404
    assert store.get_social_draft(
        draft["id"])["status"] == "pending"


def test_publish_refused_with_flag_off():
    app, store, _config = _app_with(facebook=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    draft = _make_draft(client, store, site["id"])
    r = _approve_draft(client, store, account, site["id"], "facebook",
                       draft["id"])
    assert r.status_code == 400
    assert "Social publishing is off" in r.text
    assert store.get_social_draft(
        draft["id"])["status"] == "pending"


class _FakeFacebookClient:
    def __init__(self):
        self.calls = []

    def publish_post(self, text, link=None):
        self.calls.append((text, link))
        return {"provider_post_id": "fb-post-1",
                "response": {"id": "fb-post-1"}}


def test_publish_with_fake_client_records_post_and_spend(monkeypatch):
    app, store, config = _app_with(facebook=True, publishing=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    _connect_facebook(store, site["id"])
    fake = _FakeFacebookClient()
    monkeypatch.setattr(
        saas_routes, "_social_client_for",
        lambda s, c, site_id, provider, client=None: fake)
    draft = _make_draft(client, store, site["id"])
    r = _approve_draft(client, store, account, site["id"], "facebook",
                       draft["id"])
    assert r.status_code == 200, r.text
    assert "Posted to Facebook (id fb-post-1)" in r.text
    published = store.get_social_draft(draft["id"])
    assert published["status"] == "posted"
    assert published["tweet_id"] == "fb-post-1"
    assert len(fake.calls) == 1
    # The link is signed at approval — UTM + the vtn_ tag, never a
    # bare destination — and it travels to the provider exactly once.
    text, link = fake.calls[0]
    assert text == "Hello from Ventoryn"
    assert "utm_source=ventoryn" in (link or "")
    assert "vtn_" in (link or "")
    # Spend is recorded only on a confirmed publish. Facebook has no
    # per-post Meta fee, so the recorded amount is $0.00 — the row
    # existing at all is what proves the publish path recorded it.
    rows = store._conn.execute(
        "SELECT amount_usd FROM saas_social_spend WHERE draft_id = ?",
        (draft["id"],)).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 0.0
    # Idempotent: approving again does not call the provider again —
    # the draft is already decided.
    r = _approve_draft(client, store, account, site["id"], "facebook",
                       draft["id"])
    assert r.status_code == 400
    assert "already posted" in r.text
    assert len(fake.calls) == 1


def test_connection_update_form_rejects_oauth_kinds():
    app, store, _config = _app_with(facebook=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    for kind, label in (("facebook", "Facebook"),
                        ("instagram", "Instagram")):
        r = client.post(
            f"/websites/sites/{site['id']}/connections/{kind}",
            data={"api_key": "whatever"}, follow_redirects=False)
        assert r.status_code == 400
        assert f"Connect with {label}" in r.text


def test_disconnect_facebook_marks_not_connected():
    app, store, _config = _app_with(facebook=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    _connect_facebook(store, site["id"])
    assert store.get_connection(
        site["id"], "facebook")["status"] == "connected"
    # Disconnect is a sensitive action: it needs a fresh TOTP code,
    # like any other disconnect on this page.
    r = client.post(
        f"/websites/sites/{site['id']}/connections/facebook/disconnect",
        data={"totp_code": fresh_totp_code(store, account["id"])},
        follow_redirects=False)
    assert r.status_code == 200, r.text
    assert "is disconnected" in r.text
    assert (store.get_connection(site["id"], "facebook")["status"]
            == "not_connected")

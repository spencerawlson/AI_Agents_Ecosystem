"""X (Twitter) OAuth 2.0 + PKCE connection, read-only audience summary,
and approval-first posting. All X/Stripe HTTP is mocked — no live
keys, no live posts, ever (see tests/conftest.py's network guard).
"""
import io
import json
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import ventoryn.saas as saas
from ventoryn.saas import SaasConfig, SaasStore
from ventoryn.app import create_app


@pytest.fixture()
def store():
    s = SaasStore(":memory:")
    yield s
    s.close()


@pytest.fixture()
def config():
    return SaasConfig(
        google_client_id="google-client-id",
        google_client_secret="google-client-secret",
        x_client_id="x-client-id",
        x_client_secret="x-client-secret",
        x_app_registered=True,
        x_monthly_budget_usd=5.0,
        base_url="http://testserver",
    )


@pytest.fixture()
def client(store, config):
    return TestClient(create_app(saas_store=store, saas_config=config))


def _account(store, sub="google-sub-1", email="ada@example.com"):
    account = store.get_account_by_sub(sub)
    return account or store.create_account(sub, email, "Ada")


def _login(client, store, account=None):
    account = account or _account(store)
    token = store.create_session(account["id"])
    from ventoryn.totp import code_at
    if not store.totp_state(account["id"])["enabled"]:
        setup = store.begin_totp_setup(account["id"])
        store.confirm_totp_setup(account["id"], code_at(setup["secret"]))
    store.mark_session_totp_verified(token)
    client.cookies.set(saas.SESSION_COOKIE, token)
    return account


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


_X_USERINFO_PAYLOAD = {
    "data": {"id": "x-user-1", "username": "ada_x", "name": "Ada",
              "public_metrics": {"followers_count": 42,
                                 "following_count": 7, "tweet_count": 3}},
}


def _fake_x_urlopen(request, **kwargs):
    url = request.full_url
    if url.startswith(saas.X_TOKEN_URL):
        return _FakeResponse({"access_token": "x-tok-123",
                              "refresh_token": "x-refresh-123",
                              "expires_in": 7200,
                              "token_type": "bearer"})
    if url.startswith(saas.X_USERINFO_URL):
        return _FakeResponse(_X_USERINFO_PAYLOAD)
    if url.startswith(saas.X_POST_URL):
        return _FakeResponse({"data": {"id": "tweet-1", "text": "posted"}})
    raise AssertionError(f"unexpected X call: {url}")


def _fake_x_urlopen_token_fails(request, **kwargs):
    if request.full_url.startswith(saas.X_TOKEN_URL):
        return _FakeResponse({"error": "invalid_grant"})
    raise AssertionError("should not reach past the failed token exchange")


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def test_pkce_pair_is_rfc7636_shaped():
    import base64
    import hashlib

    verifier, challenge = saas.pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert all(c.isalnum() or c in "-._~" for c in verifier)
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    assert challenge == expected
    # Two calls never reuse a verifier.
    verifier2, _ = saas.pkce_pair()
    assert verifier2 != verifier


def test_x_auth_url_shape():
    config = SaasConfig(x_client_id="cid", base_url="http://testserver")
    url = saas.x_auth_url(config, "state-1", "challenge-1")
    assert url.startswith(saas.X_AUTH_URL)
    assert "client_id=cid" in url
    assert "state=state-1" in url
    assert "code_challenge=challenge-1" in url
    assert "code_challenge_method=S256" in url
    assert "redirect_uri=http%3A%2F%2Ftestserver%2Faccount%2Fauth%2Fx%2Fcallback" in url


# ---------------------------------------------------------------------------
# Store: per-connection OAuth state
# ---------------------------------------------------------------------------

def test_connection_oauth_state_single_use(store):
    state = store.create_connection_oauth_state("site-1", "x", "verifier-1")
    pending = store.consume_connection_oauth_state(state)
    assert pending == {"site_id": "site-1", "kind": "x",
                       "code_verifier": "verifier-1"}
    assert store.consume_connection_oauth_state(state) is None  # no replays
    assert store.consume_connection_oauth_state("bogus") is None


# ---------------------------------------------------------------------------
# Connect flow, end to end
# ---------------------------------------------------------------------------

def test_x_connect_flow(client, store, config, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_x_urlopen)
    account = _login(client, store)
    site = store.create_site(account["id"], "https://example.com")

    r = client.get(f"/websites/sites/{site['id']}/connections/x/start",
                   follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith(saas.X_AUTH_URL)
    q = parse_qs(urlparse(r.headers["location"]).query)
    state = q["state"][0]
    assert q["code_challenge_method"][0] == "S256"

    r = client.get("/account/auth/x/callback",
                   params={"code": "auth-code", "state": state},
                   follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/websites/sites/{site['id']}"

    conn = store.get_connection(site["id"], "x")
    assert conn["status"] == "connected"
    assert "ada_x" in conn["note"]
    # The access token is encrypted at rest — never plaintext in the
    # stored column.
    assert "x-tok-123" not in conn["detail"]
    assert conn["_detail_raw"]["access_token"] == "x-tok-123"
    # The refresh token and the expiry are stored too, so the publish
    # path can renew the ~2-hour access token without re-prompting.
    assert conn["_detail_raw"]["refresh_token"] == "x-refresh-123"
    expires_at = conn["_detail_raw"]["expires_at"]
    assert expires_at
    assert datetime.fromisoformat(expires_at) > datetime.now(timezone.utc)


def test_x_connect_rejects_bad_state(client, store, config, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_x_urlopen)
    account = _login(client, store)
    site = store.create_site(account["id"], "https://example.com")
    r = client.get("/account/auth/x/callback",
                   params={"code": "auth-code", "state": "bogus"})
    assert r.status_code == 400
    assert store.get_connection(site["id"], "x")["status"] == "not_connected"


def test_x_connect_start_400_when_not_configured(store):
    # A deliberately unconfigured app: no x_client_id/secret at all.
    app = create_app(saas_store=store,
                     saas_config=SaasConfig(base_url="http://testserver"))
    c = TestClient(app)
    account = _login(c, store)
    site = store.create_site(account["id"], "https://example.com")
    r = c.get(f"/websites/sites/{site['id']}/connections/x/start",
              follow_redirects=False)
    assert r.status_code == 400
    assert "not available yet" in r.text


def test_x_connect_callback_surfaces_exchange_failure(client, store, config,
                                                      monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        _fake_x_urlopen_token_fails)
    account = _login(client, store)
    site = store.create_site(account["id"], "https://example.com")
    r = client.get(f"/websites/sites/{site['id']}/connections/x/start",
                   follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = client.get("/account/auth/x/callback",
                   params={"code": "bad-code", "state": state})
    assert r.status_code == 400
    assert store.get_connection(site["id"], "x")["status"] == "not_connected"


# ---------------------------------------------------------------------------
# Posting gate
# ---------------------------------------------------------------------------

def test_check_posting_allowed_for_x(store):
    account = _account(store)
    site = store.create_site(account["id"], "https://example.com")
    config_unregistered = SaasConfig(x_app_registered=False)
    allowed, reason = saas.check_posting_allowed(
        store, account, site["id"], "x", config_unregistered)
    assert allowed is False
    assert "app registration" in reason

    config_registered = SaasConfig(x_app_registered=True)
    allowed, reason = saas.check_posting_allowed(
        store, account, site["id"], "x", config_registered)
    assert allowed is False
    assert "connect this account" in reason

    store.update_connection(site["id"], "x", "connected", "Connected.",
                           {"access_token": "tok"})
    allowed, reason = saas.check_posting_allowed(
        store, account, site["id"], "x", config_registered)
    assert allowed is True


# ---------------------------------------------------------------------------
# Social drafts: compose -> approve-and-post, never automatic
# ---------------------------------------------------------------------------

def _connect_x(store, site_id, access_token="x-tok-123"):
    store.update_connection(site_id, "x", "connected", "Connected as @ada_x.",
                            {"access_token": access_token, "username": "ada_x"})


def test_draft_create_never_posts(client, store, config):
    account = _login(client, store)
    site = store.create_site(account["id"], "https://example.com")
    _connect_x(store, site["id"])
    r = client.post(f"/websites/sites/{site['id']}/social/x/drafts",
                    data={"text": "Check out our new feature!",
                          "link_url": ""}, follow_redirects=False)
    assert r.status_code == 303
    drafts = store.list_social_drafts(site["id"])
    assert len(drafts) == 1
    assert drafts[0]["status"] == "pending"
    assert drafts[0]["tweet_id"] == ""


def test_draft_reject_is_terminal(store):
    account = _account(store)
    site = store.create_site(account["id"], "https://example.com")
    draft = store.create_social_draft(site["id"], "x", "hello")
    assert store.decide_social_draft(draft["id"], "rejected") is True
    assert store.get_social_draft(draft["id"])["status"] == "rejected"
    # Already decided: a second decision is a no-op (not re-applied).
    assert store.decide_social_draft(draft["id"], "posted") is False


def test_post_to_x_approve_posts_and_signs_link(store, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_x_urlopen)
    # Production reads this from the real environment (see
    # docs/implementation/FIRST_CUSTOMER_BASELINE.md) — post_to_x
    # doesn't pass an explicit key to sign_agent_link, same as every
    # other production call site.
    monkeypatch.setenv("VENTORYN_TOKEN_KEY", "test-key")
    account = _account(store)
    site = store.create_site(account["id"], "https://example.com")
    _connect_x(store, site["id"])
    draft = store.create_social_draft(
        site["id"], "x", "New post: https://example.com/launch",
        "https://example.com/launch")
    config = SaasConfig(x_app_registered=True, x_monthly_budget_usd=5.0)

    posted, message = saas.post_to_x(store, config, account, site["id"],
                                     draft["id"])
    assert posted is True
    assert "tweet-1" in message

    updated = store.get_social_draft(draft["id"])
    assert updated["status"] == "posted"
    assert updated["tweet_id"] == "tweet-1"
    assert updated["signature_tag"]
    # The signed link was actually recorded for attribution.
    recorded = store.get_signature_by_tag(site["id"], updated["signature_tag"])
    assert recorded is not None
    assert recorded["agent"] == "x"
    # A linked post costs the reported per-post amount, recorded for
    # the budget cap.
    assert store.x_spend_this_month_usd(site["id"]) == pytest.approx(0.20)


def test_post_to_x_without_link_costs_the_plain_rate(store, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_x_urlopen)
    account = _account(store)
    site = store.create_site(account["id"], "https://example.com")
    _connect_x(store, site["id"])
    draft = store.create_social_draft(site["id"], "x", "Just a note, no link")
    config = SaasConfig(x_app_registered=True, x_monthly_budget_usd=5.0)
    posted, _ = saas.post_to_x(store, config, account, site["id"], draft["id"])
    assert posted is True
    # X's reported plain-post rate is $0.015 — recorded as 2 cents
    # (rounded up, so the monthly cap is never under-counted).
    assert store.x_spend_this_month_usd(site["id"]) == pytest.approx(0.02)


def test_post_to_x_refuses_over_budget(store, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_x_urlopen)
    account = _account(store)
    site = store.create_site(account["id"], "https://example.com")
    _connect_x(store, site["id"])
    draft = store.create_social_draft(
        site["id"], "x", "https://example.com/a", "https://example.com/a")
    # Budget smaller than one linked post's reported cost.
    config = SaasConfig(x_app_registered=True, x_monthly_budget_usd=0.10)
    posted, message = saas.post_to_x(store, config, account, site["id"],
                                     draft["id"])
    assert posted is False
    assert "budget" in message
    # Nothing was posted: the draft is still pending, nothing charged.
    assert store.get_social_draft(draft["id"])["status"] == "pending"
    assert store.x_spend_this_month_usd(site["id"]) == 0.0


def test_post_to_x_refuses_when_not_connected(store):
    account = _account(store)
    site = store.create_site(account["id"], "https://example.com")
    draft = store.create_social_draft(site["id"], "x", "hello")
    config = SaasConfig(x_app_registered=True)
    posted, message = saas.post_to_x(store, config, account, site["id"],
                                     draft["id"])
    assert posted is False
    assert "connect this account" in message


def test_post_to_x_refuses_already_decided_draft(store, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_x_urlopen)
    account = _account(store)
    site = store.create_site(account["id"], "https://example.com")
    _connect_x(store, site["id"])
    draft = store.create_social_draft(site["id"], "x", "hello")
    store.decide_social_draft(draft["id"], "rejected")
    config = SaasConfig(x_app_registered=True)
    posted, message = saas.post_to_x(store, config, account, site["id"],
                                     draft["id"])
    assert posted is False
    assert "already rejected" in message


# ---------------------------------------------------------------------------
# X token refresh — access tokens live ~2 hours; the stored refresh
# token renews them so a post days after connecting still works.
# ---------------------------------------------------------------------------

def _connect_x_expired(store, site_id):
    past = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    store.update_connection(site_id, "x", "connected", "Connected as @ada_x.",
                            {"access_token": "x-tok-old",
                             "refresh_token": "x-refresh-old",
                             "username": "ada_x", "expires_at": past})


def test_post_to_x_refreshes_expired_token(store, config):
    account = _account(store)
    site = store.create_site(account["id"], "https://example.com")
    _connect_x_expired(store, site["id"])
    draft = store.create_social_draft(site["id"], "x", "Just a note, no link")
    posted_with = []

    def _urlopen(request, **kwargs):
        if request.full_url.startswith(saas.X_TOKEN_URL):
            return _FakeResponse({"access_token": "x-tok-new",
                                  "refresh_token": "x-refresh-new",
                                  "expires_in": 7200,
                                  "token_type": "bearer"})
        if request.full_url.startswith(saas.X_POST_URL):
            posted_with.append(request.get_header("Authorization"))
            return _FakeResponse({"data": {"id": "tweet-9", "text": "posted"}})
        raise AssertionError(f"unexpected X call: {request.full_url}")

    posted, message = saas.post_to_x(store, config, account, site["id"],
                                     draft["id"], urlopen=_urlopen)
    assert posted is True
    assert "tweet-9" in message
    # The post went out on the FRESH token, not the expired one.
    assert posted_with == ["Bearer x-tok-new"]
    raw = store.get_connection(site["id"], "x")["_detail_raw"]
    assert raw["access_token"] == "x-tok-new"
    # The ROTATED refresh token was persisted before the new access
    # token was used — the old one is single-use.
    assert raw["refresh_token"] == "x-refresh-new"
    assert datetime.fromisoformat(raw["expires_at"]) > datetime.now(timezone.utc)


def test_post_to_x_401_refreshes_once_and_retries(store, config):
    account = _account(store)
    site = store.create_site(account["id"], "https://example.com")
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    store.update_connection(site["id"], "x", "connected", "Connected as @ada_x.",
                            {"access_token": "x-tok-fresh",
                             "refresh_token": "x-refresh-old",
                             "username": "ada_x", "expires_at": future})
    draft = store.create_social_draft(site["id"], "x", "Just a note, no link")
    calls = {"token": 0, "post": 0}
    posted_with = []

    def _urlopen(request, **kwargs):
        if request.full_url.startswith(saas.X_TOKEN_URL):
            calls["token"] += 1
            return _FakeResponse({"access_token": "x-tok-new",
                                  "refresh_token": "x-refresh-new",
                                  "expires_in": 7200,
                                  "token_type": "bearer"})
        if request.full_url.startswith(saas.X_POST_URL):
            calls["post"] += 1
            posted_with.append(request.get_header("Authorization"))
            if calls["post"] == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 401, "Unauthorized", {},
                    io.BytesIO(b""))
            return _FakeResponse({"data": {"id": "tweet-2", "text": "posted"}})
        raise AssertionError(f"unexpected X call: {request.full_url}")

    posted, message = saas.post_to_x(store, config, account, site["id"],
                                     draft["id"], urlopen=_urlopen)
    assert posted is True
    assert "tweet-2" in message
    # Exactly one refresh and one retry — no infinite loop, no second
    # refresh after the retry.
    assert calls == {"token": 1, "post": 2}
    assert posted_with == ["Bearer x-tok-fresh", "Bearer x-tok-new"]
    raw = store.get_connection(site["id"], "x")["_detail_raw"]
    assert raw["refresh_token"] == "x-refresh-new"


def test_fresh_x_access_token_returns_stored_token_when_fresh(store, config):
    account = _account(store)
    site = store.create_site(account["id"], "https://example.com")
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    store.update_connection(site["id"], "x", "connected", "Connected as @ada_x.",
                            {"access_token": "x-tok-fresh",
                             "refresh_token": "x-refresh-old",
                             "username": "ada_x", "expires_at": future})

    def _no_http(request, **kwargs):
        raise AssertionError("no HTTP call expected for a fresh token")

    token, refreshed = saas.fresh_x_access_token(
        store, config, site["id"], urlopen=_no_http)
    assert token == "x-tok-fresh"
    assert refreshed is False


def test_fresh_x_access_token_raises_when_refresh_rejected(store, config):
    account = _account(store)
    site = store.create_site(account["id"], "https://example.com")
    _connect_x_expired(store, site["id"])

    def _urlopen(request, **kwargs):
        if request.full_url.startswith(saas.X_TOKEN_URL):
            return _FakeResponse({"error": "invalid_grant"})
        raise AssertionError(f"unexpected X call: {request.full_url}")

    with pytest.raises(saas.SaasError, match="Reconnect X"):
        saas.fresh_x_access_token(store, config, site["id"], urlopen=_urlopen)


# ---------------------------------------------------------------------------
# Read-only audience summary
# ---------------------------------------------------------------------------

def test_x_account_summary(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_x_urlopen)
    summary = saas.x_account_summary("x-tok-123")
    assert summary == {"username": "ada_x", "followers": 42,
                       "following": 7, "posts": 3}


def test_x_account_summary_none_on_failure(monkeypatch):
    def _broken(request, **kwargs):
        raise AssertionError("network should not actually be reached")

    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _FakeResponse({}))
    assert saas.x_account_summary("x-tok-123") is None


def test_x_connect_from_integrations_returns_to_that_window(client, store, config,
                                                            monkeypatch):
    # Started from an Integrations window: after X sends the owner back,
    # they land on the X app page with the same website's window open.
    monkeypatch.setattr("urllib.request.urlopen", _fake_x_urlopen)
    account = _login(client, store)
    site = store.create_site(account["id"], "https://example.com")
    back = f"/integrations/x?site={site['id']}"
    r = client.get(f"/websites/sites/{site['id']}/connections/x/start",
                   params={"return_to": back}, follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = client.get("/account/auth/x/callback",
                   params={"code": "auth-code", "state": state},
                   follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == back
    assert store.get_connection(site["id"], "x")["status"] == "connected"
    # The one-shot cookie is cleared, so a later connect from the site
    # page goes back to the site page.
    assert 'ventoryn_x_return=""' in r.headers.get("set-cookie", "") or \
        "Max-Age=0" in r.headers.get("set-cookie", "")


def test_site_page_shows_campaign_estimate_and_cost_before_posting(client, store, monkeypatch):
    # The page reads the connected account's audience; keep it offline.
    monkeypatch.setattr(saas, "x_account_summary", lambda *a, **k: None, raising=False)
    import ventoryn.saas_routes as routes
    monkeypatch.setattr(routes, "x_account_summary", lambda *a, **k: None, raising=False)
    account = _login(client, store)
    site = store.create_site(account["id"], "https://example.com")
    _connect_x(store, site["id"])
    store.create_social_draft(site["id"], "x", "Launch day", "https://example.com/a")
    html = client.get(f"/websites/sites/{site['id']}").text
    assert "Campaign cost estimate" in html
    assert "30 X posts with a link over 30 days at $0.200 each: about $6.00." in html
    assert "more than the $5.00 left" in html and "covers 25 of those posts" in html
    assert "Cost if posted: about <strong>$0.200</strong> on X (post with a link" in html
    assert "X budget this month: $0.00 of $5.00 used." in html

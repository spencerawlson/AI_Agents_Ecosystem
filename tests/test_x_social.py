"""X (Twitter) OAuth 2.0 + PKCE connection, read-only audience summary,
and approval-first posting. All X/Stripe HTTP is mocked — no live
keys, no live posts, ever (see tests/conftest.py's network guard).
"""
import json
import sys
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

    r = client.get(f"/account/sites/{site['id']}/connections/x/start",
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
    assert r.headers["location"] == f"/account/sites/{site['id']}"

    conn = store.get_connection(site["id"], "x")
    assert conn["status"] == "connected"
    assert "ada_x" in conn["note"]
    # The access token is encrypted at rest — never plaintext in the
    # stored column.
    assert "x-tok-123" not in conn["detail"]
    assert conn["_detail_raw"]["access_token"] == "x-tok-123"
    assert conn["_detail_raw"]["username"] == "ada_x"


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
    r = c.get(f"/account/sites/{site['id']}/connections/x/start",
              follow_redirects=False)
    assert r.status_code == 400
    assert "not available yet" in r.text


def test_x_connect_callback_surfaces_exchange_failure(client, store, config,
                                                      monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        _fake_x_urlopen_token_fails)
    account = _login(client, store)
    site = store.create_site(account["id"], "https://example.com")
    r = client.get(f"/account/sites/{site['id']}/connections/x/start",
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
    r = client.post(f"/account/sites/{site['id']}/social/x/drafts",
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


def test_post_to_x_without_link_costs_nothing(store, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_x_urlopen)
    account = _account(store)
    site = store.create_site(account["id"], "https://example.com")
    _connect_x(store, site["id"])
    draft = store.create_social_draft(site["id"], "x", "Just a note, no link")
    config = SaasConfig(x_app_registered=True, x_monthly_budget_usd=5.0)
    posted, _ = saas.post_to_x(store, config, account, site["id"], draft["id"])
    assert posted is True
    assert store.x_spend_this_month_usd(site["id"]) == 0.0


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

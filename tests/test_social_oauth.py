"""Social OAuth adapters (Facebook/Instagram) — authorize URLs,
code exchange, refresh, encrypted token storage, honest refusal.

No live calls: every HTTP exchange goes through a fake client.
X is deliberately not here: X OAuth stays inline on main
(ventoryn/saas.py), and this module has no PKCE, no begin/complete
handshake, and no disconnect helper — routes use
SaasStore.create_connection_oauth_state / update_connection and
main's disconnect route directly.
"""


import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

import pytest

from ventoryn.saas import SaasConfig, SaasError, SaasStore
from ventoryn.social_oauth import (
    SCOPES,
    build_authorize_url,
    exchange_code,
    exchange_for_page_token,
    get_profile,
    list_facebook_pages,
    refresh_access_token,
    refresh_access_token_if_needed,
)


@pytest.fixture(autouse=True)
def _token_key(monkeypatch):
    monkeypatch.setenv("VENTORYN_TOKEN_KEY", "test-token-key-phase2")


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttp:
    """httpx-like fake: routes keyed by (method, URL substring)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _serve(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        # Longest needle first: /me/accounts must win over /me.
        for (m, needle), outcome in sorted(
                self.routes.items(), key=lambda kv: -len(kv[0][1])):
            if m == method and needle in url:
                if isinstance(outcome, Exception):
                    raise outcome
                status, payload = outcome
                return FakeResponse(status, payload)
        raise AssertionError(f"unexpected {method} {url}")

    def get(self, url, headers=None, params=None):
        full = url
        if params:
            import urllib.parse

            full += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return self._serve("GET", full, headers=headers, params=params)

    def post(self, url, data=None, headers=None, json=None):
        return self._serve("POST", url, data=data, headers=headers, json=json)


FB_TOKEN_OK = (200, {"access_token": "fb-acc-token", "token_type": "bearer",
                     "expires_in": 5184000})
FB_PROFILE_OK = (200, {"id": "fb-user-9", "name": "Fb User"})
FB_ACCOUNTS_OK = (200, {"data": [{"id": "page-1", "name": "Test Page",
                                  "access_token": "page-token-1",
                                  "instagram_business_account": {"id": "ig-7"}}]})


FB_ENV = {"VENTORYN_FACEBOOK_APP_ID": "fb-app-id",
          "VENTORYN_FACEBOOK_APP_SECRET": "fb-app-secret"}


def _fb_client():
    return FakeHttp({("GET", "graph.facebook.com/v24.0/oauth/access_token"): FB_TOKEN_OK,
                     ("GET", "graph.facebook.com/v24.0/me"): FB_PROFILE_OK,
                     ("GET", "graph.facebook.com/v24.0/me/accounts"): FB_ACCOUNTS_OK})


def test_facebook_authorize_url_carries_scopes_and_no_verifier():
    config = SaasConfig.from_env(FB_ENV)
    url, verifier = build_authorize_url(
        "facebook", config, "https://example.com/callback", "state-fb")
    assert "facebook.com/v24.0/dialog/oauth" in url
    assert "pages_manage_posts" in url
    assert "pages_show_list" in url
    assert verifier is None


def test_instagram_scopes_never_include_messaging():
    assert "instagram_business_basic" in SCOPES["instagram"]
    assert "instagram_business_content_publish" in SCOPES["instagram"]
    assert "message" not in SCOPES["instagram"].lower()
    assert "manage_messages" not in SCOPES["instagram"]


def test_facebook_exchange_code_via_fake_client():
    config = SaasConfig.from_env(FB_ENV)
    tokens = exchange_code("facebook", config, "fb-code-1",
                           "https://example.com/callback",
                           client=_fb_client())
    assert tokens["access_token"] == "fb-acc-token"
    assert tokens["provider_user_id"] == "fb-user-9"


def test_refresh_without_token_refused():
    config = SaasConfig.from_env(FB_ENV)
    with pytest.raises(SaasError):
        refresh_access_token("facebook", config, "", client=_fb_client())
    with pytest.raises(SaasError):
        refresh_access_token("instagram", config, "", client=_fb_client())


def test_oauth_refused_when_app_not_registered():
    # For Meta, "registered" is the configured app id + secret:
    # SaasConfig.app_registered returns facebook_configured for
    # facebook/instagram, so an unconfigured app is unregistered.
    config = SaasConfig.from_env({})
    assert not config.app_registered("facebook")
    assert not config.app_registered("instagram")
    with pytest.raises(SaasError, match="not configured"):
        build_authorize_url("facebook", config,
                            "https://example.com/callback", "state-1")


def test_oauth_refused_when_credentials_unconfigured():
    fb_config = SaasConfig.from_env({})
    with pytest.raises(SaasError, match="not configured"):
        build_authorize_url("facebook", fb_config,
                            "https://example.com/callback", "state-1")
    ig_config = SaasConfig.from_env({})
    with pytest.raises(SaasError, match="not configured"):
        build_authorize_url("instagram", ig_config,
                            "https://example.com/callback", "state-1")


def test_exchange_refused_when_unconfigured():
    config = SaasConfig.from_env({})
    with pytest.raises(SaasError):
        exchange_code("facebook", config, "code",
                      "https://example.com/callback", client=_fb_client())


def test_token_storage_is_encrypted_at_rest():
    store = SaasStore()
    account = store.create_account("sub-1", "a@example.com")
    site = store.create_site(account["id"], "https://example.com")
    store.update_connection(
        site["id"], "facebook", "connected",
        "Connected — Facebook Page linked.", {
        "access_token": "SECRET-ACCESS-TOKEN",
        "refresh_token": "SECRET-REFRESH-TOKEN",
        "provider_user_id": "fb-user-9",
        "scope": SCOPES["facebook"], "expires_in": 5184000})
    # Raw DB row: ciphertext, never plaintext.
    row = store._conn.execute(
        "SELECT detail FROM saas_connections WHERE site_id = ? AND kind = 'facebook'",
        (site["id"],)).fetchone()
    detail = json.loads(row[0])
    assert detail["access_token"].startswith("v1.")
    assert detail["refresh_token"].startswith("v1.")
    assert "SECRET-ACCESS-TOKEN" not in row[0]
    assert "SECRET-REFRESH-TOKEN" not in row[0]
    # get_connection decrypts for server-side use.
    conn = store.get_connection(site["id"], "facebook")
    assert conn["_detail_raw"]["access_token"] == "SECRET-ACCESS-TOKEN"
    assert conn["status"] == "connected"
    # get_connections strips secrets for rendering.
    listed = store.get_connections(site["id"])
    fb_conn = next(c for c in listed if c["kind"] == "facebook")
    assert "access_token" not in fb_conn["detail"]
    assert "refresh_token" not in fb_conn["detail"]


def test_facebook_page_token_exchange():
    config = SaasConfig.from_env(FB_ENV)
    page = exchange_for_page_token("fb-user-token", config,
                                   client=_fb_client())
    assert page["page_id"] == "page-1"
    assert page["access_token"] == "page-token-1"
    assert page["instagram_business_account_id"] == "ig-7"


def test_facebook_refresh_via_long_lived_exchange():
    config = SaasConfig.from_env(FB_ENV)
    refreshed = FakeHttp({
        ("GET", "graph.facebook.com/v24.0/oauth/access_token"):
            (200, {"access_token": "fb-acc-2",
                   "expires_in": 5184000, "token_type": "bearer"}),
    })
    tokens = refresh_access_token("facebook", config, "fb-acc-token",
                                  client=refreshed)
    assert tokens["access_token"] == "fb-acc-2"
    assert tokens["expires_in"] == 5184000


def test_get_profile_facebook():
    profile = get_profile("facebook", "tok", client=_fb_client())
    assert profile["provider_user_id"] == "fb-user-9"
    assert profile["name"] == "Fb User"


def test_list_facebook_pages():
    config = SaasConfig.from_env(FB_ENV)
    pages = list_facebook_pages("fb-user-token", config, client=_fb_client())
    assert pages == [{"id": "page-1", "name": "Test Page"}]


def test_refresh_if_needed_renews_expiring_instagram_token():
    store = SaasStore()
    account = store.create_account("sub-1", "a@example.com")
    site = store.create_site(account["id"], "https://example.com")
    soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    store.update_connection(
        site["id"], "instagram", "connected",
        "Connected — Instagram Business account linked.",
        {"access_token": "ig-old-token", "ig_user_id": "ig-7",
         "provider_user_id": "ig-7", "expires_at": soon})
    fake = FakeHttp({
        ("GET", "graph.facebook.com/v24.0/oauth/access_token"):
            (200, {"access_token": "ig-new-token",
                   "expires_in": 5184000, "token_type": "bearer"}),
    })
    config = SaasConfig.from_env(FB_ENV)
    token = refresh_access_token_if_needed(
        store, config, site["id"], "instagram", client=fake)
    assert token == "ig-new-token"
    detail = store.get_connection(site["id"], "instagram")["_detail_raw"]
    assert detail["access_token"] == "ig-new-token"
    assert detail["ig_user_id"] == "ig-7"


def test_refresh_if_needed_returns_facebook_page_token_as_stored():
    store = SaasStore()
    account = store.create_account("sub-1", "a@example.com")
    site = store.create_site(account["id"], "https://example.com")
    soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    store.update_connection(
        site["id"], "facebook", "connected",
        "Connected — Facebook Page linked.",
        {"access_token": "page-token-1", "page_id": "page-1",
         "expires_at": soon})

    class _NoCall:
        def get(self, *args, **kwargs):
            raise AssertionError("no HTTP call expected")

        def post(self, *args, **kwargs):
            raise AssertionError("no HTTP call expected")

    config = SaasConfig.from_env(FB_ENV)
    token = refresh_access_token_if_needed(
        store, config, site["id"], "facebook", client=_NoCall())
    assert token == "page-token-1"

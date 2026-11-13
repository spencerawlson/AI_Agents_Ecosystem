"""OAuth 2.0 adapters for Facebook and Instagram (Meta Graph).

One pattern per provider, all HTTP through an injected client — an
httpx-like object with ``.get`` / ``.post`` (a real ``httpx.Client``
is built only when no client is passed), or a urlopen-style callable
for the stdlib path. NOTHING here makes a live call at import time,
and tests use fakes only — no real Meta HTTP in tests, ever.

Provider facts (endpoints, scopes) come from the Phase 0
verification docs (docs/implementation/FACEBOOK_API_VERIFICATION.md,
INSTAGRAM_API_VERIFICATION.md). Those docs are DRAFTS built from
secondary reporting — prices and limits in them are reported, not
directly verified — so no price or limit is quoted from here. The
endpoints and scopes below are the documented OAuth shapes; a
live-browser re-check is still owed before the first real customer
connects.

Refusal is honest: when the Meta app is not configured
(SaasConfig.facebook_configured, which is also what
app_registered returns for facebook/instagram), every entry point
raises SaasError with a plain-language reason instead of failing
or faking a connection.

Facebook: scopes ``pages_show_list pages_read_engagement
pages_manage_posts``. A Page token is obtained from the long-lived
user token (see exchange_for_page_token); the long-lived exchange
itself is exchange_long_lived_token.

Instagram (via Meta login, same Graph endpoints): scopes
``instagram_basic instagram_content_publish pages_show_list
pages_read_engagement business_management`` (Instagram API with
Facebook Login).
No DM/messaging scope is ever requested — Axionory plans no DMs.

X is deliberately not here: X OAuth stays inline on main
(ventoryn/saas.py: x_auth_url, exchange_x_code, x_post,
post_to_x), and Meta login uses no PKCE, so there is no
code_verifier / code_challenge in this module at all.

Connect-flow ownership (main, not this module): routes start a
flow with SaasStore.create_connection_oauth_state and
build_authorize_url here, then exchange the code with
exchange_code and persist the tokens with
SaasStore.update_connection directly. There is no
begin/complete/save handshake helper here — main's
connection-state store replaces them, and disconnect is main's
disconnect route (revoke + disconnect_connection).
"""


from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from ventoryn.saas import SaasConfig, SaasError, SaasStore


# ---------------------------------------------------------------------------
# Constant provider URLs — never user-supplied (SSRF guard: no fetched
# URL in this module ever comes from customer input).
# ---------------------------------------------------------------------------

GRAPH_BASE = "https://graph.facebook.com/v24.0"
FB_AUTHORIZE_URL = "https://www.facebook.com/v24.0/dialog/oauth"
FB_TOKEN_URL = f"{GRAPH_BASE}/oauth/access_token"
FB_PROFILE_URL = f"{GRAPH_BASE}/me"

SCOPES: dict[str, str] = {
    "facebook": "pages_show_list pages_read_engagement pages_manage_posts",
    # Instagram API with Facebook Login: the Business account is found
    # through the Page it is linked to (/me/accounts), so the
    # Facebook-Login permissions apply — instagram_business_* belong to
    # the separate Instagram Login and would be refused here.
    "instagram": ("instagram_basic instagram_content_publish "
                  "pages_show_list pages_read_engagement business_management"),
}

PROVIDERS = frozenset({"facebook", "instagram"})

_HTTP_TIMEOUT_S = 30.0


# ---------------------------------------------------------------------------
# HTTP plumbing — httpx-like client, or a urlopen-style callable.
# ---------------------------------------------------------------------------

class _Response:
    """Minimal response shape shared by both client kinds."""

    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _UrlopenClient:
    """Wrap a urlopen-style callable (urllib) as an httpx-like client."""

    def __init__(self, urlopen) -> None:
        self._urlopen = urlopen

    def get(self, url: str, headers: dict | None = None,
            params: dict | None = None) -> _Response:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers=headers or {},
                                         method="GET")
        with self._urlopen(request) as resp:
            return _Response(resp.status,
                             json.loads(resp.read().decode("utf-8")))

    def post(self, url: str, data: dict | None = None,
             headers: dict | None = None) -> _Response:
        body = urllib.parse.urlencode(data or {}).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers=headers or {}, method="POST")
        with self._urlopen(request) as resp:
            return _Response(resp.status,
                             json.loads(resp.read().decode("utf-8")))


def _client_for(client):
    """Normalize the injected client. None builds a real httpx client
    (explicit timeout, constant URLs only); a callable is treated as
    a urlopen-style opener; anything else must already be httpx-like."""
    if client is None:
        import httpx

        return httpx.Client(timeout=_HTTP_TIMEOUT_S)
    if callable(client) and not hasattr(client, "get"):
        return _UrlopenClient(client)
    return client


def _check(response, provider: str, what: str) -> dict:
    """Raise SaasError (plain words, no secret) on a non-2xx response
    or a provider error body; otherwise return the JSON body."""
    status = getattr(response, "status_code", 0)
    try:
        body = response.json()
    except Exception:
        body = {}
    if not (200 <= status < 300) or (isinstance(body, dict)
                                     and body.get("error")):
        detail = ""
        if isinstance(body, dict):
            err = body.get("error") or {}
            if isinstance(err, dict):
                detail = err.get("message") or err.get("error_description") or ""
            elif isinstance(err, str):
                detail = err
        raise SaasError(
            f"{provider.capitalize()} did not accept the {what}. "
            f"Try again. {detail}".strip())
    return body if isinstance(body, dict) else {}


def _require_provider(provider: str) -> None:
    if provider not in PROVIDERS:
        raise SaasError(f"Unknown social provider: {provider}")


def _client_credentials(config: SaasConfig, provider: str
                        ) -> tuple[str, str]:
    """(app id, app secret) for a provider. Raises SaasError when the
    operator app is not registered or its credentials are unconfigured
    — the honest refusal, never a crash and never a fake."""
    if provider in ("facebook", "instagram"):
        if not config.facebook_configured:
            raise SaasError(
                "Facebook/Instagram is not connected — the Meta app id "
                "and secret are not configured yet.")
        return config.facebook_app_id, config.facebook_app_secret
    raise SaasError(f"Unknown social provider: {provider}")


# ---------------------------------------------------------------------------
# Authorize URLs
# ---------------------------------------------------------------------------

def build_authorize_url(provider: str, config: SaasConfig,
                        redirect_uri: str, state: str
                        ) -> tuple[str, str | None]:
    """The Meta authorization URL for a connect flow.

    Returns ``(url, None)`` — the second element is always None:
    Facebook/Instagram use no PKCE, so there is no code_verifier
    for the callback to present. The caller stores the ``state``
    server-side with ``SaasStore.create_connection_oauth_state``
    and checks it on the callback; the state never goes in a
    cookie or a URL the browser keeps."""
    _require_provider(provider)
    _client_credentials(config, provider)
    client_id, _secret = _client_credentials(config, provider)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES[provider],
        "state": state,
        "response_type": "code",
    }
    return f"{FB_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}", None


# ---------------------------------------------------------------------------
# Code exchange / refresh / profile
# ---------------------------------------------------------------------------

def _tokens_from_body(body: dict, provider: str) -> dict:
    return {
        "access_token": body.get("access_token") or "",
        "refresh_token": body.get("refresh_token") or "",
        "expires_in": body.get("expires_in"),
        "scope": body.get("scope") or "",
        "token_type": body.get("token_type") or "",
        "provider": provider,
        "provider_user_id": "",
    }


def exchange_code(provider: str, config: SaasConfig, code: str,
                  redirect_uri: str, client=None) -> dict:
    """Exchange an authorization code for tokens. Returns
    {"access_token", "refresh_token", "expires_in", "scope",
    "provider_user_id", ...}. The provider_user_id comes from a
    profile fetch with the new token; when that fetch fails the
    exchange still succeeds and the id is "" (tokens are the
    critical part — the profile is best-effort enrichment)."""
    _require_provider(provider)
    http = _client_for(client)
    client_id, client_secret = _client_credentials(config, provider)
    body = _check(http.get(
        FB_TOKEN_URL,
        params={"client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri, "code": code}),
        provider, "sign-in")
    tokens = _tokens_from_body(body, provider)
    if not tokens["access_token"]:
        raise SaasError(
            f"{provider.capitalize()} did not return an access token. "
            "Try again.")
    try:
        profile = get_profile(provider, tokens["access_token"], client=http)
        tokens["provider_user_id"] = profile.get("provider_user_id") or ""
    except SaasError:
        tokens["provider_user_id"] = ""
    return tokens


def refresh_access_token(provider: str, config: SaasConfig,
                         refresh_token: str, client=None) -> dict:
    """Renew an access token via Meta's long-lived token exchange
    (grant_type=fb_exchange_token) — the refresh path for
    Facebook/Instagram user tokens. Returns the new tokens dict."""
    _require_provider(provider)
    if not refresh_token:
        raise SaasError(
            "There is no refresh token to renew with — reconnect "
            f"{provider.capitalize()} to continue posting.")
    http = _client_for(client)
    client_id, client_secret = _client_credentials(config, provider)
    body = _check(http.get(
        FB_TOKEN_URL,
        params={"grant_type": "fb_exchange_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "fb_exchange_token": refresh_token}),
        provider, "token refresh")
    tokens = _tokens_from_body(body, provider)
    if not tokens["access_token"]:
        raise SaasError(
            f"{provider.capitalize()} did not return a new access token. "
            "Reconnect to continue posting.")
    return tokens


def get_profile(provider: str, access_token: str, client=None,
                ig_user_id: str | None = None) -> dict:
    """Fetch the connected identity: {"provider_user_id", "name",
    "username"}. Facebook: GET /v24.0/me. Instagram:
    GET /v24.0/{ig-user-id} (the id comes from
    find_instagram_account); without one, /me is tried."""
    _require_provider(provider)
    http = _client_for(client)
    headers = {"Authorization": f"Bearer {access_token}"}
    if provider == "facebook":
        body = _check(http.get(
            FB_PROFILE_URL, params={"fields": "id,name"},
            headers=headers), provider, "profile read")
        return {"provider_user_id": body.get("id") or "",
                "name": body.get("name") or "",
                "username": ""}
    url = (f"{GRAPH_BASE}/{ig_user_id}" if ig_user_id else FB_PROFILE_URL)
    body = _check(http.get(
        url, params={"fields": "id,username"},
        headers=headers), provider, "profile read")
    return {"provider_user_id": body.get("id") or "",
            "name": "",
            "username": body.get("username") or ""}


# ---------------------------------------------------------------------------
# Facebook: long-lived user token + Page token
# ---------------------------------------------------------------------------

def exchange_long_lived_token(user_access_token: str, config: SaasConfig,
                              client=None) -> dict:
    """Exchange a short-lived Facebook user token for a long-lived one
    (documented exchange: GET /oauth/access_token with
    grant_type=fb_exchange_token). Page tokens obtained from a
    long-lived user token do not expire on a timer (Meta warns
    lifetimes can change without notice)."""
    _client_credentials(config, "facebook")
    http = _client_for(client)
    body = _check(http.get(
        FB_TOKEN_URL,
        params={"grant_type": "fb_exchange_token",
                "client_id": config.facebook_app_id,
                "client_secret": config.facebook_app_secret,
                "fb_exchange_token": user_access_token}),
        "facebook", "long-lived token exchange")
    return _tokens_from_body(body, "facebook")


def exchange_for_page_token(user_access_token: str, config: SaasConfig,
                            page_id: str | None = None,
                            client=None) -> dict:
    """Get a Page access token from a (long-lived) user token:
    GET /me/accounts lists the Pages the user manages; the token for
    `page_id` (or the first Page when none is named) is returned as
    {"page_id", "page_name", "access_token",
    "instagram_business_account_id"}. The connecting user must hold
    the CREATE_CONTENT task on the Page."""
    _client_credentials(config, "facebook")
    http = _client_for(client)
    body = _check(http.get(
        f"{GRAPH_BASE}/me/accounts",
        params={"fields": "id,name,access_token,instagram_business_account",
                "access_token": user_access_token}),
        "facebook", "Page list read")
    pages = body.get("data") or []
    chosen = None
    for page in pages:
        if page_id is None or page.get("id") == page_id:
            chosen = page
            break
    if chosen is None:
        raise SaasError(
            "No Facebook Page was found for this account — connect a "
            "Page you manage before publishing.")
    ig = chosen.get("instagram_business_account") or {}
    return {"page_id": chosen.get("id") or "",
            "page_name": chosen.get("name") or "",
            "access_token": chosen.get("access_token") or "",
            "instagram_business_account_id": ig.get("id") or ""}


def find_instagram_account(user_access_token: str, config: SaasConfig,
                           client=None) -> str:
    """The Instagram Business user id linked to the first managed
    Page, or "" when no Page has a linked Instagram account."""
    http = _client_for(client)
    try:
        page = exchange_for_page_token(
            user_access_token, config, client=http)
    except SaasError:
        return ""
    return page.get("instagram_business_account_id") or ""


def list_facebook_pages(user_access_token: str, config: SaasConfig,
                        client=None) -> list[dict]:
    """The Pages a user manages: [{id, name}] each, for the connect
    picker. Tokens are NOT returned here — the picker posts a page
    id back and the route exchanges for that Page's token. An empty
    list means the user manages no Page (or holds no task on one)."""
    _client_credentials(config, "facebook")
    http = _client_for(client)
    body = _check(http.get(
        f"{GRAPH_BASE}/me/accounts",
        params={"fields": "id,name",
                "access_token": user_access_token}),
        "facebook", "Page list read")
    return [{"id": p.get("id") or "", "name": p.get("name") or ""}
            for p in (body.get("data") or [])
            if p.get("id")]


def refresh_access_token_if_needed(store: SaasStore, config: SaasConfig,
                                   site_id: str, kind: str,
                                   client=None) -> str:
    """A usable access token for a connected Facebook/Instagram
    account. When the stored token's expiry is within 60 seconds,
    it is renewed first and the stored tokens are updated
    (existing fields — provider_user_id, page_id, ig_user_id —
    are kept, never wiped by a refresh response that omits them).
    Instagram renews with the long-lived exchange; Facebook Page
    tokens do not expire on a timer, so they are returned as
    stored. Tokens stored without an expiry are returned as
    stored. Raises SaasError when there is no token or a renewal
    fails — the caller tells the customer to reconnect; nothing
    is published on a stale token."""
    connection = store.get_connection(site_id, kind)
    detail = (connection or {}).get("_detail_raw") or {}
    token = detail.get("access_token") or ""
    if not token:
        raise SaasError(
            f"No {kind.capitalize()} token is stored — connect "
            f"{kind.capitalize()} again to continue posting.")
    expires_at = (detail.get("expires_at") or "").strip()
    needs_refresh = False
    if expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            needs_refresh = (expiry - datetime.now(timezone.utc)
                             < timedelta(seconds=60))
        except ValueError:
            needs_refresh = False
    if not needs_refresh:
        return token
    if kind == "instagram":
        tokens = exchange_long_lived_token(token, config, client=client)
    else:  # facebook: Page tokens do not expire on a timer.
        return token
    merged = dict(detail)
    merged["access_token"] = tokens.get("access_token") or token
    if tokens.get("refresh_token"):
        merged["refresh_token"] = tokens["refresh_token"]
    if tokens.get("expires_in"):
        merged["expires_in"] = tokens["expires_in"]
        try:
            merged["expires_at"] = (
                datetime.now(timezone.utc)
                + timedelta(seconds=int(tokens["expires_in"]))).isoformat()
        except (TypeError, ValueError):
            pass
    store.update_connection(site_id, kind, "connected",
                            (connection or {}).get("note") or
                            f"Connected — {kind} account linked.",
                            merged)
    return merged["access_token"]

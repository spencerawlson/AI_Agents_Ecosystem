"""Merchant store connections: Shopify OAuth (custom app install) and
WooCommerce API keys.

Axionory's merchant clients connect THEIR OWN stores so the agents
can read order/revenue data (return on ad spend, cost per customer,
conversion attribution — registry requirements 8 and 10). Spencer
holds no store; nothing here creates one, signs up, or spends money.

Shopify: the authorization-code grant for a custom app installed on
a merchant's store. Read-only scopes only — read_orders (order and
revenue data, the whole point), read_products (attribute revenue to
products), read_customers (customer counts for CAC). No write scope
is ever requested: no order creation, no refunds, no product edits,
no charges. The token is an offline token — it does not expire; it
stops working only when the merchant uninstalls the app or the
scopes change. Webhook verification (HMAC) is a follow-up, not
this module.

WooCommerce: the merchant generates a Read key in wp-admin
(WooCommerce -> Settings -> Advanced -> REST API) and pastes the
store URL + consumer key + consumer secret here. HTTPS only — the
secret must never travel over plain HTTP. No app review, no business
verification; the store owner holds the keys.

One pattern per provider, all HTTP through an injected client — an
httpx-like object with ``.get`` / ``.post`` (a real ``httpx.Client``
is built only when no client is passed), a urlopen-style callable,
or (Shopify verify/revoke) the adapter transport. NOTHING here makes
a live call at import time, and tests use fakes only.

Shopify facts (endpoints, HMAC rule, token shape) come from the
registry (providers.md §18) and Shopify's documented authorization
code grant; a live-browser re-check is still owed before the first
real merchant connects. The shop domain is customer-supplied, so it
is validated against an anchored ``*.myshopify.com`` pattern before
any URL is built from it — a forged shop must never receive the
token exchange (which carries the client secret). The callback is a
public endpoint, so its HMAC is verified before anything else
happens with its parameters.
"""

from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import json
import re
import urllib.parse
import urllib.request

from ventoryn.saas import SaasConfig, SaasError

# ---------------------------------------------------------------------------
# Shopify — constant provider URLs. Never user-supplied (SSRF guard:
# no fetched URL in this module ever comes from customer input except
# the shop host, which validate_shop_domain() anchors first).
# ---------------------------------------------------------------------------

SHOPIFY_SCOPES = "read_orders,read_products,read_customers"
SHOPIFY_SCOPES_DISPLAY = "read_orders read_products read_customers"
_SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
_HTTP_TIMEOUT_S = 30.0


def validate_shop_domain(domain: str) -> str:
    """A myshopify.com shop domain, lowercased and stripped — or a
    SaasError. The anchored pattern is the SSRF guard: anything else
    (another host, a subdomain trick, userinfo, a path) is refused
    before any URL is built from it."""
    clean = (domain or "").strip().lower()
    if not clean or not _SHOP_RE.match(clean):
        raise SaasError(
            "That does not look like a Shopify store address — it "
            "should end in .myshopify.com (for example "
            "your-store.myshopify.com).")
    return clean


def shopify_authorize_url(shop: str) -> str:
    return f"https://{shop}/admin/oauth/authorize"


def shopify_token_url(shop: str) -> str:
    return f"https://{shop}/admin/oauth/access_token"


def shopify_redirect_uri(base_url: str) -> str:
    """The callback Shopify must redirect to — registered verbatim
    in the Shopify app (operator step)."""
    return f"{base_url.rstrip('/')}/account/auth/shopify/callback"


def _shopify_credentials(config: SaasConfig) -> tuple[str, str]:
    """(client id, client secret) for Axionory's Shopify app. Raises
    SaasError when the operator app is not configured — the honest
    refusal, never a crash and never a fake."""
    if not config.shopify_configured:
        raise SaasError(
            "Shopify is not set up on this server yet — Axionory's own "
            "Shopify app registration is still needed. No store was "
            "connected and nothing was stored.")
    return config.shopify_client_id, config.shopify_client_secret


# ---------------------------------------------------------------------------
# HTTP plumbing — httpx-like client, or a urlopen-style callable.
# Mirrors ventoryn/social_oauth.py.
# ---------------------------------------------------------------------------

class _Response:
    """Minimal response shape shared by both client kinds."""

    def __init__(self, status_code: int, payload: dict | str) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        if isinstance(self._payload, dict):
            return self._payload
        return json.loads(self._payload)


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


def _check(response, what: str) -> dict:
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
            err = body.get("error") or body.get("errors") or ""
            if isinstance(err, dict):
                detail = err.get("message") or ""
            elif isinstance(err, str):
                detail = err
        raise SaasError(
            f"Shopify did not accept the {what}. Try again. "
            f"{detail}".strip())
    return body if isinstance(body, dict) else {}


# ---------------------------------------------------------------------------
# Shopify: authorize URL / HMAC / code exchange
# ---------------------------------------------------------------------------

def build_authorize_url(shop: str, config: SaasConfig,
                        redirect_uri: str, state: str) -> str:
    """The Shopify authorization URL for a merchant install.

    The caller stores the ``state`` server-side with
    ``SaasStore.create_connection_oauth_state`` and checks it on the
    callback; the state never goes in a cookie or a URL the browser
    keeps. No PKCE: Shopify's web-app authorization code grant does
    not use it. The shop domain is validated here — a forged shop
    never reaches the authorize step."""
    shop = validate_shop_domain(shop)
    client_id, _secret = _shopify_credentials(config)
    params = {
        "client_id": client_id,
        "scope": SHOPIFY_SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{shopify_authorize_url(shop)}?{urllib.parse.urlencode(params)}"


def _hmac_escape(value: str) -> str:
    """Shopify's HMAC message escaping: % first, then & and =."""
    return (value.replace("%", "%25").replace("&", "%26")
            .replace("=", "%3D"))


def _hmac_for(params: dict, secret: str) -> str:
    """Shopify's HMAC over every parameter except ``hmac`` itself:
    escaped, sorted by key, joined as ``k=v`` with ``&``."""
    message = "&".join(
        f"{_hmac_escape(str(k))}={_hmac_escape(str(v))}"
        for k, v in sorted(params.items()) if k != "hmac")
    return _hmac.new(secret.encode("utf-8"),
                     message.encode("utf-8"),
                     hashlib.sha256).hexdigest()


def validate_callback_hmac(params: dict, client_secret: str) -> bool:
    """True when Shopify's HMAC over the callback parameters checks
    out. The message is every parameter except ``hmac``, escaped,
    sorted by key, joined as ``k=v`` with ``&``. A missing or
    mismatched HMAC is False — the callback is refused outright."""
    provided = params.get("hmac") or ""
    if not provided or not client_secret:
        return False
    return _hmac.compare_digest(_hmac_for(params, client_secret),
                                provided)


def exchange_code(shop: str, config: SaasConfig, code: str,
                  client=None) -> dict:
    """Exchange an authorization code for an offline access token.
    Returns {"access_token", "scope", "shop"}. Offline tokens do not
    expire and carry no refresh token — the token stops working only
    when the merchant uninstalls the app or the scopes change."""
    shop = validate_shop_domain(shop)
    http = _client_for(client)
    client_id, client_secret = _shopify_credentials(config)
    body = _check(http.post(
        shopify_token_url(shop),
        data={"client_id": client_id, "client_secret": client_secret,
              "code": code}),
        "sign-in")
    token = body.get("access_token") or ""
    if not token:
        raise SaasError(
            "Shopify did not return an access token. Try again.")
    return {"access_token": token,
            "scope": body.get("scope") or "",
            "shop": shop}


# ---------------------------------------------------------------------------
# Shopify: verify + revoke (through the GraphQL adapter / REST)
# ---------------------------------------------------------------------------

# The scopes this connection needs (read-only). The adapter's own
# SCOPES list is the owner-operated store pipeline's — it does not
# include read_customers — so the missing-scope check here is
# computed from the granted handles directly, not from verify().
_SHOPIFY_NEEDED_SCOPES = ("read_orders", "read_products",
                          "read_customers")
_VERIFY_Q = """
query {
  shop { name myshopifyDomain currencyCode }
  currentAppInstallation { accessScopes { handle } }
}"""


def verify_shopify_connection(shop: str, access_token: str,
                              transport=None) -> dict:
    """Read-only probe of a stored Shopify token: shop identity plus
    any of the read scopes that are missing. Returns
    {"shop": {...}, "missing_scopes": [...]}. Raises SaasError when
    the token is rejected — the caller does not mark the connection
    connected on a failed probe."""
    from core.shopify_adapter import ShopifyCommerceAdapter, ShopifyError

    shop = validate_shop_domain(shop)
    if not access_token:
        raise SaasError("There is no Shopify token to check — reconnect "
                        "the store to continue.")
    adapter = ShopifyCommerceAdapter(shop, access_token=access_token,
                                     transport=transport)
    try:
        data = adapter.graphql(_VERIFY_Q)
    except ShopifyError as exc:
        raise SaasError(
            f"Shopify did not accept this store's token ({exc}). "
            "Reconnect the store to continue.") from exc
    granted = {s["handle"] for s in
               data["currentAppInstallation"]["accessScopes"]}
    # write_X implies read_X in Shopify's scope model.
    have = granted | {h.replace("write_", "read_", 1) for h in granted}
    missing = [s for s in _SHOPIFY_NEEDED_SCOPES if s not in have]
    return {"shop": data["shop"], "missing_scopes": missing}


def revoke_shopify_token(shop: str, access_token: str,
                         api_version: str = "2026-07",
                         transport=None, urlopen=None) -> bool:
    """Revoke a Shopify access token server-side: DELETE
    /admin/api/{version}/api-permissions/current.json with the token
    in the X-Shopify-Access-Token header. True when Shopify
    confirmed (2xx); False otherwise — the caller still deletes the
    local copy and tells the merchant to remove the app in Shopify
    admin to be sure. `transport` is the adapter-style callable
    (tests); `urlopen` is a urlopen-style opener, wrapped the same
    way when no transport is given."""
    from core.shopify_adapter import _urllib_transport

    shop = validate_shop_domain(shop)
    if not access_token:
        return True
    if transport is None and urlopen is not None:
        import urllib.error

        def transport(method, url, headers, body, _opener=urlopen):
            req = urllib.request.Request(url, data=body, method=method)
            for k, v in headers.items():
                req.add_header(k, v)
            try:
                with _opener(req) as resp:
                    return resp.status, resp.read().decode()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode(errors="replace")
    transport = transport or _urllib_transport
    status, _text = transport(
        "DELETE",
        f"https://{shop}/admin/api/{api_version}/api-permissions/current.json",
        {"X-Shopify-Access-Token": access_token}, None)
    return 200 <= status < 300


# ---------------------------------------------------------------------------
# WooCommerce: URL validation + connection test
# ---------------------------------------------------------------------------

def validate_store_url(url: str) -> str:
    """A WooCommerce store URL: HTTPS only, host present, no
    userinfo, trailing slash stripped. Plain HTTP is refused —
    the consumer secret must never travel over it (WooCommerce
    over HTTP needs OAuth 1.0a signing, which this flow does not
    do)."""
    clean = (url or "").strip()
    if not clean:
        raise SaasError("Enter your store's address, for example "
                        "https://your-store.com.")
    parsed = urllib.parse.urlparse(clean)
    if parsed.scheme != "https":
        raise SaasError(
            "Your store address must start with https:// — the API "
            "key must never travel over plain HTTP.")
    if not parsed.netloc or parsed.username or parsed.password:
        raise SaasError(
            "That store address does not look right — use the store's "
            "https:// address with nothing before it.")
    return f"https://{parsed.netloc}{parsed.path.rstrip('/')}"


def test_woocommerce_connection(store_url: str, consumer_key: str,
                                consumer_secret: str,
                                client=None) -> dict:
    """Read-only probe of WooCommerce credentials: GET
    /wp-json/wc/v3/orders?per_page=1 with HTTP Basic Auth (key as
    username, secret as password). Returns {"store_url": ...,
    "orders": <count in the first page>}. Raises SaasError on a
    wrong key, an unreachable store, or a non-JSON answer — the
    caller stores nothing on a failed probe."""
    store_url = validate_store_url(store_url)
    if not consumer_key or not consumer_secret:
        raise SaasError(
            "Enter both the consumer key and the consumer secret from "
            "WooCommerce → Settings → Advanced → REST API.")
    http = _client_for(client)
    auth = base64.b64encode(
        f"{consumer_key}:{consumer_secret}".encode("utf-8")).decode("ascii")
    url = f"{store_url}/wp-json/wc/v3/orders?per_page=1"
    try:
        response = http.get(url, headers={"Authorization": f"Basic {auth}"})
    except Exception as exc:  # noqa: BLE001 - network failure is a refusal
        raise SaasError(
            f"Could not reach your store at {store_url} ({exc}). "
            "Check the address and try again.") from exc
    status = getattr(response, "status_code", 0)
    if status in (401, 403):
        raise SaasError(
            "WooCommerce did not accept that key and secret — check "
            "them in WooCommerce → Settings → Advanced → REST API and "
            "try again.")
    try:
        body = response.json()
    except Exception:
        body = None
    if not (200 <= status < 300) or not isinstance(body, list):
        raise SaasError(
            f"Your store did not answer like a WooCommerce store "
            f"(HTTP {status}). Check the address — it must be the "
            "store's own https:// address.")
    return {"store_url": store_url, "orders": len(body)}

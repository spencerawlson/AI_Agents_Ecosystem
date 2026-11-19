"""Shopify + WooCommerce merchant-client connections.

No live network anywhere here: every HTTP call goes through a fake
client (Shopify token exchange, WooCommerce probe) or a fake
transport (Shopify verify / revoke). No credential value appears in
this file — every token, key and secret is an obvious fake
("shpat_fake-…", "ck_fake-…", "cs_fake-…").
"""

import hashlib
import hmac as _hmac
import json

import pytest

from ventoryn import commerce_oauth as co
from ventoryn.saas import (
    CONNECTION_CONSENT,
    CONNECTION_REVOKE_NOTE,
    CONNECTION_SCOPES,
    SaasConfig,
    SaasError,
    SaasStore,
)

SHOP = "acme-store.myshopify.com"
FAKE_TOKEN = "shpat_fake-token-123"
FAKE_KEY = "ck_fake-key-123"
FAKE_SECRET = "cs_fake-secret-123"
CLIENT_SECRET = "fake-client-secret"

CONFIG = SaasConfig(shopify_client_id="fake-client-id",
                    shopify_client_secret=CLIENT_SECRET)


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, (dict, list)):
            return self._payload
        return json.loads(self._payload)


class FakeHttp:
    """httpx-like fake: one canned response per (method, path)."""

    def __init__(self):
        self.calls = []
        self.responses = {}

    def on(self, method, path, response):
        self.responses[(method, path)] = response
        return self

    def _call(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        path = url.split("://", 1)[-1].split("/", 1)[-1].split("?", 1)[0]
        response = self.responses.get((method, "/" + path))
        if response is None:
            raise AssertionError(f"unexpected {method} {url}")
        return response

    def get(self, url, headers=None, params=None):
        return self._call("GET", url, headers=headers, params=params)

    def post(self, url, data=None, headers=None):
        return self._call("POST", url, data=data, headers=headers)


def fake_shopify_graphql(scopes=("read_orders", "read_products",
                                 "read_customers")):
    """Fake adapter transport for the scopes query verify() runs."""

    def transport(method, url, headers, body):
        assert method == "POST"
        assert url == (f"https://{SHOP}/admin/api/2026-07/graphql.json")
        payload = json.loads(body)
        assert "currentAppInstallation" in payload["query"]
        data = {
            "shop": {"name": "Acme Store",
                     "myshopifyDomain": SHOP,
                     "currencyCode": "USD"},
            "currentAppInstallation": {
                "accessScopes": [{"handle": s} for s in scopes]},
        }
        return 200, json.dumps({"data": data})

    return transport


def _hmac_for(params, secret=CLIENT_SECRET):
    message = "&".join(
        f"{k}={v}" for k, v in sorted(params.items()) if k != "hmac")
    return _hmac.new(secret.encode(), message.encode(),
                     hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Shopify: shop domain validation
# ---------------------------------------------------------------------------

def test_validate_shop_domain_accepts_myshopify():
    assert co.validate_shop_domain("acme-store.myshopify.com") == SHOP
    assert co.validate_shop_domain("  ACME-STORE.MYSHOPIFY.COM ") == SHOP


@pytest.mark.parametrize("bad", [
    "evil.com",
    "acme-store.myshopify.com.evil.com",
    "acme-store.myshopify.com/path",
    "https://acme-store.myshopify.com",
    "user@acme-store.myshopify.com",
    "acme store.myshopify.com",
    "",
    "myshopify.com",
    "-bad.myshopify.com",
])
def test_validate_shop_domain_refuses_forged_hosts(bad):
    with pytest.raises(SaasError):
        co.validate_shop_domain(bad)


# ---------------------------------------------------------------------------
# Shopify: authorize URL
# ---------------------------------------------------------------------------

def test_authorize_url_read_only_scopes_only():
    url = co.build_authorize_url(SHOP, CONFIG,
                                 "https://www.axionory.com/account/auth/shopify/callback",
                                 "state-123")
    assert url.startswith(f"https://{SHOP}/admin/oauth/authorize?")
    assert "client_id=fake-client-id" in url
    assert "scope=read_orders%2Cread_products%2Cread_customers" in url
    assert "state=state-123" in url
    assert "write_" not in url


def test_authorize_url_refuses_unconfigured_app():
    with pytest.raises(SaasError):
        co.build_authorize_url(SHOP, SaasConfig(),
                               "https://example.com/callback", "state-1")


# ---------------------------------------------------------------------------
# Shopify: callback HMAC
# ---------------------------------------------------------------------------

def test_callback_hmac_valid_tampered_missing():
    params = {"code": "auth-code", "shop": SHOP,
              "state": "state-123", "timestamp": "1700000000"}
    params["hmac"] = _hmac_for(params)
    assert co.validate_callback_hmac(params, CLIENT_SECRET) is True
    tampered = dict(params, shop="other-store.myshopify.com")
    assert co.validate_callback_hmac(tampered, CLIENT_SECRET) is False
    assert co.validate_callback_hmac(params, "wrong-secret") is False
    assert co.validate_callback_hmac(
        {k: v for k, v in params.items() if k != "hmac"},
        CLIENT_SECRET) is False


# ---------------------------------------------------------------------------
# Shopify: code exchange
# ---------------------------------------------------------------------------

def test_exchange_code_success_and_failures():
    http = FakeHttp().on(
        "POST", "/admin/oauth/access_token",
        FakeResponse(200, {"access_token": FAKE_TOKEN,
                           "scope": co.SHOPIFY_SCOPES}))
    out = co.exchange_code(SHOP, CONFIG, "auth-code", client=http)
    assert out["access_token"] == FAKE_TOKEN
    assert out["scope"] == co.SHOPIFY_SCOPES
    assert out["shop"] == SHOP
    method, url, kwargs = http.calls[0]
    assert method == "POST"
    assert url == f"https://{SHOP}/admin/oauth/access_token"
    assert kwargs["data"]["client_id"] == "fake-client-id"
    assert kwargs["data"]["client_secret"] == CLIENT_SECRET
    assert kwargs["data"]["code"] == "auth-code"

    http = FakeHttp().on(
        "POST", "/admin/oauth/access_token",
        FakeResponse(400, {"error": "invalid_grant",
                           "error_description": "The authorization code "
                           "was not found or was already used."}))
    with pytest.raises(SaasError):
        co.exchange_code(SHOP, CONFIG, "used-code", client=http)

    http = FakeHttp().on(
        "POST", "/admin/oauth/access_token", FakeResponse(200, {}))
    with pytest.raises(SaasError):
        co.exchange_code(SHOP, CONFIG, "auth-code", client=http)

    with pytest.raises(SaasError):
        co.exchange_code(SHOP, SaasConfig(), "auth-code", client=http)


# ---------------------------------------------------------------------------
# Shopify: verify + revoke
# ---------------------------------------------------------------------------

def test_verify_reports_missing_scopes():
    out = co.verify_shopify_connection(
        SHOP, FAKE_TOKEN,
        transport=fake_shopify_graphql(("read_orders", "read_products")))
    assert out["shop"]["name"] == "Acme Store"
    assert out["shop"]["currencyCode"] == "USD"
    assert out["missing_scopes"] == ["read_customers"]

    out = co.verify_shopify_connection(
        SHOP, FAKE_TOKEN, transport=fake_shopify_graphql())
    assert out["missing_scopes"] == []

    def unauth(method, url, headers, body):
        return 401, json.dumps({"errors": "Unauthorized"})

    with pytest.raises(SaasError):
        co.verify_shopify_connection(SHOP, FAKE_TOKEN, transport=unauth)


def test_revoke_shopify_token():
    seen = {}

    def ok(method, url, headers, body):
        seen["method"] = method
        seen["url"] = url
        seen["headers"] = headers
        return 200, "{}"

    assert co.revoke_shopify_token(SHOP, FAKE_TOKEN, transport=ok) is True
    assert seen["method"] == "DELETE"
    assert seen["url"] == (f"https://{SHOP}/admin/api/2026-07/"
                           "api-permissions/current.json")
    assert seen["headers"]["X-Shopify-Access-Token"] == FAKE_TOKEN

    def failing(method, url, headers, body):
        return 500, "{}"

    assert co.revoke_shopify_token(SHOP, FAKE_TOKEN,
                                   transport=failing) is False
    assert co.revoke_shopify_token(SHOP, "", transport=ok) is True


# ---------------------------------------------------------------------------
# WooCommerce: URL validation + connection test
# ---------------------------------------------------------------------------

def test_validate_store_url():
    assert co.validate_store_url("https://shop.example.com") == \
        "https://shop.example.com"
    assert co.validate_store_url("https://shop.example.com/") == \
        "https://shop.example.com"
    assert co.validate_store_url("https://shop.example.com/store/") == \
        "https://shop.example.com/store"


@pytest.mark.parametrize("bad", [
    "http://shop.example.com",
    "shop.example.com",
    "https://user:pass@shop.example.com",
    "https://",
    "",
    "ftp://shop.example.com",
])
def test_validate_store_url_refuses(bad):
    with pytest.raises(SaasError):
        co.validate_store_url(bad)


def test_woocommerce_connection_test():
    import base64

    http = FakeHttp().on(
        "GET", "/wp-json/wc/v3/orders",
        FakeResponse(200, [{"id": 1}, {"id": 2}]))
    out = co.test_woocommerce_connection("https://shop.example.com",
                                         FAKE_KEY, FAKE_SECRET,
                                         client=http)
    assert out["store_url"] == "https://shop.example.com"
    assert out["orders"] == 2
    method, url, kwargs = http.calls[0]
    assert method == "GET"
    assert url == ("https://shop.example.com/wp-json/wc/v3/orders"
                   "?per_page=1")
    expected = base64.b64encode(
        f"{FAKE_KEY}:{FAKE_SECRET}".encode()).decode()
    assert kwargs["headers"]["Authorization"] == f"Basic {expected}"

    http = FakeHttp().on("GET", "/wp-json/wc/v3/orders",
                         FakeResponse(401, {"code": "woocommerce_rest_cannot_view",
                                            "message": "Sorry, you cannot list resources."}))
    with pytest.raises(SaasError):
        co.test_woocommerce_connection("https://shop.example.com",
                                       FAKE_KEY, "wrong-secret",
                                       client=http)

    http = FakeHttp().on("GET", "/wp-json/wc/v3/orders",
                         FakeResponse(200, {"not": "a list"}))
    with pytest.raises(SaasError):
        co.test_woocommerce_connection("https://shop.example.com",
                                       FAKE_KEY, FAKE_SECRET,
                                       client=http)

    with pytest.raises(SaasError):
        co.test_woocommerce_connection("http://shop.example.com",
                                       FAKE_KEY, FAKE_SECRET,
                                       client=http)
    with pytest.raises(SaasError):
        co.test_woocommerce_connection("https://shop.example.com",
                                       "", FAKE_SECRET, client=http)


# ---------------------------------------------------------------------------
# Config + storage: encrypted at rest, stripped in listings, wiped on
# disconnect; consent/scope strings stay read-only.
# ---------------------------------------------------------------------------

def test_shopify_config_and_registration():
    assert CONFIG.shopify_configured is True
    assert CONFIG.app_registered("shopify") is True
    assert SaasConfig().shopify_configured is False
    assert SaasConfig().app_registered("shopify") is False
    env_config = SaasConfig.from_env({
        "VENTORYN_SHOPIFY_CLIENT_ID": "env-id",
        "VENTORYN_SHOPIFY_CLIENT_SECRET": "env-secret"})
    assert env_config.shopify_configured is True


def test_encrypted_storage_round_trip_and_stripping():
    store = SaasStore(token_key="test-token-key-phase2")
    store.ensure_connections("site-1")
    store.update_connection(
        "site-1", "shopify", "connected", "Connected",
        {"shop_domain": SHOP, "access_token": FAKE_TOKEN,
         "scope": co.SHOPIFY_SCOPES, "shop_name": "Acme Store",
         "currency_code": "USD"})
    store.update_connection(
        "site-1", "woocommerce", "connected", "Connected",
        {"store_url": "https://shop.example.com",
         "consumer_key": FAKE_KEY, "consumer_secret": FAKE_SECRET})
    # Raw rows hold ciphertext, never the plaintext credential.
    rows = store._conn.execute(
        "SELECT detail FROM saas_connections WHERE site_id = 'site-1'"
    ).fetchall()
    raw = " ".join(r[0] for r in rows)
    assert FAKE_TOKEN not in raw
    assert FAKE_KEY not in raw
    assert FAKE_SECRET not in raw
    assert "v1." in raw
    # get_connection decrypts for the owner.
    shopify = store.get_connection("site-1", "shopify")
    assert shopify["_detail_raw"]["access_token"] == FAKE_TOKEN
    woo = store.get_connection("site-1", "woocommerce")
    assert woo["_detail_raw"]["consumer_secret"] == FAKE_SECRET
    # get_connections strips every secret from every row.
    for conn in store.get_connections("site-1"):
        assert "_detail_raw" not in conn
        assert FAKE_TOKEN not in str(conn)
        assert FAKE_SECRET not in str(conn)
    # Disconnect wipes the stored detail.
    store.disconnect_connection("site-1", "shopify")
    after = store.get_connection("site-1", "shopify")
    assert after["status"] == "not_connected"
    assert after.get("_detail_raw") in (None, {})
    store.disconnect_connection("site-1", "woocommerce")
    after = store.get_connection("site-1", "woocommerce")
    assert after["status"] == "not_connected"
    assert after.get("_detail_raw") in (None, {})


def test_scope_and_consent_strings_are_read_only():
    for kind in ("shopify", "woocommerce"):
        assert "write" not in CONNECTION_SCOPES[kind].lower()
        consent = CONNECTION_CONSENT[kind].lower()
        assert "read" in consent
        assert "cannot create" in consent
        assert "refund" in consent
        assert "charge" in consent
    assert "shopify" in CONNECTION_REVOKE_NOTE
    assert "woocommerce" in CONNECTION_REVOKE_NOTE

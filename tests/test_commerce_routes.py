"""Shopify + WooCommerce connections through the real routes.

No live provider calls anywhere: the Shopify token exchange and
verify, and the WooCommerce probe, are fakes patched in. No
credential value appears in this file — every token, key and
secret is an obvious fake ("shpat_fake-…", "ck_fake-…",
"cs_fake-…").
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from core.audit import AuditLog
from core.ledger import Ledger
from core.registry import BusinessRegistry
from orchestrator.approvals import ApprovalGate
from ventoryn.app import create_app
from ventoryn.saas import SaasError
import ventoryn.saas_routes as saas_routes
from ventoryn.commerce_oauth import _hmac_for as _shopify_hmac

from _signin import (
    fresh_totp_code,
    operator_store_and_config,
    sign_in,
)

CUSTOMER = ("ada@example.com", "google-sub-ada", "Ada")
SHOP = "acme-store.myshopify.com"
STORE_URL = "https://shop.example.com"
CLIENT_SECRET = "fake-shopify-secret"


@pytest.fixture(autouse=True)
def _token_key(monkeypatch):
    monkeypatch.setenv("VENTORYN_TOKEN_KEY", "test-token-key-commerce")


def _app_with(shopify=False):
    """An app whose config has the Shopify app registered when
    asked. Nothing is registered by default — the honest
    not-connected state is the default under test, like
    production."""
    store, config = operator_store_and_config()
    if shopify:
        config.shopify_client_id = "fake-shopify-id"
        config.shopify_client_secret = CLIENT_SECRET
    app = create_app(registry=BusinessRegistry(),
                     ledger=Ledger(),
                     approvals=ApprovalGate(),
                     audit=AuditLog(),
                     saas_store=store, saas_config=config)
    return app, store, config


def _customer_client(app, store):
    email, sub, name = CUSTOMER
    client = TestClient(app)
    account = sign_in(client, store, email=email, sub=sub, name=name)
    return client, account


def _seed_site(store, account, url="https://ada.example.com"):
    return store.create_site(account["id"], url)


def _valid_hmac(code, shop, state, timestamp, host):
    params = {"code": code, "shop": shop, "state": state,
              "timestamp": timestamp, "host": host}
    return _shopify_hmac(params, CLIENT_SECRET)


# ---------------------------------------------------------------------------
# Site page renders both rows
# ---------------------------------------------------------------------------

def test_site_page_shows_shopify_and_woocommerce_rows():
    app, store, _config = _app_with()
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    r = client.get(f"/websites/sites/{site['id']}")
    assert r.status_code == 200, r.text
    assert "Shopify" in r.text
    assert "WooCommerce" in r.text
    assert "read_orders read_products read_customers" in r.text
    assert "Connect WooCommerce" in r.text
    assert "Shopify is not set up on this server yet" in r.text


def test_site_page_shows_shopify_form_when_configured():
    app, store, _config = _app_with(shopify=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    r = client.get(f"/websites/sites/{site['id']}")
    assert r.status_code == 200, r.text
    assert "Connect with Shopify" in r.text
    assert "your-store.myshopify.com" in r.text


# ---------------------------------------------------------------------------
# Shopify start
# ---------------------------------------------------------------------------

def test_shopify_start_unconfigured_refuses():
    app, store, _config = _app_with()
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    store.ensure_connections(site["id"])
    r = client.post(
        f"/websites/sites/{site['id']}/connections/shopify/start",
        data={"shop_domain": SHOP}, follow_redirects=False)
    assert r.status_code == 400, r.text
    assert "Shopify is not available yet" in r.text
    conn = store.get_connection(site["id"], "shopify")
    assert conn["status"] == "not_connected"


def test_shopify_start_configured_redirects_to_authorize():
    app, store, _config = _app_with(shopify=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    r = client.post(
        f"/websites/sites/{site['id']}/connections/shopify/start",
        data={"shop_domain": SHOP}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"].startswith(
        f"https://{SHOP}/admin/oauth/authorize?")
    assert "client_id=fake-shopify-id" in r.headers["location"]
    assert "state=" in r.headers["location"]


def test_shopify_start_refuses_bad_domain():
    app, store, _config = _app_with(shopify=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    r = client.post(
        f"/websites/sites/{site['id']}/connections/shopify/start",
        data={"shop_domain": "evil.com"}, follow_redirects=False)
    assert r.status_code == 400, r.text
    assert ".myshopify.com" in r.text


def test_shopify_start_wrong_account_404():
    app, store, _config = _app_with(shopify=True)
    client, account = _customer_client(app, store)
    other = store.create_account("google-sub-ben", "ben@example.com",
                                 "Ben")
    site = _seed_site(store, other, url="https://ben.example.com")
    r = client.post(
        f"/websites/sites/{site['id']}/connections/shopify/start",
        data={"shop_domain": SHOP}, follow_redirects=False)
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Shopify callback
# ---------------------------------------------------------------------------

def _fake_exchange(shop, config, code):
    assert shop == SHOP
    assert code == "auth-code"
    return {"access_token": "shpat_fake-token-123",
            "scope": "read_orders,read_products,read_customers",
            "shop": SHOP}


def _fake_verify(shop, token):
    assert shop == SHOP
    assert token == "shpat_fake-token-123"
    return {"shop": {"name": "Acme Store",
                     "myshopifyDomain": SHOP,
                     "currencyCode": "USD"},
            "missing_scopes": []}


def test_shopify_callback_connects(monkeypatch):
    app, store, _config = _app_with(shopify=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    store.ensure_connections(site["id"])
    state = store.create_connection_oauth_state(site["id"], "shopify", "")
    monkeypatch.setattr(saas_routes, "exchange_shopify_code",
                        _fake_exchange)
    monkeypatch.setattr(saas_routes, "verify_shopify_connection",
                        _fake_verify)
    hmac = _valid_hmac("auth-code", SHOP, state, "1700000000",
                       "acme-admin.shopify.com")
    r = client.get("/account/auth/shopify/callback",
                   params={"code": "auth-code", "shop": SHOP,
                           "state": state, "timestamp": "1700000000",
                           "host": "acme-admin.shopify.com",
                           "hmac": hmac},
                   follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == f"/websites/sites/{site['id']}"
    conn = store.get_connection(site["id"], "shopify")
    assert conn["status"] == "connected"
    assert "Acme Store" in conn["note"]
    raw = conn["_detail_raw"]
    assert raw["access_token"] == "shpat_fake-token-123"
    assert raw["shop_domain"] == SHOP
    assert raw["shop_name"] == "Acme Store"
    assert raw["currency_code"] == "USD"


def test_shopify_callback_bad_state_refuses(monkeypatch):
    app, store, _config = _app_with(shopify=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    store.ensure_connections(site["id"])
    monkeypatch.setattr(saas_routes, "exchange_shopify_code",
                        _fake_exchange)
    r = client.get("/account/auth/shopify/callback",
                   params={"code": "auth-code", "shop": SHOP,
                           "state": "no-such-state",
                           "timestamp": "1700000000",
                           "hmac": "deadbeef"},
                   follow_redirects=False)
    assert r.status_code == 400, r.text
    conn = store.get_connection(site["id"], "shopify")
    assert conn["status"] == "not_connected"


def test_shopify_callback_bad_hmac_refuses(monkeypatch):
    app, store, _config = _app_with(shopify=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    store.ensure_connections(site["id"])
    state = store.create_connection_oauth_state(site["id"], "shopify", "")
    monkeypatch.setattr(saas_routes, "exchange_shopify_code",
                        _fake_exchange)
    r = client.get("/account/auth/shopify/callback",
                   params={"code": "auth-code", "shop": SHOP,
                           "state": state, "timestamp": "1700000000",
                           "host": "acme-admin.shopify.com",
                           "hmac": "tampered-hmac"},
                   follow_redirects=False)
    assert r.status_code == 400, r.text
    assert "did not check out" in r.text
    conn = store.get_connection(site["id"], "shopify")
    assert conn["status"] == "not_connected"


def test_shopify_callback_failed_exchange_stores_nothing(monkeypatch):
    app, store, _config = _app_with(shopify=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    store.ensure_connections(site["id"])
    state = store.create_connection_oauth_state(site["id"], "shopify", "")

    def failing_exchange(shop, config, code):
        raise SaasError("Shopify did not accept the sign-in.")

    monkeypatch.setattr(saas_routes, "exchange_shopify_code",
                        failing_exchange)
    hmac = _valid_hmac("auth-code", SHOP, state, "1700000000",
                       "acme-admin.shopify.com")
    r = client.get("/account/auth/shopify/callback",
                   params={"code": "auth-code", "shop": SHOP,
                           "state": state, "timestamp": "1700000000",
                           "host": "acme-admin.shopify.com",
                           "hmac": hmac},
                   follow_redirects=False)
    assert r.status_code == 400, r.text
    conn = store.get_connection(site["id"], "shopify")
    assert conn["status"] == "not_connected"
    assert conn.get("_detail_raw") in (None, {})


# ---------------------------------------------------------------------------
# WooCommerce connect
# ---------------------------------------------------------------------------

def test_woocommerce_connect_success(monkeypatch):
    app, store, _config = _app_with()
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    store.ensure_connections(site["id"])

    def fake_test(store_url, key, secret, client=None):
        assert store_url == STORE_URL
        assert key == "ck_fake-key-123"
        assert secret == "cs_fake-secret-123"
        return {"store_url": STORE_URL, "orders": 2}

    monkeypatch.setattr(saas_routes, "test_woocommerce_connection",
                        fake_test)
    r = client.post(
        f"/websites/sites/{site['id']}/connections/woocommerce",
        data={"store_url": STORE_URL, "consumer_key": "ck_fake-key-123",
              "consumer_secret": "cs_fake-secret-123"},
        follow_redirects=False)
    assert r.status_code == 303, r.text
    conn = store.get_connection(site["id"], "woocommerce")
    assert conn["status"] == "connected"
    assert STORE_URL in conn["note"]
    raw = conn["_detail_raw"]
    assert raw["store_url"] == STORE_URL
    assert raw["consumer_key"] == "ck_fake-key-123"
    assert raw["consumer_secret"] == "cs_fake-secret-123"
    # The site page shows the store, never the key or secret.
    page = client.get(f"/websites/sites/{site['id']}")
    assert STORE_URL in page.text
    assert "ck_fake-key-123" not in page.text
    assert "cs_fake-secret-123" not in page.text


def test_woocommerce_connect_http_url_refuses_and_stores_nothing(monkeypatch):
    app, store, _config = _app_with()
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    store.ensure_connections(site["id"])

    def real_test(store_url, key, secret, client=None):
        from ventoryn.commerce_oauth import test_woocommerce_connection
        return test_woocommerce_connection(store_url, key, secret,
                                           client=client)

    monkeypatch.setattr(saas_routes, "test_woocommerce_connection",
                        real_test)
    r = client.post(
        f"/websites/sites/{site['id']}/connections/woocommerce",
        data={"store_url": "http://shop.example.com",
              "consumer_key": "ck_fake-key-123",
              "consumer_secret": "cs_fake-secret-123"},
        follow_redirects=False)
    assert r.status_code == 400, r.text
    assert "https://" in r.text
    conn = store.get_connection(site["id"], "woocommerce")
    assert conn["status"] == "not_connected"
    assert conn.get("_detail_raw") in (None, {})


def test_woocommerce_connect_failed_test_stores_nothing(monkeypatch):
    app, store, _config = _app_with()
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    store.ensure_connections(site["id"])

    def failing_test(store_url, key, secret, client=None):
        raise SaasError("WooCommerce did not accept that key and secret.")

    monkeypatch.setattr(saas_routes, "test_woocommerce_connection",
                        failing_test)
    r = client.post(
        f"/websites/sites/{site['id']}/connections/woocommerce",
        data={"store_url": STORE_URL, "consumer_key": "ck_fake-key-123",
              "consumer_secret": "cs_fake-secret-123"},
        follow_redirects=False)
    assert r.status_code == 400, r.text
    conn = store.get_connection(site["id"], "woocommerce")
    assert conn["status"] == "not_connected"
    assert conn.get("_detail_raw") in (None, {})


# ---------------------------------------------------------------------------
# Integrations grid + per-app windows
# ---------------------------------------------------------------------------

def test_integrations_grid_lists_both_apps():
    app, store, _config = _app_with()
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    html = client.get("/integrations").text
    assert "href='/integrations/shopify'" in html
    assert "href='/integrations/woocommerce'" in html
    assert "Read orders and revenue" in html


def test_integrations_shopify_window_unconfigured():
    app, store, _config = _app_with()
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    html = client.get(f"/integrations/shopify?site={site['id']}").text
    assert "has not set up Axionory's Shopify app yet" in html
    assert "Connect with Shopify" not in html


def test_integrations_shopify_window_configured():
    app, store, _config = _app_with(shopify=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    html = client.get(f"/integrations/shopify?site={site['id']}").text
    assert "Connect with Shopify" in html
    assert "your-store.myshopify.com" in html


def test_integrations_woocommerce_window():
    app, store, _config = _app_with()
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    html = client.get(f"/integrations/woocommerce?site={site['id']}").text
    assert "Connect WooCommerce" in html
    assert "Consumer key" in html
    assert "Consumer secret" in html


# ---------------------------------------------------------------------------
# Disconnect revokes and wipes
# ---------------------------------------------------------------------------

def test_disconnect_revokes_and_wipes(monkeypatch):
    app, store, _config = _app_with(shopify=True)
    client, account = _customer_client(app, store)
    site = _seed_site(store, account)
    store.ensure_connections(site["id"])
    store.update_connection(
        site["id"], "shopify", "connected", "Connected",
        {"shop_domain": SHOP, "access_token": "shpat_fake-token-123",
         "scope": "read_orders,read_products,read_customers",
         "shop_name": "Acme Store", "currency_code": "USD"})
    store.update_connection(
        site["id"], "woocommerce", "connected", "Connected",
        {"store_url": STORE_URL, "consumer_key": "ck_fake-key-123",
         "consumer_secret": "cs_fake-secret-123"})
    seen = {}

    def fake_revoke(kind, raw, client_id="", client_secret="",
                    urlopen=None):
        seen[kind] = raw
        return True, "Revoked."

    monkeypatch.setattr(saas_routes, "revoke_provider_token", fake_revoke)
    for kind in ("shopify", "woocommerce"):
        r = client.post(
            f"/websites/sites/{site['id']}/connections/{kind}/disconnect",
            data={"totp_code": fresh_totp_code(store, account["id"])},
            follow_redirects=False)
        assert r.status_code in (200, 303), r.text
        conn = store.get_connection(site["id"], kind)
        assert conn["status"] == "not_connected"
        assert conn.get("_detail_raw") in (None, {})
    assert seen["shopify"]["access_token"] == "shpat_fake-token-123"
    assert seen["woocommerce"]["consumer_secret"] == "cs_fake-secret-123"

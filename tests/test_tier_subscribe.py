"""The Subscribe page's plan picker: the legacy flat-credit plan
alongside the Axionory tiers (ventoryn/plans.py), and the
already-subscribed wallet account view (balance + add funds).
"""

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ventoryn.saas_routes as saas_routes

from test_saas_shell import _account, _login, client, config, store  # noqa: F401


def test_subscribe_page_shows_all_four_plans_tiers_coming_soon(client, store):
    _login(client, store)
    r = client.get("/account/subscribe")
    assert r.status_code == 200
    # The legacy plan still has a working checkout button.
    assert "Subscribe — $20/month" in r.text
    # Each tier is named and priced, but not yet payable (no Stripe
    # price configured in the test config fixture).
    for label, price in (("Starter", "$20"), ("Growth", "$39"), ("Pro", "$99")):
        assert label in r.text
        assert price in r.text
    assert r.text.count("Coming soon") == 3
    assert "name='plan' value='starter'" not in r.text
    assert "name='plan' value='growth'" not in r.text
    assert "name='plan' value='pro'" not in r.text


def test_subscribe_page_tier_checkout_when_price_configured(client, store, config,
                                                             monkeypatch):
    _login(client, store)
    # The route closes over `config` from register_saas_routes — the
    # way to exercise the configured branch is a fresh app built with
    # the tier price set (not monkeypatching, which cannot reach a
    # value already closed over per-app-instance).
    tier_config = replace(config, stripe_price_id_starter="price_starter_usd")
    from ventoryn.app import create_app
    app = create_app(saas_store=store, saas_config=tier_config)
    from fastapi.testclient import TestClient
    c = TestClient(app)
    c.cookies.update(client.cookies)
    r = c.get("/account/subscribe")
    assert r.status_code == 200
    assert "name='plan' value='starter'" in r.text
    assert "Coming soon" in r.text  # growth/pro still unconfigured

    captured = {}

    def _fake_checkout(cfg, acct, site_url=None, plan=""):
        captured["plan"] = plan
        return {"id": "cs_tier_1",
                "url": "https://checkout.stripe.com/pay/cs_tier_1"}

    import ventoryn.saas_routes as routes_module
    monkeypatch.setattr(routes_module, "create_checkout_session", _fake_checkout)
    r = c.post("/account/subscribe", data={"plan": "starter"},
              follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://checkout.stripe.com/pay/cs_tier_1"
    assert captured["plan"] == "starter"


def test_subscribe_post_rejects_unrecognized_plan(client, store, monkeypatch):
    _login(client, store)
    captured = {}

    def _fake_checkout(cfg, acct, site_url=None, plan=""):
        captured["plan"] = plan
        return {"id": "cs_1", "url": "https://checkout.stripe.com/pay/cs_1"}

    monkeypatch.setattr(saas_routes, "create_checkout_session", _fake_checkout)
    r = client.post("/account/subscribe", data={"plan": "evil-tier"},
                    follow_redirects=False)
    assert r.status_code == 303
    # A tampered/unrecognized plan silently falls back to legacy —
    # never raises, never charges an unknown price.
    assert captured["plan"] == ""


def test_subscribe_page_for_wallet_account_shows_balance_and_topup(client, store):
    account = _login(client, store)
    store.set_plan(account["id"], "starter")
    store.set_subscription(account["id"], "active", "sub_test_1")
    store.grant_wallet_credit(account["id"], 2500, "test")
    store.reserve_wallet_funds(account["id"], 50, "an in-flight job")
    r = client.get("/account/subscribe")
    assert r.status_code == 200
    assert "Starter" in r.text and "active" in r.text
    assert "$25.00" in r.text  # wallet balance
    assert "$0.50" in r.text  # held by the running job
    assert "$24.50" in r.text  # available now
    # Not the legacy "subscribed to Axionory Growth" message.
    assert "Your monthly credits arrive with each" not in r.text
    # Top-up presets are wired to the real route.
    assert "action='/account/wallet/topup'" in r.text
    assert "value='1000'" in r.text  # $10 preset


def test_legacy_active_account_still_shows_legacy_message(client, store):
    account = _login(client, store)
    store.set_subscription(account["id"], "active", "sub_test_2")
    r = client.get("/account/subscribe")
    assert r.status_code == 200
    assert "You are subscribed to Axionory Growth at $20/month." in r.text
    assert "Wallet balance" not in r.text

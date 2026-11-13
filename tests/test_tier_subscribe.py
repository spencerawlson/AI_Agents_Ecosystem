"""The Subscribe page's plan picker: the three Axionory tiers
(ventoryn/plans.py) — the legacy flat-credit plan is no longer
offered to new signups — and the already-subscribed wallet account
view (balance + add funds).
"""

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ventoryn.saas_routes as saas_routes
from ventoryn.saas import CREDIT_COSTS

from test_saas_shell import _account, _login, client, config, store  # noqa: F401


def _credit_table_html(text: str) -> str:
    """The "What a credit buys" table: everything after its heading."""
    return text.split("What a credit buys", 1)[1]


def test_subscribe_page_shows_three_tiers_no_legacy(client, store):
    _login(client, store)
    r = client.get("/account/subscribe")
    assert r.status_code == 200
    # The legacy flat-credit plan is not offered to new signups.
    assert "Axionory Growth" not in r.text
    assert "1,000 credits every month" not in r.text
    assert "the original flat-rate plan" not in r.text
    assert "Payments are not set up yet" not in r.text
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


def test_credit_table_zero_balance_needs_credits(client, store):
    """The reported bug: a 0-credit account saw "Available now" on
    every job while Overview said "0 left". STATUS is the gating unit
    for this account, so every job reads "Needs credits" and the
    balance is shown above the table."""
    _login(client, store)
    r = client.get("/account/subscribe")
    assert r.status_code == 200
    table = _credit_table_html(r.text)
    assert "Your balance:" in table and "0 credits" in table
    assert "Available now" not in table
    assert table.count("Needs credits") == len(CREDIT_COSTS)


def test_credit_table_funded_account_shows_available_now(client, store):
    account = _login(client, store)
    store.grant_credits(account["id"], 100, "test top-up")
    r = client.get("/account/subscribe")
    table = _credit_table_html(r.text)
    assert "100 credits" in table
    assert "Needs credits" not in table
    assert table.count("Available now") == len(CREDIT_COSTS)


def test_credit_table_boundary_is_the_job_cost(client, store):
    """Exactly 5 credits: the 5-credit analyze fits, the 10-credit
    x campaign does not."""
    account = _login(client, store)
    store.grant_credits(account["id"], 5, "test top-up")
    r = client.get("/account/subscribe")
    table = _credit_table_html(r.text)
    assert "5 credits" in table
    analyze = table.split("analyze", 1)[1].split("</tr>", 1)[0]
    assert "Available now" in analyze
    campaign = table.split("x campaign", 1)[1].split("</tr>", 1)[0]
    assert "Needs credits" in campaign


def test_credit_table_wallet_account_zero_balance(client, store):
    """A tier account gates on the wallet, not credits: with an empty
    wallet every job reads "Needs wallet funds". (No active
    subscription here, so the page shows the plan picker + table
    rather than the wallet view.)"""
    account = _login(client, store)
    store.set_plan(account["id"], "starter")
    r = client.get("/account/subscribe")
    assert r.status_code == 200
    table = _credit_table_html(r.text)
    assert "Your wallet balance:" in table and "$0.00" in table
    assert "Available now" not in table
    assert "Needs credits" not in table
    assert table.count("Needs wallet funds") == len(CREDIT_COSTS)


def test_credit_table_wallet_account_with_funds(client, store):
    """$1.00 in the wallet: the jobs whose reserve estimate fits
    (analyze 50c, x campaign 40c, geo audit 50c, community listening
    60c, social report 40c) are available; the bigger ones (seo
    article 150c, video render 200c) need funds."""
    account = _login(client, store)
    store.set_plan(account["id"], "starter")
    store.grant_wallet_credit(account["id"], 100, "test")
    r = client.get("/account/subscribe")
    table = _credit_table_html(r.text)
    assert "$1.00" in table
    assert table.count("Available now") == 5
    assert table.count("Needs wallet funds") == 2

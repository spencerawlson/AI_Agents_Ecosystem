"""Choose a plan on the landing page -> sign in -> that plan's checkout.

"Choose Growth" links to /account/subscribe?plan=growth. Signed out,
that goes to Google sign-in with the plan carried in a cookie; after
sign-in and 2FA the visitor lands on the Growth checkout step (plan,
price, one button to Stripe). Signed in, it opens that step directly.
"""

import sys
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

import ventoryn.saas_routes as saas_routes
from test_saas_shell import _fake_google_urlopen, _login, config, store  # noqa: F401
from ventoryn.app import create_app


def _client(store, config, **overrides):
    cfg = replace(config, stripe_price_id_starter="price_s",
                  stripe_price_id_growth="price_g", stripe_price_id_pro="price_p",
                  **overrides)
    return TestClient(create_app(saas_store=store, saas_config=cfg))


def test_signed_out_choice_goes_to_sign_in_with_the_plan(store, config):
    c = _client(store, config)
    r = c.get("/account/subscribe?plan=growth", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/account/auth/google/start?plan=growth"
    # An unknown plan is ignored: the usual signed-out redirect.
    r = c.get("/account/subscribe?plan=gold", follow_redirects=False)
    assert r.headers["location"] == "/"


def test_sign_in_returns_to_the_chosen_plan(store, config, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_google_urlopen)
    c = _client(store, config)
    r = c.get("/account/auth/google/start?plan=pro", follow_redirects=False)
    assert c.cookies.get(saas_routes.PENDING_PLAN_COOKIE) == "pro"
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = c.get("/account/auth/google/callback", params={"code": "c", "state": state},
              follow_redirects=False)
    # 2FA first (mandatory), then the Pro checkout step.
    nxt = unquote(parse_qs(urlparse(r.headers["location"]).query)["next"][0])
    assert nxt == "/account/subscribe?plan=pro"
    assert not c.cookies.get(saas_routes.PENDING_PLAN_COOKIE)


def test_signed_in_choice_shows_that_plan_and_its_price(store, config):
    c = _client(store, config)
    _login(c, store)
    r = c.get("/account/subscribe?plan=growth")
    assert r.status_code == 200
    assert "<h2>Growth</h2>" in r.text and ">$39</span> / month" in r.text
    assert "Continue to payment — $39/month" in r.text
    assert "name='plan' value='growth'" in r.text
    # Only the chosen plan's button, not the three-card picker.
    assert "name='plan' value='starter'" not in r.text
    assert "name='plan' value='pro'" not in r.text


def test_continue_opens_stripe_checkout_for_that_price(store, config, monkeypatch):
    seen = {}

    def fake_checkout(cfg, account, site_url=None, plan=""):
        seen["plan"], seen["price"] = plan, cfg.tier_price_id(plan)
        return {"url": "https://checkout.stripe.com/c/pay/cs_test"}

    monkeypatch.setattr(saas_routes, "create_checkout_session", fake_checkout)
    c = _client(store, config)
    _login(c, store)
    r = c.post("/account/subscribe", data={"plan": "growth"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("https://checkout.stripe.com/")
    assert seen == {"plan": "growth", "price": "price_g"}


def test_operator_choice_explains_the_owner_never_pays(store, config):
    c = _client(store, config, operator_email="owner@example.com")
    op = store.create_account("sub-op", "owner@example.com", "Owner")
    _login(c, store, op)
    r = c.get("/account/subscribe?plan=starter")
    assert "You chose <strong>Starter — $20/month</strong>" in r.text
    assert "different Google account" in r.text
    assert "Continue to payment" not in r.text

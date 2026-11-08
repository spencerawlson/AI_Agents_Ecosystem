"""Ventoryn landing page + the customer/admin split (ShipItAyiti flow).

The model under test:
  - GET / is the public landing page: what Ventoryn is, the URL box
    + free preview, $20/month, sign-in with Google — no internal
    data, no links to operator pages.
  - After sign-in EVERYONE goes to Live Ops (/ops), scoped to the
    signed-in account: only that account's sites and work.
  - The customer owns their portfolio, report and approvals (with
    an approve button); nothing ships until that customer approves.
  - /admin is the operator's separate URL: subscribers, accounts,
    agent health — aggregates only, never a customer's portfolio
    or approvals, no approve-on-behalf.
  - Spencer's existing businesses attach to his account by the
    SAAS_OPERATOR_EMAIL config — never a hardcoded email.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import core.saas as saas
import dashboard.saas_routes as saas_routes
from core.audit import AuditLog
from core.ledger import Ledger
from core.registry import BusinessRegistry
from core.saas import SaasConfig, SaasStore
from dashboard.app import create_app
from orchestrator.approvals import ApprovalGate

from _signin import (
    OPERATOR_EMAIL,
    operator_store_and_config,
    sign_in,
    sign_in_as_operator,
)

CUSTOMER_A = ("ada@example.com", "google-sub-ada", "Ada")
CUSTOMER_B = ("ben@example.com", "google-sub-ben", "Ben")


def _app_with(registry=None, ledger=None, approvals=None, audit=None):
    store, config = operator_store_and_config()
    app = create_app(registry=registry or BusinessRegistry(),
                     ledger=ledger or Ledger(),
                     approvals=approvals or ApprovalGate(),
                     audit=audit or AuditLog(),
                     saas_store=store, saas_config=config)
    return app, store


def _customer_client(app, store, who):
    email, sub, name = who
    client = TestClient(app)
    account = sign_in(client, store, email=email, sub=sub, name=name)
    return client, account


def _operator_client(app, store):
    client = TestClient(app)
    account = sign_in(client, store)
    return client, account


# ---------------------------------------------------------------------------
# Landing page
# ---------------------------------------------------------------------------

def test_landing_page_public():
    app, _store = _app_with()
    r = TestClient(app).get("/")
    assert r.status_code == 200
    # What it is, the promise, the price, the URL box, sign-in.
    assert "first 100 paying customers" in r.text
    assert "$20/month" in r.text
    assert 'id="landing-analyze-form"' in r.text or \
        "id='landing-analyze-form'" in r.text
    assert 'id="landing-url"' in r.text or "id='landing-url'" in r.text
    assert 'id="landing-preview"' in r.text or \
        "id='landing-preview'" in r.text
    assert 'id="landing-cta"' in r.text or "id='landing-cta'" in r.text
    assert "Sign in with Google" in r.text
    assert "How it works" in r.text
    # The preview reuses the analyze pipeline in-process.
    assert 'fetch("/api/analyze"' in r.text
    # The first site is named as the proof case — with no results.
    assert "Road to CISSP" in r.text
    assert "No results are claimed here yet" in r.text
    # No invented traction.
    assert "testimonial" not in r.text.lower()
    assert "trusted by" not in r.text.lower()
    # No internal data, no operator links for a visitor.
    assert "/ops" not in r.text
    assert "/game" not in r.text
    assert "/portfolio" not in r.text
    assert "/reports" not in r.text
    assert "/approvals" not in r.text
    assert "for website owners" not in r.text
    assert "Back to portfolio" not in r.text
    # Mobile-first: viewport + 44px tap targets.
    assert "width=device-width" in r.text
    assert "min-height: 44px" in r.text


def test_landing_page_signed_in_shows_live_ops():
    app, store = _app_with()
    client, _account = _customer_client(app, store, CUSTOMER_A)
    r = client.get("/")
    assert r.status_code == 200
    assert "Continue to Live Ops" in r.text
    assert "ada@example.com" in r.text
    # Still no business names or internal data on the public page.
    assert "Test Store" not in r.text


def test_landing_page_no_internal_data_even_with_businesses():
    registry = BusinessRegistry()
    ledger = Ledger()
    biz = registry.create("Secret Store", "ecommerce")
    ledger.record(biz.id, "revenue", 9999)
    app, _store = _app_with(registry=registry, ledger=ledger)
    r = TestClient(app).get("/")
    assert "Secret Store" not in r.text
    assert "9,999" not in r.text
    assert "$9,999" not in r.text


# ---------------------------------------------------------------------------
# Sign-in gates
# ---------------------------------------------------------------------------

def test_ops_and_game_require_sign_in():
    app, _store = _app_with()
    client = TestClient(app)
    for path in ("/ops", "/game"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"


def test_api_snapshot_and_report_require_sign_in():
    app, _store = _app_with()
    client = TestClient(app)
    assert client.get("/api/snapshot").status_code == 401
    assert client.get("/api/report").status_code == 401


def test_account_pages_redirect_to_landing_when_unsigned():
    app, _store = _app_with()
    client = TestClient(app)
    for path in ("/account", "/account/login", "/account/dashboard",
                 "/portfolio", "/approvals", "/report"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/", path


# ---------------------------------------------------------------------------
# /ops scoping
# ---------------------------------------------------------------------------

def test_ops_shows_only_the_signed_in_accounts_sites():
    registry = BusinessRegistry()
    ledger = Ledger()
    biz = registry.create("Ops Store", "ecommerce")
    ledger.record(biz.id, "revenue", 5000)
    app, store = _app_with(registry=registry, ledger=ledger)
    client_a, account_a = _customer_client(app, store, CUSTOMER_A)
    client_b, _account_b = _customer_client(app, store, CUSTOMER_B)
    store.create_site(account_a["id"], "https://ada.example.com")
    site_b = store.create_site(_account_b["id"], "https://ben.example.com")

    data = client_a.get("/api/snapshot").json()
    names = [b["name"] for b in data["businesses"]]
    assert names == ["ada.example.com"]
    assert data["totals"]["revenue"] == 0.0
    assert data["experiment_001"] is None
    assert all(a["tasks_completed"] == 0 for a in data["agents"])
    assert data["account"]["email"] == "ada@example.com"
    assert data["account"]["is_operator"] is False

    data_b = client_b.get("/api/snapshot").json()
    assert [b["name"] for b in data_b["businesses"]] == ["ben.example.com"]
    assert "ada.example.com" not in str(data_b["businesses"])

    # The operator sees his registry businesses — and no customer's.
    client_op, _op = _operator_client(app, store)
    data_op = client_op.get("/api/snapshot").json()
    names_op = [b["name"] for b in data_op["businesses"]]
    assert "Ops Store" in names_op
    assert "ada.example.com" not in names_op
    assert "ben.example.com" not in names_op
    assert data_op["account"]["is_operator"] is True
    assert data_op["totals"]["revenue"] == 5000.0


def test_ops_serves_for_a_signed_in_customer():
    app, store = _app_with()
    client, _account = _customer_client(app, store, CUSTOMER_A)
    r = client.get("/ops")
    assert r.status_code == 200
    assert "Ventoryn — Live Ops" in r.text
    # The customer nav, not the old operator link paragraph.
    assert "My Portfolio" in r.text
    assert "My account" in r.text
    assert "Sign out" in r.text
    # The operator simulation controls are hidden for customers
    # (JS hides them from the account in the snapshot).
    assert "applyAccount" in r.text
    assert "state.pendingSiteUrl" in r.text


def test_tick_is_operator_only():
    app, store = _app_with()
    client, _account = _customer_client(app, store, CUSTOMER_A)
    r = client.post("/api/tick")
    assert r.status_code == 403
    client_op, _op = _operator_client(app, store)
    r = client_op.post("/api/tick")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Portfolio / report / approvals — the customer's own
# ---------------------------------------------------------------------------

def test_portfolio_scoped_and_redesigned():
    registry = BusinessRegistry()
    ledger = Ledger()
    biz = registry.create("Ops Store", "ecommerce")
    ledger.record(biz.id, "revenue", 5000)
    ledger.record(biz.id, "advertising", -1000)
    app, store = _app_with(registry=registry, ledger=ledger)
    client_a, account_a = _customer_client(app, store, CUSTOMER_A)
    store.create_site(account_a["id"], "https://ada.example.com")
    store.record_conversion(
        store.list_sites(account_a["id"])[0]["id"], 1900, "usd", "manual")

    r = client_a.get("/portfolio")
    assert r.status_code == 200
    assert "ada.example.com" in r.text
    assert "$19.00" in r.text
    assert "Ops Store" not in r.text  # never another account's business
    # Redesigned: styled cards, not a bare table.
    assert "biz-card" in r.text
    assert "stat-card" in r.text
    assert '<table border="1">' not in r.text

    client_op, _op = _operator_client(app, store)
    r = client_op.get("/portfolio")
    assert "Ops Store" in r.text
    assert "$4,000.00" in r.text  # net profit
    assert "ada.example.com" not in r.text  # not the operator's site


def test_report_scoped_to_the_account():
    registry = BusinessRegistry()
    ledger = Ledger()
    biz = registry.create("Ops Store", "ecommerce")
    ledger.record(biz.id, "revenue", 500)
    app, store = _app_with(registry=registry, ledger=ledger)
    client_a, account_a = _customer_client(app, store, CUSTOMER_A)
    site = store.create_site(account_a["id"], "https://ada.example.com")
    store.record_conversion(site["id"], 1900, "usd", "manual")

    r = client_a.get("/report")
    assert r.status_code == 200
    assert "ada.example.com" in r.text
    assert "$19.00" in r.text
    assert "Ops Store" not in r.text

    client_op, _op = _operator_client(app, store)
    r = client_op.get("/report")
    assert "Ops Store" in r.text
    assert "$500.00" in r.text
    assert "ada.example.com" not in r.text


def test_approvals_scoped_and_gated_by_owner():
    registry = BusinessRegistry()
    biz = registry.create("Ops Store", "ecommerce")
    gate = ApprovalGate()
    gate.request("ad_spend_increase", amount_usd=100.0, business_id=biz.id)
    app, store = _app_with(registry=registry, approvals=gate)

    # The customer never sees the operator's approval…
    client_a, _account_a = _customer_client(app, store, CUSTOMER_A)
    r = client_a.get("/approvals")
    assert r.status_code == 200
    assert "ad_spend_increase" not in r.text
    assert "Nothing awaiting approval" in r.text
    # …and cannot decide it: nothing ships until the owner approves.
    appr = gate.pending()[0]
    r = client_a.post(f"/approvals/{appr.id}/decide",
                      data={"approved": "true"})
    assert r.status_code == 403
    assert len(gate.pending()) == 1

    # The operator sees it, with the approve button, and can approve.
    client_op, _op = _operator_client(app, store)
    r = client_op.get("/approvals")
    assert "ad_spend_increase" in r.text
    assert "Approve" in r.text
    r = client_op.post(f"/approvals/{appr.id}/decide",
                       data={"approved": "true"})
    assert r.status_code == 200
    assert len(gate.pending()) == 0


# ---------------------------------------------------------------------------
# /admin — operator oversight only
# ---------------------------------------------------------------------------

def _admin_setup():
    app, store = _app_with()
    client_a, account_a = _customer_client(app, store, CUSTOMER_A)
    _client_b, account_b = _customer_client(app, store, CUSTOMER_B)
    store.create_site(account_a["id"], "https://ada.example.com")
    store.set_subscription(account_a["id"], "active", "sub_ada")
    store.set_subscription(account_b["id"], "canceled", "sub_ben")
    client_op, _op = _operator_client(app, store)
    return app, store, client_a, client_op


def test_admin_requires_the_operator_account():
    app, store, client_a, _op = _admin_setup()
    unsigned = TestClient(app)
    for path in ("/admin", "/admin/subscribers", "/admin/accounts",
                 "/admin/agents"):
        assert unsigned.get(path, follow_redirects=False).status_code == 303
        r = client_a.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/", path


def test_admin_lists_subscribers_and_accounts():
    _app, _store, _client_a, client_op = _admin_setup()
    r = client_op.get("/admin/subscribers")
    assert r.status_code == 200
    assert "ada@example.com" in r.text  # subscribed
    assert "ben@example.com" not in r.text  # canceled: not a subscriber
    assert "Subscribers" in r.text
    r = client_op.get("/admin/accounts")
    assert r.status_code == 200
    assert "ada@example.com" in r.text
    assert "ben@example.com" in r.text  # every account exists here
    assert "operator@example.com" in r.text


def test_admin_has_no_customer_portfolio_or_approval_content():
    _app, _store, _client_a, client_op = _admin_setup()
    for path in ("/admin/subscribers", "/admin/accounts", "/admin/agents"):
        r = client_op.get(path)
        assert r.status_code == 200
        # No per-customer portfolio content…
        assert "ada.example.com" not in r.text
        assert "ben.example.com" not in r.text
        # …no approvals, no approve buttons for customer items…
        assert "Approve" not in r.text
        assert "ad_spend_increase" not in r.text
        # …and the admin nav, not the customer nav.
        assert "Subscribers" in r.text
        assert "Accounts" in r.text
        assert "Agents" in r.text
        assert "My Portfolio" not in r.text
        assert "My account" not in r.text


def test_admin_agents_shows_activity_and_problems():
    audit = AuditLog()
    audit.record("discovery", "run_completed", task_id="t1", run_id="r1",
                 tokens_used=100, cost_usd=0.02, result={"tick": 1})
    audit.record("marketing", "run_failed", task_id="t2", run_id="r2",
                 tokens_used=50, cost_usd=0.01, error="boom")
    app, store = _app_with(audit=audit)
    client_op, _op = _operator_client(app, store)
    r = client_op.get("/admin/agents")
    assert r.status_code == 200
    assert "discovery" in r.text
    assert "marketing" in r.text
    assert "run_failed" in r.text
    assert "boom" in r.text


# ---------------------------------------------------------------------------
# Operator tools stay operator-only; nav cleanup
# ---------------------------------------------------------------------------

def test_operator_tools_reject_regular_accounts():
    registry = BusinessRegistry()
    biz = registry.create("Ops Store", "ecommerce")
    app, store = _app_with(registry=registry)
    client_a, _account_a = _customer_client(app, store, CUSTOMER_A)
    for path in ("/businesses", f"/businesses/{biz.id}", "/experiments",
                 "/experiment-001", "/reports"):
        r = client_a.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/", path
    client_op, _op = _operator_client(app, store)
    assert client_op.get("/businesses").status_code == 200
    assert client_op.get("/experiments").status_code == 200


def test_customer_nav_exact_and_no_removed_links():
    app, store = _app_with()
    client_a, account_a = _customer_client(app, store, CUSTOMER_A)
    store.create_site(account_a["id"], "https://ada.example.com")
    for path in ("/portfolio", "/approvals", "/report",
                 "/account/dashboard"):
        r = client_a.get(path)
        assert r.status_code == 200
        for item in ("Live Ops", "My Portfolio", "Approvals", "Report",
                     "My account", "Subscribe", "Sign out"):
            assert item in r.text, (path, item)
        for gone in ("Experiments", "Experiment 001", "Daily plan",
                     "Attribution", "for website owners",
                     "Back to portfolio"):
            assert gone not in r.text, (path, gone)
        # The nav never links the old /game address.
        assert 'href="/game"' not in r.text


def test_no_back_to_portfolio_anywhere_signed_in():
    app, store = _app_with()
    client_op, _op = _operator_client(app, store)
    for path in ("/portfolio", "/approvals", "/report", "/ops",
                 "/businesses", "/experiments", "/reports"):
        r = client_op.get(path)
        assert "Back to portfolio" not in r.text, path


def test_spencer_businesses_attach_by_email_config():
    """The existing ecosystem businesses are linked to the operator
    account by the SAAS_OPERATOR_EMAIL config — the operator sees
    them in his portfolio and snapshot; any other email does not."""
    registry = BusinessRegistry()
    ledger = Ledger()
    registry.create("Farelune", "travel")
    registry.create("Road to CISSP", "education")
    app, store = _app_with(registry=registry, ledger=ledger)
    client_op, _op = _operator_client(app, store)
    r = client_op.get("/portfolio")
    assert "Farelune" in r.text
    assert "Road to CISSP" in r.text
    client_a, _account_a = _customer_client(app, store, CUSTOMER_A)
    r = client_a.get("/portfolio")
    assert "Farelune" not in r.text
    assert "Road to CISSP" not in r.text


# ---------------------------------------------------------------------------
# The funnel: URL -> preview -> sign-in -> pay -> site added
# ---------------------------------------------------------------------------

def _fake_google_urlopen(request, **kwargs):
    class _FakeResponse:
        def __init__(self, payload):
            import json as _json

            self._body = _json.dumps(payload).encode("utf-8")

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    url = request.full_url
    if "oauth2.googleapis.com/token" in url:
        return _FakeResponse({"access_token": "tok-123"})
    if "openidconnect.googleapis.com" in url:
        return _FakeResponse({"sub": "google-sub-ada",
                              "email": "ada@example.com", "name": "Ada"})
    raise AssertionError(f"unexpected Google call: {url}")


def test_funnel_url_carried_through_sign_in_subscribe_site(
        monkeypatch):
    """Landing URL -> Google sign-in -> /ops with the URL -> subscribe
    with the URL prefilled -> site added. The URL is never retyped."""
    config = SaasConfig(
        google_client_id="google-client-id",
        google_client_secret="google-secret",
        stripe_secret_key="sk_test_shell",
        stripe_webhook_secret="whsec_test",
        stripe_price_id="price_20usd",
        monthly_credits=100,
        base_url="http://testserver",
        operator_email=OPERATOR_EMAIL,
    )
    store = SaasStore(":memory:")
    app = create_app(saas_store=store, saas_config=config)
    client = TestClient(app)
    monkeypatch.setattr("urllib.request.urlopen", _fake_google_urlopen)

    # 1. The visitor types the URL, then signs in with it carried.
    r = client.get("/account/auth/google/start",
                   params={"site_url": "https://ada.example.com"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert "accounts.google.com" in r.headers["location"]
    assert "ventoryn_pending_site" in r.headers.get("set-cookie", "")
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]

    # 2. Google calls back — everyone lands on Live Ops, with the URL.
    r = client.get("/account/auth/google/callback",
                   params={"code": "auth-code", "state": state},
                   follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == \
        "/ops?site_url=https%3A%2F%2Fada.example.com"

    # 3. /ops (signed in) prefills the analyzer with that URL.
    r = client.get("/ops?site_url=https%3A%2F%2Fada.example.com")
    assert r.status_code == 200
    assert "pendingSiteUrl" in r.text
    assert "Subscribe — $20/month to add it" in r.text

    # 4. Subscribe carries the URL onward (never retyped).
    r = client.get("/account/subscribe",
                   params={"site_url": "https://ada.example.com"})
    assert r.status_code == 200
    assert "ada.example.com" in r.text
    assert "name='site_url'" in r.text

    def _fake_checkout(cfg, account, site_url=None):
        assert site_url == "https://ada.example.com"
        return {"id": "cs_test_1",
                "url": "https://checkout.stripe.com/pay/cs_test_1"}

    monkeypatch.setattr(saas_routes, "create_checkout_session",
                        _fake_checkout)
    r = client.post("/account/subscribe",
                    data={"site_url": "https://ada.example.com"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == \
        "https://checkout.stripe.com/pay/cs_test_1"

    # 5. The webhook would mark the subscription active; the
    # dashboard then offers the site, prefilled, and it is added.
    account = store.get_account_by_sub("google-sub-ada")
    store.set_subscription(account["id"], "active", "sub_ada")
    r = client.get("/account/dashboard",
                   params={"subscribed": "1",
                           "site_url": "https://ada.example.com"})
    assert r.status_code == 200
    assert "Add your website" in r.text
    assert "ada.example.com" in r.text
    r = client.post("/account/sites",
                    data={"url": "https://ada.example.com",
                          "country": "ca", "language": "en"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert store.list_sites(account["id"])[0]["url"] == \
        "https://ada.example.com"
    # And it is now hers alone: her snapshot and portfolio show it.
    assert [b["name"] for b in
            client.get("/api/snapshot").json()["businesses"]] == \
        ["ada.example.com"]
    assert "ada.example.com" in client.get("/portfolio").text


# ---------------------------------------------------------------------------
# Store: list_accounts
# ---------------------------------------------------------------------------

def test_list_accounts_returns_every_account_oldest_first():
    store = SaasStore(":memory:")
    a = store.create_account("sub-1", "first@example.com", "First")
    b = store.create_account("sub-2", "second@example.com", "Second")
    rows = store.list_accounts()
    assert [r["id"] for r in rows] == [a["id"], b["id"]]
    assert [r["email"] for r in rows] == ["first@example.com",
                                          "second@example.com"]
    store.close()

"""SaaS shell tests: accounts (Google), Stripe billing, credits, sites.

All Stripe/Google HTTP is mocked — no live keys, no live charges, ever.
"""

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import core.saas as saas
import dashboard.saas_routes as saas_routes
from core.saas import SaasConfig, SaasStore
from dashboard.app import create_app


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
        stripe_secret_key="sk_test_shell",
        stripe_webhook_secret="whsec_platform",
        stripe_price_id="price_20usd",
        monthly_credits=100,
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
    # Two-factor is mandatory: the test session has passed it.
    from core.totp import code_at
    if not store.totp_state(account["id"])["enabled"]:
        setup = store.begin_totp_setup(account["id"])
        store.confirm_totp_setup(account["id"], code_at(setup["secret"]))
    store.mark_session_totp_verified(token)
    client.cookies.set(saas.SESSION_COOKIE, token)
    return account


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def test_validate_site_url():
    assert saas.validate_site_url("https://road2cissp.com") == \
        "https://road2cissp.com"
    assert saas.validate_site_url("http://example.com/page/") == \
        "http://example.com/page"
    assert saas.validate_site_url("example.com") == "https://example.com"
    assert saas.validate_site_url("  example.com/page  ") == \
        "https://example.com/page"
    for bad in ("", "not a url", "example", "ftp://example.com",
                "https://localhost:8000", "http://127.0.0.1",
                "https://10.0.0.5", "https://192.168.1.1",
                "https://myhost.local", "https://[::1]/"):
        with pytest.raises(ValueError):
            saas.validate_site_url(bad)


# ---------------------------------------------------------------------------
# Store: sessions, OAuth states, credits
# ---------------------------------------------------------------------------

def test_sessions(store):
    account = _account(store)
    token = store.create_session(account["id"])
    assert store.get_account_by_session(token)["id"] == account["id"]
    assert store.get_account_by_session("nope") is None
    store.delete_session(token)
    assert store.get_account_by_session(token) is None


def test_oauth_state_single_use(store):
    state = store.create_oauth_state()
    assert store.consume_oauth_state(state) is True
    assert store.consume_oauth_state(state) is False  # no replays
    assert store.consume_oauth_state("bogus") is False


def test_credits_grant_charge_and_refusal(store):
    account = _account(store)
    assert store.grant_credits(account["id"], 100, "test grant") == 100
    job = store.charge_credits(account["id"], "analyze", "site-1")
    assert job["credits_charged"] == 5
    assert store.credit_balance(account["id"]) == 95
    with pytest.raises(saas.InsufficientCredits) as exc:
        for _ in range(30):  # drain it; the failing charge must not go negative
            store.charge_credits(account["id"], "analyze", "site-1")
    assert exc.value.balance >= 0
    assert store.credit_balance(account["id"]) >= 0
    assert store.credit_balance(account["id"]) < 5
    with pytest.raises(saas.SaasError):
        store.charge_credits(account["id"], "nope", "site-1")


def test_site_defaults_to_honest_connections(store):
    account = _account(store)
    site = store.create_site(account["id"], "https://road2cissp.com")
    kinds = {c["kind"]: c for c in store.get_connections(site["id"])}
    assert set(kinds) == {"reddit", "x", "plausible", "stripe_sales"}
    assert all(c["status"] == "not_connected" for c in kinds.values())
    assert "app registration" in kinds["reddit"]["note"]


# ---------------------------------------------------------------------------
# Google login flow (mocked HTTP)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _fake_google_urlopen(request, **kwargs):
    url = request.full_url
    if "oauth2.googleapis.com/token" in url:
        return _FakeResponse({"access_token": "tok-123"})
    if "openidconnect.googleapis.com" in url:
        return _FakeResponse({"sub": "google-sub-1",
                              "email": "ada@example.com", "name": "Ada"})
    raise AssertionError(f"unexpected Google call: {url}")


def test_google_login_flow(client, store, config, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_google_urlopen)
    r = client.get("/account/auth/google/start", follow_redirects=False)
    assert r.status_code == 303
    assert "accounts.google.com/o/oauth2/v2/auth" in r.headers["location"]
    assert "client_id=google-client-id" in r.headers["location"]
    assert "redirect_uri=http%3A%2F%2Ftestserver%2Faccount%2Fauth%2Fgoogle%2Fcallback" \
        in r.headers["location"]
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]

    r = client.get("/account/auth/google/callback",
                   params={"code": "auth-code", "state": state},
                   follow_redirects=False)
    assert r.status_code == 303
    # Two-factor is mandatory from the first sign-in: a fresh account
    # goes to the setup page first, not straight to Live Ops.
    assert r.headers["location"] == \
        "/account/two-factor/setup?next=%2Fops"
    assert saas.SESSION_COOKIE in r.headers.get("set-cookie", "")
    account = store.get_account_by_sub("google-sub-1")
    assert account is not None and account["email"] == "ada@example.com"

    # Complete the forced setup: the QR page, then the code from the
    # pending secret. The success page shows the backup codes once.
    r = client.get("/account/two-factor/setup")
    assert r.status_code == 200
    assert "Two-factor authentication" in r.text
    from core.totp import code_at
    secret = store.get_totp_pending_secret(account["id"])
    r = client.post("/account/two-factor/setup",
                    data={"code": code_at(secret), "next": "/ops"},
                    follow_redirects=False)
    assert r.status_code == 200
    assert "Two-factor is on" in r.text
    assert "backup" in r.text.lower()

    r = client.get("/account/dashboard")
    assert r.status_code == 200
    assert "0" in r.text  # zero balance is real here (no subscription yet)

    # The landing page names the signed-in account.
    r = client.get("/")
    assert r.status_code == 200
    assert "ada@example.com" in r.text
    assert "Continue to Live Ops" in r.text


def test_google_callback_rejects_bad_state(client, store):
    r = client.get("/account/auth/google/callback",
                   params={"code": "auth-code", "state": "bogus"})
    assert r.status_code == 400
    assert store.get_account_by_sub("google-sub-1") is None


def test_login_page_says_so_when_google_unconfigured(store):
    """Sign-in lives on the landing page now: with Google unconfigured
    the landing page says so plainly instead of offering a button,
    and /account/login sends you there."""
    app = create_app(saas_store=store,
                     saas_config=SaasConfig(base_url="http://testserver"))
    c = TestClient(app)
    r = c.get("/account/login", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    r = c.get("/")
    assert r.status_code == 200
    assert "not set up yet" in r.text
    # No sign-in button is offered when Google is not configured
    # (the how-it-works prose may still name it, and the preview
    # script holds the URL for after sign-in is configured).
    assert "id='landing-signin'" not in r.text


# ---------------------------------------------------------------------------
# Dashboard + subscribe pages
# ---------------------------------------------------------------------------

def test_dashboard_requires_login(client):
    r = client.get("/account/dashboard", follow_redirects=False)
    assert r.status_code == 303
    # Sign-in lives on the landing page — that is where an unsigned
    # visitor is sent.
    assert r.headers["location"] == "/"


def test_dashboard_honest_unavailable_states(client, store):
    account = _login(client, store)
    site = store.create_site(account["id"], "https://road2cissp.com")
    r = client.get("/account/dashboard")
    assert r.status_code == 200
    assert "road2cissp.com" in r.text
    assert "unavailable" in r.text  # funnel values, nothing connected yet
    # My account shows the plan name only — the Subscribe pitch
    # ($20/month) and the credit table live only on the Subscribe
    # page, never here.
    assert "Ventoryn Growth" in r.text
    assert "$20/month" not in r.text
    assert "Subscribe — $20/month" not in r.text
    assert "What a credit buys" not in r.text
    assert "Credit activity" not in r.text
    # Mobile: viewport + 44px tap targets.
    assert "width=device-width" in r.text
    assert "min-height: 44px" in r.text


def test_site_create_rejects_bad_url(client, store):
    _login(client, store)
    r = client.post("/account/sites",
                    data={"url": "https://localhost:8000", "country": "ca",
                          "language": "en"})
    assert r.status_code == 400
    assert store.list_sites(_account(store)["id"]) == []


def test_subscribe_page_shows_20_and_checkout(client, store, monkeypatch):
    account = _login(client, store)
    r = client.get("/account/subscribe")
    assert r.status_code == 200
    assert "$20/month" in r.text
    assert "100 credits" in r.text

    def _fake_checkout(config, account, site_url=None):
        assert config.stripe_price_id == "price_20usd"
        return {"id": "cs_test_1",
                "url": "https://checkout.stripe.com/pay/cs_test_1"}

    monkeypatch.setattr(saas_routes, "create_checkout_session",
                        _fake_checkout)
    r = client.post("/account/subscribe", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == \
        "https://checkout.stripe.com/pay/cs_test_1"


def test_subscribe_page_says_so_without_stripe(store):
    app = create_app(
        saas_store=store,
        saas_config=SaasConfig(
            google_client_id="x", google_client_secret="y",
            base_url="http://testserver"))
    c = TestClient(app)
    _login(c, store)
    r = c.get("/account/subscribe")
    assert r.status_code == 200
    assert "$20/month" in r.text
    assert "not set up yet" in r.text
    assert "Subscribe — $20/month" not in r.text


# ---------------------------------------------------------------------------
# Stripe webhook
# ---------------------------------------------------------------------------

def _signed(secret, event, timestamp=None):
    payload = json.dumps(event).encode("utf-8")
    timestamp = timestamp if timestamp is not None else int(time.time())
    sig = hmac.new(secret.encode("utf-8"),
                   f"{timestamp}.".encode("utf-8") + payload,
                   hashlib.sha256).hexdigest()
    return payload, {"stripe-signature": f"t={timestamp},v1={sig}"}


def test_webhook_rejects_bad_signature(client):
    payload, _ = _signed("whsec_platform", {"id": "evt_1", "type": "x"})
    r = client.post("/account/billing/webhook", content=payload,
                    headers={"stripe-signature": "t=1,v1=deadbeef"})
    assert r.status_code == 400


def test_webhook_checkout_grants_credits_once(client, store):
    account = _account(store)
    event = {"id": "evt_checkout_1", "type": "checkout.session.completed",
             "data": {"object": {"client_reference_id": account["id"],
                                 "customer": "cus_1",
                                 "subscription": "sub_1"}}}
    payload, headers = _signed("whsec_platform", event)
    r = client.post("/account/billing/webhook", content=payload,
                    headers=headers)
    assert r.status_code == 200
    assert r.json()["result"] == "ok"
    account = store.get_account(account["id"])
    assert account["subscription_status"] == "active"
    assert account["stripe_customer_id"] == "cus_1"
    assert store.credit_balance(account["id"]) == 100
    # Redelivery: same event id is a duplicate, credits granted once.
    r = client.post("/account/billing/webhook", content=payload,
                    headers=headers)
    assert r.json()["result"] == "duplicate"
    assert store.credit_balance(account["id"]) == 100


def test_webhook_subscription_deleted_and_renewal(client, store):
    account = _account(store)
    store.set_stripe_customer(account["id"], "cus_1", "sub_1")
    store.set_subscription(account["id"], "active", "sub_1")
    store.grant_credits(account["id"], 100, "test")
    event = {"id": "evt_sub_del", "type": "customer.subscription.deleted",
             "data": {"object": {"id": "sub_1", "customer": "cus_1",
                                 "status": "canceled"}}}
    payload, headers = _signed("whsec_platform", event)
    assert client.post("/account/billing/webhook", content=payload,
                       headers=headers).status_code == 200
    assert store.get_account(account["id"])["subscription_status"] == \
        "canceled"

    event = {"id": "evt_inv_1", "type": "invoice.payment_succeeded",
             "data": {"object": {"subscription": "sub_1",
                                 "customer": "cus_1",
                                 "billing_reason": "subscription_cycle"}}}
    payload, headers = _signed("whsec_platform", event)
    client.post("/account/billing/webhook", content=payload, headers=headers)
    assert store.credit_balance(account["id"]) == 200  # renewal grant

    event = {"id": "evt_inv_2", "type": "invoice.payment_succeeded",
             "data": {"object": {"subscription": "sub_1",
                                 "customer": "cus_1",
                                 "billing_reason": "subscription_create"}}}
    payload, headers = _signed("whsec_platform", event)
    client.post("/account/billing/webhook", content=payload, headers=headers)
    assert store.credit_balance(account["id"]) == 200  # not double-granted


def test_customer_sale_webhook_counts_paying_customer(client, store):
    account = _account(store)
    site = store.create_site(account["id"], "https://road2cissp.com")
    store.update_connection(site["id"], "stripe_sales", "connected",
                            "Connected", {"webhook_secret": "whsec_site"})
    event = {"id": "evt_sale_1", "type": "checkout.session.completed",
             "data": {"object": {
                 "amount_total": 1900, "currency": "usd",
                 "metadata": {"ventoryn_site_id": site["id"]}}}}
    payload, headers = _signed("whsec_site", event)
    r = client.post("/account/billing/webhook", content=payload,
                    headers=headers)
    assert r.status_code == 200
    assert r.json()["result"] == "ok"
    assert store.paying_customer_count(site["id"]) == 1

    # Same event id again: duplicate, still one paying customer.
    client.post("/account/billing/webhook", content=payload, headers=headers)
    assert store.paying_customer_count(site["id"]) == 1

    # A sale for a different site id is ignored, not counted here.
    event2 = {"id": "evt_sale_2", "type": "checkout.session.completed",
              "data": {"object": {
                  "amount_total": 1900, "currency": "usd",
                  "metadata": {"ventoryn_site_id": "someone-else"}}}}
    payload2, headers2 = _signed("whsec_site", event2)
    r = client.post("/account/billing/webhook", content=payload2,
                    headers=headers2)
    assert r.json()["result"] == "ignored"
    assert store.paying_customer_count(site["id"]) == 1


# ---------------------------------------------------------------------------
# Jobs + connections + manual conversions
# ---------------------------------------------------------------------------

def test_job_analyze_charges_and_reports(client, store, tmp_path, monkeypatch):
    account = _login(client, store)
    store.grant_credits(account["id"], 100, "test grant")
    site = store.create_site(account["id"], "https://road2cissp.com")

    def _fake_analysis(url, country, language):
        assert url == "https://road2cissp.com"
        return {"url": url, "country": country, "language": language,
                "at": "2026-11-08T00:00:00+00:00", "site_score": 85,
                "geo_score": 72, "site": {"pages_checked": 1,
                                          "sitemap_urls": 1},
                "geo": {"pages_checked": 1},
                "market": {"fetched": False, "findings": []},
                "community_result": None,
                "app_result": None,
                "findings": [{"severity": "low", "area": "x",
                              "issue": "i", "fix": "f"}],
                "community": "", "keywords": [], "app_id": ""}

    monkeypatch.setattr("ecosystem.analyze.run_analysis", _fake_analysis)
    monkeypatch.setattr(saas_routes, "REPORTS_DIR", tmp_path)
    r = client.post(f"/account/sites/{site['id']}/jobs",
                    data={"job_type": "analyze"}, follow_redirects=False)
    assert r.status_code == 303
    assert store.credit_balance(account["id"]) == 95
    jobs = store.list_jobs(account["id"], site_id=site["id"])
    assert len(jobs) == 1
    assert jobs[0]["status"] == "done"
    assert "Site 85/100" in jobs[0]["result"]
    reports = list(tmp_path.glob("analyze_road2cissp-com_*.md"))
    assert len(reports) == 1
    assert "Site score: **85/100**" in reports[0].read_text(encoding="utf-8")


def test_job_insufficient_credits_refused(client, store):
    account = _login(client, store)
    site = store.create_site(account["id"], "https://road2cissp.com")
    r = client.post(f"/account/sites/{site['id']}/jobs",
                    data={"job_type": "analyze"})
    assert r.status_code == 402
    assert "balance is 0" in r.text
    assert store.list_jobs(account["id"]) == []


def test_job_not_available_yet_is_not_charged(client, store):
    account = _login(client, store)
    store.grant_credits(account["id"], 100, "test grant")
    site = store.create_site(account["id"], "https://road2cissp.com")
    r = client.post(f"/account/sites/{site['id']}/jobs",
                    data={"job_type": "seo_article"})
    assert r.status_code == 400
    assert "not available yet" in r.text
    assert store.credit_balance(account["id"]) == 100


def test_connections_are_honest_and_secrets_never_rendered(client, store):
    account = _login(client, store)
    site = store.create_site(account["id"], "https://road2cissp.com")
    r = client.post(
        f"/account/sites/{site['id']}/connections/reddit", data={})
    assert r.status_code == 400
    assert "app registration" in r.text

    r = client.post(
        f"/account/sites/{site['id']}/connections/plausible",
        data={"site_domain": "road2cissp.com",
              "api_key": "plausible-secret-key"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post(
        f"/account/sites/{site['id']}/connections/stripe_sales",
        data={"webhook_secret": "whsec-site-secret"},
        follow_redirects=False)
    assert r.status_code == 303
    r = client.get(f"/account/sites/{site['id']}")
    assert r.status_code == 200
    assert "plausible-secret-key" not in r.text
    assert "whsec-site-secret" not in r.text
    assert "Connected" in r.text


def test_manual_sale_is_the_owner_recorded_fallback(client, store):
    account = _login(client, store)
    site = store.create_site(account["id"], "https://road2cissp.com")
    r = client.post(f"/account/sites/{site['id']}/conversions",
                    data={"amount_usd": "19.00"}, follow_redirects=False)
    assert r.status_code == 303
    assert store.paying_customer_count(site["id"]) == 1
    # Revenue is private by default: the sale counts toward the 100
    # goal, but no dollar figure is shown.
    r = client.get(f"/account/sites/{site['id']}")
    assert "$19.00" not in r.text
    assert "hidden" in r.text
    assert "Pending" in r.text  # owner-recorded, no Stripe event yet
    # Turn the revenue display on and the figure appears. The privacy
    # toggle is a sensitive action: with 2FA enabled it needs a fresh
    # code.
    from _signin import fresh_totp_code
    r = client.post(f"/account/sites/{site['id']}/privacy",
                    data={"revenue_show": "on", "count_show": "on",
                          "details_show": "on",
                          "totp_code": fresh_totp_code(store, account["id"]),
                          "return_to": f"/account/sites/{site['id']}"},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.get(f"/account/sites/{site['id']}")
    assert "$19.00" in r.text

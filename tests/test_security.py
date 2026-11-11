"""Security requirements (Spencer-approved formulation).

- Tokens are stored ENCRYPTED at rest, server-side only, with the
  minimum scopes. Disconnect REVOKES at the provider, not just
  locally. Per-account "pause all posting" switch.
- Consent text states plainly what Ventoryn can and cannot do.
- Revenue privacy: per-account scoped; the display toggle is
  respected everywhere including admin totals (off = no dollar
  figure anywhere; the sale still counts as a verified customer);
  revenue figures never go into logs.
- Webhooks: HMAC signature-verified, fail closed, replay protection
  and idempotent counting (a payment reported twice counts once).
- Admin password: constant-time server-side compare, login
  rate-limiting (lockout after a few failures), every admin login in
  the audit log. Sessions: HttpOnly, Secure, SameSite=lax; logout
  invalidates the session server-side.
- Deletion: disconnecting or deleting an account deletes its tokens
  and revenue data and revokes access at the provider.
- Attribution honesty: a sale is attributed only where the Stripe
  metadata carries the Ventoryn tag; otherwise unattributed.
- X's API is paid and rate-limited — the X connection page says so.
"""

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from core.audit import AuditLog
from core.ledger import Ledger
from core.registry import BusinessRegistry
from ventoryn.saas import (
    CONNECTION_SCOPES,
    SaasConfig,
    SaasStore,
    check_posting_allowed,
    revoke_provider_token,
)
from ventoryn.app import create_app
from orchestrator.approvals import ApprovalGate

from _signin import (
    OPERATOR_EMAIL,
    fresh_totp_code,
    operator_store_and_config,
    sign_in,
)

CUSTOMER_A = ("ada@example.com", "google-sub-ada", "Ada")
ADMIN_PASSWORD = "test-admin-password"


def _app_with(admin_password=ADMIN_PASSWORD):
    store, config = operator_store_and_config(
        admin_password=admin_password)
    app = create_app(registry=BusinessRegistry(), ledger=Ledger(),
                     approvals=ApprovalGate(), audit=AuditLog(),
                     saas_store=store, saas_config=config)
    return app, store, config


def _stripe_signature(payload: bytes, secret: str) -> str:
    ts = int(time.time())
    sig = hmac.new(secret.encode("utf-8"),
                   f"{ts}.".encode("utf-8") + payload,
                   hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _sale_event(event_id, amount=1900, tag=None):
    metadata = {"ventoryn_site_id": ""}
    if tag:
        metadata["ventoryn_tag"] = tag
    return {"id": event_id, "type": "checkout.session.completed",
            "data": {"object": {
                "amount_total": amount, "currency": "usd",
                "metadata": metadata}}}


def test_tokens_not_stored_in_plaintext():
    store = SaasStore(":memory:")
    account = store.create_account("google-sub-ada", CUSTOMER_A[0], "Ada")
    site = store.create_site(account["id"], "https://ada.example.com")
    raw_secret = "whsec_live_secret_value"
    store.update_connection(site["id"], "stripe_sales", "connected",
                            "Connected.", {"webhook_secret": raw_secret})
    # The stored detail must not contain the raw secret anywhere.
    with store._lock:
        row = store._conn.execute(
            "SELECT detail FROM saas_connections"
            " WHERE site_id = ? AND kind = 'stripe_sales'",
            (site["id"],)).fetchone()
    assert raw_secret not in row[0]
    assert row[0].startswith('{"webhook_secret": "v1.')
    # …but it decrypts back to exactly the raw value.
    secrets = dict(store.stripe_sales_secrets())
    assert secrets[site["id"]] == raw_secret


def test_disconnect_revokes_at_the_provider():
    calls = []

    class _FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request):
        calls.append(request)
        return _FakeResponse()

    revoked, note = revoke_provider_token(
        "reddit", {"access_token": "tok_reddit"},
        client_id="reddit-client", client_secret="reddit-secret",
        urlopen=fake_urlopen)
    assert revoked is True
    assert len(calls) == 1
    assert calls[0].full_url == (
        "https://www.reddit.com/api/v1/revoke_token")
    assert "tok_reddit" in calls[0].data.decode("utf-8")
    # No live token and no app credentials: nothing to revoke, but
    # the disconnect is still safe (revoked=True, honest note).
    revoked, note = revoke_provider_token("reddit", {})
    assert revoked is True
    assert "Nothing to revoke" in note
    # Plausible / Stripe have no provider revoke API: the note says
    # to remove it in the provider's dashboard.
    revoked, note = revoke_provider_token("plausible", {})
    assert revoked is True
    assert "Plausible dashboard" in note


def test_minimal_scopes_asserted():
    assert CONNECTION_SCOPES["reddit"] == "submit read identity"
    assert CONNECTION_SCOPES["x"] == (
        "tweet.read tweet.write users.read offline.access")
    assert CONNECTION_SCOPES["plausible"] == "stats:read"
    assert CONNECTION_SCOPES["stripe_sales"] == "read payments"
    # No DM scope, no charge/refund scope anywhere.
    assert "dm" not in " ".join(CONNECTION_SCOPES.values()).lower()
    assert "charge" not in " ".join(CONNECTION_SCOPES.values()).lower()
    assert "refund" not in " ".join(CONNECTION_SCOPES.values()).lower()


def test_webhook_bad_signature_rejected_and_replay_counts_once():
    app, store, _config = _app_with()
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    site = store.create_site(account["id"], "https://ada.example.com")
    # Connect Stripe sales (the webhook secret is the shared secret).
    r = client.post(
        f"/websites/sites/{site['id']}/connections/stripe_sales",
        data={"webhook_secret": "whsec_test"}, follow_redirects=False)
    assert r.status_code == 303
    event = _sale_event("evt_webhook_1")
    event["data"]["object"]["metadata"]["ventoryn_site_id"] = site["id"]
    payload = json.dumps(event).encode("utf-8")
    # A bad signature is rejected (fail closed).
    r = client.post("/account/billing/webhook", content=payload,
                    headers={"stripe-signature": "t=1,v1=deadbeef"})
    assert r.status_code == 400
    assert store.paying_customer_count(site["id"]) == 0
    # The real signature is accepted…
    r = client.post(
        "/account/billing/webhook", content=payload,
        headers={"stripe-signature": _stripe_signature(
            payload, "whsec_test")})
    assert r.status_code == 200
    assert r.json()["result"] == "ok"
    assert store.paying_customer_count(site["id"]) == 1
    # …and a replayed delivery counts once.
    r = client.post(
        "/account/billing/webhook", content=payload,
        headers={"stripe-signature": _stripe_signature(
            payload, "whsec_test")})
    assert r.status_code == 200
    assert r.json()["result"] == "duplicate"
    assert store.paying_customer_count(site["id"]) == 1
    assert len(store.list_conversions(site["id"])) == 1


def test_admin_login_rate_limiting():
    app, store, _config = _app_with()
    client = TestClient(app)
    for _ in range(5):
        r = client.post("/admin/login", data={"password": "wrong"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/login?error=1"
    assert store.admin_login_locked("testclient") is True
    # Locked out: even the RIGHT password fails while locked.
    r = client.post("/admin/login", data={"password": ADMIN_PASSWORD},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login?error=1"


def test_admin_login_audit_log_records_attempts():
    audit = AuditLog()
    store, config = operator_store_and_config(
        admin_password=ADMIN_PASSWORD)
    app = create_app(registry=BusinessRegistry(), ledger=Ledger(),
                     approvals=ApprovalGate(), audit=audit,
                     saas_store=store, saas_config=config)
    client = TestClient(app)
    client.post("/admin/login", data={"password": "wrong"})
    client.post("/admin/login", data={"password": ADMIN_PASSWORD})
    events = [e for e in audit.all_events()
              if e.event == "admin_login"]
    assert len(events) == 2
    assert events[0].result.get("success") is False
    assert events[1].result.get("success") is True
    # No password (or code) ever lands in the audit result.
    assert ADMIN_PASSWORD not in json.dumps(
        [e.result for e in events])


def test_revenue_toggle_off_hides_everywhere_but_count_increments():
    app, store, _config = _app_with()
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    site = store.create_site(account["id"], "https://ada.example.com")
    r = client.post(
        f"/websites/sites/{site['id']}/connections/stripe_sales",
        data={"webhook_secret": "whsec_test"}, follow_redirects=False)
    assert r.status_code == 303
    store.record_conversion(site["id"], 1900, "usd", "manual")
    # Revenue display OFF (the default): no dollar figure on the
    # customer report…
    r = client.get("/report")
    assert "$19.00" not in r.text
    # …nor in the admin totals…
    admin = TestClient(app)
    r = admin.post("/admin/login", data={"password": ADMIN_PASSWORD},
                   follow_redirects=False)
    assert r.status_code == 303
    r = admin.get("/admin")
    assert r.status_code == 200
    assert "$19.00" not in r.text
    assert "Platform revenue" in r.text
    # …but the paying-customer count still increments (toward 100).
    assert store.paying_customer_count(site["id"]) == 1
    totals = store.admin_revenue_totals()
    assert totals["paying_customers"] == 1
    assert totals["total_cents"] == 0


def test_delete_account_purges_tokens_and_revenue():
    app, store, _config = _app_with()
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    site = store.create_site(account["id"], "https://ada.example.com")
    store.update_connection(site["id"], "stripe_sales", "connected",
                            "Connected.", {"webhook_secret": "whsec_x"})
    store.record_conversion(site["id"], 1900, "usd", "manual")
    assert store.get_connection(site["id"], "stripe_sales") is not None
    assert len(store.list_conversions(site["id"])) == 1
    # Delete (fresh 2FA code, since the account has 2FA enabled).
    r = client.post(
        "/account/delete",
        data={"totp_code": fresh_totp_code(store, account["id"])},
        follow_redirects=False)
    assert r.status_code == 303
    assert store.get_account(account["id"]) is None
    assert store.get_site(site["id"]) is None
    assert store.get_connection(site["id"], "stripe_sales") is None
    assert store.list_conversions(site["id"]) == []
    # The deleted session stops working immediately.
    r = client.get("/ops", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_pause_all_posting_blocks_everything():
    app, store, config = _app_with()
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    site = store.create_site(account["id"], "https://ada.example.com")
    store.update_connection(site["id"], "plausible", "connected",
                            "Connected.", {"api_key": "key"})
    allowed, _reason = check_posting_allowed(
        store, account, site["id"], "plausible", config)
    assert allowed is True
    r = client.post("/account/security/pause",
                    data={"paused": "1"}, follow_redirects=False)
    assert r.status_code == 303
    account = store.get_account(account["id"])
    allowed, reason = check_posting_allowed(
        store, account, site["id"], "plausible", config)
    assert allowed is False
    assert "paused" in reason.lower()

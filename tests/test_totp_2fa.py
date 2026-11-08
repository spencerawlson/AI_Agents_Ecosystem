"""Forced TOTP two-factor + zero-trust (Ventoryn).

- Two-factor is MANDATORY for every account from the first sign-in:
  a fresh account cannot reach /ops or any product page until 2FA
  setup completes; a signed-in session without 2FA is redirected to
  setup from every route. Once set up, the code is required at every
  sign-in. Backup codes work once each and then fail. The setup page
  explains in plain words why it is required and what to do if the
  phone is lost; the operator can reset 2FA for an account from
  /admin (audited), which forces setup again at the next sign-in.
- Admin 2FA: password + 6-digit code once enabled; backup codes
  once-only; rate-limiting and the audit log stay.
- Zero-trust: a session deleted server-side is rejected on the next
  request; a customer session cannot open /admin; sensitive actions
  demand a fresh 2FA code when 2FA is enabled.
- The corrected split: ANY Google account can sign up and use the
  product (Live Ops, My Portfolio, Approvals, Report); /admin is the
  operator's alone (password + 2FA).
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import core.saas as saas
from core.audit import AuditLog
from core.ledger import Ledger
from core.registry import BusinessRegistry
from core.saas import SaasConfig, SaasStore
from core.totp import code_at
from dashboard.app import create_app
from orchestrator.approvals import ApprovalGate

from _signin import (
    OPERATOR_EMAIL,
    fresh_totp_code,
    operator_store_and_config,
    sign_in,
    sign_in_raw,
)

CUSTOMER_A = ("ada@example.com", "google-sub-ada", "Ada")
CUSTOMER_B = ("ben@example.com", "google-sub-ben", "Ben")
ADMIN_PASSWORD = "test-admin-password"


def _app_with(admin_password=ADMIN_PASSWORD):
    store, config = operator_store_and_config(
        admin_password=admin_password)
    app = create_app(registry=BusinessRegistry(), ledger=Ledger(),
                     approvals=ApprovalGate(), audit=AuditLog(),
                     saas_store=store, saas_config=config)
    return app, store, config


def _admin_client(app, password=ADMIN_PASSWORD, code=""):
    client = TestClient(app)
    data = {"password": password}
    if code:
        data["code"] = code
    r = client.post("/admin/login", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/admin"
    return client


def _backup_codes_from_page(html: str) -> list[str]:
    # The success page shows each code as "XXXX XXXX".
    return [m.replace(" ", "")
            for m in re.findall(r"<div>(\d{4} \d{4})</div>", html)]


# ---------------------------------------------------------------------------
# Admin 2FA
# ---------------------------------------------------------------------------

def test_admin_2fa_setup_then_code_required_every_login():
    app, store, _config = _app_with()
    admin = _admin_client(app)
    # Setup: the QR + manual key are shown once; confirm by code.
    r = admin.get("/admin/two-factor/setup")
    assert r.status_code == 200
    assert "Set up two-factor" in r.text
    assert "QR" in r.text or "<svg" in r.text  # the QR code (SVG)
    assert "manual" in r.text.lower()
    secret = store.get_admin_totp_pending_secret()
    assert secret
    r = admin.post("/admin/two-factor/setup",
                   data={"code": code_at(secret)})
    assert r.status_code == 200
    assert "Two-factor is on" in r.text
    codes = _backup_codes_from_page(r.text)
    assert len(codes) == 8
    assert store.admin_totp_state()["enabled"] is True
    # Log out; password alone no longer opens /admin.
    admin.get("/admin/logout")
    r = admin.post("/admin/login", data={"password": ADMIN_PASSWORD},
                   follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login?error=1"
    # Password + a valid code does.
    _admin_client(app, code=code_at(secret))
    # …and a wrong code fails.
    r = admin.post("/admin/login",
                   data={"password": ADMIN_PASSWORD, "code": "000000"},
                   follow_redirects=False)
    assert r.headers["location"] == "/admin/login?error=1"


def test_admin_backup_code_works_once_then_fails():
    app, store, _config = _app_with()
    admin = _admin_client(app)
    admin.get("/admin/two-factor/setup")
    secret = store.get_admin_totp_pending_secret()
    r = admin.post("/admin/two-factor/setup",
                   data={"code": code_at(secret)})
    codes = _backup_codes_from_page(r.text)
    assert codes
    admin.get("/admin/logout")
    # The first backup code opens /admin…
    _admin_client(app, code=codes[0])
    admin.get("/admin/logout")
    # …the same code never works again.
    r = admin.post("/admin/login",
                   data={"password": ADMIN_PASSWORD, "code": codes[0]},
                   follow_redirects=False)
    assert r.headers["location"] == "/admin/login?error=1"
    # …but a different backup code still does.
    _admin_client(app, code=codes[1])


def test_admin_login_without_code_fails_once_enabled():
    app, store, _config = _app_with()
    admin = _admin_client(app)
    admin.get("/admin/two-factor/setup")
    secret = store.get_admin_totp_pending_secret()
    admin.post("/admin/two-factor/setup",
               data={"code": code_at(secret)})
    # The OLD password-only session (created before 2FA was enabled)
    # cannot open /admin any more — the gate re-checks the 2FA state
    # on every request. No silent trust.
    r = admin.get("/admin", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"
    # A fresh password-only login is refused.
    fresh = TestClient(app)
    r = fresh.post("/admin/login", data={"password": ADMIN_PASSWORD},
                   follow_redirects=False)
    assert r.headers["location"] == "/admin/login?error=1"


# ---------------------------------------------------------------------------
# Forced customer 2FA
# ---------------------------------------------------------------------------

def test_fresh_account_cannot_reach_any_product_page_until_setup():
    app, store, _config = _app_with()
    client = TestClient(app)
    sign_in_raw(client, store, email=CUSTOMER_A[0],
                sub="google-sub-ada", name="Ada")
    for path in ("/ops", "/portfolio", "/approvals", "/report",
                 "/account/dashboard", "/account/subscribe",
                 "/account/sites/new"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"].startswith(
            "/account/two-factor/setup?next="), (path, r.headers)
    # The setup page itself is reachable, and it explains why.
    r = client.get("/account/two-factor/setup?next=%2Fops")
    assert r.status_code == 200
    assert "Two-factor" in r.text
    assert "backup" in r.text.lower()
    assert "<svg" in r.text  # the QR code (vector)


def test_customer_2fa_setup_then_sign_in_with_code_works():
    app, store, _config = _app_with()
    client = TestClient(app)
    account = sign_in_raw(client, store, email=CUSTOMER_A[0],
                          sub="google-sub-ada", name="Ada")
    r = client.get("/account/two-factor/setup?next=%2Fops")
    assert r.status_code == 200
    secret = store.get_totp_pending_secret(account["id"])
    assert secret
    r = client.post("/account/two-factor/setup",
                    data={"code": code_at(secret), "next": "/ops"})
    assert r.status_code == 200
    assert "Two-factor is on" in r.text
    codes = _backup_codes_from_page(r.text)
    assert len(codes) == 8
    # Setup complete: Live Ops opens.
    assert client.get("/ops").status_code == 200
    # Sign out and back in: the code is required at every sign-in.
    client.get("/account/logout")
    sign_in_raw(client, store, email=CUSTOMER_A[0],
                sub="google-sub-ada", name="Ada")
    r = client.get("/ops", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith(
        "/account/two-factor/verify?next=")
    r = client.post("/account/two-factor/verify",
                    data={"code": code_at(secret), "next": "/ops"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ops"
    assert client.get("/ops").status_code == 200


def test_customer_backup_code_works_once_then_fails():
    app, store, _config = _app_with()
    client = TestClient(app)
    account = sign_in_raw(client, store, email=CUSTOMER_A[0],
                          sub="google-sub-ada", name="Ada")
    client.get("/account/two-factor/setup?next=%2Fops")
    secret = store.get_totp_pending_secret(account["id"])
    r = client.post("/account/two-factor/setup",
                    data={"code": code_at(secret), "next": "/ops"})
    codes = _backup_codes_from_page(r.text)
    client.get("/account/logout")
    sign_in_raw(client, store, email=CUSTOMER_A[0],
                sub="google-sub-ada", name="Ada")
    r = client.post("/account/two-factor/verify",
                    data={"code": codes[0], "next": "/ops"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ops"
    # The same backup code never works again.
    client.get("/account/logout")
    sign_in_raw(client, store, email=CUSTOMER_A[0],
                sub="google-sub-ada", name="Ada")
    r = client.post("/account/two-factor/verify",
                    data={"code": codes[0], "next": "/ops"},
                    follow_redirects=False)
    assert r.status_code != 303 or r.headers.get("location") != "/ops"
    assert client.get("/ops", follow_redirects=False).status_code == 303


def test_operator_2fa_reset_forces_setup_again():
    app, store, _config = _app_with()
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    assert store.totp_state(account["id"])["enabled"] is True
    admin = _admin_client(app)
    r = admin.post(f"/admin/accounts/{account['id']}/reset-totp",
                   follow_redirects=False)
    assert r.status_code == 303
    assert store.totp_state(account["id"])["enabled"] is False
    # The customer's next sign-in goes to SETUP, not verify.
    client.get("/account/logout")
    sign_in_raw(client, store, email=CUSTOMER_A[0],
                sub="google-sub-ada", name="Ada")
    r = client.get("/ops", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith(
        "/account/two-factor/setup?next=")


# ---------------------------------------------------------------------------
# Zero-trust
# ---------------------------------------------------------------------------

def test_session_deleted_server_side_is_rejected():
    app, store, _config = _app_with()
    client = TestClient(app)
    sign_in(client, store, email=CUSTOMER_A[0],
            sub="google-sub-ada", name="Ada")
    assert client.get("/ops").status_code == 200
    token = client.cookies.get(saas.SESSION_COOKIE)
    store.delete_session(token)
    # The cookie is still in the browser — the session is dead anyway.
    r = client.get("/ops", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_customer_session_cannot_open_admin():
    app, store, _config = _app_with()
    client = TestClient(app)
    sign_in(client, store, email=CUSTOMER_A[0],
            sub="google-sub-ada", name="Ada")
    for path in ("/admin", "/admin/subscribers", "/admin/accounts",
                 "/admin/agents"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/admin/login", path
    # Even the operator's own Google session cannot open /admin.
    op = TestClient(app)
    sign_in(op, store)
    r = op.get("/admin", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"


def test_sensitive_action_demands_fresh_2fa_code():
    app, store, _config = _app_with()
    client = TestClient(app)
    account = sign_in(client, store, email=CUSTOMER_A[0],
                      sub="google-sub-ada", name="Ada")
    site = store.create_site(account["id"], "https://ada.example.com")
    # No fresh code: refused.
    r = client.post(f"/account/sites/{site['id']}/privacy",
                    data={"revenue_show": "on"})
    assert r.status_code == 400
    assert store.get_site(site["id"])["revenue_show"] == 0
    # A fresh code: accepted.
    r = client.post(
        f"/account/sites/{site['id']}/privacy",
        data={"revenue_show": "on", "count_show": "on",
              "details_show": "on",
              "totp_code": fresh_totp_code(store, account["id"])},
        follow_redirects=False)
    assert r.status_code == 303
    assert store.get_site(site["id"])["revenue_show"] == 1


# ---------------------------------------------------------------------------
# The corrected split: anyone may sign up; /admin is his alone
# ---------------------------------------------------------------------------

def test_any_google_account_can_sign_up_and_use_the_product():
    app, store, _config = _app_with()
    client = TestClient(app)
    # A brand-new account, not the operator.
    sign_in(client, store, email="stranger@example.com",
            sub="google-sub-stranger", name="Stranger")
    for path in ("/ops", "/portfolio", "/approvals", "/report"):
        r = client.get(path)
        assert r.status_code == 200, path
    # …but that same account cannot reach /admin.
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"

"""Admin credit grants: the operator assigns spendable balance to
an account (a few credits a month, like his own unlimited).

Routes (all behind the admin password, never a Google session):
- POST /admin/accounts/{id}/grants — create a grant; the first
  month's amount lands immediately.
- GET  /admin/grants — list grants, this month's totals, pause /
  resume / revoke controls.
- POST /admin/grants/{id}/pause|resume|revoke.

Currency is auto-selected from the account's plan at creation:
legacy credits for a no-plan account, wallet dollars for a tier
account. No grant to the operator's own account.
"""

import pytest
from fastapi.testclient import TestClient

from core.audit import AuditLog
from core.ledger import Ledger
from core.registry import BusinessRegistry
from orchestrator.approvals import ApprovalGate
from orchestrator.engine import Orchestrator
from tests._signin import OPERATOR_EMAIL, operator_store_and_config
from ventoryn.app import create_app

ADMIN_PASSWORD = "adminpw"


def _app_with(store, config):
    registry = BusinessRegistry()
    ledger = Ledger()
    approvals = ApprovalGate()
    audit = AuditLog()
    # The admin password travels on the config (operator_store_and_
    # config(admin_password=...)), never as a create_app argument.
    app = create_app(registry=registry, ledger=ledger, approvals=approvals,
                     audit=audit, saas_store=store, saas_config=config)
    return app, audit


def _admin_client(store, config):
    app, audit = _app_with(store, config)
    client = TestClient(app)
    login = client.post("/admin/login", data={"password": ADMIN_PASSWORD},
                        follow_redirects=False)
    assert login.status_code == 303
    return client, audit


def _customer(store, email="ada@example.com"):
    return store.create_account(f"sub-{email}", email, "Ada")


def test_admin_grant_requires_admin_session():
    store, config = operator_store_and_config(admin_password=ADMIN_PASSWORD)
    app, _ = _app_with(store, config)
    client = TestClient(app)
    account = _customer(store)
    response = client.post(f"/admin/accounts/{account['id']}/grants",
                           data={"amount": "50", "kind": "monthly"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    response = client.get("/admin/grants", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_admin_grant_monthly_lands_immediately():
    store, config = operator_store_and_config(admin_password=ADMIN_PASSWORD)
    client, audit = _admin_client(store, config)
    account = _customer(store)
    response = client.post(f"/admin/accounts/{account['id']}/grants",
                           data={"amount": "50", "kind": "monthly",
                                 "note": "help"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/grants"
    assert store.credit_balance(account["id"]) == 50
    grants = store.list_operator_grants(account["id"])
    assert len(grants) == 1
    assert grants[0]["kind"] == "monthly"
    assert grants[0]["note"] == "help"
    page = client.get("/admin/grants")
    assert page.status_code == 200
    assert account["email"] in page.text
    assert "50 credits" in page.text
    events = [e for e in audit.all_events()
              if e.event == "admin_credit_grant"]
    assert len(events) == 1
    assert events[0].result["email"] == account["email"]
    assert events[0].result["amount"] == 50


def test_admin_grant_wallet_account_uses_dollars():
    store, config = operator_store_and_config(admin_password=ADMIN_PASSWORD)
    client, _ = _admin_client(store, config)
    account = _customer(store)
    store.set_plan(account["id"], "starter")
    response = client.post(f"/admin/accounts/{account['id']}/grants",
                           data={"amount": "5", "kind": "monthly"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/grants"
    assert store.wallet_balance_cents(account["id"]) == 500
    page = client.get("/admin/grants")
    assert "$5.00" in page.text


def test_admin_grant_invalid_amount():
    store, config = operator_store_and_config(admin_password=ADMIN_PASSWORD)
    client, _ = _admin_client(store, config)
    account = _customer(store)
    for bad in ("0", "1001", "abc", ""):
        response = client.post(
            f"/admin/accounts/{account['id']}/grants",
            data={"amount": bad, "kind": "monthly"},
            follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == (
            "/admin/accounts?error=bad_amount")
    assert store.credit_balance(account["id"]) == 0
    assert store.list_operator_grants(account["id"]) == []


def test_admin_grant_nonexistent_account():
    store, config = operator_store_and_config(admin_password=ADMIN_PASSWORD)
    client, _ = _admin_client(store, config)
    response = client.post("/admin/accounts/nope/grants",
                           data={"amount": "50", "kind": "monthly"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/accounts?error=unknown"


def test_admin_grant_to_operator_refused():
    store, config = operator_store_and_config(admin_password=ADMIN_PASSWORD)
    client, _ = _admin_client(store, config)
    operator = store.create_account("google-sub-op", OPERATOR_EMAIL,
                                    "Operator")
    response = client.post(f"/admin/accounts/{operator['id']}/grants",
                           data={"amount": "50", "kind": "monthly"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/accounts?error=operator"
    assert store.list_operator_grants(operator["id"]) == []


def test_admin_grant_duplicate_monthly_refused():
    store, config = operator_store_and_config(admin_password=ADMIN_PASSWORD)
    client, _ = _admin_client(store, config)
    account = _customer(store)
    client.post(f"/admin/accounts/{account['id']}/grants",
                data={"amount": "50", "kind": "monthly"},
                follow_redirects=False)
    response = client.post(f"/admin/accounts/{account['id']}/grants",
                           data={"amount": "10", "kind": "monthly"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == (
        "/admin/accounts?error=duplicate")
    assert len(store.list_operator_grants(account["id"])) == 1
    assert store.credit_balance(account["id"]) == 50


def test_admin_grant_pause_resume_revoke():
    store, config = operator_store_and_config(admin_password=ADMIN_PASSWORD)
    client, audit = _admin_client(store, config)
    account = _customer(store)
    client.post(f"/admin/accounts/{account['id']}/grants",
                data={"amount": "50", "kind": "monthly"},
                follow_redirects=False)
    grant = store.list_operator_grants(account["id"])[0]
    for action, status in (("pause", "paused"), ("resume", "active"),
                           ("revoke", "revoked")):
        response = client.post(f"/admin/grants/{grant['id']}/{action}",
                               follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/grants"
        assert store.get_operator_grant(grant["id"])["status"] == status
    # Revoked is terminal: resuming changes nothing.
    response = client.post(f"/admin/grants/{grant['id']}/resume",
                           follow_redirects=False)
    assert response.status_code == 303
    assert store.get_operator_grant(grant["id"])["status"] == "revoked"
    # The balance already granted stays — revoke is not a clawback.
    assert store.credit_balance(account["id"]) == 50
    events = [e.event for e in audit.all_events()
              if e.event.startswith("admin_credit_grant")]
    assert events.count("admin_credit_grant_pause") == 1
    assert events.count("admin_credit_grant_resume") == 1
    assert events.count("admin_credit_grant_revoke") == 1


def test_admin_accounts_page_shows_spendable_balance_and_grant_form():
    store, config = operator_store_and_config(admin_password=ADMIN_PASSWORD)
    client, _ = _admin_client(store, config)
    legacy = _customer(store, "legacy@example.com")
    store.grant_credits(legacy["id"], 50, "seed")
    tier = _customer(store, "tier@example.com")
    store.set_plan(tier["id"], "starter")
    store.grant_wallet_credit(tier["id"], 500, "seed")
    page = client.get("/admin/accounts")
    assert page.status_code == 200
    assert f"/admin/accounts/{legacy['id']}/grants" in page.text
    assert f"/admin/accounts/{tier['id']}/grants" in page.text
    # The tier account shows its wallet funds, not a 0 credits
    # balance.
    assert "$5.00" in page.text


def test_admin_grants_page_totals():
    store, config = operator_store_and_config(admin_password=ADMIN_PASSWORD)
    client, _ = _admin_client(store, config)
    account = _customer(store)
    client.post(f"/admin/accounts/{account['id']}/grants",
                data={"amount": "50", "kind": "monthly"},
                follow_redirects=False)
    page = client.get("/admin/grants")
    assert page.status_code == 200
    assert "Granted this month" in page.text
    assert "50 credits" in page.text

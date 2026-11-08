"""Registry businesses belong to the account recorded on them.

No business is tied to an account by name or by config: a business
carries owner_email. The owner sees it (portfolio, /ops, approvals,
/businesses); an unclaimed one is visible to the operator only, who
can claim it into their own account; nobody else ever sees it.
"""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _signin import OPERATOR_EMAIL, operator_store_and_config, sign_in
from core.ledger import Ledger
from core.registry import BusinessRegistry
from ventoryn.app import create_app
from orchestrator.approvals import ApprovalGate


def _app():
    registry = BusinessRegistry()
    store, config = operator_store_and_config()
    app = create_app(registry=registry, ledger=Ledger(),
                     approvals=ApprovalGate(), saas_store=store,
                     saas_config=config)
    return app, registry, store


def test_unclaimed_business_is_claimable_by_operator_only():
    app, registry, store = _app()
    biz = registry.create("Farelune", "saas")
    op = TestClient(app)
    sign_in(op, store)
    r = op.get("/businesses")
    assert "Farelune" in r.text and "Claim" in r.text
    r = op.post(f"/businesses/{biz.id}/claim", follow_redirects=False)
    assert r.status_code == 303
    assert registry.get(biz.id).owner_email == OPERATOR_EMAIL
    # Once owned it cannot be claimed again.
    assert op.post(f"/businesses/{biz.id}/claim").status_code == 404
    assert "Yours" in op.get("/businesses").text


def test_owned_business_is_invisible_to_other_accounts():
    app, registry, store = _app()
    biz = registry.create("Road to CISSP", "education_website")
    registry.assign_owner(biz.id, OPERATOR_EMAIL)
    other = TestClient(app)
    sign_in(other, store, email="someone@example.com", sub="sub-other")
    assert "Road to CISSP" not in other.get("/portfolio").text
    snap = other.get("/api/snapshot").json()
    assert all(b["name"] != "Road to CISSP" for b in snap["businesses"])
    owner = TestClient(app)
    sign_in(owner, store)
    assert "Road to CISSP" in owner.get("/portfolio").text


def test_operator_does_not_see_another_accounts_business():
    app, registry, store = _app()
    biz = registry.create("Someone Else's Shop", "ecommerce")
    registry.assign_owner(biz.id, "someone@example.com")
    op = TestClient(app)
    sign_in(op, store)
    assert "Someone Else" not in op.get("/portfolio").text
    assert "Someone Else" not in op.get("/businesses").text
    assert op.get(f"/businesses/{biz.id}").status_code == 404
    assert op.post(f"/businesses/{biz.id}/claim").status_code == 404

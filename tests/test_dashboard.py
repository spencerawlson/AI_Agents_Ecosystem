"""Dashboard smoke test."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from core.ledger import Ledger
from core.registry import BusinessRegistry
from dashboard.app import create_app
from orchestrator.approvals import ApprovalGate


def test_dashboard():
    registry = BusinessRegistry()
    ledger = Ledger()
    approvals = ApprovalGate()

    biz = registry.create("Test Store", "ecommerce")
    ledger.record(biz.id, "revenue", 1000)
    ledger.record(biz.id, "advertising", -200)
    approvals.request("ad_spend_increase", amount_usd=100.0, business_id=biz.id)

    app = create_app(registry=registry, ledger=ledger, approvals=approvals)
    client = TestClient(app)

    r = client.get("/")
    assert r.status_code == 200
    assert "Test Store" in r.text
    assert "$800.00" in r.text  # net profit

    r = client.get(f"/businesses/{biz.id}")
    assert r.status_code == 200
    assert "Test Store" in r.text

    r = client.get("/approvals")
    assert r.status_code == 200
    assert "ad_spend_increase" in r.text

    # Approve via the form
    appr = approvals.pending()[0]
    r = client.post(f"/approvals/{appr.id}/decide", data={"approved": "true"})
    assert r.status_code == 200
    assert len(approvals.pending()) == 0

    print("Dashboard tests passed.")


if __name__ == "__main__":
    test_dashboard()

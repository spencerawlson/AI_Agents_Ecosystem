"""Dashboard smoke test."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from core.ledger import Ledger
from core.registry import BusinessRegistry
from dashboard.app import create_app
from orchestrator.approvals import ApprovalGate
from orchestrator.engine import Orchestrator
from orchestrator.registry import AgentRegistry


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


def test_api_report():
    """GET /api/report returns the agent + financial report JSON."""
    registry = BusinessRegistry()
    ledger = Ledger()

    biz = registry.create("Report Store", "ecommerce")
    ledger.record(biz.id, "revenue", 1000)
    ledger.record(biz.id, "advertising", -200)

    agent_registry = AgentRegistry()
    agent_registry.register("discovery", capabilities=["search"],
                            description="test agent")
    orchestrator = Orchestrator(registry=agent_registry)

    app = create_app(orchestrator=orchestrator, registry=registry,
                     ledger=ledger)
    client = TestClient(app)

    r = client.get("/api/report")
    assert r.status_code == 200
    data = r.json()
    assert set(data) >= {"generated_at", "tick", "agents", "financials",
                         "businesses", "experiment_001"}
    assert data["financials"]["revenue"] == 1000.0
    assert data["financials"]["total_costs"] == 200.0
    assert data["financials"]["net_profit"] == 800.0
    assert data["financials"]["blended_margin"] == 0.8
    assert data["businesses"][0]["name"] == "Report Store"
    assert data["businesses"][0]["net_profit"] == 800.0
    assert isinstance(data["agents"], list) and len(data["agents"]) > 0
    assert set(data["agents"][0]) >= {"type", "tasks_completed", "tokens_used",
                                     "cost_usd", "last_task"}
    assert data["experiment_001"] is None  # no monitor tick yet


def test_api_report_experiment_shape():
    """experiment_001 block has the required keys when a monitor ran."""
    registry = BusinessRegistry()
    app = create_app(registry=registry, ledger=Ledger())
    app.state.game["experiment_001"] = {
        "verdict": "ON_TRACK", "days_elapsed": 5, "days_remaining": 55,
        "orders": 3, "revenue_usd": 36.0, "spend_usd": 4.5, "pace_pct": 42,
    }
    client = TestClient(app)
    exp = client.get("/api/report").json()["experiment_001"]
    assert set(exp) == {"verdict", "days_elapsed", "days_remaining", "orders",
                        "revenue_usd", "spend_usd", "pace_pct"}
    assert exp["verdict"] == "ON_TRACK"


def test_report_page():
    """GET /report renders the server-side report with agent + P&L tables."""
    registry = BusinessRegistry()
    ledger = Ledger()

    biz = registry.create("Report Store", "ecommerce")
    ledger.record(biz.id, "revenue", 500)

    app = create_app(registry=registry, ledger=ledger)
    client = TestClient(app)

    r = client.get("/report")
    assert r.status_code == 200
    assert "Agent &amp; Financial Report" in r.text
    assert "Report Store" in r.text
    assert "Agent performance" in r.text
    assert "Business P" in r.text
    assert "$500.00" in r.text
    assert "/game" in r.text  # back-link to the live view


if __name__ == "__main__":
    test_dashboard()
    test_api_report()
    test_api_report_experiment_shape()
    test_report_page()
    print("Report tests passed.")

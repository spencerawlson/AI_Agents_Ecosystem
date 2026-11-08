"""Dashboard smoke test."""

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from core.audit import AuditLog
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
    assert "/ops" in r.text  # Live Ops link, not the old "game" tag
    assert "Live Ops" in r.text

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
    assert "/ops" in r.text  # link to the Live Ops view
    assert "Live Ops" in r.text


def test_game_view_is_live_ops():
    """/game serves the Ecosystem Tycoon — Live Ops page with a vector
    cast (one inline SVG per character, no emoji in the roster) and the
    live ops window; /ops is an alias for the same page."""
    app = create_app(registry=BusinessRegistry(), ledger=Ledger())
    client = TestClient(app)
    for path in ("/game", "/ops"):
        r = client.get(path)
        assert r.status_code == 200
        assert "Ecosystem Tycoon — Live Ops" in r.text
        assert "Live game view" not in r.text
        assert "What the team is doing" in r.text
        assert 'id="ops-feed"' in r.text
        assert 'id="ops-bars"' in r.text
        assert 'id="ops-activity"' in r.text
        assert 'id="ops-status"' in r.text
        assert "setInterval" in r.text and "5000" in r.text
        # One well-formed inline SVG vector icon per character.
        attrs = re.search(r"const SVG_ATTRS = '(.*?)';", r.text).group(1)
        roster = r.text.split("const ROSTER = [", 1)[1].split("];", 1)[0]
        icons = re.findall(r"'<svg ' \+ SVG_ATTRS \+ '(.*?)</svg>'",
                           roster, re.S)
        assert len(icons) == 8
        for body in icons:
            svg = "<svg " + attrs + body + "</svg>"
            root = ET.fromstring(svg)
            assert root.get("viewBox") == "0 0 64 64"
            assert root.get("data-icon") in {
                "scout", "analyst", "builder", "studio",
                "growth", "care", "ops", "ledger"}
        # No emoji left in the cast.
        for old in ("🔭", "🔬", "🛠️", "🎨", "📣", "💬", "⚙️", "📒"):
            assert old not in roster
        assert "char-emoji" not in r.text


def test_api_snapshot_feeds_live_ops():
    """GET /api/snapshot carries everything the ops window shows:
    per-agent last_task, and audit events with time / actor / event /
    business / task / result."""
    registry = BusinessRegistry()
    ledger = Ledger()
    audit = AuditLog()
    biz = registry.create("Ops Store", "ecommerce")
    audit.record("discovery", "tick_completed", task_id="task_1",
                 business_id=biz.id,
                 result={"winner": "Ops Store", "score": 87})
    agent_registry = AgentRegistry()
    agent_registry.register("discovery", capabilities=["search"],
                            description="d")
    agent_registry.register("research", capabilities=["analyze"],
                            description="r")
    orchestrator = Orchestrator(registry=agent_registry)
    app = create_app(orchestrator=orchestrator, registry=registry,
                     ledger=ledger, audit=audit)
    app.state.game["tick"] = 3
    app.state.game["agent_stats"] = {
        "discovery": {"tasks": 2, "tokens": 120, "cost": 0.01,
                      "last_task": "ranked opportunities"},
    }
    client = TestClient(app)
    data = client.get("/api/snapshot").json()
    assert data["tick"] == 3
    by_type = {a["type"]: a for a in data["agents"]}
    assert by_type["discovery"]["tasks_completed"] == 2
    assert by_type["discovery"]["last_task"] == "ranked opportunities"
    assert by_type["research"]["tasks_completed"] == 0
    assert by_type["research"]["last_task"] == ""
    assert len(data["events"]) == 1
    evt = data["events"][0]
    assert set(evt) >= {"ts", "actor", "event", "business_id", "task_id",
                        "result"}
    assert evt["actor"] == "discovery"
    assert evt["event"] == "tick_completed"
    assert evt["business_id"] == biz.id
    assert evt["task_id"] == "task_1"
    assert evt["result"]["winner"] == "Ops Store"


if __name__ == "__main__":
    test_dashboard()
    test_api_report()
    test_api_report_experiment_shape()
    test_report_page()
    test_game_view_is_live_ops()
    test_api_snapshot_feeds_live_ops()
    print("Report tests passed.")

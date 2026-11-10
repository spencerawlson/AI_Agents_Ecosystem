"""Dashboard smoke test."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from core.audit import AuditLog
from core.ledger import Ledger
from core.registry import BusinessRegistry
from ventoryn.app import create_app
from orchestrator.approvals import ApprovalGate
from orchestrator.engine import Orchestrator
from orchestrator.registry import AgentRegistry

from _signin import operator_store_and_config, sign_in


def _operator_app(**kwargs):
    """create_app wired with an operator store/config, plus a signed-in
    operator client. Returns (app, client, store)."""
    store, config = operator_store_and_config()
    app = create_app(saas_store=store, saas_config=config, **kwargs)
    client = TestClient(app)
    sign_in(client, store)
    return app, client, store


def test_dashboard():
    registry = BusinessRegistry()
    ledger = Ledger()
    approvals = ApprovalGate()

    biz = registry.create("Test Store", "ecommerce")
    ledger.record(biz.id, "revenue", 1000)
    ledger.record(biz.id, "advertising", -200)
    approvals.request("ad_spend_increase", amount_usd=100.0, business_id=biz.id)

    app, client, _store = _operator_app(
        registry=registry, ledger=ledger, approvals=approvals)

    # / is the public landing page now — the portfolio moved to
    # /portfolio, and it is the operator's own (signed-in) view.
    r = client.get("/")
    assert r.status_code == 200
    assert "Founding Customer" in r.text
    assert "first 100 paying customers" not in r.text
    assert "Sign in with Google" in r.text
    assert "$20/month" in r.text

    r = client.get("/portfolio")
    assert r.status_code == 200
    assert "Test Store" in r.text
    assert "$800.00" in r.text  # net profit
    assert "/ops" in r.text  # Overview link, not the old "game" tag
    assert "Overview" in r.text  # the sidebar's Live Ops item
    assert "v-biz" in r.text  # colored cards, not a bare table
    assert "v-stat" in r.text

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

    app, client, _store = _operator_app(
        orchestrator=orchestrator, registry=registry, ledger=ledger)

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
    app, client, _store = _operator_app(
        registry=registry, ledger=Ledger())
    app.state.game["experiment_001"] = {
        "verdict": "ON_TRACK", "days_elapsed": 5, "days_remaining": 55,
        "orders": 3, "revenue_usd": 36.0, "spend_usd": 4.5, "pace_pct": 42,
    }
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

    app, client, _store = _operator_app(
        registry=registry, ledger=ledger)

    r = client.get("/report")
    assert r.status_code == 200
    assert "Agent &amp; Financial Report" in r.text
    assert "Report Store" in r.text
    assert "Agent performance" in r.text
    assert "Business P" in r.text
    assert "$500.00" in r.text
    # The report renders through the platform shell now: the
    # sidebar links back to Live Ops (Overview), no pill nav.
    assert "class='app-sidebar'" in r.text
    assert "/ops" in r.text
    assert "Overview" in r.text
    assert "class='customer-nav'" not in r.text


def test_game_redirects_to_live_ops():
    """The game view is gone: /game redirects to the professional
    server-rendered Overview (/ops), which keeps the top summary
    and the Live activity feed but no crew, no HUD, no runtime
    controls, and no game file behind it."""
    app, client, _store = _operator_app(
        registry=BusinessRegistry(), ledger=Ledger())
    r = client.get("/game", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/ops"
    r = client.get("/ops")
    assert r.status_code == 200
    assert "Overview — Ventoryn" in r.text
    assert "Live game view" not in r.text
    assert "Your crew" not in r.text
    assert "What the team is doing" not in r.text
    assert 'id="ops-feed"' not in r.text
    assert 'id="ops-bars"' not in r.text
    assert 'id="ops-activity"' not in r.text
    assert 'id="ops-status"' not in r.text
    assert "confetti" not in r.text.lower()
    assert "game.html" not in r.text
    # The professional Overview keeps the Live activity feed.
    assert "Live activity" in r.text
    # The poll-error wording is gone with the game file.
    assert "game server" not in r.text


def test_api_snapshot_feeds_live_ops():
    """GET /api/snapshot carries everything the ops window shows:
    per-agent stats DERIVED FROM the audit log (so the bars move with
    the feed), and audit events with time / actor / event / business /
    task / result / cost / tokens."""
    registry = BusinessRegistry()
    ledger = Ledger()
    audit = AuditLog()
    biz = registry.create("Ops Store", "ecommerce")
    audit.record("discovery", "run_completed", task_id="task_1",
                 run_id="run_1", business_id=biz.id,
                 tokens_used=120, cost_usd=0.01,
                 result={"tick": 1, "opportunities": 5})
    audit.record("discovery", "task_verified", task_id="task_1",
                 run_id="run_1", business_id=biz.id,
                 tokens_used=120, cost_usd=0.01,
                 result={"passed": True, "rework_count": 0})
    audit.record("discovery", "tick_completed", task_id="task_1",
                 business_id=biz.id,
                 result={"winner": "Ops Store", "score": 87})
    agent_registry = AgentRegistry()
    agent_registry.register("discovery", capabilities=["search"],
                            description="d")
    agent_registry.register("research", capabilities=["analyze"],
                            description="r")
    orchestrator = Orchestrator(registry=agent_registry)
    app, client, _store = _operator_app(
        orchestrator=orchestrator, registry=registry,
        ledger=ledger, audit=audit)
    app.state.game["tick"] = 3
    data = client.get("/api/snapshot").json()
    assert data["tick"] == 3
    by_type = {a["type"]: a for a in data["agents"]}
    # One run, two events for it: counted once, cost once.
    assert by_type["discovery"]["tasks_completed"] == 1
    assert by_type["discovery"]["tokens_used"] == 120
    assert by_type["discovery"]["cost_usd"] == 0.01
    assert by_type["discovery"]["last_task"] == "task_verified · 120 tokens"
    assert by_type["research"]["tasks_completed"] == 0
    assert by_type["research"]["last_task"] == ""
    # The tick summary is still in the feed — it just never moves the bars.
    assert len(data["events"]) == 3
    evt = data["events"][0]
    assert set(evt) >= {"ts", "actor", "event", "business_id", "task_id",
                        "result", "cost_usd", "tokens_used"}
    assert evt["actor"] == "discovery"
    assert evt["event"] == "tick_completed"
    assert evt["business_id"] == biz.id
    assert evt["task_id"] == "task_1"
    assert evt["result"]["winner"] == "Ops Store"


def test_api_snapshot_includes_event_cost():
    """GET /api/snapshot carries each event's cost + tokens, so the ops
    feed can show what a run spent — and $0 / 0 tokens for old
    cost-less events, which the feed then shows nothing for."""
    registry = BusinessRegistry()
    ledger = Ledger()
    audit = AuditLog()
    biz = registry.create("Cost Store", "ecommerce")
    audit.record("discovery", "run_completed", task_id="task_1",
                 business_id=biz.id, tokens_used=1240, cost_usd=0.02,
                 result={"tick": 1, "opportunities": 5})
    audit.record("worker", "tick_completed", business_id=biz.id,
                 result={"tick": 1, "winner": "Cost Store", "score": 80})
    agent_registry = AgentRegistry()
    agent_registry.register("discovery", capabilities=["search"],
                            description="d")
    orchestrator = Orchestrator(registry=agent_registry)
    app, client, _store = _operator_app(
        orchestrator=orchestrator, registry=registry,
        ledger=ledger, audit=audit)
    data = client.get("/api/snapshot").json()
    by_event = {e["event"]: e for e in data["events"]}
    assert by_event["run_completed"]["cost_usd"] == 0.02
    assert by_event["run_completed"]["tokens_used"] == 1240
    assert by_event["tick_completed"]["cost_usd"] == 0.0
    assert by_event["tick_completed"]["tokens_used"] == 0


def test_create_app_keeps_empty_audit_log():
    """Regression: create_app used to do `audit or AuditLog()`, and an
    empty AuditLog is falsy (__len__ == 0) — so the dashboard silently
    swapped in a second, empty log and never saw the orchestrator's or
    worker's events. The feed showed one event per tick (its own
    tick_completed) while the money piled up elsewhere."""
    audit = AuditLog()
    assert len(audit) == 0
    app, client, _store = _operator_app(
        registry=BusinessRegistry(), ledger=Ledger(), audit=audit)
    audit.record("discovery", "run_completed", tokens_used=10,
                 cost_usd=0.01)
    data = client.get("/api/snapshot").json()
    assert len(data["events"]) == 1
    assert data["events"][0]["event"] == "run_completed"


if __name__ == "__main__":
    test_dashboard()
    test_api_report()
    test_api_report_experiment_shape()
    test_report_page()
    test_game_redirects_to_live_ops()
    test_api_snapshot_feeds_live_ops()
    test_api_snapshot_includes_event_cost()
    test_create_app_keeps_empty_audit_log()
    print("Report tests passed.")


def test_live_ops_is_ventoryn_and_professional():
    """The live-ops page is Ventoryn now (not Ecosystem Tycoon), and
    the professional Overview is thumb-friendly through the platform
    shell: the analyze fields keep 44px+ tap targets, the sidebar
    collapses behind a 44px toggle on a phone, and there is no crew
    carousel, no game HUD, and no runtime controls on the page.
    (The websites section has its own, unrelated scroll-snap
    carousel — that one's expected.) The live-ops doc moved with
    the name."""
    app, client, _store = _operator_app(
        registry=BusinessRegistry(), ledger=Ledger())
    r = client.get("/ops")
    assert r.status_code == 200
    assert "Overview — Ventoryn" in r.text
    assert "Ecosystem Tycoon" not in r.text
    assert "Your crew" not in r.text
    assert 'id="crew-prev"' not in r.text
    assert "What the team is doing" not in r.text
    # Thumb-friendly: the analyze fields and the sidebar toggle.
    assert "min-height: 44px" in r.text
    # The live-ops doc moved with the name.
    root = Path(__file__).resolve().parent.parent
    doc = root / "docs" / "ventoryn-live-ops.md"
    assert doc.exists()
    assert doc.read_text(encoding="utf-8").splitlines()[0] == "# Ventoryn — Live Ops"

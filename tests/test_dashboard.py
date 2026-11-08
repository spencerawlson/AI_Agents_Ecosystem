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
    assert "first 100 paying customers" in r.text
    assert "Sign in with Google" in r.text
    assert "$20/month" in r.text

    r = client.get("/portfolio")
    assert r.status_code == 200
    assert "Test Store" in r.text
    assert "$800.00" in r.text  # net profit
    assert "/ops" in r.text  # Live Ops link, not the old "game" tag
    assert "Live Ops" in r.text
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
    assert "/ops" in r.text  # link to the Live Ops view
    assert "Live Ops" in r.text


def test_game_view_is_live_ops():
    """/game serves the Ventoryn — Live Ops page with a vector
    cast (one inline SVG per character, no emoji in the roster) and the
    live ops window; /ops is an alias for the same page."""
    app, client, _store = _operator_app(
        registry=BusinessRegistry(), ledger=Ledger())
    for path in ("/game", "/ops"):
        r = client.get(path)
        assert r.status_code == 200
        assert "Ventoryn — Live Ops" in r.text
        assert "Live game view" not in r.text
        assert "What the team is doing" in r.text
        assert 'id="ops-feed"' in r.text
        assert 'id="ops-bars"' in r.text
        assert 'id="ops-activity"' in r.text
        assert 'id="ops-status"' in r.text
        assert "setInterval" in r.text and "5000" in r.text
        # The ops feed shows what a run spent (class + formatter).
        assert "ops-cost" in r.text
        assert "fmtCost" in r.text
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
    test_game_view_is_live_ops()
    test_api_snapshot_feeds_live_ops()
    test_api_snapshot_includes_event_cost()
    test_create_app_keeps_empty_audit_log()
    print("Report tests passed.")


def test_live_ops_is_ventoryn_and_mobile_friendly():
    """The live-ops page is Ventoryn now (not Ecosystem Tycoon), and its
    phone layout is thumb-friendly: the crew section becomes a
    one-card-at-a-time scroll-snap carousel at <=560px (native swipe,
    prev/next buttons, dots + "n of 8"), the ops charts size their SVG
    viewBox to the panel on screen (so labels don't shrink to ~6px on
    a phone), and the footer controls keep 44px+ tap targets. The
    desktop crew grid is untouched."""
    app, client, _store = _operator_app(
        registry=BusinessRegistry(), ledger=Ledger())
    r = client.get("/ops")
    assert r.status_code == 200
    assert "Ventoryn — Live Ops" in r.text
    assert "Ecosystem Tycoon" not in r.text
    # Desktop crew grid untouched; the old single-column rule is gone,
    # replaced by the phone carousel strip.
    assert "#stations { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }" in r.text
    assert "#stations { grid-template-columns: 1fr; }" not in r.text
    # Carousel: scroll-snap strip, one card per view, buttons, dots,
    # position indicator, and the JS that drives them.
    assert "scroll-snap-type: x mandatory" in r.text
    assert "scroll-snap-align: start" in r.text
    assert 'id="crew-prev"' in r.text
    assert 'id="crew-next"' in r.text
    assert 'id="crew-dots"' in r.text
    assert 'id="crew-pos"' in r.text
    assert "1 of 8" in r.text
    assert "buildCrewNav" in r.text
    assert "crewScrollTo" in r.text
    # Ops charts measure the panel instead of a fixed 560-unit viewBox.
    assert "wrap.clientWidth" in r.text
    # Footer controls stay thumb-friendly.
    assert "min-height: 48px" in r.text
    # The live-ops doc moved with the name.
    root = Path(__file__).resolve().parent.parent
    doc = root / "docs" / "ventoryn-live-ops.md"
    assert doc.exists()
    assert doc.read_text().splitlines()[0] == "# Ventoryn — Live Ops"
    assert not (root / "docs" / "tycoon-live-ops.md").exists()
    assert not (root / "docs" / "collegium-live-ops.md").exists()

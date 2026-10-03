"""Hardening tests: XSS, budgets, failure modes."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from agents.base import BaseAgent
from core.registry import BusinessRegistry
from dashboard.app import create_app
from orchestrator.engine import Orchestrator
from orchestrator.models import Task, TaskStatus
from orchestrator.registry import AgentRegistry


class TrackingAgent(BaseAgent):
    agent_type = "tracker"

    def run(self, task: Task) -> dict:
        self.record_usage(task, tokens=1234, cost_usd=0.05)
        return {"ok": True}


class ExplodingAgent(BaseAgent):
    agent_type = "exploder"

    def run(self, task: Task) -> dict:
        raise RuntimeError("boom")


def _orchestrator_with(agent):
    reg = AgentRegistry()
    reg.register(agent.agent_type, capabilities=[])
    orch = Orchestrator(registry=reg)
    orch.register_handler(agent.agent_type, agent)
    return orch


def test_run_records_usage():
    orch = _orchestrator_with(TrackingAgent())
    task = orch.submit("tracker", budget_usd=1.0, budget_tokens=5000)
    run = orch.dispatch(task.id)
    assert run.status == TaskStatus.COMPLETED
    assert run.tokens_used == 1234
    assert run.cost_usd == 0.05


def test_failed_run_captured():
    orch = _orchestrator_with(ExplodingAgent())
    task = orch.submit("exploder")
    run = orch.dispatch(task.id)
    assert run.status == TaskStatus.FAILED
    assert run.error == "boom"
    assert orch.store.get_task(task.id).status == TaskStatus.FAILED


def test_negative_budget_rejected():
    orch = _orchestrator_with(TrackingAgent())
    try:
        orch.submit("tracker", budget_usd=-1.0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for negative budget")


def test_double_dispatch_rejected():
    orch = _orchestrator_with(TrackingAgent())
    task = orch.submit("tracker")
    orch.dispatch(task.id)
    try:
        orch.dispatch(task.id)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for double dispatch")


def test_dashboard_escapes_xss():
    registry = BusinessRegistry()
    biz = registry.create('<script>alert("xss")</script>', "ecommerce")
    app = create_app(registry=registry)
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "<script>" not in r.text
    assert "&lt;script&gt;" in r.text
    r = client.get(f"/businesses/{biz.id}")
    assert "<script>" not in r.text


if __name__ == "__main__":
    test_run_records_usage()
    test_failed_run_captured()
    test_negative_budget_rejected()
    test_double_dispatch_rejected()
    test_dashboard_escapes_xss()
    print("All hardening tests passed.")
"""End-to-end test: registry → orchestrator → agent → approvals → audit."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.base import BaseAgent, BudgetExceeded
from core.audit import AuditLog
from orchestrator.approvals import ApprovalGate
from orchestrator.engine import Orchestrator
from orchestrator.models import ApprovalStatus, Task, TaskStatus
from orchestrator.registry import AgentRegistry


class DiscoveryAgent(BaseAgent):
    agent_type = "discovery"
    capabilities = ["niche_discovery", "trend_detection"]
    description = "Finds business opportunities."

    def run(self, task: Task) -> dict:
        self.record_usage(task, tokens=500, cost_usd=0.02)
        return {
            "opportunities": [
                {"niche": "ergonomic desk accessories", "demand_score": 0.82},
                {"niche": "print-on-demand pet portraits", "demand_score": 0.74},
            ]
        }


class SpendyAgent(BaseAgent):
    agent_type = "spendy"

    def run(self, task: Task) -> dict:
        self.record_usage(task, tokens=10_000, cost_usd=5.00)
        return {}


def test_registry():
    reg = AgentRegistry()
    reg.register("discovery", capabilities=["niche_discovery"], description="d")
    assert reg.resolve("niche_discovery") == ["discovery"]
    assert reg.resolve("nope") == []
    assert reg.get("discovery").description == "d"


def test_orchestrator_dispatch():
    reg = AgentRegistry()
    reg.register("discovery", capabilities=["niche_discovery"])
    orch = Orchestrator(registry=reg)
    orch.register_handler("discovery", DiscoveryAgent())

    task = orch.submit("discovery", inputs={"market": "us"}, budget_usd=1.0, budget_tokens=2000)
    assert task.status == TaskStatus.PENDING

    run = orch.dispatch(task.id)
    assert run.status == TaskStatus.COMPLETED
    assert len(run.output["opportunities"]) == 2
    assert orch.store.get_task(task.id).status == TaskStatus.COMPLETED


def test_budget_enforcement():
    agent = SpendyAgent()
    task = Task(agent_type="spendy", budget_usd=1.0, budget_tokens=100)
    try:
        agent(task)
    except BudgetExceeded:
        return
    raise AssertionError("expected BudgetExceeded")


def test_approval_gates():
    gate = ApprovalGate()
    assert gate.requires_approval("ad_spend_increase", 30.0) is False
    assert gate.requires_approval("ad_spend_increase", 100.0) is True
    assert gate.requires_secondary("ad_spend_increase", 300.0) is True
    assert gate.requires_approval("delete_business") is True

    appr = gate.request("ad_spend_increase", amount_usd=100.0, business_id="biz_1")
    assert appr.status == ApprovalStatus.PENDING
    gate.decide(appr.id, approved=True, decided_by="spencer")
    assert gate.get(appr.id).status == ApprovalStatus.APPROVED


def test_audit_log():
    log = AuditLog()
    log.record(
        agent_type="discovery",
        event="task_completed",
        business_id="biz_1",
        cost_usd=0.02,
        tokens_used=500,
    )
    assert len(log) == 1
    assert log.total_ai_cost("biz_1") == 0.02
    assert log.total_ai_cost("other") == 0.0


if __name__ == "__main__":
    test_registry()
    test_orchestrator_dispatch()
    test_budget_enforcement()
    test_approval_gates()
    test_audit_log()
    print("All orchestrator + agent framework tests passed.")

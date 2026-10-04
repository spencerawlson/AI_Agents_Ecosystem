"""Tests for the five-layer verification framework (quintuple check)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel

from agents.base import BaseAgent
from agents.review.agent import ReviewAgent
from core.audit import AuditLog
from core.quality import (
    CheckLayer,
    register_contract,
    register_validator,
    verify_output,
)
from orchestrator.engine import Orchestrator
from orchestrator.models import Task, TaskStatus
from orchestrator.registry import AgentRegistry


class GoodAgent(BaseAgent):
    agent_type = "good"

    def run(self, task: Task) -> dict:
        return {"opportunities": [{"niche": "x"}, {"niche": "y"}, {"niche": "z"}]}


class SloppyAgent(BaseAgent):
    """Ignores rework feedback; always returns thin output."""
    agent_type = "sloppy"

    def run(self, task: Task) -> dict:
        return {"note": "TODO fill in later"}


class LearningAgent(BaseAgent):
    """Fixes its output once it sees rework feedback."""
    agent_type = "learning"

    def run(self, task: Task) -> dict:
        if "_rework_feedback" in task.inputs:
            return {"opportunities": [{"niche": "a"}, {"niche": "b"}, {"niche": "c"}],
                    "summary": "three vetted opportunities with demand scores"}
        return {"note": "draft"}


class SelfCriticalAgent(BaseAgent):
    agent_type = "selfcritical"

    def run(self, task: Task) -> dict:
        return {"result": "ok"}

    def self_check(self, task: Task, output: dict) -> list[str]:
        return ["self_check: result lacks supporting detail"]


def _orch(audit=None):
    reg = AgentRegistry()
    orch = Orchestrator(registry=reg, audit=audit)
    return reg, orch


def test_vacuous_pass_keeps_existing_behavior():
    reg, orch = _orch()
    reg.register("good", capabilities=[])
    orch.register_handler("good", GoodAgent())
    task = orch.submit("good")
    run = orch.dispatch(task.id)
    assert run.status == TaskStatus.COMPLETED
    assert run.verification is not None
    assert run.verification["passed"] is True
    assert [l["layer"] for l in run.verification["layers"]] == [l.value for l in CheckLayer]
    assert run.rework_count == 0


def test_peer_review_catches_thin_output_and_rework_exhausts():
    reg, orch = _orch()
    reg.register("sloppy", capabilities=[])
    reg.register("review", capabilities=["peer_review"])
    orch.register_handler("sloppy", SloppyAgent())
    orch.register_handler("review", ReviewAgent())
    task = orch.submit("sloppy", max_rework=1)
    run = orch.dispatch(task.id)
    assert run.status == TaskStatus.FAILED
    assert run.rework_count == 1
    assert task.rework_count == 1
    assert "_rework_feedback" in orch.store.get_task(task.id).inputs
    peer = [l for l in run.verification["layers"] if l["layer"] == "peer_review"][0]
    assert peer["passed"] is False
    assert any("placeholder" in f for f in peer["findings"])


def test_rework_loop_recovers_when_agent_fixes_output():
    reg, orch = _orch()
    reg.register("learning", capabilities=[])
    reg.register("review", capabilities=["peer_review"])
    orch.register_handler("learning", LearningAgent())
    orch.register_handler("review", ReviewAgent())
    task = orch.submit(
        "learning",
        acceptance_criteria=["output includes three opportunities with demand scores"],
        max_rework=2,
    )
    run = orch.dispatch(task.id)
    assert run.status == TaskStatus.COMPLETED
    assert run.rework_count == 1
    assert run.verification["passed"] is True


def test_self_check_failure_blocks_completion():
    reg, orch = _orch()
    reg.register("selfcritical", capabilities=[])
    orch.register_handler("selfcritical", SelfCriticalAgent())
    task = orch.submit("selfcritical", max_rework=0)
    run = orch.dispatch(task.id)
    assert run.status == TaskStatus.FAILED
    layer1 = [l for l in run.verification["layers"] if l["layer"] == "self_check"][0]
    assert layer1["passed"] is False


def test_contract_validation():
    class Out(BaseModel):
        opportunities: list[dict]

    register_contract("good", Out)
    try:
        task = Task(agent_type="good", acceptance_criteria=[])
        ok = verify_output(task, {"opportunities": [{"niche": "x"}]})
        assert [l for l in ok.layers if l.layer == CheckLayer.CONTRACT][0].passed
        bad = verify_output(task, {"wrong": 1})
        assert not [l for l in bad.layers if l.layer == CheckLayer.CONTRACT][0].passed
        assert bad.passed is False
    finally:
        from core.quality import _contracts
        del _contracts["good"]


def test_deterministic_validator():
    def at_least_three(task, output):
        opps = output.get("opportunities", [])
        return [] if len(opps) >= 3 else [f"only {len(opps)} opportunities, need 3"]

    register_validator("good", at_least_three)
    try:
        task = Task(agent_type="good")
        assert verify_output(task, {"opportunities": [1, 2, 3]}).passed
        report = verify_output(task, {"opportunities": [1]})
        assert not report.passed
        det = [l for l in report.layers if l.layer == CheckLayer.DETERMINISTIC][0]
        assert det.findings == ["only 1 opportunities, need 3"]
    finally:
        from core.quality import _validators
        del _validators["good"]


def test_verification_events_audited():
    audit = AuditLog()
    reg, orch = _orch(audit=audit)
    reg.register("good", capabilities=[])
    orch.register_handler("good", GoodAgent())
    task = orch.submit("good")
    orch.dispatch(task.id)
    events = [e.event for e in audit.for_task(task.id)]
    assert "task_verified" in events


def test_review_agent_standalone():
    agent = ReviewAgent()
    task = Task(agent_type="x", acceptance_criteria=["output includes revenue forecast"])
    issues = agent.review(task, {"note": "draft"})
    assert any("revenue forecast" in i for i in issues)
    clean = agent.review(task, {"revenue_forecast": {"q1": 1000}, "notes": "done"})
    assert clean == []

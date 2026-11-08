"""Tasks & spend per agent bars are derived from the audit log.

The bug: the bars read game["agent_stats"], an in-memory dict filled
only by api_tick, only on a successful tick, only for discovery +
research — so the other five agents could never move, a failed tick
left the bars at zero, and a restart wiped them, while the feed (audit
log) and the header (ledger) kept showing the real work and spend.
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.base import BaseAgent
from core.audit import AuditLog
from core.state_store import SharedAuditLog
from dashboard.app import agent_stats_from_audit, create_app
from orchestrator.engine import Orchestrator
from orchestrator.models import TaskStatus
from orchestrator.registry import AgentRegistry

SEVEN = ["creative", "discovery", "marketing", "operations",
         "research", "review", "support"]


def _registry(*types):
    reg = AgentRegistry()
    for t in types:
        reg.register(t, capabilities=[f"cap_{t}"], description=t)
    return reg


def _snapshot(audit, *types):
    orch = Orchestrator(registry=_registry(*types))
    app = create_app(orchestrator=orch, audit=audit)
    return TestClient(app).get("/api/snapshot").json()


def _by_type(rows):
    return {r["type"]: r for r in rows}


# -- bars from the audit log ----------------------------------------------


def test_bars_from_audit_log():
    audit = AuditLog()
    audit.record("discovery", "run_completed", task_id="task_1",
                 run_id="run_1", tokens_used=1200, cost_usd=0.02,
                 result={"tick": 1, "opportunities": 5})
    audit.record("research", "run_completed", task_id="task_2",
                 run_id="run_2", tokens_used=800, cost_usd=0.01,
                 result={"tick": 1, "reports": 3})
    audit.record("worker", "tick_completed", business_id="biz_1",
                 result={"tick": 1, "winner": "biz_1", "score": 80})
    data = _snapshot(audit, "discovery", "research")
    by_type = _by_type(data["agents"])
    assert by_type["discovery"]["tasks_completed"] == 1
    assert by_type["discovery"]["tokens_used"] == 1200
    assert by_type["discovery"]["cost_usd"] == 0.02
    assert by_type["discovery"]["last_task"] == "run_completed · 1,200 tokens"
    assert by_type["research"]["tasks_completed"] == 1
    assert by_type["research"]["tokens_used"] == 800
    assert by_type["research"]["cost_usd"] == 0.01
    # Snapshot shape is unchanged for game.html.
    assert set(by_type["discovery"]) == {"type", "tasks_completed",
                                         "tokens_used", "cost_usd",
                                         "last_task"}


def test_bars_dedupe_verified_and_run_completed():
    """One run, two events for it (orchestrator + worker): one task,
    cost and tokens counted once."""
    audit = AuditLog()
    audit.record("discovery", "task_verified", task_id="task_1",
                 run_id="run_1", tokens_used=2707, cost_usd=0.0012,
                 result={"passed": True, "rework_count": 0})
    audit.record("discovery", "run_completed", task_id="task_1",
                 run_id="run_1", tokens_used=2707, cost_usd=0.0012,
                 result={"tick": 1, "opportunities": 5})
    rows = agent_stats_from_audit(audit, ["discovery"])
    assert rows[0]["tasks_completed"] == 1
    assert rows[0]["tokens_used"] == 2707
    assert rows[0]["cost_usd"] == 0.0012
    assert rows[0]["last_task"] == "run_completed · 2,707 tokens"


def test_bars_failed_run_counts_spend_not_task():
    """A failed run spent money but completed nothing."""
    audit = AuditLog()
    audit.record("discovery", "run_failed", task_id="task_1",
                 run_id="run_1", tokens_used=500, cost_usd=0.03,
                 error="boom", result={"tick": 1})
    audit.record("discovery", "task_verified", task_id="task_1",
                 run_id="run_1", tokens_used=500, cost_usd=0.03,
                 result={"passed": False, "failures": ["bad output"]})
    rows = agent_stats_from_audit(audit, ["discovery"])
    assert rows[0]["tasks_completed"] == 0
    assert rows[0]["tokens_used"] == 500
    assert rows[0]["cost_usd"] == 0.03
    assert rows[0]["last_task"] == "task_verified · 500 tokens"


def test_bars_rework_then_verified_counts_once():
    """A reworked attempt and the successful retry are different runs:
    both attempts' spend counts, but only one task completes."""
    audit = AuditLog()
    audit.record("creative", "task_rework_requested", task_id="task_1",
                 run_id="run_1", tokens_used=300, cost_usd=0.01,
                 result={"attempt": 1, "failures": ["thin copy"]})
    audit.record("creative", "task_verified", task_id="task_1",
                 run_id="run_2", tokens_used=600, cost_usd=0.02,
                 result={"passed": True, "rework_count": 1})
    audit.record("creative", "run_completed", task_id="task_1",
                 run_id="run_2", tokens_used=600, cost_usd=0.02,
                 result={"tick": 1})
    rows = agent_stats_from_audit(audit, ["creative"])
    assert rows[0]["tasks_completed"] == 1
    assert rows[0]["tokens_used"] == 900
    assert rows[0]["cost_usd"] == pytest.approx(0.03)


def test_bars_ignore_summary_events():
    """tick_completed / growth_cycle / stop-loss summaries carry no run
    and must never move the bars — even one with a task_id."""
    audit = AuditLog()
    audit.record("discovery", "tick_completed", task_id="task_1",
                 result={"winner": "biz_1", "score": 87})
    audit.record("road2cissp_growth", "growth_cycle",
                 result={"jobs": 3})
    audit.record("etsy_monitor", "stop_loss_triggered",
                 result={"triggered": False})
    audit.record("discovery", "run_completed",
                 tokens_used=100, cost_usd=0.01)
    rows = agent_stats_from_audit(audit, ["discovery"])
    assert rows[0]["tasks_completed"] == 0
    assert rows[0]["tokens_used"] == 0
    assert rows[0]["cost_usd"] == 0.0
    assert rows[0]["last_task"] == ""


def test_bars_all_seven_types_present_with_zeros():
    from ecosystem.runtime import AGENT_CLASSES

    types = [c.agent_type for c in AGENT_CLASSES]
    assert sorted(types) == SEVEN
    audit = AuditLog()
    rows = agent_stats_from_audit(audit, sorted(types))
    assert [r["type"] for r in rows] == SEVEN
    for r in rows:
        assert r["tasks_completed"] == 0
        assert r["tokens_used"] == 0
        assert r["cost_usd"] == 0.0
        assert r["last_task"] == ""


def test_bars_memory_and_shared_agree(tmp_path):
    """Same events in an in-memory log and in the shared DB give the
    same bars."""
    audit = AuditLog()
    audit.record("discovery", "task_verified", task_id="t1", run_id="r1",
                 tokens_used=2707, cost_usd=0.0012,
                 result={"passed": True})
    audit.record("discovery", "run_completed", task_id="t1", run_id="r1",
                 tokens_used=2707, cost_usd=0.0012, result={"tick": 1})
    audit.record("marketing", "run_failed", task_id="t2", run_id="r2",
                 tokens_used=600, cost_usd=0.01, error="boom")
    shared = SharedAuditLog(tmp_path / "state.db")
    for e in audit.all_events():
        shared.record(e.agent_type, e.event, task_id=e.task_id,
                      run_id=e.run_id, business_id=e.business_id,
                      result=e.result, error=e.error,
                      tokens_used=e.tokens_used, cost_usd=e.cost_usd)
    assert (agent_stats_from_audit(shared, ["discovery", "marketing"])
            == agent_stats_from_audit(audit, ["discovery", "marketing"]))


def test_bars_persist_across_restarts_shared_db(tmp_path, monkeypatch):
    """A second runtime on the same ECOSYSTEM_STATE_DB sees the same
    bars — the bars survive a dashboard restart, like the feed."""
    from ecosystem.runtime import build_runtime

    monkeypatch.setenv("ECOSYSTEM_STATE_DB", str(tmp_path / "state.db"))
    rt1 = build_runtime()
    rt1.audit.record("discovery", "run_completed", task_id="t1",
                     run_id="r1", tokens_used=100, cost_usd=0.02,
                     result={"tick": 1})
    rt1.audit.record("marketing", "task_verified", task_id="t2",
                     run_id="r2", tokens_used=600, cost_usd=0.01,
                     result={"passed": True})
    types = ["discovery", "marketing"]
    first = agent_stats_from_audit(rt1.audit, types)
    rt2 = build_runtime()
    second = agent_stats_from_audit(rt2.audit, types)
    assert second == first
    by_type = _by_type(second)
    assert by_type["discovery"]["tasks_completed"] == 1
    assert by_type["marketing"]["tasks_completed"] == 1
    assert by_type["marketing"]["cost_usd"] == 0.01


def test_bars_move_on_a_real_tick(monkeypatch):
    """A real tick (heuristic agents, no key, no network) moves the
    bars for discovery and research — and the bars agree with the
    run_completed events in the log."""
    from ecosystem.runtime import build_runtime
    from ecosystem.worker import run_tick

    monkeypatch.delenv("ECOSYSTEM_STATE_DB", raising=False)
    rt = build_runtime()
    result = run_tick(rt, 1)
    assert result["ok"] is True
    rows = _by_type(agent_stats_from_audit(
        rt.audit, ["discovery", "research"]))
    runs = {e.agent_type: e for e in rt.audit.all_events()
            if e.event == "run_completed"}
    for agent in ("discovery", "research"):
        assert rows[agent]["tasks_completed"] == 1
        assert rows[agent]["tokens_used"] == runs[agent].tokens_used
        assert rows[agent]["cost_usd"] == runs[agent].cost_usd


# -- the engine must carry the spend on failed runs -----------------------


class FailingAgent(BaseAgent):
    agent_type = "creative"
    capabilities = ["draft"]
    description = "Always fails its own check."

    def run(self, task):
        self.record_usage(task, tokens=100, cost_usd=0.01)
        return {}

    def self_check(self, task, output):
        return ["output is empty"]


def test_failed_verification_events_carry_spend():
    """A run that fails verification still spent money — the rework
    and failed-verified events must carry its tokens/cost, or the bars
    could never count failed spend."""
    log = AuditLog()
    orch = Orchestrator(registry=_registry("creative"), audit=log)
    orch.register_handler("creative", FailingAgent())
    task = orch.submit("creative", inputs={}, budget_usd=1.0,
                       budget_tokens=5000)
    run = orch.dispatch(task.id)
    assert run.status == TaskStatus.FAILED
    reworks = [e for e in log.all_events()
               if e.event == "task_rework_requested"]
    assert len(reworks) == 2  # max_rework=2: two reworks, then failure
    assert all(e.tokens_used == 100 and e.cost_usd == 0.01
               for e in reworks)
    failed = [e for e in log.all_events() if e.event == "task_verified"]
    assert len(failed) == 1
    assert failed[0].result["passed"] is False
    assert failed[0].tokens_used == 100
    assert failed[0].cost_usd == 0.01
    rows = agent_stats_from_audit(log, ["creative"])
    assert rows[0]["tasks_completed"] == 0
    assert rows[0]["tokens_used"] == 300
    assert rows[0]["cost_usd"] == pytest.approx(0.03)

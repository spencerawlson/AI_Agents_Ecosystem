"""Audit log persistence + per-run events.

The bug: the ledger survived restarts (shared SQLite) but the audit
log was purely in-memory, so the dashboard feed went empty while the
money stayed — and a tick only ever recorded one summary event, so
the feed could never say what any single agent did or spent.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.audit import AuditLog
from core.state_store import SharedAuditLog
from orchestrator.models import TaskStatus


def _record_some(log):
    log.record("discovery", "run_completed", business_id="biz_1",
               tokens_used=1200, cost_usd=0.02,
               result={"tick": 1, "opportunities": 5})
    log.record("research", "run_completed", business_id="biz_1",
               tokens_used=800, cost_usd=0.01,
               result={"tick": 1, "reports": 3})
    log.record("worker", "tick_completed", business_id="biz_1",
               result={"tick": 1, "winner": "biz_1", "score": 80})


# -- SharedAuditLog -----------------------------------------------------------

def test_shared_audit_semantics_match_memory(tmp_path):
    db = tmp_path / "state.db"
    shared = SharedAuditLog(db)
    memory = AuditLog()
    _record_some(shared)
    _record_some(memory)
    assert len(shared) == len(memory) == 3
    assert [e.event for e in shared.latest(10)] == \
        [e.event for e in memory.latest(10)]
    assert [e.event for e in shared.all_events()] == \
        [e.event for e in memory.all_events()]
    assert [e.event for e in shared.for_business("biz_1")] == \
        [e.event for e in memory.for_business("biz_1")]
    assert [e.agent_type for e in shared.for_business("biz_1")] == \
        [e.agent_type for e in memory.for_business("biz_1")]
    assert shared.total_ai_cost() == memory.total_ai_cost() == \
        pytest.approx(0.03)
    assert shared.total_ai_cost("biz_1") == pytest.approx(0.03)
    assert shared.total_ai_cost("biz_nope") == 0.0


def test_shared_audit_persists_across_instances(tmp_path):
    db = tmp_path / "state.db"
    first = SharedAuditLog(db)
    _record_some(first)
    # A second instance = a second process (dashboard vs worker).
    second = SharedAuditLog(db)
    assert len(second) == 3
    assert second.latest(1)[0].event == "tick_completed"
    assert second.latest(1)[0].result["winner"] == "biz_1"
    # ... and the first instance sees what the second records,
    # with no restart: reads go to the database.
    second.record("support", "run_completed", tokens_used=10, cost_usd=0.0)
    assert len(first) == 4
    assert first.latest(1)[0].agent_type == "support"


def test_shared_audit_cap_500(tmp_path):
    db = tmp_path / "state.db"
    log = SharedAuditLog(db)
    for i in range(505):
        log.record("discovery", "run_completed", result={"tick": i})
    assert len(log) == 500
    assert log.latest(500)[0].result == {"tick": 504}
    assert log.latest(500)[-1].result == {"tick": 5}
    # A fresh instance sees the same capped history.
    assert len(SharedAuditLog(db)) == 500


def test_runtime_uses_shared_audit_only_when_configured(tmp_path,
                                                        monkeypatch):
    from ecosystem.runtime import build_runtime

    db = str(tmp_path / "eco.db")
    monkeypatch.setenv("ECOSYSTEM_STATE_DB", db)
    rt1 = build_runtime()
    assert isinstance(rt1.audit, SharedAuditLog)
    rt1.audit.record("discovery", "run_completed",
                     tokens_used=50, cost_usd=0.005)
    rt2 = build_runtime()  # a second process's runtime
    assert len(rt2.audit) == 1
    assert rt2.audit.latest(1)[0].event == "run_completed"

    monkeypatch.delenv("ECOSYSTEM_STATE_DB")
    rt3 = build_runtime()
    rt4 = build_runtime()
    assert not isinstance(rt3.audit, SharedAuditLog)
    rt3.audit.record("discovery", "run_completed")
    assert len(rt4.audit) == 0  # in-memory stays private


# -- per-run events in run_tick -----------------------------------------------

def test_run_tick_records_per_run_events(monkeypatch):
    """A real tick (heuristic agents, no key, no network) records one
    event per agent run — with tokens and cost — plus the tick summary."""
    from ecosystem.runtime import build_runtime
    from ecosystem.worker import run_tick

    monkeypatch.delenv("ECOSYSTEM_STATE_DB", raising=False)
    rt = build_runtime()
    result = run_tick(rt, 1)
    assert result["ok"] is True
    events = rt.audit.latest(10)
    runs = [e for e in events if e.event == "run_completed"]
    assert {e.agent_type for e in runs} == {"discovery", "research"}
    by_agent = {e.agent_type: e for e in runs}
    assert by_agent["discovery"].result["tick"] == 1
    assert by_agent["discovery"].result["opportunities"] == \
        result["opportunities"]
    assert by_agent["research"].result["tick"] == 1
    assert isinstance(by_agent["research"].result["reports"], int)
    assert by_agent["research"].result["reports"] >= 0
    for e in runs:
        assert e.tokens_used >= 0
        assert e.cost_usd >= 0.0
    # The tick summary is still there, once.
    summaries = [e for e in events
                 if e.event == "tick_completed" and e.agent_type == "worker"]
    assert len(summaries) == 1
    assert summaries[0].result["winner"] == result["winner"]


class _StubOrchestrator:
    """Fake orchestrator returning canned runs, in dispatch order."""

    def __init__(self, runs):
        self._runs = runs
        self._i = 0

    def submit(self, agent_type, inputs=None, budget_usd=None,
               budget_tokens=None):
        return SimpleNamespace(id=f"task_{agent_type}")

    def dispatch(self, task_id):
        run = self._runs[self._i]
        self._i += 1
        return run


def _failed_run(agent_type, error="boom"):
    return SimpleNamespace(
        id=f"run_{agent_type}", task_id=f"task_{agent_type}",
        agent_type=agent_type, status=TaskStatus.FAILED,
        tokens_used=10, cost_usd=0.01, error=error, output=None)


def _ok_run(agent_type, output):
    return SimpleNamespace(
        id=f"run_{agent_type}", task_id=f"task_{agent_type}",
        agent_type=agent_type, status=TaskStatus.COMPLETED,
        tokens_used=100, cost_usd=0.02, error=None, output=output)


def test_run_tick_records_failed_discovery_run(monkeypatch):
    from ecosystem.runtime import build_runtime
    from ecosystem.worker import run_tick

    monkeypatch.delenv("ECOSYSTEM_STATE_DB", raising=False)
    rt = build_runtime()
    rt.orchestrator = _StubOrchestrator([_failed_run("discovery")])
    result = run_tick(rt, 1)
    assert result["ok"] is False
    evt = rt.audit.latest(1)[0]
    assert evt.event == "run_failed"
    assert evt.agent_type == "discovery"
    assert evt.error == "boom"
    assert evt.tokens_used == 10
    assert evt.cost_usd == 0.01


def test_run_tick_records_failed_research_run(monkeypatch):
    from ecosystem.runtime import build_runtime
    from ecosystem.worker import run_tick

    monkeypatch.delenv("ECOSYSTEM_STATE_DB", raising=False)
    rt = build_runtime()
    rt.orchestrator = _StubOrchestrator([
        _ok_run("discovery", {"opportunities": [{"niche": "x"}]}),
        _failed_run("research", "verification failed"),
    ])
    result = run_tick(rt, 1)
    assert result["ok"] is False
    events = rt.audit.latest(5)
    by_event = {e.event: e for e in events}
    # The discovery run that did complete is recorded too.
    assert by_event["run_completed"].agent_type == "discovery"
    assert by_event["run_completed"].result["opportunities"] == 1
    assert by_event["run_failed"].agent_type == "research"
    assert by_event["run_failed"].error == "verification failed"
